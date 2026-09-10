#!/usr/bin/env bash
# run_h3.sh -- single entry point for the MiniMax-H3 workflow.
#
#   ./run_h3.sh check                 # env + model audit + front-end validation (no inference)
#   ./run_h3.sh bench                 # re-measure the hardware ceilings
#   ./run_h3.sh plan                  # recompute cache/plan.json (presets + memory plan)
#   ./run_h3.sh loadcheck             # load every model, swap them, unload -- no inference
#   ./run_h3.sh textcache <args...>   # precompute a prompt embedding (runs the text encoder)
#   ./run_h3.sh gen <args...>         # the actual generation (NOT run in this session)
#   ./run_h3.sh dry <args...>         # print the resolved plan for a request
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$HERE/env/h3_env.sh" >/dev/null
cd "$H3_WORKSPACE"

case "${1:-}" in
  check)
    python scripts/h3_validate.py
    echo
    python scripts/h3_audit.py
    ;;
  bench)     python scripts/h3_bench.py ;;
  plan)
    python scripts/h3_validate.py >/dev/null
    python scripts/h3_audit.py
    ;;
  loadcheck) shift; python scripts/h3_generate.py --load-only "$@" ;;
  textcache) shift; python scripts/h3_generate.py --cache-text-only "$@" ;;
  dry)       shift; python scripts/h3_generate.py --dry-run "$@" ;;
  gen)       shift; python scripts/h3_generate.py "$@" ;;
  fetch)     python scripts/h3_fetch_processor.py ;;
  *)
    sed -n '2,12p' "$0"
    exit 1
    ;;
esac
