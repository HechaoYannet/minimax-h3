#!/usr/bin/env python3
"""h3_bench.py -- hardware micro-benchmarks for the MiniMax-H3 workload on this box.

Pure measurement: no model weights are loaded, no diffusion inference is run.
It answers the questions that decide the VRAM / offload configuration:

  1. GPU + host facts (VRAM actually free, RAM, CPU, disk).
  2. Dense bf16 GEMM throughput at the shapes the H3 DiT actually uses.
  3. Which PyTorch SDPA backend serves the H3 attention shape (seq ~16k, 56 heads, d=128).
  4. Host<->device copy bandwidth (pageable vs pinned) -> cost of per-step layer streaming.
  5. bitsandbytes NF4 layer cost: dequant + GEMM for a real H3 projection.
  6. Sequential read throughput of the volume holding the weights.
"""
import json, os, statistics, sys, time

import torch

OUT = {}

def gb(x):
    return round(x / 1024 ** 3, 3)

def timed(fn, warmup=1, iters=3):
    for _ in range(warmup):
        fn()
    torch.cuda.synchronize()
    ts = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        torch.cuda.synchronize()
        ts.append(time.perf_counter() - t0)
    return min(ts), statistics.median(ts)


def section(name):
    print("\n" + "=" * 78)
    print(name)
    print("=" * 78)


# ---------------------------------------------------------------- 1. facts
section("1. HOST / DEVICE FACTS")
props = torch.cuda.get_device_properties(0)
free_b, total_b = torch.cuda.mem_get_info()
OUT["gpu"] = {
    "name": props.name,
    "capability": f"{props.major}.{props.minor}",
    "sm_count": props.multi_processor_count,
    "vram_total_gb": gb(total_b),
    "vram_free_gb": gb(free_b),
    "vram_reserved_after_init_gb": gb(total_b - free_b),
    "torch": torch.__version__,
    "cuda": torch.version.cuda,
    "arch_list": torch.cuda.get_arch_list(),
    "cudnn": torch.backends.cudnn.version(),
}
for k, v in OUT["gpu"].items():
    print(f"  {k:30s} {v}")

try:
    with open("/proc/meminfo") as f:
        mem = {l.split(":")[0]: int(l.split()[1]) for l in f if ":" in l}
    OUT["host_mem"] = {
        "MemTotal_gb": round(mem["MemTotal"] / 1024 ** 2, 2),
        "MemAvailable_gb": round(mem["MemAvailable"] / 1024 ** 2, 2),
        "SwapTotal_gb": round(mem.get("SwapTotal", 0) / 1024 ** 2, 2),
        "Cached_gb": round(mem.get("Cached", 0) / 1024 ** 2, 2),
    }
    print(f"  host RAM                     {OUT['host_mem']}")
except Exception as e:  # pragma: no cover
    print("  /proc/meminfo unavailable:", e)

OUT["cpu_threads"] = os.cpu_count()
print(f"  cpu_threads                  {OUT['cpu_threads']}")
print(f"  torch.get_num_threads()      {torch.get_num_threads()}")

# attention backend selection as diffsynth would pick it
sys.path.insert(0, os.environ.get("H3_REPO", "/home/yhc/source/minimax-h3/DiffSynth-Studio"))
try:
    from diffsynth.core.attention import attention as _attn_mod
    OUT["attention_implementation"] = _attn_mod.ATTENTION_IMPLEMENTATION
    print(f"  diffsynth attention backend  {_attn_mod.ATTENTION_IMPLEMENTATION}")
    for flag in ("FLASH_ATTN_4_AVAILABLE", "FLASH_ATTN_3_AVAILABLE", "FLASH_ATTN_2_AVAILABLE",
                 "SAGE_ATTN_AVAILABLE", "XFORMERS_AVAILABLE", "FLEX_ATTN_AVAILABLE", "TORCH_SUPPORT_GQA"):
        print(f"    {flag:28s} {getattr(_attn_mod, flag, None)}")
except Exception as e:
    OUT["attention_implementation"] = f"unavailable: {type(e).__name__}: {e}"
    print("  diffsynth attention backend  unavailable:", e)

