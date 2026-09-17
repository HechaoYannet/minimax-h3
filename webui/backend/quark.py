"""webui.backend.quark -- 夸克网盘 CLI 的调用封装与任务管理。

定位：本模块是 quarkclouddrive CLI（web_disk/quarkclouddrive-*/scripts/quark-drive.cjs，
一个 Node 程序）的调用方。三条边界：

  1. **不读 CLI 源码、不碰账号口令**：只按官方文档的命令行契约调用，stdout 是 NDJSON，
     一行一个 JSON；授权（access_token）由 CLI 自己维护在它的配置目录里，本服务读不到也不需要。
  2. **node 从哪来**：后端跑在 WSL，而 CLI 需要 Node.js。WSL 里没有 Linux node 时，
     走 Windows 侧的 node.exe（WSL interop）—— 这条路要求把路径翻成 D:\\... 形式，
     并且 CLI 认 agent 环境的那几个环境变量要经 WSLENV 透传（见 config/quark.yaml 的 agent 段）。
  3. **长任务不进 HTTP 线程**：上传/下载都可能跑几分钟，统一做成 DiskTask（后台线程 + 事件流），
     HTTP 只负责提交与订阅，跟生成作业（jobs.py）是同一套做法。

失败分类很重要：CLI 用负数 code 表达「未登录 / 文件不存在 / 网络失败」，
其中未登录（-103/-104/msg 含「未授权/未登录」）会让页面直接把「去授权」按钮顶出来，
而不是丢一句「失败」了事。
"""
from __future__ import annotations

import json
import os
import queue
import random
import re
import shutil
import signal
import string
import subprocess
import threading
import time
import uuid

from . import pack
from .runlog import BoundLog
from .util import append_jsonl, iso, now, read_json, tail_lines, wsl_to_windows, write_json

NDJSON_ACTIONS = {"result", "progress", "list", "artifact"}

# 没登录 / 授权失效：CLI 的文案在不同命令下不完全一致，用 code + 关键词双保险。
# 三个 code 的含义别搞混（都在实测里见过）：
#   -103  未登录，请先执行 login     -> 需要引导授权
#   -1408 未完成授权认证             -> 需要引导授权
#   -104  无法识别当前 Agent 环境    -> 环境问题，不是授权问题（把用户往授权页引是误导）
#   -118  login 时表示「你已经授权过了」-> **不是错误**，任务应该算成功
AUTH_CODES = {-103, -1408}
ENV_CODES = {-104}
BENIGN_CODES = {-118}
AUTH_WORDS = ("未登录", "未授权", "未完成授权", "授权已过期", "认证失败")


class QuarkError(RuntimeError):
    def __init__(self, message: str, code=None, need_login: bool = False, payload=None):
        super().__init__(message)
        self.code = code
        self.need_login = need_login
        self.payload = payload or {}


def looks_unauthorized(code, msg: str) -> bool:
    if isinstance(code, int) and code in AUTH_CODES:
        return True
    if isinstance(code, int) and (code in BENIGN_CODES or code in ENV_CODES):
        return False
    m = msg or ""
    if "未登录" in m or "未授权" in m or "未完成授权" in m or "授权已过期" in m:
        return True
    return any(w in m.lower() for w in ("unauthorized", "not_authenticated"))


# --------------------------------------------------------------------------- 运行环境

class QuarkEnv:
    """探测「用哪个 node、CLI 在不在」，结果带 60s 缓存（状态接口每次刷新都调它）。"""

    CACHE_S = 60.0

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cache: dict | None = None
        self._cache_at = 0.0

    # -- 探测
    def _linux_node(self) -> str | None:
        override = (self.cfg.get("node") or "").strip()
        if override and os.path.exists(override) and not override.lower().endswith(".exe"):
            return override
        found = shutil.which("node")
        if found and self._node_ok(found):
            return found
        return None

    def _windows_node(self) -> str | None:
        override = (self.cfg.get("node") or "").strip()
        if override and override.lower().endswith(".exe") and os.path.exists(override):
            return override
        cands = []
        for probe in ("node.exe", "npm"):
            p = shutil.which(probe)
            if p:
                cands.append(os.path.join(os.path.dirname(p), "node.exe"))
        cands += ["/mnt/c/Program Files/nodejs/node.exe",
                  "/mnt/c/Program Files (x86)/nodejs/node.exe"]
        for c in cands:
            if os.path.exists(c) and self._node_ok(c):
                return c
        return None

    @staticmethod
    def _node_ok(node: str) -> bool:
        try:
            r = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15)
            return r.returncode == 0 and (r.stdout or "").strip().lower().startswith("v")
        except Exception:
            return False

    def detect(self, fresh: bool = False) -> dict:
        with self._lock:
            if not fresh and self._cache and (now() - self._cache_at) < self.CACHE_S:
                return self._cache
            cfg = self.cfg
            cli = (cfg.get("cli") or "").strip()
            cli_exists = bool(cli) and os.path.isfile(cli)
            want = str(cfg.get("runner") or "auto").lower()
            node = None
            mode = None
            if want in ("auto", "linux"):
                node = self._linux_node()
                if node:
                    mode = "linux"
            if node is None and want in ("auto", "windows"):
                node = self._windows_node()
                if node:
                    mode = "windows"
            info = {
                "ok": bool(node and cli_exists),
                "mode": mode,
                "node": node,
                "node_version": self._node_version(node) if node else None,
                "cli": cli,
                "cli_exists": cli_exists,
                "cli_version": None,
                "runner_wanted": want,
                "reason": None,
            }
            if not node:
                info["reason"] = ("没找到可用的 node：WSL 里没装 Node.js，"
                                  "Windows 侧也没探到 node.exe（可改 config/quark.yaml 的 node 指定绝对路径）")
            elif not cli_exists:
                info["reason"] = f"CLI 入口不存在：{cli}"
            self._cache, self._cache_at = info, now()
            return info

    @staticmethod
    def _node_version(node: str) -> str | None:
        try:
            r = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15)
            return (r.stdout or "").strip() or None
        except Exception:
            return None


