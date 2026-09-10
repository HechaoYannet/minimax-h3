#!/usr/bin/env bash
# Re-run specific sweep points in isolation, dropping any previous FAILED record
# for them first.  h3_sweep.py skips configs already present in the results file,
# so retesting a failure needs the stale entry removed.
set -uo pipefail
cd /mnt/d/otherProject/minimax-h3
suite="$1"; shift
python3 - "$suite" "$@" <<"PY"
import json, os, sys
suite, labels = sys.argv[1], set(sys.argv[2:])
p = f"cache/sweep/{suite}.json"
if os.path.exists(p):
    recs = [r for r in json.load(open(p))
            if not (r["config"]["label"] in labels and "bench" not in r)]
    json.dump(recs, open(p, "w"), indent=2)
    print(f"kept {len(recs)} records")
PY
for L in "$@"; do
  python3 -u scripts/h3_sweep.py "$suite" --only "$L"
done
