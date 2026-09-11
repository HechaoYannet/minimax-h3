#!/usr/bin/env bash
# h3_env.sh -- environment for the MiniMax-H3 workflow.
#   usage:  source env/h3_env.sh
#
# Works unchanged in two places:
#   * the WSL box    -- conda env `diffsynth`, models under $H3_ROOT/models
#   * the container  -- system python, models mounted at /models.  docker/entrypoint.sh
#                       exports H3_* before this file is sourced; the defaults below
#                       are only fallbacks, hence the ${VAR:-...} form everywhere.
#
# Deliberately does NOT set DIFFSYNTH_SKIP_DOWNLOAD: every model is passed by
# explicit local path, so nothing is ever fetched, and leaving the switch alone
# keeps the one-off processor fetch working.

export H3_ROOT="${H3_ROOT:-/home/yhc/source/minimax-h3}"
export H3_REPO="${H3_REPO:-$H3_ROOT/DiffSynth-Studio}"
export H3_MODELS="${H3_MODELS:-$H3_ROOT/models}"
export H3_WORKSPACE="${H3_WORKSPACE:-/mnt/d/otherProject/minimax-h3}"
export H3_SCRIPTS="${H3_SCRIPTS:-$H3_WORKSPACE/scripts}"

# The processor/tokenizer lives here (11 MiB, fetched from ModelScope once).
export H3_PROCESSOR="${H3_PROCESSOR:-$H3_MODELS/MiniMax-H3/Ref2VA/processor}"

# WSL box: activate the conda env.  Container: there is no conda -- the image's
# system python *is* the environment -- so skip it without a word.
if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
  source "$HOME/miniconda3/etc/profile.d/conda.sh"
  conda activate diffsynth
fi

# 24 logical cores, but the hot path is GPU-bound; leaving a few cores for the
# H2D copy threads and the WSL vmmem process is faster than grabbing all of them.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-8}"

# The DiT has to be re-copied into VRAM every step (9.76 GiB model, 7.9 GiB card),
# so the allocator sees a lot of churn. expandable_segments cuts fragmentation.
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"

# No flash_attn / sageattention / xformers in this env, so diffsynth falls back to
# torch SDPA -- whose cuDNN backend measured fastest (43.6 TFLOP/s at 16k tokens).
export DIFFSYNTH_ATTENTION_IMPLEMENTATION="${DIFFSYNTH_ATTENTION_IMPLEMENTATION:-torch}"

# DiskMap mmaps the checkpoints; a big buffer just means fewer re-opens.
export DIFFSYNTH_DISK_MAP_BUFFER_SIZE="${DIFFSYNTH_DISK_MAP_BUFFER_SIZE:-2000000000}"

echo "h3 env: python=$(command -v python || echo '(none)') conda=${CONDA_PREFIX:-none} torch=$(python -c 'import torch;print(torch.__version__)' 2>/dev/null || echo '?')"
echo "        models=$H3_MODELS"
echo "        workspace=$H3_WORKSPACE"
