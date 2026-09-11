"""webui.backend.telemetry -- 硬件占用与系统信息采集。

只用标准库 + nvidia-smi，**不 import torch**（见 util.py 顶部的说明）。
nvidia-smi 在 WSL 里的路径是 /usr/lib/wsl/lib/nvidia-smi，实测单次约 30 ms，
所以 1.5 s 轮询一次的开销可以忽略。

采集内容：
  GPU   利用率 / 显存(含进程占用) / 温度 / 功耗 / 频率 / 显存控制器利用率
  RAM   WSL 内部总量/可用/缓存，以及它对宿主内存的配额（cgroup v2 memory.max）
  CPU   总体利用率（/proc/stat 差分）、负载、核数
  磁盘  仓库所在分区的剩余空间（输出 mp4 要写盘）
"""
from __future__ import annotations

import collections
import os
import subprocess
import threading
import time

from .util import iso, now

_GIB = 1024 ** 3
_MIB = 1024 ** 2


def _run(cmd: list[str], timeout: float = 4.0) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timeout after {timeout}s"
    except Exception as e:  # pragma: no cover
        return 1, "", str(e)


# --------------------------------------------------------------------------- GPU
_GPU_FIELDS = [
    ("utilization.gpu", "util_pct", int),
    ("memory.used", "vram_used_mib", int),
    ("memory.total", "vram_total_mib", int),
    ("temperature.gpu", "temp_c", int),
    ("power.draw", "power_w", float),
    ("clocks.sm", "clock_sm_mhz", int),
    ("clocks.max.sm", "clock_max_mhz", int),
    ("utilization.memory", "mem_util_pct", int),
    ("pstate", "pstate", str),
    ("name", "name", str),
]
_MIN_FIELDS = ["utilization.gpu", "memory.used", "memory.total", "temperature.gpu"]


def _parse_value(raw: str, caster):
    v = raw.strip()
    if v in ("", "[N/A]", "N/A", "[Not Supported]"):
        return None
    if caster is str:
        return v
    try:
        return caster(float(v.split()[0]))
    except Exception:
        return None


def read_gpu(nvidia_smi: str = "nvidia-smi") -> dict:
    fields = [f for f, _, _ in _GPU_FIELDS]
    rc, out, err = _run([nvidia_smi,
                         "--query-gpu=" + ",".join(fields),
                         "--format=csv,noheader,nounits"])
    if rc != 0 or not out:
        # 退到最小字段集：某些驱动/虚拟化环境不支持 clocks/pstate 查询
        rc2, out2, err2 = _run([nvidia_smi,
                                "--query-gpu=" + ",".join(_MIN_FIELDS),
                                "--format=csv,noheader,nounits"])
        if rc2 != 0 or not out2:
            return {"ok": False, "error": (err or err2 or "nvidia-smi failed").strip()[:300]}
        fields, out = _MIN_FIELDS, out2
    line = out.splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    gpu: dict = {"ok": True}
    for (field, key, caster), raw in zip(_GPU_FIELDS[: len(fields)], parts):
        gpu[key] = _parse_value(raw, caster)
    if gpu.get("vram_total_mib"):
        gpu["vram_total_gib"] = round(gpu["vram_total_mib"] / 1024, 2)
        gpu["vram_used_gib"] = round((gpu.get("vram_used_mib") or 0) / 1024, 2)
        gpu["vram_free_gib"] = round(gpu["vram_total_gib"] - gpu["vram_used_gib"], 2)
    return gpu


def gpu_processes(nvidia_smi: str = "nvidia-smi") -> list[dict]:
    """谁在占显存。生成作业占满时会很有用（判断是不是被上一次的僵尸进程占着）。"""
    rc, out, _ = _run([nvidia_smi,
                       "--query-compute-apps=pid,process_name,used_memory",
                       "--format=csv,noheader,nounits"])
    if rc != 0 or not out:
        return []
    procs = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                procs.append({"pid": int(parts[0]), "name": os.path.basename(parts[1]),
                              "vram_mib": int(float(parts[2]))})
            except ValueError:
                continue
    return procs


# --------------------------------------------------------------------------- CPU / RAM
def _cpu_times() -> tuple[int, int]:
    with open("/proc/stat") as f:
        parts = f.readline().split()
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


_prev_cpu: tuple[float, int, int] | None = None


def cpu_util() -> float | None:
    """两次采样之间的总体 CPU 利用率（%）。首次调用返回 None。"""
    global _prev_cpu
    total, idle = _cpu_times()
    t = time.time()
    pct = None
    if _prev_cpu is not None:
        pt, ptotal, pidle = _prev_cpu
        dtotal, didle = total - ptotal, idle - pidle
        if dtotal > 0 and t - pt > 0.05:
            pct = round(100.0 * (1 - didle / dtotal), 1)
    _prev_cpu = (t, total, idle)
    return pct


