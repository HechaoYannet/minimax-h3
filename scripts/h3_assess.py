#!/usr/bin/env python3
"""h3_assess.py -- objective quality metrics for a generated clip.

Taste cannot be measured from a script, but several failure modes that matter for
choosing a step count CAN be:

  * a frozen clip (the model gave up and repeated one frame)
  * a clip that dissolves into noise at the end (too few steps)
  * a clip with no motion at all
  * silent or clipped audio

Frame-to-frame difference is the useful signal: if the last few Euler steps are
still changing the picture, the trajectory was truncated; if consecutive frames
are identical, temporal detail collapsed.

    python scripts/h3_assess.py clip.mp4 clip2.mp4 ...
"""
from __future__ import annotations

import json
import os
import sys

import av
import numpy as np


def load(path):
    # Two containers on purpose.  A single PyAV container is one demuxer: after
    # decode(video) has walked the file, decode(audio) finds no packets left and
    # silently yields nothing, which reports every clip as silent.
    with av.open(path) as cv:
        frames = [f.to_ndarray(format="rgb24") for f in cv.decode(cv.streams.video[0])]
    audio = []
    with av.open(path) as ca:
        for f in ca.decode(audio=0):
            a = np.asarray(f.to_ndarray(), dtype=np.float32)
            # (channels, samples) for planar formats -> mono
            audio.append(a if a.ndim == 1 else a.mean(axis=0))
    w = np.concatenate(audio) if audio else np.zeros(1, dtype=np.float32)
    return np.stack(frames), w


def assess(path):
    frames, w = load(path)
    f = frames.astype(np.float32) / 255.0
    d = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2, 3))          # per-frame-pair MAE
    n = len(f)
    q = max(1, n // 4)
    rec = {
        "path": os.path.basename(path),
        "frames": int(n),
        "seconds": round(n / 24.0, 2),
        "size": f"{frames.shape[2]}x{frames.shape[1]}",
        "diff_mean": round(float(d.mean()), 5),
        "diff_p10": round(float(np.percentile(d, 10)), 5),
        "diff_p90": round(float(np.percentile(d, 90)), 5),
        "diff_first_q": round(float(d[:q].mean()), 5),
        "diff_last_q": round(float(d[-q:].mean()), 5),
        "frozen_frames": int((d < 1e-5).sum()),
        "audio_rms": round(float(np.sqrt((w ** 2).mean())), 5),
        "audio_peak": round(float(np.abs(w).max()), 5),
        "audio_clipped_frac": round(float((np.abs(w) > 0.99).mean()), 5),
        "audio_nonfinite": int((~np.isfinite(w)).sum()),
    }
    # A clip whose last quarter moves much less than its first is either
    # converging (fine) or frozen (not fine); the frozen_frames count separates
    # those two, so report both and let the caller judge.
    rec["motion_decay"] = (round(rec["diff_last_q"] / rec["diff_first_q"], 3)
                           if rec["diff_first_q"] > 0 else None)
    return rec


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    out = [assess(p) for p in sys.argv[1:]]
    for r in out:
        print(json.dumps(r, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
