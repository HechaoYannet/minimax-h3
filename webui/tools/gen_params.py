#!/usr/bin/env python3
"""gen_params.py -- 从工程本体生成前端参数规格(config/params.spec.json)。

单一事实来源原则：参数的取值范围、默认值、可选项一律从
  * cache/plan.json      (h3_audit.py 产出的预设与内存方案)
  * scripts/h3_generate.py 的 argparse 定义
推导，不在这里手写一份平行副本。上游改了参数，重跑本脚本即可。

用法：
    python3 webui/tools/gen_params.py            # 写 config/params.spec.json
    python3 webui/tools/gen_params.py --print    # 只打印
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from param_docs import GENERAL_NOTES, PANEL_DOCS, PARAM_DOCS, SECTION_ORDER  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
GEN = os.path.join(REPO, "scripts", "h3_generate.py")
PLAN = os.path.join(REPO, "cache", "plan.json")
OUT = os.path.join(REPO, "config", "params.spec.json")

# ---------------------------------------------------------------------------
# 1. 读预设
# ---------------------------------------------------------------------------


def load_presets():
    with open(PLAN, encoding="utf-8") as f:
        plan = json.load(f)
    resolved = plan.get("resolved", {})
    presets = {}
    for name, p in plan.get("presets", {}).items():
        al = p.get("aligned", {})
        rows = p.get("rows", {})
        if not al or "height" not in al:
            continue
        presets[name] = {
            "height": al.get("height"),
            "width": al.get("width"),
            "num_frames": al.get("num_frames"),
            "steps": p.get("steps"),
            "seconds": al.get("seconds"),
            "seq_len": rows.get("seq_len"),
            "step_s": p.get("step_s"),
            "denoise_min": p.get("denoise_min"),
            "ref_image_short_edge": p.get("ref_image_short_edge"),
            "ref_video_short_edge": p.get("ref_video_short_edge"),
            "ref_video_max_pixels": p.get("ref_video_max_pixels"),
            "note": p.get("note", ""),
        }
    return plan, resolved, presets


# ---------------------------------------------------------------------------
# 2. 读 h3_generate.py 的 argparse 定义（静态解析，不 import torch）
# ---------------------------------------------------------------------------

ARG_RE = re.compile(r'^\s*(?P<var>\w+)\.add_argument\(\s*(?P<args>.*)$')


def parse_argparse():
    """返回 {dest: {flags, group, default, choices, type, help}}。"""
    src = open(GEN, encoding="utf-8").read()
    # 只取 main() 里那一段，避免匹配到别处的 add_argument
    m = re.search(r"def main\(\):(.*?)\nif __name__", src, re.S)
    body = m.group(1) if m else src
    groups = {}
    cur_group = "misc"
    out = {}
    for line in body.splitlines():
        gm = re.match(r'\s*(\w+) = ap\.add_argument_group\("([^"]+)"\)', line)
        if gm:
            groups[gm.group(1)] = gm.group(2)
            continue
        am = ARG_RE.match(line)
        if not am:
            continue
        var = am.group(1)
        rest = am.group(2)
        if var == "ap":
            cur_group = "misc"
        else:
            cur_group = groups.get(var, cur_group)
        flags = re.findall(r'"(--[^"]+)"', rest)
        dest = None
        dm = re.search(r'dest="(\w+)"', rest)
        if dm:
            dest = dm.group(1)
        elif flags:
            dest = flags[0].lstrip("-").replace("-", "_")
        if not dest:
            continue
        choices = None
        cm = re.search(r"choices=\[([^\]]*)\]", rest)
        if cm:
            choices = [c.strip().strip('"\'') for c in cm.group(1).split(",") if c.strip()]
        typ = "str"
        tm = re.search(r"type=(\w+)", rest)
        if tm:
            typ = {"int": "int", "float": "float", "str": "str"}.get(tm.group(1), "str")
        is_flag = "store_true" in rest
        if is_flag:
            typ = "bool"
        out[dest] = {
            "flags": flags,
            "group": cur_group,
            "type": typ,
            "choices": choices,
            "is_flag": is_flag,
        }
    return out


# ---------------------------------------------------------------------------
# 3. 控件元数据：单位、范围、中文标签、说明、风险等级
#      （只有「怎么显示」是手写的；「取值从哪来」全部来自上面两步）
# ---------------------------------------------------------------------------

LBL = {
    "preset": ("预设档位", "group", "从 cache/plan.json 读出的调优预设，一档对应一组已验证的形状与步数。"),
    "width": ("宽度", "px", "必须是 32 的倍数，框架会向上吸附并在日志里记录。"),
    "height": ("高度", "px", "必须是 32 的倍数。8 GiB 卡上 1024x576 是实测跑通的最高点。"),
    "num_frames": ("帧数", "帧", "必须满足 num_frames % 17 == 5；24 fps，帧数/24 = 秒数。"),
    "seconds": ("时长", "秒", "由帧数换算：帧数 / 24。改帧数时自动联动。"),
    "steps": ("采样步数", "步", "去噪步数。步数×单步耗时≈去噪总时长；挂 LoRA 时建议 ≥30。"),
    "seed": ("随机种子", "seed", "同种子+同参数=同结果；换种子可快速试构图。"),
    "ref_image_short_edge": ("参考图短边", "px", "最陡的性能杠杆：2048→1536→1024→768→512 逐级降价，同时降低细节保留。"),
    "ref_video_short_edge": ("参考视频短边", "px", "参考视频与目标等长，代价极大；draft 档默认 384。"),
    "ref_video_max_pixels": ("参考视频像素上限", "px", "与短边共同约束参考视频的读取尺寸。"),
    "vram_limit": ("显存阈值", "GiB", "「整卡已用量」阈值，不是权重预算。抬高只多常驻几层，实测提速有限。"),
    "activation_reserve": ("激活预留", "GiB", "按 vram_limit = 启动可用 − 激活预留 − LoRA 反推。想冒险时调它。"),
    "dit_onload": ("DiT 装载方式", None, "cpu=常驻内存(峰值 11.1 GiB)；disk=走 mmap(峰值 4.8 GiB)，长片段更稳。"),
    "sdpa_backend": ("注意力后端", None, "DiT 走 SDPA；实测 cuDNN 最快。其余路径固定 flash。"),
    "tile_size": ("VAE 解码分块", "px", "分块解码，避免整帧物化导致显存尖峰。"),
    "tile_overlap": ("VAE 分块重叠", "px", "分块之间的重叠像素，减少接缝。"),
    "no_tiled": ("不分块解码", None, "一次性解码整段 VAE，显存需求高得多，仅在显存充裕时使用。"),
    "lora": ("LoRA 文件", None, "挂 LoRA 会自动切到 beta 调度器（euler+beta 是 AfterMidnight 的硬性要求）。"),
    "lora_alpha": ("LoRA 权重", "α", "LoRA 强度倍率。1.0 为作者标定值。"),
    "scheduler": ("调度器", None, "auto=挂 LoRA 用 beta、否则 flow；beta 在低 sigma 段更密，音频更稳。"),
    "beta_alpha": ("beta α", None, "ComfyUI beta 分位数参数，默认 0.6。"),
    "beta_beta": ("beta β", None, "ComfyUI beta 分位数参数，默认 0.6。"),
    "text_cache": ("复用提示词缓存", None, "命中缓存可完全跳过 26 B 参数的文本编码器。"),
    "refresh_text_cache": ("强制重算缓存", None, "忽略已有缓存重新编码提示词。"),
}

ORDER = [
    ("shape", "形状与时长", ["preset", "width", "height", "num_frames", "seconds", "steps", "seed"]),
    ("refs", "参考条件代价", ["ref_image_short_edge", "ref_video_short_edge", "ref_video_max_pixels"]),
    ("memory", "显存 / 内存 / 速度", ["vram_limit", "activation_reserve", "dit_onload", "sdpa_backend"]),
    ("vae", "VAE 解码", ["tile_size", "tile_overlap", "no_tiled"]),
    ("lora", "LoRA", ["lora", "lora_alpha"]),
    ("scheduler", "调度器 / 采样器", ["scheduler", "beta_alpha", "beta_beta"]),
    ("cache", "提示词缓存", ["text_cache", "refresh_text_cache"]),
]


def build():
    plan, resolved, presets = load_presets()
    args = parse_argparse()
    denoise = resolved.get("denoise", {})
    vae = resolved.get("vae", {})
    mem = plan.get("memory", {})

    # 每个参数在选择「预设」时的真实默认值（复刻 h3_generate.main 的解析链）
    def preset_default(name, fallback):
        for p in presets.values():
            if p.get(name) is not None:
                return {"__preset__": name}
        return fallback

    auto_defaults = {
        "width": {"__preset__": "width"},
        "height": {"__preset__": "height"},
        "num_frames": {"__preset__": "num_frames"},
        "steps": {"__preset__": "steps"},
        "ref_image_short_edge": {"__preset__": "ref_image_short_edge", "fallback": 1024},
        "ref_video_short_edge": {"__preset__": "ref_video_short_edge", "fallback": 384},
        "ref_video_max_pixels": {"__preset__": "ref_video_max_pixels"},
        "vram_limit": resolved.get("vram_limit_gib", 4.58),
        "tile_size": vae.get("tile_size", 256),
        "tile_overlap": vae.get("tile_overlap", 64),
        "seed": 42,
    }

    RANGES = {
        "width": [160, 1920, 32],
        "height": [160, 1920, 32],
        # 框架只要求 %17==5，但实测 5 帧会在 VAE 解码处崩（见 param_docs 的 num_frames）
    "num_frames": [22, 400, 1],
        "steps": [1, 80, 1],
        "seed": [0, 2**31 - 1, 1],
        "ref_image_short_edge": [256, 2048, 64],
        "ref_video_short_edge": [256, 1024, 64],
        "ref_video_max_pixels": [65536, 2000000, 1024],
        "vram_limit": [2.0, 7.5, 0.01],
        "activation_reserve": [0.5, 6.0, 0.05],
        "tile_size": [128, 1024, 64],
        "tile_overlap": [0, 256, 16],
        "lora_alpha": [0.0, 2.0, 0.05],
        "beta_alpha": [0.1, 3.0, 0.05],
        "beta_beta": [0.1, 3.0, 0.05],
    }

    params = []
    for group, gtitle, names in ORDER:
        for name in names:
            doc = PARAM_DOCS.get(name) or {}
            label = doc.get("label") or LBL.get(name, (name, None, ""))[0]
            unit = doc.get("unit", LBL.get(name, (name, None, ""))[1])
            help_ = doc.get("brief") or LBL.get(name, (name, None, ""))[2]
            spec = args.get(name)
            item = {
                "id": name,
                "group": group,
                "group_title": gtitle,
                "label": label,
                "unit": unit,
                "help": help_,
                # 详情来自 webui/tools/param_docs.py（单一数据源）：
                # detail = 它到底控制什么；tips/risks/ranges = 经验、风险、取值语义。
                "detail": doc.get("detail", ""),
                "tips": doc.get("tips", []),
                "risks": doc.get("risks", []),
                "ranges": doc.get("ranges", []),
                "type": (spec or {}).get("type", "int" if name in RANGES else "str"),
                "choices": (spec or {}).get("choices"),
                "cli": (spec or {}).get("flags", []),
            }
            if name in auto_defaults:
                item["default"] = auto_defaults[name]
            if name in RANGES:
                lo, hi, step = RANGES[name]
                item["min"], item["max"], item["step"] = lo, hi, step
            if name == "seconds":
                item["computed_from"] = "num_frames"
                item["fps"] = 24
            params.append(item)

    fps = 24
    shape = {
        "fps": fps,
        "frame_mod": {"mod": 17, "rem": 5, "note": "num_frames % 17 == 5"},
        # 实测边界：5 帧能过形状检查但在 VAE 解码处崩；工程里跑过的最小/最长档
        "frames_min_measured": 22,
        "frames_max_measured": 243,
        "frames_note": "低于 22 帧 VAE 解码会失败；243 帧是 640x384 档实测跑通的最长片段",
        "size_mod": {"mod": 32, "note": "width/height % 32 == 0"},
        "vram_total_gib": 7.93,
        "startup_free_gib": 6.84,
        "ram_total_gib": 22.91,
    }

    # 时间模型：s/step = a + b*seq + c*seq^2，用 plan.json 里 preset 的
    # (seq_len, step_s) 做最小二乘拟合。这些 preset 的 step_s 来自实测/校准过的模型，
    # 所以拟合值只在预设覆盖范围内可信，前端会一并显示「外推」提示。
    pts = [(p["seq_len"], p["step_s"]) for p in presets.values()
           if p.get("seq_len") and p.get("step_s")]
    fit = None
    if len(pts) >= 3:
        import statistics
        n = len(pts)
        # 正规方程解 3 参数最小二乘（无 numpy 依赖）
        s = [0.0] * 5
        rhs = [0.0] * 3
        for x, y in pts:
            for k in range(5):
                s[k] += x ** k
            rhs[0] += y
            rhs[1] += y * x
            rhs[2] += y * x * x
        A = [[s[0], s[1], s[2]], [s[1], s[2], s[3]], [s[2], s[3], s[4]]]
        # 高斯消元
        M = [row[:] + [rhs[i]] for i, row in enumerate(A)]
        for i in range(3):
            piv = max(range(i, 3), key=lambda r: abs(M[r][i]))
            M[i], M[piv] = M[piv], M[i]
            for r in range(i + 1, 3):
                f = M[r][i] / M[i][i]
                for c in range(i, 4):
                    M[r][c] -= f * M[i][c]
        sol = [0.0] * 3
        for i in (2, 1, 0):
            sol[i] = (M[i][3] - sum(M[i][j] * sol[j] for j in range(i + 1, 3))) / M[i][i]
        fit = {"a": sol[0], "b": sol[1], "c": sol[2], "n_points": n,
               "seq_range": [min(p[0] for p in pts), max(p[0] for p in pts)]}

    undocumented = [p["id"] for p in params
                    if not p.get("detail") and p["id"] not in ("preset", "seconds")]
    return {
        "generated_by": "webui/tools/gen_params.py",
        "source": {"plan": "cache/plan.json", "cli": "scripts/h3_generate.py",
                   "docs": "webui/tools/param_docs.py"},
        "params_undocumented": undocumented,
        "panel_docs": PANEL_DOCS,
        "general_notes": [{"title": t, "lines": ls} for t, ls in GENERAL_NOTES],
        "presets": presets,
        "default_preset": resolved.get("default_preset", "standard"),
        "params": params,
        "shape": shape,
        "model": {
            "resolved": {
                "vram_limit_gib": resolved.get("vram_limit_gib"),
                "denoise": denoise,
                "vae": vae,
                "attention": resolved.get("attention", {}),
                "scheduler": resolved.get("scheduler", {}),
                "guidance": resolved.get("guidance", {}),
            },
            "memory": mem,
        },
        "timing_fit": fit,
        "measured": [
            {"label": "blitz 640x384x22/8", "seq": 2944, "step_s": 5.58, "total_s": 52.2, "peak_gib": 3.30},
            {"label": "fast 640x384x39/8", "seq": 4224, "step_s": 8.22, "total_s": 75.5, "peak_gib": 3.29},
            {"label": "draft 640x384x73/20", "seq": 7232, "step_s": 13.59, "total_s": 285.8, "peak_gib": 3.05},
            {"label": "832x480x73/20", "seq": 13184, "step_s": 21.18, "total_s": 443.0, "peak_gib": 3.43},
            {"label": "960x544x73/20", "seq": 15824, "step_s": 26.44, "total_s": 550.0, "peak_gib": 3.88},
            {"label": "1024x576x73/20", "seq": 17024, "step_s": 28.62, "total_s": 594.0, "peak_gib": 4.16},
            {"label": "standard 832x480x124/30", "seq": 17024, "step_s": 33.36, "total_s": 1028.0, "peak_gib": 4.55},
            {"label": "640x384x124/20", "seq": 12288, "step_s": 20.09, "total_s": 422.0, "peak_gib": 3.25},
            {"label": "max-long 640x384x243/20", "seq": 24576, "step_s": 37.07, "total_s": 987.0, "peak_gib": 4.93},
            {"label": "quality 1024x576x124/30", "seq": 23872, "step_s": 48.99, "total_s": 1503.0, "peak_gib": 5.94},
        ],
        "known_failures": [
            {"label": "1344x768x124/30 (官方原生)", "reason": "OOM：干净状态单独跑也超显存（§11.2）"},
            {"label": "832x480x243/30", "reason": "OOM：长序列 + 大画布"},
            {"label": "640x384x345/20", "reason": "OOM：帧数超出 243 的墙"},
        ],
        "notes": [
            "实测一次 OOM 会污染后续所有配置，必须 wsl --shutdown 后重跑（README §11.3）",
            "长片段/高分辨率一律用 dit_onload=disk，避免顶到 WSL 的 22.9 GiB 内存配额",
        ],
    }


def render_markdown(data: dict) -> str:
    """把规格渲染成一份可搜索、可提交的参考文档（docs/PARAMETERS.md）。"""
    by_id = {p["id"]: p for p in data["params"]}
    TICK = "\x60"          # 反引号：写成转义，避免本文件被 shell/编辑器工具链二次解析
    out: list[str] = []
    A = out.append

    A("# 生成参数参考手册")
    A("")
    A("> 本文件由 webui/tools/gen_params.py 从三处自动生成，**不要手改**：")
    A(">")
    A("> - 参数取值与预设：cache/plan.json（scripts/h3_audit.py 产出）")
    A("> - 命令行开关：scripts/h3_generate.py 的 argparse 定义")
    A("> - 中文说明：webui/tools/param_docs.py")
    A(">")
    A("> 重新生成：python3 webui/tools/gen_params.py --write-docs")
    A("")
    A("页面上的参数卡片、页内参数手册、以及本文件，全部来自同一份说明文本 —— 改一处，三处同步。")
    A("")

    if data.get("params_undocumented"):
        A("> 尚未写详细说明的参数：" + "、".join(data["params_undocumented"]) + "。")
        A("")

    A("## 目录")
    A("")
    for title, names in SECTION_ORDER:
        if [n for n in names if n in by_id]:
            A("- [" + title + "](#" + title + ")")
    A("- [补充说明：形状、序列长度、显存与内存](#补充说明形状序列长度显存与内存)")
    A("- [预设一览（实测/校准值）](#预设一览实测校准值)")
    A("")

    for title, names in SECTION_ORDER:
        ids = [n for n in names if n in by_id]
        if not ids:
            continue
        A("## " + title)
        A("")
        for pid in ids:
            p = by_id[pid]
            cli = " ".join(p.get("cli") or [])
            default = p.get("default")
            if isinstance(default, dict) and "__preset__" in default:
                default = "跟随预设（" + default["__preset__"] + "）"
            A("### " + p["label"] + "　" + TICK + (cli or pid) + TICK)
            A("")
            meta = []
            if p.get("unit"):
                meta.append("单位 " + str(p["unit"]))
            if p.get("choices"):
                meta.append("可选 " + " / ".join(p["choices"]))
            if p.get("min") is not None:
                meta.append("范围 " + str(p["min"]) + " ~ " + str(p["max"]))
            if default is not None:
                meta.append("默认 " + str(default))
            if meta:
                A("*" + " · ".join(meta) + "*")
                A("")
            if p.get("help"):
                A("**一句话**：" + p["help"])
                A("")
            if p.get("detail"):
                A(p["detail"])
                A("")
            if p.get("tips"):
                A("**经验与推荐**")
                A("")
                for t in p["tips"]:
                    A("- " + t)
                A("")
            if p.get("risks"):
                A("**风险**")
                A("")
                for t in p["risks"]:
                    A("- " + t)
                A("")
            if p.get("ranges"):
                A("**取值语义**")
                A("")
                A("| 取值 | 含义 |")
                A("|---|---|")
                for rng, meaning in p["ranges"]:
                    A("| " + TICK + str(rng) + TICK + " | " + str(meaning) + " |")
                A("")

    A("## 补充说明：形状、序列长度、显存与内存")
    A("")
    for title, lines in GENERAL_NOTES:
        A("### " + title)
        A("")
        for ln in lines:
            A("- " + ln)
        A("")

    A("## 预设一览（实测/校准值）")
    A("")
    A("| 预设 | 形状 | 步数 | seq | s/步 | 去噪 | 参考图短边 | 参考视频短边 |")
    A("|---|---|---|---|---|---|---|---|")
    for name, p in data.get("presets", {}).items():
        A("| " + TICK + name + TICK + " | " + str(p.get("width")) + "x" + str(p.get("height"))
          + "x" + str(p.get("num_frames")) + " | " + str(p.get("steps")) + " | "
          + str(p.get("seq_len")) + " | " + str(p.get("step_s")) + " | "
          + str(p.get("denoise_min")) + " min | " + str(p.get("ref_image_short_edge")) + " | "
          + str(p.get("ref_video_short_edge")) + " |")
    A("")
    A("已知跑不通的档位（实测）：")
    A("")
    for f in data.get("known_failures", []):
        A("- " + TICK + f["label"] + TICK + "：" + f["reason"])
    A("")

    A("## 实测数据点（耗时预估的插值依据）")
    A("")
    A("| 配置 | seq | s/步 | 总时长 | 峰值显存 |")
    A("|---|---|---|---|---|")
    for m in data.get("measured", []):
        A("| " + m["label"] + " | " + str(m["seq"]) + " | " + str(m["step_s"]) + " | "
          + str(m["total_s"]) + " s | " + str(m["peak_gib"]) + " GiB |")
    A("")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", dest="to_stdout")
    ap.add_argument("--write-docs", action="store_true", dest="write_docs",
                    help="同时渲染 docs/PARAMETERS.md")
    a = ap.parse_args()
    data = build()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if a.to_stdout:
        print(text)
        return 0
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"wrote {OUT} ({len(data['params'])} params, {len(data['presets'])} presets)")
    if data.get("params_undocumented"):
        print("  note: params without detailed docs -> " + ", ".join(data["params_undocumented"]))
    if a.write_docs:
        doc = os.path.join(REPO, "docs", "PARAMETERS.md")
        os.makedirs(os.path.dirname(doc), exist_ok=True)
        with open(doc, "w", encoding="utf-8") as f:
            f.write(render_markdown(data))
        print(f"wrote {doc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
