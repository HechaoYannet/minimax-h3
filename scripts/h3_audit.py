#!/usr/bin/env python3
"""h3_audit.py -- inventory the deployed MiniMax-H3 files and turn measured
hardware limits into a concrete, executable work plan.

Loads NO model weights and runs NO inference: it reads safetensors *headers*
(key names / shapes / dtypes) plus the micro-benchmark results from h3_bench.py.
Every shape/time formula here is cross-checked against the framework's own code
by h3_validate.py, which writes cache/validate.json; this script compares.

Outputs  cache/plan.json  (consumed by h3_generate.py)  +  a stdout report.
"""
from __future__ import annotations

import argparse, json, math, os, struct, sys, time
from dataclasses import dataclass, field, asdict

H3_ROOT = os.environ.get("H3_ROOT", "/home/yhc/source/minimax-h3")
H3_REPO = os.environ.get("H3_REPO", os.path.join(H3_ROOT, "DiffSynth-Studio"))
MODELS = os.environ.get("H3_MODELS", os.path.join(H3_ROOT, "models"))
WS = os.environ.get("H3_WORKSPACE", "/mnt/d/otherProject/minimax-h3")
if H3_REPO not in sys.path:
    sys.path.insert(0, H3_REPO)

FILES = {
    "dit":          ("minimax-h3-ref2va-pruned-nf4.safetensors", True),
    "text_encoder": ("minimax-h3-text-encoder-nf4.safetensors", True),
    "video_vae":    ("video_vae_nf4.safetensors", True),
    "audio_vae":    ("audio_vae_nf4.safetensors", True),
    "lora":         ("AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors", False),
}
DTYPE_BYTES = {"F64": 8, "F32": 4, "F16": 2, "BF16": 2, "I64": 8, "I32": 4,
               "I16": 2, "I8": 1, "U8": 1, "BOOL": 1, "F8_E4M3": 1, "F8_E5M2": 1}

# ---- MiniMax-H3 DiT constants (diffsynth/models/minimax_h3_dit.py) ----------
# MiniMaxH3DiTBlock = attn(qkv_proj 5376->21504, out_proj 5376->7168) +
# mlp(fc1 5376->28672 gated, fc2 14336->5376) = 385.3 M params/block.
# The AdaLN projection is only 8 -> 96768 here: the *pruned* checkpoint replaces
# the 2688-dim time embedder with the 1025x8 adaln_t_table buffer, so it costs
# 0.87 M params instead of 260 M.  h3_validate.py measures the real total by
# building the model on the meta device; the constant below is only a fallback.
DIT_BLOCK_PARAMS = 5376 * 21504 + 5376 * 7168 + 5376 * 28672 + 14336 * 5376
DIT_BLOCKS = 50
REFINER_BLOCKS = 2
DIT_PARAMS_LINEAR = (DIT_BLOCKS + REFINER_BLOCKS) * DIT_BLOCK_PARAMS
if os.environ.get("H3_DIT_PARAMS"):
    DIT_PARAMS_LINEAR = float(os.environ["H3_DIT_PARAMS"])
HEADS, HEAD_DIM = 56, 128
SEQ_ALIGN = 64
TIME_DIV_FACTOR, TIME_DIV_REMAINDER = 17, 5
AUDIO_LATENT_FPS = 40.0
AUDIO_CHANNELS = 2


def read_safetensors_header(path):
    with open(path, "rb") as f:
        return json.loads(f.read(struct.unpack("<Q", f.read(8))[0]))


def human(n):
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {u}"
        n /= 1024
    return f"{n:.1f} PiB"


def gb(n):
    return round(n / 1024 ** 3, 3)


# --------------------------------------------------------------------------- files
@dataclass
class ModelFile:
    key: str
    name: str
    path: str
    expect_registered: bool = True
    size_bytes: int = 0
    tensor_count: int = 0
    dtype_bytes: dict = field(default_factory=dict)
    top_level_bytes: dict = field(default_factory=dict)
    hash: str | None = None
    matched_model_name: str | None = None
    matched_model_class: str | None = None
    matched_quant: dict | None = None
    recognized: bool = False
    note: str = ""

    def as_dict(self):
        d = asdict(self)
        d["size_h"] = human(self.size_bytes)
        return d


