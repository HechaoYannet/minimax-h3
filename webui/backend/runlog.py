"""webui.backend.runlog -- 运行日志系统：结构化、可落盘、可订阅、可在页面里查。

为什么单独做一个模块（而不是继续用 util.Log 那个 40 行的环形缓冲）：

  1. **要能事后查**。以前日志只活在内存里，服务一重启就没了；调 LLM、跑生成出的
     问题往往在「刚才那一次」，重启后最需要的证据恰好消失。所以这里把每条记录
     同时写进 cache/webui/logs/ 下的 JSONL 文件，并按大小轮转。
  2. **要能按来源/等级筛**。日志现在混杂着 HTTP 请求、作业子进程输出、LLM 调用、
     遥测采样，页面上一锅粥看不出重点。每条记录带 source / event / run_id / job 字段，
     前端可以只看 llm，或只看 error。
  3. **要能调试 LLM 与产物**。LLM 每次调用（system、user、流式输出、tokens、耗时、
     报错）单独落一份完整记录，见 LLMRunStore；作业产物也带路径/大小/帧数落到运行日志。
     其中 response.raw 是**解析前**的模型原文 —— 模型没按格式输出时，这是唯一的现场。

设计约束与工程其它部分一致：只用标准库；日志模块本身**永远不能把服务弄挂**，
所以任何写盘/序列化失败都吞掉并记在内部状态里（stats() 里能看到 file_error）。
"""
from __future__ import annotations

import collections
import json
import os
import queue
import threading
import time

from .util import iso, now, tail_lines, write_json

# 日志等级：数值越大越严重。level 阈值以下的记录直接丢弃（不入内存也不写盘）。
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
LEVEL_ORDER = ["debug", "info", "warn", "error"]


def _iso_ms(ts: float | None = None) -> str:
    """带毫秒的本地时间戳；排查时序问题时比秒级好用。"""
    t = time.time() if ts is None else ts
    lt = time.localtime(t)
    return time.strftime("%Y-%m-%d %H:%M:%S", lt) + ".%03d" % int((t % 1) * 1000)


def _clip(value, limit: int):
    """把任意值裁成可安全放进内存/JSON 的形状。"""
    if isinstance(value, str):
        if limit > 0 and len(value) > limit:
            return value[:limit] + f"…(+{len(value) - limit} 字符已截断)"
        return value
    if isinstance(value, dict):
        return {str(k): _clip(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v, limit) for v in value]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    text = repr(value)
    return _clip(text, limit)


def _match(rec: dict, level: str | None, source: str | None, q: str | None,
           since_seq: int = 0, until_seq: int = 0) -> bool:
    if rec.get("seq", 0) <= since_seq:
        return False
    if until_seq and rec.get("seq", 0) > until_seq:
        return False
    if level and level != "all" and LEVELS.get(rec.get("level"), 0) < LEVELS.get(level, 0):
        return False
    if source and source != "all" and rec.get("source") != source:
        return False
    if q:
        needle = q.lower()
        hay = (str(rec.get("msg") or "") + " " + str(rec.get("event") or "") + " "
               + json.dumps(rec, ensure_ascii=False))
        if needle not in hay.lower():
            return False
    return True


