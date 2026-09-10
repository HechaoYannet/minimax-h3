#!/usr/bin/env python3
"""h3_scheduler.py -- step-placement ("scheduler") options for MiniMax-H3.

Why this file exists
--------------------
The AfterMidnight Ref2VA LoRA carries the requirement:

    "use euler sampler and beta scheduler, or you will get weird audio problems"

Half of that is already true in diffsynth: FlowMatchScheduler.step() is a plain
first-order Euler update,

    prev_sample = sample + model_output * (sigma_next - sigma)

which is the same integrator ComfyUI's sample_euler uses.  What differs is the
*placement of the sigma grid*, and that is what "beta scheduler" changes.

diffsynth's MiniMax-H3 schedule (set_timesteps_minimax_h3) is uniform in the
pre-shift domain:

    u      = linspace(1, 0, n+1)[:-1]          # 1, 1-1/n, ..., 1/n
    sigma  = shift * u / (1 + (shift-1) * u)   # the standard flow SNR shift

ComfyUI's "beta" scheduler keeps that same shifted sigma table but selects the
n steps at beta(alpha, beta) quantiles, concentrating steps at both ends of the
trajectory.  Translated from comfy/samplers.py:

    def beta_scheduler(model_sampling, steps, alpha=0.6, beta=0.6):
        total_timesteps = (len(model_sampling.sigmas) - 1)
        ts = 1 - numpy.linspace(0, 1, steps, endpoint=False)
        ts = numpy.rint(scipy.stats.beta.ppf(ts, alpha, beta) * total_timesteps)
        sigs = []                                    # dedup, keep first occurrence
        last_t = -1
        for t in ts:
            if t != last_t:
                sigs += [float(model_sampling.sigmas[int(t)])]
            last_t = t
        sigs += [0.0]
        return torch.FloatTensor(sigs)

with model_sampling.sigmas ascending (sigmas[0] == sigma_min, sigmas[-1] ==
sigma_max) as built by ModelSamplingDiscreteFlow.set_parameters.  The result is a
*descending* list ending in 0.0 -- exactly the layout FlowMatchScheduler.step()
expects.

This module reproduces that, keeping whatever shift the pipeline itself passes
(12.0 for video, 3.0 for audio), so only the step placement changes.

Run directly to see the sigma grids side by side.
"""
from __future__ import annotations

import math

import torch

# ComfyUI's ModelSamplingDiscreteFlow builds its table from arange(1, 1001)/1000.
TABLE_SIZE = 1000
DEFAULT_ALPHA = 0.6
DEFAULT_BETA = 0.6


def shifted_sigma_table(shift: float, table_size: int = TABLE_SIZE) -> torch.Tensor:
    """Ascending sigma table, exactly ComfyUI's ModelSamplingDiscreteFlow.sigmas."""
    u = torch.arange(1, table_size + 1, dtype=torch.float64) / table_size
    return shift * u / (1.0 + (shift - 1.0) * u)


def _beta_ppf(q: torch.Tensor, alpha: float, beta: float) -> torch.Tensor:
    """Inverse regularised incomplete beta function (== scipy.stats.beta.ppf).

    scipy is available (librosa pulls it in), but this fallback keeps the module
    importable anywhere: vectorised bisection on torch.special.betainc, which is
    accurate to ~1e-12 over the 1000-entry index grid we actually need."""
    try:
        from scipy.stats import beta as _scipy_beta
        return torch.as_tensor(_scipy_beta.ppf(q.numpy(), alpha, beta), dtype=torch.float64)
    except Exception:
        pass
    lo = torch.full_like(q, 1e-12)
    hi = torch.ones_like(q)
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        cdf = torch.special.betainc(torch.as_tensor(alpha, dtype=torch.float64),
                                    torch.as_tensor(beta, dtype=torch.float64), mid)
        lo = torch.where(cdf < q, mid, lo)
        hi = torch.where(cdf < q, hi, mid)
    return 0.5 * (lo + hi)


def beta_sigmas(num_inference_steps: int, shift: float, alpha: float = DEFAULT_ALPHA,
                beta: float = DEFAULT_BETA, table_size: int = TABLE_SIZE,
                denoising_strength: float = 1.0):
    """Descending sigmas whose steps sit at beta(alpha, beta) quantiles.

    Returns (sigmas, timesteps) with len(sigmas) == len(timesteps) + 1 and
    sigmas[-1] == 0.0, matching what FlowMatchScheduler.step() indexes."""
    table = shifted_sigma_table(shift, table_size)
    # numpy.linspace(0, 1, n, endpoint=False).  torch.linspace has no 'endpoint'
    # kwarg in this build, so build the ramp with arange instead.
    q = 1.0 - torch.arange(num_inference_steps, dtype=torch.float64) / num_inference_steps
    idx = torch.round(_beta_ppf(q, alpha, beta) * (table_size - 1)).to(torch.long)
    idx = idx.clamp(0, table_size - 1)
    keep = torch.ones_like(idx, dtype=torch.bool)
    keep[1:] = idx[1:] != idx[:-1]          # ComfyUI's "if t != last_t" dedup
    idx = idx[keep]
    if denoising_strength < 1.0:
        cap = shift * denoising_strength / (1.0 + (shift - 1.0) * denoising_strength)
        idx = idx[table[idx] <= cap]
    sig = table[idx]
    timesteps = (sig * 1000.0).to(torch.float32)
    sigmas = torch.cat([sig, torch.zeros(1, dtype=torch.float64)]).to(torch.float32)
    return sigmas, timesteps