# ---------------------------------------------------------------- 2. GEMM
section("2. DENSE bf16 GEMM THROUGHPUT  (H3 DiT projections)")
# (label, in_features, out_features, tokens)
SHAPES = [
    ("attn.qkv_proj  5376->21504", 5376, 21504, 16000),
    ("attn.out_proj  5376->5376 ", 5376, 5376, 16000),
    ("mlp.fc1        5376->14336", 5376, 14336, 16000),
    ("mlp.fc2       14336->5376 ", 14336, 5376, 16000),
    ("refiner qkv    5376->21504", 5376, 21504, 1500),
]
OUT["gemm"] = []
for label, i, o, n in SHAPES:
    a = torch.randn(n, i, dtype=torch.bfloat16, device="cuda")
    w = torch.randn(o, i, dtype=torch.bfloat16, device="cuda")
    best, med = timed(lambda: torch.nn.functional.linear(a, w))
    flops = 2.0 * n * i * o
    tflops = flops / best / 1e12
    OUT["gemm"].append({"shape": label, "tokens": n, "ms": round(best * 1e3, 2), "tflops": round(tflops, 1)})
    print(f"  {label}  N={n:6d}  {best*1e3:8.2f} ms  {tflops:6.1f} TFLOP/s")
    del a, w
    torch.cuda.empty_cache()

# full-DiT step estimate assuming 50 blocks of the above
per_block_flops = 2 * 16000 * (5376 * 21504 + 5376 * 5376 + 5376 * 14336 + 14336 * 5376)
OUT["dit_step_estimate"] = {
    "params_billion_pruned": round((5376 * 21504 + 5376 * 5376 + 5376 * 14336 + 14336 * 5376) * 50 / 1e9, 2),
    "flops_per_step_tflop": round(per_block_flops * 50 / 1e12, 1),
    "tokens": 16000,
}
print(f"  -> pruned DiT linear params      {OUT['dit_step_estimate']['params_billion_pruned']} B")
print(f"  -> linear FLOPs per denoise step {OUT['dit_step_estimate']['flops_per_step_tflop']} TFLOP (N=16000)")

