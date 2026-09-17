#!/usr/bin/env python3
"""webui.tools.test_disk -- 夸克网盘接入的离线自测（不碰真实服务、不碰真实缓存）。

跑法（WSL 里，仓库根目录）：
    python3 webui/tools/test_disk.py -v

覆盖：
  1. 加密 zip 的往返（标准库 zipfile 能解开 = 7-Zip/WinRAR 也能解开）+ 错误密码必须被拒
  2. ffmpeg 转码（有 ffmpeg 时；没有就跳过）
  3. 配置层：config/quark.yaml -> quark_config() 的默认值与路径展开
  4. CLI 环境探测：node / CLI 入口 / agent 环境标记（认不出来就是 -104）
  5. HTTP 层：起一个**临时**实例（cache 指向 /tmp），把 /api/disk/* 全打一遍
     - status / files（未授权时应给 401 + need_login，而不是 500）
     - publish（dry_run=true：只压缩打包，不真的上传）
     - login --token <乱码>（不打开浏览器，验证失败路径）
     - tasks / task / stream / log / cancel

注意：这里绝不会用真实的 cache/webui 目录 —— 那会触发 JobManager 的孤儿清理，
把正在跑的生成作业 SIGKILL 掉。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI = os.path.dirname(HERE)
ROOT = os.path.dirname(WEBUI)
if WEBUI not in sys.path:
    sys.path.insert(0, WEBUI)

PASS, FAIL, SKIP = [], [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    mark = "ok  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def skip(name, why):
    SKIP.append(name)
    print(f"  [skip] {name}  -- {why}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# 环境里可能挂着 http_proxy；本机回环不该走代理（否则拿到的是代理的 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method, url, body=None, timeout=180):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


# --------------------------------------------------------------------------- 1) 压缩包
def test_zip(tmp):
    """两条打包路径都要能用：bsdtar（快路径）与纯 Python（兜底）。

    判定标准只有一个：**标准库 zipfile 用配置里的密码能解出原字节** ——
    标准库能解开，7-Zip / WinRAR / Bandizip 也就能解开。
    """
    import shutil
    from backend import pack
    src = os.path.join(tmp, "小视频 测试.mp4")
    payload = os.urandom(1_500_000) + b"abcdef" * 2000
    with open(src, "wb") as f:
        f.write(payload)
    name = "小视频 测试.mp4"          # 中文名：bsdtar 必须写成 UTF-8（bit11），否则解出来是乱码

    tar = pack.find_zip_tar()
    if tar:
        check("找到 bsdtar（快路径）", True, f"{tar['bin']} mode={tar['mode']}")
    else:
        skip("bsdtar 快路径", "系统里没有可用的 bsdtar，会全程走纯 Python 兜底")

    for tool in ("tar", "python"):
        if tool == "tar" and not tar:
            continue
        dst = os.path.join(tmp, f"out_{tool}.zip")
        t0 = time.time()
        r = pack.write_encrypted_zip([(src, name)], dst, "123456", level=6, tool=tool)
        dt = time.time() - t0
        check(f"[{tool}] 生成加密 zip", os.path.isfile(dst) and r["bytes"] > 0,
              f"{r['bytes']} B · {dt:.2f}s · tool={r.get('tool')}")
        try:
            import zipfile
            with zipfile.ZipFile(dst) as z:
                got = z.namelist()
            check(f"[{tool}] 包内文件名是 UTF-8 原文", got == [name], repr(got))
        except Exception as e:
            check(f"[{tool}] 包内文件名是 UTF-8 原文", False, repr(e))
        try:
            back = pack.read_encrypted_zip(dst, "123456")
            check(f"[{tool}] 密码可解且逐字节一致", back == payload, f"{len(back)} B")
        except Exception as e:
            check(f"[{tool}] 密码可解且逐字节一致", False, repr(e))
        check(f"[{tool}] 错误密码被拒", pack.zip_readable(dst, "000000") is not None)

    store = os.path.join(tmp, "store.zip")
    pack.write_encrypted_zip([(src, "a.bin")], store, "p", level=0)
    check("level=0（store）也能往返", pack.read_encrypted_zip(store, "p") == payload)
    _ = shutil


# --------------------------------------------------------------------------- 2) 转码
def test_transcode(tmp):
    from backend import pack
    if not pack.ffmpeg_bin():
        return skip("ffmpeg 转码", "系统里没有 ffmpeg")
    src = os.path.join(tmp, "src.mp4")
    import subprocess
    cmd = [pack.ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "testsrc=size=320x240:rate=12:duration=2", "-pix_fmt", "yuv420p", src]
    if subprocess.run(cmd).returncode != 0:
        return skip("ffmpeg 转码", "生成测试视频失败")
    dst = os.path.join(tmp, "out.mp4")
    events = []
    r = pack.transcode(src, dst, {"crf": 30, "preset": "ultrafast", "max_edge": 640,
                                  "audio_bitrate": "64k", "pix_fmt": "yuv420p"},
                       on_progress=events.append)
    check("ffmpeg 转码", os.path.isfile(dst) and r["after"] > 0,
          f"{r['before']} -> {r['after']} B，进度回调 {len(events)} 次")


# --------------------------------------------------------------------------- 3) 配置
def test_config():
    from backend import config as cfgmod
    q = cfgmod.quark_config()
    check("config/quark.yaml 存在且有 cli 路径", bool(q.get("cli")) and os.path.isabs(q["cli"]), q.get("cli"))
    check("压缩包密码默认 123456", str((q.get("archive") or {}).get("password")) == "123456",
          str((q.get("archive") or {}).get("password")))
    check("默认**不**重新编码（保画质）", (q.get("compress") or {}).get("enabled") is False,
          f"compress.enabled={(q.get('compress') or {}).get('enabled')}")
    check("压缩包默认只打包不 deflate（视频压不动，省 CPU）",
          int((q.get("archive") or {}).get("level")) == 0,
          f"archive.level={(q.get('archive') or {}).get('level')}")
    check("下载目录展开为绝对路径", os.path.isabs((q.get("download") or {}).get("dir") or ""),
          (q.get("download") or {}).get("dir"))
    check("CLI 入口在磁盘上存在", os.path.isfile(q["cli"]), q["cli"])


# --------------------------------------------------------------------------- 4) CLI 环境
def test_cli_env():
    from backend import config as cfgmod
    from backend.quark import QuarkCLI, QuarkEnv, looks_unauthorized
    q = cfgmod.quark_config()
    env = QuarkEnv(q)
    info = env.detect(fresh=True)
    check("探测到可用的 node + CLI", bool(info.get("ok")),
          f"mode={info.get('mode')} node={info.get('node')} {info.get('reason') or ''}")
    check("agent 环境标记能通过（否则 CLI 回 -104）",
          "DSH_SESSION_ID" in (QuarkCLI(q, env).build_env().get("WSLENV") or "")
          or info.get("mode") == "linux", "WSLENV=" + str(QuarkCLI(q, env).build_env().get("WSLENV")))
    # 三个 code 的含义必须分清（-118 被误判成未授权，是真机上踩过的 bug）
    check("-103 判为未授权", looks_unauthorized(-103, "未登录，请先执行 login 命令完成登录授权"))
    check("-1408 判为未授权", looks_unauthorized(-1408, "未完成授权认证"))
    check("-118 不算未授权（已授权，换账号要先解绑）",
          not looks_unauthorized(-118, "你已授权夸克网盘账号（x），若想切换账号，请先解除授权"))
    check("-104 不算未授权（是 agent 环境问题）",
          not looks_unauthorized(-104, "无法识别当前 Agent 环境，禁止继续使用"))
    if not info.get("ok"):
        return skip("CLI 实际调用", "环境不可用")


# --------------------------------------------------------------------------- 5) HTTP
def test_http(tmp):
    from backend import config as cfgmod
    from backend import server as srv

    cfg = cfgmod.server_config()
    cache = os.path.join(tmp, "cache")
    cfg["paths"] = dict(cfg["paths"])
    for k in ("cache_dir", "outputs_dir", "jobs_dir", "uploads_dir"):
        cfg["paths"][k] = os.path.join(cache, k)
    cfg["logging"] = dict(cfg.get("logging") or {})
    cfg["logging"]["dir"] = os.path.join(cache, "logs")
    cfg["logging"]["console"] = False
    cfg["telemetry"] = dict(cfg.get("telemetry") or {})
    cfg["telemetry"]["expose_server_log"] = True
    port = free_port()
    cfg["server"] = dict(cfg.get("server") or {})
    cfg["server"]["host"] = "127.0.0.1"
    cfg["server"]["port"] = port

    app, httpd = srv.create(cfg)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    try:
        code, r = http("GET", base + "/api/config")
        d = (r or {}).get("disk") or {}
        check("GET /api/config 带 disk 段", code == 200 and bool(d), f"HTTP {code}")
        check("  └ 默认密码 123456", ((d.get("archive") or {}).get("password")) == "123456",
              str((d.get("archive") or {}).get("password")))

        # 还没查过账号时，probe=0 不能给出「未授权」这种假结论（页面据此显示「加载中…」）
        code, r = http("GET", base + "/api/disk/status?probe=0")
        cold = (r or {}).get("disk") or {}
        check("probe=0 冷启动：auth_known=false 且不给 logged_in",
              code == 200 and cold.get("auth_known") is False and "logged_in" not in cold,
              f"HTTP {code} auth_known={cold.get('auth_known')} logged_in={cold.get('logged_in', '(缺省)')}")

        code, r = http("GET", base + "/api/disk/status?probe=1")
        st = (r or {}).get("disk") or {}
        check("GET /api/disk/status", code == 200 and bool(st.get("runner")),
              f"HTTP {code} cli_ok={(st.get('runner') or {}).get('cli_ok')} "
              f"logged_in={st.get('logged_in')} msg={st.get('message')}")

        check("probe=1 之后：auth_known=true 且结论明确",
              st.get("auth_known") is True and st.get("logged_in") in (True, False),
              f"auth_known={st.get('auth_known')} logged_in={st.get('logged_in')}")
        code, r = http("GET", base + "/api/disk/status?probe=0")
        warm = (r or {}).get("disk") or {}
        check("probe=0 热启动：复用上次结论",
              warm.get("auth_known") is True and warm.get("logged_in") == st.get("logged_in"),
              f"auth_known={warm.get('auth_known')} logged_in={warm.get('logged_in')}")

        # 未授权时：浏览应该给 401 + need_login，而不是 500
        code, r = http("GET", base + "/api/disk/files?parent_fid=0")
        if st.get("logged_in"):
            check("GET /api/disk/files（已授权环境）", code == 200, f"HTTP {code}")
        else:
            check("GET /api/disk/files 未授权 -> 401 + need_login",
                  code == 401 and r.get("need_login") is True, f"HTTP {code} {r.get('error')}")

        code, r = http("GET", base + "/api/disk/search?keyword=test")
        check("GET /api/disk/search 不炸（401/200 都算过）", code in (200, 401), f"HTTP {code}")

        # publish：dry_run，只压缩打包
        src = os.path.join(tmp, "src2.mp4")
        if os.path.isfile(os.path.join(tmp, "out.mp4")):
            import shutil
            shutil.copy(os.path.join(tmp, "out.mp4"), src)
        else:
            with open(src, "wb") as f:
                f.write(os.urandom(200_000))
        code, r = http("POST", base + "/api/disk/publish",
                       {"path": src, "dry_run": True, "compress": True, "archive": True})
        tid = ((r or {}).get("task") or {}).get("id")
        check("POST /api/disk/publish (dry_run)", code == 200 and bool(tid), f"HTTP {code} {r.get('error')}")
        task = {}
        if tid:
            for _ in range(120):
                code, rr = http("GET", base + f"/api/disk/tasks/{tid}")
                task = (rr or {}).get("task") or {}
                if task.get("status") in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.5)
            check("  └ 试压任务跑完", task.get("status") == "done",
                  f"status={task.get('status')} error={task.get('error')}")
            res = task.get("result") or {}
            check("  └ 产出加密 zip", bool(res.get("artifact")) and str(res.get("artifact")).endswith(".zip"),
                  str(res.get("artifact")))
            check("  └ 体积确实变小", (res.get("ratio") or 1) < 1.0, f"ratio={res.get('ratio')}")
            from backend import pack as _pack
            arches = [e for e in (http("GET", base + "/api/disk/tasks/" + tid + "/events")[1].get("events") or [])
                      if e.get("type") == "archived"]
            tool_used = arches[-1].get("tool") if arches else None
            if _pack.find_zip_tar():
                check("  └ 走的是 bsdtar 快路径", tool_used == "bsdtar",
                      "tool=" + str(tool_used) + " packer=" + str(_pack.packer_state()))
            else:
                skip("  └ bsdtar 快路径", "本机没有可用的 bsdtar")
            if res.get("artifact") and os.path.isfile(res["artifact"]):
                import zipfile
                from backend import pack
                try:
                    back = pack.read_encrypted_zip(res["artifact"], "123456")
                    check("  └ 产物能用配置里的密码解开", True, str(len(back)) + " B")
                except Exception as e:
                    check("  └ 产物能用配置里的密码解开", False, repr(e))
                # 真机联调踩出来的：包里必须是「放进去的那个文件」的名字，
                # 不能是压缩包自己的名字（否则解压出来是个叫 xxx.zip 的 mp4）
                with zipfile.ZipFile(res["artifact"]) as z:
                    names = z.namelist()
                check("  └ 包内条目名不是压缩包自己的名字",
                      len(names) == 1 and not names[0].endswith(".zip"), "namelist=" + str(names))
            code, rr = http("GET", base + f"/api/disk/tasks/{tid}/log")
            check("  └ /log 可读", code == 200, f"HTTP {code}")
            code, rr = http("GET", base + f"/api/disk/tasks?limit=5")
            check("  └ /tasks 列表含该任务",
                  any(x.get("id") == tid for x in ((rr or {}).get("tasks") or [])))

        # 默认路径（不勾「重新编码」）：传上去的必须是原件，体积不该变小
        code, r = http("POST", base + "/api/disk/publish",
                       {"path": src, "dry_run": True, "archive": True})
        tid2 = ((r or {}).get("task") or {}).get("id")
        if tid2:
            task2 = {}
            for _ in range(120):
                code, rr = http("GET", base + f"/api/disk/tasks/{tid2}")
                task2 = (rr or {}).get("task") or {}
                if task2.get("status") in ("done", "failed", "cancelled"):
                    break
                time.sleep(0.5)
            res2 = task2.get("result") or {}
            check("默认不重新编码：体积不缩水（原画质）",
                  task2.get("status") == "done" and (res2.get("ratio") or 0) >= 0.98,
                  f"status={task2.get('status')} ratio={res2.get('ratio')}")
            check("  └ 快路径没被误判死", not _pack.packer_state().get("broken"), str(_pack.packer_state()))
            code, rr = http("GET", base + f"/api/disk/tasks/{tid2}/events")
            stages = [ev.get("stage") for ev in ((rr or {}).get("events") or [])
                      if ev.get("type") == "stage"]
            check("  └ 阶段里没有 transcode（确实没走编码）", "transcode" not in stages,
                  "stages=" + ",".join([s for s in stages if s]))
        # 一个必然失败的任务：不存在的文件
        code, r = http("POST", base + "/api/disk/publish", {"path": os.path.join(tmp, "nope.mp4")})
        check("POST /api/disk/publish 文件不存在 -> 400", code == 400, f"HTTP {code}")

        # 登录失败路径（乱码授权码，不会打开浏览器）
        if not (st.get("logged_in")):
            code, r = http("POST", base + "/api/disk/login", {"token": "not-a-real-code"})
            lt = ((r or {}).get("task") or {}).get("id")
            check("POST /api/disk/login 能提交", code == 200 and bool(lt), f"HTTP {code}")
            if lt:
                ltask = {}
                for _ in range(120):
                    code, rr = http("GET", base + f"/api/disk/tasks/{lt}")
                    ltask = (rr or {}).get("task") or {}
                    if ltask.get("status") in ("done", "failed", "cancelled"):
                        break
                    time.sleep(0.5)
                check("  └ 乱码授权码被拒（失败态）", ltask.get("status") == "failed",
                      f"status={ltask.get('status')} error={ltask.get('error')}")
        else:
            skip("登录失败路径", "当前环境已授权，不打扰账号")
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            app.disk.cli.kill_all()
        except Exception:
            pass


def test_task_order(tmp):
    """重启后任务列表仍然「新的在前」。

    曾经把 dir 列表倒序装载，导致 self.order 与运行期相反，list() 取尾部 limit 条
    拿到的是最旧的几条 —— 服务一重启，页面上就看不到刚拿到的分享链接了。
    """
    from backend import config as cfgmod
    from backend.quark import DiskManager

    class _L:
        def child(self, **kw): return self
        def info(self, *a, **k): pass
        def warn(self, *a, **k): pass

    cache = os.path.join(tmp, "order-cache")
    cfg = {"paths": {"cache_dir": cache}, "root": ROOT}
    disk_cfg = cfgmod.quark_config()
    mgr = DiskManager(cfg, _L(), disk_cfg, ROOT)
    ids = []
    for i in range(4):
        t = mgr._new_task("probe", {"i": i})
        t.status, t.finished, t.progress = "done", t.created + i, 1.0
        t.result = {"i": i}
        t.snapshot()
        ids.append(t.id)
        time.sleep(0.02)          # 让 id 里的秒级时间戳不同
    fresh = DiskManager(cfg, _L(), disk_cfg, ROOT)   # 模拟重启后再加载
    got = [x["id"] for x in fresh.list(2)]
    check("重启后任务列表「新的在前」且只取最新几条",
          got == list(reversed(ids))[:2], f"got={got} want={list(reversed(ids))[:2]}")

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="夸克网盘接入离线自测")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（排查用）")
    a = ap.parse_args(argv)
    t0 = time.time()
    # 临时目录放在仓库的 cache/ 下而不是 /tmp：Windows 侧的 bsdtar 与 node.exe
    # 只看得见 /mnt/<盘>/ 里的东西，用 /tmp 会把「快路径」直接测成兜底路径。
    base = os.path.join(ROOT, "cache")
    os.makedirs(base, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="_selftest-disk-", dir=base)
    print(f"临时目录：{tmp}\n")
    print("[1] 加密压缩包")
    test_zip(tmp)
    print("[2] 视频转码")
    test_transcode(tmp)
    print("[3] 配置")
    test_config()
    print("[4] CLI 环境")
    test_cli_env()
    print("[4.5] 任务列表顺序（重启后）")
    test_task_order(tmp)
    print("[5] HTTP 端点（临时实例，独立 cache）")
    test_http(tmp)
    print()
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)} · 跳过 {len(SKIP)} · {time.time() - t0:.1f}s")
    if not a.keep:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"（按 --keep 保留了 {tmp}）")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
