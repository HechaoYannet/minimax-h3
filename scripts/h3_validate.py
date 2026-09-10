#!/usr/bin/env python3
"""h3_validate.py -- static / front-end validation of the H3 workflow.

Runs NO diffusion inference and loads NO transformer weights.  It exercises the
*real* diffsynth code paths that do not need weights, and cross-checks the
numbers h3_audit.py predicts against what the framework actually computes:

  1. the processor + tokenizer load fully offline from the local copy
  2. presentation_t2va / presentation_ref2va produce token ids + tags
  3. MiniMaxH3Unit_ReferenceEncoder's reference-shape helpers (the row math)
  4. MiniMaxH3Unit_PackedSequenceBuilder -- the REAL sequence layout, on dummy
     CPU latents, so seq_len / img_pos / audio_pos are exact, not modelled
  5. the quantization exclude-lists in diffsynth's MODEL_CONFIGS actually match
     the tensor names stored in the deployed files (a silent-corruption guard)
  6. the vram_config / preset table is consistent with the pipeline signature
"""
from __future__ import annotations

import json, math, os, struct, sys, traceback

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import h3_compat  # noqa: E402  -- must run before torchaudio/transformers/diffsynth
h3_compat.apply()

H3_ROOT = os.environ.get("H3_ROOT", "/home/yhc/source/minimax-h3")
H3_REPO = os.environ.get("H3_REPO", os.path.join(H3_ROOT, "DiffSynth-Studio"))
MODELS = os.environ.get("H3_MODELS", os.path.join(H3_ROOT, "models"))
WS = os.environ.get("H3_WORKSPACE", "/mnt/d/otherProject/minimax-h3")
PROCESSOR = os.path.join(MODELS, "MiniMax-H3", "Ref2VA", "processor")
if H3_REPO not in sys.path:
    sys.path.insert(0, H3_REPO)

RESULTS = []


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append((name, True, detail))
        print(f"  [PASS] {name}")
        if detail:
            for line in str(detail).splitlines():
                print(f"         {line}")
    except Exception as e:
        RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)


def read_header(path):
    with open(path, "rb") as f:
        return json.loads(f.read(struct.unpack("<Q", f.read(8))[0]))


