"""webui.backend.estimate -- 由形状推算序列长度 / 耗时 / 显存风险。纯算术，不碰 GPU。

关键点：**行数与序列长度的算法直接复用 scripts/h3_audit.py**，不在这里另写一份。
h3_audit 是工程的「前端静态校验 + 序列推算」模块，只依赖标准库，import 它是安全的；
复用它就保证了网页上显示的 seq_len 与 cache/plan.json、与实际运行时完全一致。
（README §5 明确说过：seq 已与框架 PackedSequenceBuilder 逐一对齐，误差 0。）

耗时则不用解析模型，而用「实测点 log-log 插值」：这台机器上解析外推差得很远
（draft 预测 2.7 min / 实测 4.7 min），实测点插值更诚实。
"""
from __future__ import annotations

import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if os.path.join(_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import h3_audit  # noqa: E402  -- 复用官方的行数/形状吸附算法

FPS = 24

# 从 h3_audit 借来的常量（保持同名，方便对照阅读）
AUDIO_LATENT_FPS = h3_audit.AUDIO_LATENT_FPS
AUDIO_CHANNELS = h3_audit.AUDIO_CHANNELS
SEQ_ALIGN = h3_audit.SEQ_ALIGN
TEXT_ROWS_DEFAULT = 1100


# --------------------------------------------------------------------------- 形状
def normalise_shape(width: int, height: int, num_frames: int):
    """吸附到框架网格；返回 (w, h, f, notes)。"""
    notes: list[str] = []
    w, h, f = int(width or 0), int(height or 0), int(num_frames or 0)
    if w <= 0 or h <= 0 or f <= 0:
        return w, h, f, ["形状参数不完整"]
    h2, w2, f2, _lt, _lh, _lw = h3_audit.align_shape(h, w, f)
    if (w2, h2) != (w, h):
        notes.append(f"宽高需为 32 的倍数：{w}x{h} → {w2}x{h2}")
    if f2 != f:
        notes.append(f"帧数需满足 num_frames % 17 == 5：{f} → {f2}"
                     f"（{f / FPS:.2f}s → {f2 / FPS:.2f}s）")
    if f2 < MIN_FRAMES:
        f2 = MIN_FRAMES
        notes.append(f"帧数下限按实测抬到 {MIN_FRAMES}（低于此值框架的形状检查能过，"
                     f"但 VAE 解码会失败）")
    return w2, h2, f2, notes


# 框架只要求 num_frames % 17 == 5，但实测 5 帧会在 VAE 解码处炸
# （decode_video 返回 None -> AttributeError），工程里跑过的最小档是 22 帧（blitz）。
MIN_FRAMES = 22
MAX_FRAMES_MEASURED = 243          # 640x384 实测跑通的最长档（max-long）


def frame_options(near: int, span: int = 6) -> list[int]:
    """给出 near 附近所有合法且实测可跑的帧数（22 + 17k），供前端做步进。"""
    base = [MIN_FRAMES + 17 * k for k in range(0, 14)]
    return [f for f in base if abs(f - near) <= span * 17] or base


# --------------------------------------------------------------------------- 行数
def rows_for(width: int, height: int, num_frames: int,
             refs: list[dict] | None = None,
             ref_image_short_edge: int | None = None,
             ref_video_short_edge: int | None = None,
             ref_video_max_pixels: int | None = None,
             text_rows: int = TEXT_ROWS_DEFAULT) -> dict:
    """refs: [{"kind": "image"|"video"|"video_audio"|"audio", "w","h","frames","seconds"}, ...]

    没给尺寸的参考素材按 common case 估算（图片 1024x1024、视频 832x480 与目标等长、
    音频按时长），并在返回里标注 estimated=True，前端会提示这是估算值。
    """
    h, w, f, lt, lh, lw = h3_audit.align_shape(int(height), int(width), int(num_frames))
    edges = {
        "image": int(ref_image_short_edge or 1024),
        "video": int(ref_video_short_edge or 384),
        "video_max_pixels": int(ref_video_max_pixels or (int(ref_video_short_edge or 384) * 672)),
    }
    img_rows = vid_rows = ref_audio_rows = 0
    estimated = False
    img_dims: list[tuple] = []
    vid_dims: list[tuple] = []
    per_ref: list[dict] = []

    for r in (refs or []):
        kind = (r or {}).get("kind") or "image"
        if kind == "image":
            iw = int(r.get("w") or 1024)
            ih = int(r.get("h") or 1024)
            if not r.get("w"):
                estimated = True
            n, lhh, lww = h3_audit.ref_image_rows(iw, ih, edges["image"])
            img_rows += n
            img_dims.append((iw, ih, lhh, lww))
            per_ref.append({"kind": kind, "rows": n, "detail": f"{iw}x{ih} → latent {lhh}x{lww}"})
        elif kind in ("video", "video_audio"):
            rw = int(r.get("w") or 832)
            rh = int(r.get("h") or 480)
            rf = int(r.get("frames") or num_frames)
            if not r.get("w"):
                estimated = True
            n, ltr, lhh, lww, used = h3_audit.ref_video_rows(
                rw, rh, rf, f, edges["video"], edges["video_max_pixels"])
            vid_rows += n
            vid_dims.append((rw, rh, ltr, lhh, lww))
            per_ref.append({"kind": kind, "rows": n,
                            "detail": f"{rw}x{rh}x{rf}f → {ltr} latent frames of {lhh}x{lww}"})
            if kind == "video_audio":
                # 只统计真实送进去的长度：框架会先把参考视频裁到 used 帧
                ar = round(used / 24.0 * AUDIO_LATENT_FPS) * AUDIO_CHANNELS
                ref_audio_rows += ar
                per_ref.append({"kind": "video_audio(track)", "rows": ar,
                                "detail": f"自带音轨（裁到 {used} 帧）"})
        elif kind == "audio":
            secs = float(r.get("seconds") or 3.0)
            if not r.get("seconds"):
                estimated = True
            ar = round(secs * AUDIO_LATENT_FPS) * AUDIO_CHANNELS
            ref_audio_rows += ar
            per_ref.append({"kind": kind, "rows": ar, "detail": f"{secs:.2f}s 音频参考"})

    at = round(f / 24.0 * AUDIO_LATENT_FPS)
    target_rows = lt * (lh // 2) * (lw // 2)
    audio_rows = at * AUDIO_CHANNELS
    used = text_rows + img_rows + vid_rows + ref_audio_rows + audio_rows + target_rows
    seq = -(-used // SEQ_ALIGN) * SEQ_ALIGN

    return {
        "text": text_rows, "ref_image": img_rows, "ref_video": vid_rows,
        "ref_audio": ref_audio_rows, "target_audio": audio_rows,
        "target_video": target_rows, "used": used, "seq_len": seq,
        "aligned": {"width": w, "height": h, "num_frames": f,
                    "seconds": round(f / FPS, 2), "latent_t": lt,
                    "latent_h": lh, "latent_w": lw, "audio_latent_t": at},
        "ref_image_latents": img_dims, "ref_video_latents": vid_dims,
        "per_ref": per_ref, "estimated": estimated,
        "edges": edges,
    }


# --------------------------------------------------------------------------- 耗时
def _measured_points(points: list[dict]) -> list[tuple[float, float]]:
    pts = []
    for p in points or []:
        try:
            pts.append((float(p["seq"]), float(p["step_s"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(pts)


def step_seconds(seq: int, points: list[dict]):
    """单步耗时 + 置信度标签。实测点之间做 log-log 插值，两端按区间斜率外推。"""
    pts = _measured_points(points)
    if not pts:
        return 0.0, "no-data"
    if seq <= pts[0][0]:
        return round(pts[0][1], 2), "at-floor"
    if seq >= pts[-1][0]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
        slope = (math.log(y1) - math.log(y0)) / (math.log(x1) - math.log(x0))
        return round(y1 * (seq / x1) ** slope, 2), "extrapolated"
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= seq <= x1:
            if x1 == x0:
                return round(y1, 2), "interpolated"
            t = (seq - x0) / (x1 - x0)
            val = math.exp(math.log(y0) + t * (math.log(y1) - math.log(y0)))
            gap = (x1 - x0) / max(1.0, seq)
            return round(val, 2), ("interpolated" if gap <= 0.6 else "wide-gap")
    return round(pts[-1][1], 2), "unknown"


FIXED_OVERHEAD = {
    "pipeline_build_s": 14.0,   # 建 pipeline + 模型换入换出（loadcheck 实测 5.5~14 s）
    "ref_decode_s": 7.0,        # 参考图/视频解码（实测约 6~8 s）
    "text_encode_cold_s": 12.0,  # 文本编码器冷启动（实测 12.1 s，缓存命中 0）
    "vae_decode_s": 6.0,        # VAE 解码 + 封装（0.92s 视频约 2.7 s，10 s 视频约 8 s）
}


def estimate(width: int, height: int, num_frames: int, steps: int, points: list[dict],
             **rows_kw) -> dict:
    rows = rows_for(width, height, num_frames, **rows_kw)
    s_step, confidence = step_seconds(rows["seq_len"], points)
    denoise = s_step * int(steps or 0)
    fixed = FIXED_OVERHEAD["pipeline_build_s"] + FIXED_OVERHEAD["vae_decode_s"]
    if rows_kw.get("refs"):
        fixed += FIXED_OVERHEAD["ref_decode_s"]
    total = denoise + fixed
    return {
        "rows": rows,
        "step_s": s_step,
        "step_confidence": confidence,
        "denoise_s": round(denoise, 1),
        "fixed_s": round(fixed, 1),
        "total_s": round(total, 1),
        "total_min": round(total / 60, 1),
        "seconds": rows["aligned"]["seconds"],
        "realtime_factor": round(total / rows["aligned"]["seconds"], 1)
                           if rows["aligned"]["seconds"] else None,
        "text_cache_hit_note": "文本编码已缓存时固定开销再减 " + str(FIXED_OVERHEAD["text_encode_cold_s"]) + " s",
    }


# --------------------------------------------------------------------------- 风险
def risks(width: int, height: int, num_frames: int, seq: int, *,
          vram_limit: float | None = None, ref_image_short_edge: int | None = None,
          dit_onload: str = "cpu", lora: str | None = None,
          steps: int | None = None, refs: list[dict] | None = None,
          ref_video_short_edge: int | None = None, spec: dict | None = None) -> list[dict]:
    """中文风险提示。level: danger(会失败) / warn(可能失败或很慢) / info(值得知道)。"""
    out: list[dict] = []
    spec = spec or {}
    shape = spec.get("shape", {})
    ram_total = shape.get("ram_total_gib", 22.91)
    kinds = {((r or {}).get("kind") or "image") for r in (refs or [])}

    if seq >= 30000:
        out.append({"level": "danger", "code": "vram_wall",
                    "msg": f"seq≈{seq} 落在实测的显存墙之外（最高跑通点 seq 23872 / 峰值 5.94 GiB）。",
                    "hint": "官方原生 1344x768x124 在这张 8 GiB 卡上单独跑也 OOM。"
                            "降分辨率或降帧数 —— 长时长与大画布只能二选一。"})
    elif seq >= 24000:
        out.append({"level": "warn", "code": "vram_high",
                    "msg": f"seq≈{seq} 已逼近实测上限，峰值显存预计 5.4~6.0 GiB。",
                    "hint": "建议参考图短边 768、dit_onload=disk，并先关掉其它占显存的程序。"})

    if dit_onload == "cpu" and (num_frames > 124 or width * height > 832 * 480):
        out.append({"level": "warn", "code": "ram_wall",
                    "msg": f"dit_onload=cpu 会把 9.76 GiB 的 DiT 放进内存，叠上文本编码器的 "
                           f"14.27 GiB mmap 会顶到 WSL 的 {ram_total} GiB 配额。",
                    "hint": "长片段/大画布一律改用 dit_onload=disk；README §11.3 记录过一次"
                            "因此把整个 WSL 发行版打死。"})

    if kinds & {"video", "video_audio"} and (ref_video_short_edge or 384) >= 768:
        out.append({"level": "warn", "code": "ref_video_cost",
                    "msg": f"参考视频短边 {ref_video_short_edge}：与目标等长时会额外增加大量序列行，"
                           "注意力是平方项，实测可让总时长翻倍。",
                    "hint": "把参考视频短边降到 384~512。"})

    if lora:
        out.append({"level": "info", "code": "lora_scheduler",
                    "msg": "挂了 LoRA：调度器会自动切到 beta（AfterMidnight 要求 euler + beta，"
                           "否则音频会出问题）。",
                    "hint": "scheduler=auto 就是这个行为；想强制可以把 scheduler 改成 flow/beta。"})

    if vram_limit is not None:
        if vram_limit >= 6.5:
            out.append({"level": "warn", "code": "vram_limit_high",
                        "msg": f"vram_limit={vram_limit} GiB 会让更多层常驻显存，但实测提速有限"
                               "（4.58→6.30 时单步 13.17→13.34 s），余量却不到 1 GiB。",
                        "hint": "不确定就保持 4.58；要冒险用 activation_reserve 反推更直观。"})
        elif vram_limit <= 3.5:
            out.append({"level": "info", "code": "vram_limit_low",
                        "msg": f"vram_limit={vram_limit} GiB 偏低，每步要重建的层更多，会明显变慢。",
                        "hint": "这台机器的甜点大约在 4.5~5.0。"})

    if num_frames < MIN_FRAMES:
        out.append({"level": "warn", "code": "frames_too_short",
                    "msg": f"{num_frames} 帧（{num_frames / 24:.2f}s）虽然满足 17k+5 的网格规则，"
                           f"但实测低于 {MIN_FRAMES} 帧时 VAE 解码会失败。",
                    "hint": f"最短请用 {MIN_FRAMES} 帧（{MIN_FRAMES / 24:.2f}s）；"
                            "工程的 blitz 档就是这一档。"})
    if steps is not None and steps < 20:
        out.append({"level": "info", "code": "steps_low",
                    "msg": f"{steps} 步适合试构图/试种子；挂 LoRA 时低步数容易出音频瑕疵。",
                    "hint": "定稿建议 30 步以上。"})
    if ref_image_short_edge and ref_image_short_edge >= 1536:
        out.append({"level": "info", "code": "ref_image_cost",
                    "msg": f"参考图短边 {ref_image_short_edge}：注意力是平方项，"
                           "2048（框架默认）比 1024 多约 54% 注意力。",
                    "hint": "人物/产品一致性要求高时才用 1536 以上。"})
    return out
