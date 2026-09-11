"""mock_deepseek.py -- 本地假 DeepSeek 服务，用来离线验证提示词优化链路。

它按 OpenAI 的 chat/completions 流式格式吐一段固定内容：
先一个 reasoning_content 增量，然后 content 分块吐出三个标记段。
用来验证：SSE 解析、标记状态机、中文回译与结构说明的分段、usage 统计。

用法：
    python3 webui/tools/mock_deepseek.py --port 8799
然后把 config/deepseek.yaml 的 api.url 改成 http://127.0.0.1:8799/chat/completions
（api.key 随便填一个非空值），即可在完全离线的情况下点「优化提示词」。
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROMPT = """[Shot 1] Live-action, cinematic, a medium-wide shot frames a young woman in a blue cardigan standing under the awning of a convenience store at night. Rain falls in visible streaks behind her. The camera pushes in with small amplitude at slow speed as she closes the umbrella, shakes the water from it, and steps toward the sliding door. A quiet, breathy young woman (S1) says: <d>[Chinese] 今天真冷。</d> The automatic door slides open with a soft chime, and warm light spills across the wet pavement."""

ZH = """integrated_multimodal_description: [Shot 1] 实拍、电影感，一个中景框住一位穿蓝色开衫的年轻女性，她站在便利店门口的雨棚下，身后是清晰可见的雨丝。镜头以小幅度、慢速推进，她把伞收起来、抖掉水，朝自动门走去。声音轻而带气音的年轻女性 (S1) 说：<d>[Chinese] 今天真冷。</d> 自动门伴随轻响滑开，暖光洒在湿漉漉的地面上。

overall_soundscape: 雨点持续打在雨棚与地面的声音，伴随远处车流经过的水声。

non_diegetic_music: N/A"""

NOTES = """- 判断为 I2VA：只有一张参考图，按官方规范作为 0.00 秒的首帧，故首行写对齐指令。
- 台词保留中文原文，未翻译，符合规范。
- 时长按 3.04 秒控制，只用一个镜头，避免超出目标时长。"""

SECRET = "sk-mock-key"


def _content_parts(req: dict):
    """遍历 messages，返回所有 content 块（多模态时 user.content 是数组）。"""
    for m in (req.get("messages") or []):
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    yield m, part


def count_images(req: dict) -> tuple[int, int]:
    """返回 (图片数, 图片字节数)。顺便校验 data: URL 的 base64 能解开。"""
    import base64 as _b64

    n = 0
    total = 0
    for _m, part in _content_parts(req):
        if part.get("type") != "image_url":
            continue
        n += 1
        url = ((part.get("image_url") or {}).get("url") or "")
        if url.startswith("data:") and ";base64," in url:
            try:
                total += len(_b64.b64decode(url.split(";base64,", 1)[1]))
            except Exception:
                pass
    return n, total


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # 离线自测用：最后一次请求体、以及本次进程收到的所有请求体
    last_request = None
    requests = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        # 鉴权：不带头就 401，用来验证前端的错误提示
        if not (self.headers.get("Authorization") or "").startswith("Bearer "):
            body = json.dumps({"error": {"message": "missing api key"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception:
            req = {}
        H.last_request = req
        H.requests.append(req)
        n_img, img_bytes = count_images(req)
        print("[mock] model=%s stream=%s thinking=%s msgs=%d images=%d img_bytes=%d"
              % (req.get("model"), req.get("stream"),
                 "reasoning_effort" in req or "thinking" in req,
                 len(req.get("messages") or []), n_img, img_bytes),
              flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(payload: str):
            data = ("data: " + payload + "\n\n").encode("utf-8")
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        def piece(text, key="content"):
            chunk(json.dumps({"choices": [{"delta": {key: text}}]}, ensure_ascii=False))

        piece("先想一下结构…这个请求是单图首帧。", "reasoning_content")
        time.sleep(0.05)
        full = ("<<<H3_PROMPT>>>\n" + PROMPT + "\n<<<END_H3_PROMPT>>>\n\n"
                "<<<H3_ZH>>>\n" + ZH + "\n<<<END_H3_ZH>>>\n\n"
                "<<<H3_NOTES>>>\n" + NOTES + "\n<<<END_H3_NOTES>>>\n")
        step = 37
        for i in range(0, len(full), step):
            piece(full[i:i + step])
            time.sleep(0.01)
        chunk(json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 3200, "completion_tokens": 700,
                                    "total_tokens": 3900}}, ensure_ascii=False))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    a = ap.parse_args()
    print(f"[mock] DeepSeek 兼容端点在 http://127.0.0.1:{a.port}/chat/completions")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
