#!/usr/bin/env bash
set -uo pipefail
cd /mnt/d/otherProject/minimax-h3
P=workspace/pink-harem-ref2va
L=~/source/minimax-h3/models/AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors
run() {
  local name="$1"; shift
  echo "===== $name ====="
  ./run_h3.sh gen --height 384 --width 640 --num-frames 39 --steps 8 \
    --ref-image-short-edge 512 --ref-video-short-edge 384 \
    --prompt-file "$P/prompt.txt" --lora "$L" \
    --out "$P/outputs/refmodes/$name.mp4" "$@" 2>&1 | grep -E "^\[h3|RuntimeError" | tail -5
}
run ref-audio       --ref-audio "$P/materials/ref-tone.mp3"
run ref-video-audio --ref-video-audio "$P/materials/ref-clip.mp4"
run mixed           --ref-image "$P/materials/ref-pink-harem.jpg" --ref-audio "$P/materials/ref-tone.mp3"
echo "===== outputs ====="; ls -la "$P/outputs/refmodes"