# --------------------------------------------------------------------------- RunLog
class RunLog:
    """进程级运行日志：环形缓冲（页面实时流用）+ JSONL 落盘（事后查用）。

    典型的记录长这样：

        {"seq": 12, "t": 1757..., "iso": "2026-09-11 20:31:07.412", "level": "info",
         "source": "llm", "event": "llm.request", "msg": "LLM 请求已发出",
         "run": "20260911-203107-3f9a", "model": "deepseek-flash", "user_chars": 3200}
    """

    def __init__(self, directory: str, *, level: str = "info", capacity: int = 2000,
                 file_enabled: bool = True, max_file_mb: float = 8.0, backups: int = 5,
                 console: bool = True, name: str = "webui", max_field_chars: int = 4000):
        self.dir = os.path.abspath(directory)
        self.name = name
        self.capacity = max(100, int(capacity or 2000))
        self._level = LEVELS.get(str(level or "info").lower(), 20)
        self.file_enabled = bool(file_enabled)
        self.max_file_bytes = max(64 * 1024, int(float(max_file_mb or 8.0) * 1024 * 1024))
        self.backups = max(1, int(backups or 5))
        self.console = bool(console)
        self.max_field_chars = max(200, int(max_field_chars or 4000))

        self._buf: collections.deque = collections.deque(maxlen=self.capacity)
        self._lock = threading.RLock()
        self._subs: list[queue.Queue] = []
        self._seq = 0
        self._evicted = 0
        self._counts = {k: 0 for k in LEVELS}
        self._file_error: str | None = None
        self._fh = None
        self._path = os.path.join(self.dir, f"{name}.jsonl")
        self._bytes = 0
        try:
            os.makedirs(self.dir, exist_ok=True)
            if self.file_enabled:
                self._fh = open(self._path, "a", encoding="utf-8")
                self._bytes = os.path.getsize(self._path)
                self._load_tail()
        except OSError as e:  # 落不了盘也要能跑：内存缓冲仍然可用
            self._file_error = f"{e}"
            self._fh = None

    def _load_tail(self) -> None:
        """启动时把上一个进程写在 webui.jsonl 尾部的日志读回内存。

        这样「重启后页面里还能看到上一次的日志」，也让 seq 从历史最大值继续，
        SSE 的 since_seq 续传仍然单调。文件可能很大/写坏，所以逐行容错。
        """
        if not self.file_enabled or not os.path.exists(self._path):
            return
        try:
            lines = tail_lines(self._path, self.capacity)
        except Exception:
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not isinstance(rec, dict) or "msg" not in rec:
                continue
            self._buf.append(rec)
            lv = rec.get("level")
            if lv in self._counts:
                self._counts[lv] += 1
            try:
                self._seq = max(self._seq, int(rec.get("seq") or 0))
            except (TypeError, ValueError):
                pass

    # ---------------------------------------------------------------- 等级
    @property
    def level(self) -> str:
        for name, val in LEVELS.items():
            if val == self._level:
                return name
        return "info"

    def set_level(self, level: str) -> str:
        self._level = LEVELS.get(str(level or "").lower(), self._level)
        self.info(f"日志等级切换为 {self.level}", source="sys", event="log.level")
        return self.level

    # ---------------------------------------------------------------- 写入
    def emit(self, level: str, msg, *, source: str = "server", event: str | None = None,
             **fields):
        """写一条记录。level 低于阈值时返回 None。"""
        lv = str(level or "info").lower()
        if lv not in LEVELS:
            lv = "info"
        if LEVELS[lv] < self._level:
            return None
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "t": now(), "iso": _iso_ms(), "level": lv,
                   "source": str(source or "server"), "event": event or "", "msg": str(msg)}
            for k, v in fields.items():
                if k in ("seq", "t", "iso", "level", "source", "event", "msg"):
                    continue
                rec[k] = _clip(v, self.max_field_chars)
            self._buf.append(rec)
            self._counts[lv] += 1
            if len(self._buf) == self.capacity:
                self._evicted += 1
            self._write(rec)
            subs = list(self._subs)
        if self.console and LEVELS[lv] >= 20:
            print(self.format_line(rec), flush=True)
        for q in subs:
            try:
                q.put_nowait(rec)
            except Exception:
                pass
        return rec

    def ingest(self, rec: dict) -> dict | None:
        """接收一条「已经成型」的记录（前端上报的异常走这里）。

        只信任少数字段：等级归一化、来源固定为 ui、其余当附加字段。
        """
        if not isinstance(rec, dict):
            return None
        return self.emit(rec.get("level") or "info", rec.get("msg") or "",
                         source=rec.get("source") or "ui",
                         event=rec.get("event") or "ui.event",
                         **{k: v for k, v in rec.items()
                            if k not in ("level", "msg", "source", "event")})

    def debug(self, msg, **kw):
        return self.emit("debug", msg, **kw)

    def info(self, msg, **kw):
        return self.emit("info", msg, **kw)

    def warn(self, msg, **kw):
        return self.emit("warn", msg, **kw)

    def error(self, msg, **kw):
        return self.emit("error", msg, **kw)

    def child(self, **base) -> "BoundLog":
        """带固定上下文的子记录器：作业日志用 log.child(source='job', job=<id>)。"""
        return BoundLog(self, base)

    # ---------------------------------------------------------------- 落盘
    def _write(self, rec: dict) -> None:
        if self._fh is None:
            return
        try:
            line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
            data = line.encode("utf-8", "replace")
            self._fh.write(data.decode("utf-8"))
            self._fh.flush()
            self._bytes += len(data)
            if self._bytes >= self.max_file_bytes:
                self._rotate()
        except Exception as e:  # 只记状态，不抛
            self._file_error = f"{e}"
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def _rotate(self) -> None:
        """webui.jsonl -> webui.1.jsonl -> … -> webui.N.jsonl，超出删除。"""
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None
        try:
            oldest = os.path.join(self.dir, f"{self.name}.{self.backups}.jsonl")
            if os.path.exists(oldest):
                os.remove(oldest)
            for i in range(self.backups - 1, 0, -1):
                src = os.path.join(self.dir, f"{self.name}.{i}.jsonl")
                dst = os.path.join(self.dir, f"{self.name}.{i + 1}.jsonl")
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(self._path):
                os.replace(self._path, os.path.join(self.dir, f"{self.name}.1.jsonl"))
            self._fh = open(self._path, "a", encoding="utf-8")
            self._bytes = 0
        except OSError as e:
            self._file_error = f"rotate failed: {e}"

    def close(self) -> None:
        with self._lock:
            try:
                if self._fh:
                    self._fh.close()
            except Exception:
                pass
            self._fh = None

    # ---------------------------------------------------------------- 读取
    def tail(self, n: int = 200, **filters) -> list[dict]:
        with self._lock:
            items = list(self._buf)
        sel = [r for r in items if _match(r, filters.get("level"), filters.get("source"),
                                         filters.get("q"), int(filters.get("since_seq") or 0),
                                         int(filters.get("until_seq") or 0))]
        return sel[-max(0, int(n)):] if n else sel

    def query(self, *, n: int = 200, offset: int = 0, **filters) -> dict:
        with self._lock:
            items = list(self._buf)
        sel = [r for r in items if _match(r, filters.get("level"), filters.get("source"),
                                         filters.get("q"), int(filters.get("since_seq") or 0),
                                         int(filters.get("until_seq") or 0))]
        total = len(sel)
        offset = max(0, int(offset))
        n = max(1, min(int(n or 200), 5000))
        page = sel[::-1][offset:offset + n]          # 默认从最新往回给
        page.reverse()                               # 页内按时间正序，页面好读
        return {"records": page, "matched": total, "retained": len(items),
                "next_offset": offset + len(page)}

    def snapshot(self, **filters) -> list[dict]:
        return self.tail(0, **filters)

    def matches(self, rec: dict, *, level=None, source=None, q=None,
                since_seq: int = 0) -> bool:
        """判断一条记录是否命中筛选条件；SSE 实时流用它做服务端过滤。"""
        return _match(rec, level, source, q, since_seq)

    def stats(self) -> dict:
        with self._lock:
            items = list(self._buf)
            files = []
            try:
                for name in os.listdir(self.dir):
                    if name.startswith(self.name) and name.endswith(".jsonl"):
                        p = os.path.join(self.dir, name)
                        try:
                            st = os.stat(p)
                            files.append({"name": name, "bytes": st.st_size,
                                          "mtime": iso(st.st_mtime)})
                        except OSError:
                            continue
            except OSError:
                pass
            files.sort(key=lambda f: f["name"], reverse=True)
            return {
                "level": self.level, "capacity": self.capacity, "retained": len(items),
                "evicted": self._evicted, "seq": self._seq,
                "counts": dict(self._counts),
                "sources": sorted({r.get("source") for r in items if r.get("source")}),
                "oldest": items[0]["iso"] if items else None,
                "newest": items[-1]["iso"] if items else None,
                "dir": self.dir, "file": self._path, "file_enabled": self.file_enabled,
                "file_bytes": self._bytes, "file_error": self._file_error,
                "backups": self.backups, "files": files[:12],
                "console": self.console,
            }

    def clear(self, files: bool = False) -> dict:
        """清空内存缓冲；files=True 时同时清空已落盘的日志文件。"""
        with self._lock:
            removed = len(self._buf)
            self._buf.clear()
            self._evicted = 0
            for k in self._counts:
                self._counts[k] = 0
            if files:
                self._close_and_clear_files()
        self.info(f"日志缓冲已清空（{removed} 条）", source="sys", event="log.clear")
        return {"cleared": removed, "files": bool(files)}

    def _close_and_clear_files(self):
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        self._fh = None
        try:
            for name in os.listdir(self.dir):
                if name.startswith(self.name) and name.endswith(".jsonl"):
                    os.remove(os.path.join(self.dir, name))
        except OSError as e:
            self._file_error = f"{e}"
        try:
            self._fh = open(self._path, "a", encoding="utf-8")
            self._bytes = 0
        except OSError as e:
            self._file_error = f"{e}"

    # ---------------------------------------------------------------- 订阅（SSE）
    def subscribe(self) -> "queue.Queue[dict]":
        q: "queue.Queue[dict]" = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    # ---------------------------------------------------------------- 文本
    def format_line(self, rec: dict) -> str:
        lv = str(rec.get("level") or "info").upper().ljust(5)
        parts = [rec.get("iso") or _iso_ms(rec.get("t")), lv,
                 "[" + str(rec.get("source") or "-") + "]"]
        if rec.get("event"):
            parts.append(str(rec["event"]))
        parts.append(str(rec.get("msg") or ""))
        head = " ".join(parts)
        extras = {k: v for k, v in rec.items()
                  if k not in ("seq", "t", "iso", "level", "source", "event", "msg")}
        if extras:
            try:
                head += " | " + json.dumps(extras, ensure_ascii=False, default=str)
            except Exception:
                pass
        return head

    def format_text(self, records: list[dict]) -> str:
        return "\n".join(self.format_line(r) for r in records) + ("\n" if records else "")


