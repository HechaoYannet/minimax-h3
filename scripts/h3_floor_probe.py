#!/usr/bin/env python3
"""h3_floor_probe.py -- split one DiT step into "staging" and "compute".

Short clips sit on a fixed per-step floor: at 640x384x22f the step costs 7.7 s
even though the whole forward is only ~3.1 TFLOP (about 2 s at the measured
44 TFLOP/s).  All 264 NF4 Linears are re-staged on every step, so the question
is how much of the step that staging actually is.

Method: run the real pipeline but interpose on pipe.model_fn, which receives the
exact packed inputs the scheduler built.  Time it once normally, then again with
AutoTorchModule.check_free_vram forced False.  With the gate closed no layer is
ever promoted to "preparing", so computation_module() takes the transient branch
instead of self.module -- same weights, without the promotion.  The difference is
the staging cost.  (Both runs are numerically useless; only the clock matters.)

    python scripts/h3_floor_probe.py --num-frames 22 --width 640 --height 384
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import h3_compat  # noqa: E402
h3_compat.apply()

import torch  # noqa: E402
import h3_generate as G  # noqa: E402

WS = os.path.dirname(HERE)
PINK = os.path.join(WS, "workspace/pink-harem-ref2va")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--num-frames", type=int, default=22)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=384)
    ap.add_argument("--img-edge", type=int, default=256)
    ap.add_argument("--vram-limit", type=float, default=None)
    ap.add_argument("--dit-onload", default="cpu")
    args = ap.parse_args()

    plan = G.load_plan()
    resolved = plan.get("resolved", {})
    vlim = args.vram_limit if args.vram_limit is not None else resolved.get("vram_limit_gib", 5.0)
    refs = G.build_references([("image", os.path.join(PINK, "materials/ref-pink-harem.jpg"))],
                              args.height, args.width, args.num_frames)
    prompt = open(os.path.join(PINK, "prompt.txt"), encoding="utf-8").read().strip()
    edges = {"ref_image_short_edge": args.img_edge, "ref_video_short_edge": 384,
             "ref_video_max_pixels": 384 * 672}

    pipe = G.build_pipeline(plan, ["dit", "video_vae", "audio_vae", "processor", "text_encoder"],
                            vlim, dit_onload=args.dit_onload)
    import glob
    lora = glob.glob(os.path.expanduser(
        "~/source/minimax-h3/models/AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors"))
    if lora:
        G.load_lora(pipe, lora[0])
    embeds, tags = G.compute_text_embedding(pipe, prompt, refs, args.height, args.width,
                                            args.num_frames, edges)
    G.install_cached_prompt_embedder(pipe, embeds, tags)
    import h3_scheduler
    h3_scheduler.install(pipe, mode="beta", alpha=0.6, beta=0.6)

    real_fn = pipe.model_fn
    from diffsynth.core.vram.layers import AutoTorchModule
    orig_gate = AutoTorchModule.check_free_vram
    captured = {}
    timings = {}

    def measured(dit, **kw):
        captured["kw"] = kw
        return real_fn(dit, **kw)

    def do():
        torch.cuda.synchronize()
        t = time.perf_counter()
        with torch.no_grad():
            real_fn(pipe.dit, **captured["kw"])
        torch.cuda.synchronize()
        return time.perf_counter() - t

    pipe.model_fn = measured

    def probe(iterable):
        it = list(iterable)
        yield it[0]                       # let the pipeline build its packed state

        timings["staged"] = do()
        AutoTorchModule.check_free_vram = lambda self: False
        timings["unstaged"] = do()
        AutoTorchModule.check_free_vram = orig_gate
        timings["staged2"] = do()

        n_prep = sum(1 for m in pipe.dit.modules()
                     if isinstance(m, AutoTorchModule) and m.state == 2)
        n_all = sum(1 for m in pipe.dit.modules() if isinstance(m, AutoTorchModule))
        vl = captured["kw"].get("video_latents")
        print(f"\n[floor] video_latents {tuple(vl.shape) if vl is not None else '?'}  "
              f"wrapped {n_all}, resident(state 2) {n_prep}", flush=True)
        print(f"[floor] staged    {timings['staged']:.2f} s", flush=True)
        print(f"[floor] unstaged  {timings['unstaged']:.2f} s", flush=True)
        print(f"[floor] staged#2  {timings['staged2']:.2f} s", flush=True)
        print(f"[floor] => promotion/staging costs about "
              f"{timings['staged'] - timings['unstaged']:.2f} s/step", flush=True)
        raise SystemExit(0)

    pipe(prompt=None, text_embedding=None, height=args.height, width=args.width,
         num_frames=args.num_frames, num_inference_steps=2, seed=42, cfg_scale=1.0,
         flow_shift=12.0, audio_flow_shift=3.0, references=refs, tiled=True,
         tile_size=256, tile_overlap=64, progress_bar_cmd=probe, **edges)


if __name__ == "__main__":
    main()
