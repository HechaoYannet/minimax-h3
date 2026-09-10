#!/usr/bin/env python3
"""h3_te_probe.py -- measure the text encoder's real VRAM profile on this box.

Builds the pipeline exactly the way h3_generate.py does (so ModelPool resolves
the NF4 quant_config), then runs the text encoder and prints, after every block,
both the CUDA free memory and the bytes the model actually holds on the GPU.

Context: the previous session concluded that AutoWrappedQuantizedModule leaks
~238 MiB per NF4 layer on the GPU path, and pinned the encoder to the CPU because
of it.  That measurement was taken while Windows "shared GPU memory" was enabled,
where CUDA spills into host RAM and the driver says "Failed to create GPU mapping"
instead of raising a clean OOM.  This probe re-measures with shared memory off.
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import h3_compat  # noqa: E402
h3_compat.apply()

import torch  # noqa: E402

import h3_generate as G  # noqa: E402

WS = "/mnt/d/otherProject/minimax-h3/workspace/pink-harem-ref2va"


def gib(n):
    return n / 1024 ** 3


def free_gib():
    return gib(torch.cuda.mem_get_info()[0])


def live_gpu(*models):
    """Bytes the given module trees actually hold on the GPU right now."""
    seen, total = set(), 0
    for model in models:
        for t in list(model.parameters()) + list(model.buffers()):
            if t is not None and not t.is_meta and t.device.type == "cuda" and id(t) not in seen:
                seen.add(id(t))
                total += t.numel() * t.element_size()
    return total


def main():
    plan = G.load_plan()
    resolved = plan.get("resolved", {})

    references = G.build_references([("image", f"{WS}/materials/ref-pink-harem.jpg")],
                                    384, 640, 73)
    prompt = open(f"{WS}/prompt.txt", encoding="utf-8").read().strip()
    edges = {"ref_image_short_edge": 1024, "ref_video_short_edge": 384,
             "ref_video_max_pixels": 384 * 672}

    vram_limit = resolved.get("vram_limit_gib", 5.0)
    print(f"[probe] free before build {free_gib():.2f} GiB, vram_limit {vram_limit}")
    pipe = G.build_pipeline(plan, ["dit", "video_vae", "audio_vae", "processor", "text_encoder"],
                            vram_limit)
    print(f"[probe] pipeline ready, free {free_gib():.2f} GiB")

    # Reproduce the failing run exactly: it had the LoRA attached before the encoder.
    if "--no-lora" not in sys.argv:
        import glob as _glob
        lora = _glob.glob(os.path.expanduser(
            "~/source/minimax-h3/models/AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors"))
        if lora:
            G.load_lora(pipe, lora[0])
            print(f"[probe] LoRA loaded, free {free_gib():.2f} GiB, "
                  f"alloc {gib(torch.cuda.memory_allocated()):.2f} GiB")

    te = pipe.text_encoder
    q = te.model.language_model.layers[0].self_attn.q_proj
    print(f"[probe] q_proj: {type(q).__name__} / inner {type(q.module).__name__}, "
          f"disk_offload={q.disk_offload}, vram_limit={q.vram_limit}, "
          f"states offload/onload/prep/comp = "
          f"{q.offload_device}/{q.onload_device}/{q.preparing_device}/{q.computation_device}")

    layers = te.model.language_model.layers
    vis_blocks = te.model.visual.blocks
    counter = {"lang": 0, "vis": 0}

    def traced(kind, orig):
        def wrapper(self, *a, **kw):
            out = orig(self, *a, **kw)
            counter[kind] += 1
            print(f"[probe] {kind} {counter[kind]:2d}: free {free_gib():5.3f}  "
                  f"alloc {gib(torch.cuda.memory_allocated()):5.3f}  "
                  f"live_gpu {gib(live_gpu(te)):5.3f}", flush=True)
            return out
        return wrapper

    saved = []
    for kind, holder, idx in (("lang", layers, 0), ("vis", vis_blocks, 0)):
        cls = type(holder[idx])
        saved.append((cls, cls.forward))
        cls.forward = traced(kind, cls.forward)

    for name, holder in (("visual", te.model), ("embed_tokens", te.model.language_model)):
        target = getattr(holder, name)
        saved.append(("bound", target, target.forward))

        def wrapper(*a, __n=name, __t=target, **kw):
            out = __t.__class__.forward.__wrapped__(__t, *a, **kw) \
                if hasattr(__t.__class__.forward, "__wrapped__") else None
            return out

    def trace_bound(name, target):
        import types
        orig = target.forward

        def wrapper(*a, **kw):
            out = orig(*a, **kw)
            print(f"[probe] {name:12s}: free {free_gib():5.3f}  "
                  f"alloc {gib(torch.cuda.memory_allocated()):5.3f}  "
                  f"live_gpu {gib(live_gpu(te)):5.3f}", flush=True)
            return out

        target.forward = wrapper
        return orig

    orig_vis = trace_bound("visual", te.model.visual)
    orig_emb = trace_bound("embed_tokens", te.model.language_model.embed_tokens)

    # The real workflow builds EVERY model first, so by the time the text encoder
    # runs the DiT is already onloaded to CPU and has parked vram_limit worth of
    # NF4 weights on the card.  Reproduce that state before measuring.
    import sys as _sys
    if "--no-dit" not in _sys.argv:
        print(f"[probe] free before load_models_to_device(dit) {free_gib():.2f} GiB")
        pipe.load_models_to_device(["dit"])
        print(f"[probe] after load_models_to_device(dit): free {free_gib():.2f} GiB, "
              f"alloc {gib(torch.cuda.memory_allocated()):.2f} GiB")

    torch.cuda.reset_peak_memory_stats()
    print(f"[probe] free before fwd {free_gib():.2f} GiB")
    try:
        if "--grad" in sys.argv:
            embeds, tags = G.compute_text_embedding(pipe, prompt, references, 384, 640, 73, edges)
        else:
            with torch.no_grad():
                embeds, tags = G.compute_text_embedding(pipe, prompt, references, 384, 640, 73, edges)
        print(f"[probe] FORWARD OK: {tuple(embeds.shape)}, "
              f"vision tokens {int((tags == 0).sum())}, "
              f"peak {gib(torch.cuda.max_memory_allocated()):.2f} GiB, "
              f"free after {free_gib():.2f} GiB, "
              f"live_gpu {gib(live_gpu(te)):.2f} GiB")
    except Exception as exc:
        print(f"[probe] FORWARD FAILED (lang {counter['lang']}, vis {counter['vis']}): "
              f"{type(exc).__name__}: {str(exc)[:300]}")
        print(f"[probe] free {free_gib():.2f} GiB, "
              f"peak {gib(torch.cuda.max_memory_allocated()):.2f} GiB, "
              f"live_gpu {gib(live_gpu(te)):.2f} GiB")
        raise
    finally:
        te.model.visual.forward = orig_vis
        te.model.language_model.embed_tokens.forward = orig_emb
        for cls, fwd in saved:
            if cls != "bound":
                cls.forward = fwd


if __name__ == "__main__":
    main()
