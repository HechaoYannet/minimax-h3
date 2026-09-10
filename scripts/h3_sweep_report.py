#!/usr/bin/env python3
"""h3_sweep_report.py -- render cache/sweep/*.json as a comparison table."""
from __future__ import annotations
import glob, json, os, sys

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def main():
    names = sys.argv[1:] or ["speed", "capability"]
    for name in names:
        path = os.path.join(WS, "cache", "sweep", name + ".json")
        if not os.path.exists(path):
            continue
        recs = json.load(open(path))
        print(f"\n=== {name} ({len(recs)} configs) ===")
        hdr = (f"{'label':20s} {'shape':>14s} {'steps':>5s} {'seq':>7s} "
               f"{'s/step':>7s} {'denoise':>8s} {'decode':>7s} {'pipe':>7s} "
               f"{'out_s':>6s} {'xReal':>6s} {'peak':>6s}")
        print(hdr)
        print("-" * len(hdr))
        for r in recs:
            c = r["config"]; b = r.get("bench")
            shape = f"{c['width']}x{c['height']}x{c['num_frames']}"
            if not b:
                print(f"{c['label']:20s} {shape:>14s} {'--':>5s}   FAILED  {r.get('error','')[:70]}")
                continue
            seq = b.get("seq_len_predicted")
            real = (b["total_s"] / b["seconds_out"]) if b.get("seconds_out") else float("nan")
            print(f"{c['label']:20s} {shape:>14s} {b['steps']:5d} {str(seq):>7s} "
                  f"{b['step_s_last']:7.2f} {b['denoise_s']:8.1f} {b.get('decode_s', float('nan')):7.2f} "
                  f"{b['total_s']:7.1f} {b.get('seconds_out', float('nan')):6.2f} {real:6.1f} "
                  f"{b['peak_vram_gib']:6.2f}")

if __name__ == "__main__":
    main()