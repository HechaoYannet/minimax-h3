"""webui.backend.server -- 前端 API 服务（Windows 浏览器 <-> WSL 后端）。

只依赖标准库：http.server + threading。原因见 util.py：这台机器内存紧张，
后端不该为了一组 REST 路由去装 FastAPI/uvicorn 并常驻更多内存。

端点一览（协议细节见 webui/README.md）：
  GET  /api/health                  服务与生成环境自检
  GET  /api/config                  参数规格 + 预设 + 服务配置（不含 API Key）
  GET  /api/telemetry               当前硬件占用 + 最近曲线
  GET  /api/lora                    可用的 LoRA 文件
  GET  /api/logs                    运行日志查询（level/source/q/分页 + 统计）
  GET  /api/logs/stream             SSE：运行日志实时流（支持同样的过滤）
  GET  /api/logs/download           导出当前筛选的日志（txt / json）
  POST /api/logs/clear              清空内存缓冲（files=true 连磁盘文件一起清）
  POST /api/logs/level              运行时切换日志等级（debug/info/warn/error）
  POST /api/logs/client             浏览器端日志上报（前端异常、关键动作）
  GET  /api/llm/runs                LLM 调用记录列表（每次「优化提示词」一条）
  GET  /api/llm/runs/<id>           单次 LLM 调用的完整记录（请求/输出/usage/报错）
  GET  /api/jobs                    作业列表
  POST /api/jobs                    提交生成作业
  GET  /api/jobs/<id>               单个作业
  GET  /api/jobs/<id>/stream        SSE：作业事件流
  GET  /api/jobs/<id>/log           作业原始日志尾部
  POST /api/jobs/<id>/cancel        取消作业
  GET  /api/jobs/<id>/result        产物路径（含 Windows 视角路径）
  POST /api/optimize                SSE：DeepSeek 提示词优化
  POST /api/upload                  上传参考素材 -> 返回 WSL 内路径
  POST /api/paths                   把 Windows 路径翻译成 WSL 路径并校验存在性

夸克网盘（config/quark.yaml，见 backend/quark.py）：
  GET  /api/disk/status             网盘 CLI / node / 授权状态（probe=0 只看缓存）
  GET  /api/disk/files             浏览网盘目录（parent_fid / page_size / all）
  GET  /api/disk/search            搜索网盘文件（keyword / size / search_type）
  POST /api/disk/login             发起授权登录（不带 token 走浏览器 OAuth）
  POST /api/disk/publish           压缩 + 加密打包 + 上传 + 建分享链接（长任务）
  POST /api/disk/fetch             把网盘文件下载回本机（长任务）
  GET  /api/disk/tasks             网盘任务列表
  GET  /api/disk/tasks/<id>         单个任务
  GET  /api/disk/tasks/<id>/stream  SSE：任务事件流
  GET  /api/disk/tasks/<id>/log     任务原始输出尾部
  POST /api/disk/tasks/<id>/cancel  取消任务
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import config as cfgmod
from .estimate import estimate, normalise_shape, risks
from .jobs import JobManager, build_command, make_request
from .pack import packer_state
from .promptopt import Optimizer
from .quark import DiskManager, QuarkError
from .runlog import LEVEL_ORDER, LLMRunStore, RunLog
from .telemetry import Telemetry
from .util import (human_dur, iso, now, read_json, repo_root, tail_lines, wsl_to_windows,
                   write_json)

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".mp4": "video/mp4", ".md": "text/markdown; charset=utf-8",
}


def _q1(q: dict, key: str, default=None):
    """取 querystring 的第一个值（parse_qs 的值永远是列表）。"""
    v = q.get(key)
    if not v:
        return default
    return v[0]


def _qint(q: dict, key: str, default: int = 0) -> int:
    try:
        return int(_q1(q, key, default))
    except (TypeError, ValueError):
        return default


class App:
    """进程级共享状态。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = cfg["root"]
        # ---- 运行日志：内存环形缓冲 + JSONL 落盘；LLM 调用另存完整证据 -------
        logcfg = cfg.get("logging") or {}
        self.log = RunLog(
            logcfg.get("dir") or os.path.join(self.root, "cache", "webui", "logs"),
            level=logcfg.get("level", "info"),
            capacity=logcfg.get("capacity", 2000),
            file_enabled=logcfg.get("file_enabled", True),
            max_file_mb=logcfg.get("max_file_mb", 8),
            backups=logcfg.get("backups", 5),
            console=logcfg.get("console", True),
            max_field_chars=logcfg.get("max_field_chars", 4000))
        self.log_expose = bool(logcfg.get("expose", True))
        self.llm_runs = LLMRunStore(
            os.path.join(self.log.dir, "llm"), self.log,
            enabled=logcfg.get("llm_enabled", True),
            capture=logcfg.get("llm_capture", "full"),
            max_runs=logcfg.get("llm_max_runs", 100),
            max_chars=logcfg.get("llm_max_chars", 200000))
        self.telemetry = Telemetry(
            nvidia_smi=(cfg.get("telemetry") or {}).get("nvidia_smi", "nvidia-smi"),
            interval=(cfg.get("defaults") or {}).get("telemetry_interval_s", 1.5),
            repo_root=self.root)
        self.jobs = JobManager(cfg, self.log)
        self.optimizer = Optimizer(self.log, self.llm_runs)
        # 夸克网盘：CLI 调用 + 网盘任务（加密打包上传 / 下载回本机）
        self.qcfg = cfgmod.quark_config()
        self.disk = DiskManager(cfg, self.log, self.qcfg, self.root)
        self.disk.jobs = self.jobs
        self._disk_status = {"at": 0.0, "data": None}
        self._disk_status_lock = threading.Lock()
        self.web_dir = os.path.join(self.root, "webui", "web")
        self.started = now()
        self._probe_cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 工具
    def spec(self) -> dict:
        return cfgmod.params_spec()

    def deepseek_cfg(self) -> dict:
        return cfgmod.deepseek_config()

    # ---------------------------------------------------------------- 网盘
    def disk_summary(self, fresh: bool = False) -> dict:
        """网盘配置 + CLI 环境；fresh=True 时顺带真去问一次账号（有网络开销）。"""
        q = self.qcfg
        info = self.disk.cli.envprobe.detect(fresh=False)
        out = {
            # 有没有真的问过账号？没问过时前端要显示「加载中…」，
            # 而不是把「不知道」渲染成「未授权」（那是假的确定信息）
            "auth_known": False,
            "enabled": bool(q.get("enabled", True)),
            "runner": {"mode": info.get("mode"), "node": info.get("node"),
                       "node_version": info.get("node_version"),
                       "cli": info.get("cli"), "cli_ok": info.get("ok"),
                       "reason": info.get("reason"), "wanted": info.get("runner_wanted")},
            "compress": {k: v for k, v in (q.get("compress") or {}).items()},
            "archive": {"enabled": (q.get("archive") or {}).get("enabled"),
                        "password": (q.get("archive") or {}).get("password"),
                        "level": (q.get("archive") or {}).get("level")},
            "share": dict(q.get("share") or {}),
            "upload": {"parent_fid": (q.get("upload") or {}).get("parent_fid") or "",
                       "dir_name": (q.get("upload") or {}).get("dir_name") or ""},
            # 打包实现的实际状态：没探过 / 探到哪个 bsdtar / 是否已降级 + 原因
            "packer": packer_state(),
            "download_dir": (q.get("download") or {}).get("dir"),
            "keep_work": bool(q.get("keep_work", True)),
            "session_id": self.disk.session_id,
            "source": q.get("source"),
        }
        # probe=0：把「上次已知」的授权状态一起给出去（不发网络请求）
        with self._disk_status_lock:
            cached = self._disk_status.get("data")
            warm = cached and (now() - self._disk_status["at"]) < 12
        if cached:
            out.update(cached)
            out["auth_known"] = True
        if not fresh or warm:
            return out
        probe = self.disk.cli.status_probe()
        snap = {"logged_in": probe.get("logged_in"), "account": probe.get("account"),
                "message": probe.get("message"), "need_login": probe.get("need_login")}
        with self._disk_status_lock:
            self._disk_status["at"] = now()
            self._disk_status["data"] = snap
        out.update(snap)
        out["auth_known"] = True
        return out

    def probe_media(self, path: str) -> dict:
        """用 ffprobe 读参考素材的真实尺寸/帧数/时长；拿不到就返回 available=False。"""
        if path in self._probe_cache:
            return self._probe_cache[path]
        info: dict = {"available": False}
        import shutil
        import subprocess
        exe = shutil.which("ffprobe")
        if exe and os.path.exists(path):
            try:
                p = subprocess.run(
                    [exe, "-v", "error", "-print_format", "json", "-show_streams",
                     "-show_format", path],
                    capture_output=True, text=True, timeout=20)
                if p.returncode == 0:
                    data = json.loads(p.stdout or "{}")
                    v = next((s for s in data.get("streams", [])
                              if s.get("codec_type") == "video"), None)
                    a = next((s for s in data.get("streams", [])
                              if s.get("codec_type") == "audio"), None)
                    info = {"available": True,
                            "duration_s": float(data.get("format", {}).get("duration") or 0) or None,
                            "has_audio": bool(a)}
                    if v:
                        if int(v.get("width") or 0) and int(v.get("height") or 0):
                            info["w"] = int(v.get("width"))
                            info["h"] = int(v.get("height"))
                        else:
                            # 有 video 流但读不到尺寸（损坏/截断的文件）：别让调用方
                            # 拿到 w=0 去做「0×0 参考图」这种荒谬的估算
                            info["available"] = False
                            info["error"] = "ffprobe 读不到视频尺寸（文件可能损坏或不完整）"
                        fr = v.get("avg_frame_rate") or "0/1"
                        try:
                            num, den = fr.split("/")
                            fps = float(num) / float(den) if float(den) else 0
                        except Exception:
                            fps = 0
                        info["fps"] = round(fps, 3)
                        if info.get("duration_s") and fps:
                            info["frames"] = int(round(info["duration_s"] * fps))
            except Exception as e:
                info = {"available": False, "error": str(e)[:200]}
        with self._lock:
            self._probe_cache[path] = info
        return info


