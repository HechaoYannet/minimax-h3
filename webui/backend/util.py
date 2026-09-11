"""webui.backend.util -- 无第三方依赖的小工具集合。

设计约束：整个后端只允许用 Python 标准库 + (可选) PyYAML，绝不 import torch。
原因是这台机器上 WSL 只有 22.9 GiB 内存，而生成作业本身峰值就要 11 GiB；
后端进程多占一点，生成就多一分被 OOM kill 的风险。
"""
from __future__ import annotations

import json
import os
import re
import time


# --------------------------------------------------------------------------- 路径
def repo_root() -> str:
    """仓库根目录（本文件是 <root>/webui/backend/util.py）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def wsl_to_windows(path: str) -> str:
    """/mnt/d/x -> D:\\x ；不是 /mnt/<盘>/ 形式就原样返回。"""
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", path)
    if not m:
        return path
    return m.group(1).upper() + ":\\" + m.group(2).replace("/", "\\")


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def safe_rel(path: str, root: str) -> str:
    """把绝对路径表示成相对 root 的路径（跨 Windows/WSL 两个视角都能用）。"""
    try:
        r = os.path.relpath(path, root)
    except ValueError:
        return path
    return path if r.startswith("..") else r


# --------------------------------------------------------------------------- 时间
def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts if ts else now()))


def human_dur(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds < 0:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


# --------------------------------------------------------------------------- JSON
# 运行日志见 runlog.py：结构化、可落盘、可订阅。这里只留与日志无关的小工具。
def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        return default


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_jsonl(path: str, obj) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()


def tail_lines(path: str, n: int = 200) -> list[str]:
    """读文件末尾 n 行；文件很大时只读尾部 256 KiB。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    try:
        with open(path, "rb") as f:
            chunk = min(size, 256 * 1024)
            if size > chunk:
                f.seek(size - chunk)
                data = f.read()
                data = data.split(b"\n", 1)[-1]
            else:
                data = f.read()
        text = data.decode("utf-8", "replace")
        return text.splitlines()[-n:]
    except OSError:
        return []