class BoundLog:
    """固定了部分上下文字段的记录器；接口与 RunLog 一致。"""

    def __init__(self, parent: RunLog, base: dict):
        self._parent = parent
        self._base = dict(base)

    def _emit(self, level: str, msg, **kw):
        merged = dict(self._base)
        merged.update(kw)
        # source 固定来源优先：child(source='job') 不会被临时 kw 覆盖掉
        if self._base.get("source") and "source" not in kw:
            merged["source"] = self._base["source"]
        return self._parent.emit(level, msg, **merged)

    def debug(self, msg, **kw):
        return self._emit("debug", msg, **kw)

    def info(self, msg, **kw):
        return self._emit("info", msg, **kw)

    def warn(self, msg, **kw):
        return self._emit("warn", msg, **kw)

    def error(self, msg, **kw):
        return self._emit("error", msg, **kw)

    def child(self, **base) -> "BoundLog":
        merged = dict(self._base)
        merged.update(base)
        return BoundLog(self._parent, merged)

    @property
    def parent(self) -> RunLog:
        return self._parent


# --------------------------------------------------------------------------- LLM 调用记录
class LLMRun:
    """一次「优化提示词」的完整证据链。

    生命周期：begin -> set_request -> [note_thinking/note_delta/note_event] -> finish/abort。
    finish 之前都可以被中断（客户端断开），abort 会把它标成 aborted 并保留已有内容。
    """

    def __init__(self, store: "LLMRunStore", meta: dict):
        self.store = store
        self.id = meta.get("id")
        self.meta = dict(meta)
        self.full = store.capture == "full"
        self.started = now()
        self.finished: float | None = None
        self.status = "running"
        self.model = meta.get("model")
        self.url = meta.get("url")
        self.mode = meta.get("mode")
        self.request: dict = {}
        # raw/stray/markers 是「模型没按格式输出」时的唯一证据：
        #   raw      = 模型吐出的原文（含标记），解析失败时也能看到它到底说了什么
        #   stray    = 落在标记之外、被解析器丢掉的部分（废话/前言/尾注）
        #   markers  = 实际见到过哪些开始标记（一个都没有 = 完全没按格式走）
        self.response: dict = {"sections": {"prompt": "", "translation": "", "notes": ""},
                               "section_chars": {"prompt": 0, "translation": 0, "notes": 0},
                               "thinking": "", "chunks": 0, "content_chars": 0,
                               "reasoning_chars": 0, "missing": [],
                               "raw": "", "raw_chars": 0,
                               "stray": "", "stray_chars": 0,
                               "markers": [], "bad_chunks": 0, "format_ok": None}
        self.usage: dict | None = None
        self.error: dict | None = None
        self.events: list[dict] = []
        self._saved = False
        # RLock：finish() 持有锁时还会调 note_event()，普通 Lock 会自锁死
        self._lock = threading.RLock()

    # -- 采集
    def set_request(self, payload: dict, system: str, user: str,
                    system_sources: list[str] | None = None,
                    attachments: list[dict] | None = None) -> None:
        # payload 里的 messages 可能带 base64 图片（多模态），一律不落盘；
        # 只留附件元数据（文件名/尺寸/字节数）。data_uri 再过一道，防止调用方手滑传进来。
        safe = {k: v for k, v in (payload or {}).items() if k != "messages"}
        atts = [{k: v for k, v in (a or {}).items() if k != "data_uri"}
                for a in (attachments or [])]
        with self._lock:
            self.request = {
                "model": safe.get("model"), "stream": safe.get("stream"),
                "params": {k: v for k, v in safe.items() if k not in ("model", "stream")},
                "system_chars": len(system or ""), "user_chars": len(user or ""),
                "system_sources": list(system_sources or []),
                "image_count": len(atts), "attachments": atts,
                # 图片本体（base64）不落盘，但要留下「这次实际发了多少字节图片」
                "image_bytes": sum(len((a or {}).get("data_uri") or "")
                                   for a in (attachments or [])),
            }
            if self.full:
                self.request["system"] = self.store._clip(system or "")
                self.request["user"] = self.store._clip(user or "")

    def note_thinking(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self.response["reasoning_chars"] += len(text)
            if self.full:
                self.response["thinking"] = self.store._clip(self.response["thinking"] + text)

    def note_raw(self, text: str) -> None:
        """原样记下模型输出（解析前）。格式不符时靠它还原模型到底说了什么。"""
        if not text:
            return
        with self._lock:
            self.response["raw_chars"] += len(text)
            if self.full:
                self.response["raw"] = self.store._clip(self.response["raw"] + text)

    def note_stray(self, text: str) -> None:
        """记下落在标记之外、被解析器丢掉的内容（前言/尾注/格式错乱的正文）。"""
        if not text:
            return
        with self._lock:
            self.response["stray_chars"] += len(text)
            if self.full:
                self.response["stray"] = self.store._clip(self.response["stray"] + text)

    def note_marker(self, name: str) -> None:
        """记下实际见到的开始标记；一个都没有 = 模型完全没按协议输出。"""
        if not name:
            return
        with self._lock:
            if name not in self.response["markers"]:
                self.response["markers"].append(name)

    def note_bad_chunk(self, line: str, error: str = "") -> None:
        """流里解析不出来的行（服务端被代理改写、非 SSE 格式等）也要留证。"""
        with self._lock:
            self.response["bad_chunks"] += 1
            self.note_event("bad_chunk", (line or "")[:200], error=error[:200])

    def note_delta(self, section: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            self.response["chunks"] += 1
            self.response["content_chars"] += len(text)
            chars = self.response["section_chars"]
            if section in chars:
                chars[section] += len(text)
            # summary 模式只留长度，不留正文（省磁盘；需要正文就把 llm_capture 改成 full）
            if self.full and section in self.response["sections"]:
                sec = self.response["sections"]
                sec[section] = self.store._clip(sec[section] + text)

    def note_event(self, kind: str, message: str, **fields) -> None:
        with self._lock:
            ev = {"t": _iso_ms(), "kind": kind, "message": str(message)}
            ev.update({k: self.store._clip(v) for k, v in fields.items()})
            self.events.append(ev)
            if len(self.events) > 200:
                del self.events[:100]

    def set_usage(self, usage: dict | None) -> None:
        if usage:
            with self._lock:
                self.usage = dict(usage)

    def finish(self, status: str = "ok", error: dict | None = None,
               missing: list[str] | None = None) -> dict:
        with self._lock:
            if self.finished is None:
                self.finished = now()
            self.status = status
            if error:
                self.error = dict(error)
                self.note_event("error", error.get("message") or "error", **{
                    k: v for k, v in error.items() if k != "message"})
            if missing is not None:
                self.response["missing"] = list(missing)
                # 三段标记是否都拿到了：false 时页面/排障脚本第一眼就能看出来
                self.response["format_ok"] = not missing
        return self.save()

    def abort(self, reason: str) -> dict:
        with self._lock:
            self.note_event("abort", reason)
        return self.finish("aborted", {"message": reason})

    @property
    def duration_s(self) -> float:
        return round((self.finished or now()) - self.started, 3)

    def summary(self) -> dict:
        sec = self.response.get("sections") or {}
        return {
            "id": self.id, "started": _iso_ms(self.started), "started_t": self.started,
            "finished": _iso_ms(self.finished) if self.finished else None,
            "duration_s": self.duration_s, "status": self.status,
            "model": self.model, "mode": self.mode, "url": self.url,
            "thinking": self.meta.get("thinking"),
            "system_chars": self.request.get("system_chars"),
            "user_chars": self.request.get("user_chars"),
            "images": self.request.get("image_count") or 0,
            "chunks": self.response.get("chunks"),
            "content_chars": self.response.get("content_chars"),
            "reasoning_chars": self.response.get("reasoning_chars"),
            "output_chars": dict(self.response.get("section_chars")
                                 or {k: len(v or "") for k, v in sec.items()}),
            "missing": self.response.get("missing") or [],
            # 格式诊断：raw_chars 是模型输出总长，missing 为空才代表协议走通
            "raw_chars": self.response.get("raw_chars"),
            "stray_chars": self.response.get("stray_chars"),
            "markers": list(self.response.get("markers") or []),
            "bad_chunks": self.response.get("bad_chunks"),
            "format_ok": self.response.get("format_ok"),
            "usage": self.usage,
            "error": (self.error or {}).get("message") if self.error else None,
            "events": len(self.events),
            "capture": self.store.capture,
        }

    def save(self) -> dict:
        if self._saved:
            return self.summary()
        self._saved = True
        return self.store._persist(self)


class LLMRunStore:
    """LLM 调用记录仓库：每次调用一份 JSON + 一份可快速列举的索引。"""

    def __init__(self, directory: str, log: RunLog | None = None, *, enabled: bool = True,
                 capture: str = "full", max_runs: int = 100, max_chars: int = 200_000):
        self.enabled = bool(enabled)
        self.capture = capture if capture in ("off", "summary", "full") else "full"
        self.dir = os.path.abspath(directory)
        self.log = log
        self.max_runs = max(1, int(max_runs or 100))
        self.max_chars = max(1000, int(max_chars or 200_000))
        self._lock = threading.Lock()
        self._seq = 0
        self._index_path = os.path.join(self.dir, "index.jsonl")
        if self.enabled and self.capture != "off":
            try:
                os.makedirs(self.dir, exist_ok=True)
            except OSError as e:
                if self.log:
                    self.log.warn(f"LLM 记录目录创建失败：{e}", source="llm", event="llm.error")
                self.enabled = False

    def _clip(self, value, limit: int | None = None):
        return _clip(value, limit or self.max_chars)

    def _new_id(self) -> str:
        with self._lock:
            self._seq += 1
            seq = self._seq
        return time.strftime("%Y%m%d-%H%M%S") + "-%04d" % seq

    def begin(self, meta: dict) -> LLMRun:
        meta = dict(meta or {})
        meta.setdefault("id", self._new_id())
        run = LLMRun(self, meta)
        if self.log:
            self.log.info("LLM 调用开始", source="llm", event="llm.start", run=run.id,
                          model=run.model, mode=run.mode, url=run.url,
                          thinking=bool(meta.get("thinking")),
                          stream=bool(meta.get("stream")),
                          images=int(meta.get("images") or 0), capture=self.capture)
        return run

    def _run_path(self, run_id: str) -> str:
        return os.path.join(self.dir, f"{run_id}.json")

    def _persist(self, run: LLMRun) -> dict:
        summary = run.summary()
        if not self.enabled or self.capture == "off":
            return summary
        payload = dict(summary)
        payload.update({"request": run.request, "response": run.response,
                        "events": run.events, "capture": self.capture})
        try:
            write_json(self._run_path(run.id), payload)
            with self._lock:
                with open(self._index_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")
            self.prune()
        except Exception as e:
            if self.log:
                self.log.warn(f"LLM 记录落盘失败：{e}", source="llm", event="llm.error",
                              run=run.id)
        if self.log:
            log = self.log.warn if run.status == "error" else self.log.info
            log("LLM 调用结束", source="llm", event="llm.finish", run=run.id,
                status=run.status, duration_s=summary["duration_s"],
                tokens=(run.usage or {}).get("total_tokens"),
                content_chars=summary["content_chars"], raw_chars=summary["raw_chars"],
                markers=",".join(summary["markers"] or []) or "-",
                images=summary.get("images"),
                error=summary["error"])
            # 调用本身成功、但模型没按标记协议输出：这不算 error，却是最需要看见的一类失败，
            # 单独记一条 warn，页面按 event=llm.format 就能筛出来。
            missing = summary.get("missing") or []
            if run.status == "ok" and missing:
                self.log.warn("LLM 输出缺少标记段", source="llm", event="llm.format",
                              run=run.id, missing=",".join(missing),
                              markers=",".join(summary["markers"] or []) or "-",
                              raw_chars=summary["raw_chars"],
                              stray_chars=summary["stray_chars"],
                              hint="模型没按 <<<H3_*>>> 协议输出；"
                                   "完整原文见本 run 的 response.raw")
        return summary

    # -- 查询
    def list_runs(self, limit: int = 50, status: str | None = None,
                  q: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit or 50), 500))
        # 索引可能因崩溃缺尾部，read 失败就当空
        lines = []
        try:
            lines = tail_lines(self._index_path, max(limit * 4, 200))
        except Exception:
            lines = []
        out: list[dict] = []
        seen = set()
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except Exception:
                continue
            if not isinstance(item, dict) or item.get("id") in seen:
                continue
            seen.add(item.get("id"))
            if status and status != "all" and item.get("status") != status:
                continue
            if q and q.lower() not in json.dumps(item, ensure_ascii=False).lower():
                continue
            out.append(item)
            if len(out) >= limit:
                break
        # 索引缺失/为空时退回扫目录（例如手工删过文件）
        if not out and os.path.isdir(self.dir):
            try:
                files = [os.path.join(self.dir, n) for n in os.listdir(self.dir)
                         if n.endswith(".json")]
                files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
                for p in files[:limit]:
                    try:
                        with open(p, encoding="utf-8") as f:
                            data = json.load(f)
                        data.pop("request", None)
                        data.pop("response", None)
                        data.pop("events", None)
                        out.append(data)
                    except Exception:
                        continue
            except OSError:
                pass
        return out

    def get(self, run_id: str) -> dict | None:
        if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
            return None
        try:
            with open(self._run_path(run_id), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def prune(self) -> int:
        """只保留最近 max_runs 份记录，顺手重建索引。"""
        if not os.path.isdir(self.dir):
            return 0
        try:
            names = [n for n in os.listdir(self.dir) if n.endswith(".json")]
        except OSError:
            return 0
        paths = [os.path.join(self.dir, n) for n in names]
        paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        removed = 0
        for p in paths[self.max_runs:]:
            try:
                os.remove(p)
                removed += 1
            except OSError:
                continue
        if removed:
            keep = set(paths[:self.max_runs])
            # 索引比记录文件更容易过期；直接按保留文件重建，代价可控（几十条）
            try:
                items = []
                for p in list(keep):
                    try:
                        with open(p, encoding="utf-8") as f:
                            d = json.load(f)
                        d.pop("request", None)
                        d.pop("response", None)
                        d.pop("events", None)
                        items.append(d)
                    except Exception:
                        continue
                items.sort(key=lambda d: d.get("started_t") or 0)
                with open(self._index_path, "w", encoding="utf-8") as f:
                    for d in items:
                        f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
            except Exception:
                pass
        return removed

    def stats(self) -> dict:
        count = 0
        total_bytes = 0
        try:
            for n in os.listdir(self.dir):
                if n.endswith(".json"):
                    count += 1
                    try:
                        total_bytes += os.path.getsize(os.path.join(self.dir, n))
                    except OSError:
                        pass
        except OSError:
            pass
        return {"enabled": self.enabled, "capture": self.capture, "dir": self.dir,
                "runs": count, "max_runs": self.max_runs,
                "bytes": total_bytes, "index": self._index_path}