APP: App | None = None


# --------------------------------------------------------------------------- multipart
def parse_multipart(body: bytes, boundary: bytes) -> dict[str, dict]:
    """极简 multipart/form-data 解析：返回 {name: {"filename":..., "data": bytes}}。"""
    out: dict[str, dict] = {}
    delim = b"--" + boundary
    parts = body.split(delim)
    for part in parts[1:]:
        if part[:2] == b"--":
            break
        part = part.lstrip(b"\r\n")
        head, _, data = part.partition(b"\r\n\r\n")
        if not _:
            continue
        data = data.rstrip(b"\r\n")
        headers = head.decode("utf-8", "replace")
        m = re.search(r'name="([^"]*)"', headers)
        if not m:
            continue
        name = m.group(1)
        fn = re.search(r'filename="([^"]*)"', headers)
        out[name] = {"filename": fn.group(1) if fn else None, "data": data}
    return out


# --------------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "h3-webui/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础输出
    def log_message(self, fmt, *args):  # 默认会往 stderr 刷；请求日志统一在 _route 里记
        pass

    def log_error(self, fmt, *args):
        if APP:
            APP.log.debug(f"HTTP: {fmt % args}", source="http", event="http.error",
                          ip=self.address_string())

    def send_response(self, code, message=None):
        # 记下状态码，_route 结束时连同耗时写一条请求日志
        self._status = int(code)
        super().send_response(code, message)

    def _log_request(self, method: str, path: str, q: dict, dur: float):
        app = APP
        if app is None or not getattr(app, "log_expose", True):
            return
        status = getattr(self, "_status", 0)
        if path == "/api/telemetry" and status < 400:
            return  # 前端 1.5s 轮询一次，记下来只会淹没别的信息
        fields = {"method": method, "path": path, "status": status,
                  "dur_ms": int(dur * 1000), "ip": self.address_string()}
        query = {k: (v[0] if v else "") for k, v in (q or {}).items()}
        if query:
            fields["query"] = query
        if status >= 500:
            app.log.error("请求失败", source="http", event="http.request", **fields)
        elif status >= 400:
            app.log.warn("请求被拒绝", source="http", event="http.request", **fields)
        elif not getattr(self, "_err_logged", False):
            app.log.debug("请求完成", source="http", event="http.request", **fields)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def err(self, code: int, message: str, **extra):
        self.json({"ok": False, "error": message, **extra}, code)

    def sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # SSE 是「一次请求一次响应」：没有 Content-Length，必须让 handler 返回时关连接，
        # 否则 HTTP/1.1 keep-alive 会让客户端一直等 body 结束（/api/optimize 曾因此挂到超时）。
        self.close_connection = True

    def sse(self, event: str, data) -> bool:
        """返回 False 表示客户端已断开。"""
        try:
            payload = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = json.dumps({"_repr": repr(data)}, ensure_ascii=False)
        try:
            self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def sse_ping(self) -> bool:
        try:
            self.wfile.write(b": ping\n\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n else b""

    def json_body(self) -> dict:
        raw = self.body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"请求体不是合法 JSON：{e}") from e

    # ---- 路由
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        q = parse_qs(parsed.query)
        self._status = 0
        self._err_logged = False
        t0 = time.time()
        try:
            if path.startswith("/api/"):
                self._api(method, path, q)
            elif method == "GET":
                self._static(path)
            else:
                self.err(405, "不支持的方法")
        except QuarkError as e:
            # 网盘命令的业务失败：未授权给 401（前端据此直接顶出「去授权」），其余给 400
            self._err_logged = e.need_login
            if APP and not e.need_login:
                APP.log.info(f"{method} {path} 网盘命令失败：{e}", source="http",
                             event="disk.cli_failed", code=e.code)
            self.err(401 if e.need_login else 400, str(e),
                     need_login=bool(e.need_login), cli_code=e.code)
        except ValueError as e:
            tb = traceback.format_exc()
            self._err_logged = True
            if APP:
                APP.log.warn(f"{method} {path} 参数错误：{e}", source="http",
                             event="http.bad_request", trace=tb[-1500:])
            print(f"[api] {method} {path} bad request:\n{tb}", flush=True)
            self.err(400, str(e), trace=tb.strip().splitlines()[-8:])
        except Exception as e:
            tb = traceback.format_exc()
            self._err_logged = True
            if APP:
                APP.log.error(f"{method} {path} 失败：{e}", source="http",
                              event="http.exception", trace=tb[-1500:])
            print(f"[api] {method} {path} failed:\n{tb}", flush=True)
            self.err(500, f"服务端错误：{e}", trace=tb.strip().splitlines()[-6:])
        finally:
            self._log_request(method, path, q, time.time() - t0)

    # ---- 静态资源
    def _static(self, path: str):
        if APP is None:
            return self.err(503, "服务未就绪")
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(APP.web_dir, rel))
        if not full.startswith(APP.web_dir):
            return self.err(403, "越界路径")
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.exists(full):
            # SPA 回退：单页应用刷新任意路径都回到 index.html
            full = os.path.join(APP.web_dir, "index.html")
            if not os.path.exists(full):
                return self.err(404, "找不到前端文件；确认 webui/web/index.html 存在")
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---- API
    def _api(self, method: str, path: str, q: dict):
        app = APP
        assert app is not None
        seg = [s for s in path.split("/") if s]  # ["api", ...]

        if seg == ["api", "health"] and method == "GET":
            return self.api_health()
        if seg == ["api", "config"] and method == "GET":
            return self.api_config()
        if seg == ["api", "file"] and method == "GET":
            return self.api_file(q)
        if seg == ["api", "telemetry"] and method == "GET":
            return self.api_telemetry(q)
        if seg == ["api", "lora"] and method == "GET":
            return self.api_lora()
        if seg == ["api", "logs"] and method == "GET":
            return self.api_logs(q)
        if seg == ["api", "logs", "stream"] and method == "GET":
            return self.stream_logs(q)
        if seg == ["api", "logs", "download"] and method == "GET":
            return self.api_logs_download(q)
        if seg == ["api", "logs", "clear"] and method == "POST":
            return self.api_logs_clear()
        if seg == ["api", "logs", "level"] and method == "POST":
            return self.api_logs_level()
        if seg == ["api", "logs", "client"] and method == "POST":
            return self.api_logs_client()
        if seg == ["api", "llm", "runs"] and method == "GET":
            return self.api_llm_runs(q)
        if len(seg) == 4 and seg[:3] == ["api", "llm", "runs"] and method == "GET":
            return self.api_llm_run(seg[3])
        if seg == ["api", "estimate"] and method == "POST":
            return self.api_estimate()
        if seg == ["api", "preview"] and method == "POST":
            return self.api_preview()
        if seg == ["api", "jobs"] and method == "GET":
            return self.json({"ok": True, "jobs": app.jobs.list(int(q.get("limit", ["30"])[0])),
                              "queue": app.jobs.queue_state()})
        if seg == ["api", "jobs"] and method == "POST":
            return self.api_submit()
        # /api/jobs/<id> 与 /api/jobs/<id>/<action>：段数决定走哪一支
        if len(seg) == 3 and seg[:2] == ["api", "jobs"] and method == "GET":
            j = app.jobs.get(seg[2])
            if not j:
                return self.err(404, "作业不存在")
            return self.json({"ok": True, "job": j.to_dict()})
        if len(seg) == 4 and seg[:2] == ["api", "jobs"]:
            jid, action = seg[2], seg[3]
            j = app.jobs.get(jid)
            if not j:
                return self.err(404, "作业不存在（服务重启后只保留最近 50 条记录）")
            if action == "stream" and method == "GET":
                return self.stream_job(j, int(q.get("from", ["0"])[0]))
            if action == "log" and method == "GET":
                n = int(q.get("n", ["300"])[0])
                return self.json({"ok": True, "lines": tail_lines(j.paths["log"], n)})
            if action == "events" and method == "GET":
                return self.json({"ok": True, "events": j.events_after(int(q.get("from", ["0"])[0]))})
            if action == "cancel" and method == "POST":
                ok, msg = app.jobs.cancel(jid)
                return self.json({"ok": ok, "message": msg}, 200 if ok else 409)
            if action == "result" and method == "GET":
                return self.api_result(j)
        # ---- 夸克网盘
        if seg == ["api", "disk", "status"] and method == "GET":
            return self.api_disk_status(q)
        if seg == ["api", "disk", "files"] and method == "GET":
            return self.api_disk_files(q)
        if seg == ["api", "disk", "search"] and method == "GET":
            return self.api_disk_search(q)
        if seg == ["api", "disk", "login"] and method == "POST":
            return self.api_disk_login()
        if seg == ["api", "disk", "publish"] and method == "POST":
            return self.api_disk_publish()
        if seg == ["api", "disk", "fetch"] and method == "POST":
            return self.api_disk_fetch()
        if seg == ["api", "disk", "tasks"] and method == "GET":
            return self.json({"ok": True, "tasks": app.disk.list(_qint(q, "limit", 30)),
                              "running": app.disk._running})
        if len(seg) == 4 and seg[:3] == ["api", "disk", "tasks"] and method == "GET":
            t = app.disk.get(seg[3])
            if not t:
                return self.err(404, "网盘任务不存在")
            return self.json({"ok": True, "task": t.to_dict()})
        if len(seg) == 5 and seg[:3] == ["api", "disk", "tasks"]:
            tid, action = seg[3], seg[4]
            t = app.disk.get(tid)
            if not t:
                return self.err(404, "网盘任务不存在")
            if action == "stream" and method == "GET":
                return self.stream_disk(t, _qint(q, "from", 0))
            if action == "log" and method == "GET":
                return self.json({"ok": True, "lines": tail_lines(t.paths["log"], _qint(q, "n", 300))})
            if action == "cancel" and method == "POST":
                ok, msg = app.disk.cancel(tid)
                return self.json({"ok": ok, "message": msg}, 200 if ok else 409)
            if action == "events" and method == "GET":
                return self.json({"ok": True, "events": t.events_after(_qint(q, "from", 0))})
        if seg == ["api", "optimize"] and method == "POST":
            return self.api_optimize()
        if seg == ["api", "upload"] and method == "POST":
            return self.api_upload()
        if seg == ["api", "paths"] and method == "POST":
            return self.api_paths()
        return self.err(404, f"未知端点 {path}")

    # ---- 各端点实现
    def api_health(self):
        app = APP
        assert app is not None
        root = app.root
        checks = []
        gen = os.path.join(root, "scripts", "h3_generate.py")
        run = os.path.join(root, "run_h3.sh")
        checks.append({"name": "仓库根目录", "ok": os.path.isdir(root), "detail": root})
        checks.append({"name": "生成入口 run_h3.sh", "ok": os.path.exists(run),
                       "detail": run if os.path.exists(run) else "文件缺失"})
        checks.append({"name": "生成脚本 h3_generate.py", "ok": os.path.exists(gen),
                       "detail": gen if os.path.exists(gen) else "文件缺失"})
        spec = app.spec()
        checks.append({"name": "参数规格 config/params.spec.json",
                       "ok": bool(spec.get("params")), "detail": spec.get("error") or "ok"})
        plan = os.path.join(root, "cache", "plan.json")
        checks.append({"name": "调优方案 cache/plan.json", "ok": os.path.exists(plan),
                       "detail": plan})
        ds = app.deepseek_cfg()
        checks.append({"name": "DeepSeek API Key", "ok": ds["api"]["key_present"],
                       "detail": ("已从 " + ds["api"]["key_source"] + " 读取") if ds["api"]["key_present"]
                       else "未配置：提示词优化不可用，但生成不受影响"})
        import shutil
        ff = shutil.which("ffprobe")
        checks.append({"name": "ffprobe（读参考素材尺寸）", "ok": bool(ff),
                       "detail": ff or "未找到：参考素材尺寸需要手填"})
        ffm = shutil.which("ffmpeg")
        mm = ds.get("multimodal") or {}
        mm_on = bool(mm.get("enabled"))
        checks.append({"name": "ffmpeg（多模态附图缩放/视频抽帧）",
                       "ok": bool(ffm) or not mm_on,
                       "detail": (ffm or "未找到")
                       + ("；multimodal 已开启" if mm_on else "；multimodal 已关闭，不影响纯文本")
                       + ("" if ffm else "；缺它就只能附未压缩的原图，视频抽帧不可用")})
        gpu = app.telemetry.current().get("gpu") or {}
        checks.append({"name": "nvidia-smi", "ok": bool(gpu.get("ok")),
                       "detail": gpu.get("name") or gpu.get("error")})
        # 夸克网盘：CLI 能不能跑（不发网络请求，授权状态在「网盘」页签看）
        dinfo = app.disk_summary(fresh=False).get("runner") or {}
        dq = app.qcfg
        d_on = bool(dq.get("enabled", True))
        checks.append({"name": "夸克网盘 CLI（加密打包上传 / 下载到本机）",
                       "ok": (not d_on) or bool(dinfo.get("cli_ok")),
                       "detail": ("已关闭（config/quark.yaml 的 enabled=false）" if not d_on else
                                  (f"{dinfo.get('mode') or '?'} runner · {dinfo.get('node_version') or '?'}"
                                   f" · {dinfo.get('cli')}" if dinfo.get("cli_ok")
                                   else (dinfo.get("reason") or "不可用")))})
        checks.append({"name": "ffmpeg（网盘上传前的视频压缩）",
                       "ok": bool(ffm) or not ((dq.get("compress") or {}).get("enabled")),
                       "detail": (ffm or "未找到")
                       + ("；compress.enabled=true，缺 ffmpeg 时只打包不转码"
                          if (dq.get("compress") or {}).get("enabled") else "；压缩已关闭")})
        lstats = app.log.stats()
        checks.append({"name": "运行日志（落盘）",
                       "ok": not (app.log.file_enabled and lstats.get("file_error")),
                       "detail": (f"{lstats['level']} · {lstats['retained']}/{lstats['capacity']} 条"
                                  f" · {app.log.dir}"
                                  + (f" · 写盘错误：{lstats['file_error']}" if lstats.get("file_error")
                                     else ""))})
        return self.json({"ok": True, "checks": checks, "root": root,
                          "uptime_s": round(now() - app.started, 1),
                          "python": os.sys.version.split()[0],
                          "host": os.uname().nodename if hasattr(os, "uname") else "?",
                          "repo_windows": wsl_to_windows(root),
                          "logging": lstats, "llm": app.llm_runs.stats()})

    def api_file(self, q):
        """把 WSL 里的媒体文件发给浏览器（前端预览产物/参考图用）。

        只允许仓库目录与模型目录下的文件：前端是本地单人工具，但也不能变成一个
        任意文件读取的口子。Range 请求按整文件返回（这些文件是本地磁盘，够快了）。
        """
        app = APP
        assert app is not None
        raw = (q.get("path") or [""])[0]
        if not raw:
            return self.err(400, "缺少 path 参数")
        p = os.path.realpath(os.path.expanduser(raw))
        allowed = [os.path.realpath(app.root)]
        models = os.environ.get("H3_MODELS")
        if models:
            allowed.append(os.path.realpath(models))
        if not any(p == a or p.startswith(a + os.sep) for a in allowed):
            return self.err(403, "只允许读取仓库目录或模型目录下的文件")
        if not os.path.isfile(p):
            return self.err(404, "文件不存在")
        ctype = STATIC_TYPES.get(os.path.splitext(p)[1].lower()) or {
            ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
            ".wav": "audio/wav", ".mp3": "audio/mpeg",
        }.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def api_config(self):
        app = APP
        assert app is not None
        cfg = app.cfg
        ds = app.deepseek_cfg()
        model = (ds.get("model") or {})
        return self.json({
            "ok": True,
            "spec": app.spec(),
            "server": {
                "defaults": cfg.get("defaults"),
                "limits": cfg.get("limits"),
                "paths": cfg.get("paths"),
                "telemetry_interval_s": (cfg.get("defaults") or {}).get("telemetry_interval_s", 1.5),
                "expose_server_log": (cfg.get("telemetry") or {}).get("expose_server_log", True),
                "max_concurrent": (cfg.get("jobs") or {}).get("max_concurrent", 1),
            },
            # 运行日志与 LLM 记录：页面据此决定显示哪些面板/默认筛选
            "logging": {
                "level": app.log.level,
                "expose": app.log_expose,
                "dir": app.log.dir,
                "capacity": app.log.capacity,
                "file_enabled": app.log.file_enabled,
                "llm_enabled": app.llm_runs.enabled,
                "llm_capture": app.llm_runs.capture,
                "llm_max_runs": app.llm_runs.max_runs,
                "levels": LEVEL_ORDER,
            },
            "deepseek": {
                "url": (ds.get("api") or {}).get("url"),
                "model": model.get("name"),
                "thinking": model.get("thinking"),
                "temperature": model.get("temperature"),
                "max_tokens": model.get("max_tokens"),
                "stream": model.get("stream"),
                "key_present": (ds.get("api") or {}).get("key_present"),
                "key_source": (ds.get("api") or {}).get("key_source"),
                "want_translation": (ds.get("prompt") or {}).get("want_translation", True),
                "system_prompt_file": (ds.get("prompt") or {}).get("system_prompt_file"),
                # 多模态附图配置（不含任何密钥），前端据此显示「附图 N 张」等提示
                "multimodal": ds.get("multimodal") or {},
            },
            # 夸克网盘：只给配置与本地环境探测，不发网络请求（真状态在 /api/disk/status）
            "disk": app.disk_summary(fresh=False),
            "wsl": {"distro": os.environ.get("WSL_DISTRO_NAME"),
                    "repo_root": cfg["root"], "repo_windows": wsl_to_windows(cfg["root"])},
        })

    def api_telemetry(self, q):
        app = APP
        assert app is not None
        n = int(q.get("history", ["120"])[0])
        cur = app.telemetry.current()
        return self.json({"ok": True, "now": cur, "peaks": app.telemetry.peaks(),
                          "history": app.telemetry.history_list(n),
                          "interval_s": app.telemetry.interval,
                          "server_time": iso()})

    # ---- 运行日志 -----------------------------------------------------
    def _log_exposed(self) -> bool:
        app = APP
        return bool(app and getattr(app, "log_expose", True))

    def api_logs(self, q):
        """运行日志查询：支持 level / source / q / since_seq / n / offset 过滤。

        records 是结构化记录；lines 与 records 等价，保留给老脚本。
        """
        app = APP
        assert app is not None
        if not self._log_exposed():
            return self.err(403, "运行日志未对外暴露（config/server.yaml: logging.expose=false）")
        res = app.log.query(n=_qint(q, "n", 300), offset=_qint(q, "offset", 0),
                            level=_q1(q, "level"), source=_q1(q, "source"),
                            q=_q1(q, "q"), since_seq=_qint(q, "since_seq", 0))
        res.update({"ok": True, "stats": app.log.stats(), "llm": app.llm_runs.stats(),
                    "level": app.log.level, "levels": LEVEL_ORDER})
        res["lines"] = res["records"]
        return self.json(res)

    def api_logs_download(self, q):
        """把当前筛选下的日志导出成 txt / json，方便贴给别人或存档。"""
        app = APP
        assert app is not None
        if not self._log_exposed():
            return self.err(403, "运行日志未对外暴露")
        recs = app.log.tail(_qint(q, "n", 2000), level=_q1(q, "level"),
                            source=_q1(q, "source"), q=_q1(q, "q"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if (_q1(q, "format", "txt") or "txt").lower() == "json":
            body = json.dumps({"generated": iso(), "stats": app.log.stats(),
                               "records": recs}, ensure_ascii=False, indent=2).encode("utf-8")
            name, ctype = f"webui-log-{stamp}.json", "application/json; charset=utf-8"
        else:
            header = ("# MiniMax-H3 webui 运行日志导出\n"
                      f"# 生成时间 {iso()}  条数 {len(recs)}  "
                      f"level={_q1(q, 'level') or 'all'} source={_q1(q, 'source') or 'all'} "
                      f"q={_q1(q, 'q') or '-'}\n")
            body = (header + app.log.format_text(recs)).encode("utf-8")
            name, ctype = f"webui-log-{stamp}.txt", "text/plain; charset=utf-8"
        self._send(200, body, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})

    def api_logs_clear(self):
        app = APP
        assert app is not None
        if not self._log_exposed():
            return self.err(403, "运行日志未对外暴露")
        body = self.json_body()
        res = app.log.clear(files=bool(body.get("files")))
        return self.json({"ok": True, **res, "stats": app.log.stats()})

    def api_logs_level(self):
        """运行时切换日志等级（页面「日志」页签的下拉框就是它的入口）。"""
        app = APP
        assert app is not None
        body = self.json_body()
        level = str(body.get("level") or "").lower()
        if level not in LEVEL_ORDER:
            return self.err(400, f"level 必须是 {'/'.join(LEVEL_ORDER)} 之一")
        app.log.set_level(level)
        return self.json({"ok": True, "level": app.log.level, "stats": app.log.stats()})

    def api_logs_client(self):
        """浏览器端上报：前端异常 / 关键动作写进同一份运行日志。

        用 sendBeacon 时 Content-Type 可能是 text/plain，所以这里不看头，直接解析 body。
        单次限制 50 条、64 KiB，避免前端异常风暴把日志文件冲爆。
        """
        app = APP
        assert app is not None
        if not self._log_exposed():
            return self.err(403, "运行日志未对外暴露")
        raw = self.body()
        if len(raw) > 64 * 1024:
            return self.err(413, "上报内容超过 64 KiB")
        if not raw:
            return self.err(400, "空的上报内容")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            return self.err(400, f"上报内容不是合法 JSON：{e}")
        items = data if isinstance(data, list) else [data]
        accepted = 0
        for it in items[:50]:
            if not isinstance(it, dict):
                continue
            it = dict(it)
            it.setdefault("source", "ui")
            if app.log.ingest(it):
                accepted += 1
        return self.json({"ok": True, "accepted": accepted})

    # ---- LLM 调用记录 -------------------------------------------------
    def api_llm_runs(self, q):
        app = APP
        assert app is not None
        if not self._log_exposed():
            return self.err(403, "运行日志未对外暴露")
        runs = app.llm_runs.list_runs(limit=_qint(q, "limit", 50),
                                      status=_q1(q, "status"), q=_q1(q, "q"))
        return self.json({"ok": True, "runs": runs, "stats": app.llm_runs.stats()})

    def api_llm_run(self, run_id: str):
        app = APP
        assert app is not None
        if not self._log_exposed():
            return self.err(403, "运行日志未对外暴露")
        run = app.llm_runs.get(run_id)
        if not run:
            return self.err(404, "没有这条调用记录（可能已被 llm_max_runs 清理）")
        return self.json({"ok": True, "run": run})

    def api_lora(self):
        app = APP
        assert app is not None
        import glob
        models_dir = os.environ.get("H3_MODELS") or os.path.expanduser("~/source/minimax-h3/models")
        files = []
        for pat in ("*.safetensors", "*/*.safetensors"):
            for p in glob.glob(os.path.join(models_dir, pat)):
                base = os.path.basename(p)
                if base.endswith(":Zone.Identifier") or base.startswith(("minimax-h3-", "video_vae", "audio_vae")):
                    continue
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                files.append({"path": p, "name": base,
                              "size_gib": round(size / 1024 ** 3, 2),
                              "mtime": iso(os.path.getmtime(p))})
        files.sort(key=lambda x: x["mtime"], reverse=True)
        return self.json({"ok": True, "dir": models_dir, "files": files})

    def api_estimate(self):
        """实时预估：形状吸附 + seq + 耗时 + 风险。前端改一个参数就调一次。"""
        app = APP
        assert app is not None
        raw = self.json_body()
        spec = app.spec()
        w, h, f, notes = normalise_shape(int(raw.get("width") or 832),
                                         int(raw.get("height") or 480),
                                         int(raw.get("num_frames") or 124))
        steps = int(raw.get("steps") or 30)
        refs = raw.get("refs") or []
        ea = estimate(w, h, f, steps, spec.get("measured") or [], refs=refs,
                      ref_image_short_edge=raw.get("ref_image_short_edge"),
                      ref_video_short_edge=raw.get("ref_video_short_edge"),
                      ref_video_max_pixels=raw.get("ref_video_max_pixels"))
        rk = risks(w, h, f, ea["rows"]["seq_len"], vram_limit=raw.get("vram_limit"),
                   dit_onload=raw.get("dit_onload") or "cpu", lora=raw.get("lora"),
                   steps=steps, refs=refs,
                   ref_image_short_edge=raw.get("ref_image_short_edge"),
                   ref_video_short_edge=raw.get("ref_video_short_edge"), spec=spec)
        return self.json({"ok": True, "aligned": {"width": w, "height": h, "num_frames": f,
                                                  "seconds": round(f / 24.0, 2)},
                          "shape_notes": notes, "estimate": ea, "risks": rk})

    def api_preview(self):
        """干跑：真正解析一遍参数（不加载任何权重），把命令与解析结果返回。"""
        app = APP
        assert app is not None
        raw = self.json_body()
        spec = app.spec()
        request, errors, warnings = make_request(raw, app.cfg, spec)
        if errors:
            return self.json({"ok": False, "error": "；".join(errors), "errors": errors,
                              "warnings": warnings}, 400)
        if raw.get("load_only"):
            request["load_only"] = True
        d = os.path.join(app.cfg["paths"]["jobs_dir"], "_preview")
        os.makedirs(d, exist_ok=True)
        prompt_path = os.path.join(d, "prompt.txt")
        with open(prompt_path, "w", encoding="utf-8") as fp:
            fp.write(request["prompt"] + "\n")
        cmd = build_command(app.root, request, prompt_path)
        return self.json({"ok": True, "cmd": ["bash"] + cmd, "request": request,
                          "warnings": warnings})

    def api_submit(self):
        app = APP
        assert app is not None
        raw = self.json_body()
        spec = app.spec()
        request, errors, warnings = make_request(raw, app.cfg, spec)
        if errors:
            return self.json({"ok": False, "error": "；".join(errors), "errors": errors,
                              "warnings": warnings}, 400)
        # 防呆：硬件明显不够时也要用户明确确认一次
        rk = risks(request["width"], request["height"], request["num_frames"],
                   request["_estimate"]["rows"]["seq_len"],
                   vram_limit=request.get("vram_limit"), dit_onload=request["dit_onload"],
                   lora=request.get("lora"), steps=request["steps"], refs=request["refs"],
                   ref_image_short_edge=request.get("ref_image_short_edge"),
                   ref_video_short_edge=request.get("ref_video_short_edge"), spec=spec)
        dangers = [r for r in rk if r["level"] == "danger"]
        if dangers and not raw.get("force"):
            return self.json({"ok": False, "error": "该配置被判定为大概率失败",
                              "risks": rk, "need_force": True,
                              "hint": "确认要继续的话带上 force=true 重新提交。"}, 409)
        # 提示词落盘（避免命令行里塞长文本）
        stamp = time.strftime("%Y%m%d-%H%M%S")
        d = os.path.join(app.cfg["paths"]["jobs_dir"], "pending-" + stamp)
        os.makedirs(d, exist_ok=True)
        prompt_path = os.path.join(d, "prompt.txt")
        with open(prompt_path, "w", encoding="utf-8") as fp:
            fp.write(request["prompt"] + "\n")
        cmd = build_command(app.root, request, prompt_path)
        j = app.jobs.submit(request, cmd)
        return self.json({"ok": True, "job": j.to_dict(), "warnings": warnings,
                          "risks": rk, "cmd": ["bash"] + cmd})

    def api_result(self, j):
        app = APP
        assert app is not None
        out = (j.result or {}).get("out") or j.request.get("out")
        data = {"ok": True, "job": j.to_dict(include_request=False)}
        if out and os.path.exists(out):
            data["output"] = {
                "wsl": out, "windows": wsl_to_windows(out),
                "size_mib": round(os.path.getsize(out) / 1024 ** 2, 2),
                "rel": os.path.relpath(out, app.root).replace("\\", "/"),
            }
        else:
            data["output"] = None
        return self.json(data)

    def api_paths(self):
        """把 Windows 路径翻成 WSL 路径并校验（用户在页面上直接粘贴 D:\\... 也能用）。"""
        app = APP
        assert app is not None
        raw = self.json_body()
        items = raw.get("paths") or []
        want_probe = bool(raw.get("probe"))
        out = []
        for p in items:
            wp = translate_path(p)
            exists = bool(wp) and os.path.exists(wp)
            item = {"input": p, "wsl": wp, "exists": exists,
                    "windows": wsl_to_windows(wp) if wp else None}
            if exists and want_probe:
                # 顺手 ffprobe 一次：前端「+ 路径」加进来的素材要靠它补尺寸/帧数
                item["probe"] = app.probe_media(wp)
            out.append(item)
        return self.json({"ok": True, "paths": out})

    def api_upload(self):
        app = APP
        assert app is not None
        ctype = self.headers.get("Content-Type") or ""
        max_mb = (app.cfg.get("limits") or {}).get("max_upload_mb", 512)
        if ctype.startswith("multipart/form-data"):
            m = re.search(r"boundary=([^;]+)", ctype)
            if not m:
                return self.err(400, "multipart 缺少 boundary")
            form = parse_multipart(self.body(), m.group(1).strip('"').encode())
            f = form.get("file")
            if not f or not f.get("data"):
                return self.err(400, "没有收到文件字段 file")
            name = os.path.basename(f.get("filename") or "upload.bin")
            data = f["data"]
        else:
            name = os.path.basename(self.headers.get("X-Filename") or "upload.bin")
            data = self.body()
        if len(data) > max_mb * 1024 * 1024:
            return self.err(413, f"文件超过 {max_mb} MiB 上限")
        if not data:
            return self.err(400, "文件为空")
        updir = os.path.join(app.cfg["paths"]["uploads_dir"],
                             time.strftime("%Y%m%d"))
        os.makedirs(updir, exist_ok=True)
        safe = re.sub(r"[^\w.\-]+", "_", name)[:120] or "upload.bin"
        path = os.path.join(updir, time.strftime("%H%M%S") + "-" + safe)
        with open(path, "wb") as fp:
            fp.write(data)
        info = app.probe_media(path)
        app.log.info(f"上传参考素材 {safe}（{len(data)/1024**2:.1f} MiB）-> {path}")
        return self.json({"ok": True, "path": path, "windows": wsl_to_windows(path),
                          "name": safe, "bytes": len(data), "probe": info})

    def api_optimize(self):
        app = APP
        assert app is not None
        raw = self.json_body()
        prompt = (raw.get("chinese") or "").strip()
        if not prompt:
            self.sse_start()
            self.sse("error", {"type": "error", "message": "请先写点中文创作意图"})
            return
        gen = self
        self.sse_start()
        gen.sse("open", {"ok": True, "ts": iso()})
        try:
            for ev in app.optimizer.stream(raw):
                name = ev.get("type") or "message"
                if not gen.sse(name, ev):
                    app.log.info("优化请求：客户端断开")
                    return
        except Exception as e:
            app.log.error(f"优化失败：{e}")
            gen.sse("error", {"type": "error", "message": f"优化过程异常：{e}"})

    # ---- 网盘端点
    DISK_CATEGORY = {0: "文件夹", 1: "视频", 2: "音频", 3: "图片", 4: "文档",
                     5: "种子", 6: "其他", 7: "压缩包", 8: "应用"}

    @staticmethod
    def _disk_file_item(it: dict) -> dict:
        cat = it.get("category")
        try:
            cat = int(cat)
        except (TypeError, ValueError):
            cat = None
        is_dir = str(it.get("file_type") or "") == "0" or cat == 0
        return {
            "fid": it.get("fid"), "name": it.get("filename") or it.get("file_name") or "",
            "size": it.get("size"), "category": cat,
            "category_zh": it.get("obj_category") or Handler.DISK_CATEGORY.get(cat, "文件"),
            "is_dir": is_dir, "include_items": it.get("includeItems"),
            "updated_at": it.get("updated_at"),
            "duration": it.get("duration"), "video_width": it.get("video_width"),
            "video_height": it.get("video_height"),
            "thumbnail": it.get("big_thumbnail"),
        }

    def api_disk_status(self, q):
        app = APP
        assert app is not None
        fresh = _q1(q, "probe", "1") not in ("0", "false", "no", "off")
        data = app.disk_summary(fresh=fresh)
        return self.json({"ok": True, "disk": data, "tasks": app.disk.list(_qint(q, "tasks", 10))})

    def api_disk_files(self, q):
        """浏览网盘目录。默认只取一页；all=1 时走 CLI 的 --all（生成完整 Artifact）。"""
        app = APP
        assert app is not None
        parent = _q1(q, "parent_fid", "0") or "0"
        size = max(1, min(100, _qint(q, "page_size", 100)))
        fetch_all = _q1(q, "all", "0") in ("1", "true", "yes")
        args = ["browse", "--parent-fid", parent, "--page-size", str(size)]
        if fetch_all:
            args.append("--all")
        r = app.disk.cli.call(args, timeout=180)
        files, artifact = [], None
        for ev in r["events"]:
            if ev.get("type") == "list":
                d = ev.get("data") or {}
                if d.get("fid"):
                    files.append(self._disk_file_item(d))
            elif ev.get("type") == "artifact":
                artifact = (ev.get("data") or {}).get("file_path")
        res = (r.get("result") or {}).get("data") or {}
        if fetch_all and artifact:
            files = [self._disk_file_item(x) for x in _read_artifact(artifact)] or files
        return self.json({"ok": True, "parent_fid": parent, "files": files,
                          "total": res.get("total", len(files)),
                          "has_more": bool(res.get("hasMore")),
                          "artifact": artifact, "all": fetch_all})

    def api_disk_search(self, q):
        """搜索网盘文件。完整结果在 CLI 的 Artifact（JSONL）里，这里读回前 N 条。"""
        app = APP
        assert app is not None
        kw = (_q1(q, "keyword", "") or "").strip()
        if not kw:
            return self.err(400, "缺少 keyword")
        args = ["search", "--keyword", kw[:50], "--size", str(max(1, min(100, _qint(q, "size", 100)))),
                "--stdout-only"]
        stype = _q1(q, "search_type", "")
        if stype:
            args += ["--search-type", stype]
        parent = _q1(q, "parent_fid", "")
        if parent:
            args += ["--parent-fid", parent]
        r = app.disk.cli.call(args, timeout=180)
        files, artifact, preview = [], None, []
        for ev in r["events"]:
            if ev.get("type") == "list":
                d = ev.get("data") or {}
                if d.get("fid"):
                    files.append(self._disk_file_item(d))
            elif ev.get("type") == "artifact":
                artifact = (ev.get("data") or {}).get("file_path")
        res = (r.get("result") or {}).get("data") or {}
        for x in (res.get("file_list") or []):
            preview.append(self._disk_file_item(x))
        if artifact:
            # Artifact 是 CLI 落盘的绝对路径；Windows runner 下它是 D:\... 形式，这里翻回 WSL 视角
            full = [self._disk_file_item(x) for x in _read_artifact(artifact)]
            if full:
                files = full
        if not files:
            files = preview
        return self.json({"ok": True, "keyword": kw, "files": files[:300],
                          "total": res.get("total", len(files)), "artifact": artifact})

    def api_disk_login(self):
        app = APP
        assert app is not None
        raw = self.json_body()
        token = (raw.get("token") or "").strip()
        t = app.disk.submit_login({"token": token, "timeout": raw.get("timeout") or 600})
        return self.json({"ok": True, "task": t.to_dict()})

    def api_disk_publish(self):
        """压缩 + 加密打包 + 上传 + 建分享链接 —— 一次提交，走任务流看进度。"""
        app = APP
        assert app is not None
        raw = self.json_body()
        path = (raw.get("path") or "").strip()
        job_id = (raw.get("job_id") or "").strip()
        if not path and not job_id:
            return self.err(400, "需要 path 或 job_id")
        params = {
            "path": translate_path(path) if path else "",
            "job_id": job_id,
            "compress": raw.get("compress"),
            "archive": raw.get("archive"),
            "share": raw.get("share"),
            "password": (raw.get("password") or "").strip() or None,
            "title": (raw.get("title") or "").strip() or None,
            "parent_fid": (raw.get("parent_fid") or "").strip(),
            "url_type": raw.get("url_type"),
            "expired_type": raw.get("expired_type"),
            "keep_work": raw.get("keep_work"),
            "dry_run": bool(raw.get("dry_run")),
            "session_input": (raw.get("session_input") or "").strip() or None,
        }
        # 没传的布尔项要落到配置默认值上；留个 None 进去会被 bool(None) 当成「显式关闭」
        params = {k: v for k, v in params.items() if v is not None}
        src = params["path"]
        if src and not os.path.isfile(src):
            return self.err(400, f"文件不存在：{src}")
        t = app.disk.submit_publish(params)
        return self.json({"ok": True, "task": t.to_dict()})

    def api_disk_fetch(self):
        """把网盘里的文件下载回本机（当参考素材用）。"""
        app = APP
        assert app is not None
        raw = self.json_body()
        fids = raw.get("fids") or ([raw["fid"]] if raw.get("fid") else [])
        fids = [str(f).strip() for f in fids if str(f or "").strip()]
        if not fids:
            return self.err(400, "缺少 fid")
        outdir = (raw.get("output_dir") or "").strip()
        params = {"fids": fids, "name": (raw.get("name") or "").strip(),
                  "output_dir": translate_path(outdir) if outdir else None,
                  "session_input": (raw.get("session_input") or "").strip() or None}
        t = app.disk.submit_fetch(params)
        return self.json({"ok": True, "task": t.to_dict()})

    # ---- SSE
    def stream_disk(self, t, from_seq: int):
        app = APP
        assert app is not None
        self.sse_start()
        qq = t.subscribe()
        try:
            self.sse("snapshot", t.to_dict())
            for ev in t.events_after(from_seq):
                if not self.sse(ev.get("type") or "event", ev):
                    return
            last = time.time()
            while True:
                try:
                    ev = qq.get(timeout=5)
                except Exception:
                    if t.status in ("done", "failed", "cancelled", "interrupted"):
                        break
                    if time.time() - last > 12:
                        if not self.sse_ping():
                            return
                        last = time.time()
                    continue
                last = time.time()
                if not self.sse(ev.get("type") or "event", ev):
                    return
                if ev.get("type") == "exit":
                    break
        finally:
            t.unsubscribe(qq)

    # ---- SSE
    def stream_job(self, j, from_seq: int):
        app = APP
        assert app is not None
        self.sse_start()
        q = j.subscribe()
        try:
            hist = j.events_after(from_seq)
            self.sse("snapshot", j.to_dict())
            for ev in hist:
                if not self.sse(ev.get("type") or "event", ev):
                    return
            last = time.time()
            while True:
                try:
                    ev = q.get(timeout=5)
                except Exception:
                    if j.status in ("done", "failed", "cancelled", "interrupted"):
                        break
                    if time.time() - last > 12:
                        if not self.sse_ping():
                            return
                        last = time.time()
                    continue
                last = time.time()
                if not self.sse(ev.get("type") or "event", ev):
                    return
                if ev.get("type") == "exit":
                    break
        finally:
            j.unsubscribe(q)

    def stream_logs(self, q=None):
        app = APP
        assert app is not None
        q = q or {}
        if not self._log_exposed():
            self.sse_start()
            self.sse("error", {"type": "error", "message": "运行日志未对外暴露"})
            return
        level, source = _q1(q, "level"), _q1(q, "source")
        search, since = _q1(q, "q"), _qint(q, "since_seq", 0)
        self.sse_start()
        self.sse("snapshot", {"stats": app.log.stats(), "llm": app.llm_runs.stats(),
                              "level": app.log.level})
        qq = app.log.subscribe()
        try:
            # 先补最近的历史（同样走筛选），再转实时
            for rec in app.log.tail(200, level=level, source=source, q=search, since_seq=since):
                if not self.sse("log", rec):
                    return
            while True:
                try:
                    rec = qq.get(timeout=15)
                except Exception:
                    if not self.sse_ping():
                        return
                    continue
                if not app.log.matches(rec, level=level, source=source, q=search,
                                       since_seq=since):
                    continue
                if not self.sse("log", rec):
                    return
        finally:
            app.log.unsubscribe(qq)


# --------------------------------------------------------------------------- 网盘工具
def _read_artifact(path: str, limit: int = 2000) -> list[dict]:
    r"""读 CLI 落盘的完整结果（JSONL，一行一个文件对象）。读不到就当空结果。

    Windows runner 下 CLI 写的是 D:... 路径，先翻成 /mnt/d/... 再读。
    """
    out: list[dict] = []
    path = translate_path(path) or path
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and (obj.get("fid") or obj.get("filename")):
                    out.append(obj)
                if len(out) >= limit:
                    break
    except OSError:
        return []
    return out


# --------------------------------------------------------------------------- 路径翻译
_WIN_DRIVE = re.compile(r"^([a-zA-Z]):[\\/](.*)$")


def translate_path(p: str) -> str | None:
    """Windows 路径 / 反斜杠路径 -> WSL 路径。已经是 POSIX 路径就原样返回。"""
    if not p:
        return None
    s = p.strip().strip('"')
    m = _WIN_DRIVE.match(s)
    if m:
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    if s.startswith("\\\\wsl"):
        # \\wsl.localhost\Ubuntu\home\x -> /home/x
        tail = s.split("\\", 4)
        return "/" + ("/".join(tail[4:]).replace("\\", "/") if len(tail) > 4 else "")
    if s.startswith("/"):
        return s
    return os.path.join(repo_root(), s)


# --------------------------------------------------------------------------- 服务器
class Server(ThreadingHTTPServer):
    """对「客户端中途断开」免疫的 HTTP 服务器。

    http.server 只捕获 socket.timeout：浏览器关标签页 / 刷新 / 丢弃 keep-alive
    连接 / 局域网掉线时对端会发 RST，服务端在 handle_one_request 里读请求行就会抛
    ConnectionResetError。异常冒到 socketserver.BaseServer.handle_error 后，那里
    **不会**走 Handler.log_error，而是直接把整段 traceback（带 ---- 分割线）打到
    stderr，把真正有用的日志淹没。

    这些都只是网络噪声，不代表服务端出故障：统一降级成一条 debug 日志。
    """

    daemon_threads = True

    # ConnectionResetError / ConnectionAbortedError / BrokenPipeError 都是它的子类
    _CLIENT_ABORT = ConnectionError

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, self._CLIENT_ABORT):
            log = getattr(APP, "log", None) if APP else None
            if log:
                log.debug(f"连接被对端中断：{exc!r}", source="http", event="http.abort",
                          ip=str(client_address[0]) if isinstance(client_address, tuple)
                          else str(client_address))
            return
        super().handle_error(request, client_address)