def inspect_file(key, name, expect_registered):
    path = os.path.join(MODELS, name)
    mf = ModelFile(key=key, name=name, path=path, expect_registered=expect_registered)
    if not os.path.exists(path):
        mf.note = "MISSING"
        return mf
    mf.size_bytes = os.path.getsize(path)
    header = read_safetensors_header(path)
    header.pop("__metadata__", None)
    mf.tensor_count = len(header)
    for k, v in header.items():
        nb = DTYPE_BYTES.get(v["dtype"], 0) * math.prod(v["shape"])
        mf.dtype_bytes[v["dtype"]] = mf.dtype_bytes.get(v["dtype"], 0) + nb
        if k.startswith("blocks."):
            top = "blocks." + k.split(".")[1]
        elif k.startswith("lora_unet_blocks_"):
            top = "lora_unet_blocks"
        elif k.startswith("model.language_model.layers."):
            top = "text.layers." + k.split(".")[3]
        else:
            top = k.split(".")[0]
        mf.top_level_bytes[top] = mf.top_level_bytes.get(top, 0) + nb
    mf.top_level_bytes = dict(sorted(mf.top_level_bytes.items(), key=lambda kv: -kv[1])[:10])
    from diffsynth.core.loader import hash_model_file
    t0 = time.perf_counter()
    mf.hash = hash_model_file(path)
    mf.note = f"hashed in {time.perf_counter()-t0:.1f}s"
    from diffsynth.configs import MODEL_CONFIGS
    for cfg in MODEL_CONFIGS:
        if cfg["model_hash"] == mf.hash:
            mf.recognized = True
            mf.matched_model_name = cfg["model_name"]
            mf.matched_model_class = cfg["model_class"]
            mf.matched_quant = cfg.get("quant_config")
            break
    return mf


# --------------------------------------------------------------------------- sequence
def nearest_multiple(value, multiple):
    """Exact copy of MiniMaxH3Unit_ReferenceEncoder._nearest_multiple."""
    return max(multiple, int(round(float(value) / multiple)) * multiple)


