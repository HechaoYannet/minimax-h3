#!/usr/bin/env python3
"""h3_generate.py -- MiniMax-H3 Ref2VA generation workflow tuned for this box.

Hardware: RTX 5070 Laptop (8 GiB, sm_120) / 22.9 GiB RAM / 24 threads / WSL2.
Reads cache/plan.json (written by h3_audit.py) for every tuned default.

Design decisions, all backed by measurements in cache/bench.json:
  * cfg_scale=1.0 -> ONE forward per denoise step.  The negative pass is skipped
    entirely by the pipeline, so a 2x saving is already the default; do not raise
    cfg_scale on this box.
  * The DiT (9.76 GiB NF4) cannot fit in 8 GiB, so vram_limit parks as much of it
    on the GPU as the activation burst allows and the rest is re-staged from the
    disk map each step (~4.4 s/step of host time at the default vram_limit).
  * The text encoder (14.27 GiB NF4) is bigger than RAM, so it streams from disk
    (onload_device='disk').  Its prompt embedding is cached to disk and reused,
    which removes the 26 B-param encoder pass from the warm path (12.1 s cold).
  * Sequence length dominates cost (attention is quadratic).  Ref2VA makes this
    easy to get wrong: a 1024x1024 reference image at the framework default
    ref_image_short_edge=2048 adds 4096 rows (+54% attention); at 1024 it adds
    1024 rows (+12%).  A same-length reference *video* at the default 768 short
    edge costs ~2x the whole image-only sequence.

MEASURED END TO END (draft preset, 640x384x73f, 20 steps, LoRA attached):
  reference decode + text encoder  12.1 s cold / 0.0 s cache hit
  denoise                          13.4 s/step -> 4.6 min
  VAE decode + mux                 ~1 s
  peak VRAM                        3.05 GiB of 7.93

Two traps this file exists to avoid, both of which cost the previous session a
lot of time (see the comments at compute_text_embedding and the text-encoder
section further down):
  1. The unit chain must run under torch.no_grad().  MiniMaxH3Pipeline.__call__
     carries the decorator, but driving pipe.unit_runner directly does not, and
     the autograd graph pins every activation until the run dies with a
     bitsandbytes 'CUDA driver error: device not ready' that is really an OOM.
  2. Windows 'shared GPU memory' must be OFF.  With it on, CUDA spills into host
     RAM and a genuine OOM surfaces as 'Failed to create GPU mapping' instead of
     a real allocation error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h3_compat  # noqa: E402  -- must precede torchaudio/transformers/diffsynth

h3_compat.apply()

import torch  # noqa: E402

H3_ROOT = os.environ.get("H3_ROOT", "/home/yhc/source/minimax-h3")
H3_REPO = os.environ.get("H3_REPO", os.path.join(H3_ROOT, "DiffSynth-Studio"))
MODELS = os.environ.get("H3_MODELS", os.path.join(H3_ROOT, "models"))
WS = os.environ.get("H3_WORKSPACE", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if H3_REPO not in sys.path:
    sys.path.insert(0, H3_REPO)

FILES = {
    "dit": "minimax-h3-ref2va-pruned-nf4.safetensors",
    "text_encoder": "minimax-h3-text-encoder-nf4.safetensors",
    "video_vae": "video_vae_nf4.safetensors",
    "audio_vae": "audio_vae_nf4.safetensors",
}
LORA_FILE = "AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors"
PROCESSOR_DIR = os.path.join(MODELS, "MiniMax-H3", "Ref2VA", "processor")
CACHE_DIR = os.path.join(WS, "cache", "text_embed")


def log(msg):
    print(f"[h3 {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_plan():
    path = os.path.join(WS, "cache", "plan.json")
    if os.path.exists(path):
        return json.load(open(path))
    log("cache/plan.json not found -- falling back to built-in defaults")
    return {"resolved": {"vram_limit_gib": 5.0, "denoise": {"steps": 30, "flow_shift": 12.0,
                                                            "audio_flow_shift": 3.0},
                         "vae": {"tiled": True, "tile_size": 256, "tile_overlap": 64},
                         "vram_config": {}}, "presets": {}}


def dtype_of(v):
    if isinstance(v, str) and v == "bfloat16":
        return torch.bfloat16
    return v


# --------------------------------------------------------------------------- text cache
def text_cache_key(prompt, references, height, width, num_frames, edges):
    """Exact, content-addressed key: identical inputs -> identical embedding."""
    import numpy as np
    h = hashlib.sha256()
    h.update((prompt or "").encode("utf-8"))
    h.update(f"|{height}x{width}x{num_frames}|".encode())
    h.update(json.dumps(edges, sort_keys=True).encode())
    for f in sorted(FILES.values()):
        p = os.path.join(MODELS, f)
        h.update(f"|{f}:{os.path.getsize(p)}".encode())
    for r in references or []:
        h.update(("|" + r["type"]).encode())
        if r.get("image") is not None:
            h.update(np.asarray(r["image"].convert("RGB")).tobytes())
        if r.get("video") is not None:
            h.update(np.stack([np.asarray(f.convert("RGB")) for f in r["video"]]).tobytes())
        if r.get("audio") is not None:
            h.update(np.asarray(r["audio"].float()).tobytes())
            h.update(str(r.get("sample_rate")).encode())
    return h.hexdigest()[:20]


def save_text_cache(key, embeds, tags):
    from safetensors.torch import save_file
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, key + ".safetensors")
    save_file({"prompt_embeds": embeds.contiguous().to(torch.bfloat16),
               "text_token_tags": tags.contiguous().to(torch.int64)}, path)
    return path


def load_text_cache(key):
    from safetensors.torch import load_file
    path = os.path.join(CACHE_DIR, key + ".safetensors")
    if not os.path.exists(path):
        return None
    d = load_file(path)
    return d["prompt_embeds"], d["text_token_tags"]


# --------------------------------------------------------------------------- pipeline
def normalise_vram_config(d):
    """Enforce the framework's undocumented invariant.

    AutoWrappedLinear.onload() does
        self.load_from_disk(self.onload_dtype, self.onload_device)
    and load_from_disk() feeds those straight into Tensor.to(dtype=..., device=...).
    The string "disk" is only a valid *dtype* while the device is also "disk": any
    other pairing raises
        TypeError: to() received an invalid combination of arguments
    on the first non-quantized Linear of the model.  The official example scripts
    always pair (disk, disk) or (bfloat16, cpu); the planner does too, and this is
    the belt-and-braces check."""
    d = dict(d)
    if d.get("onload_device") != "disk" and d.get("onload_dtype") == "disk":
        log(f"  vram_config: onload_device={d.get('onload_device')!r} with "
            f"onload_dtype='disk' -> forcing onload_dtype="
            f"{d.get('computation_dtype') or torch.bfloat16}")
        d["onload_dtype"] = d.get("computation_dtype") or torch.bfloat16
    if d.get("preparing_device") != "disk" and d.get("preparing_dtype") == "disk":
        d["preparing_dtype"] = d.get("computation_dtype") or torch.bfloat16
    return d


# ---------------------------------------------------------------------------
# Why the text encoder no longer needs a special case
# ---------------------------------------------------------------------------
# The previous session hit this on the first real encoder pass:
#
#     RuntimeError: CUDA driver error: device not ready
#     The CUDA driver logged these messages: Failed to create GPU mapping
#
# and concluded that "AutoWrappedQuantizedModule leaks ~238 MiB per NF4 layer on
# the GPU path", then pinned the whole encoder to the CPU to dodge it.
#
# That conclusion was wrong.  Measured directly (scripts/h3_te_probe.py, tracing
# free VRAM and live GPU bytes after every vision block and every decoder layer):
#
#     embed_tokens        free 5.30   live_gpu 1.45 GiB  <- loaded once
#     visual blocks 1-27  free 3.78   live_gpu 2.56 GiB  <- flat after block 1
#     lang layer 1        free 3.18   live_gpu 2.72 GiB  <- +0.17, then FLAT
#     lang layers 2-50    free 3.02   (no growth at all)
#     FORWARD OK: (3170, 5120) embeds, vision tokens 1506,
#                 peak 3.64 GiB with 3.02 GiB still free
#
# Nothing leaks.  What actually happened is that Windows "shared GPU memory" was
# still on: once the card filled, CUDA silently spilled into host RAM over PCIe
# and the driver gave up with "Failed to create GPU mapping" instead of raising a
# clean OOM.  With shared memory off the GPU path is comfortably inside budget.
#
# The CPU pinning was not just unnecessary, it was actively harmful: it put a
# 26 B-parameter model on 24 CPU cores *and* left input_ids/pixel tensors on
# cuda, which is exactly the device mismatch that killed run5.
#
# patch_cpu_vram_gate() is kept only as a safety net for anyone who does set a
# CPU computation device: check_free_vram() calls torch.<dev>.mem_get_info, which
# does not exist for torch.cpu, so the CPU path would raise AttributeError before
# it could compute.  Nothing calls it from the normal path any more.

def patch_cpu_vram_gate():
    """Make check_free_vram() answer 'yes' on a non-CUDA computation device.

    torch.cpu.mem_get_info does not exist, so the CPU path of a quantized layer would
    raise AttributeError before it could compute.  Only needed if you deliberately
    set a CPU computation device; on this box the encoder runs on the GPU."""
    from diffsynth.core.vram.layers import AutoTorchModule
    if getattr(AutoTorchModule, "_h3_cpu_gate_patched", False):
        return
    original = AutoTorchModule.check_free_vram

    def check_free_vram(self):
        if self.computation_device_type not in ("cuda", "mps", "npu"):
            return True
        return original(self)

    AutoTorchModule.check_free_vram = check_free_vram
    AutoTorchModule._h3_cpu_gate_patched = True


def build_pipeline(plan, want, vram_limit, dit_onload="cpu"):
    from diffsynth.pipelines.minimax_h3_audio_video import MiniMaxH3Pipeline, ModelConfig
    vc = plan.get("resolved", {}).get("vram_config", {})
    cfgs = []
    for key in want:
        if key not in FILES:
            continue
        d = dict(vc.get(key) or {})
        d = {k: dtype_of(v) for k, v in d.items()}
        if not d:
            d = {"offload_dtype": "disk", "offload_device": "disk",
                 "onload_dtype": "disk", "onload_device": "disk",
                 "preparing_dtype": torch.bfloat16, "preparing_device": "cuda",
                 "computation_dtype": torch.bfloat16, "computation_device": "cuda"}
        if key == "dit":
            d["onload_device"] = dit_onload
        d = normalise_vram_config(d)
        cfgs.append(ModelConfig(path=os.path.join(MODELS, FILES[key]), **d))
    proc = None
    if "processor" in want:
        proc = ModelConfig(path=PROCESSOR_DIR)
    pipe = MiniMaxH3Pipeline.from_pretrained(
        torch_dtype=torch.bfloat16, device="cuda",
        model_configs=cfgs, processor_config=proc, vram_limit=vram_limit)
    pipe.vram_management_enabled = pipe.check_vram_management_state()
    return pipe


def _install_encoder_trace(pipe):
    """Print free VRAM after every vision block / decoder layer (H3_TRACE_ENCODER=1).

    Diagnostic only: bitsandbytes surfaces a driver-level allocation failure as
    "CUDA driver error: device not ready", which says nothing about how far the
    forward pass got.  This makes the failure point unambiguous."""
    te = pipe.text_encoder

    def live_gpu():
        seen, total = set(), 0
        for t in list(te.parameters()) + list(te.buffers()):
            if t is not None and not t.is_meta and t.device.type == "cuda" and id(t) not in seen:
                seen.add(id(t))
                total += t.numel() * t.element_size()
        return total / 1024 ** 3

    counter = {"lang": 0, "vis": 0}

    def wrap(cls, kind):
        orig = cls.forward

        def wrapper(self, *a, **kw):
            out = orig(self, *a, **kw)
            counter[kind] += 1
            free, total = torch.cuda.mem_get_info()
            print(f"[trace] {kind} {counter[kind]:2d}: free {free/1024**3:5.3f}  "
                  f"alloc {torch.cuda.memory_allocated()/1024**3:5.3f}  "
                  f"reserved {torch.cuda.memory_reserved()/1024**3:5.3f}  "
                  f"live_gpu {live_gpu():5.3f}", flush=True)
            return out

        cls.forward = wrapper

    wrap(type(te.model.visual.blocks[0]), "vis")
    wrap(type(te.model.language_model.layers[0]), "lang")
    print(f"[trace] encoder tracing on; free {torch.cuda.mem_get_info()[0]/1024**3:.3f} GiB",
          flush=True)


def _log_encoder_progress(tag):
    free, total = torch.cuda.mem_get_info()
    print(f"[trace] {tag}: free {free/1024**3:5.3f}  "
          f"alloc {torch.cuda.memory_allocated()/1024**3:5.3f}  "
          f"reserved {torch.cuda.memory_reserved()/1024**3:5.3f}", flush=True)


@torch.no_grad()
def compute_text_embedding(pipe, prompt, references, height, width, num_frames, edges):
    """Run the unit chain up to and including the prompt embedder, then stop.
    Returns the same (prompt_embeds, text_token_tags) pair a full call would use.

    @torch.no_grad() is load-bearing, not decoration.  MiniMaxH3Pipeline.__call__
    carries it, but this function drives pipe.unit_runner directly and so inherits
    nothing.  Without it the Qwen3-VL forward builds an autograd graph that pins
    every intermediate activation for the whole 27-block vision tower and all 50
    language layers, and the run dies partway through with

        RuntimeError: CUDA driver error: device not ready
        The CUDA driver logged these messages: Failed to create GPU mapping

    which is an OOM wearing a costume -- measured at +0.24 GiB per vision block,
    0.86 GiB free by block 10 and zero by block 17.  With no_grad the same pass
    is flat at 2.53 GiB live / 3.02 GiB free from block 1 to block 27.

    What made this so hard to see: bitsandbytes raises that driver error rather
    than torch.OutOfMemoryError, and the whole pipeline is loaded with
    requires_grad=False, so nothing *looks* like training."""
    from diffsynth.pipelines.minimax_h3_audio_video import MiniMaxH3Unit_PromptEmbedder
    posi = {"prompt": prompt}
    nega = {"negative_prompt": " "}
    shared = {
        "cfg_scale": 1.0, "height": height, "width": width, "num_frames": num_frames,
        "seed": 0, "rand_device": "cpu",
        "tiled": True, "tile_size": 256, "tile_overlap": 64,
        "use_gradient_checkpointing": False, "use_gradient_checkpointing_offload": False,
        "keyframes": None, "keyframe_indices": None,
        "references": references,
        "control_video": None, "control_scale": 1.0,
        "retake_video": None, "frame_regions_to_retake": None,
        "retake_audio": None, "seconds_regions_to_retake": None,
        "imgvid_cond_noise_aug": pipe.imgvid_cond_noise_aug,
        "audio_cond_noise_aug": pipe.audio_cond_noise_aug,
        "text_embedding": None,
        **edges,
    }
    # Same model swap the full pipeline performs before the unit chain, so the
    # VAE weights the ReferenceEncoder just used are handed back before the
    # 14.27 GiB encoder starts streaming.
    pipe.load_models_to_device(["text_encoder"])
    trace = os.environ.get("H3_TRACE_ENCODER")
    if trace:
        _install_encoder_trace(pipe)
    for unit in pipe.units:
        if trace:
            _log_encoder_progress("before " + type(unit).__name__)
        shared, posi, nega = pipe.unit_runner(unit, pipe, shared, posi, nega)
        if trace:
            _log_encoder_progress("after  " + type(unit).__name__)
        if isinstance(unit, MiniMaxH3Unit_PromptEmbedder):
            return posi["prompt_embeds"].detach().to("cpu"), posi["text_token_tags"].detach().to("cpu")
    raise RuntimeError("PromptEmbedder unit not reached")


# --------------------------------------------------------------------------- LoRA
def convert_kohya_lora_keys(state_dict, model):
    """Map kohya/sd-scripts LoRA keys onto diffsynth's dotted module names.

    The deployed LoRA is sd-scripts format:
        lora_unet_blocks_0_attn_qkv_proj.lora_down.weight
    diffsynth's GeneralLoRALoader only understands the *suffix* (lora_down /
    lora_up / alpha) -- it does not translate the 'lora_unet_<path with _ for .>'
    prefix, so load_lora() matches 0 modules and silently does nothing.  We build
    the mapping from the live model instead of guessing, so an unexpected key is
    reported rather than dropped."""
    from diffsynth.core.vram.layers import LoRAHotLoadMixin
    names = {}
    for _, m in model.named_modules():
        n = getattr(m, "name", "")
        if n and isinstance(m, LoRAHotLoadMixin):
            names[n.replace(".", "_")] = n
    out, unmapped = {}, set()
    for k, v in state_dict.items():
        if not k.startswith("lora_unet_"):
            out[k] = v
            continue
        modkey, _, field = k[len("lora_unet_"):].partition(".")
        target = names.get(modkey)
        if target is None:
            unmapped.add(modkey)
            continue
        out[f"{target}.{field}"] = v
    return out, sorted(unmapped), names


def load_lora(pipe, path, alpha=1.0):
    from safetensors.torch import load_file
    raw = load_file(path)                       # CPU, float32
    converted, unmapped, names = convert_kohya_lora_keys(raw, pipe.dit)
    if unmapped:
        log(f"  WARNING: {len(unmapped)} LoRA key prefix(es) match no module, e.g. "
            f"{unmapped[:4]} (of {len(names)} LoRA-capable modules)")
    log(f"  LoRA keys: {len(raw)} -> {len(converted)} mapped, "
        f"{len(set(k.rsplit('.', 2)[0] for k in converted))} target modules")
    converted = {k: (v.to(torch.bfloat16) if v.is_floating_point() else v)
                 for k, v in converted.items()}
    before = torch.cuda.memory_allocated()
    pipe.load_lora(pipe.dit, state_dict=converted, alpha=alpha)
    del converted, raw
    torch.cuda.empty_cache()
    patched = sum(1 for _, m in pipe.dit.named_modules()
                  if getattr(m, "lora_A_weights", None))
    if patched == 0:
        raise RuntimeError(
            "LoRA attached to 0 modules. Refusing to continue: the run would silently "
            "produce base-model output while you believe the LoRA is applied.")
    log(f"  LoRA active on {patched} modules, VRAM +"
        f"{(torch.cuda.memory_allocated()-before)/1024**3:.2f} GiB")
    return patched


def install_cached_prompt_embedder(pipe, embeds, tags):
    """Swap the prompt-embedder unit for one that replays a cached embedding.

    MiniMaxH3Unit_PromptEmbedder already accepts a text_embedding= kwarg, but the
    early-return path it takes then hard-codes text_token_tags to all ones, and
    PackedSequenceBuilder writes those tags into packed['token_tags'][text_pos].
    Replaying the real tags keeps the vision placeholder positions correct."""
    from diffsynth.pipelines.minimax_h3_audio_video import MiniMaxH3Unit_PromptEmbedder

    class _Cached(MiniMaxH3Unit_PromptEmbedder):
        def process(self, pipe, prompt, keyframes=None, ref_blocks=None,
                    height=None, width=None, text_embedding=None):
            return {"prompt_embeds": embeds.to(pipe.device, pipe.torch_dtype),
                    "text_token_tags": tags.to(pipe.device, torch.long)}

    n = 0
    for i, u in enumerate(pipe.units):
        if isinstance(u, MiniMaxH3Unit_PromptEmbedder):
            pipe.units[i] = _Cached()
            n += 1
    if n != 1:
        raise RuntimeError(f"expected exactly one PromptEmbedder unit, found {n}")
    return pipe


def timed_progress(steps, state):
    def factory(iterable):
        t0 = [time.perf_counter()]
        for i, x in enumerate(iterable):
            if i > 0:
                dt = (time.perf_counter() - t0[0]) / i
                alloc = torch.cuda.memory_allocated() / 1024 ** 3
                reserved = torch.cuda.memory_reserved() / 1024 ** 3
                peak = torch.cuda.max_memory_allocated() / 1024 ** 3
                print(f"    step {i:3d}/{steps}  {dt:6.2f} s/step  eta {(steps-i)*dt/60:5.1f} min"
                      f"  alloc {alloc:4.2f}  reserved {reserved:4.2f}  peak {peak:4.2f} GiB",
                      flush=True)
            yield x
    return factory


# --------------------------------------------------------------------------- references
class _RefAction(argparse.Action):
    """Append (kind, path) to one shared, order-preserving list.

    All four --ref-* flags write to the same dest.  argparse dispatches actions
    in command-line order, so the ordering the user typed is preserved ACROSS
    flags -- and that ordering is semantic, not cosmetic:
    MiniMaxH3Unit_PromptEmbedder.preprocess_ref_blocks walks the references
    list in order to number the <Image n> / <Video n> / <Audio n> placeholders
    that go into the tokenizer (condition_labels -> presentation_ref2va), and
    MiniMaxH3Unit_ReferenceEncoder concatenates ref_visual_anchor and
    ref_audio_anchor in that same order.
    """

    def __init__(self, option_strings, dest, kind=None, **kwargs):
        super().__init__(option_strings, dest, **kwargs)
        self.kind = kind

    def __call__(self, parser, namespace, values, option_string=None):
        # copy, never mutate: argparse hands every action the same default object
        items = list(getattr(namespace, self.dest, None) or [])
        items.append((self.kind, values))
        setattr(namespace, self.dest, items)


_AUDIO_FALLBACK_LOGGED = False


def decode_audio(path):
    """Decode any audio/video source to a ([C, T] float32 tensor, sample_rate).

    diffsynth's own reader is tried first, but it cannot work on this box:
    diffsynth.utils.data.audio.read_audio goes through torchcodec, and
    torchcodec needs FFmpeg *shared* libraries (libavutil.so.61, ...) that are
    not installed -- only imageio's standalone ffmpeg binary is.  Worse,
    read_video_audio wraps read_audio in a bare except and turns the failure
    into waveform=None, so the breakage is silent there.

    The fallback shells out to that same bundled ffmpeg binary, which is what
    imageio already uses to decode reference *video* successfully, so both
    media types then share one working decoder stack.
    """
    try:
        from diffsynth.utils.data.audio import read_audio
        waveform, sample_rate = read_audio(path, resample=False)
        return waveform.float().contiguous(), int(sample_rate)
    except Exception as exc:
        global _AUDIO_FALLBACK_LOGGED
        if not _AUDIO_FALLBACK_LOGGED:
            log("  framework read_audio unavailable (%s: %s) -- using the bundled "
                "ffmpeg binary for every audio reference"
                % (type(exc).__name__, str(exc).splitlines()[0][:80]))
            _AUDIO_FALLBACK_LOGGED = True
    return _decode_audio_via_ffmpeg(path)


def _decode_audio_via_ffmpeg(path):
    """ffmpeg -> 32-bit float WAV -> soundfile.  Works for mp3/wav/flac and for
    the soundtrack of an mp4, none of which libsndfile can open directly.
    """
    import subprocess
    import tempfile
    import numpy as np
    import soundfile as sf
    import imageio_ffmpeg

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "audio.wav")
        proc = subprocess.run(
            [exe, "-v", "error", "-y", "-i", path, "-vn", "-ac", "2",
             "-c:a", "pcm_f32le", wav],
            capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.exists(wav):
            raise SystemExit("could not decode audio from %s: %s"
                             % (path, (proc.stderr or "ffmpeg failed").strip()[:300]))
        data, sample_rate = sf.read(wav, dtype="float32", always_2d=True)
    return torch.from_numpy(np.ascontiguousarray(data.T)), int(sample_rate)


def build_references(specs, height, width, num_frames, fps=24):
    """Turn the ordered --ref-* specs into the framework references list.

    Audio is read at the file's own rate on purpose: the framework resamples it
    itself against pipe.audio_vae.sample_rate inside
    MiniMaxH3Unit_ReferenceEncoder._encode_audio_ref, so nothing here needs the
    pipeline to exist yet and no sample rate has to be guessed."""
    from PIL import Image
    from diffsynth.utils.data.audio_video import read_video_audio

    refs = []
    for kind, path in specs:
        if kind == "image":
            ref = {"type": "image", "image": Image.open(path).convert("RGB")}
            detail = "%dx%d" % ref["image"].size
        elif kind in ("video", "video_audio"):
            frames, waveform, sample_rate = read_video_audio(
                path, height=height, width=width, num_frames=num_frames, fps=fps)
            ref = {"type": kind, "video": frames}
            detail = "%d frames" % len(frames)
            if kind == "video_audio":
                if waveform is None:
                    # Decode it ourselves and apply the same alignment rule the
                    # framework uses: clip the audio to the span the kept frames
                    # actually cover, so the two stay a time-aligned pair.
                    try:
                        waveform, sample_rate = decode_audio(path)
                    except SystemExit:
                        raise SystemExit(
                            "--ref-video-audio %s: no decodable audio track found; "
                            "use --ref-video if this reference is meant to be silent."
                            % path)
                    keep = int(round(len(frames) / float(fps) * sample_rate))
                    waveform = waveform[:, :keep]
                if waveform is None or waveform.numel() == 0:
                    raise SystemExit(
                        "--ref-video-audio %s: no audio track could be decoded; "
                        "use --ref-video for a silent reference." % path)
                ref["audio"] = waveform
                ref["sample_rate"] = int(sample_rate)
                detail += ", audio %s @ %d Hz" % (tuple(waveform.shape), int(sample_rate))
        elif kind == "audio":
            waveform, sample_rate = decode_audio(path)
            ref = {"type": "audio", "audio": waveform, "sample_rate": int(sample_rate)}
            detail = "%s @ %d Hz" % (tuple(waveform.shape), int(sample_rate))
        else:  # pragma: no cover
            raise SystemExit("unknown reference kind %r" % (kind,))
        refs.append(ref)
        log("  reference %d: %-11s %s  %s" % (len(refs), kind, detail, path))
    return refs


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("input")
    src.add_argument("--prompt-file", help="UTF-8 text file with the prompt")
    src.add_argument("--prompt", help="prompt inline (overrides --prompt-file)")
    # All four write to the same dest so the order across flags is preserved;
    # see _RefAction.  That order is what numbers <Image n>/<Video n>/<Audio n>.
    src.add_argument("--ref-image", action=_RefAction, kind="image", dest="ref_specs",
                     default=[], metavar="PATH", help="reference image (repeatable)")
    src.add_argument("--ref-video", action=_RefAction, kind="video", dest="ref_specs",
                     metavar="PATH",
                     help="reference video, silent (repeatable). Read at the target "
                          "height/width/num_frames; --ref-video-short-edge bounds its cost")
    src.add_argument("--ref-video-audio", action=_RefAction, kind="video_audio", dest="ref_specs",
                     metavar="PATH",
                     help="reference video together with its own soundtrack (repeatable) -- "
                          "the video-editing reference type of the official example")
    src.add_argument("--ref-audio", action=_RefAction, kind="audio", dest="ref_specs",
                     metavar="PATH",
                     help="audio-only reference, e.g. a voice timbre (repeatable)")
    src.add_argument("--out", default=None, help="output .mp4 path")
    pres = ap.add_argument_group("shape")
    pres.add_argument("--preset", default=None, help="preset name from plan.json")
    pres.add_argument("--height", type=int, default=None)
    pres.add_argument("--width", type=int, default=None)
    pres.add_argument("--num-frames", type=int, default=None)
    pres.add_argument("--steps", type=int, default=None, help="denoise steps (overrides preset)")
    pres.add_argument("--seed", type=int, default=42)
    pres.add_argument("--ref-image-short-edge", type=int, default=None,
                      help="reference image short edge; 2048 (framework default) costs "
                           "+54%% attention per 1024x1024 reference, 1024 costs +12%%")
    pres.add_argument("--ref-video-short-edge", type=int, default=None)
    pres.add_argument("--ref-video-max-pixels", type=int, default=None)
    knob = ap.add_argument_group("memory / speed")
    knob.add_argument("--vram-limit", type=float, default=None,
                      help="GiB threshold on TOTAL used VRAM; default from plan.json. "
                           "This is the single most important speed knob: every wrapped "
                           "layer the gate refuses to stage is re-staged from the disk map "
                           "on every denoise step (measured 18 ms/layer). Raise it until "
                           "--activation-reserve stops being needed, or a step OOMs.")
    knob.add_argument("--activation-reserve", type=float, default=None,
                      help="GiB carved out of the startup-free VRAM for the activation "
                           "burst. vram_limit = startup_free - activation_reserve - LoRA. "
                           "Default 1.7 GiB at 832x480/124f, but the draft preset's real "
                           "burst is only ~1 GiB, so this can usually be lowered a lot.")
    knob.add_argument("--dit-onload", choices=["cpu", "disk"], default="cpu",
                      help="cpu = keep the 9.76 GiB DiT in host RAM (peak RSS 11.1 of 22.9 GiB); "
                           "disk = stream it from mmap, peak RSS 4.8 GiB, slightly slower per step")
    knob.add_argument("--sdpa-backend", default="cudnn", choices=["cudnn", "flash", "efficient", "auto"],
                      help="torch SDPA backend for the H3 attention; cuDNN measured fastest")
    knob.add_argument("--tile-size", type=int, default=None, help="VAE decode tile size")
    knob.add_argument("--tile-overlap", type=int, default=None)
    knob.add_argument("--no-tiled", action="store_true", help="decode the VAE in one shot")
    lora = ap.add_argument_group("lora")
    lora.add_argument("--lora", default=None, help="LoRA .safetensors path")
    lora.add_argument("--lora-alpha", type=float, default=1.0)
    sched = ap.add_argument_group("scheduler / sampler")
    sched.add_argument("--scheduler", choices=["auto", "flow", "beta"], default="auto",
                       help="step placement. 'flow' = diffsynth's uniform shifted grid; "
                            "'beta' = ComfyUI's beta(0.6,0.6) quantile grid. 'auto' picks "
                            "beta whenever a LoRA is attached, because the AfterMidnight "
                            "Ref2VA LoRA requires 'euler sampler + beta scheduler' or the "
                            "audio degrades. The sampler is first-order Euler either way.")
    sched.add_argument("--beta-alpha", type=float, default=0.6)
    sched.add_argument("--beta-beta", type=float, default=0.6)
    tc = ap.add_argument_group("text embedding cache")
    tc.add_argument("--text-cache", action="store_true",
                    help="reuse a cached prompt embedding when the key matches (default: on)")
    tc.add_argument("--no-text-cache", action="store_true")
    tc.add_argument("--refresh-text-cache", action="store_true")
    tc.add_argument("--cache-text-only", action="store_true",
                    help="compute and store the embedding, then exit without denoising")
    misc = ap.add_argument_group("misc")
    misc.add_argument("--dry-run", action="store_true",
                      help="resolve and print the full plan without loading any weight")
    misc.add_argument("--load-only", action="store_true",
                      help="load every model and exercise the offload/onload plumbing, then "
                           "exit WITHOUT running any denoising step (validates the vram_config)")
    misc.add_argument("--json", action="store_true")
    args = ap.parse_args()

    plan = load_plan()
    resolved = plan.get("resolved", {})
    presets = plan.get("presets", {})

    # ---- resolve shape -----------------------------------------------------
    p = dict(presets.get(args.preset or resolved.get("default_preset", "standard"), {}))
    height = args.height or (p.get("aligned", {}).get("height")) or 480
    width = args.width or (p.get("aligned", {}).get("width")) or 832
    num_frames = args.num_frames or (p.get("aligned", {}).get("num_frames")) or 124
    steps = args.steps or (p.get("steps")) or resolved.get("denoise", {}).get("steps", 30)
    img_edge = args.ref_image_short_edge or p.get("ref_image_short_edge") or 1024
    vid_edge = args.ref_video_short_edge or p.get("ref_video_short_edge") or 384
    vid_maxpx = args.ref_video_max_pixels or p.get("ref_video_max_pixels") or vid_edge * 672
    vae = resolved.get("vae", {})
    tile_size = args.tile_size if args.tile_size is not None else vae.get("tile_size", 256)
    tile_overlap = args.tile_overlap if args.tile_overlap is not None else vae.get("tile_overlap", 64)
    tiled = not args.no_tiled

    prompt = args.prompt
    if prompt is None and args.prompt_file:
        prompt = open(args.prompt_file, encoding="utf-8").read().strip()
    edges = {"ref_image_short_edge": img_edge, "ref_video_short_edge": vid_edge,
             "ref_video_max_pixels": vid_maxpx}

    # ---- references --------------------------------------------------------
    # Decoded lazily: --dry-run only reports the resolved order, and --load-only
    # never needs the pixels.
    references = None
    if args.ref_specs and not args.dry_run and not args.load_only:
        log(f"decoding {len(args.ref_specs)} reference(s); the order below defines "
            f"the <Image n>/<Video n>/<Audio n> labels in the prompt")
        references = build_references(args.ref_specs, height, width, num_frames)

    summary = {"preset": args.preset, "height": height, "width": width,
               "num_frames": num_frames, "steps": steps, "seed": args.seed,
               "refs": [{"type": k, "path": p} for k, p in args.ref_specs],
               "edges": edges, "vram_limit": args.vram_limit or resolved.get("vram_limit_gib"),
               "dit_onload": args.dit_onload, "tiled": tiled,
               "tile_size": tile_size, "tile_overlap": tile_overlap,
               "lora": args.lora, "prompt_chars": len(prompt or ""),
               "out": args.out}
    if args.dry_run:
        print("=" * 78)
        print("DRY RUN -- nothing is loaded")
        print("=" * 78)
        print(json.dumps(summary, indent=2))
        if p:
            print(f"\npreset model says: seq {p['rows']['seq_len']}, {p['step_s']} s/step,"
                  f" {p['denoise_min']} min for {p['steps']} steps")
            print("  rows: " + json.dumps(p["rows"]))
            print(f"  (that is what preset '{args.preset or resolved.get('default_preset', 'standard')}' "
                  f"assumes -- {p.get('refs')} -- NOT necessarily what you passed above;")
            print("   different references change seq, and attention is quadratic in seq)")
        if args.ref_specs:
            print("\nreference order -- this is what numbers the prompt's placeholders and")
            print("what ref_visual_anchor / ref_audio_anchor are concatenated in:")
            for i, (kind, path) in enumerate(args.ref_specs, 1):
                print(f"  {i}. {kind:12s} {path}")
        return 0

    if not prompt and not args.load_only:
        ap.error("--prompt or --prompt-file is required")

    # ---- text embedding cache ---------------------------------------------
    use_cache = not args.no_text_cache
    if args.load_only:
        key, cached, need_encoder = None, None, False
        need = ["dit", "video_vae", "audio_vae", "processor", "text_encoder"]
    else:
        key = text_cache_key(prompt, references, height, width, num_frames, edges)
        cached = None if args.refresh_text_cache else (load_text_cache(key) if use_cache else None)
        need_encoder = cached is None
        log(f"text-embedding key {key} -> " + ("cache hit" if cached is not None else "cache miss"))
        need = ["dit", "video_vae", "audio_vae", "processor"]
        if need_encoder:
            need.append("text_encoder")
    vram_limit = args.vram_limit
    if vram_limit is None and args.activation_reserve is not None:
        # The plan's own formula, re-evaluated live step by step in h3_audit.py:
        #   vram_limit = startup_free - activation_reserve - lora_reserve
        startup_free = torch.cuda.mem_get_info()[0] / 1024 ** 3
        # Same term h3_audit.py subtracts (line ~303): the LoRA ships F32 and is
        # held as bf16, so it costs half its file size on the card.
        lora_gb = (os.path.getsize(args.lora) / 2 / 1024 ** 3) if args.lora else 0.0
        vram_limit = startup_free - args.activation_reserve - lora_gb
        log(f"vram_limit from --activation-reserve {args.activation_reserve} GiB: "
            f"{startup_free:.2f} free - {args.activation_reserve} - {lora_gb} LoRA "
            f"= {vram_limit:.2f} GiB")
    if vram_limit is None:
        vram_limit = resolved.get("vram_limit_gib", 5.0)
    log(f"building pipeline (vram_limit={vram_limit:.2f} GiB, dit_onload={args.dit_onload})")
    t0 = time.perf_counter()
    pipe = build_pipeline(plan, need, vram_limit, dit_onload=args.dit_onload)
    log(f"pipeline ready in {time.perf_counter()-t0:.1f} s"
        f" (vram_management={pipe.vram_management_enabled})")

    if args.lora:
        log(f"hot-loading LoRA {os.path.basename(args.lora)}")
        t0 = time.perf_counter()
        load_lora(pipe, args.lora, alpha=args.lora_alpha)
        log(f"LoRA loaded in {time.perf_counter()-t0:.1f} s,"
            f" VRAM now {torch.cuda.memory_allocated()/1024**3:.2f} GiB")
    # ---- scheduler ---------------------------------------------------------
    import h3_scheduler
    mode = args.scheduler
    if mode == "auto":
        mode = "beta" if args.lora else "flow"
        log(f"scheduler auto -> '{mode}'" + (" (LoRA attached)" if args.lora else ""))
    if args.lora and mode == "flow":
        log("  !! WARNING: the AfterMidnight Ref2VA LoRA requires the beta scheduler; "
            "running 'flow' is expected to produce degraded audio.")
    info = h3_scheduler.install(pipe, mode=mode, alpha=args.beta_alpha, beta=args.beta_beta)
    # Trigger the schedules once so the real step placement is known before we start.
    fs = resolved.get("denoise", {}).get("flow_shift", 12.0)
    afs = resolved.get("denoise", {}).get("audio_flow_shift", 3.0)
    pipe.scheduler.set_timesteps(steps, shift=fs)
    pipe.scheduler_audio.set_timesteps(steps, shift=afs)
    v, a = info["scheduler"], info["scheduler_audio"]
    detail = (f"beta({args.beta_alpha}, {args.beta_beta})" if mode == "beta"
              else "uniform shifted grid")
    log(f"scheduler '{mode}' {detail}; sampler = first-order Euler (FlowMatchScheduler.step)")
    log(f"  video: shift={v['shift']:g} -> {v['steps']} steps, "
        f"sigma {v['sigma_max']:.4f} -> {v['sigma_min_nonzero']:.4f} -> 0")
    log(f"  audio: shift={a['shift']:g} -> {a['steps']} steps, "
        f"sigma {a['sigma_max']:.4f} -> {a['sigma_min_nonzero']:.4f} -> 0")
    if v["steps"] != a["steps"]:
        raise RuntimeError("video/audio step counts diverged; the pipeline indexes "
                           "scheduler_audio.timesteps by progress_id")

    if args.load_only:
        from diffsynth.core.vram.layers import AutoTorchModule

        def rss():
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024 ** 2
            return float("nan")

        def audit(model):
            """Where are this model's wrapped layers parked, and how many bytes?"""
            states, nbytes = {}, 0
            for m in model.modules():
                if isinstance(m, AutoTorchModule):
                    states[m.state] = states.get(m.state, 0) + 1
                    for p in m.parameters():
                        if not p.is_meta:
                            nbytes += p.numel() * p.element_size()
            return states, nbytes / 1024 ** 3

        log(f"LOAD-ONLY smoke test.  baseline RSS {rss():.2f} GiB,"
            f" GPU alloc {torch.cuda.memory_allocated()/1024**3:.2f} GiB")
        log(f"  (framework states: 0=offloaded/meta  1=onload target  2=preparing target)")
        for names in (["dit"], ["video_vae", "audio_vae"], ["text_encoder"]):
            t0 = time.perf_counter()
            pipe.load_models_to_device(names)
            dt = time.perf_counter() - t0
            st, nb = audit(getattr(pipe, names[0]))
            log(f"  onload {str(names):26s} {dt:6.1f} s  RSS {rss():5.2f} GiB"
                f"  live params {nb:5.2f} GiB  states={st}")
        t0 = time.perf_counter()
        for name, model in pipe.named_children():
            if hasattr(model, "offload"):
                model.offload()
            else:
                for m in model.modules():
                    if hasattr(m, "offload"):
                        m.offload()
        torch.cuda.empty_cache()
        st, nb = audit(pipe.text_encoder)
        log(f"  offload everything {time.perf_counter()-t0:6.1f} s  RSS {rss():5.2f} GiB"
            f"  live params {nb:5.2f} GiB  states={st}")
        log("LOAD-ONLY OK: the vram_config loads, swaps and unloads cleanly.")
        return 0

    if need_encoder:
        log("running the text encoder (26 B params, streamed from disk)")
        t0 = time.perf_counter()
        embeds, tags = compute_text_embedding(pipe, prompt, references, height, width,
                                              num_frames, edges)
        log(f"text embedding done in {time.perf_counter()-t0:.1f} s"
            f" -> {tuple(embeds.shape)} embeds, vision tokens {(tags == 0).sum().item()}")
        if use_cache or args.cache_text_only:
            log("saved " + save_text_cache(key, embeds, tags))
    else:
        embeds, tags = cached
        log(f"replaying cached embedding {tuple(embeds.shape)}")

    if args.cache_text_only:
        return 0

    install_cached_prompt_embedder(pipe, embeds, tags)

    # ---- denoise + decode --------------------------------------------------
    backend = {"cudnn": "CUDNN_ATTENTION", "flash": "FLASH_ATTENTION",
               "efficient": "EFFICIENT_ATTENTION"}.get(args.sdpa_backend)
    log(f"denoising: {width}x{height}x{num_frames}f, {steps} steps, seed {args.seed},"
        f" sdpa={args.sdpa_backend or 'auto'}")
    torch.cuda.reset_peak_memory_stats()
    t0 = time.perf_counter()
    ctx = None
    if backend:
        from torch.nn.attention import SDPBackend, sdpa_kernel
        ctx = sdpa_kernel(getattr(SDPBackend, backend))
    try:
        if ctx is not None:
            ctx.__enter__()
        video, audio = pipe(
            prompt=None, text_embedding=None,
            height=height, width=width, num_frames=num_frames,
            num_inference_steps=steps, seed=args.seed,
            cfg_scale=1.0,
            flow_shift=resolved.get("denoise", {}).get("flow_shift", 12.0),
            audio_flow_shift=resolved.get("denoise", {}).get("audio_flow_shift", 3.0),
            references=references,
            tiled=tiled, tile_size=tile_size, tile_overlap=tile_overlap,
            progress_bar_cmd=timed_progress(steps, {}),
            **edges,
        )
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)
    dt = time.perf_counter() - t0
    log(f"generation finished in {dt/60:.1f} min"
        f" ({dt/steps:.1f} s/step), peak VRAM {torch.cuda.max_memory_allocated()/1024**3:.2f} GiB")

    out = args.out or os.path.join(WS, "outputs",
                                   f"h3_{args.preset or 'custom'}_{args.seed}.mp4")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    from diffsynth.utils.data.audio_video import write_video_audio
    write_video_audio(video=video, audio=audio, output_path=out, fps=24,
                      audio_sample_rate=pipe.audio_vae.sample_rate)
    log(f"wrote {out}  ({len(video)} frames = {len(video)/24:.2f} s)")
    if args.json:
        print(json.dumps({**summary, "seconds": dt, "output": out}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())