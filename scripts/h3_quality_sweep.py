#!/usr/bin/env python3
"""h3_quality_sweep.py -- does the step count (or the reference edge) actually matter?

The speed sweep says what things cost.  This says what you lose by paying less.
It generates the same seed at several step counts and reference-image short
edges, writes real mp4s, and assesses each with h3_assess.py.

    python scripts/h3_quality_sweep.py steps      # 4 / 6 / 8 / 12 / 20 / 40
    python scripts/h3_quality_sweep.py edges      # img_edge 256 / 512 / 768 / 1024 / 1536
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
H3_ROOT = os.environ.get("H3_ROOT", "/home/yhc/source/minimax-h3")
MODELS = os.environ.get("H3_MODELS", os.path.join(H3_ROOT, "models"))
PINK = os.path.join(WS, "workspace/pink-harem-ref2va")
OUTDIR = os.path.join(PINK, "quality")
OUT = os.path.join(WS, "cache", "sweep")

PROMPT = os.path.join(PINK, "prompt.txt")
REF = os.path.join(PINK, "materials/ref-pink-harem.jpg")
LORA = os.path.join(MODELS, "AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors")

STEPS = [("s04", 4), ("s06", 6), ("s08", 8), ("s12", 12), ("s20", 20), ("s40", 40)]
EDGES = [("e256", 256), ("e512", 512), ("e768", 768), ("e1024", 1024), ("e1536", 1536)]

FIXED = dict(height=384, width=640, num_frames=73, seed=42, img_edge=768)


def generate(label, steps, img_edge, out_path, timeout=5400):
    cmd = [os.path.join(WS, "run_h3.sh"), "gen",
           "--prompt-file", PROMPT, "--ref-image", REF, "--lora", LORA,
           "--height", str(FIXED["height"]), "--width", str(FIXED["width"]),
           "--num-frames", str(FIXED["num_frames"]), "--steps", str(steps),
           "--seed", str(FIXED["seed"]),
           "--ref-image-short-edge", str(img_edge),
           "--out", out_path]
    t0 = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=WS)
    return {"label": label, "steps": steps, "img_edge": img_edge,
            "out": out_path, "wall_s": round(time.perf_counter() - t0, 1),
            "returncode": p.returncode,
            "log": (p.stdout or "")[-4000:] + (p.stderr or "")[-4000:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("axis", choices=["steps", "edges"])
    args = ap.parse_args()
    os.makedirs(OUTDIR, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)

    recs = []
    path = os.path.join(OUT, f"quality_{args.axis}.json")

    for label, value in (STEPS if args.axis == "steps" else EDGES):
        # On the steps axis the edge is fixed; on the edges axis the step count is.
        # 12 steps is the reference point: enough that the trajectory has converged,
        # cheap enough to run five of them.
        steps = value if args.axis == "steps" else 12
        img_edge = value if args.axis == "edges" else FIXED["img_edge"]
        out = os.path.join(OUTDIR, f"{args.axis}_{label}.mp4")
        print(f"\n===== {args.axis} {label} (steps={steps}, img_edge={img_edge}) =====",
              flush=True)
        rec = generate(label, steps, img_edge, out)
        if rec["returncode"] == 0 and os.path.exists(out):
            a = subprocess.run([sys.executable, os.path.join(HERE, "h3_assess.py"), out],
                               capture_output=True, text=True)
            try:
                rec["assess"] = json.loads(a.stdout.strip().splitlines()[-1])
            except Exception:
                rec["assess"] = {"error": a.stdout[-300:] + a.stderr[-300:]}
            print("  " + json.dumps(rec["assess"], sort_keys=True), flush=True)
        else:
            rec["error"] = rec.pop("log", "")[-600:]
            print("  FAILED: " + rec["error"][-200:], flush=True)
        recs.append(rec)
        json.dump(recs, open(path, "w"), indent=2)

    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
