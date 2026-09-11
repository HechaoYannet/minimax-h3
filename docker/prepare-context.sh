#!/usr/bin/env bash
# prepare-context.sh -- stage the build context that only the WSL side can supply.
#
#   wsl -d Ubuntu -u yhc -- bash /mnt/d/otherProject/minimax-h3/docker/prepare-context.sh
#
# Produces three artifacts under docker/vendor/ (all git-ignored):
#
#   conda-env.tar      the diffsynth conda environment, verbatim      (~7 GB)
#   diffsynth.tar      DiffSynth-Studio source at the pinned commit   (~12 MB)
#   processor/         Ref2VA processor + tokenizer                   (11 MB)
#
# Why the environment travels as a tar instead of a pip install: measured from
# inside this network, files.pythonhosted.org serves 0.13 MB/s and
# download.pytorch.org 0.9 MB/s, so the ~6 GB of wheels would take hours.  The
# tar is a copy of the environment that actually passed the end-to-end run.
#
# GitHub is unreachable from inside a container here, hence diffsynth.tar too.
set -euo pipefail

PIN="${DIFFSYNTH_PIN:-50e5efbc8c4170b72878e826ddc735f926408c2d}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$HERE/vendor"
ENVS="${H3_CONDA_ENVS:-/home/yhc/miniconda3/envs}"
ENV_NAME="${H3_CONDA_ENV:-diffsynth}"
SRC="${H3_REPO:-/home/yhc/source/minimax-h3/DiffSynth-Studio}"
MODELS="${H3_MODELS:-/home/yhc/source/minimax-h3/models}"

mkdir -p "$VENDOR"

# --- 1. the conda environment --------------------------------------------------
ENV_TAR="$VENDOR/conda-env.tar"
echo "== conda env =="
if [ -s "$ENV_TAR" ] && [ "$ENV_TAR" -nt "$ENVS/$ENV_NAME/conda-meta/history" ]; then
  echo "ok: $ENV_TAR is newer than the env itself"
else
  [ -d "$ENVS/$ENV_NAME" ] || { echo "no such conda env: $ENVS/$ENV_NAME" >&2; exit 1; }
  echo "packing $ENVS/$ENV_NAME ($(du -sh "$ENVS/$ENV_NAME" | cut -f1)) -- a couple of minutes"
  rm -f "$ENV_TAR"
  tar -C "$ENVS" -cf "$ENV_TAR" "$ENV_NAME"
fi
echo "    $(du -h "$ENV_TAR" | cut -f1)"

# --- 2. DiffSynth-Studio source ------------------------------------------------
TAR="$VENDOR/diffsynth.tar"
echo "== DiffSynth-Studio =="
if [ -s "$TAR" ]; then
  echo "ok: $TAR already staged"
elif git -C "$SRC" cat-file -e "${PIN}^{commit}" 2>/dev/null; then
  echo "archiving $SRC @ ${PIN:0:7}"
  HEAD_SHA="$(git -C "$SRC" rev-parse HEAD)"
  [ "${HEAD_SHA}" = "$PIN" ] || echo "note: checkout is at ${HEAD_SHA:0:7}, pinning ${PIN:0:7} explicitly"
  git -C "$SRC" archive --format=tar "$PIN" > "$TAR"
else
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  echo "no local checkout holding ${PIN:0:7} -- cloning (needs GitHub access)"
  git clone --quiet https://github.com/modelscope/DiffSynth-Studio.git "$TMP/ds"
  git -C "$TMP/ds" archive --format=tar "$PIN" > "$TAR"
fi
echo "    $(du -h "$TAR" | cut -f1)  $(tar -tf "$TAR" | wc -l) entries"

# --- 3. processor + tokenizer --------------------------------------------------
PROC="$VENDOR/processor"
echo "== processor =="
if [ -d "$PROC" ]; then
  echo "ok: $PROC already staged"
else
  echo "copying from $MODELS/MiniMax-H3/Ref2VA/processor"
  cp -a "$MODELS/MiniMax-H3/Ref2VA/processor" "$PROC"
fi
echo "    $(du -sh "$PROC" | cut -f1)  $(ls -1 "$PROC" | wc -l) files"

# --- 4. sanity -----------------------------------------------------------------
missing=0
for f in merges.txt tokenizer.json tokenizer_config.json vocab.json preprocessor_config.json; do
  [ -f "$PROC/$f" ] || { echo "MISSING: $PROC/$f" >&2; missing=1; }
done
[ -f "$HERE/Dockerfile" ] || { echo "MISSING: docker/Dockerfile" >&2; missing=1; }
[ -s "$ENV_TAR" ] || { echo "MISSING: $ENV_TAR" >&2; missing=1; }
tar -tf "$ENV_TAR" | head -1 | grep -q "^$ENV_NAME/" || { echo "BAD: $ENV_TAR has no $ENV_NAME/ prefix" >&2; missing=1; }
[ "$missing" -eq 0 ] || exit 1

echo
echo "build context ready:"
du -sh "$VENDOR"/* | sed 's/^/  /'