# --------------------------------------------------------------------------- CLI 调用

class QuarkCLI:
    """一次 CLI 调用的封装：拼命令 -> 跑 -> 逐行解析 NDJSON。"""

    def __init__(self, cfg: dict, env: QuarkEnv, log=None, session_id: str | None = None):
        self.cfg = cfg
        self.envprobe = env
        self.log = log
        self.session_id = session_id or self.new_session_id()
        self._procs: set[subprocess.Popen] = set()
        self._plock = threading.Lock()

    @staticmethod
    def new_session_id() -> str:
        """CLI 要求 {timestamp}-{random6} 且同一对话复用（禁止语义化名字）。"""
        ts = int(time.time())
        alphabet = string.ascii_lowercase + string.digits
        return f"{ts}-{''.join(random.choice(alphabet) for _ in range(6))}"

    # -- 环境变量：WSLENV 是 Windows runner 下环境变量能过界的唯一通道
    def build_env(self) -> dict:
        """CLI 认 agent 环境的两个标记。

        实测（见 docs/QUARK.md）：只给 QK_AGENT_ID 仍然会 -104；必须带上 DSH_* 之一
        （DSH_SESSION_ID 或 DSH_HOME）。DSH_SESSION_ID 是纯字符串、不涉及路径翻译，
        所以默认用它；DSH_HOME 只在配置里显式给了 Windows 形式路径时才注入
        （从 WSL 的 $HOME 猜出来的 /home/xxx 会被 CLI 当成无效路径）。
        """
        agent = self.cfg.get("agent") or {}
        env = dict(os.environ)
        env["QK_AGENT_ID"] = str(agent.get("id") or "deepseek")
        env.setdefault("DSH_SESSION_ID", f"session-{self.session_id}")
        dsh = (agent.get("dsh_home") or "").strip()
        if dsh:
            env["DSH_HOME"] = dsh
        share = [v for v in ("QK_AGENT_ID", "DSH_SESSION_ID", "DSH_HOME") if env.get(v)]
        prev = (env.get("WSLENV") or "").strip(":")
        parts = [p for p in prev.split(":") if p] if prev else []
        have = {p.split("/")[0].upper() for p in parts}
        for v in share:
            if v.upper() not in have:
                parts.append(v + "/w")
        if parts:
            env["WSLENV"] = ":".join(parts)
        return env

    # -- 路径：Windows runner 下所有本地路径都必须是 D:\... 形式
    def to_runner_path(self, path: str, mode: str) -> str:
        return wsl_to_windows(path) if mode == "windows" else path

    def build_command(self, args: list[str], mode: str, session_input: str | None = None) -> list[str]:
        info = self.envprobe.detect()
        node = info.get("node")
        if not node:
            raise QuarkError(info.get("reason") or "找不到 node，无法调用网盘 CLI")
        common = ["--session-id", self.session_id]
        text = (session_input or (self.cfg.get("agent") or {}).get("session_input") or "").strip()
        if text:
            common += ["--session-input", text]
        # CLI 自己也是「本地路径」：Windows runner 下必须是 D:\... 形式
        return [node, self.to_runner_path(self.cfg["cli"], mode)] + list(args) + common

    def run(self, args: list[str], on_event=None, cancel=None, timeout: float | None = None,
            cwd: str | None = None, session_input: str | None = None,
            path_values: list[str] | None = None) -> dict:
        """跑一次 CLI。

        path_values 里给出「参数里含本地路径」的值，Windows runner 下会翻成 D:\\...
        （翻了才知道往哪翻：upload 的位置参数、download 的 --output-dir 都是路径，
        而 --keyword 之类的文本参数不能乱翻。）
        """
        info = self.envprobe.detect()
        mode = info.get("mode") or "linux"
        argv = self.build_command(args, mode, session_input)
        if path_values:
            # 只翻「本来就是本地路径」的参数值（upload 的位置参数、download 的 --output-dir），
            # --keyword 之类的文本参数不能乱翻
            wanted = {p: self.to_runner_path(p, mode) for p in path_values}
            argv = [wanted.get(a, a) for a in argv]
        env = self.build_env()
        workdir = cwd or self.cfg.get("root") or os.getcwd()
        events: list[dict] = []
        result: dict = {}
        t0 = time.time()
        if self.log:
            self.log.debug("调用网盘 CLI", cmd=" ".join(argv[:3] + ["..."]) , args=" ".join(args[:6]),
                           mode=mode)
        proc = subprocess.Popen(argv, cwd=workdir, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace",
                                bufsize=1, start_new_session=True)
        with self._plock:
            self._procs.add(proc)
        # 看门狗：像 login 这种会「卡住等人在浏览器里点」的命令，可能长时间一行输出都没有，
        # 光靠在读循环里判断时间根本轮不到 —— 必须有个独立计时器兜底。
        timed_out = threading.Event()
        timer = None
        if timeout:
            def _on_timeout():
                timed_out.set()
                self._kill(proc)
            timer = threading.Timer(timeout, _on_timeout)
            timer.daemon = True
            timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if not line.strip():
                    continue
                ev = None
                if line.lstrip().startswith("{"):
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        ev = None
                if isinstance(ev, dict):
                    events.append(ev)
                    if ev.get("type") == "result":
                        result = ev
                    if on_event:
                        on_event(ev)
                elif on_event:
                    on_event({"type": "log", "line": line})
                if cancel is not None and cancel.is_set():
                    break
                if timeout and (time.time() - t0) > timeout:
                    self._kill(proc)
                    raise QuarkError(f"网盘命令超时（>{int(timeout)}s）")
        finally:
            if timer:
                timer.cancel()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                proc.wait(timeout=10)
            with self._plock:
                self._procs.discard(proc)
        if timed_out.is_set():
            raise QuarkError(f"网盘命令超时（>{int(timeout)}s）：{' '.join(args[:2])}")
        if cancel is not None and cancel.is_set():
            raise InterruptedError("已取消")
        code = result.get("code", 0 if proc.returncode == 0 else proc.returncode)
        msg = result.get("msg") or ""
        if events and result:
            # -118 是 login 的「你已经授权过了」，属于正常终态，不能当失败抛出去
            if code not in (0, None) and code not in BENIGN_CODES:
                raise QuarkError(msg or f"网盘命令失败（code={code}）", code=code,
                                 need_login=looks_unauthorized(code, msg), payload=result)
        elif proc.returncode != 0:
            last = events[-1] if events else {}
            raise QuarkError(last.get("msg") or f"网盘命令退出码 {proc.returncode}",
                             code=last.get("code", proc.returncode),
                             need_login=looks_unauthorized(last.get("code"), last.get("msg") or ""),
                             payload=last)
        return {"result": result, "events": events, "code": code, "msg": msg}

    @staticmethod
    def _kill(proc: subprocess.Popen):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
            return
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def kill_all(self):
        with self._plock:
            procs = list(self._procs)
        for p in procs:
            self._kill(p)

    # -- 便捷封装
    def call(self, args: list[str], **kw) -> dict:
        return self.run(args, **kw)

    def status_probe(self) -> dict:
        """探活：CLI 在不在 + 当前是否已授权（用 get-user-info，两个接口都成功才算成功）。"""
        info = self.envprobe.detect()
        out = {"cli": info, "logged_in": False, "account": None, "message": None}
        if not info.get("ok"):
            out["message"] = info.get("reason")
            return out
        try:
            r = self.call(["get-user-info"], timeout=45)
            data = (r.get("result") or {}).get("data") or {}
            out["logged_in"] = True
            out["account"] = data
            out["message"] = (r.get("result") or {}).get("msg") or "已授权"
        except QuarkError as e:
            out["logged_in"] = False
            out["message"] = str(e)
            out["need_login"] = bool(e.need_login)
        except Exception as e:  # 网络异常等
            out["message"] = f"探测失败：{e}"
        return out


