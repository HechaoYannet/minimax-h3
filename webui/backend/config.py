"""webui.backend.config -- 配置读取层。

两份配置都在 <repo>/config/ 下，都是热读取：每次请求重新解析，
所以改完配置刷新页面就生效，不需要重启服务（也避免了「改了配置以为生效了」这类坑）。

  config/server.yaml       前端服务本身（监听地址、并发、上限、默认值）
  config/deepseek.yaml     提示词优化用的 DeepSeek API
  config/params.spec.json  生成参数规格（由 webui/tools/gen_params.py 从工程本体生成）
"""
from __future__ import annotations

import os
import shutil

from .util import read_json, repo_root

try:
    import yaml  # PyYAML 在 conda diffsynth 环境里已存在；缺了也能降级运行
except Exception:  # pragma: no cover
    yaml = None


ROOT = repo_root()
SERVER_YAML = os.path.join(ROOT, "config", "server.yaml")
DEEPSEEK_YAML = os.path.join(ROOT, "config", "deepseek.yaml")
PARAMS_SPEC = os.path.join(ROOT, "config", "params.spec.json")

# deepseek.yaml 的 multimodal 段缺失时的默认值。deepseek-flash 本身是多模态模型，
# 所以默认开启；换成不支持图片的模型（或网关）时，把它设成 enabled: false 即可
# 退回纯文本请求。所有阈值都留了余量（服务端硬上限见 webui/backend/media.py）。
MULTIMODAL_FALLBACK = {
    "enabled": True,
    "images": True,            # 附参考图
    "video_frames": 0,         # 每个参考视频抽几帧（0=不抽）
    "max_images": 8,           # 单次请求最多附几张
    "detail": "high",          # low | high | original | auto
    "max_edge": 1280,          # 附件长边上限（px）；0=不缩放
    "max_mb_per_image": 20,    # 单图预算（服务端硬上限 32 MiB）
    "max_mb_total": 40,        # 附件合计预算（服务端请求体上限 48 MiB）
}

# server.yaml 缺失时的兜底，保证服务永远起得来
SERVER_FALLBACK = {
    "server": {"host": "0.0.0.0", "port": 8765, "open_browser": True},
    "paths": {
        "cache_dir": "cache/webui",
        "outputs_dir": "workspace/webui/outputs",
        "jobs_dir": "cache/webui/jobs",
        "uploads_dir": "cache/webui/uploads",
    },
    "jobs": {"max_concurrent": 1, "queue_enabled": True, "cancel_grace_s": 20,
             "log_tail_lines": 400},
    "limits": {"max_num_frames": 400, "max_steps": 80, "max_prompt_chars": 12000,
               "max_upload_mb": 512,
               "allowed_ref_ext": {"image": [".png", ".jpg", ".jpeg", ".webp", ".bmp"],
                                   "video": [".mp4", ".mov", ".mkv", ".webm", ".avi"],
                                   "audio": [".mp3", ".wav", ".flac", ".m4a", ".aac",
                                             ".ogg", ".opus"]}},
    "defaults": {"preset": "draft", "seed": 42, "scheduler": "auto",
                 "dit_onload": "cpu", "sdpa_backend": "cudnn", "lora": "",
                 "lora_enabled": False, "telemetry_interval_s": 1.5},
    "telemetry": {"nvidia_smi": "nvidia-smi", "expose_server_log": True},
    "logging": {
        "level": "info", "capacity": 2000, "console": True,
        "file_enabled": True, "dir": "cache/webui/logs",
        "max_file_mb": 8, "backups": 5, "max_field_chars": 4000,
        "expose": True,
        "llm_enabled": True, "llm_capture": "full",
        "llm_max_runs": 100, "llm_max_chars": 200000,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_yaml(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if yaml is not None:
        try:
            return yaml.safe_load(text) or {}
        except Exception as e:  # YAML 写坏了要报出来，而不是静默用默认值
            raise ValueError(f"{os.path.basename(path)} 解析失败：{e}") from e
    return _mini_yaml(text)


def _mini_yaml(text: str) -> dict:
    """PyYAML 缺失时的极简解析器：只支持本项目用到的两级键值 + 内联列表。

    宁可降级也不要静默失败——解析不了的行会被跳过。
    """
    out: dict = {}
    stack = [(0, out)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("#"):
            val = ""
        if "#" in val and not (val.startswith('"') or val.startswith("'")):
            val = val.split("#", 1)[0].strip()
        while stack and indent < stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if not val:
            child: dict = {}
            parent[key] = child
            stack.append((indent + 1, child))
            continue
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip().strip('"\'') for x in val[1:-1].split(",") if x.strip()]
            parent[key] = items
        elif val.lower() in ("true", "false"):
            parent[key] = val.lower() == "true"
        elif val.lower() in ("null", "~"):
            parent[key] = None
        else:
            v = val.strip('"\'')
            try:
                parent[key] = int(v) if v.lstrip("-").isdigit() else float(v)
            except ValueError:
                parent[key] = v
    return out


def server_config() -> dict:
    try:
        raw = _read_yaml(SERVER_YAML)
    except ValueError:
        raw = {}
    cfg = _deep_merge(SERVER_FALLBACK, raw)
    paths = cfg["paths"] = dict(cfg.get("paths", {}))
    for k in ("cache_dir", "outputs_dir", "jobs_dir", "uploads_dir"):
        paths[k] = os.path.abspath(os.path.join(ROOT, paths.get(k) or SERVER_FALLBACK["paths"][k]))
    # 运行日志目录：同样支持相对仓库根的写法（默认 cache/webui/logs）
    logcfg = cfg["logging"] = dict(cfg.get("logging", {}))
    d = logcfg.get("dir") or SERVER_FALLBACK["logging"]["dir"]
    logcfg["dir"] = d if os.path.isabs(d) else os.path.abspath(os.path.join(ROOT, d))
    cfg["root"] = ROOT
    return cfg


def deepseek_config() -> dict:
    cfg = _read_yaml(DEEPSEEK_YAML)
    api = dict(cfg.get("api") or {})
    inline_key = (api.get("key") or "").strip()
    key = inline_key
    if not key:
        env_name = api.get("key_env") or "DEEPSEEK_API_KEY"
        key = (os.environ.get(env_name) or "").strip()
    api["key"] = key
    api["key_present"] = bool(key)
    api["key_source"] = "config/deepseek.yaml" if inline_key else (api.get("key_env") or "DEEPSEEK_API_KEY")
    cfg["api"] = api

    p = dict(cfg.get("prompt") or {})
    for k in ("system_prompt_file",):
        v = p.get(k)
        if isinstance(v, str) and v:
            p[k] = v if os.path.isabs(v) else os.path.join(ROOT, v)
    md = p.get("mode_digests")
    if isinstance(md, dict):
        p["mode_digests"] = {
            kk: (vv if (not vv or os.path.isabs(vv)) else os.path.join(ROOT, vv))
            for kk, vv in md.items()
        }
    cfg["prompt"] = p

    # 多模态附图：缺段/缺项都补默认值，前端 / 后端读到的永远是完整结构
    mm = cfg.get("multimodal")
    cfg["multimodal"] = _deep_merge(MULTIMODAL_FALLBACK,
                                    mm if isinstance(mm, dict) else {})
    return cfg


def params_spec() -> dict:
    spec = read_json(PARAMS_SPEC, None)
    if spec is None:
        return {"error": "config/params.spec.json 缺失；请运行 python3 webui/tools/gen_params.py 重新生成",
                "params": [], "presets": {}}
    return spec


def which(name: str):
    return shutil.which(name)
