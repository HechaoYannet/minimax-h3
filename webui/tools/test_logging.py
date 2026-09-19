#!/usr/bin/env python3
"""test_logging.py -- 运行日志系统的离线自测（纯标准库，不需要浏览器/权重）。

覆盖三块，都是「改了日志系统之后必须仍然成立」的契约：

  1. RunLog      等级过滤 / 来源过滤 / 关键词搜索 / 分页 / JSONL 落盘 / 轮转 / 清空
  2. LLMRunStore 完整记录、summary 模式不存正文、列举、按 id 取回、只保留 N 份
  3. 端到端      真起一次 HTTP 服务打 /api/logs 系列；再让 Optimizer 打本地
                 mock DeepSeek，验证 LLM 记录里确实有请求、输出、tokens、错误、中断
  4. 格式失败    模型不按 <<<H3_*>>> 协议输出时，原始输出（response.raw）、标记之外
                 的文字、见过的标记、未闭合的段都要能在记录里查到（否则无法排障）

用法：
    cd <repo> && python3 webui/tools/test_logging.py -v
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI = os.path.dirname(HERE)
ROOT = os.path.dirname(WEBUI)
for p in (WEBUI, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend import config as cfgmod          # noqa: E402
from backend import media                     # noqa: E402
from backend import promptopt                 # noqa: E402
from backend import server                    # noqa: E402
from backend.runlog import LLMRunStore, RunLog  # noqa: E402

try:
    import mock_deepseek                      # noqa: E402
except Exception:                             # pragma: no cover
    mock_deepseek = None


def _http(method: str, url: str, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or ({"Content-Type": "application/json"}
                                                     if data else {}))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _json_http(method, url, body=None):
    status, raw, headers = _http(method, url, body)
    try:
        return status, json.loads(raw.decode("utf-8")), headers
    except Exception:
        return status, {"_raw": raw.decode("utf-8", "replace")}, headers


def _sse_events(raw: str):
    """把 /api/optimize 的 SSE 正文解析成 [(event, data), ...]。"""
    out = []
    for block in raw.split("\n\n"):
        ev, data = "message", ""
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if data:
            try:
                out.append((ev, json.loads(data)))
            except json.JSONDecodeError:
                continue
    return out


# --------------------------------------------------------------------------- RunLog
class RunLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = RunLog(os.path.join(self.tmp.name, "logs"), level="debug",
                          console=False, capacity=50)
        self.addCleanup(self.log.close)

    def test_levels_sources_search_paging(self):
        self.log.info("普通信息", source="server")
        self.log.warn("作业警告", source="job", job="j1")
        self.log.error("LLM boom", source="llm", run="r1")
        self.log.debug("请求完成", source="http", path="/api/config")

        self.assertEqual([r["msg"] for r in self.log.tail(10, level="warn")],
                         ["作业警告", "LLM boom"])
        self.assertEqual(len(self.log.tail(10, source="llm")), 1)
        self.assertEqual(len(self.log.tail(10, q="boom")), 1)
        self.assertEqual(len(self.log.tail(10, source="job", q="作业")), 1)
        # 结构化字段要能查到
        rec = self.log.tail(10, source="llm")[0]
        self.assertEqual(rec["run"], "r1")
        self.assertTrue(rec["iso"].count(":") == 2 and "." in rec["iso"])

        page = self.log.query(n=2, offset=0)
        self.assertEqual(page["matched"], 4)
        self.assertEqual(len(page["records"]), 2)
        self.assertEqual(page["records"][-1]["msg"], "请求完成")   # 页内时间正序
        page2 = self.log.query(n=2, offset=2)
        self.assertEqual({r["msg"] for r in page2["records"]} & {"普通信息", "作业警告"},
                         {"普通信息", "作业警告"})

        stats = self.log.stats()
        self.assertEqual(stats["counts"]["error"], 1)
        self.assertIn("llm", stats["sources"])
        self.assertIsNone(stats["file_error"])

    def test_file_persist_and_rotate(self):
        self.log.max_file_bytes = 400          # 人为调小，几行就触发轮转
        for i in range(60):
            self.log.info(f"第 {i} 条记录，填充一些内容让文件长起来", source="sys", i=i)
        names = sorted(n for n in os.listdir(self.log.dir) if n.endswith(".jsonl"))
        self.assertIn("webui.jsonl", names)
        self.assertTrue(any(n.startswith("webui.1") for n in names),
                        f"应当发生轮转，实际文件：{names}")
        self.assertTrue(len(names) <= 1 + self.log.backups + 1)
        # 每个文件都必须是合法 JSONL
        for n in names:
            with open(os.path.join(self.log.dir, n), encoding="utf-8") as f:
                for line in f:
                    json.loads(line)
        self.assertIsNone(self.log.stats()["file_error"])

    def test_reload_tail_across_restart(self):
        self.log.info("第一次进程的日志 A", source="sys")
        self.log.info("第一次进程的日志 B", source="job", job="j9")
        max_seq = self.log.stats()["seq"]
        self.log.close()
        # 模拟服务重启：同一目录再开一个 RunLog
        again = RunLog(self.log.dir, level="debug", console=False, capacity=50)
        self.addCleanup(again.close)
        msgs = [r["msg"] for r in again.tail(10)]
        self.assertIn("第一次进程的日志 A", msgs)
        self.assertIn("第一次进程的日志 B", msgs)
        again.info("第二次进程的日志", source="sys")
        recs = again.tail(10)
        self.assertEqual(recs[-1]["msg"], "第二次进程的日志")
        # 新记录的 seq 必须大于历史最大值，否则 SSE 的 since_seq 会乱
        self.assertGreater(recs[-1]["seq"], max_seq)

    def test_clear(self):
        self.log.info("a", source="sys")
        self.log.info("b", source="sys")
        res = self.log.clear(files=True)
        self.assertEqual(res["cleared"], 2)
        # 清空后只剩 clear 自身那条
        self.assertEqual([r["msg"] for r in self.log.tail(10)], ["日志缓冲已清空（2 条）"])
        self.assertEqual(os.listdir(self.log.dir), ["webui.jsonl"])


# --------------------------------------------------------------------------- LLMRunStore
class LLMRunStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = RunLog(os.path.join(self.tmp.name, "logs"), console=False)
        self.addCleanup(self.log.close)

    def test_full_capture_roundtrip(self):
        store = LLMRunStore(os.path.join(self.tmp.name, "llm"), self.log,
                            capture="full", max_runs=10)
        run = store.begin({"model": "m", "mode": "oneref", "thinking": True})
        run.set_request({"model": "m", "stream": True, "temperature": 0.7},
                        "SYS-PROMPT", "USER-MSG", ["config/prompts/system.md"])
        run.note_thinking("想一想…")
        run.note_delta("prompt", "HELLO ")
        run.note_delta("prompt", "WORLD")
        run.note_delta("translation", "你好")
        run.set_usage({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14})
        summary = run.finish("ok", missing=[])

        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["usage"]["total_tokens"], 14)
        runs = store.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["id"], run.id)
        self.assertEqual(runs[0]["output_chars"]["prompt"], len("HELLO WORLD"))

        full = store.get(run.id)
        self.assertEqual(full["request"]["system"], "SYS-PROMPT")
        self.assertEqual(full["request"]["user"], "USER-MSG")
        self.assertEqual(full["response"]["sections"]["prompt"], "HELLO WORLD")
        self.assertEqual(full["response"]["sections"]["translation"], "你好")
        self.assertEqual(full["response"]["thinking"], "想一想…")
        # 路径穿越要挡住
        self.assertIsNone(store.get("../secret"))

    def test_raw_output_kept_when_model_ignores_the_format(self):
        """模型没按标记协议输出时，记录里必须有它的原文，否则无从排查。"""
        store = LLMRunStore(os.path.join(self.tmp.name, "llm-raw"), self.log,
                            capture="full", max_runs=10)
        run = store.begin({"model": "m"})
        run.set_request({"model": "m", "stream": True}, "SYS", "USER")
        prose = "当然可以！下面是我写的提示词：\nA woman stands in the rain."
        run.note_raw(prose)
        run.note_stray("当然可以！下面是我写的提示词：\n")
        run.note_bad_chunk("<html>502 Bad Gateway</html>", "Expecting value")
        summary = run.finish("ok", missing=["prompt", "translation", "notes"])

        self.assertFalse(summary["format_ok"])
        self.assertEqual(summary["markers"], [])          # 一个标记都没见到
        self.assertEqual(summary["raw_chars"], len(prose))
        self.assertEqual(summary["stray_chars"], len("当然可以！下面是我写的提示词：\n"))

        full = store.get(run.id)
        self.assertEqual(full["response"]["raw"], prose)   # 原文可回放
        self.assertIn("A woman stands in the rain.", full["response"]["raw"])
        self.assertIn("当然可以", full["response"]["stray"])
        self.assertEqual(full["response"]["missing"],
                         ["prompt", "translation", "notes"])
        # 事件里也要能看见「流里有解析不了的行」（代理改写成 HTML 报错页的情形）
        self.assertEqual(full["response"]["bad_chunks"], 1)
        self.assertTrue(any(ev["kind"] == "bad_chunk" for ev in full["events"]))

    def test_raw_chars_survive_summary_capture(self):
        """summary 模式不存原文，但长度与标记要留着 —— 至少能判断格式对不对。"""
        store = LLMRunStore(os.path.join(self.tmp.name, "llm-raw-sum"), self.log,
                            capture="summary", max_runs=10)
        run = store.begin({"model": "m"})
        run.note_raw("没有任何标记的一段话")
        run.note_marker("prompt")
        summary = run.finish("ok", missing=["translation", "notes"])
        self.assertEqual(summary["raw_chars"], len("没有任何标记的一段话"))
        self.assertEqual(summary["markers"], ["prompt"])
        data = store.get(run.id)
        self.assertEqual(data["response"]["raw"], "")
        self.assertEqual(data["response"]["raw_chars"], len("没有任何标记的一段话"))

    def test_summary_capture_keeps_sizes_not_text(self):
        store = LLMRunStore(os.path.join(self.tmp.name, "llm2"), self.log,
                            capture="summary", max_runs=10)
        run = store.begin({"model": "m"})
        run.set_request({"model": "m"}, "SECRET-SYSTEM", "SECRET-USER")
        run.note_delta("prompt", "0123456789")
        run.finish("ok")
        data = store.get(run.id)
        self.assertNotIn("system", data["request"])
        self.assertEqual(data["request"]["system_chars"], len("SECRET-SYSTEM"))
        self.assertEqual(data["response"]["sections"]["prompt"], "")
        self.assertEqual(data["response"]["section_chars"]["prompt"], 10)
        self.assertEqual(data["output_chars"]["prompt"], 10)

    def test_prune_keeps_recent(self):
        store = LLMRunStore(os.path.join(self.tmp.name, "llm3"), self.log,
                            capture="full", max_runs=2)
        ids = []
        for i in range(4):
            run = store.begin({"model": f"m{i}"})
            run.note_delta("prompt", f"out{i}")
            run.finish("ok")
            ids.append(run.id)
        files = [n for n in os.listdir(store.dir) if n.endswith(".json")]
        self.assertEqual(len(files), 2)
        self.assertIsNone(store.get(ids[0]))
        self.assertIsNotNone(store.get(ids[-1]))


# --------------------------------------------------------------------------- 多模态附图
def _png_bytes(w: int, h: int) -> bytes:
    """造一张合法的最小 PNG（纯标准库，不依赖 Pillow）。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)          # 8bit truecolor
    row = b"\x00" + b"\xff\x00\x00" * w                          # filter + RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(row * h)) + chunk(b"IEND", b""))


