"""webui.backend.media -- 参考素材 -> OpenAI 兼容的多模态 content 块。

DeepSeek 的 `deepseek-flash` 接受「content 是块数组」的 user message：

    {"type": "text", "text": "..."}
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,...", "detail": "high"}}

本模块负责把本地参考图（以及可选的参考视频抽帧）变成后者。约束全部来自官方
「图像理解」文档（https://api-docs.deepseek.com/zh-cn/guides/vision）：

  * 支持格式：JPEG / PNG / GIF / WebP，格式按**文件内容**判定，不看扩展名；
  * 单图 base64/URL 上限 32 MiB，单请求请求体上限 48 MiB，最多 600 张图；
  * 单边最大 8192 px（≥15 张图时 4096）；
  * 图片单边超出约 1300 px 后服务端会等比缩到约 1300x1300 的总像素，**再大会浪费请求体**，
    所以默认把长边收到 1280 px；`detail: low` 时服务端只按 512x512 处理，这里直接按 512 收。
  * 图片只允许出现在 user message 里。

设计约束与工程其它部分一致：**只用标准库**（外加系统 ffmpeg 做缩放/抽帧），
绝不 import torch / Pillow。任何失败都返回带 error 的元数据，不抛异常 ——
多模态是「锦上添花」，绝不能因为它把提示词优化整条链路弄挂。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import struct
import subprocess
import tempfile

# --------------------------------------------------------------------------- 常量
# 服务端硬上限（见模块 docstring），这里只做兜底，正常配置都比它小。
HARD_MAX_IMAGE_BYTES = 32 * 1024 * 1024
HARD_MAX_REQUEST_BYTES = 48 * 1024 * 1024
HARD_MAX_IMAGES = 600
HARD_MAX_EDGE = 8192

SUPPORTED = {"jpeg": "image/jpeg", "png": "image/png",
             "gif": "image/gif", "webp": "image/webp"}

# ffprobe 认成这些 codec 才算「真的是图片」。没有它，ffprobe 会把任意字节猜成
# bintext/rawvideo 之类，然后 ffmpeg 热心地把它「解码」成一张图（实测过）。
_PROBE_IMAGE_CODECS = {
    "bmp", "mjpeg", "jpeg", "ljpeg", "jpegls", "png", "gif", "webp", "tiff",
    "avif", "heic", "heif", "jpeg2000", "jp2", "dpx", "exr", "targa", "pcx",
    "pnm", "pbm", "pgm", "ppm", "pam", "qdraw", "xwd", "svg", "ico",
}

# 读取文件头做格式/尺寸判定；256 KiB 足够覆盖 JPEG 的 SOF 段。
_HEAD_BYTES = 256 * 1024

_FFMPEG: str | None = None
_FFMPEG_PROBED = False
_FFPROBE: str | None = None
_FFPROBE_PROBED = False


# --------------------------------------------------------------------------- 格式识别
def sniff(data: bytes) -> str | None:
    """按文件内容判定格式，返回 jpeg/png/gif/webp 之一，未知返回 None。"""
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def dimensions(data: bytes, fmt: str) -> tuple[int, int] | None:
    """从文件头解析 (width, height)；解析不出来返回 None。纯标准库。"""
    try:
        if fmt == "png":
            if len(data) < 24:
                return None
            w, h = struct.unpack(">II", data[16:24])
            return (int(w), int(h)) if w and h else None
        if fmt == "gif":
            if len(data) < 10:
                return None
            w, h = struct.unpack("<HH", data[6:10])
            return (int(w), int(h)) if w and h else None
        if fmt == "webp":
            return _webp_dims(data)
        if fmt == "jpeg":
            return _jpeg_dims(data)
    except Exception:
        return None
    return None


def _webp_dims(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    fourcc = data[12:16]
    if fourcc == b"VP8 ":
        w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
    elif fourcc == b"VP8L":
        if len(data) < 25 or data[20] != 0x2F:
            return None
        bits = struct.unpack("<I", data[21:25])[0]
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
    elif fourcc == b"VP8X":
        if len(data) < 30:
            return None
        w = (data[24] | (data[25] << 8) | (data[26] << 16)) + 1
        h = (data[27] | (data[28] << 8) | (data[29] << 16)) + 1
    else:
        return None
    return (int(w), int(h)) if w and h else None


def _jpeg_dims(data: bytes) -> tuple[int, int] | None:
    """扫 JPEG 段，找 SOFn（0xC0~0xCF，排除 DHT/JPG/DAC）。"""
    i, n = 2, len(data)
    while i + 3 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:   # 无长度字段
            i += 2
            continue
        if marker == 0xDA:                                      # 到 SOS 就结束
            return None
        if i + 4 > n:
            return None
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        if seglen < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (int(w), int(h)) if w and h else None
        i += 2 + seglen
    return None


# --------------------------------------------------------------------------- ffmpeg
def ffmpeg_path() -> str | None:
    global _FFMPEG, _FFMPEG_PROBED
    if not _FFMPEG_PROBED:
        _FFMPEG = shutil.which("ffmpeg")
        _FFMPEG_PROBED = True
    return _FFMPEG


def ffprobe_path() -> str | None:
    global _FFPROBE, _FFPROBE_PROBED
    if not _FFPROBE_PROBED:
        _FFPROBE = shutil.which("ffprobe")
        _FFPROBE_PROBED = True
    return _FFPROBE


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _probe_image_dims(path: str) -> tuple[int, int] | None:
    """用 ffprobe 认一下这到底是不是图片，并顺便读出尺寸。

    只在文件头认不出格式（BMP/TIFF/AVIF/…）时调用：ffprobe 不认就直接判失败，
    不把二进制垃圾丢给 ffmpeg —— ffmpeg 会「热心」地把任意字节猜成某种图片格式。
    """
    fp = ffprobe_path()
    if not fp:
        return None
    cmd = [fp, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,codec_type,codec_name",
           "-of", "json", path]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=30)
        if p.returncode != 0:
            return None
        data = json.loads((p.stdout or b"").decode("utf-8", "replace") or "{}")
        st = (data.get("streams") or [{}])[0]
        codec = str(st.get("codec_name") or "").lower()
        if st.get("codec_type") != "video" or codec not in _PROBE_IMAGE_CODECS:
            return None
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        return (w, h) if w and h else None
    except Exception:                                          # noqa: BLE001
        return None


def _run(cmd: list[str], timeout: float = 90.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=timeout)
        return p.returncode, (p.stderr or b"").decode("utf-8", "replace")[:400]
    except FileNotFoundError:
        return 127, "ffmpeg 不存在"
    except subprocess.TimeoutExpired:
        return 124, f"ffmpeg 超时（>{timeout:.0f}s）"
    except Exception as e:                                     # noqa: BLE001
        return 1, f"{e}"


def _vf(cap: int) -> str:
    """等比缩放到长边 <= cap，且**永不放大**。

    ffmpeg 的 force_original_aspect_ratio=decrease 在目标框比原图大时仍会放大，
    所以用 min(iw,cap) / min(ih,cap) 把上界钉死（实测 800x600 曾被放大到 8192x6144）。
    filtergraph 表达式里的逗号按 ffmpeg 规则写成「反斜杠 + 逗号」。
    """
    return (f"scale=min(iw\\,{int(cap)}):min(ih\\,{int(cap)}):"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2")


def _scale_dst(src: str, dst: str, cap: int, quality: int = 3) -> tuple[bool, str]:
    """等比缩放到长边 <= cap 并转成 JPEG（只缩不放）。"""
    ff = ffmpeg_path()
    if not ff:
        return False, "找不到 ffmpeg"
    rc, err = _run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                    "-vf", _vf(cap), "-frames:v", "1", "-q:v", str(quality),
                    "-f", "image2", dst])
    return (rc == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0), err


def _grab_frame(src: str, dst: str, at_s: float, cap: int, quality: int = 3) -> tuple[bool, str]:
    """在 at_s 秒抓一帧并等比缩到长边 <= cap（JPEG，只缩不放）。"""
    ff = ffmpeg_path()
    if not ff:
        return False, "找不到 ffmpeg"
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y"]
    if at_s > 0:
        cmd += ["-ss", f"{at_s:.3f}"]
    cmd += ["-i", src, "-frames:v", "1", "-vf", _vf(cap), "-q:v", str(quality),
            "-f", "image2", dst]
    rc, err = _run(cmd)
    return (rc == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0), err


# --------------------------------------------------------------------------- 单张图
def _read_head(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(_HEAD_BYTES)


def _data_uri(raw: bytes, mime: str) -> str:
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")


def _effective_cap(max_edge, detail) -> int:
    """把配置折算成真正的长边上限。

    detail=low 时服务端只按 512x512 处理，多发像素纯属浪费请求体；
    max_edge=0 表示不主动缩放（此时仅做必要的格式转换）。
    """
    cap = int(max_edge or 0)
    if str(detail or "").strip().lower() == "low":
        cap = min(cap, 512) if cap else 512
    return cap


def prepare_image(path: str, *, max_edge=1280, detail="high",
                  max_bytes=20 * 1024 * 1024) -> dict:
    """把一张本地图片准备成可附进请求的 image_url 块。

    返回元数据 dict；失败时 ok=False 且带 error。成功时含：
      data_uri / mime / width / height / sent_width / sent_height /
      bytes_in / bytes_out / resized / format
    """
    max_bytes = max(1, int(max_bytes or 20 * 1024 * 1024))
    info: dict = {"ok": False, "kind": "image", "path": path,
                  "name": os.path.basename(path or "")}
    if not path or not os.path.isfile(path):
        info["error"] = "文件不存在或不可读"
        return info
    try:
        bytes_in = os.path.getsize(path)
    except OSError as e:
        info["error"] = f"stat 失败：{e}"
        return info
    info["bytes_in"] = bytes_in

    head = b""
    try:
        head = _read_head(path)
    except OSError as e:
        info["error"] = f"读取失败：{e}"
        return info
    fmt = sniff(head)
    dims = dimensions(head, fmt) if fmt else None
    if fmt is None:
        # 文件头认不出（BMP/TIFF/AVIF/垃圾）：先让 ffprobe 确认确实是图片，
        # 否则 ffmpeg 会把任意字节「热心」地猜成某种图片格式并产出图片。
        probed = _probe_image_dims(path)
        if probed:
            dims = probed
            info["format"] = "other"
        elif ffprobe_path():
            info["error"] = ("不是可识别的图片格式（支持 JPEG/PNG/GIF/WebP，"
                             "以及 ffmpeg 能解码的 BMP/TIFF 等）")
            return info
    if fmt is not None:
        info["format"] = fmt
    info["mime"] = SUPPORTED.get(fmt or "", "application/octet-stream")
    if dims:
        info["width"], info["height"] = dims

    cap = _effective_cap(max_edge, detail)
    supported = fmt in SUPPORTED
    # 需要转换：格式本身不被服务端接受（BMP/TIFF/未知），或尺寸未知（交给 ffmpeg 判）
    need_convert = (not supported) or (dims is None)
    # 需要缩放：明确超过长边上限，或原图大到接近单图上限
    need_scale = bool(cap) and ((dims is not None and max(dims) > cap) or
                                bytes_in > max_bytes)
    if supported and bytes_in > HARD_MAX_IMAGE_BYTES:
        need_scale = True

    if not need_convert and not need_scale:
        try:
            raw = _read_bytes(path)
        except OSError as e:
            info["error"] = f"读取失败：{e}"
            return info
        if len(raw) <= HARD_MAX_IMAGE_BYTES and len(raw) <= max_bytes:
            return _finish(info, raw, SUPPORTED[fmt], resized=False)

    # 走到这里必须借 ffmpeg：转换 / 缩放 / 又转换又缩放
    if not ffmpeg_path():
        if supported and bytes_in <= min(HARD_MAX_IMAGE_BYTES, max_bytes):
            raw = _read_bytes(path)
            note = "本机没有 ffmpeg，未压缩直接附上原图"
            out = _finish(info, raw, SUPPORTED[fmt], resized=False)
            out["note"] = note
            return out
        info["error"] = "需要 ffmpeg 缩放/转换图片，但系统里找不到 ffmpeg"
        return info

    base_cap = cap or HARD_MAX_EDGE
    attempts = []
    for c in (base_cap, 1024, 768, 512, 384):
        c = max(64, min(int(c), HARD_MAX_EDGE))
        if c not in attempts:
            attempts.append(c)

    tmpdir = tempfile.mkdtemp(prefix="h3mm-")
    last_err = ""
    try:
        dst = os.path.join(tmpdir, "out.jpg")
        for c in attempts:
            ok, err = _scale_dst(path, dst, c)
            if not ok:
                last_err = err or "ffmpeg 转换失败"
                info["error"] = f"ffmpeg 处理图片失败：{last_err}"
                return info
            out_bytes = os.path.getsize(dst)
            if out_bytes <= max_bytes or c == attempts[-1]:
                raw = _read_bytes(dst)
                out = _finish(info, raw, "image/jpeg", resized=True)
                if out_bytes > HARD_MAX_IMAGE_BYTES:
                    info["error"] = (f"缩放后仍超过单图上限 "
                                     f"({out_bytes / 1024 ** 2:.1f} MiB > "
                                     f"{HARD_MAX_IMAGE_BYTES / 1024 ** 2:.0f} MiB)")
                    return info
                if out_bytes > max_bytes:
                    info["error"] = (f"缩放后仍超过本次预算 "
                                     f"({out_bytes / 1024 ** 2:.1f} MiB > "
                                     f"{max_bytes / 1024 ** 2:.1f} MiB)")
                    return info
                if c != base_cap:
                    out["note"] = f"为压到预算内缩放至长边 {c}px"
                return out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    info["error"] = last_err or "ffmpeg 未能产出可用图片"
    return info


def _finish(info: dict, raw: bytes, mime: str, *, resized: bool) -> dict:
    out = dict(info)
    out["ok"] = True
    out.pop("error", None)
    out["mime"] = mime
    out["bytes_out"] = len(raw)
    out["resized"] = bool(resized)
    out["data_uri"] = _data_uri(raw, mime)
    fmt = sniff(raw)
    dims = dimensions(raw, fmt) if fmt else None
    if dims:
        out["sent_width"], out["sent_height"] = dims
    else:
        out.setdefault("sent_width", out.get("width"))
        out.setdefault("sent_height", out.get("height"))
    return out


def prepare_video_frame(path: str, *, at_s: float = 0.0, max_edge=1280, detail="high",
                        max_bytes=20 * 1024 * 1024) -> dict:
    """从参考视频里抓一帧当作图片附件。失败时 ok=False。"""
    info: dict = {"ok": False, "kind": "video_frame", "path": path,
                  "name": os.path.basename(path or "")}
    if not path or not os.path.isfile(path):
        info["error"] = "文件不存在或不可读"
        return info
    if not ffmpeg_path():
        info["error"] = "抽视频帧需要 ffmpeg，但系统里找不到"
        return info
    cap = _effective_cap(max_edge, detail) or 1280
    tmpdir = tempfile.mkdtemp(prefix="h3mm-")
    try:
        dst = os.path.join(tmpdir, "frame.jpg")
        ok, err = _grab_frame(path, dst, max(0.0, float(at_s or 0.0)), cap)
        if not ok:
            info["error"] = f"抽帧失败：{err or '未知错误'}"
            return info
        raw = _read_bytes(dst)
        info["bytes_in"] = os.path.getsize(path)
        out = _finish(info, raw, "image/jpeg", resized=True)
        out["at_s"] = round(float(at_s or 0.0), 3)
        return out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------- 批量收集
def public_attachment(att: dict) -> dict:
    """去掉 data_uri 后的元数据：可以安全写进日志 / LLM 记录。"""
    return {k: v for k, v in att.items() if k != "data_uri"}


def _mb(value, default: float) -> int:
    try:
        return int(float(value) * 1024 * 1024)
    except (TypeError, ValueError):
        return int(default * 1024 * 1024)


def collect_attachments(refs: list[dict], mm: dict, *, log=None) -> tuple[list[dict], list[str]]:
    """按参考清单顺序收集要附上的图片。

    refs 是前端传来的**有序**清单（含 kind / path / label / name / seconds）。
    mm 是 config/deepseek.yaml 的 multimodal 段。返回 (attachments, notes)：
    attachments[i] 含 data_uri，顺序即 content 块顺序；notes 是给人看的跳过/失败原因。
    """
    mm = mm or {}
    if not mm.get("enabled"):
        return [], []
    images_on = mm.get("images", True)
    try:
        video_frames = max(0, int(mm.get("video_frames") or 0))
    except (TypeError, ValueError):
        video_frames = 0
    if not images_on and not video_frames:
        return [], []

    try:
        max_images = max(1, int(mm.get("max_images") or 8))
    except (TypeError, ValueError):
        max_images = 8
    max_images = min(max_images, HARD_MAX_IMAGES)
    detail = mm.get("detail") or "high"
    max_edge = mm.get("max_edge", 1280)
    per_image = _mb(mm.get("max_mb_per_image"), 20)
    total_cap = min(_mb(mm.get("max_mb_total"), 40), HARD_MAX_REQUEST_BYTES)
    per_image = min(per_image, HARD_MAX_IMAGE_BYTES)

    attachments: list[dict] = []
    notes: list[str] = []
    used = 0
    skipped_budget = 0
    skipped_count = 0

    def budget_ok(size_bytes: int) -> bool:
        # base64 后约放大 4/3
        return used + int(size_bytes * 4 / 3) + 1024 <= total_cap

    for ref in refs or []:
        kind = (ref.get("kind") or "image")
        label = ref.get("label") or ""
        name = ref.get("name") or os.path.basename(ref.get("path") or "") or "(未命名)"
        path = ref.get("path") or ""
        if kind == "image":
            if not images_on:
                continue
            if len(attachments) >= max_images:
                skipped_count += 1
                continue
            if not path:
                notes.append(f"{label or name}：没有本地路径，无法附图")
                continue
            try:
                est = os.path.getsize(path)
            except OSError:
                est = 0
            if est and not budget_ok(est):
                skipped_budget += 1
                continue
            att = prepare_image(path, max_edge=max_edge, detail=detail, max_bytes=per_image)
        elif kind in ("video", "video_audio") and video_frames > 0:
            if not path:
                notes.append(f"{label or name}：没有本地路径，无法抽帧")
                continue
            secs = ref.get("seconds")
            try:
                secs = float(secs) if secs else 0.0
            except (TypeError, ValueError):
                secs = 0.0
            for i in range(video_frames):
                if len(attachments) >= max_images:
                    skipped_count += 1
                    break
                at = (secs * (i + 0.5) / video_frames) if secs > 0 else 0.0
                att = prepare_video_frame(path, at_s=at, max_edge=max_edge, detail=detail,
                                          max_bytes=per_image)
                if not att.get("ok"):
                    notes.append(f"{label or name} 第 {i + 1} 帧：{att.get('error')}")
                    continue
                if not budget_ok(att.get("bytes_out") or 0):
                    skipped_budget += 1
                    continue
                att["label"] = (label + f" 采样帧 {i + 1}/{video_frames}") if label \
                    else f"采样帧 {i + 1}/{video_frames}"
                attachments.append(att)
                used += int((att.get("bytes_out") or 0) * 4 / 3)
            continue
        else:
            continue

        if not att.get("ok"):
            notes.append(f"{label or name}：{att.get('error')}")
            continue
        if not budget_ok(att.get("bytes_out") or 0):
            skipped_budget += 1
            continue
        att["label"] = label
        att["name"] = name
        attachments.append(att)
        used += int((att.get("bytes_out") or 0) * 4 / 3)

    if skipped_budget:
        notes.append(f"{skipped_budget} 张图片因为超过本次请求体预算被跳过")
    if skipped_count:
        notes.append(f"还有 {skipped_count} 张图片超过 max_images={max_images}，未附上")
    if notes and log is not None:
        log.warn("多模态附图有跳过/失败：" + "；".join(notes),
                 source="llm", event="llm.multimodal")
    return attachments, notes


# --------------------------------------------------------------------------- CLI
if __name__ == "__main__":                                     # pragma: no cover
    import json
    import sys

    for p in sys.argv[1:]:
        print(json.dumps(public_attachment(
            prepare_image(p, max_edge=1280, detail="high")), ensure_ascii=False, indent=2))