def flow_sigmas(num_inference_steps: int, shift: float, denoising_strength: float = 1.0):
    """diffsynth's own set_timesteps_minimax_h3, reproduced for comparison."""
    base = torch.linspace(denoising_strength, 0.0, num_inference_steps + 1,
                          dtype=torch.float64)[:-1]
    sig = shift * base / (1.0 + (shift - 1.0) * base)
    timesteps = (sig * 1000.0).to(torch.float32)
    sigmas = torch.cat([sig, torch.zeros(1, dtype=torch.float64)]).to(torch.float32)
    return sigmas, timesteps


def install(pipe, mode: str = "beta", alpha: float = DEFAULT_ALPHA,
            beta: float = DEFAULT_BETA):
    """Wrap pipe.scheduler / pipe.scheduler_audio so their sigma grids become beta.

    The pipeline calls scheduler.set_timesteps(n, shift=...) itself, so the shift
    it chose (12.0 video / 3.0 audio) is preserved -- only the step placement
    changes.  Both schedulers get the *same* beta index sequence, so the video
    loop and the audio lookup by progress_id stay in lockstep.
    """
    info = {"mode": mode, "alpha": alpha, "beta": beta}
    for name, sched in (("scheduler", pipe.scheduler), ("scheduler_audio", pipe.scheduler_audio)):
        original = sched.set_timesteps

        def make(original=original, sched=sched, name=name):
            def set_timesteps(num_inference_steps=100, denoising_strength=1.0,
                              training=False, **kw):
                original(num_inference_steps=num_inference_steps,
                         denoising_strength=denoising_strength,
                         training=training, **kw)
                if training:
                    return
                shift = kw.get("shift")
                if shift is None:
                    shift = _recover_shift(sched.sigmas, num_inference_steps,
                                           denoising_strength)
                if mode == "beta":
                    sig, ts = beta_sigmas(num_inference_steps, float(shift), alpha, beta,
                                          denoising_strength=denoising_strength)
                    sched.sigmas, sched.timesteps = sig, ts
                else:
                    sig, ts = sched.sigmas, sched.timesteps
                # beta appends the terminal 0.0 (ComfyUI layout); diffsynth's own
                # schedule does not and lets step() force sigma_ = 0 on the last step.
                last = float(sig[-2]) if sig.numel() == ts.numel() + 1 else float(sig[-1])
                info[name] = {"shift": float(shift), "steps": int(ts.numel()),
                              "sigma_max": float(sig[0]), "sigma_min_nonzero": last,
                              "has_terminal_zero": bool(float(sig[-1]) == 0.0)}
            return set_timesteps

        sched.set_timesteps = make()
    return info


def _recover_shift(sigmas, num_inference_steps, denoising_strength=1.0):
    """Invert sigma = shift*b/(1+(shift-1)*b) from the framework's own schedule."""
    b = (num_inference_steps - 1) / num_inference_steps if num_inference_steps > 1 else 0.5
    s = float(sigmas[1])
    return s * (1 - b) / (b * (1 - s))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--video-shift", type=float, default=12.0)
    ap.add_argument("--audio-shift", type=float, default=3.0)
    ap.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    ap.add_argument("--beta", type=float, default=DEFAULT_BETA)
    a = ap.parse_args()

    for label, shift in (("video", a.video_shift), ("audio", a.audio_shift)):
        f_sig, f_ts = flow_sigmas(a.steps, shift)
        b_sig, b_ts = beta_sigmas(a.steps, shift, a.alpha, a.beta)
        print("=" * 84)
        print(f"{label}: shift={shift}  steps={a.steps}")
        print(f"  flow (diffsynth default): {f_ts.numel()} steps, "
              f"sigma {float(f_sig[0]):.6f} -> {float(f_sig[-2]):.6f} -> 0")
        print(f"  beta({a.alpha}, {a.beta})          : {b_ts.numel()} steps, "
              f"sigma {float(b_sig[0]):.6f} -> {float(b_sig[-2]):.6f} -> 0")
        print()
        print(f"  {'i':>3s} {'flow sigma':>12s} {'beta sigma':>12s}   "
              f"{'flow dsigma':>12s} {'beta dsigma':>12s}")
        n = max(f_ts.numel(), b_ts.numel())
        for i in range(n):
            fs = float(f_sig[i]) if i < f_sig.numel() else float("nan")
            bs = float(b_sig[i]) if i < b_sig.numel() else float("nan")
            fd = float(f_sig[i + 1] - f_sig[i]) if i + 1 < f_sig.numel() else float("nan")
            bd = float(b_sig[i + 1] - b_sig[i]) if i + 1 < b_sig.numel() else float("nan")
            print(f"  {i:3d} {fs:12.6f} {bs:12.6f}   {fd:12.6f} {bd:12.6f}")
        print()


if __name__ == "__main__":
    main()
