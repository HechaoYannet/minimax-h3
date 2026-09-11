#!/usr/bin/env python3
"""MiniMax-H3 创作台后端入口。

用法（在 WSL 里，仓库根目录下）：
    python3 webui/serve.py                 # 按 config/server.yaml 启动
    python3 webui/serve.py --port 8899
    python3 webui/serve.py --check         # 只自检，不起服务

Windows 侧用 webui/start.ps1 启动即可，它会调用本文件。
这个薄壳存在的唯一原因是：让 `python3 webui/serve.py` 与 `python3 -m backend.server`
两种跑法都成立 —— 前者是脚本，没有父包，必须先把 webui/ 放进 sys.path。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from backend.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