def _jpeg_header_bytes(w: int, h: int) -> bytes:
    """只有 SOI/APP0/SOF0/EOI 的 JPEG 头，够 media.dimensions 解析。"""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", h, w)
            + b"\x03" + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01")
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def _bmp_bytes(w: int, h: int) -> bytes:
    """24bit 未压缩 BMP（服务端不支持，用来验证「必须转换且不放大」）。"""
    row = b"\x00\x00\xff" * w
    row += b"\x00" * ((-len(row)) % 4)
    data = row * h
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(data), 2835, 2835, 0, 0)
    fh = b"BM" + struct.pack("<IHHI", 14 + 40 + len(data), 0, 0, 14 + 40)
    return fh + dib + data


class MediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_sniff_by_content_not_extension(self):
        png = _png_bytes(32, 16)
        self.assertEqual(media.sniff(png), "png")
        self.assertEqual(media.dimensions(png, "png"), (32, 16))
        p = self._write("actually_a_png.jpg", png)       # 扩展名骗人，按内容判
        with open(p, "rb") as f:
            self.assertEqual(media.sniff(f.read()), "png")

    def test_jpeg_gif_webp_dims(self):
        jp = _jpeg_header_bytes(1200, 800)
        self.assertEqual(media.sniff(jp), "jpeg")
        self.assertEqual(media.dimensions(jp, "jpeg"), (1200, 800))

        gif = b"GIF89a" + struct.pack("<HH", 320, 240) + b"\x00\x00\x00"
        self.assertEqual(media.sniff(gif), "gif")
        self.assertEqual(media.dimensions(gif, "gif"), (320, 240))

        webp = (b"RIFF" + struct.pack("<I", 18) + b"WEBP" + b"VP8X"
                + struct.pack("<I", 10) + b"\x00" + b"\x00\x00\x00"
                + (640 - 1).to_bytes(3, "little") + (480 - 1).to_bytes(3, "little"))
        self.assertEqual(media.sniff(webp), "webp")
        self.assertEqual(media.dimensions(webp, "webp"), (640, 480))

        self.assertIsNone(media.sniff(b"not an image at all"))
        self.assertIsNone(media.dimensions(b"", "png"))

    def test_prepare_small_image_passthrough(self):
        p = self._write("ref.png", _png_bytes(64, 48))
        m = media.prepare_image(p, max_edge=1280, detail="high")
        self.assertTrue(m["ok"], m.get("error"))
        self.assertEqual((m["width"], m["height"]), (64, 48))
        self.assertEqual((m["sent_width"], m["sent_height"]), (64, 48))
        self.assertFalse(m["resized"])
        self.assertEqual(m["mime"], "image/png")
        self.assertTrue(m["data_uri"].startswith("data:image/png;base64,"))
        self.assertNotIn("data_uri", media.public_attachment(m))

    def test_prepare_missing_or_bad_file(self):
        m = media.prepare_image(os.path.join(self.tmp.name, "nope.png"))
        self.assertFalse(m["ok"])
        self.assertIn("不存在", m["error"])
        bad = self._write("bad.bin", b"\x00\x01\x02\x03" * 200)
        m = media.prepare_image(bad)
        self.assertFalse(m["ok"])
        self.assertTrue(m.get("error"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
    def test_ffmpeg_downscales_but_never_upscales(self):
        big = self._write("big.png", _png_bytes(400, 300))
        m = media.prepare_image(big, max_edge=100, detail="high")
        self.assertTrue(m["ok"], m.get("error"))
        self.assertTrue(m["resized"])
        self.assertLessEqual(max(m["sent_width"], m["sent_height"]), 100)

        small = self._write("small.bmp", _bmp_bytes(40, 30))   # 必须转格式
        m2 = media.prepare_image(small, max_edge=1280, detail="high")
        self.assertTrue(m2["ok"], m2.get("error"))
        self.assertEqual(m2["mime"], "image/jpeg")
        self.assertEqual((m2["sent_width"], m2["sent_height"]), (40, 30))  # 没被放大

    def test_collect_respects_max_images_and_budget(self):
        paths = [self._write(f"r{i}.png", _png_bytes(32, 32)) for i in range(4)]
        refs = [{"kind": "image", "path": p, "name": os.path.basename(p),
                 "label": f"<Picture {i + 1}>"} for i, p in enumerate(paths)]
        mm = {"enabled": True, "images": True, "video_frames": 0, "max_images": 2,
              "detail": "high", "max_edge": 1280, "max_mb_per_image": 20, "max_mb_total": 40}
        atts, notes = media.collect_attachments(refs, mm)
        self.assertEqual(len(atts), 2)
        self.assertEqual([a["label"] for a in atts], ["<Picture 1>", "<Picture 2>"])
        self.assertTrue(any("max_images" in n for n in notes))

        atts, notes = media.collect_attachments(refs, dict(mm, enabled=False))
        self.assertEqual(atts, [])
        self.assertEqual(notes, [])

        atts, notes = media.collect_attachments([{"kind": "image", "name": "x.png"}], mm)
        self.assertEqual(atts, [])
        self.assertTrue(notes)

    def test_video_frame_missing_file_is_graceful(self):
        m = media.prepare_video_frame(os.path.join(self.tmp.name, "nope.mp4"))
        self.assertFalse(m["ok"])
        self.assertTrue(m.get("error"))


# --------------------------------------------------------------------------- HTTP 端点
class ServerEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = cfgmod.server_config()
        tmp = self.tmp.name
        cfg["paths"].update({"cache_dir": os.path.join(tmp, "cache"),
                             "jobs_dir": os.path.join(tmp, "jobs"),
                             "outputs_dir": os.path.join(tmp, "out"),
                             "uploads_dir": os.path.join(tmp, "up")})
        cfg["logging"].update({"dir": os.path.join(tmp, "logs"), "console": False,
                               "level": "debug"})
        cfg["server"]["host"] = "127.0.0.1"
        cfg["server"]["port"] = 0
        app, httpd = server.create(cfg)
        self.app, self.httpd = app, httpd
        self.port = httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()

        def _stop():
            httpd.shutdown()
            httpd.server_close()
            app.telemetry.stop()
            app.log.close()
            if server.APP is app:
                server.APP = None
        self.addCleanup(_stop)

    def test_endpoints(self):
        status, data, _ = _json_http("GET", self.base + "/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(data["logging"]["expose"])
        self.assertEqual(data["logging"]["llm_capture"], "full")
        # 多模态配置要暴露给前端（至少带 enabled 字段），但不含任何密钥
        self.assertIn("multimodal", data["deepseek"])
        self.assertIn("enabled", data["deepseek"]["multimodal"])

        status, data, _ = _json_http("GET", self.base + "/api/logs?n=50")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("stats", data)
        self.assertEqual(data["lines"], data["records"])

        status, data, _ = _json_http("POST", self.base + "/api/logs/client",
                                     {"level": "warn", "source": "ui",
                                      "event": "ui.test", "msg": "前端单元测试上报"})
        self.assertEqual(status, 200)
        self.assertEqual(data["accepted"], 1)
        status, data, _ = _json_http(
            "GET", self.base + "/api/logs?source=ui&q=" + urllib.parse.quote("单元测试"))
        self.assertEqual(len(data["records"]), 1)
        self.assertEqual(data["records"][0]["source"], "ui")

        status, data, _ = _json_http("POST", self.base + "/api/logs/level", {"level": "debug"})
        self.assertEqual(status, 200)
        self.assertEqual(data["level"], "debug")

        status, raw, headers = _http("GET", self.base + "/api/logs/download")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn(b"# MiniMax-H3 webui", raw)

        status, data, _ = _json_http("GET", self.base + "/api/llm/runs?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(data["runs"], [])

        status, data, _ = _json_http("GET", self.base + "/api/health")
        self.assertEqual(status, 200)
        self.assertIn("logging", data)
        self.assertTrue(any(c["name"].startswith("运行日志") for c in data["checks"]))

        # SSE 是长连接：读到前两行就够判断握手是否正常，然后主动断开
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/logs/stream?level=error")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn(b"snapshot", resp.readline())
        self.assertIn(b"data:", resp.readline())
        conn.close()

    def test_client_abort_is_not_a_traceback(self):
        """对端甩连接（RST）只记一条 http.abort，不往 stderr 打整段 traceback。

        复现浏览器关标签页 / 刷新 / 丢弃 keep-alive 连接的场景：SO_LINGER=0 让 close()
        直接发 RST，服务端在 handle_one_request 里读请求行/请求头时抛
        ConnectionResetError。老实现会由 socketserver.handle_error 原样打印 traceback
        （它不经过 Handler.log_error），把真正的日志淹没。
        """
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(b"GET /api/health HTTP/1.1\r\n")   # 半截请求：让服务端卡在 readline
            time.sleep(0.2)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()
            deadline = time.time() + 5
            aborts: list[dict] = []
            while time.time() < deadline and not aborts:
                recs = self.app.log.query(n=200)["records"]
                aborts = [r for r in recs if r.get("event") == "http.abort"]
                time.sleep(0.05)
        self.assertTrue(aborts, "对端断开没有记下 http.abort")
        self.assertEqual(aborts[-1].get("ip"), "127.0.0.1")
        self.assertEqual(buf.getvalue(), "", "连接重置不应该再打印 traceback")

    def test_real_server_error_still_prints_traceback(self):
        """只有「对端断开」这一类被吞掉；真正的服务端异常仍要留下 traceback。"""
        buf = io.StringIO()
        try:
            raise RuntimeError("boom-for-test")
        except RuntimeError:
            with contextlib.redirect_stderr(buf):
                self.httpd.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("RuntimeError", buf.getvalue())
        self.assertIn("boom-for-test", buf.getvalue())


# --------------------------------------------------------------------------- 优化路由端到端
@unittest.skipIf(mock_deepseek is None, "mock_deepseek 不可用")
class OptimizeRouteTest(unittest.TestCase):
    """走一遍真正的 HTTP 路由 POST /api/optimize（SSE），再用 /api/llm/runs/<id> 取证。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = self.tmp.name
        cfg = cfgmod.server_config()
        cfg["paths"].update({"cache_dir": os.path.join(tmp, "cache"),
                             "jobs_dir": os.path.join(tmp, "jobs"),
                             "outputs_dir": os.path.join(tmp, "out"),
                             "uploads_dir": os.path.join(tmp, "up")})
        cfg["logging"].update({"dir": os.path.join(tmp, "logs"), "console": False,
                               "level": "debug"})
        cfg["server"]["host"] = "127.0.0.1"
        cfg["server"]["port"] = 0
        app, httpd = server.create(cfg)
        self.app, self.httpd = app, httpd
        self.port = httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def _stop():
            httpd.shutdown()
            httpd.server_close()
            app.telemetry.stop()
            app.log.close()
            if server.APP is app:
                server.APP = None
        self.addCleanup(_stop)

        mock_deepseek.H.last_request = None
        mock_deepseek.H.requests = []
        self.mock = mock_deepseek.ThreadingHTTPServer(("127.0.0.1", 0), mock_deepseek.H)
        threading.Thread(target=self.mock.serve_forever, daemon=True).start()
        self.addCleanup(self.mock.shutdown)
        self.addCleanup(self.mock.server_close)

        self._orig = cfgmod.deepseek_config
        self.addCleanup(lambda: setattr(cfgmod, "deepseek_config", self._orig))
        mport = self.mock.server_address[1]
        cfgmod.deepseek_config = lambda: {
            "api": {"url": f"http://127.0.0.1:{mport}/chat/completions", "key": "sk-test",
                    "key_present": True, "key_env": "DEEPSEEK_API_KEY",
                    "timeout_s": 8, "max_retries": 0},
            "model": {"name": "mock-model", "stream": True,
                      "thinking": {"enabled": False}, "temperature": 0.7, "max_tokens": 100},
            "prompt": {"system_prompt_inline": "路由测试用 system。", "want_translation": True},
            "multimodal": {"enabled": True, "images": True, "video_frames": 0,
                           "max_images": 8, "detail": "high", "max_edge": 1280,
                           "max_mb_per_image": 20, "max_mb_total": 40},
        }

    def test_optimize_attaches_image(self):
        path = os.path.join(self.tmp.name, "ref.png")
        with open(path, "wb") as f:
            f.write(_png_bytes(80, 60))
        body = {"chinese": "雨夜的便利店门口", "width": 640, "height": 384,
                "num_frames": 22, "steps": 4, "seed": 42,
                "refs": [{"kind": "image", "path": path, "name": "ref.png",
                          "label": "<Picture 1>"}]}
        req = urllib.request.Request(self.base + "/api/optimize",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")

        events = []
        for block in raw.split("\n\n"):
            ev, data = "message", ""
            for line in block.splitlines():
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data += line[5:].strip()
            if data:
                try:
                    events.append((ev, json.loads(data)))
                except json.JSONDecodeError:
                    continue
        kinds = [e for e, _ in events]
        self.assertIn("meta", kinds)
        self.assertIn("done", kinds)
        meta = [d for e, d in events if e == "meta"][0]
        self.assertEqual(meta["multimodal"]["images"], 1)
        done = [d for e, d in events if e == "done"][0]

        # 附图和真请求都要对得上
        content = mock_deepseek.H.last_request["messages"][1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(
            len([c for c in content if c.get("type") == "image_url"]), 1)

        # 通过 HTTP 取回 LLM 记录：有附件元数据、没有图片本体
        status, rec, _ = _json_http("GET", self.base + "/api/llm/runs/" + done["run_id"])
        self.assertEqual(status, 200)
        run = rec["run"]
        self.assertEqual(run["request"]["image_count"], 1)
        att = run["request"]["attachments"][0]
        self.assertEqual(att["name"], "ref.png")
        self.assertNotIn("data_uri", att)
        self.assertNotIn("base64,", json.dumps(run, ensure_ascii=False))

    def test_bad_format_is_retrievable_over_http(self):
        """模型不按格式输出时，光看页面提示不够：要能顺着 run_id 取回原始输出。"""
        mock_deepseek.H.mode = "prose"
        self.addCleanup(setattr, mock_deepseek.H, "mode", "ok")
        body = {"chinese": "雨夜的便利店门口", "width": 640, "height": 384,
                "num_frames": 22, "steps": 4, "seed": 42, "refs": []}
        req = urllib.request.Request(self.base + "/api/optimize",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            events = _sse_events(r.read().decode("utf-8", "replace"))

        done = [d for e, d in events if e == "done"][0]
        self.assertFalse(done["format_ok"])
        self.assertEqual(done["missing"], ["prompt", "translation", "notes"])
        self.assertEqual(done["markers"], [])
        self.assertGreater(done["raw_chars"], 50)          # 模型确实说了话

        status, rec, _ = _json_http("GET", self.base + "/api/llm/runs/" + done["run_id"])
        self.assertEqual(status, 200)
        run = rec["run"]
        self.assertIn("下面是我根据你的中文意图写的提示词", run["response"]["raw"])
        self.assertIn("下面是我根据你的中文意图写的提示词", run["response"]["stray"])
        self.assertFalse(run["format_ok"])
        self.assertEqual(run["response"]["missing"],
                         ["prompt", "translation", "notes"])
        # 运行日志里按 event=llm.format 就能筛出这次格式失败
        status, data, _ = _json_http("GET", self.base + "/api/logs?source=llm&q=llm.format")
        self.assertEqual(status, 200)
        self.assertTrue(any(r.get("event") == "llm.format" for r in data["records"]),
                        "格式失败应当有一条 llm.format 告警")


# --------------------------------------------------------------------------- LLM 端到端
@unittest.skipIf(mock_deepseek is None, "mock_deepseek 不可用")
class OptimizerLoggingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = RunLog(os.path.join(self.tmp.name, "logs"), level="debug", console=False)
        self.addCleanup(self.log.close)
        self.store = LLMRunStore(os.path.join(self.tmp.name, "llm"), self.log,
                                 capture="full", max_runs=20)
        self.opt = promptopt.Optimizer(self.log, self.store)
        mock_deepseek.H.last_request = None
        mock_deepseek.H.requests = []
        mock_deepseek.H.mode = "ok"
        self.addCleanup(setattr, mock_deepseek.H, "mode", "ok")
        self.mock = mock_deepseek.ThreadingHTTPServer(("127.0.0.1", 0), mock_deepseek.H)
        self.addCleanup(self.mock.server_close)
        threading.Thread(target=self.mock.serve_forever, daemon=True).start()
        self.addCleanup(self.mock.shutdown)
        self._orig = cfgmod.deepseek_config
        # 安全默认：忘了调 _patch() 的测试指向死端口、快速失败，
        # 绝不能因为漏了一行就真的把请求发到线上 API。
        self._patch(url="http://127.0.0.1:1/chat/completions")

    def tearDown(self):
        cfgmod.deepseek_config = self._orig

    def _patch(self, url=None, retries=0, multimodal=None):
        port = self.mock.server_address[1]
        cfgmod.deepseek_config = lambda: {
            "api": {"url": url or f"http://127.0.0.1:{port}/chat/completions",
                    "key": "sk-test", "key_present": True, "key_env": "DEEPSEEK_API_KEY",
                    "timeout_s": 8, "max_retries": retries},
            "model": {"name": "mock-model", "stream": True,
                      "thinking": {"enabled": False}, "temperature": 0.7,
                      "max_tokens": 100},
            "prompt": {"system_prompt_inline": "你是测试用提示词工程师。",
                       "want_translation": True},
            "multimodal": multimodal or {"enabled": False},
        }

    def _req(self):
        return {"chinese": "雨夜的便利店门口", "width": 640, "height": 384,
                "num_frames": 22, "steps": 4, "seed": 42, "refs": []}

    def test_success_writes_full_record(self):
        self._patch()
        events = list(self.opt.stream(self._req()))
        kinds = [e["type"] for e in events]
        self.assertIn("meta", kinds)
        self.assertIn("done", kinds)
        done = [e for e in events if e["type"] == "done"][0]
        rid = done["run_id"]
        self.assertTrue(rid)

        run = self.store.get(rid)
        self.assertIsNotNone(run, "LLM 调用记录应当落盘")
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["usage"]["total_tokens"], 3900)
        self.assertIn("雨夜的便利店门口", run["request"]["user"])
        self.assertIn("你是测试用提示词工程师", run["request"]["system"])
        self.assertIn("[Shot 1]", run["response"]["sections"]["prompt"])
        self.assertIn("integrated_multimodal_description",
                      run["response"]["sections"]["translation"])
        self.assertGreater(run["content_chars"], 100)

        # 运行日志里也要能看到这次调用的开始/结束
        msgs = [r["event"] for r in self.log.tail(50, source="llm")]
        self.assertIn("llm.start", msgs)
        self.assertIn("llm.finish", msgs)

    def test_format_failure_is_diagnosable_from_the_record(self):
        """模型完全不按标记协议输出（自然语言 / 自作主张回 JSON）时，
        记录里必须留下原文、见过的标记、以及一条 llm.format 告警。"""
        self._patch()
        for mode in ("prose", "json"):
            with self.subTest(mode=mode):
                mock_deepseek.H.mode = mode
                events = list(self.opt.stream(self._req()))
                self.addCleanup(setattr, mock_deepseek.H, "mode", "ok")
                done = [e for e in events if e["type"] == "done"][0]
                self.assertFalse(done["format_ok"])
                self.assertEqual(done["missing"], ["prompt", "translation", "notes"])
                self.assertEqual(done["markers"], [])
                self.assertGreater(done["raw_chars"], 50, "模型确实输出了内容")

                run = self.store.get(done["run_id"])
                self.assertEqual(run["status"], "ok")          # 调用本身没失败
                self.assertFalse(run["format_ok"])
                self.assertEqual(run["response"]["markers"], [])
                self.assertTrue(run["response"]["raw"], "原始输出必须落盘")
                self.assertIn("下面是我根据你的中文意图写的提示词"
                              if mode == "prose" else '"prompt"',
                              run["response"]["raw"])
                # 被解析器丢掉的标记外文字也要留着
                self.assertGreater(run["response"]["stray_chars"], 0)
                evs = [r["event"] for r in self.log.tail(80, source="llm")]
                self.assertIn("llm.format", evs)

    def test_unterminated_section_is_recovered(self):
        """漏写结束标记时，正文不能凭空消失：按当前段收下 + 记一条 unterminated。"""
        self._patch()
        mock_deepseek.H.mode = "half"
        self.addCleanup(setattr, mock_deepseek.H, "mode", "ok")
        events = list(self.opt.stream(self._req()))
        done = [e for e in events if e["type"] == "done"][0]
        self.assertIn("[Shot 1]", done["result"])
        self.assertEqual(done["missing"], ["translation", "notes"])
        self.assertEqual(done["markers"], ["prompt"])
        run = self.store.get(done["run_id"])
        self.assertIn("[Shot 1]", run["response"]["sections"]["prompt"])
        self.assertTrue(any(e["kind"] == "unterminated" for e in run["events"]),
                        "缺少结束标记应当留下事件")

    def test_multimodal_attaches_images(self):
        path = os.path.join(self.tmp.name, "ref.png")
        with open(path, "wb") as f:
            f.write(_png_bytes(64, 48))
        mm = {"enabled": True, "images": True, "video_frames": 0, "max_images": 8,
              "detail": "high", "max_edge": 1280, "max_mb_per_image": 20,
              "max_mb_total": 40}
        self._patch(multimodal=mm)
        req = self._req()
        req["refs"] = [{"kind": "image", "path": path, "name": "ref.png",
                        "label": "<Picture 1>"}]
        events = list(self.opt.stream(req))
        meta = [e for e in events if e["type"] == "meta"][0]
        done = [e for e in events if e["type"] == "done"][0]
        self.assertEqual(meta["multimodal"]["images"], 1)
        self.assertEqual(meta["multimodal"]["attachments"][0]["name"], "ref.png")

        # 真正发出去的请求：content 是块数组，含一个合法的 data:image_url
        sent = mock_deepseek.H.last_request
        content = sent["messages"][1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("随本消息附上的图片", content[0]["text"])
        imgs = [c for c in content if c.get("type") == "image_url"]
        self.assertEqual(len(imgs), 1)
        self.assertTrue(imgs[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(imgs[0]["image_url"]["detail"], "high")

        run = self.store.get(done["run_id"])
        self.assertEqual(run["request"]["image_count"], 1)
        att = run["request"]["attachments"][0]
        self.assertEqual(att["name"], "ref.png")
        self.assertNotIn("data_uri", att)
        # full 捕获会把 user 文本落盘，但 base64 图片本体绝不能进记录
        with open(self.store._run_path(done["run_id"]), encoding="utf-8") as f:
            self.assertNotIn("base64,", f.read())

    def test_multimodal_disabled_keeps_plain_string(self):
        self._patch(multimodal={"enabled": False})
        req = self._req()
        req["refs"] = [{"kind": "image", "path": "/nonexistent/x.png", "name": "x.png",
                        "label": "<Picture 1>"}]
        events = list(self.opt.stream(req))
        meta = [e for e in events if e["type"] == "meta"][0]
        self.assertEqual(meta["multimodal"]["images"], 0)
        self.assertIsInstance(mock_deepseek.H.last_request["messages"][1]["content"], str)

    def test_client_disconnect_marks_aborted(self):
        self._patch()
        gen = self.opt.stream(self._req())
        meta = next(gen)              # 先拿到 meta，此时请求已发出
        self.assertEqual(meta["type"], "meta")
        rid = meta["run_id"]
        gen.close()                    # 模拟前端关页面 / 断开 SSE
        run = self.store.get(rid)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "aborted")
        self.assertTrue(any(e["kind"] == "abort" for e in run["events"]))

    def test_connection_error_is_recorded(self):
        self._patch(url="http://127.0.0.1:1/chat/completions", retries=0)
        events = list(self.opt.stream(self._req()))
        errs = [e for e in events if e["type"] == "error"]
        self.assertTrue(errs, "连不上时应当 yield error 事件")
        rid = errs[0].get("run_id")
        run = self.store.get(rid)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "error")
        self.assertIn("无法连接", run["error"])
        self.assertTrue(any(e["kind"] == "error" for e in run["events"]))

    def test_missing_api_key_is_logged(self):
        cfgmod.deepseek_config = lambda: {
            "api": {"url": "", "key": "", "key_present": False, "key_env": "DEEPSEEK_API_KEY"},
            "model": {}, "prompt": {}}
        events = list(self.opt.stream(self._req()))
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("API Key", events[0]["message"])
        self.assertTrue(any(r["event"] == "llm.no_key" for r in self.log.tail(20, source="llm")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