def align_shape(height, width, num_frames):
    if height % 32:
        height = -(-height // 32) * 32
    if width % 32:
        width = -(-width // 32) * 32
    if num_frames % TIME_DIV_FACTOR != TIME_DIV_REMAINDER:
        num_frames = ((num_frames - TIME_DIV_REMAINDER + TIME_DIV_FACTOR - 1)
                      // TIME_DIV_FACTOR) * TIME_DIV_FACTOR + TIME_DIV_REMAINDER
    return (height, width, num_frames, ((num_frames - 5) // 17) * 5 + 2,
            height // 16, width // 16)


def latent_frames_for(frames):
    """_trim_reference_video_length() then the latent-frame count."""
    chunks = max(1, (int(frames) - TIME_DIV_REMAINDER) // TIME_DIV_FACTOR)
    used = chunks * TIME_DIV_FACTOR + TIME_DIV_REMAINDER
    return ((used - 5) // 17) * 5 + 2, used


def ref_image_rows(img_w, img_h, short_edge):
    """Rows one reference image adds -- uses the IMAGE's own size, not the canvas."""
    scale = short_edge / min(img_w, img_h)
    tw = nearest_multiple(img_w * scale, 32)
    th = nearest_multiple(img_h * scale, 32)
    lw, lh = tw // 16, th // 16
    return (lh // 2) * (lw // 2), lh, lw


def ref_video_rows(ref_w, ref_h, ref_frames, target_frames, short_edge, max_pixels):
    scale = min(short_edge / min(ref_w, ref_h),
                math.sqrt(max_pixels / float(ref_w * ref_h)))
    tw = nearest_multiple(ref_w * scale, 32)
    th = nearest_multiple(ref_h * scale, 32)
    lw, lh = tw // 16, th // 16
    lt, used = latent_frames_for(min(ref_frames, target_frames))
    return lt * (lh // 2) * (lw // 2), lt, lh, lw, used


def plan_shape(height, width, num_frames, steps, text_len, refs, edges,
               tflops_nf4, attn_tflops, n_blocks=DIT_BLOCKS):
    """refs: list of ('image', w, h) | ('video', w, h, frames) | ('video_audio', w, h, frames)
             | ('audio', seconds)"""
    h, w, f, lt, lh, lw = align_shape(height, width, num_frames)
    at = round(f / 24.0 * AUDIO_LATENT_FPS)
    target_rows = lt * (lh // 2) * (lw // 2)
    audio_rows = at * AUDIO_CHANNELS

    img_rows = vid_rows = ref_audio_rows = 0
    img_dims, vid_dims = [], []
    for r in refs:
        if r[0] == "image":
            n, ih, iw = ref_image_rows(r[1], r[2], edges["image"])
            img_rows += n
            img_dims.append((r[1], r[2], ih, iw))
        elif r[0] in ("video", "video_audio"):
            n, lt_r, ih, iw, used = ref_video_rows(r[1], r[2], r[3], f,
                                                   edges["video"], edges["video_max_pixels"])
            vid_rows += n
            vid_dims.append((r[1], r[2], lt_r, ih, iw))
            if r[0] == "video_audio":
                ref_audio_rows += round(r[3] / 24.0 * AUDIO_LATENT_FPS) * AUDIO_CHANNELS
        elif r[0] == "audio":
            ref_audio_rows += round(r[1] * AUDIO_LATENT_FPS) * AUDIO_CHANNELS

    used = text_len + img_rows + vid_rows + ref_audio_rows + audio_rows + target_rows
    seq = -(-used // SEQ_ALIGN) * SEQ_ALIGN
    linear_s = 2.0 * seq * DIT_PARAMS_LINEAR / (tflops_nf4 * 1e12)
    attn_s = n_blocks * 4 * HEADS * seq * seq * HEAD_DIM / (attn_tflops * 1e12)
    step_s = linear_s + attn_s
    return {
        "requested": {"height": height, "width": width, "num_frames": num_frames, "steps": steps},
        "aligned": {"height": h, "width": w, "num_frames": f, "seconds": round(f / 24.0, 2),
                    "latent_t": lt, "latent_h": lh, "latent_w": lw, "audio_latent_t": at},
        "rows": {"text": text_len, "ref_image": img_rows, "ref_video": vid_rows,
                 "ref_audio": ref_audio_rows, "target_audio": audio_rows,
                 "target_video": target_rows, "used": used, "seq_len": seq},
        "ref_image_latents": [(a, b, c, d) for a, b, c, d in img_dims],
        "ref_video_latents": vid_dims,
        "linear_tflop_per_step": round(2.0 * seq * DIT_PARAMS_LINEAR / 1e12, 1),
        "linear_s_per_step": round(linear_s, 2),
        "attn_s_per_step": round(attn_s, 2),
        "attn_share": round(attn_s / step_s, 3),
        "step_s": round(step_s, 2),
        "steps": steps,
        "denoise_s": round(step_s * steps, 1),
        "denoise_min": round(step_s * steps / 60.0, 1),
        "wallclock_per_output_second": round(step_s * steps / (f / 24.0), 1),
    }


# --------------------------------------------------------------------------- presets
PRESETS = [
    # name,        h,   w, frames, steps, refs,                             img_edge, vid_edge
    ("draft",     384,  640,    73,   20, [("image", 1024, 1024)],             768, 384),
    ("preview",   480,  832,    73,   30, [("image", 1024, 1024)],            1024, 384),
    ("standard",  480,  832,   124,   30, [("image", 1024, 1024)],            1024, 512),
    ("quality",   576, 1024,   124,   40, [("image", 1024, 1024)],            1024, 512),
    ("text-only", 480,  832,   124,   30, [],                                    0,   0),
    ("video-edit", 480, 832,   124,   30, [("video_audio", 832, 480, 124)],      0, 384),
    ("max-native", 768, 1344,  124,   50, [("image", 1024, 1024)],            1024, 512),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default=os.path.join(WS, "cache", "bench.json"))
    ap.add_argument("--validate", default=os.path.join(WS, "cache", "validate.json"))
    ap.add_argument("--out", default=os.path.join(WS, "cache", "plan.json"))
    ap.add_argument("--text-len", type=int, default=1100)
    ap.add_argument("--ram-reserve", type=float, default=1.5)
    ap.add_argument("--activation-reserve", type=float, default=1.7)
    args = ap.parse_args()

    global DIT_PARAMS_LINEAR
    if os.path.exists(args.validate):
        _v = json.load(open(args.validate))
        _c = (_v.get("census") or {}).get("dit")
        if _c and _c.get("quantized_params"):
            DIT_PARAMS_LINEAR = float(_c["quantized_params"])
            print(f"[census] DiT linear params measured from the deployed checkpoint: "
                  f"{DIT_PARAMS_LINEAR/1e9:.3f} B "
                  f"({_c['packed_weight_bytes']/DIT_PARAMS_LINEAR:.4f} byte/param packed)")
    bench = json.load(open(args.bench)) if os.path.exists(args.bench) else {}
    if not bench:
        print(f"[warn] {args.bench} missing -- run h3_bench.py first")
        bench = {"gpu": {"vram_total_gb": 8.0, "vram_free_gb": 7.6, "vram_reserved_after_init_gb": 1.1},
                 "pcie": {}, "host_mem": {"MemTotal_gb": 15.3}}

    report = {"models": {}, "warnings": [], "errors": []}
    print("=" * 110)
    print("A. DEPLOYED MODEL FILES")
    print("   diffsynth picks the model class + quant config by hashing sorted 'key:shape' pairs,")
    print("   so a re-saved file that is not byte-identical in its key set will not load.")
    print("=" * 110)
    by_key = {}
    for key, (name, expect) in FILES.items():
        mf = inspect_file(key, name, expect)
        by_key[key] = mf
        report["models"][key] = mf.as_dict()
        if mf.note == "MISSING":
            (report["errors"] if expect else report["warnings"]).append(f"{name} is missing")
            print(f"  [MISSING] {key:13s} {name}")
            continue
        flag = "OK " if mf.recognized else ("n/a" if not expect else "!!!")
        print(f"  [{flag}] {key:13s} {human(mf.size_bytes):>12s}  {mf.tensor_count:5d} tensors  {name}")
        print(f"         hash {mf.hash}   ({mf.note})")
        if mf.recognized:
            print(f"         -> {mf.matched_model_name}   class={mf.matched_model_class.split('.')[-1]}")
            if mf.matched_quant:
                q = dict(mf.matched_quant); ex = q.pop("exclude_modules", None)
                print(f"            quant={q}" + (f"  exclude_modules={ex}" if ex else ""))
        elif expect:
            report["errors"].append(f"{name}: hash {mf.hash} matches no MODEL_CONFIGS entry")
            print("         -> NOT RECOGNIZED (loading will raise 'Cannot detect the model type')")
        else:
            print("         -> user LoRA, not a registered model file (expected)")
        print("         dtypes: " + ", ".join(f"{k}={human(v)}" for k, v in
                                             sorted(mf.dtype_bytes.items(), key=lambda kv: -kv[1])[:4]))
        print("         top-level: " + ", ".join(f"{k}={human(v)}" for k, v in
                                                 list(mf.top_level_bytes.items())[:6]))

    dit_gb = gb(by_key["dit"].size_bytes)
    text_gb = gb(by_key["text_encoder"].size_bytes)
    vae_gb = gb(by_key["video_vae"].size_bytes + by_key["audio_vae"].size_bytes)
    lora_gb = gb(by_key["lora"].size_bytes / 2)
    report["sizes_gib"] = {"dit": dit_gb, "text_encoder": text_gb, "vaes": vae_gb, "lora_bf16": lora_gb}

    g, hm = bench.get("gpu", {}), bench.get("host_mem", {})
    gr, nr = bench.get("gemm", []), bench.get("nf4", [])
    tflops_nf4 = max((x["tflops"] for x in nr), default=40.7)
    tflops_dense = max((x["tflops"] for x in gr), default=43.6)
    sd = bench.get("sdpa_real_layout", {})
    pts = [(int(s), v["CUDNN_ATTENTION"]) for s, v in sd.items()
           if isinstance(v.get("CUDNN_ATTENTION"), (int, float))]
    attn_tflops = (4 * HEADS * pts[0][0] ** 2 * HEAD_DIM / (pts[0][1] / 1e3) / 1e12) if pts else 43.6
    sc = bench.get("stream_cost", [])
    # packed_mb is MiB and ms is milliseconds, so this ratio is GiB/s.  That is
    # deliberate and load-bearing: the weight volume it divides (streamed, from
    # gb()) is also GiB, so streamed / stream_gibs is seconds.  stream_gbs is the
    # identical throughput expressed in decimal GB/s, for reporting.
    stream_gibs = ((sum(c["packed_mb"] for c in sc) / 1024) / (sum(c["ms"] for c in sc) / 1e3)) if sc else 4.0
    stream_gbs = stream_gibs * (1024 ** 3) / 1e9

    print()
    print("=" * 110)
    print("B. MEASURED CEILINGS ON THIS BOX")
    print("=" * 110)
    print(f"  {'architecture':30s} DiT {DIT_PARAMS_LINEAR/1e9:.2f} B linear params "
          f"({DIT_BLOCKS} blocks + {REFINER_BLOCKS} refiner) x {DIT_BLOCK_PARAMS/1e6:.0f} M/block")
    for k, v in [
        ("GPU", f"{g.get('name')}  sm_{str(g.get('capability','?')).replace('.','')}  {g.get('sm_count')} SMs"
                f"  {g.get('vram_total_gb')} GiB VRAM"),
        ("VRAM free at start", f"{g.get('vram_free_gb')} GiB (context+desktop hold "
                               f"{g.get('vram_reserved_after_init_gb')} GiB)"),
        ("host RAM", f"{hm.get('MemTotal_gb')} GiB total / {hm.get('MemAvailable_gb')} GiB free, "
                     f"swap {hm.get('SwapTotal_gb')} GiB"),
        ("dense bf16 GEMM", f"{tflops_dense:.1f} TFLOP/s"),
        ("NF4 bnb GEMM", f"{tflops_nf4:.1f} TFLOP/s ({100*tflops_nf4/tflops_dense:.0f}% of dense)"),
        ("SDPA attention", f"{attn_tflops:.1f} TFLOP/s (cuDNN, real [1,H,S,D] view layout)"),
        ("H2D pageable", f"{bench.get('pcie',{}).get('h2d_pageable_gbs')} GB/s  <- module.to('cuda')"),
        ("H2D pinned", f"{bench.get('pcie',{}).get('h2d_pinned_gbs')} GB/s (7.7x, unused lever)"),
        ("layer stream (deepcopy+cuda)", f"{stream_gibs:.2f} GiB/s = {stream_gbs:.2f} GB/s effective"),
        ("weights volume", f"{bench.get('disk',{}).get('cold_read_gbs')} GB/s sequential read"),
        ("attention backend", f"{bench.get('attention_implementation')} "
                              f"(no flash_attn / sageattention / xformers installed)"),
    ]:
        print(f"  {k:30s} {v}")
    report["ceilings"] = {"tflops_nf4": tflops_nf4, "attn_tflops": round(attn_tflops, 1),
                          "stream_gibs": round(stream_gibs, 2),
                          "stream_gbs": round(stream_gbs, 2),
                          "dit_params_linear": DIT_PARAMS_LINEAR}

    print()
    print("=" * 110)
    print("C. WHAT A REQUEST COSTS   (text_len=%d, from the real tokenizer)" % args.text_len)
    print("=" * 110)
    print(f"  linear : 2 * seq * {DIT_PARAMS_LINEAR/1e9:.2f}e9 / {tflops_nf4:.1f} TFLOP/s"
          f"      (50 dense blocks, every token through every weight)")
    print(f"  attn   : 50 * 4*56*seq^2*128 / {attn_tflops:.1f} TFLOP/s"
          f"   (one FULL non-causal attention per block -- quadratic in seq)")
    print()
    print(f"  {'preset':12s} {'aligned':>15s} {'seq':>7s} {'lin TFLOP':>10s} {'lin s':>7s}"
          f" {'attn s':>7s} {'attn%':>6s} {'s/step':>7s} {'steps':>6s} {'denoise':>9s} {'xRT':>7s}")
    presets = {}
    for name, h, w, f, steps, refs, iedge, vedge in PRESETS:
        edges = {"image": iedge or 1024, "video": vedge or 384,
                 "video_max_pixels": (vedge or 384) * 672}
        b = plan_shape(h, w, f, steps, args.text_len, refs, edges, tflops_nf4, attn_tflops)
        b.update(note=name, refs=[list(r) for r in refs], ref_image_short_edge=edges["image"],
                 ref_video_short_edge=edges["video"], ref_video_max_pixels=edges["video_max_pixels"])
        presets[name] = b
        a = b["aligned"]
        print(f"  {name:12s} {a['width']:5d}x{a['height']:<5d}x{a['num_frames']:<4d}"
              f" {b['rows']['seq_len']:7d} {b['linear_tflop_per_step']:10.1f}"
              f" {b['linear_s_per_step']:7.2f} {b['attn_s_per_step']:7.2f}"
              f" {100*b['attn_share']:5.0f}% {b['step_s']:7.2f} {steps:6d}"
              f" {b['denoise_min']:7.1f} m {b['wallclock_per_output_second']:6.1f}x")
    report["presets"] = presets

    # ---- cross-check against the framework's own sequence builder -----------
    if os.path.exists(args.validate):
        v = json.load(open(args.validate))
        packed = v.get("packed") or []
        print()
        print("  cross-check vs MiniMaxH3Unit_PackedSequenceBuilder.run on dummy latents:")
        print(f"    {'shape':>15s} {'model seq':>10s} {'framework seq':>14s} {'delta':>7s}")
        ok = True
        for row in packed:
            w2, h2, f2 = (int(x) for x in row["shape"].replace("x", " ").split())
            edge = row.get("ref_image_short_edge", 1024)
            m = plan_shape(h2, w2, f2, 30, row["text"], [("image", 1024, 1024)],
                           {"image": edge, "video": 512, "video_max_pixels": 512 * 672},
                           tflops_nf4, attn_tflops)
            delta = m["rows"]["seq_len"] - row["seq_len"]
            ok &= delta == 0
            print(f"    {row['shape']:>15s} {m['rows']['seq_len']:10d} {row['seq_len']:14d} {delta:7d}")
        if not ok:
            report["errors"].append("planner seq_len does not match the framework's sequence builder")
            print("    !! MISMATCH -- the time model above is not trustworthy")
        else:
            print("    OK: every shape matches the framework exactly")
    else:
        print("\n  [note] run h3_validate.py first to cross-check these numbers against the framework")

    print()
    print("  rows of the 'standard' preset:")
    rb = presets["standard"]["rows"]
    for k, v in rb.items():
        print(f"    {k:14s} {v:7d}  {100.0*v/max(rb['used'],1):5.1f}%")

    print()
    print("  reference-image cost for a 1024x1024 image (480x832 output, seq %d):"
          % presets["standard"]["rows"]["seq_len"])
    base_seq = presets["standard"]["rows"]["seq_len"]
    for edge in (2048, 1536, 1024, 768, 512):
        r, lh, lw = ref_image_rows(1024, 1024, edge)
        print(f"    short_edge={edge:5d} -> latent {lh:3d}x{lw:<3d} = {r:5d} rows,"
              f" +{r/base_seq*100:5.1f}% seq, +{(1+r/base_seq)**2*100-100:5.1f}% attention")

    print()
    print("  reference-video cost (832x480 source, same length as a 124-frame target):")
    for edge in (768, 512, 384, 256):
        r, lt, lh, lw, used = ref_video_rows(832, 480, 124, 124, edge, edge * 672)
        seq = 1100 + r + 414 + 14430
        print(f"    short_edge={edge:5d} -> {lt:2d} latent frames of {lh:3d}x{lw:<3d}"
              f" = {r:6d} rows -> seq ~{seq:6d} ({seq/16960:.2f}x the image-only case)")

    print()
    print("=" * 110)
    print("D. MEMORY PLAN")
    print("=" * 110)
    total_vram = g.get("vram_total_gb", 7.93)
    free_vram = g.get("vram_free_gb", 6.84)
    ctx = g.get("vram_reserved_after_init_gb", 1.1)
    lora_on_gpu = lora_gb
    res = args.activation_reserve + (lora_on_gpu if lora_gb else 0.0)
    vram_limit = round(free_vram - res, 2)
    resident = max(0.0, vram_limit - ctx)
    streamed = max(0.0, dit_gb - resident)
    stream_s = streamed / stream_gibs
    std_step = presets["standard"]["step_s"]
    print(f"  activation reserve {res:.2f} GiB ({args.activation_reserve} for the biggest burst --")
    print(f"    mlp.fc1 output is seq x 28672 x 2 B = {16960*28672*2/1024**3:.2f} GiB at the standard shape --")
    print(f"    plus {lora_on_gpu:.2f} GiB if the LoRA is attached)")
    print(f"  vram_limit = free_at_start({free_vram}) - reserve({res:.2f}) = {vram_limit} GiB")
    print(f"    compared against TOTAL USED device memory, so it parks ~{resident:.2f} GiB of the"
          f" {dit_gb} GiB NF4 DiT on the GPU")
    print(f"    the other {streamed:.2f} GiB is deepcopy+to('cuda')'d every step: {stream_s:.2f} s/step"
          f" = {100*stream_s/std_step:.1f}% of a standard step")
    report["memory"] = {"vram_limit_gib": vram_limit, "dit_resident_gib": round(resident, 2),
                        "dit_streamed_per_step_gib": round(streamed, 2),
                        "stream_cost_s_per_step": round(stream_s, 2)}
    print()
    print("  (streamed volumes are GiB, throughput is GiB/s -- the ratio below is seconds)")
    print(f"  {'vram_limit':>10s} {'resident':>9s} {'streamed':>9s} {'s/step':>8s} {'headroom':>9s}")
    for lim in (4.0, 4.9, 5.5, 6.0, 6.5, 7.0):
        r_ = max(0.0, lim - ctx); s_ = max(0.0, dit_gb - r_)
        print(f"  {lim:10.2f} {r_:9.2f} {s_:9.2f} {s_/stream_gibs:8.2f} {total_vram-lim:9.2f}")

    print()
    print("  RAM per phase (peak resident model bytes):")
    print(f"    VAE encode/decode : {vae_gb:5.2f} GiB  (video+audio VAE, onload cpu)")
    print(f"    prompt embed      : 0.00 GiB  text encoder is {text_gb:.2f} GiB -> MUST stream"
          " (onload_device='disk')")
    print(f"    denoise           : {dit_gb:5.2f} GiB  (DiT, onload cpu) + torch/cuda ~1.2 + python ~0.5"
          f" = ~{dit_gb+1.7:.2f} of {hm.get('MemTotal_gb')} GiB")
    if dit_gb + 1.7 > hm.get("MemTotal_gb", 15.3) - args.ram_reserve:
        report["warnings"].append(f"DiT on CPU leaves <{args.ram_reserve} GiB RAM headroom; "
                                  "use --dit-onload disk for the DiT")
        print(f"    [warn] headroom below {args.ram_reserve} GiB -- prefer --dit-onload disk")

    print()
    print("=" * 110)
    print("E. RESOLVED SETTINGS  (written to plan.json; h3_generate.py reads them as defaults)")
    print("=" * 110)
    rec = {
        "vram_limit_gib": vram_limit,
        "attention": {"implementation": "torch", "force_sdpa_backend": "cudnn",
                      "reason": f"cuDNN SDPA measured fastest ({attn_tflops:.1f} TFLOP/s); "
                                "flash_attn / sageattention / xformers are not installed"},
        "guidance": {"cfg_scale": 1.0,
                     "reason": "cfg_scale=1.0 skips the negative pass entirely -> halves per-step "
                               "cost. Use the text-side levers instead of CFG."},
        "denoise": {"steps": 30, "flow_shift": 12.0, "audio_flow_shift": 3.0},
        "sampler": {
            "name": "euler",
            "note": "FlowMatchScheduler.step() is already a first-order Euler update "
                    "(prev = sample + model_output * (sigma_next - sigma)); nothing to change",
        },
        "scheduler": {
            "default": "auto",
            "auto_rule": "beta when a LoRA is attached, flow otherwise",
            "beta": {"alpha": 0.6, "beta": 0.6,
                     "source": "comfy/samplers.py beta_scheduler, indexed into "
                               "ModelSamplingDiscreteFlow.sigmas"},
            "why": "the AfterMidnight Ref2VA LoRA requires 'euler sampler + beta scheduler' "
                   "or the audio degrades. At 30 steps / shift 12 the final grid sigma is "
                   "0.2927 with flow vs 0.0780 with beta, i.e. beta spends far more of the "
                   "trajectory at low sigma where audio detail is resolved.",
            "module": "scripts/h3_scheduler.py",
        },
        "vae": {"tiled": True, "tile_size": 256, "tile_overlap": 64},
        "env": {"PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                "OMP_NUM_THREADS": "8",
                "DIFFSYNTH_ATTENTION_IMPLEMENTATION": "torch"},
        "default_preset": "standard",
        # INVARIANT (enforced by h3_generate.build_pipeline): onload_dtype may only be
        # the string "disk" when onload_device is also "disk".  AutoWrappedLinear.
        # onload() hands onload_dtype straight to Tensor.to(dtype=...), so
        # ("disk", "cpu") raises TypeError on the first non-quantized Linear.
        "vram_config": {
            "dit": {"offload_dtype": "disk", "offload_device": "disk",
                    "onload_dtype": "bfloat16", "onload_device": "cpu",
                    "preparing_dtype": "bfloat16", "preparing_device": "cuda",
                    "computation_dtype": "bfloat16", "computation_device": "cuda"},
            "text_encoder": {"offload_dtype": "disk", "offload_device": "disk",
                             "onload_dtype": "disk", "onload_device": "disk",
                             "preparing_dtype": "bfloat16", "preparing_device": "cuda",
                             "computation_dtype": "bfloat16", "computation_device": "cuda"},
            "video_vae": {"offload_dtype": "disk", "offload_device": "disk",
                          "onload_dtype": "bfloat16", "onload_device": "cpu",
                          "preparing_dtype": "bfloat16", "preparing_device": "cuda",
                          "computation_dtype": "bfloat16", "computation_device": "cuda"},
            "audio_vae": {"offload_dtype": "disk", "offload_device": "disk",
                          "onload_dtype": "bfloat16", "onload_device": "cpu",
                          "preparing_dtype": "bfloat16", "preparing_device": "cuda",
                          "computation_dtype": "bfloat16", "computation_device": "cuda"},
        },
    }
    report["resolved"] = rec
    print(json.dumps(rec, indent=4))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nplan written to {args.out}")
    for tag in ("warnings", "errors"):
        if report[tag]:
            print(f"\n{tag.upper()}:")
            for w in report[tag]:
                print("  -", w)


if __name__ == "__main__":
    main()
