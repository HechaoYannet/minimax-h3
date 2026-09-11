# NOTE: there is deliberately no "# syntax=" line here.  That directive makes
# BuildKit fetch docker/dockerfile:1 from Docker Hub, which is unreachable on
# this network (docker/README.md 1).  Nothing below needs a modern frontend.
# =============================================================================
#  MiniMax-H3 · Ref2VA  --  the WSL-side environment, frozen into an image
#
#  This is a *copy* of the working WSL box rather than a rebuild of it:
#
#      /home/yhc/miniconda3/envs/diffsynth            the conda env, verbatim
#      /home/yhc/source/minimax-h3/DiffSynth-Studio   source at the pinned commit
#
#  Those paths are deliberately identical to WSL: the editable install of
#  diffsynth (__editable__.diffsynth-2.1.7.pth + its finder), the shebangs in
#  bin/ and every absolute path baked into the environment keep working with no
#  patching at all.
#
#  Only the two mounted paths move:
#      H3_MODELS     /models      27 GB of NF4 weights -- not in the image
#      H3_WORKSPACE  /workspace   this repository (baked in, usually over-mounted)
#
#  Why a copied environment instead of "pip install -r requirements.lock":
#  measured from inside a container on this network, files.pythonhosted.org
#  serves 0.13 MB/s and download.pytorch.org 0.9 MB/s.  The ~6 GB of torch +
#  CUDA wheels would take hours, while the tar is exact and takes minutes.
#  docker/requirements.lock keeps the package list for a rebuild elsewhere.
#
#  Build from the Windows side, where Docker Desktop lives:
#      pwsh docker/build.ps1
# =============================================================================
ARG BASE_REGISTRY=docker.m.daocloud.io/library
FROM ${BASE_REGISTRY}/python:3.14-slim

LABEL org.opencontainers.image.title="minimax-h3-ref2va" \
      org.opencontainers.image.description="MiniMax-H3 Ref2VA inference environment copied from the WSL box: conda env diffsynth (Python 3.14.7, torch 2.14.0+cu132, DiffSynth-Studio 2.1.7)" \
      org.opencontainers.image.source="https://github.com/HechaoYannet/minimax-h3" \
      org.opencontainers.image.licenses="Apache-2.0"

# Debian mirrors: deb.debian.org measured 0.16 MB/s from here, USTC 3.0 MB/s.
ARG APT_MIRROR=mirrors.ustc.edu.cn

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    H3_ROOT=/home/yhc/source/minimax-h3 \
    H3_REPO=/home/yhc/source/minimax-h3/DiffSynth-Studio \
    H3_MODELS=/models \
    H3_WORKSPACE=/workspace \
    CONDA_PREFIX=/home/yhc/miniconda3/envs/diffsynth \
    PATH=/home/yhc/miniconda3/envs/diffsynth/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin

# ffmpeg is here for its *shared libraries*: torchcodec dlopen()s libavutil and
# friends, and their absence is what README 3.3 describes.  torchcodec 0.16
# ships one core per FFmpeg major (libtorchcodec_core4..core9 -> libavutil
# 56..61) and picks a match at import time, so the distro's FFmpeg 7.1 is
# enough -- no hand-built FFmpeg required.  libgomp1 is what torch's OpenMP
# needs and the conda env does not carry its own.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        -o Acquire::Retries=3 -o Acquire::http::Timeout=30 \
        ffmpeg \
        libgomp1 \
        ca-certificates \
        curl \
        git \
        less \
        procps; \
    rm -rf /var/lib/apt/lists/*

# The conda environment, verbatim (docker/prepare-context.sh packs it).  ADD
# unpacks the tar, keeping the diffsynth/ prefix under the destination.
ADD docker/vendor/conda-env.tar /home/yhc/miniconda3/envs/

# DiffSynth-Studio at the pinned commit, at the path its editable install
# already points to.  The tar is a git archive, so it has no top-level dir and
# unpacks straight into place.
ADD docker/vendor/diffsynth.tar /home/yhc/source/minimax-h3/DiffSynth-Studio/

# processor/tokenizer fallback (11 MiB).  It normally travels with the mounted
# weights; this copy lets the image encode a prompt without any model volume.
COPY docker/vendor/processor /opt/h3/processor

# The weights mount point, so a bare "docker run" still has /models to talk
# about (and the processor fallback above has somewhere to link into).
RUN mkdir -p /models

# The workflow itself, laid out exactly like the checkout.  Bind-mount a
# checkout over /workspace to iterate on the scripts without rebuilding.
COPY run_h3.sh /workspace/run_h3.sh
COPY env /workspace/env
COPY scripts /workspace/scripts
COPY references /workspace/references
COPY cache/plan.json cache/bench.json cache/validate.json /workspace/cache/
COPY docker/entrypoint.sh /usr/local/bin/h3-entrypoint

# Belt and braces: a CRLF in a shell script turns into the famously unhelpful
# "bash\r: No such file or directory".
RUN set -eux; \
    sed -i 's/\r$//' /workspace/run_h3.sh /workspace/env/h3_env.sh /usr/local/bin/h3-entrypoint; \
    chmod +x /workspace/run_h3.sh /usr/local/bin/h3-entrypoint

# Build-time proof that the copied environment is coherent: the interpreter, the
# CUDA build of torch, the audio stack (torchcodec needs the ffmpeg above) and
# the H3 pipeline module all have to import.  No GPU is needed to import.
RUN set -eux; \
    python -c "import sys, torch, torchvision, torchaudio, torchcodec, bitsandbytes, transformers; \
import diffsynth; \
from diffsynth.pipelines.minimax_h3_audio_video import MiniMaxH3Pipeline; \
print('python     ', sys.version.split()[0]); \
print('torch      ', torch.__version__, 'cuda', torch.version.cuda); \
print('torchaudio ', torchaudio.__version__, '| torchcodec', torchcodec.__version__); \
print('diffsynth  ', diffsynth.__file__)"

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/h3-entrypoint"]
CMD ["bash"]
