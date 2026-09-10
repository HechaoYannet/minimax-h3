#!/usr/bin/env bash
# Build synthetic reference media for the video/audio reference paths, which the
# workflow has never exercised end to end.  The source is the same still used by
# the image path, turned into 3 s of Ken-Burns motion plus a tone, so the test is
# reproducible from committed material rather than a binary blob.
set -euo pipefail
cd /mnt/d/otherProject/minimax-h3/workspace/pink-harem-ref2va
FF=$(~/miniconda3/envs/diffsynth/bin/python -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())")
mkdir -p materials

# 3 s @ 24 fps, 832x480, gentle zoom so consecutive frames differ
"$FF" -v error -y -loop 1 -i materials/ref-pink-harem.jpg \
  -f lavfi -i "sine=frequency=220:sample_rate=44100:duration=3" \
  -filter_complex "[0:v]scale=832:480:force_original_aspect_ratio=increase,crop=832:480,zoompan=z=min(zoom+0.0012\,1.25):d=72:s=832x480:fps=24,format=yuv420p[v];[1:a]volume=0.25[a]" \
  -map "[v]" -map "[a]" -t 3 -c:v libx264 -preset veryfast -crf 20 -c:a aac -b:a 128k \
  materials/ref-clip.mp4

# the same motion, silent
"$FF" -v error -y -i materials/ref-clip.mp4 -an -c:v copy materials/ref-clip-silent.mp4

# an audio-only reference (the voice-timbre slot)
"$FF" -v error -y -f lavfi -i "sine=frequency=330:sample_rate=44100:duration=3,tremolo=f=4:d=0.7" \
  -c:a libmp3lame -b:a 192k materials/ref-tone.mp3

ls -la materials/
