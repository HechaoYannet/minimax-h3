r"""webui.backend.pack -- 上传前的「打包」：加密 zip（+ 可选的 ffmpeg 重编码）。

两件事，默认只做第一件：

  * **加密压缩包（默认开，本模块的主角）**：把产物原样封进一个带密码的 zip。
    目的**不是**省体积（mp4 早就压过了，deflate 只能省 1% 左右，所以默认 level=0 只打包），
    而是两件事：
      1. 内容变成密文，网盘的自动内容抽检扫不到里面是什么；
      2. 夸克分享链接的提取码由服务端生成、调用方指定不了，
         要让密码统一成 config/quark.yaml 里的那一个，只能落在压缩包上。
  * **ffmpeg 重编码（默认关）**：有损。只有明确要省流量/省网盘空间时才在
    config/quark.yaml 里打开 compress.enabled，或页面上针对单次上传临时勾。
    画质优先时不要开它。

密码保护用的是传统 ZipCrypto（PKWARE 传统加密，method 8 + flag bit0），
不是 AES：7-Zip / WinRAR / Bandizip / 系统自带解压都可以直接打开，
Python 的 zipfile 也能读，方便离线自测（见 webui/tools/test_disk.py）。
代价是 ZipCrypto 本身强度不高 —— 它挡的是「网盘自动抽检」和「链接被顺手点开」，
不挡存心破解的人。

**两条实现路径，优先用快的**：

1. **bsdtar（libarchive）**：Windows 自带的 `C:\Windows\System32\tar.exe` 就是 bsdtar 3.8，
   原生支持 `--format zip --options zip:encryption=zipcrypt,hdrcharset=UTF-8 --passphrase`。
   C 实现，几百 MB/s，中文文件名也能正确写成 UTF-8（带 bit11 标记）。
   路径从 WSL 经 interop 传给 Windows 的 tar.exe，和调 node 是同一套路子。
2. **纯 Python 兜底**：没有 bsdtar 时用本模块自己写的 ZipCrypto 写入器。
   它只有 ~2 MiB/s（逐字节的密钥流是串行的，Python 层没法向量化），
   所以只作为兜底与自测的参考实现。

写完一律用标准库 `zipfile` **验一遍密码能解开**（校验字节不对就说明密码或实现有问题），
验不过就退回纯 Python 重写 —— 宁可慢，不能交付一个收件人打不开的包。

除标准库（zlib/struct/os）外只额外依赖系统 ffmpeg 与可选的 bsdtar，不引任何 Python 第三方包。
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import threading
import time
import zlib

from .util import wsl_to_windows

# --------------------------------------------------------------------------- 基础

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts"}


def ffmpeg_bin() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_bin() -> str | None:
    return shutil.which("ffprobe")


def human(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.2f} {unit}" if n < 10 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def probe_duration(path: str) -> float | None:
    """用 ffprobe 读时长（秒）；读不到返回 None（不阻断流程）。"""
    ff = ffprobe_bin()
    if not ff:
        return None
    try:
        out = subprocess.run(
            [ff, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30)
        return float((out.stdout or "").strip())
    except Exception:
        return None


# --------------------------------------------------------------------------- 加密 zip

_CRC_TABLE: list[int] = []


def _crc_table() -> list[int]:
    global _CRC_TABLE
    if not _CRC_TABLE:
        t = []
        for n in range(256):
            c = n
            for _ in range(8):
                c = (0xEDB88320 ^ (c >> 1)) if (c & 1) else (c >> 1)
            t.append(c & 0xFFFFFFFF)
        _CRC_TABLE = t
    return _CRC_TABLE


class ZipCrypto:
    """PKWARE 传统加密（ZipCrypto）的加密侧。

    与解密实现（Info-ZIP unzip / Python zipfile._ZipDecrypter / 7-Zip）逐位对应：
        key0 = crc32(key0, plain)
        key1 = (key1 + (key0 & 0xff)) * 134775813 + 1
        key2 = crc32(key2, key1 >> 24)
        cipher = plain ^ ((key2 | 2) * ((key2 | 2) ^ 1) >> 8) & 0xff
    """

    def __init__(self, password: bytes):
        t = _crc_table()
        self._t = t
        self.k0, self.k1, self.k2 = 0x12345678, 0x23456789, 0x34567890
        for b in password:
            self._update(b)

    def _crc(self, crc: int, b: int) -> int:
        return ((crc >> 8) ^ self._t[(crc ^ b) & 0xFF]) & 0xFFFFFFFF

    def _update(self, b: int) -> None:
        self.k0 = self._crc(self.k0, b)
        self.k1 = (self.k1 + (self.k0 & 0xFF)) & 0xFFFFFFFF
        self.k1 = (self.k1 * 134775813 + 1) & 0xFFFFFFFF
        self.k2 = self._crc(self.k2, (self.k1 >> 24) & 0xFF)

    def _stream_byte(self) -> int:
        temp = (self.k2 | 2) & 0xFFFF
        return ((temp * (temp ^ 1)) >> 8) & 0xFF

    def encrypt(self, data: bytes) -> bytes:
        out = bytearray(len(data))
        for i, p in enumerate(data):
            c = p ^ self._stream_byte()
            self._update(p)
            out[i] = c
        return bytes(out)


def _dos_time(ts: float) -> tuple[int, int]:
    """Unix 时间戳 -> DOS (date, time)。1980 年之前按 1980-01-01 处理。"""
    lt = time.localtime(ts)
    year = max(1980, lt.tm_year)
    date = ((year - 1980) << 9) | (lt.tm_mon << 5) | lt.tm_mday
    tm = (lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2)
    return date, tm


def _crc_file(path: str, cancel=None, on_chunk=None) -> tuple[int, int]:
    crc, size = 0, 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("已取消")
            chunk = f.read(1 << 20)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
            if on_chunk:
                on_chunk(size)
    return crc & 0xFFFFFFFF, size


ZIP64_LIMIT = 0xFFFFFFFE


# --------------------------------------------------------------------------- bsdtar 快路径

_TAR_CACHE: dict = {"probed": False, "tar": None, "broken": False, "why": ""}


class _TarSkip(Exception):
    """bsdtar 这一次用不了（比如路径在 WSL 的 /tmp 里、Windows 看不见）。

    这不算「工具坏了」—— 只是这个输入它处理不了，换纯 Python 就好，下次还得再试它。
    """


def find_zip_tar(explicit: str = "") -> dict | None:
    """找一个能写加密 zip 的 tar（libarchive / bsdtar）。

    Windows 10+ 自带的 `System32\tar.exe` 就是 bsdtar，从 WSL 直接 `/mnt/c/...` 就能调。
    返回 `{"bin": 路径, "mode": "windows"|"linux"}`；GNU tar 会被跳过（它不支持 --format zip）。
    """
    cands: list[str] = []
    if explicit:
        cands.append(explicit)
    else:
        if os.path.isdir("/mnt/c/Windows/System32"):
            cands.append("/mnt/c/Windows/System32/tar.exe")
        for name in ("bsdtar", "tar"):
            p = shutil.which(name)
            if p:
                cands.append(p)
    for c in cands:
        if not os.path.exists(c):
            continue
        try:
            r = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=25)
        except Exception:
            continue
        blob = ((r.stdout or "") + (r.stderr or "")).lower()
        if "bsdtar" not in blob:
            continue        # GNU tar：没有 zip 格式，也没有加密
        return {"bin": c, "mode": "windows" if c.lower().endswith(".exe") else "linux"}
    return None


def zip_readable(path: str, password: str, expect=None) -> str | None:
    """能不能用这个密码解开？返回 None 表示 OK，否则返回原因（给日志/回退决策用）。

    expect 给了就一并核对包内条目名 —— 「包叫 A.zip、里面装着 A.zip（其实是 mp4）」这种
    错位必须在这里拦住，不能等收件人解压时才发现。
    """
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if not names:
                return "包里没有条目"
            if expect is not None and list(names) != list(expect):
                return f"包内条目名不符：{names} != {list(expect)}"
            with z.open(names[0], pwd=password.encode("utf-8")) as f:
                f.read(1)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def _tar_write(entries, dst: str, password: str, level: int, tar: dict,
               on_progress=None, cancel=None) -> dict:
    mode = tar["mode"]
    conv = wsl_to_windows if mode == "windows" else (lambda p: p)
    if mode == "windows":
        # Windows 的 tar.exe 只看得见 /mnt/<盘>/ 下的东西（WSL 的 /tmp、/home 它一概看不见）
        for p in [dst] + [s for s, _ in entries]:
            if conv(p) == p:
                raise _TarSkip(f"{p} 不在 Windows 可见的 /mnt/<盘>/ 下")
    comp = "store" if level <= 0 else "deflate"
    opts = (f"zip:encryption=zipcrypt,hdrcharset=UTF-8,"
            f"zip:compression={comp},zip:compression-level={max(0, min(9, level))}")
    argv = [tar["bin"], "-c", "-f", conv(dst), "--format", "zip",
            "--options", opts, "--passphrase", password]
    for src, arc in entries:
        # bsdtar 是「切到 -C 目录，再按这个名字找文件」，所以成员名必须等于源文件名。
        # 不一致是调用方的问题，不算 tar 坏了 —— 退纯 Python（它按 src 读、按 arc 写）。
        if os.path.basename(src) != arc:
            raise _TarSkip(f"包内名 {arc} 与源文件名 {os.path.basename(src)} 不一致")
        argv += ["-C", conv(os.path.dirname(os.path.abspath(src))), arc]
    total = sum(os.path.getsize(s) for s, _ in entries)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    if os.path.exists(dst):
        os.remove(dst)
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, errors="replace", start_new_session=True)
    stop = threading.Event()

    def _poll():
        # bsdtar 不给字节进度，这里看产物文件涨到哪了（store 模式下就是线性拷贝，够准）
        while not stop.wait(0.35):
            try:
                cur = os.path.getsize(dst)
            except OSError:
                continue
            if on_progress and total:
                on_progress({"phase": "zip", "bytes": min(cur, total), "total": total,
                             "percent": min(99, int(cur * 100 / total))})

    th = threading.Thread(target=_poll, daemon=True)
    th.start()
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise InterruptedError("已取消")
            time.sleep(0.2)
    finally:
        stop.set()
        th.join(timeout=2)
    err = (proc.stderr.read() if proc.stderr else "") or ""
    if proc.returncode != 0:
        # 报错要带上前几行：bsdtar 的收尾句永远是「Error exit delayed from previous errors」，
        # 真正的原因在它上面
        lines = [x.strip() for x in err.splitlines() if x.strip()] or ["未知错误"]
        safe = " ".join(argv).replace(password, "******")
        raise RuntimeError("bsdtar 打包失败（rc=%d）：%s ｜ cmd: %s"
                           % (proc.returncode, " / ".join(lines[:3])[:300], safe[:400]))
    if on_progress and total:
        on_progress({"phase": "zip", "bytes": total, "total": total, "percent": 100})
    return {"ok": True, "path": dst, "bytes": os.path.getsize(dst),
            "entries": [{"name": arc, "size": os.path.getsize(src)} for src, arc in entries],
            "size_before": total, "password_set": True, "tool": "bsdtar"}


def packer_state() -> dict:
    """当前进程用哪条打包路径（给状态接口/排障看）：快路径、有没有被判死、为什么）。"""
    return {"tar": (_TAR_CACHE.get("tar") or {}).get("bin"),
            "mode": (_TAR_CACHE.get("tar") or {}).get("mode"),
            "probed": bool(_TAR_CACHE.get("probed")),
            "broken": bool(_TAR_CACHE.get("broken")),
            "why": _TAR_CACHE.get("why") or ""}


def write_encrypted_zip(entries, dst: str, password: str, level: int = 6,
                        on_progress=None, cancel=None, tool: str = "auto",
                        tar_bin: str = "", on_note=None) -> dict:
    """把 entries=[(源文件, 压缩包内文件名)] 写成一个加密 zip。

    默认先试 bsdtar（快、中文名正确），写完用标准库验一遍密码；验不过或没有 bsdtar，
    就退回纯 Python 实现（见 _write_zip_python）。`tool` 可强制 "tar" / "python"。
    """
    if not entries:
        raise ValueError("没有要打包的文件")
    if not (password or "").strip():
        raise ValueError("压缩包密码为空")
    for _src, arc in entries:
        if os.sep in arc or "/" in arc:
            tool = "python"     # 带路径的成员名 bsdtar 要额外拼 -C，走兜底更省心
            break

    if tool in ("auto", "tar") and not _TAR_CACHE["broken"]:
        if not _TAR_CACHE["probed"]:
            _TAR_CACHE["tar"] = find_zip_tar(tar_bin)
            _TAR_CACHE["probed"] = True
        tar = _TAR_CACHE["tar"]
        if tar:
            try:
                res = _tar_write(entries, dst, password, level, tar, on_progress, cancel)
                why = zip_readable(dst, password, [arc for _src, arc in entries])
                if why is None:
                    return res
                raise RuntimeError(f"产物校验没过（{why}）")
            except InterruptedError:
                raise
            except _TarSkip as e:
                # 路径问题，不是工具问题：这次退兜底，下次还试它
                _TAR_CACHE["why"] = str(e)
                if on_note:
                    on_note(f"bsdtar 这次用不了（{e}），改用纯 Python 打包（慢，但结果一样）")
                if tool == "tar":
                    raise
            except Exception as e:
                # 一次性失败：以后都别再用它，避免每个任务都白干一遍
                _TAR_CACHE["broken"] = True
                _TAR_CACHE["why"] = str(e)
                if on_note:
                    on_note(f"bsdtar 打包失败（{e}），本次及之后都用纯 Python 打包")
                if tool == "tar":
                    raise
        elif tool == "tar":
            raise RuntimeError("没找到可用的 bsdtar（Windows 自带的 tar.exe 应该就够了）")

    return _write_zip_python(entries, dst, password, level, on_progress, cancel)


def _write_zip_python(entries, dst: str, password: str, level: int = 6,
                      on_progress=None, cancel=None) -> dict:
    """纯 Python 的 ZipCrypto 写入器（兜底实现，~2 MiB/s）。

    两遍读源文件：第一遍算 CRC/大小（zip 的本地头里要写 CRC，而 12 字节加密头
    的第 12 个字节就是 CRC 高字节 —— 所以没有「边读边写」的余地），第二遍加密写出。
    全程流式，内存占用与文件大小无关；慢是真的慢（逐字节的密钥流没法向量化），
    所以它只在没有 bsdtar 时兜底。
    """
    pwd = (password or "").encode("utf-8")

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    metas = []
    total = 0
    for src, arcname in entries:
        if not os.path.isfile(src):
            raise FileNotFoundError(f"文件不存在：{src}")
        total += os.path.getsize(src)

    read_done = [0]

    def _tick(n):
        read_done[0] = n
        if on_progress:
            on_progress({"phase": "crc", "bytes": n, "total": total,
                         "percent": int(n * 100 / total) if total else 0})

    for src, arcname in entries:
        if cancel is not None and cancel.is_set():
            raise InterruptedError("已取消")
        crc, size = _crc_file(src, cancel=cancel, on_chunk=_tick)
        if size >= ZIP64_LIMIT:
            raise ValueError(f"{os.path.basename(src)} 超过 4 GiB，"
                             "当前实现不写 Zip64 头，请先压缩/切分")
        metas.append({"src": src, "arc": arcname, "crc": crc, "size": size,
                      "mtime": os.path.getmtime(src)})

    written = [0]
    method = 8 if level > 0 else 0
    with open(dst, "wb") as out:
        cd = bytearray()
        for m in metas:
            name = m["arc"].encode("utf-8")
            flags = 0x0001                       # bit0 = 加密
            if any(c > 127 for c in name):
                flags |= 0x0800                  # bit11 = 文件名是 UTF-8
            date, tm = _dos_time(m["mtime"])
            offset = out.tell()

            # 本地头先占位，数据写完再回来补 CRC/压缩后大小
            out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, method,
                                  tm, date, 0, 0, 0, len(name), 0))
            out.write(name)

            crypt = ZipCrypto(pwd)
            # 12 字节加密头：前 11 字节随机 + 第 12 字节 = CRC 高字节
            # （bit3 未置位时，各解压实现都按 CRC 高字节校验密码，见 Python zipfile）
            out.write(crypt.encrypt(os.urandom(11) + bytes([(m["crc"] >> 24) & 0xFF])))

            comp = zlib.compressobj(level if level > 0 else 0, zlib.DEFLATED, -15) \
                if level > 0 else None
            csize = 12
            done = 0
            with open(m["src"], "rb") as src:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("已取消")
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    blob = comp.compress(chunk) if comp else chunk
                    done += len(chunk)
                    if blob:
                        blob = crypt.encrypt(blob)
                        out.write(blob)
                        csize += len(blob)
                    written[0] += len(chunk)
                    if on_progress:
                        on_progress({"phase": "zip", "bytes": written[0], "total": total,
                                     "percent": int(written[0] * 100 / total) if total else 0})
            tail = crypt.encrypt(comp.flush()) if comp else b""
            if tail:
                out.write(tail)
                csize += len(tail)

            end = out.tell()
            out.seek(offset)
            out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, method,
                                  tm, date, m["crc"], csize, m["size"], len(name), 0))
            out.seek(end)

            cd += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, method,
                              tm, date, m["crc"], csize, m["size"], len(name), 0, 0, 0, 0,
                              0, offset) + name

        cd_offset = out.tell()
        out.write(bytes(cd))
        out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(metas), len(metas),
                              len(cd), cd_offset, 0))

    return {"ok": True, "path": dst, "bytes": os.path.getsize(dst),
            "entries": [{"name": m["arc"], "size": m["size"]} for m in metas],
            "size_before": sum(m["size"] for m in metas), "password_set": True,
            "tool": "python"}


def read_encrypted_zip(path: str, password: str, name: str | None = None) -> bytes:
    """自测用：用标准库把加密 zip 解回来（能被 zipfile 读通 = 通用解压工具也能读通）。"""
    import zipfile
    with zipfile.ZipFile(path) as z:
        target = name or z.namelist()[0]
        return z.read(target, pwd=password.encode("utf-8"))


# --------------------------------------------------------------------------- ffmpeg 转码

def transcode(src: str, dst: str, opts: dict, on_progress=None, on_log=None,
              cancel=None) -> dict:
    """按 config 里的参数把视频转小。返回 {ok, path, before, after, seconds}。"""
    ff = ffmpeg_bin()
    if not ff:
        raise RuntimeError("找不到 ffmpeg，无法压缩视频；可把 config/quark.yaml 的 compress.enabled 设为 false")

    crf = int(opts.get("crf", 26))
    preset = str(opts.get("preset") or "veryfast")
    max_edge = int(opts.get("max_edge") or 0)
    abitrate = str(opts.get("audio_bitrate") or "96k")
    pix = str(opts.get("pix_fmt") or "yuv420p")

    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-progress", "pipe:1", "-i", src,
           "-map", "0:v:0", "-map", "0:a?",
           "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
           "-pix_fmt", pix,
           "-c:a", "aac", "-b:a", abitrate,
           "-movflags", "+faststart"]
    if max_edge > 0:
        # 只缩不放：原图比 max_edge 小就保持原样；-2 保证高是偶数（yuv420p 要求）
        cmd += ["-vf", f"scale='min({max_edge},iw)':-2"]
    cmd.append(dst)

    duration = probe_duration(src)
    before = os.path.getsize(src)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", bufsize=1,
                            start_new_session=True)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                break
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k == "out_time_ms" and duration and on_progress:
                try:
                    done = float(v) / 1_000_000.0
                    on_progress({"phase": "transcode",
                                 "percent": max(0, min(99, int(done * 100 / duration))),
                                 "seconds": round(done, 1), "total_seconds": round(duration, 1)})
                except ValueError:
                    pass
    finally:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                proc.kill()
            raise RuntimeError("ffmpeg 无响应，已强制结束")
    if proc.returncode != 0:
        err = (proc.stderr.read() if proc.stderr else "") or ""
        if cancel is not None and cancel.is_set():
            raise InterruptedError("已取消")
        raise RuntimeError("ffmpeg 转码失败：" + (err.strip().splitlines() or ["未知错误"])[-1][:400])
    after = os.path.getsize(dst)
    return {"ok": True, "path": dst, "before": before, "after": after,
            "ratio": round(after / before, 4) if before else 1.0,
            "seconds": round(time.time() - t0, 1)}