# --------------------------------------------------------------------------- 启动
def create(cfg: dict | None = None) -> tuple[App, Server]:
    global APP
    cfg = cfg or cfgmod.server_config()
    APP = App(cfg)
    APP.log.info("服务启动", source="sys", event="server.start",
                 root=cfg["root"], python=os.sys.version.split()[0],
                 log_dir=APP.log.dir, log_level=APP.log.level,
                 llm_capture=APP.llm_runs.capture)
    APP.telemetry.start()
    host = (cfg.get("server") or {}).get("host", "0.0.0.0")
    port = int((cfg.get("server") or {}).get("port", 8765))
    httpd = Server((host, port), Handler)
    return APP, httpd


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="MiniMax-H3 前端 API 服务")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--check", action="store_true", help="只做自检并退出（用于启动脚本）")
    a = ap.parse_args(argv)

    cfg = cfgmod.server_config()
    if a.host:
        cfg["server"]["host"] = a.host
    if a.port:
        cfg["server"]["port"] = a.port

    if a.check:
        app = App(cfg)
        spec = app.spec()
        ds = app.deepseek_cfg()
        print(json.dumps({
            "root": cfg["root"], "params": len(spec.get("params") or []),
            "presets": list((spec.get("presets") or {}).keys()),
            "deepseek_key": ds["api"]["key_present"],
            "host": cfg["server"]["host"], "port": cfg["server"]["port"],
        }, ensure_ascii=False))
        return 0

    app, httpd = create(cfg)
    url = f"http://127.0.0.1:{cfg['server']['port']}/"
    print(f"[webui] 服务已启动：{url}")
    print(f"[webui] 仓库根：{cfg['root']}（Windows: {wsl_to_windows(cfg['root'])}）")
    print(f"[webui] 参数规格：{len(app.spec().get('params') or [])} 项；"
          f"DeepSeek Key：{'已配置' if app.deepseek_cfg()['api']['key_present'] else '未配置'}")
    print(f"[webui] 运行日志：{app.log.dir}（{app.log.level}，页面「日志」页签可实时查看）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[webui] 收到中断，正在退出…")
    finally:
        app.telemetry.stop()
        httpd.server_close()
        app.log.info("服务停止", source="sys", event="server.stop")
        app.log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
