#!/usr/bin/env bash
# entrypoint.sh -- container-side counterpart of env/h3_env.sh.
#
# Exports the same variables the WSL box uses, points them at the mount points
# instead of the WSL paths, then dispatches run_h3.sh verbs so that
#
#     docker run --rm --gpus all -v <models>:/models <image> check
#
# behaves like ./run_h3.sh check does on the box.
set -euo pipefail

export H3_ROOT="${H3_ROOT:-/opt/h3}"
export H3_REPO="${H3_REPO:-$H3_ROOT/DiffSynth-Studio}"
export H3_MODELS="${H3_MODELS:-/models}"
export H3_WORKSPACE="${H3_WORKSPACE:-/workspace}"
export H3_SCRIPTS="${H3_SCRIPTS:-$H3_WORKSPACE/scripts}"
export H3_PROCESSOR="${H3_PROCESSOR:-$H3_MODELS/MiniMax-H3/Ref2VA/processor}"

# Same tuned defaults as env/h3_env.sh (see the comments there).
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-8}"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
export DIFFSYNTH_ATTENTION_IMPLEMENTATION="${DIFFSYNTH_ATTENTION_IMPLEMENTATION:-torch}"
export DIFFSYNTH_DISK_MAP_BUFFER_SIZE="${DIFFSYNTH_DISK_MAP_BUFFER_SIZE:-2000000000}"

# The processor ships inside the image (11 MiB).  h3_validate.py / h3_generate.py
# derive its path from H3_MODELS, so a model directory that only carries weights
# needs this link to become usable.  Read-only mounts simply get a warning.
if [ ! -d "$H3_PROCESSOR" ] && [ -d /opt/h3/processor ]; then
    if mkdir -p "$(dirname "$H3_PROCESSOR")" 2>/dev/null && \
       ln -sfn /opt/h3/processor "$H3_PROCESSOR" 2>/dev/null; then
        echo "h3: $H3_PROCESSOR was missing -- linked the copy baked into the image" >&2
    else
        export H3_PROCESSOR=/opt/h3/processor
        echo "h3: $H3_MODELS is not writable and has no processor/ -- using /opt/h3/processor" >&2
        echo "h3: scripts that derive the path from H3_MODELS will need it mounted instead" >&2
    fi
fi

# NOTE: "bash -c ..." must reach bash intact, so the shell cases fall through to
# the generic exec below rather than running a bare `exec bash` for any `bash`.
case "${1:-}" in
    check|bench|plan|loadcheck|textcache|gen|dry|fetch)
        if [ ! -x "$H3_WORKSPACE/run_h3.sh" ]; then
            echo "h3: $H3_WORKSPACE/run_h3.sh not found or not executable." >&2
            echo "h3: mount the repository at /workspace, or drop the arguments to get a shell." >&2
            exit 1
        fi
        exec "$H3_WORKSPACE/run_h3.sh" "$@"
        ;;
    "")
        exec bash
        ;;
    *)
        exec "$@"
        ;;
esac
