#!/usr/bin/env bash
# stage-models.sh -- copy the weights from the WSL filesystem to a Windows path.
#
#   wsl -d Ubuntu -u yhc -- bash /mnt/d/otherProject/minimax-h3/docker/stage-models.sh
#
# Why this exists: the weights live in the Ubuntu distro's filesystem, and Docker
# Desktop can only bind-mount Windows paths -- mounting \\wsl.localhost\... fails
# with "accessing specified distro mount service" unless WSL integration is on for
# this distro.  So the copy that the container mounts is the one on D:.
#
# 27 GB, once.  Idempotent: re-run it and cp only rewrites what changed.
set -euo pipefail

SRC="${H3_MODELS_SRC:-/home/yhc/source/minimax-h3/models}"
DST="${H3_MODELS_DST:-/mnt/d/otherProject/minimax-h3/models}"

[ -d "$SRC" ] || { echo "no such model directory: $SRC" >&2; exit 1; }
mkdir -p "$DST"

echo "staging $SRC -> $DST"
echo "  ($(du -sh "$SRC" | cut -f1) to copy; this takes a few minutes)"
cp -a --no-preserve=ownership "$SRC/." "$DST/"

# Windows tags every file it copies with an alternate data stream; they are
# harmless but they clutter the mount, so drop them.
find "$DST" -name '*:Zone.Identifier' -delete

echo "--- staged ---"
du -sh "$DST"
ls -1 "$DST"
