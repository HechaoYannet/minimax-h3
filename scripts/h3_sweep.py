#!/usr/bin/env python3
"""h3_sweep.py -- run a labelled matrix of h3_generate configurations.

Each configuration is a real full pipeline call (reference encode -> cached
prompt embedding -> denoise -> VAE decode) that emits exactly one
'BENCH {json}' line, so every number here is measured end to end, not
extrapolated.  Videos are not written; the point is the timings.

    python scripts/h3_sweep.py speed            # the min-latency frontier
    python scripts/h3_sweep.py capability       # the max-quality frontier
    python scripts/h3_sweep.py --list

Results go to cache/sweep/<name>.json and are appended to a single log.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.dirname(HERE)
H3_ROOT = os.environ.get("H3_ROOT", "/home/yhc/source/minimax-h3")
MODELS = os.environ.get("H3_MODELS", os.path.join(H3_ROOT, "models"))

PROMPT = os.path.join(WS, "workspace/pink-harem-ref2va/prompt.txt")
REF_IMAGE = os.path.join(WS, "workspace/pink-harem-ref2va/materials/ref-pink-harem.jpg")
LORA = os.path.join(MODELS, "AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors")
OUT = os.path.join(WS, "cache", "sweep")


def cfg(label, frames, steps, width=640, height=384, img_edge=768, dit_onload=None, **kw):
    """One point on the frontier.  width/height are the GENERATION shape.

    dit_onload="cpu" materialises the whole 9.76 GiB DiT in host RAM.  Combined
    with a cold text-encoder run (14.27 GiB of checkpoint mmap) that reaches the
    22.9 GiB WSL ceiling and the OOM killer takes the whole distro down -- which
    is what happened to this box once.  "disk" keeps the DiT mmap-only."""
    d = dict(label=label, num_frames=frames, steps=steps, width=width, height=height,
             ref_image_short_edge=img_edge, dit_onload=dit_onload)
    d.update(kw)
    return d


# --------------------------------------------------------------------------- speed
# How fast can one clip be?  Everything here keeps the draft's 640x384 canvas and
# the same reference image, and varies only what the user would trade away.
SPEED = [
    # frame count: the 17n+5 grid, 8 steps each.  22f = 0.92 s of video.
    cfg("f22_s08", 22, 8),
    cfg("f39_s08", 39, 8),
    cfg("f56_s08", 56, 8),
    cfg("f73_s08", 73, 8),
    # step count at the shortest clip
    cfg("f22_s04", 22, 4),
    cfg("f22_s12", 22, 12),
    cfg("f22_s20", 22, 20),
    # canvas at a fixed short clip
    cfg("f39_s08_512x288", 39, 8, width=512, height=288),
    cfg("f39_s08_448x256", 39, 8, width=448, height=256),
    cfg("f39_s08_384x224", 39, 8, width=384, height=224),
    # the reference image is a big slice of a short clip's sequence
    cfg("f22_s08_img512", 22, 8, img_edge=512),
    cfg("f22_s08_img256", 22, 8, img_edge=256),
    cfg("f39_s08_img512", 39, 8, img_edge=512),
    cfg("f39_s08_img1536", 39, 8, img_edge=1536),
    # the current draft, for reference
    cfg("f73_s20", 73, 20),
]

# --------------------------------------------------------------------------- capability
# What is the ceiling?  Long clips, high resolution, many steps, and the
# reference modalities the workflow has never exercised.
# Ordered so the ladder grows one axis at a time and every step is a config the
# previous one already proved.  A config that exceeds the box takes the whole WSL
# distro down, so jumping straight to 832x480x124 loses the diagnosis; growing
# into it keeps the last good point as a reference.
CAPABILITY = [
    # Everything from here on runs the DiT from mmap (--dit-onload disk): the
    # materialised-on-CPU variant plus a cold encoder pass is what took the box
    # down, and the measured speed difference is small.
    #
    # canvas, at a duration the draft preset already proved
    cfg("can_832x480_73_s20", 73, 20, width=832, height=480, img_edge=1024, dit_onload="disk"),
    cfg("can_960x544_73_s20", 73, 20, width=960, height=544, img_edge=1024, dit_onload="disk"),
    cfg("can_1024x576_73_s20", 73, 20, width=1024, height=576, img_edge=1024, dit_onload="disk"),
    # the delivery shape
    cfg("std_832x480_124_s30", 124, 30, width=832, height=480, img_edge=1024, dit_onload="disk"),
    # duration, at the resolution the draft preset proved
    cfg("dur_640x384_243_s20", 243, 20, dit_onload="disk"),
    cfg("dur_640x384_328_s20", 328, 20, dit_onload="disk"),
    # and beyond
    cfg("cap_832x480_243_s30", 243, 30, width=832, height=480, img_edge=1024, dit_onload="disk"),
    cfg("cap_1024x576_124_s30", 124, 30, width=1024, height=576, img_edge=1024, dit_onload="disk"),
    cfg("cap_1344x768_124_s30", 124, 30, width=1344, height=768, img_edge=1024, dit_onload="disk"),
]


SUITES = {"speed": SPEED, "capability": CAPABILITY}


def run_one(c: dict, log_fh, timeout=14400) -> dict:
    cmd = [os.path.join(WS, "run_h3.sh"), "gen",
           "--prompt-file", PROMPT,
           "--ref-image", REF_IMAGE,
           "--lora", LORA,
           "--height", str(c["height"]), "--width", str(c["width"]),
           "--num-frames", str(c["num_frames"]), "--steps", str(c["steps"]),
           "--ref-image-short-edge", str(c["ref_image_short_edge"]),
           "--bench", "--label", c["label"]]
    if c.get("dit_onload"):
        cmd += ["--dit-onload", c["dit_onload"]]
    print(f"\n===== {c['label']}: {c['width']}x{c['height']}x{c['num_frames']}f "
          f"{c['steps']} steps, img_edge {c['ref_image_short_edge']} =====", flush=True)
    t0 = time.perf_counter()

    # A configuration that exceeds the box can take the whole WSL distro down,
    # not just this process, which destroys any traceback.  Sample free RAM and
    # VRAM from the parent while the child runs so the last line of the watch log
    # says how close to the edge the failing config got.
    watch = os.path.join(OUT, "watch.log")
    wf = open(watch, "a")
    wf.write(f"\n##### {c['label']} started {time.strftime('%H:%M:%S')}\n")
    wf.flush()
    stop = threading.Event()

    def rss_tree(pid):
        """RSS of the child plus every descendant: the runner forks run_h3.sh ->
        bash -> python, so the interesting process is a grandchild."""
        total, stack = 0, [pid]
        while stack:
            p = stack.pop()
            try:
                with open(f"/proc/{p}/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            total += int(line.split()[1])
                            break
                with open(f"/proc/{p}/task/{p}/children") as f:
                    stack.extend(int(x) for x in f.read().split())
            except Exception:
                pass
        return total

    def watch_mem():
        while not stop.wait(3.0):
            try:
                rss = rss_tree(proc.pid)
                mem = subprocess.run(["free", "-m"], capture_output=True,
                                     text=True).stdout.splitlines()[1].split()
                gpu = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used,memory.free",
                     "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
                wf.write(f"  {time.strftime('%H:%M:%S')} rss {rss//1024:5d} MiB  "
                         f"ram used/free/avail {mem[2]}/{mem[3]}/{mem[6]} MiB  "
                         f"cache {mem[5]} MiB  gpu {gpu}\n")
                wf.flush()
            except Exception:
                return

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, cwd=WS)
    th = threading.Thread(target=watch_mem, daemon=True)
    th.start()
    try:
        out, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        rc = -9
    finally:
        stop.set()
        wf.write(f"  exit {rc}\n")
        wf.flush()
        wf.close()
    proc = type("P", (), {"stdout": out, "stderr": "", "returncode": rc})()
    wall = time.perf_counter() - t0
    log_fh.write(f"\n##### {c['label']} ({wall:.0f} s wall)\n")
    log_fh.write(proc.stdout[-20000:])
    log_fh.write(proc.stderr[-20000:])
    log_fh.flush()

    rec = {"config": c, "wall_s": round(wall, 1), "returncode": proc.returncode}
    for line in proc.stdout.splitlines():
        if line.startswith("BENCH "):
            rec["bench"] = json.loads(line[6:])
            break
    if "bench" not in rec:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-6:]
        rec["error"] = " | ".join(tail)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("suite", nargs="?", choices=sorted(SUITES))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", default=None, help="comma-separated labels to run")
    args = ap.parse_args()

    if args.list:
        for name, suite in SUITES.items():
            print(f"{name}: " + ", ".join(c["label"] for c in suite))
        return 0
    if not args.suite:
        ap.error("pick a suite or --list")

    suite = SUITES[args.suite]
    if args.only:
        want = set(args.only.split(","))
        suite = [c for c in suite if c["label"] in want]

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, args.suite + ".json")
    results = []
    if os.path.exists(path):
        results = json.load(open(path))
        done = {r["config"]["label"] for r in results}
        suite = [c for c in suite if c["label"] not in done]

    with open(os.path.join(OUT, args.suite + ".log"), "a", buffering=1) as fh:
        for c in suite:
            try:
                rec = run_one(c, fh)
            except subprocess.TimeoutExpired:
                rec = {"config": c, "error": "timeout"}
            results.append(rec)
            json.dump(results, open(path, "w"), indent=2)
            b = rec.get("bench")
            if b:
                print(f"  -> {b['step_s_last']} s/step (stable), "
                      f"{b['denoise_s']} s denoise, {b['total_s']} s pipeline, "
                      f"seq {b.get('seq_len_predicted')}, peak {b['peak_vram_gib']} GiB",
                      flush=True)
            else:
                print(f"  -> FAILED: {rec.get('error', '?')}", flush=True)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
