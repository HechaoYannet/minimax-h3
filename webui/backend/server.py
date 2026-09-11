"""webui.backend.server -- 前端 API 服务（Windows 浏览器 <-> WSL 后端）。

只依赖标准库：http.server + threading。原因见 util.py：这台机器内存紧张，
后端不该为了一组 REST 路由去装 FastAPI/uvicorn 并常驻更多内存。

端点一览（协议细节见 webui/README.md）：
  GET  /api/health                  服务与生成环境自检
  GET  /api/config                  参数规格 + 预设 + 服务配置（不含 API Key）
  GET  /api/telemetry               当前硬件占用 + 最近曲线
  GET  /api/lora                    可用的 LoRA 文件
  GET  /api/logs                    后端自身日志（环形缓冲）
  GET  /api/logs/stream             SSE：后端日志实时流
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
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import config as cfgmod
from .estimate import estimate, normalise_shape, risks
from .jobs import JobManager, build_command, make_request
from .promptopt import Optimizer
from .telemetry import Telemetry
from .util import Log, human_dur, iso, now, read_json, repo_root, tail_lines, wsl_to_windows, write_json

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".mp4": "video/mp4", ".md": "text/markdown; charset=utf-8",
}


class App:
    """进程级共享状态。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = cfg["root"]
        self.log = Log(capacity=800)
        self.telemetry = Telemetry(
            nvidia_smi=(cfg.get("telemetry") or {}).get("nvidia_smi", "nvidia-smi"),
            interval=(cfg.get("defaults") or {}).get("telemetry_interval_s", 1.5),
            repo_root=self.root)
        self.jobs = JobManager(cfg, self.log)
        self.optimizer = Optimizer(self.log)
        self.web_dir = os.path.join(self.root, "webui", "web")
        self.started = now()
        self._probe_cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 工具
    def spec(self) -> dict:
        return cfgmod.params_spec()

    def deepseek_cfg(self) -> dict:
        return cfgmod.deepseek_config()

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
    def log_message(self, fmt, *args):  # 默认会往 stderr 刷，这里降噪
        if APP and self.path.startswith("/api/"):
            APP.log.debug(f"{self.address_string()} {fmt % args}")

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
        try:
            if path.startswith("/api/"):
                self._api(method, path, q)
            elif method == "GET":
                self._static(path)
            else:
                self.err(405, "不支持的方法")
        except ValueError as e:
            tb = traceback.format_exc()
            if APP:
                APP.log.warn(f"{method} {path} 参数错误：{e}", trace=tb[-1500:])
            print(f"[api] {method} {path} bad request:\n{tb}", flush=True)
            self.err(400, str(e), trace=tb.strip().splitlines()[-8:])
        except Exception as e:
            tb = traceback.format_exc()
            if APP:
                APP.log.error(f"{method} {path} 失败：{e}", trace=tb[-1500:])
            print(f"[api] {method} {path} failed:\n{tb}", flush=True)
            self.err(500, f"服务端错误：{e}", trace=tb.strip().splitlines()[-6:])

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
            return self.json({"ok": True, "lines": app.log.tail(int(q.get("n", ["200"])[0]))})
        if seg == ["api", "logs", "stream"] and method == "GET":
            return self.stream_logs()
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
        gpu = app.telemetry.current().get("gpu") or {}
        checks.append({"name": "nvidia-smi", "ok": bool(gpu.get("ok")),
                       "detail": gpu.get("name") or gpu.get("error")})
        return self.json({"ok": True, "checks": checks, "root": root,
                          "uptime_s": round(now() - app.started, 1),
                          "python": os.sys.version.split()[0],
                          "host": os.uname().nodename if hasattr(os, "uname") else "?",
                          "repo_windows": wsl_to_windows(root)})

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
            },
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

    def stream_logs(self):
        app = APP
        assert app is not None
        self.sse_start()
        q = app.log.subscribe()
        try:
            for rec in app.log.tail(100):
                if not self.sse("log", rec):
                    return
            while True:
                try:
                    rec = q.get(timeout=15)
                except Exception:
                    if not self.sse_ping():
                        return
                    continue
                if not self.sse("log", rec):
                    return
        finally:
            app.log.unsubscribe(q)


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


# --------------------------------------------------------------------------- 启动
def create(cfg: dict | None = None) -> tuple[App, ThreadingHTTPServer]:
    global APP
    cfg = cfg or cfgmod.server_config()
    APP = App(cfg)
    APP.log.info("服务启动", root=cfg["root"], python=os.sys.version.split()[0])
    APP.telemetry.start()
    host = (cfg.get("server") or {}).get("host", "0.0.0.0")
    port = int((cfg.get("server") or {}).get("port", 8765))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
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
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[webui] 收到中断，正在退出…")
    finally:
        app.telemetry.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