# --------------------------------------------------------------------------- 任务

STAGES = {
    "publish": [("queued", "排队中"), ("probe", "读取产物"), ("transcode", "重新编码(有损)"),
                ("archive", "打包加密"), ("upload", "上传网盘"), ("share", "创建分享链接"),
                ("done", "完成")],
    "fetch": [("queued", "排队中"), ("resolve", "解析网盘文件"), ("download", "下载中"),
              ("done", "完成")],
    "login": [("queued", "排队中"), ("login", "等待授权"), ("done", "完成")],
    "probe": [("queued", "排队中"), ("probe", "查询账号"), ("done", "完成")],
}


class DiskTask:
    def __init__(self, tid: str, kind: str, params: dict, paths: dict):
        self.id = tid
        self.kind = kind
        self.params = params or {}
        self.paths = paths
        self.status = "queued"          # queued | running | done | failed | cancelled
        self.stage = "queued"
        self.created = now()
        self.started: float | None = None
        self.finished: float | None = None
        self.progress = 0.0
        self.message = ""
        self.error: str | None = None
        self.need_login = False
        self.result: dict | None = None
        self.log_tail: list[str] = []
        self.events: list[dict] = []
        self.seq = 0
        self.proc: subprocess.Popen | None = None
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.cancel_ev = threading.Event()
        self.rlog = None

    # -- 事件
    def emit(self, ev: dict, persist: bool = True):
        ev = dict(ev)
        with self._lock:
            self.seq += 1
            ev["seq"] = self.seq
        ev.setdefault("t", now())
        ev.setdefault("iso", iso())
        with self._lock:
            self.events.append(ev)
            if len(self.events) > 4000:
                del self.events[: len(self.events) - 4000]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except Exception:
                pass
        if persist and self.paths.get("events"):
            append_jsonl(self.paths["events"], ev)

    def subscribe(self) -> queue.Queue:
        q: "queue.Queue[dict]" = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def events_after(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e.get("seq", 0) > seq]

    def log_line(self, line: str):
        self.log_tail.append(line)
        if len(self.log_tail) > 400:
            del self.log_tail[: len(self.log_tail) - 400]

    def set_stage(self, stage: str, info: str = ""):
        self.stage = stage
        zh = dict(STAGES.get(self.kind, [])).get(stage, stage)
        self.emit({"type": "stage", "stage": stage, "stage_zh": zh, "info": info})

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind,
            "kind_zh": {"publish": "加密打包上传", "fetch": "下载到本机", "login": "授权登录",
                        "probe": "账号查询"}.get(self.kind, self.kind),
            "status": self.status, "stage": self.stage,
            "stage_zh": dict(STAGES.get(self.kind, [])).get(self.stage, self.stage),
            "created": iso(self.created),
            "started": iso(self.started) if self.started else None,
            "finished": iso(self.finished) if self.finished else None,
            "elapsed_s": round((self.finished or now()) - (self.started or self.created), 1),
            "progress": round(self.progress, 4), "message": self.message,
            "error": self.error, "need_login": self.need_login,
            "result": self.result, "params": self.params,
            "log_tail": self.log_tail[-60:],
        }

    def snapshot(self):
        if self.paths.get("task"):
            write_json(self.paths["task"], self.to_dict())


