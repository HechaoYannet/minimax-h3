"""webui.backend.jobs -- 生成作业的排队、执行、进度转发与取消。

与生成系统的边界（解耦点）：
    本模块**不 import 任何生成代码**，只通过命令行调用 run_h3.sh，
    并从子进程 stdout 上解析一种带前缀的结构化行（见 scripts/h3_generate.py 的
    H3_PROGRESS_JSONL 钩子）：

        @@H3@@ {"type":"step","i":3,"total":30,...}

    前缀之外的内容一律当作人类可读日志原样转发。
    这样即使生成脚本内部的日志格式变了，最坏情况也只是「曲线图没了」，
    作业依然能跑完、能看到日志、能拿到产物。
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
import uuid

from .estimate import estimate, risks
from .runlog import BoundLog
from .util import (append_jsonl, iso, now, read_json, safe_rel, tail_lines, write_json)

PROGRESS_PREFIX = "@@H3@@ "
STAGES = [
    ("queued", "排队中"),
    ("prepare", "准备参数/解码参考"),
    ("build", "构建流水线（装载模型）"),
    ("text", "文本编码器（26 B 参数，从磁盘流式）"),
    ("denoise", "去噪采样"),
    ("decode", "VAE 解码与封装"),
    ("done", "完成"),
]
STAGE_ZH = dict(STAGES)


class Job:
    def __init__(self, jid: str, request: dict, cmd: list[str], paths: dict, root: str):
        self.id = jid
        self.request = request
        self.cmd = cmd
        self.paths = paths
        self.root = root
        self.status = "queued"
        self.stage = "queued"
        self.created = now()
        self.started: float | None = None
        self.finished: float | None = None
        self.step = 0
        self.total_steps = int(request.get("steps") or 0)
        self.step_s: float | None = None
        self.eta_s: float | None = None
        self.peak_vram_gib: float | None = None
        self.error: str | None = None
        self.result: dict | None = None
        self.log_tail: list[str] = []
        self.events: list[dict] = []          # 内存中的事件（最近 N 条）
        self.seq = 0
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._last_emit = 0.0
        self.rlog = None                     # 绑定到运行日志的记录器（由 JobManager 注入）

    # ---------------------------------------------------------------- 事件
    def emit(self, ev: dict, persist: bool = True):
        ev = dict(ev)
        with self._lock:
            self.seq += 1
            ev["seq"] = self.seq
        ev.setdefault("t", now())
        ev.setdefault("iso", iso())
        with self._lock:
            self.events.append(ev)
            if len(self.events) > 5000:
                del self.events[: len(self.events) - 5000]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except Exception:
                pass
        if persist:
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

    # ---------------------------------------------------------------- 视图
    def to_dict(self, include_request: bool = True) -> dict:
        req = dict(self.request) if include_request else {}
        if not include_request:
            # 只保留展示需要的字段
            req = {k: req.get(k) for k in
                   ("prompt_excerpt", "width", "height", "num_frames", "steps", "seed",
                    "preset", "seconds")}
        est = self.request.get("_estimate") or {}
        return {
            "id": self.id, "status": self.status, "stage": self.stage,
            "stage_zh": STAGE_ZH.get(self.stage, self.stage),
            "created": iso(self.created), "started": iso(self.started) if self.started else None,
            "finished": iso(self.finished) if self.finished else None,
            "elapsed_s": round((self.finished or now()) - (self.started or self.created), 1),
            "step": self.step, "total_steps": self.total_steps,
            "step_s": self.step_s, "eta_s": self.eta_s,
            "progress": round(self.step / self.total_steps, 4) if self.total_steps else 0.0,
            "peak_vram_gib": self.peak_vram_gib,
            "error": self.error, "result": self.result,
            "estimate": est,
            "request": req,
            "log_tail": self.log_tail[-80:],
            "cmd": [os.path.basename(self.cmd[0])] + self.cmd[1:],
        }

    def snapshot(self):
        write_json(self.paths["job"], self.to_dict())


class JobManager:
    def __init__(self, cfg: dict, log):
        self.cfg = cfg
        self.log = log
        self.root = cfg["root"]
        self.limits = cfg.get("limits") or {}
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self._lock = threading.Lock()
        self._running: str | None = None
        self._queue: list[str] = []
        self._reap_orphans()
        self._load_from_disk()

    def _reap_orphans(self):
        """上一次服务进程被杀掉时，生成子进程会变成孤儿继续吃显存/内存。

        启动时把记录在案的 pid 清一遍：它们是本服务拉起的进程组，
        留着只会让下一次生成 OOM（工程 README §11.3 的教训）。
        """
        path = os.path.join(self.cfg["paths"]["cache_dir"], "child_pids.txt")
        try:
            with open(path) as f:
                pids = [int(x) for x in f.read().split() if x.strip().isdigit()]
        except (OSError, ValueError):
            return
        killed = []
        for pid in pids:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                killed.append(pid)
            except (ProcessLookupError, PermissionError, OSError):
                continue
        try:
            os.remove(path)
        except OSError:
            pass
        if killed:
            self.log.warn(f"清理了上次残留的生成进程：{killed}",
                          source="sys", event="job.reap", pids=killed)

    # ---------------------------------------------------------------- 持久化
    def _load_from_disk(self):
        """重启后仍能看到历史作业（标记为 interrupted，避免误以为还在跑）。"""
        jdir = self.cfg["paths"]["jobs_dir"]
        if not os.path.isdir(jdir):
            return
        for name in sorted(os.listdir(jdir))[-50:]:
            meta = read_json(os.path.join(jdir, name, "job.json"))
            if not isinstance(meta, dict):
                continue
            if meta.get("status") in ("running", "queued"):
                meta["status"] = "interrupted"
                meta["stage"] = "interrupted"
                meta["error"] = meta.get("error") or "服务重启，作业已中断"
            try:
                jid = meta.pop("id")
            except KeyError:
                continue
            j = Job.__new__(Job)
            j.id = jid
            j.request = meta.get("request") or {}
            j.cmd = meta.get("cmd") or []
            j.paths = {"job": os.path.join(jdir, name, "job.json"),
                       "events": os.path.join(jdir, name, "events.jsonl"),
                       "log": os.path.join(jdir, name, "run.log")}
            j.root = self.root
            j.status = meta.get("status", "unknown")
            j.stage = meta.get("stage", "unknown")
            j.created = now()
            j.started = j.finished = None
            j.step = meta.get("step") or 0
            j.total_steps = meta.get("total_steps") or 0
            j.step_s = meta.get("step_s")
            j.eta_s = None
            j.peak_vram_gib = meta.get("peak_vram_gib")
            j.error = meta.get("error")
            j.result = meta.get("result")
            j.log_tail = meta.get("log_tail") or []
            j.events = []
            j.seq = 0
            j.proc = None
            j.thread = None
            j._subs = []
            j._lock = threading.Lock()
            j._cancel = threading.Event()
            j._last_emit = 0.0
            j.rlog = self.log.child(source="job", job=jid)
            self.jobs[jid] = j
            self.order.append(jid)

    def _jlog(self, j: Job) -> BoundLog:
        """作业专属记录器：每条日志自带 source='job' 与 job=<id>，页面可按作业筛。"""
        if getattr(j, "rlog", None) is None:
            j.rlog = self.log.child(source="job", job=j.id)
        return j.rlog

    # ---------------------------------------------------------------- 提交
    def submit(self, request: dict, cmd: list[str]) -> Job:
        jid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        d = os.path.join(self.cfg["paths"]["jobs_dir"], jid)
        os.makedirs(d, exist_ok=True)
        paths = {"dir": d, "job": os.path.join(d, "job.json"),
                 "events": os.path.join(d, "events.jsonl"),
                 "log": os.path.join(d, "run.log")}
        j = Job(jid, request, cmd, paths, self.root)
        j.rlog = self.log.child(source="job", job=jid)
        j.emit({"type": "submitted", "request": {k: v for k, v in request.items()
                                                 if not k.startswith("_")}})
        self._jlog(j).info("作业已提交",
                           shape=f"{request.get('width')}x{request.get('height')}"
                                 f"x{request.get('num_frames')}",
                           steps=request.get("steps"), preset=request.get("preset"),
                           seed=request.get("seed"), refs=len(request.get("refs") or []),
                           dit_onload=request.get("dit_onload"),
                           lora=(os.path.basename(request["lora"]) if request.get("lora") else None),
                           out=request.get("out"))
        with self._lock:
            self.jobs[jid] = j
            self.order.append(jid)
            can_start = self._running is None
            if not can_start:
                self._queue.append(jid)
        j.snapshot()
        if can_start:
            self._start(j)
        else:
            j.emit({"type": "queued", "position": len(self._queue)})
            self._jlog(j).info("作业已排队", position=len(self._queue),
                               max_concurrent=(self.cfg.get("jobs") or {}).get("max_concurrent", 1))
        return j

    # ---------------------------------------------------------------- 执行
    def _start(self, j: Job):
        with self._lock:
            if self._running is not None:
                if j.id not in self._queue:
                    self._queue.append(j.id)
                return
            self._running = j.id
        j.status = "running"
        j.stage = "prepare"
        j.started = now()
        enrich = {"PATH": os.environ.get("PATH", ""), "H3_PROGRESS_JSONL": j.paths["events"],
                  "PYTHONUNBUFFERED": "1", "LANG": os.environ.get("LANG", "C.UTF-8")}
        env = dict(os.environ)
        env.update(enrich)
        j.emit({"type": "start", "cmd": j.cmd, "cwd": self.root})
        self._jlog(j).info("作业启动", cmd=" ".join(j.cmd), cwd=self.root,
                           out=j.request.get("out"), log=j.paths["log"],
                           events=j.paths["events"])
        try:
            logf = open(j.paths["log"], "ab", buffering=0)
        except OSError as e:
            j.status, j.stage, j.error = "failed", "error", f"无法写日志文件：{e}"
            j.finished = now()
            j.emit({"type": "error", "message": j.error})
            self._jlog(j).error("无法写作业日志文件", error=str(e), log=j.paths["log"])
            with self._lock:
                self._running = None
            self._pump_queue()
            return
        try:
            j.proc = subprocess.Popen(j.cmd, cwd=self.root, env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, start_new_session=True,
                                      bufsize=1, text=True, errors="replace")
        except Exception as e:
            logf.close()
            j.status, j.stage, j.error = "failed", "error", f"启动失败：{e}"
            j.finished = now()
            j.emit({"type": "error", "message": j.error})
            self._jlog(j).error("作业进程启动失败", error=str(e), cmd=" ".join(j.cmd))
            with self._lock:
                self._running = None
            self._pump_queue()
            return

        # 记下 pid：服务进程如果被强杀，下次启动要靠它清理孤儿生成进程
        try:
            with open(os.path.join(self.cfg["paths"]["cache_dir"], "child_pids.txt"), "a") as f:
                f.write(f"{j.proc.pid}\n")
        except OSError:
            pass
        self._jlog(j).debug("生成子进程已拉起", pid=j.proc.pid)
        t = threading.Thread(target=self._reader, args=(j, logf), name=f"job-{j.id}", daemon=True)
        j.thread = t
        t.start()

    def _reader(self, j: Job, logf):
        last_log = time.time()
        try:
            assert j.proc and j.proc.stdout
            for line in j.proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    logf.write((line + "\n").encode("utf-8", "replace"))
                except OSError:
                    pass
                if line.startswith(PROGRESS_PREFIX):
                    try:
                        ev = json.loads(line[len(PROGRESS_PREFIX):])
                    except json.JSONDecodeError:
                        j.emit({"type": "log", "line": line})
                        continue
                    self._apply_progress(j, ev)
                else:
                    j.emit({"type": "log", "line": line})
                    j.log_tail.append(line)
                    if len(j.log_tail) > (self.cfg.get("jobs", {}).get("log_tail_lines", 400)):
                        del j.log_tail[: len(j.log_tail) - 400]
                    if time.time() - last_log > 10:
                        last_log = time.time()
                        j.snapshot()
        except Exception as e:  # 读管道失败也要收尾，否则作业永远卡在 running
            j.emit({"type": "warn", "message": f"读取子进程输出失败：{e}"})
            self._jlog(j).warn("读取子进程输出失败", error=str(e))
        finally:
            try:
                logf.close()
            except OSError:
                pass
            code = j.proc.wait() if j.proc else -1
            self._finish(j, code)

    def _apply_progress(self, j: Job, ev: dict):
        kind = ev.get("type")
        if kind == "stage":
            j.stage = ev.get("stage") or j.stage
            j.emit({"type": "stage", "stage": j.stage,
                    "stage_zh": STAGE_ZH.get(j.stage, j.stage), "info": ev.get("info")})
            self._jlog(j).info(f"阶段：{STAGE_ZH.get(j.stage, j.stage)}",
                               stage=j.stage, info=ev.get("info"))
            j.snapshot()
        elif kind == "step":
            j.step = int(ev.get("i") or 0)
            j.total_steps = int(ev.get("total") or j.total_steps or 0)
            j.step_s = ev.get("s_step") or ev.get("s_per_step") or j.step_s
            j.eta_s = ev.get("eta_s")
            if ev.get("peak_vram_gib"):
                j.peak_vram_gib = ev["peak_vram_gib"]
            # 逐步事件全部落盘会很大，降采样：每 2 步或最后一步存一次
            keep = (j.step % 2 == 0) or j.step >= j.total_steps
            j.emit({"type": "step", **{k: v for k, v in ev.items() if k != "type"}},
                   persist=keep)
            # 逐步日志会淹没其它信息：每约 10% 记一条 debug，够看趋势又不刷屏
            tick = max(1, (j.total_steps or 10) // 10)
            if j.step % tick == 0 or j.step >= j.total_steps:
                self._jlog(j).debug("去噪进度", step=j.step, total=j.total_steps,
                                   s_step=j.step_s, eta_s=j.eta_s,
                                   peak_vram_gib=j.peak_vram_gib)
            if keep:
                j.snapshot()
        elif kind == "result":
            j.result = dict(ev)
            j.emit({"type": "result", **{k: v for k, v in ev.items() if k != "type"}})
            # 产物是「输出日志」的主角：路径、大小、帧数、时长都记全，方便事后对账
            out = ev.get("out") or j.request.get("out")
            size_mib = None
            try:
                if out and os.path.exists(out):
                    size_mib = round(os.path.getsize(out) / 1024 ** 2, 2)
            except OSError:
                size_mib = None
            self._jlog(j).info("产物已生成", out=out, size_mib=size_mib,
                               frames=ev.get("frames"), seconds=ev.get("seconds_out"),
                               shape=f"{j.request.get('width')}x{j.request.get('height')}"
                                     f"x{j.request.get('num_frames')}")
        else:
            j.emit(dict(ev))

    def _finish(self, j: Job, code: int):
        j.finished = now()
        if j._cancel.is_set() or j.status == "cancelling":
            j.status, j.stage = "cancelled", "cancelled"
        elif code == 0:
            j.status, j.stage = "done", "done"
        else:
            j.status, j.stage = "failed", "failed"
            # 从日志尾部提取最可能的错误行，直接显示在页面上（而不是让人去翻日志）
            j.error = j.error or self._guess_error(j)
        j.emit({"type": "exit", "code": code, "status": j.status, "error": j.error})
        j.snapshot()
        dur = (j.finished - (j.started or j.created)) / 60
        jl = self._jlog(j)
        out = (j.result or {}).get("out") or j.request.get("out")
        if j.status == "done":
            jl.info("作业完成", status=j.status, code=code, duration_min=round(dur, 2),
                    out=out, peak_vram_gib=j.peak_vram_gib)
        elif j.status == "cancelled":
            jl.warn("作业已取消", status=j.status, code=code, duration_min=round(dur, 2))
        else:
            jl.error("作业失败", status=j.status, code=code, duration_min=round(dur, 2),
                     error=j.error, log=j.paths["log"])
        with self._lock:
            if self._running == j.id:
                self._running = None
        self._pump_queue()

    ERR_PATTERNS = [
        ("CUDA out of memory", "显存不足（CUDA OOM）",
         "降分辨率/帧数，或把 vram_limit 调低；OOM 后建议 wsl --shutdown 再重跑，"
         "否则后续配置会被污染（README §11.3）。"),
        ("Failed to create GPU mapping", "显存不足（驱动级报错，不是干净的 OOM）",
         "Windows「共享 GPU 显存」若开着会掩盖真 OOM；关掉它再试，见 README §10.3。"),
        ("device not ready", "CUDA 设备不可用（通常是上一步 OOM 的后遗症）",
         "wsl --shutdown 后重来；同时确认没有别的进程占着显存。"),
        ("No available kernel", "注意力后端不支持该形状",
         "别把 sdpa_backend 设成 cudnn 之外再叠加音频参考；默认 cudnn 即可。"),
        ("Killed", "进程被系统杀掉（几乎肯定是内存不足）",
         "改用 dit_onload=disk，并降低帧数；WSL 的内存配额见 ~/.wslconfig。"),
        ("No such file or directory", "找不到文件",
         "检查参考素材路径、LoRA 路径在 WSL 里是否存在（Windows 盘是 /mnt/d/...）。"),
        ("Traceback (most recent call last)", "Python 异常",
         "展开下面的日志看最后几行。"),
    ]

    def _guess_error(self, j: Job) -> str:
        tail = tail_lines(j.paths["log"], 200)
        for pat, zh, hint in self.ERR_PATTERNS:
            for line in reversed(tail):
                if pat in line:
                    return f"{zh}：{line.strip()[:200]}｜建议：{hint}"
        for line in reversed(tail):
            s = line.strip()
            if s and not s.startswith("["):
                return f"退出码非 0，日志最后一行：{s[:240]}"
        return "进程异常退出，且日志里没有可识别的错误行。"

    def _pump_queue(self):
        nxt = None
        with self._lock:
            if self._running is None and self._queue:
                nxt = self._queue.pop(0)
        if nxt and nxt in self.jobs:
            j = self.jobs[nxt]
            if j.status == "queued":
                j.emit({"type": "dequeued"})
                self._jlog(j).debug("作业出队，开始执行")
                self._start(j)

    # ---------------------------------------------------------------- 控制
    def cancel(self, jid: str) -> tuple[bool, str]:
        j = self.jobs.get(jid)
        if not j:
            return False, "作业不存在"
        if j.status == "queued":
            with self._lock:
                if jid in self._queue:
                    self._queue.remove(jid)
            j.status, j.stage, j.finished = "cancelled", "cancelled", now()
            j.emit({"type": "exit", "code": None, "status": "cancelled"})
            self._jlog(j).warn("排队中的作业被取消")
            j.snapshot()
            return True, "已从队列中移除"
        if j.status != "running" or not j.proc:
            return False, f"作业当前状态是 {j.status}，无法取消"
        j.status = "cancelling"
        grace = float((self.cfg.get("jobs") or {}).get("cancel_grace_s", 20))
        j.emit({"type": "warn", "message": f"收到取消请求：先发 SIGINT，{grace:.0f}s 后若未退出则 SIGKILL"})
        self._jlog(j).warn("收到取消请求", grace_s=grace, pid=(j.proc.pid if j.proc else None))
        threading.Thread(target=self._kill_later, args=(j, grace), daemon=True).start()
        try:
            os.killpg(os.getpgid(j.proc.pid), signal.SIGINT)
        except Exception as e:
            j.emit({"type": "warn", "message": f"SIGINT 失败（{e}），直接强杀"})
            self._hard_kill(j)
        return True, "取消请求已发出"

    def _kill_later(self, j: Job, grace: float):
        j._cancel.set()
        t0 = time.time()
        while time.time() - t0 < grace:
            if j.proc.poll() is not None:
                return
            time.sleep(0.5)
        if j.proc.poll() is None:
            j.emit({"type": "warn", "message": "优雅退出超时，强制结束进程组"})
            self._hard_kill(j)

    def _hard_kill(self, j: Job):
        try:
            os.killpg(os.getpgid(j.proc.pid), signal.SIGKILL)
        except Exception:
            try:
                j.proc.kill()
            except Exception:
                pass

    # ---------------------------------------------------------------- 查询
    def list(self, limit: int = 30) -> list[dict]:
        with self._lock:
            ids = list(self.order)
        out = []
        for jid in reversed(ids[-limit:]):
            j = self.jobs.get(jid)
            if j:
                out.append(j.to_dict(include_request=False))
        return out

    def get(self, jid: str) -> Job | None:
        return self.jobs.get(jid)

    def queue_state(self) -> dict:
        with self._lock:
            return {"running": self._running, "queued": list(self._queue)}


# --------------------------------------------------------------------------- 命令行构造
def build_command(root: str, req: dict, prompt_path: str) -> list[str]:
    """把请求翻译成 run_h3.sh 的参数。参数名与 scripts/h3_generate.py 一一对应。"""
    cmd = [os.path.join(root, "run_h3.sh"), "gen",
           "--prompt-file", prompt_path,
           "--width", str(req["width"]), "--height", str(req["height"]),
           "--num-frames", str(req["num_frames"]), "--steps", str(req["steps"]),
           "--seed", str(req["seed"]),
           "--out", req["out"]]
    if req.get("preset"):
        cmd += ["--preset", str(req["preset"])]
    for r in req.get("refs") or []:
        kind = r.get("kind") or "image"
        flag = {"image": "--ref-image", "video": "--ref-video",
                "video_audio": "--ref-video-audio", "audio": "--ref-audio"}.get(kind)
        if flag and r.get("path"):
            cmd += [flag, r["path"]]
    for key, flag in (("ref_image_short_edge", "--ref-image-short-edge"),
                      ("ref_video_short_edge", "--ref-video-short-edge"),
                      ("ref_video_max_pixels", "--ref-video-max-pixels"),
                      ("vram_limit", "--vram-limit"),
                      ("activation_reserve", "--activation-reserve"),
                      ("tile_size", "--tile-size"),
                      ("tile_overlap", "--tile-overlap"),
                      ("lora_alpha", "--lora-alpha"),
                      ("beta_alpha", "--beta-alpha"),
                      ("beta_beta", "--beta-beta")):
        if req.get(key) is not None:
            cmd += [flag, str(req[key])]
    if req.get("dit_onload"):
        cmd += ["--dit-onload", str(req["dit_onload"])]
    if req.get("sdpa_backend"):
        cmd += ["--sdpa-backend", str(req["sdpa_backend"])]
    if req.get("no_tiled"):
        cmd += ["--no-tiled"]
    if req.get("lora"):
        cmd += ["--lora", str(req["lora"])]
    if req.get("scheduler"):
        cmd += ["--scheduler", str(req["scheduler"])]
    if req.get("refresh_text_cache"):
        cmd += ["--refresh-text-cache"]
    if req.get("load_only"):
        cmd = cmd[:2] + ["--load-only"] + cmd[2:]
    return cmd


def make_request(raw: dict, cfg: dict, spec: dict) -> tuple[dict, list[str], list[dict]]:
    """校验 + 归一化前端提交的请求。返回 (request, errors, warnings)。"""
    errors: list[str] = []
    notes: list[str] = []
    limits = cfg.get("limits") or {}
    root = cfg["root"]

    def as_int(key, default):
        try:
            return int(raw.get(key, default))
        except (TypeError, ValueError):
            errors.append(f"{key} 必须是整数")
            return default

    width, height = as_int("width", 832), as_int("height", 480)
    num_frames, steps = as_int("num_frames", 124), as_int("steps", 30)
    seed = as_int("seed", 42)

    if num_frames > limits.get("max_num_frames", 400):
        errors.append(f"帧数 {num_frames} 超过上限 {limits.get('max_num_frames')}")
    if steps > limits.get("max_steps", 80):
        errors.append(f"步数 {steps} 超过上限 {limits.get('max_steps')}")
    if steps < 1:
        errors.append("步数至少为 1")
    if width < 160 or height < 160 or width > 1920 or height > 1920:
        errors.append(f"分辨率 {width}x{height} 超出允许范围（160~1920，且必须是 32 的倍数）")

    chinese = (raw.get("prompt") or "").strip()
    if not chinese:
        errors.append("提示词不能为空")
    elif len(chinese) > limits.get("max_prompt_chars", 12000):
        errors.append(f"提示词过长（{len(chinese)} > {limits.get('max_prompt_chars')} 字符）")
    if chinese and not any("\u4e00" <= c <= "\u9fff" for c in chinese):
        notes.append("提示词里没有中文：如果你已经写好了英文提示词，这是正常的；"
                     "否则建议先点「优化提示词」。")

    # 参考素材
    refs_in = raw.get("refs") or []
    ext_allowed = limits.get("allowed_ref_ext") or {}
    refs: list[dict] = []
    for r in refs_in:
        kind = (r or {}).get("kind") or "image"
        path = (r or {}).get("path") or ""
        if not path:
            continue
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(root, p)
        if not os.path.exists(p):
            errors.append(f"参考素材不存在：{path}")
            continue
        exts = ext_allowed.get(kind) or []
        if exts and os.path.splitext(p)[1].lower() not in exts:
            errors.append(f"参考素材 {os.path.basename(p)} 的扩展名不在允许列表 {exts} 内")
            continue
        item = {"kind": kind, "path": p, "name": os.path.basename(p)}
        for k in ("w", "h", "frames", "seconds"):
            if r.get(k):
                item[k] = r[k]
        refs.append(item)

    # LoRA
    lora = (raw.get("lora") or "").strip()
    if lora:
        lp = os.path.expanduser(lora)
        if not os.path.isabs(lp):
            lp = os.path.join(root, lp)
        if not os.path.exists(lp):
            errors.append(f"LoRA 文件不存在：{lora}")
        else:
            lora = lp

    out_dir = cfg["paths"]["outputs_dir"]
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_name = (raw.get("out_name") or "").strip()
    if not out_name:
        out_name = f"h3_{stamp}_{width}x{height}x{num_frames}_{steps}st_seed{seed}.mp4"
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    out_name = os.path.basename(out_name)
    out_path = os.path.join(out_dir, out_name)

    request = {
        "prompt": chinese, "prompt_excerpt": chinese[:120],
        "width": width, "height": height, "num_frames": num_frames, "steps": steps,
        "seed": seed, "preset": raw.get("preset"), "refs": refs, "lora": lora or None,
        "scheduler": raw.get("scheduler") or "auto",
        "dit_onload": raw.get("dit_onload") or "cpu",
        "sdpa_backend": raw.get("sdpa_backend") or "cudnn",
        "no_tiled": bool(raw.get("no_tiled")),
        "refresh_text_cache": bool(raw.get("refresh_text_cache")),
        "load_only": bool(raw.get("load_only")),
        "out": out_path, "out_rel": safe_rel(out_path, root),
        "seconds": round(num_frames / 24.0, 2),
    }
    for k in ("ref_image_short_edge", "ref_video_short_edge", "ref_video_max_pixels",
              "vram_limit", "activation_reserve", "tile_size", "tile_overlap",
              "lora_alpha", "beta_alpha", "beta_beta"):
        v = raw.get(k)
        if v is None or v == "":
            continue
        try:
            request[k] = float(v) if k in ("vram_limit", "activation_reserve",
                                           "lora_alpha", "beta_alpha", "beta_beta") else int(v)
        except (TypeError, ValueError):
            errors.append(f"{k} 的取值 {v!r} 不是数字")

    if request["num_frames"] < 22:
        notes.append(f"帧数 {request['num_frames']} 低于实测可用下限 22：形状检查能过，"
                     "但 VAE 解码阶段会失败（decode_video 返回 None）。")
    if request["num_frames"] % 17 != 5:
        notes.append(f"帧数 {request['num_frames']} 不是合法的 5+17k，框架会自动吸附；"
                     "建议改用 estimate 给出的合法值。")
    if request["width"] % 32 or request["height"] % 32:
        notes.append("宽高不是 32 的倍数，框架会自动向上吸附。")

    points = (spec or {}).get("measured") or []
    request["_estimate"] = estimate(request["width"], request["height"], request["num_frames"],
                                    request["steps"], points, refs=refs,
                                    ref_image_short_edge=request.get("ref_image_short_edge"),
                                    ref_video_short_edge=request.get("ref_video_short_edge"))
    warn = risks(request["width"], request["height"], request["num_frames"],
                 request["_estimate"]["rows"]["seq_len"],
                 vram_limit=request.get("vram_limit"), dit_onload=request["dit_onload"],
                 lora=request.get("lora"), steps=request["steps"], refs=refs,
                 ref_image_short_edge=request.get("ref_image_short_edge"),
                 ref_video_short_edge=request.get("ref_video_short_edge"), spec=spec)
    return request, errors, notes + [w["msg"] for w in warn if w.get("level") == "danger"]