def read_mem() -> dict:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _sep, v = line.partition(":")
                info[k.strip()] = int(v.strip().split()[0]) * 1024  # kB -> B
    except OSError:
        return {"ok": False, "error": "/proc/meminfo unreadable"}
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    mem = {
        "ok": True,
        "total_gib": round(total / _GIB, 2),
        "used_gib": round((total - avail) / _GIB, 2),
        "avail_gib": round(avail / _GIB, 2),
        "cached_gib": round(info.get("Cached", 0) / _GIB, 2),
        "swap_total_gib": round(swap_total / _GIB, 2),
        "swap_used_gib": round((swap_total - swap_free) / _GIB, 2),
        "used_pct": round(100.0 * (total - avail) / total, 1) if total else None,
    }
    # WSL 对宿主内存的配额（cgroup v2）。README §6 里的 22.91 GiB 就是它。
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(path).read().strip()
            if raw.isdigit():
                mem["quota_gib"] = round(int(raw) / _GIB, 2)
                break
        except OSError:
            continue
    try:
        with open("/proc/loadavg") as f:
            la = f.read().split()
        mem["loadavg"] = [float(x) for x in la[:3]]
    except Exception:
        pass
    mem["cpu_count"] = os.cpu_count()
    return mem


def read_disk(path: str) -> dict:
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"ok": True, "path": path,
                "total_gib": round(total / _GIB, 2),
                "free_gib": round(free / _GIB, 2),
                "used_pct": round(100.0 * (total - free) / total, 1) if total else None}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# --------------------------------------------------------------------------- 采样线程
class Telemetry:
    """后台按固定间隔采样，保留一段历史给曲线图；当前值可随时取。"""

    def __init__(self, nvidia_smi: str = "nvidia-smi", interval: float = 1.5,
                 history: int = 240, repo_root: str = "."):
        self.nvidia_smi = nvidia_smi
        self.interval = max(0.5, float(interval or 1.5))
        self.history = collections.deque(maxlen=history)
        self.repo_root = repo_root
        self._lock = threading.Lock()
        self._cur: dict = {"ts": now(), "iso": iso(), "gpu": {"ok": False, "error": "not sampled yet"}}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._err_streak = 0

    # -- 生命周期
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="telemetry", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                snap = self._sample()
                with self._lock:
                    self._cur = snap
                    self.history.append(snap)
            except Exception as e:  # 采集失败不能让服务挂
                import traceback
                tb = traceback.format_exc().strip().splitlines()
                with self._lock:
                    self._cur = {"ts": now(), "iso": iso(),
                                 "error": f"telemetry sample failed: {e}"[:300],
                                 "trace": tb[-6:]}
                print("[telemetry] sample failed:\n" + "\n".join(tb), flush=True)
            dt = time.time() - t0
            self._stop.wait(max(0.5, self.interval - dt))

    def _sample(self) -> dict:
        gpu = read_gpu(self.nvidia_smi)
        if gpu.get("ok"):
            self._err_streak = 0
            gpu["processes"] = gpu_processes(self.nvidia_smi)
        else:
            self._err_streak += 1
        mem = read_mem()
        mem["cpu_util_pct"] = cpu_util()
        return {"ts": now(), "iso": iso(), "gpu": gpu, "mem": mem,
                "disk": read_disk(self.repo_root),
                "sampler_errors": self._err_streak}

    # -- 读取
    def current(self) -> dict:
        with self._lock:
            return dict(self._cur)

    def history_list(self, n: int = 120) -> list[dict]:
        with self._lock:
            items = list(self.history)[-n:]
        return [{"ts": s.get("ts"), "gpu_util": (s.get("gpu") or {}).get("util_pct"),
                 "vram_used_gib": (s.get("gpu") or {}).get("vram_used_gib"),
                 "mem_used_gib": (s.get("mem") or {}).get("used_gib"),
                 "cpu": (s.get("mem") or {}).get("cpu_util_pct")} for s in items]

    def peaks(self) -> dict:
        with self._lock:
            items = list(self.history)
        vram = [((s.get("gpu") or {}).get("vram_used_gib") or 0) for s in items]
        ram = [((s.get("mem") or {}).get("used_gib") or 0) for s in items]
        return {"vram_peak_gib": round(max(vram), 2) if vram else None,
                "ram_peak_gib": round(max(ram), 2) if ram else None,
                "samples": len(items)}