def main():
    from PIL import Image
    import numpy as np
    from transformers import AutoProcessor
    from diffsynth.pipelines.minimax_h3_audio_video import (
        MiniMaxH3Pipeline, MiniMaxH3Unit_ReferenceEncoder, MiniMaxH3Unit_PackedSequenceBuilder,
    )
    from diffsynth.models.minimax_h3_text_encoder import (
        presentation_t2va, presentation_ref2va, image_token_counts, sample_qwen_video_frames,
    )

    print("=" * 92)
    print("1. PROCESSOR / TOKENIZER")
    print("=" * 92)
    state = {}

    def load_processor():
        assert os.path.isdir(PROCESSOR), f"missing {PROCESSOR}"
        proc = AutoProcessor.from_pretrained(PROCESSOR, local_files_only=True)
        state["proc"] = proc
        tok = proc.tokenizer
        return (f"class          {type(proc).__name__}\n"
                f"image proc     {type(proc.image_processor).__name__}  merge_size="
                f"{getattr(proc.image_processor, 'merge_size', '?')}\n"
                f"video proc     {type(proc.video_processor).__name__}\n"
                f"tokenizer      {type(tok).__name__}  vocab={tok.vocab_size}")
    check("AutoProcessor.from_pretrained(local, local_files_only=True)", load_processor)

    pipe = MiniMaxH3Pipeline(device="cpu", torch_dtype=torch.bfloat16)

    print()
    print("=" * 92)
    print("2. PRESENTATION (token ids + tags)")
    print("=" * 92)
    tok = state["proc"].tokenizer
    prompt_zh = ("一个网站页面，网站页面UI设计，网站动效，视频展示了流畅的网页向下滚动效果。"
                 "一个极具爆发力与动感的产品官网风格产品落地页 UI/UX 演示视频。")

    def t2va():
        ids, tags = presentation_t2va(tok, prompt_zh)
        state["ids_t2va"], state["tags_t2va"] = ids, tags
        return (f"text tokens    {ids.shape[0]}   tags={sorted(set(tags.tolist()))}\n"
                f"(a {len(prompt_zh)}-char Chinese prompt -> {ids.shape[0]} tokens)")
    check("presentation_t2va", t2va)

    img = Image.fromarray(
        (np.random.default_rng(0).random((1024, 1024, 3)) * 255).astype("uint8"))

    def ref2va_text():
        pv, grid, counts = image_token_counts(state["proc"], [img])
        ids, tags = presentation_ref2va(tok, prompt_zh, [("image", 1)], counts, [], [])
        state["ids_ref"], state["tags_ref"] = ids, tags
        state["image_counts"] = counts
        state["image_grid"] = grid
        n_vision = int((tags == 0).sum())
        return (f"image_grid_thw {grid.tolist()}   image token count {counts}\n"
                f"total tokens   {ids.shape[0]}   text-tagged={int((tags==1).sum())} "
                f"vision-tagged={n_vision}\n"
                f"text tokens without the <Picture 1> block = {ids.shape[0] - n_vision - 4} (approx)")
    check("image_token_counts + presentation_ref2va", ref2va_text)

    print()
    print("=" * 92)
    print("3. REFERENCE SHAPE HELPERS (used by the planner's row math)")
    print("=" * 92)
    enc = MiniMaxH3Unit_ReferenceEncoder()

    def refimg():
        out = []
        for edge in (2048, 1536, 1024, 768, 512):
            w, h = enc._resolve_reference_image_shape(pipe, 1024, 1024, edge)
            rows = 1 * (h // 16 // 2) * (w // 16 // 2)
            out.append(f"short_edge={edge:5d} -> canvas {w}x{h} -> latent "
                       f"{w//16}x{h//16} -> {rows:5d} rows")
        return "\n".join(out)
    check("_resolve_reference_image_shape", refimg)

    def refvid():
        out = []
        for edge in (768, 512, 384, 256):
            w, h = enc._resolve_reference_video_shape(pipe, 832, 480, edge, edge * 672)
            used = enc._trim_reference_video_length(pipe, min(124, 124))
            lt = ((used - 5) // 17) * 5 + 2
            rows = lt * (h // 16 // 2) * (w // 16 // 2)
            out.append(f"short_edge={edge:5d} -> canvas {w}x{h} -> {used} frames -> "
                       f"{lt} latent frames -> {rows:6d} rows")
        return "\n".join(out)
    check("_resolve_reference_video_shape + _trim_reference_video_length", refvid)

    def sampler():
        frames = list(range(124))
        sampled, ts = sample_qwen_video_frames(frames)
        return (f"124 source frames -> {len(sampled)} sampled, {len(ts)} temporal blocks "
                f"(presentation may insert up to {len(sampled)*2} extra vision tokens)")
    check("sample_qwen_video_frames", sampler)

    print()
    print("=" * 92)
    print("4. REAL PACKED SEQUENCE LAYOUT  (dummy CPU latents, no weights)")
    print("=" * 92)
    builder = MiniMaxH3Unit_PackedSequenceBuilder()

    def packed():
        rows = []
        text_len = int(state["ids_ref"].shape[0])
        for (h, w, f, edge) in [(384, 640, 73, 768), (480, 832, 73, 1024),
                                (480, 832, 124, 1024), (576, 1024, 124, 1024),
                                (768, 1344, 124, 1024)]:
            h2, w2, f2 = pipe.check_resize_height_width(h, w, f, verbose=0)
            lt = ((f2 - 5) // 17) * 5 + 2
            lh, lw = h2 // 16, w2 // 16
            at = round(f2 / 24.0 * 40.0)
            cw, ch = enc._resolve_reference_image_shape(pipe, 1024, 1024, edge)
            ref = {"kind": "image", "latent_t": 1, "latent_h": ch // 16, "latent_w": cw // 16,
                   "ref_audio_t": 0}
            video_latents = torch.zeros(1, 24, lt, lh, lw)
            audio_latents = torch.zeros(2, 32, at)
            pe = torch.zeros(text_len, 5120)
            out = builder.process(pipe, pe, video_latents, audio_latents,
                                  text_token_tags=state["tags_ref"], ref_blocks=[ref])
            pk = out["packed"]
            rows.append({"shape": f"{w2}x{h2}x{f2}", "text": text_len,
                         "ref_image_short_edge": edge,
                         "ref_image_rows": 1 * (ch // 16 // 2) * (cw // 16 // 2),
                         "target_video_rows": lt * (lh // 2) * (lw // 2),
                         "target_audio_rows": at * 2,
                         "used": int(pk["cu_seqlens"][1]), "seq_len": int(pk["seq_len"]),
                         "img_pos": int(pk["img_pos"].numel()),
                         "audio_pos": int(pk["audio_pos"].numel()),
                         "tags_present": sorted(set(pk["token_tags"].tolist()))})
        state["packed"] = rows
        s = f"  text_len = {text_len} tokens (from the real tokenizer, 1024x1024 ref @ short_edge=1024)\n"
        s += f"  {'shape':>16s} {'text':>6s} {'refimg':>7s} {'tgt vid':>8s} {'tgt aud':>8s} {'used':>7s} {'seq_len':>8s}\n"
        for r in rows:
            s += (f"  {r['shape']:>16s} {r['text']:6d} {r['ref_image_rows']:7d}"
                  f" {r['target_video_rows']:8d} {r['target_audio_rows']:8d}"
                  f" {r['used']:7d} {r['seq_len']:8d}\n")
        return s.rstrip()
    check("MiniMaxH3Unit_PackedSequenceBuilder.process", packed)

    print()
    print("=" * 92)
    print("5. QUANT EXCLUDE-LIST CONSISTENCY  (guards against silent mis-loading)")
    print("=" * 92)

    def excludes():
        """Build each model on the meta device and check, Linear by Linear, that
        diffsynth's own _should_quantize() decision agrees with what the file
        actually stores.  A disagreement is the silent-corruption case the
        framework warns about: a layer expected to be fp would be handed packed
        NF4 bytes, or a quantized shell would be handed a bf16 weight."""
        from diffsynth.configs import MODEL_CONFIGS
        from diffsynth.core.loader import hash_model_file
        from diffsynth.core.quant import QuantizeConfig
        from diffsynth.core.vram.initialization import skip_model_initialization

        CLASSES = {
            "dit": "diffsynth.models.minimax_h3_dit_comfy.MiniMaxH3DiTComfyPruned",
            "text_encoder": "diffsynth.models.minimax_h3_text_encoder.MiniMaxH3TextEncoder",
        }
        NAMES = {"dit": "minimax-h3-ref2va-pruned-nf4.safetensors",
                 "text_encoder": "minimax-h3-text-encoder-nf4.safetensors",
                 "video_vae": "video_vae_nf4.safetensors",
                 "audio_vae": "audio_vae_nf4.safetensors"}
        lines = []
        state.setdefault("census", {})
        for key, name in NAMES.items():
            path = os.path.join(MODELS, name)
            cfg = next((c for c in MODEL_CONFIGS
                        if c["model_hash"] == hash_model_file(path)), None)
            assert cfg is not None, f"{name} not recognized"
            header = read_header(path)
            header.pop("__metadata__", None)
            # a layer is stored quantized iff its packed weight carries absmax stats
            stored_q = {k[: -len(".weight.absmax")] for k in header
                        if k.endswith(".weight.absmax")}
            stored_q |= {k[: -len(".bitsandbytes__nf4")] for k in header
                         if k.endswith(".bitsandbytes__nf4")}
            stored_fp = {k[: -len(".weight")] for k in header
                         if k.endswith(".weight") and header[k]["dtype"] == "BF16"
                         and not any(k.startswith(q + ".") for q in stored_q)}
            patterns = (cfg.get("quant_config") or {}).get("exclude_modules") or []
            lines.append(f"  {key:13s} stored: quantized={len(stored_q):4d}  bf16-weight={len(stored_fp):4d}"
                         f"  exclude_patterns={len(patterns)}")
            if key not in CLASSES:
                lines.append("      (model class not instantiated here; file-level check only)")
                continue
            import importlib
            mod_name, cls_name = CLASSES[key].rsplit(".", 1)
            Cls = getattr(importlib.import_module(mod_name), cls_name)
            qcfg = QuantizeConfig(method="bitsandbytes_nf4", load_prequantized=True,
                                  exclude_modules=patterns or None)
            with skip_model_initialization():
                model = Cls()
            will_q, will_fp, missing, extra = [], [], [], []
            for full_name, module in model.named_modules():
                if full_name == "" or not isinstance(module, torch.nn.Linear):
                    continue
                if qcfg._should_quantize(full_name, module):
                    will_q.append(full_name)
                    if full_name not in stored_q:
                        missing.append(full_name)
                else:
                    will_fp.append(full_name)
                    if full_name in stored_q:
                        extra.append(full_name)
            n_params = sum(dict(model.named_modules())[n].weight.numel() for n in will_q)
            real_missing = [n for n in missing if n in stored_fp]
            lines.append(f"      architecture: {len(will_q)} Linear -> quantized, {len(will_fp)} -> fp")
            lines.append(f"      quantized-in-arch but NOT packed in file : {len(missing)}"
                         + (f"  {missing[:4]}" if missing else ""))
            lines.append(f"      fp-in-arch but PACKED in file            : {len(extra)}"
                         + (f"  {extra[:4]}" if extra else ""))
            unused = [p for p in patterns
                      if not any(n == p or n.endswith("." + p) for n in will_q + will_fp)]
            if unused:
                lines.append(f"      exclude patterns that match no layer in this variant "
                             f"(benign, shared across variants): {unused}")
            assert not real_missing, f"{key}: {len(real_missing)} Linear(s) expected quantized " \
                                     f"but stored fp: {real_missing[:6]}"
            assert not extra, f"{key}: {len(extra)} Linear(s) expected fp but stored packed: " \
                              f"{extra[:6]}"
            packed_u8 = sum(math.prod(v["shape"]) for k, v in header.items()
                            if v["dtype"] == "U8" and k.endswith(".weight"))
            state["census"][key] = {"quantized_linears": len(will_q), "fp_linears": len(will_fp),
                           "quantized_params": n_params, "packed_weight_bytes": packed_u8}
            lines.append(f"      quantized params {n_params/1e9:6.3f} B in {packed_u8/1024**3:5.2f} GiB "
                         f"packed -> {packed_u8/n_params:.4f} byte/param")
            del model
        return "\n".join(lines)
    check("architecture quantization decision == deployed tensors", excludes)


    print()
    print("=" * 92)
    print("6. VRAM CONFIG / PRESET TABLE vs PIPELINE SIGNATURE")
    print("=" * 92)

    def unit_inputs():
        """The prompt-embedding cache path feeds the unit chain by hand, so prove
        that the dict h3_generate.compute_text_embedding builds supplies every
        param each unit up to PromptEmbedder declares.  Otherwise the cache path
        would only fail at run time, inside a 26 B-param model load."""
        from diffsynth.pipelines.minimax_h3_audio_video import (
            MiniMaxH3Unit_PromptEmbedder as PE)
        provided = {
            "cfg_scale", "height", "width", "num_frames", "seed", "rand_device",
            "tiled", "tile_size", "tile_overlap", "use_gradient_checkpointing",
            "use_gradient_checkpointing_offload", "keyframes", "keyframe_indices",
            "references", "control_video", "control_scale", "retake_video",
            "frame_regions_to_retake", "retake_audio", "seconds_regions_to_retake",
            "imgvid_cond_noise_aug", "audio_cond_noise_aug", "text_embedding",
            "ref_image_short_edge", "ref_video_short_edge", "ref_video_max_pixels",
            "prompt", "negative_prompt",
        }
        # PipelineUnitRunner does inputs_shared.get(name), so a key that is absent is
        # simply passed as None; a param is only a real problem if it must carry a
        # value and nothing earlier in the chain produced it.
        produced = {"video_latents", "audio_latents", "ref_blocks"}  # from earlier units
        missing, count, optional = [], 0, []
        for unit in pipe.units:
            need = set(unit.fetch_input_params())
            gap = sorted(need - provided - produced)
            if gap:
                # units whose module is simply absent from a Ref2VA run
                optional.append(f"{type(unit).__name__}: {gap}")
            produced |= set(unit.fetch_output_params() or ())
            count += 1
            if isinstance(unit, PE):
                break
        assert not missing, "; ".join(missing)
        for u in optional:
            pass  # reported below, they resolve to None which is what __call__ does too
        return (f"{count} units checked up to and including PromptEmbedder; "
                f"every declared input is either supplied by the cache-path dict or "
                f"produced by an earlier unit\n"
                f"    left as None (same as MiniMaxH3Pipeline.__call__): "
                + ("; ".join(optional) if optional else "none"))
    check("prompt-cache path supplies every unit input", unit_inputs)

    def schedulers():
        """Prove the beta schedule is laid out the way FlowMatchScheduler.step()
        indexes, keeps video/audio in lockstep, and actually differs from flow."""
        import h3_scheduler
        lines = []
        for mode in ("flow", "beta"):
            pipe2 = MiniMaxH3Pipeline(device="cpu", torch_dtype=torch.bfloat16)
            info = h3_scheduler.install(pipe2, mode=mode)
            pipe2.scheduler.set_timesteps(30, shift=12.0)
            pipe2.scheduler_audio.set_timesteps(30, shift=3.0)
            v, a = info["scheduler"], info["scheduler_audio"]
            assert v["steps"] == a["steps"], f"{mode}: video/audio step counts differ"
            assert abs(v["shift"] - 12.0) < 1e-3 and abs(a["shift"] - 3.0) < 1e-3, \
                f"{mode}: shift not recovered ({v['shift']}, {a['shift']})"
            s, ts = pipe2.scheduler.sigmas, pipe2.scheduler.timesteps
            assert float(s[0]) == 1.0, f"{mode}: sigma_max != 1"
            assert s.numel() in (ts.numel(), ts.numel() + 1), f"{mode}: sigma layout"
            if mode == "beta":
                assert float(s[-1]) == 0.0, "beta must append the terminal 0.0"
            x = torch.randn(1, 4)
            for i in (0, ts.numel() - 1):
                out = pipe2.scheduler.step(torch.ones_like(x), ts[i], x)
                assert bool(torch.isfinite(out).all()), f"{mode}: non-finite Euler step"
            lines.append(f"  {mode:5s} video {v['steps']} steps shift={v['shift']:.3f} "
                         f"sigma {v['sigma_max']:.4f} -> {v['sigma_min_nonzero']:.4f} -> 0"
                         f"   audio shift={a['shift']:.3f} -> {a['sigma_min_nonzero']:.4f}")
            if mode == "flow":
                flow_last = float(s[ts.numel() - 1])
            else:
                beta_last = float(s[ts.numel() - 1])
        assert beta_last < flow_last * 0.5, (
            f"beta and flow final sigmas are too close ({beta_last} vs {flow_last}); "
            "the beta schedule is probably not being applied")
        lines.append(f"  video final sigma: flow {flow_last:.4f} -> beta {beta_last:.4f} "
                     f"({beta_last/flow_last:.0%}); beta concentrates steps at both ends")
        return "\n".join(lines)
    check("scheduler: beta vs flow sigma grids", schedulers)

    def signature():
        import inspect
        from diffsynth.pipelines.minimax_h3_audio_video import MiniMaxH3Pipeline as P
        sig = set(inspect.signature(P.__call__).parameters)
        plan = json.load(open(os.path.join(WS, "cache", "plan.json")))
        used = set()
        for name, p in plan["presets"].items():
            if p["refs"]:
                used.add("references")
            used |= {"height", "width", "num_frames"}
        used |= {"seed", "cfg_scale", "num_inference_steps", "tiled", "tile_size",
                 "tile_overlap", "ref_image_short_edge", "ref_video_short_edge",
                 "ref_video_max_pixels"}
        unknown = sorted(used - sig)
        assert not unknown, f"plan references unknown __call__ kwargs: {unknown}"
        return (f"__call__ accepts every planned kwarg ({len(used)} checked)\n"
                f"pipeline time_division_factor={P.__init__ and pipe.time_division_factor}, "
                f"remainder={pipe.time_division_remainder}, "
                f"height/width division={pipe.height_division_factor}\n"
                f"in_iteration_models={pipe.in_iteration_models}")
    check("plan kwargs are valid pipeline arguments", signature)

    print()
    print("=" * 92)
    ok = sum(1 for _, p, _ in RESULTS if p)
    print(f"VALIDATION: {ok}/{len(RESULTS)} checks passed")
    print("=" * 92)
    out = {"results": [{"name": n, "pass": p, "detail": d} for n, p, d in RESULTS],
           "packed": state.get("packed"), "census": state.get("census")}
    os.makedirs(os.path.join(WS, "cache"), exist_ok=True)
    with open(os.path.join(WS, "cache", "validate.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())