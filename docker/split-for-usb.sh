#!/usr/bin/env bash
# split-for-usb.sh -- cut a docker image tar into pieces small enough for FAT32.
#
#   split-for-usb.sh <image.tar> <destination-dir> [part-size]
#
# FAT32 refuses any single file over 4 GiB, and an image this size is one huge
# tar, so the tar has to travel as numbered pieces.  Called by pack-usb.ps1;
# usable on its own from WSL.  Writes SHA256SUMS next to the pieces.
set -euo pipefail

SRC="${1:?usage: split-for-usb.sh <image.tar> <dest-dir> [part-size]}"
DEST="${2:?usage: split-for-usb.sh <image.tar> <dest-dir> [part-size]}"
SIZE="${3:-3500M}"

[ -s "$SRC" ] || { echo "no such file: $SRC" >&2; exit 1; }
mkdir -p "$DEST"
rm -f "$DEST"/image.tar.part-*

echo "splitting $(du -h "$SRC" | cut -f1) into ${SIZE} pieces"
split -b "$SIZE" -d -a 2 "$SRC" "$DEST/image.tar.part-"

echo "hashing"
( cd "$DEST" && sha256sum image.tar.part-* > SHA256SUMS )

echo "--- $DEST ---"
ls -lh "$DEST" | awk 'NR>1 {printf "  %-28s %s\n", $9, $5}'
echo "  total: $(du -sh "$DEST" | cut -f1)"
echo
echo "reassemble with:  cat $DEST/image.tar.part-* > image.tar"