class DiskManager:
    """网盘任务的排队与执行。与生成作业一样：单并发 + 事件流 + 可取消。"""

    def __init__(self, cfg: dict, log, disk_cfg: dict, root: str):
        self.cfg = cfg
        self.log = log
        self.qcfg = disk_cfg
        self.root = root
        self.dir = os.path.join((cfg.get("paths") or {}).get("cache_dir")
                                or os.path.join(root, "cache", "webui"), "disk")
        os.makedirs(self.dir, exist_ok=True)
        self.tasks: dict[str, DiskTask] = {}
        self.order: list[str] = []
        self._lock = threading.Lock()
        self._running: str | None = None
        self._queue: list[str] = []
        self.session_id = QuarkCLI.new_session_id()
        self.cli = QuarkCLI(disk_cfg, QuarkEnv(disk_cfg), log, self.session_id)
        self._load_from_disk()

    # ---------------------------------------------------------------- 持久化
    def _load_from_disk(self):
        """把历史任务装回内存。

        必须「先提交的在前」，否则 list() 取尾部 limit 条拿到的是最旧的那几条 ——
        服务一重启，页面上刚拿到的分享链接就消失了（踩过）。
        而任务 ID 只精确到秒（年月日-时分秒-随机6位），同一秒内按目录名排序等于按随机后缀排，
        所以这里按数值型创建时间排；老记录没有该字段时退化为 task.json 的 mtime。
        """
        try:
            names = os.listdir(self.dir)
        except OSError:
            return
        rows = []
        for name in names:
            d = os.path.join(self.dir, name)
            data = read_json(os.path.join(d, "task.json"), None)
            if not isinstance(data, dict) or not data.get("id"):
                continue
            ts = data.get("created_ts")
            if not isinstance(ts, (int, float)):
                try:
                    ts = os.path.getmtime(os.path.join(d, "task.json"))
                except OSError:
                    ts = _parse_iso(data.get("created")) or 0.0
            rows.append((float(ts), data, d))
        rows.sort(key=lambda r: r[0])
        for _ts, data, d in rows[-40:]:
            t = DiskTask(data["id"], data.get("kind") or "publish", data.get("params") or {},
                         {"dir": d, "task": os.path.join(d, "task.json"),
                          "events": os.path.join(d, "events.jsonl"),
                          "log": os.path.join(d, "run.log")})
            t.status = data.get("status") or "done"
            if t.status in ("running", "queued"):
                t.status = "interrupted"
            t.stage = data.get("stage") or "done"
            t.result = data.get("result")
            t.error = data.get("error")
            t.created = _parse_iso(data.get("created")) or now()
            t.started = _parse_iso(data.get("started"))
            t.finished = _parse_iso(data.get("finished")) or t.created
            t.progress = float(data.get("progress") or (1.0 if t.status == "done" else 0.0))
            self.tasks[t.id] = t
            self.order.append(t.id)

    # ---------------------------------------------------------------- 提交
    def _new_task(self, kind: str, params: dict) -> DiskTask:
        tid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        d = os.path.join(self.dir, tid)
        os.makedirs(d, exist_ok=True)
        t = DiskTask(tid, kind, params, {"dir": d, "task": os.path.join(d, "task.json"),
                                         "events": os.path.join(d, "events.jsonl"),
                                         "log": os.path.join(d, "run.log")})
        t.rlog = self.log.child(source="disk", task=tid) if hasattr(self.log, "child") else self.log
        t.emit({"type": "submitted", "kind": kind, "params": params})
        with self._lock:
            self.tasks[tid] = t
            self.order.append(tid)
            can_start = self._running is None
            if not can_start:
                self._queue.append(tid)
        t.snapshot()
        if can_start:
            self._start(t)
        else:
            t.emit({"type": "queued", "position": len(self._queue)})
        return t

    def submit_publish(self, params: dict) -> DiskTask:
        return self._new_task("publish", params)

    def submit_fetch(self, params: dict) -> DiskTask:
        return self._new_task("fetch", params)

    def submit_login(self, params: dict) -> DiskTask:
        return self._new_task("login", params)

    # ---------------------------------------------------------------- 执行
    def _start(self, t: DiskTask):
        with self._lock:
            if self._running is not None:
                if t.id not in self._queue:
                    self._queue.append(t.id)
                return
            self._running = t.id
        t.status = "running"
        t.started = now()
        t.emit({"type": "start", "kind": t.kind})
        th = threading.Thread(target=self._run, args=(t,), name=f"disk-{t.id}", daemon=True)
        th.start()

    def _run(self, t: DiskTask):
        code, err = 0, None
        try:
            if t.kind == "publish":
                self._do_publish(t)
            elif t.kind == "fetch":
                self._do_fetch(t)
            elif t.kind == "login":
                self._do_login(t)
            else:
                raise QuarkError(f"未知任务类型 {t.kind}")
        except InterruptedError:
            code, err = None, None
            t.status = "cancelled"
        except QuarkError as e:
            code, err = 1, str(e)
            t.status = "failed"
            t.error = str(e)
            t.need_login = bool(e.need_login)
            t.emit({"type": "error", "message": str(e), "need_login": t.need_login})
        except Exception as e:
            code, err = 1, f"{type(e).__name__}: {e}"
            t.status = "failed"
            t.error = err
            t.emit({"type": "error", "message": err})
        finally:
            t.finished = now()
            if t.status == "running":
                t.status = "done"
            t.stage = "done" if t.status == "done" else t.stage
            if t.status == "done":
                t.progress = 1.0
                t.set_stage("done")
            t.emit({"type": "exit", "code": code, "status": t.status, "error": err})
            t.snapshot()
            with self._lock:
                self._running = None
            self._pump_queue()

    def _pump_queue(self):
        while True:
            with self._lock:
                if self._running is not None or not self._queue:
                    return
                nxt = self._queue.pop(0)
            t = self.tasks.get(nxt)
            if t is None or t.status != "queued":
                continue
            self._start(t)
            return

    # ---------------------------------------------------------------- 各任务实现
    def _cli_event_sink(self, t: DiskTask, stage: str):
        """把 CLI 的 NDJSON 行翻译成任务事件（进度/消息）。"""
        def sink(ev: dict):
            typ = ev.get("type")
            if typ == "log":
                # CLI 的纯文本行（授权链接、进度横幅…）：原样转给页面，别吞掉
                t.emit({"type": "log", "line": ev.get("line") or ""})
            elif typ == "progress":
                d = ev.get("data") or {}
                cur, tot = d.get("current"), d.get("total")
                pct = d.get("percent")
                if pct is None and tot:
                    pct = int((cur or 0) * 100 / tot)
                t.progress = _stage_base(t, stage) + min(0.999, float(pct or 0) / 100.0) * _stage_span(t, stage)
                t.emit({"type": "progress", "stage": stage, "current": cur, "total": tot,
                        "percent": pct,
                        "text": (f"{pack.human(cur)} / {pack.human(tot)}" if tot else "")})
            elif typ == "list":
                t.emit({"type": "list", "data": ev.get("data") or {}, "msg": ev.get("msg") or ""})
                d = ev.get("data") or {}
                if d.get("fileId"):
                    t.params.setdefault("_uploaded", []).append(d.get("fileId"))
            elif typ in ("result", None):
                if ev.get("msg"):
                    t.message = ev["msg"]
                    t.emit({"type": "message", "message": ev["msg"]})
            else:
                t.emit({"type": "cli", "raw": ev})
        return sink

    def _run_cli(self, t: DiskTask, args: list[str], stage: str,
                 path_values: list[str] | None = None, timeout: float | None = None,
                 session_input: str | None = None) -> dict:
        t.emit({"type": "cli", "action": args[0] if args else "", "args": args})
        return self.cli.run(args, on_event=self._cli_event_sink(t, stage), cancel=t.cancel_ev,
                            timeout=timeout, path_values=path_values, session_input=session_input)

    def _do_publish(self, t: DiskTask):
        qc = self.qcfg
        p = t.params
        t.set_stage("probe", "读取产物信息")
        src = self._resolve_source(p)
        size0 = os.path.getsize(src)
        is_vid = pack.is_video(src)
        duration = pack.probe_duration(src) if is_vid else None
        t.emit({"type": "source", "path": src, "windows": wsl_to_windows(src),
                "bytes": size0, "is_video": is_vid, "duration_s": duration})
        work = t.paths["dir"]
        stem = re.sub(r"[^\w.\-]+", "_", os.path.splitext(os.path.basename(src))[0])[:80] or "h3"
        current = src
        produced: list[str] = []

        # ---- 1) 压缩（转码）
        comp = dict(qc.get("compress") or {})
        want_compress = bool(p.get("compress", comp.get("enabled", True))) and is_vid
        if want_compress:
            if not pack.ffmpeg_bin():
                t.emit({"type": "warn", "message": "没找到 ffmpeg，跳过重新编码，直接打包"})
            else:
                # 有损操作：只有显式打开 compress 才会走到这里（默认关，保画质）
                t.set_stage("transcode", f"重新编码（有损）：CRF {comp.get('crf')} / "
                                         f"{comp.get('preset')} / 长边 {comp.get('max_edge')}px")
                out = os.path.join(work, stem + "_compressed.mp4")
                res = pack.transcode(current, out, comp,
                                     on_progress=lambda d: self._pack_progress(t, "transcode", d),
                                     cancel=t.cancel_ev)
                produced.append(out)
                t.emit({"type": "compressed", "before": res["before"], "after": res["after"],
                        "ratio": res["ratio"], "seconds": res["seconds"],
                        "text": f"{pack.human(res['before'])} → {pack.human(res['after'])}"
                                f"（{res['ratio'] * 100:.0f}%）"})
                current = out

        # ---- 2) 加密压缩包
        arc = dict(qc.get("archive") or {})
        want_zip = bool(p.get("archive", arc.get("enabled", True)))
        password = str(p.get("password") or arc.get("password") or "").strip()
        if want_zip:
            if not password:
                raise QuarkError("压缩包密码为空：请在 config/quark.yaml 的 archive.password 配置")
            t.set_stage("archive", "打包成加密 zip（内容加密，密码见配置）")
            zp = os.path.join(work, stem + ".zip")
            # 成员名必须是「放进去的那个文件」的名字（current），不是压缩包自己的名字：
            # 叫 xxx.zip 的包里再装一个 xxx.zip（其实是 mp4）会让人解压时一脸问号，
            # 而且 bsdtar 是去 -C 目录里按这个名字找文件的，名字不对它直接报错。
            res = pack.write_encrypted_zip([(current, os.path.basename(current))], zp, password,
                                           level=int(arc.get("level", 0)),
                                           tool=str(arc.get("tool") or "auto"),
                                           tar_bin=str(arc.get("tar_bin") or ""),
                                           on_progress=lambda d: self._pack_progress(t, "archive", d),
                                           on_note=lambda m: self._pack_note(t, m),
                                           cancel=t.cancel_ev)
            produced.append(zp)
            t.emit({"type": "archived", "path": zp, "bytes": res["bytes"],
                    "tool": res.get("tool"),
                    "text": f"{pack.human(res['size_before'])} → {pack.human(res['bytes'])}"
                            + ("（原画质，未重新编码）" if not want_compress else "")})
            current = zp
        else:
            password = ""
        # 先记下「本地已经准备好什么」：后面上传/分享失败时，任务卡上仍然看得到成品与密码
        t.result = {"local": {"wsl": current, "windows": wsl_to_windows(current),
                              "name": os.path.basename(current), "bytes": os.path.getsize(current),
                              "source_bytes": size0},
                    "archive_password": password, "artifacts": produced}

        # ---- 2.5) 只压不上传（dry_run）：先看能压到多少，再决定要不要占网盘/流量
        if p.get("dry_run"):
            t.result = {"dry_run": True, "local": {"wsl": src, "windows": wsl_to_windows(src),
                                                   "name": os.path.basename(src), "bytes": size0},
                        "artifact": current, "artifacts": produced,
                        "artifact_bytes": os.path.getsize(current),
                        "ratio": round(os.path.getsize(current) / size0, 4) if size0 else 1.0,
                        "archive_password": password if want_zip else ""}
            t.message = f"试压完成：{pack.human(size0)} → {pack.human(t.result['artifact_bytes'])}"
            t.emit({"type": "message", "message": t.message})
            return

        # ---- 3) 上传
        up_args = ["upload", current]
        parent = str(p.get("parent_fid") or (qc.get("upload") or {}).get("parent_fid") or "").strip()
        if not parent:
            parent = self._ensure_remote_dir(t, str((qc.get("upload") or {}).get("dir_name") or "").strip())
        t.set_stage("upload", os.path.basename(current) + (f" → 目录 {parent}" if parent else ""))
        if parent:
            up_args += ["--parent-fid", parent]
        r = self._run_cli(t, up_args, "upload", path_values=[current],
                          session_input=p.get("session_input"), timeout=3600)
        data = (r.get("result") or {}).get("data") or {}
        fids = data.get("fids") or []
        fid = fids[0] if fids else None
        upload = {"fid": fid, "fids": fids, "file": os.path.basename(current),
                  "bytes": os.path.getsize(current), "full_path": data.get("fullPath") or "",
                  "parent_fid": parent, "instant": bool(data.get("instantUpload"))}
        t.result["upload"] = upload
        t.emit({"type": "uploaded", **upload})
        if not fid:
            raise QuarkError("上传完成但没拿到文件 FID，无法创建分享链接")

        # ---- 4) 分享
        result = {"upload": upload, "archive_password": password,
                  "compressed": len(produced) > 0 and current != src}
        share_cfg = dict(qc.get("share") or {})
        want_share = bool(p.get("share", share_cfg.get("enabled", True)))
        if want_share:
            t.set_stage("share", "创建分享链接")
            sh_args = ["share", fid,
                       "--url-type", str(int(p.get("url_type") or share_cfg.get("url_type") or 1)),
                       "--expired-type", str(int(p.get("expired_type")
                                                 or share_cfg.get("expired_type") or 1))]
            title = str(p.get("title") or share_cfg.get("title") or "").strip()
            if title:
                sh_args += ["--title", title]
            try:
                sr = self._run_cli(t, sh_args, "share", session_input=p.get("session_input"),
                                   timeout=300)
                sd = (sr.get("result") or {}).get("data") or {}
                result["share"] = {"url": sd.get("share_url"), "passcode": sd.get("passcode"),
                                   "title": title,
                                   "url_type": int(p.get("url_type")
                                                   or share_cfg.get("url_type") or 1)}
                t.emit({"type": "share", **result["share"]})
            except QuarkError as e:
                # 文件已经传上去了，只是分享没建成 —— 这时报「整个任务失败」是误导：
                # 把 FID 留下来，用户还能自己去网盘 App 里分享/下载
                result["share_error"] = str(e)
                t.emit({"type": "warn", "message": f"文件已上传，但创建分享链接失败：{e}"})
        result["local"] = {"wsl": src, "windows": wsl_to_windows(src), "name": os.path.basename(src),
                           "bytes": size0}
        result["artifacts"] = produced
        t.result = result
        if not (qc.get("keep_work", True) or p.get("keep_work")):
            for f in produced:
                try:
                    os.remove(f)
                except OSError:
                    pass
            result["artifacts"] = []

    def _ensure_remote_dir(self, t: DiskTask, name: str) -> str:
        """拿到一个可以用于上传的目标目录 FID。

        为什么不能省掉 --parent-fid：实测这台机器上 CLI 的「默认目录」是空的，
        不传就直接报「参数错误: [upload dir blank]」（官方文档说的「省略目录参数走内部默认行为」
        在这条链路上不成立）。create-folder 对同名目录是幂等的（返回已存在的 FID），
        所以先建/复用同一个目录就行 —— 依然不碰根目录 "0"。
        """
        if not name:
            return ""
        t.emit({"type": "log", "line": "准备网盘目录：" + name})
        r = self._run_cli(t, ["create-folder", "--dir-path", name], "upload",
                          session_input=t.params.get("session_input"), timeout=180)
        d = (r.get("result") or {}).get("data") or {}
        fid = str(d.get("fid") or "").strip()
        if fid:
            path = d.get("full_path") or ""
            t.emit({"type": "log", "line": "目录 FID " + fid + (("（" + str(path) + "）") if path else "")})
        return fid

    def _pack_note(self, t: DiskTask, message: str):
        """打包实现降级之类的消息：既进任务卡，也进运行日志（不静默降级）。"""
        t.emit({"type": "warn", "message": message})
        try:
            if t.rlog:
                t.rlog.warn(message)
        except Exception:
            pass

    def _pack_progress(self, t: DiskTask, stage: str, d: dict):
        span = _stage_span(t, stage)
        base = _stage_base(t, stage)
        t.progress = base + min(0.999, float(d.get("percent") or 0) / 100.0) * span
        t.emit({"type": "progress", "stage": stage, "percent": d.get("percent"),
                "bytes": d.get("bytes"), "total": d.get("total"), "text": d.get("text") or ""})

    def _do_fetch(self, t: DiskTask):
        qc = self.qcfg
        p = t.params
        fids = p.get("fids") or ([p["fid"]] if p.get("fid") else [])
        if not fids:
            raise QuarkError("缺少文件 FID")
        outdir = str(p.get("output_dir") or (qc.get("download") or {}).get("dir") or "")
        outdir = os.path.abspath(outdir)
        os.makedirs(outdir, exist_ok=True)
        t.set_stage("resolve", f"{len(fids)} 个文件 → {outdir}")
        before = _dir_snapshot(outdir)
        t.emit({"type": "target", "dir": outdir, "windows": wsl_to_windows(outdir)})
        outputs = []
        for i, fid in enumerate(fids):
            t.set_stage("download", f"{i + 1}/{len(fids)} {p.get('name') or fid}")
            self._run_cli(t, ["download", "--fid", str(fid), "--output-dir", outdir],
                          "download", path_values=[outdir],
                          session_input=p.get("session_input"), timeout=7200)
            after = _dir_snapshot(outdir)
            new = [k for k, v in after.items() if before.get(k) != v]
            before = after
            outputs.append({"fid": fid, "files": new})
            t.emit({"type": "downloaded", "fid": fid, "files": new})
        t.result = {"dir": outdir, "windows": wsl_to_windows(outdir), "outputs": outputs,
                    "files": [os.path.join(outdir, f) for o in outputs for f in o["files"]]}
        t.emit({"type": "result", **t.result})

    def _do_login(self, t: DiskTask):
        p = t.params
        token = str(p.get("token") or "").strip()
        t.set_stage("login", "浏览器授权" if not token else "授权码登录")
        args = ["login"] + (["--token", token] if token else [])
        r = self._run_cli(t, args, "login", timeout=float(p.get("timeout") or 600))
        res = r.get("result") or {}
        data = res.get("data") or {}
        already = (r.get("code") == -118)
        msg = res.get("msg") or r.get("msg") or ""
        # -118：CLI 说「你已授权 X 账号，想换账号请先解除授权」——
        # 这是一次成功的探活，不是失败；原样把 CLI 的话交给页面
        t.result = {"status": data.get("status") or ("already_authorized" if already else "ok"),
                    "already": already, "msg": msg,
                    "account": data.get("account") or data.get("user") or None}
        t.message = msg
        t.emit({"type": "result", **t.result})

    # ---------------------------------------------------------------- 工具
    def _resolve_source(self, p: dict) -> str:
        path = (p.get("path") or "").strip()
        job_id = (p.get("job_id") or "").strip()
        if not path and job_id:
            jm = getattr(self, "jobs", None)
            j = jm.get(job_id) if jm else None
            if j is None:
                raise QuarkError(f"作业不存在：{job_id}")
            path = ((j.result or {}).get("out") or j.request.get("out") or "").strip()
            if not path:
                raise QuarkError("该作业还没有产物文件")
        if not path:
            raise QuarkError("没有指定要上传的文件（path 或 job_id）")
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise QuarkError(f"文件不存在：{path}")
        sd = os.path.realpath(self.dir)
        if os.path.realpath(path).startswith(sd + os.sep):
            raise QuarkError("不能把网盘任务的中间文件再上传一次")
        return path

    # ---------------------------------------------------------------- 查询/控制
    def cancel(self, tid: str) -> tuple[bool, str]:
        t = self.tasks.get(tid)
        if not t:
            return False, "任务不存在"
        if t.status == "queued":
            with self._lock:
                if tid in self._queue:
                    self._queue.remove(tid)
            t.status, t.finished = "cancelled", now()
            t.emit({"type": "exit", "code": None, "status": "cancelled", "error": None})
            t.snapshot()
            return True, "已从队列中移除"
        if t.status != "running":
            return False, f"任务当前状态是 {t.status}，无法取消"
        t.cancel_ev.set()
        try:
            self.cli.kill_all()
        except Exception:
            pass
        t.status = "cancelling"
        t.emit({"type": "warn", "message": "收到取消请求，正在结束网盘命令"})
        return True, "取消请求已发出"

    def list(self, limit: int = 30) -> list[dict]:
        with self._lock:
            ids = list(self.order)
        out = []
        for tid in reversed(ids[-limit:]):
            t = self.tasks.get(tid)
            if t:
                out.append(t.to_dict())
        return out

    def get(self, tid: str) -> DiskTask | None:
        return self.tasks.get(tid)


# --------------------------------------------------------------------------- 辅助

def _parse_iso(s):
    if not s:
        return None
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def _stage_base(t: DiskTask, stage: str) -> float:
    order = [s for s, _ in STAGES.get(t.kind, [])]
    if stage not in order or len(order) < 2:
        return 0.0
    return order.index(stage) / float(len(order) - 1)


def _stage_span(t: DiskTask, stage: str) -> float:
    order = [s for s, _ in STAGES.get(t.kind, [])]
    if len(order) < 2:
        return 1.0
    return 1.0 / (len(order) - 1)


def _dir_snapshot(d: str) -> dict:
    out = {}
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if os.path.isfile(p):
                out[name] = (st.st_size, int(st.st_mtime))
    except OSError:
        pass
    return out


def log_tail(path: str, n: int = 300) -> list[str]:
    return tail_lines(path, n)