# ---------------------------------------------------------------- 3. SDPA
section("3. SDPA BACKENDS FOR THE H3 ATTENTION SHAPE")
q = torch.randn(1, 56, 16000, 128, dtype=torch.bfloat16, device="cuda")
k = torch.randn_like(q)
v = torch.randn_like(q)
from torch.nn.attention import SDPBackend, sdpa_kernel
BACKENDS = {
    "FLASH_ATTENTION": SDPBackend.FLASH_ATTENTION,
    "EFFICIENT_ATTENTION": SDPBackend.EFFICIENT_ATTENTION,
    "CUDNN_ATTENTION": SDPBackend.CUDNN_ATTENTION,
    "MATH": SDPBackend.MATH,
}
OUT["sdpa"] = {}
for name, be in BACKENDS.items():
    try:
        with sdpa_kernel(be):
            best, med = timed(lambda: torch.nn.functional.scaled_dot_product_attention(q, k, v), warmup=1, iters=3)
        OUT["sdpa"][name] = {"ms": round(best * 1e3, 2), "ok": True}
        print(f"  {name:22s} OK   {best*1e3:9.2f} ms   ({(4*56*16000*16000*128)/best/1e12:6.1f} TFLOP/s)")
    except Exception as e:
        OUT["sdpa"][name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        print(f"  {name:22s} FAIL {type(e).__name__}: {str(e)[:90]}")
del q, k, v
torch.cuda.empty_cache()

# ---------------------------------------------------------------- 4. PCIe
section("4. HOST<->DEVICE COPY BANDWIDTH  (cost of per-step layer streaming)")
OUT["pcie"] = {}
N = 256 * 1024 * 1024  # 256 MiB
cpu = torch.empty(N, dtype=torch.uint8, pin_memory=False)
gpu = torch.empty(N, dtype=torch.uint8, device="cuda")
best, _ = timed(lambda: gpu.copy_(cpu, non_blocking=False), warmup=0, iters=3)
OUT["pcie"]["h2d_pageable_gbs"] = round(N / best / 1e9, 2)
print(f"  H2D pageable   {best*1e3:8.2f} ms  {N/best/1e9:6.2f} GB/s")
best, _ = timed(lambda: cpu.copy_(gpu, non_blocking=False), warmup=0, iters=3)
OUT["pcie"]["d2h_pageable_gbs"] = round(N / best / 1e9, 2)
print(f"  D2H pageable   {best*1e3:8.2f} ms  {N/best/1e9:6.2f} GB/s")
try:
    cpu_p = torch.empty(N, dtype=torch.uint8, pin_memory=True)
    best, _ = timed(lambda: gpu.copy_(cpu_p, non_blocking=True), warmup=0, iters=3)
    OUT["pcie"]["h2d_pinned_gbs"] = round(N / best / 1e9, 2)
    print(f"  H2D pinned     {best*1e3:8.2f} ms  {N/best/1e9:6.2f} GB/s")
    del cpu_p
except Exception as e:
    print("  pinned buffer unavailable:", e)
del cpu, gpu
torch.cuda.empty_cache()

# ---------------------------------------------------------------- 5. NF4
section("5. bitsandbytes NF4 LAYER COST  (dequant + GEMM, as the DiT runs it)")
OUT["nf4"] = []
try:
    import bitsandbytes as bnb
    OUT["bitsandbytes"] = bnb.__version__
    print(f"  bitsandbytes {bnb.__version__}")
    for label, i, o, n in [("qkv   5376->21504", 5376, 21504, 16000),
                           ("out   5376->5376 ", 5376, 5376, 16000),
                           ("fc1   5376->14336", 5376, 14336, 16000)]:
        lin = torch.nn.Linear(i, o, bias=True, dtype=torch.bfloat16, device="cuda")
        q4 = bnb.nn.Linear4bit(i, o, bias=True, compute_dtype=torch.bfloat16,
                               compress_statistics=True, quant_type="nf4").cuda()
        q4.weight = bnb.nn.Params4bit(lin.weight.data, requires_grad=False,
                                      compress_statistics=True, quant_type="nf4").to("cuda")
        q4.bias = torch.nn.Parameter(lin.bias.data, requires_grad=False)
        x = torch.randn(n, i, dtype=torch.bfloat16, device="cuda")
        packed = q4.weight.numel() * q4.weight.element_size()
        best, _ = timed(lambda: q4(x), warmup=1, iters=3)
        flops = 2.0 * n * i * o
        OUT["nf4"].append({"shape": label, "ms": round(best * 1e3, 2),
                           "tflops": round(flops / best / 1e12, 1),
                           "packed_mb": round(packed / 1024 ** 2, 1)})
        print(f"  {label} N={n}  {best*1e3:8.2f} ms  {flops/best/1e12:5.1f} TFLOP/s  packed={packed/1024**2:7.1f} MB")
        del lin, q4, x
        torch.cuda.empty_cache()
except Exception as e:
    OUT["nf4_error"] = f"{type(e).__name__}: {e}"
    print("  bitsandbytes NF4 benchmark failed:", type(e).__name__, e)

# ---------------------------------------------------------------- 6. disk
section("6. SEQUENTIAL READ OF THE WEIGHTS VOLUME")
OUT["disk"] = {}
try:
    import subprocess
    for path in ["/home/yhc/source/minimax-h3/models/minimax-h3-ref2va-pruned-nf4.safetensors"]:
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        t0 = time.perf_counter()
        with open(path, "rb") as f:
            total = 0
            while total < 2 * 1024 ** 3:
                b = f.read(8 * 1024 * 1024)
                if not b:
                    break
                total += len(b)
        dt = time.perf_counter() - t0
        OUT["disk"]["cold_read_gbs"] = round(total / dt / 1e9, 2)
        OUT["disk"]["file_gb"] = round(size / 1024 ** 3, 2)
        print(f"  read {total/1024**3:.1f} GiB in {dt:.2f}s -> {total/dt/1e9:.2f} GB/s")
except Exception as e:
    print("  disk benchmark failed:", e)

# ---------------------------------------------------------------- 3b. SDPA in the REAL layout
section("3b. SDPA IN THE LAYOUT THE H3 DiT ACTUALLY PASSES")
print("  _sdpa_varlen_attention() slices q[start:stop] then .transpose(0,1).unsqueeze(0)")
print("  -> [1, heads, seq, dim] non-contiguous views (not [1, heads, seq, dim] contiguous)")
OUT["sdpa_real_layout"] = {}
for S in (7744, 17856):
    qc = torch.randn(S, 56, 128, dtype=torch.bfloat16, device="cuda")
    kc = torch.randn(S, 56, 128, dtype=torch.bfloat16, device="cuda")
    vc = torch.randn(S, 56, 128, dtype=torch.bfloat16, device="cuda")
    qv = qc.transpose(0, 1).unsqueeze(0)
    kv = kc.transpose(0, 1).unsqueeze(0)
    vv = vc.transpose(0, 1).unsqueeze(0)
    print(f"  seq={S}")
    OUT["sdpa_real_layout"][S] = {}
    for name, be in BACKENDS.items():
        if name == "MATH":
            continue
        try:
            with sdpa_kernel(be):
                best, _ = timed(lambda: torch.nn.functional.scaled_dot_product_attention(qv, kv, vv), warmup=1, iters=3)
            OUT["sdpa_real_layout"][S][name] = round(best * 1e3, 2)
            print(f"    view   {name:20s} {best*1e3:9.2f} ms  {(4*56*S*S*128)/best/1e12:6.1f} TFLOP/s")
        except Exception as e:
            OUT["sdpa_real_layout"][S][name] = f"FAIL {type(e).__name__}"
            print(f"    view   {name:20s} FAIL {type(e).__name__}: {str(e)[:60]}")
    try:
        with sdpa_kernel(SDPBackend.FLASH_ATTENTION):
            best, _ = timed(lambda: torch.nn.functional.scaled_dot_product_attention(
                qv.contiguous(), kv.contiguous(), vv.contiguous()), warmup=1, iters=3)
        OUT["sdpa_real_layout"][S]["contig_FLASH_ATTENTION"] = round(best * 1e3, 2)
        print(f"    contig FLASH_ATTENTION   {best*1e3:9.2f} ms  (copy not included)")
    except Exception as e:
        print("    contig flash failed:", e)
    del qc, kc, vc, qv, kv, vv
    torch.cuda.empty_cache()

# ---------------------------------------------------------------- 4b. pinned, blocking
section("4b. LAYER-STREAMING COST  (what the vram_limit fallback really pays)")
try:
    cpu_p2 = torch.empty(N, dtype=torch.uint8, pin_memory=True)
    gpu2 = torch.empty(N, dtype=torch.uint8, device="cuda")
    best, _ = timed(lambda: gpu2.copy_(cpu_p2, non_blocking=False), warmup=0, iters=3)
    OUT["pcie"]["h2d_pinned_blocking_gbs"] = round(N / best / 1e9, 2)
    print(f"  H2D pinned (blocking)  {best*1e3:8.2f} ms  {N/best/1e9:6.2f} GB/s")
    del cpu_p2, gpu2
    torch.cuda.empty_cache()
except Exception as e:
    print("  pinned blocking test unavailable:", e)

OUT["stream_cost"] = []
try:
    import copy as _copy
    for label, i, o in [("qkv   5376->21504", 5376, 21504),
                        ("out   5376->5376 ", 5376, 5376),
                        ("fc1   5376->14336", 5376, 14336),
                        ("fc2  14336->5376 ", 14336, 5376)]:
        src = bnb.nn.Linear4bit(i, o, bias=True, compute_dtype=torch.bfloat16,
                                compress_statistics=True, quant_type="nf4")
        src.weight = bnb.nn.Params4bit(torch.empty(o, i, dtype=torch.bfloat16),
                                       requires_grad=False, compress_statistics=True,
                                       quant_type="nf4", blocksize=64)
        src = src.cpu()
        packed = src.weight.numel() * src.weight.element_size()
        best, _ = timed(lambda: _copy.deepcopy(src).to("cuda"), warmup=1, iters=3)
        OUT["stream_cost"].append({"layer": label, "packed_mb": round(packed / 1024 ** 2, 1),
                                   "ms": round(best * 1e3, 2),
                                   "gbs": round(packed / best / 1e9, 2)})
        print(f"  deepcopy+cuda {label}  packed={packed/1024**2:7.1f} MB  {best*1e3:8.2f} ms  {packed/best/1e9:5.2f} GB/s")
        del src
        torch.cuda.empty_cache()
    if OUT["stream_cost"]:
        full = sum(c["ms"] for c in OUT["stream_cost"])
        OUT["stream_cost_full_block_ms"] = round(full, 1)
        print(f"  -> one full DiT block (4 projections) streams in {full:.1f} ms;"
              f" 50 blocks = {full*50/1000:.1f} s per denoise step")
except Exception as e:
    OUT["stream_cost_error"] = f"{type(e).__name__}: {e}"
    print("  stream cost test failed:", type(e).__name__, e)

section("SUMMARY (json)")
print(json.dumps(OUT, indent=2))
with open(os.environ.get("H3_BENCH_OUT", "/mnt/d/otherProject/minimax-h3/cache/bench.json"), "w") as f:
    json.dump(OUT, f, indent=2)