"""webui.backend.promptopt -- 中文创作意图 -> 流水线可用的结构化英文提示词。

为什么需要这一层：参考文档(references/*.md)写的是**英文提示词规范**，而使用者是中文用户。
直接让用户看英文规范不现实，所以这里做三件事：
  1. 把「工程自己的提示词规范」(config/prompts/*.md) 拼成 system prompt；
  2. 把中文意图 + 目标视频参数 + 参考素材清单（含 <Picture n>/<Video n>/<Audio n> 编号）
     拼成 user message；
  3. 要求模型用固定标记输出 【英文提示词】【中文回译】【中文说明】三段，
     前端把英文段落送进流水线、把中文段落显示给人看。

标记协议（与 config/prompts/system.md 中约定的一致）：
    <<<H3_PROMPT>>> ... <<<END_H3_PROMPT>>>
    <<<H3_ZH>>>     ... <<<END_H3_ZH>>>
    <<<H3_NOTES>>>  ... <<<END_H3_NOTES>>>
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from . import config as cfgmod
from . import media
from .estimate import rows_for

MARKERS = {
    "prompt": ("<<<H3_PROMPT>>>", "<<<END_H3_PROMPT>>>"),
    "translation": ("<<<H3_ZH>>>", "<<<END_H3_ZH>>>"),
    "notes": ("<<<H3_NOTES>>>", "<<<END_H3_NOTES>>>"),
}


# --------------------------------------------------------------------------- 模式判定
def infer_mode(refs: list[dict]) -> tuple[str, str, str]:
    """按参考素材判定该用哪种提示词结构。

    返回 (结构键, 中文模式名, 判定理由)。
    结构键对应 config/prompts/format-*.md 与 h3_generate 的实际输入路径：
      ref2va  六个分段（有视频/音频/多图混合）
      oneref  指令行 + 三核心字段（纯图片关键帧：i2va / fl2va / l2va）
      base    三核心字段（无画面参考：t2va）
    """
    kinds = [(r.get("kind") or "image") for r in refs]
    n_img = sum(1 for k in kinds if k == "image")
    n_vid = sum(1 for k in kinds if k in ("video", "video_audio"))
    n_aud = sum(1 for k in kinds if k in ("audio", "video_audio"))

    if n_vid or n_aud:
        return ("ref2va", "全参考 Ref2VA（六段式）",
                f"参考素材里有 {'视频' if n_vid else ''}{'音频' if n_aud else ''}，"
                "走全参考的重写结构")
    if n_img == 0:
        return ("base", "文生音视频 T2VA（三核心字段）", "没有画面参考，只能从文字构建整条时间线")
    if n_img == 1:
        return ("oneref", "图生音视频 I2VA（首帧锚定）",
                "只有一张参考图：按官方规范它被当作 0.00 秒的首帧锚点")
    if n_img == 2:
        return ("oneref", "首尾帧 FL2VA（首尾帧之间插值）",
                "两张参考图：按官方规范当作首帧与尾帧，描述两者之间的运动路径")
    return ("ref2va", f"多图参考（{n_img} 张）",
            "三张及以上参考图不足以确定为关键帧关系，走全参考结构逐张定义")


# --------------------------------------------------------------------------- system prompt
def build_system(cfg: dict, mode_key: str) -> tuple[str, list[str]]:
    p = cfg.get("prompt") or {}
    parts: list[str] = []
    sources: list[str] = []

    inline = (p.get("system_prompt_inline") or "").strip()
    path = p.get("system_prompt_file")
    if path and os.path.exists(path):
        parts.append(open(path, encoding="utf-8").read().strip())
        sources.append(path)
    elif inline:
        parts.append(inline)
        sources.append("config/deepseek.yaml: prompt.system_prompt_inline")
    else:
        parts.append("你是 MiniMax-H3 视频生成提示词工程师，把中文意图改写成结构化英文提示词。")
        sources.append("(内置最小 system prompt)")

    digest = (p.get("mode_digests") or {}).get(mode_key)
    if digest and os.path.exists(digest):
        parts.append("# 结构规范\n\n" + open(digest, encoding="utf-8").read().strip())
        sources.append(digest)
    elif digest:
        parts.append(f"# 结构规范\n\n(文件缺失：{digest})")

    extra = (p.get("extra_instructions") or "").strip()
    if extra:
        parts.append("# 追加要求\n\n" + extra)

    return "\n\n---\n\n".join(parts), sources


# --------------------------------------------------------------------------- user message
def _fmt_refs(refs: list[dict]) -> str:
    if not refs:
        return "（无参考素材：纯文本生成）"
    lines = []
    for i, r in enumerate(refs, 1):
        label = r.get("label") or ""
        kind = r.get("kind") or "image"
        zh = {"image": "参考图", "video": "参考视频(静音)", "video_audio": "参考视频(带音轨)",
              "audio": "纯音频参考"}.get(kind, kind)
        size = ""
        if r.get("w") and r.get("h"):
            size = f" {int(r['w'])}x{int(r['h'])}"
        if r.get("frames"):
            size += f" {int(r['frames'])}帧"
        lines.append(f"  {i}. {zh} {label} 文件 {r.get('name') or '(未命名)'}{size}")
    return "\n".join(lines)


def _fmt_attachments(attachments: list[dict]) -> str:
    """把「随消息附上的图片」列成清单。

    多模态下 user content 是 [text, image_url, ...]，图片不带文件名；所以必须在文本里
    把「第 i 张图 = 哪个参考素材标签」写清楚，模型才能把看到的画面和 <Picture n> 对上号。
    """
    lines = ["## 随本消息附上的图片（顺序即下面的编号，与「参考素材清单」一一对应）"]
    for i, a in enumerate(attachments, 1):
        label = a.get("label") or a.get("kind") or "图片"
        sent = ""
        if a.get("sent_width") and a.get("sent_height"):
            sent = f"{int(a['sent_width'])}x{int(a['sent_height'])}"
        orig = ""
        if (a.get("width") and a.get("height")
                and (a["width"], a["height"]) != (a.get("sent_width"), a.get("sent_height"))):
            orig = f"（原图 {int(a['width'])}x{int(a['height'])}）"
        note = f"；{a['note']}" if a.get("note") else ""
        lines.append(f"{i}. {label} · {a.get('name')} · {sent}{orig}{note}")
    lines.append("务必以这些图片的实际内容为准（人物外貌、服装、配色、场景、构图），不要凭文字想象。")
    return "\n".join(lines)


def build_user_message(req: dict, mode_key: str, mode_zh: str, mode_why: str,
                       attachments: list[dict] | None = None) -> str:
    width, height, frames = int(req.get("width") or 832), int(req.get("height") or 480), \
        int(req.get("num_frames") or 124)
    fps = 24
    secs = frames / fps
    refs = req.get("refs") or []
    rows = rows_for(width, height, frames, refs=refs,
                    ref_image_short_edge=req.get("ref_image_short_edge"),
                    ref_video_short_edge=req.get("ref_video_short_edge"))
    wants = req.get("wants") or []

    blocks = [
        "## 用户中文创作意图\n" + (req.get("chinese") or "").strip(),
        "## 目标视频参数\n"
        f"- 分辨率：{width}x{height}（{rows['aligned']['width']}x{rows['aligned']['height']} 对齐后）\n"
        f"- 帧数：{frames} 帧 @ {fps} fps\n"
        f"- **时长：{secs:.2f} 秒**（描述必须覆盖且不超过这个时长）\n"
        f"- 采样步数：{req.get('steps')}；随机种子：{req.get('seed')}\n"
        f"- 是否挂 LoRA：{'是' if req.get('lora') else '否'}"
        + ("（挂 LoRA 时音频更依赖低 sigma 段，描述里可以多写持续音与氛围）" if req.get("lora") else ""),
        "## 输入模式\n"
        f"- 结构：{mode_key}（{mode_zh}）\n- 判定理由：{mode_why}\n"
        f"- 序列长度估算：seq≈{rows['seq_len']}（目标视频 {rows['target_video']} 行 + "
        f"参考 {'+'.join(str(rows[k]) for k in ('ref_image', 'ref_video', 'ref_audio'))} 行）",
        "## 参考素材清单（顺序即编号依据，不得改动）\n" + _fmt_refs(refs),
    ]
    if attachments:
        # 多模态附带图片：把「第 i 张图 = 哪个 <Picture n>/<Video n>」写死在文本里
        blocks.append(_fmt_attachments(attachments))
    if wants:
        blocks.append("## 用户特别要求\n" + "\n".join(f"- {w}" for w in wants))
    blocks.append(
        "## 你的任务\n"
        f"按「{mode_key}」的结构规范，写出这段 {secs:.2f} 秒视频的完整英文提示词，"
        "并在最后附上中文回译与简短说明。三个标记必须成对出现。")
    return "\n\n".join(blocks)


def build_user_content(user_text: str, attachments: list[dict],
                       detail: str | None) -> str | list:
    """把 user 文本与图片附件拼成 OpenAI 兼容的 content。

    没有附件时**原样返回字符串** —— 这样不开多模态时请求体与以前逐字节一致。
    有附件时返回块数组：[{"type":"text",...}, {"type":"image_url",...}, ...]。
    """
    if not attachments:
        return user_text
    blocks: list[dict] = [{"type": "text", "text": user_text}]
    for att in attachments:
        uri = att.get("data_uri")
        if not uri:
            continue
        img: dict = {"url": uri}
        d = (detail or "").strip()
        if d:
            img["detail"] = d
        blocks.append({"type": "image_url", "image_url": img})
    # 所有附件都缺 data_uri 时退回纯文本，避免发出一个只有文本块的数组
    return blocks if len(blocks) > 1 else user_text


# --------------------------------------------------------------------------- API
def _post_stream(url: str, headers: dict, payload: dict, timeout: float):
    """返回逐行的 SSE/JSON 行迭代器。用标准库 urllib，避免引入额外依赖。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp


def _iter_stream(resp):
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _extract_delta(obj: dict) -> tuple[str, str]:
    """从一条流式 chunk 里取出 (正文增量, 思考增量)。"""
    choices = obj.get("choices") or []
    if not choices:
        return "", ""
    ch = choices[0] or {}
    delta = ch.get("delta") or ch.get("message") or {}
    content = delta.get("content") or ""
    reasoning = (delta.get("reasoning_content") or delta.get("reasoning")
                 or ch.get("reasoning_content") or "")
    return content, reasoning


class Optimizer:
    def __init__(self, log, llm_runs=None):
        self.log = log
        # llm_runs 是 runlog.LLMRunStore（可为 None：只用内存日志跑测试时）
        self.llm_runs = llm_runs

    def _payload(self, cfg: dict, system: str, content) -> dict:
        # content 可以是字符串（纯文本，历史行为），也可以是 content 块数组（多模态附图）
        model = cfg.get("model") or {}
        payload: dict = {
            "model": model.get("name") or "deepseek-chat",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "stream": bool(model.get("stream", True)),
        }
        if model.get("max_tokens"):
            payload["max_tokens"] = int(model["max_tokens"])
        thinking = model.get("thinking") or {}
        if thinking.get("enabled"):
            style = thinking.get("style") or "reasoning_effort"
            if style in ("reasoning_effort", "both"):
                payload["reasoning_effort"] = thinking.get("reasoning_effort") or "medium"
            if style in ("thinking", "both"):
                payload["thinking"] = {"type": "enabled"}
        else:
            # 思考模式下部分采样参数不被支持，所以只在非思考模式带 temperature
            if model.get("temperature") is not None:
                payload["temperature"] = float(model["temperature"])
        return payload

    def stream(self, req: dict):
        """生成器：yield 事件字典，供 SSE 直接转发。

        事件类型：meta / delta / section / done / error
        LLM 每次调用都会在 self.llm_runs 里留下一条完整记录（见 runlog.LLMRunStore）：
        请求参数、system / user 全文、流式输出、思考、tokens、耗时、报错、是否被中断。
        页面上「日志 → LLM 调用记录」可以直接回看，服务重启也不丢。
        """
        cfg = cfgmod.deepseek_config()
        api = cfg.get("api") or {}
        if not api.get("key_present"):
            self.log.warn("promptopt: 未配置 API Key，拒绝调用", source="llm",
                          event="llm.no_key")
            yield {"type": "error",
                   "message": "没有配置 DeepSeek API Key",
                   "hint": f"在 config/deepseek.yaml 的 api.key 里填，"
                           f"或设置环境变量 {api.get('key_env') or 'DEEPSEEK_API_KEY'} 后重启服务。"}
            return

        refs = req.get("refs") or []
        mode_key, mode_zh, mode_why = infer_mode(refs)
        system, sources = build_system(cfg, mode_key)
        # 多模态：把本地参考图读成 base64 图片块附在 user message 上。失败只记 note，不阻断。
        mm = cfg.get("multimodal") or {}
        attachments, mm_notes = media.collect_attachments(refs, mm, log=self.log)
        mm_public = [media.public_attachment(a) for a in attachments]
        user = build_user_message(req, mode_key, mode_zh, mode_why, attachments)
        content = build_user_content(user, attachments, mm.get("detail"))
        model = cfg.get("model") or {}
        payload = self._payload(cfg, system, content)
        url = api.get("url") or "https://api.deepseek.com/chat/completions"
        thinking = bool((model.get("thinking") or {}).get("enabled"))
        src_labels = [os.path.relpath(s, cfgmod.ROOT) if os.path.isabs(s) else s
                      for s in sources]
        mm_info = {"enabled": bool(mm.get("enabled")), "images": len(attachments),
                   "detail": mm.get("detail"), "attachments": mm_public,
                   "notes": mm_notes}

        run = None
        if self.llm_runs is not None:
            run = self.llm_runs.begin({"model": payload.get("model"), "url": url,
                                       "mode": mode_key, "thinking": thinking,
                                       "stream": bool(payload.get("stream")),
                                       "images": len(attachments)})
            run.set_request(payload, system, user, src_labels, attachments=mm_public)

        self.log.info("promptopt: 请求 DeepSeek", source="llm", event="llm.request",
                      run=(run.id if run else None), model=payload.get("model"),
                      mode=mode_key, system_chars=len(system), user_chars=len(user),
                      images=len(attachments),
                      thinking=thinking, stream=bool(payload.get("stream")), url=url)

        try:
            yield from self._stream_llm(run, api, model, payload, system, user, src_labels,
                                        mode_key, mode_zh, mode_why, refs, url, req, mm_info)
        except GeneratorExit:
            # 前端关掉页面 / 主动断开：把这次调用如实标成 aborted，而不是假装成功
            if run is not None:
                run.abort("客户端断开，SSE 生成器被关闭")
                self.log.warn("promptopt: 客户端断开，LLM 调用中止", source="llm",
                              event="llm.abort", run=run.id)
            raise

    def _stream_llm(self, run, api, model, payload, system, user, src_labels,
                    mode_key, mode_zh, mode_why, refs, url, req, mm_info):
        """真正发请求并解析流。由 stream() 包一层，专门处理「被中断」这种情况。"""
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + api["key"],
                   "Accept": "text/event-stream" if payload.get("stream") else "application/json"}
        timeout = float(api.get("timeout_s") or 300)
        retries = int(api.get("max_retries") or 0)

        yield {"type": "meta", "mode": mode_key, "mode_zh": mode_zh, "mode_why": mode_why,
               "model": payload["model"],
               "thinking": bool((model.get("thinking") or {}).get("enabled")),
               "reasoning_effort": payload.get("reasoning_effort"),
               "system_sources": src_labels,
               "system_prompt": system, "user_message": user,
               "multimodal": mm_info or {"enabled": False, "images": 0,
                                         "attachments": [], "notes": []},
               "url": api.get("url"), "run_id": (run.id if run else None)}

        def fail(message, detail=None, http_status=None, hint=None):
            """统一的失败收尾：记录 -> 落盘 -> yield 错误事件。"""
            if run is not None:
                err = {"message": message}
                if detail:
                    err["detail"] = detail
                if http_status:
                    err["http_status"] = http_status
                run.finish("error", err)
            self.log.error("promptopt: LLM 调用失败", source="llm", event="llm.error",
                           run=(run.id if run else None), error=message,
                           http_status=http_status, detail=(detail or "")[:300])
            ev = {"type": "error", "message": message, "run_id": (run.id if run else None)}
            if detail:
                ev["detail"] = detail
            if hint:
                ev["hint"] = hint
            return ev

        attempt = 0
        while True:
            try:
                resp = _post_stream(url, headers, payload, timeout)
                break
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:600]
                except Exception:
                    pass
                msg = f"DeepSeek 返回 HTTP {e.code}"
                if e.code in (401, 403):
                    msg += "：API Key 无效或没有权限"
                elif e.code == 402:
                    msg += "：账户余额不足"
                elif e.code == 429:
                    msg += "：触发限流"
                if e.code >= 500 and attempt < retries:
                    attempt += 1
                    if run is not None:
                        run.note_event("retry", msg, attempt=attempt, total=retries)
                    self.log.warn("promptopt: DeepSeek 5xx，准备重试", source="llm",
                                  event="llm.retry", run=(run.id if run else None),
                                  http_status=e.code, attempt=attempt, retries=retries)
                    yield {"type": "warn", "message": f"{msg}，{attempt}/{retries} 次重试…"}
                    time.sleep(1.5 * attempt)
                    continue
                yield fail(msg, detail=body, http_status=e.code,
                           hint="检查 config/deepseek.yaml 的 api.url / api.key / model.name 是否与"
                                "服务商文档一致；若开了 thinking，注意部分采样参数不被支持。")
                return
            except Exception as e:
                if attempt < retries:
                    attempt += 1
                    if run is not None:
                        run.note_event("retry", f"请求失败：{e}", attempt=attempt, total=retries)
                    self.log.warn("promptopt: 请求失败，准备重试", source="llm",
                                  event="llm.retry", run=(run.id if run else None),
                                  error=str(e), attempt=attempt, retries=retries)
                    yield {"type": "warn", "message": f"请求失败（{e}），{attempt}/{retries} 次重试…"}
                    time.sleep(1.5 * attempt)
                    continue
                yield fail(f"无法连接 DeepSeek：{e}",
                           hint="确认本机（WSL 内）能访问外网；用 curl 试一下 api.url 更直观。")
                return

        # ---- 解析流 --------------------------------------------------------
        state = {"section": None, "buf": "", "text": {"prompt": "", "translation": "", "notes": ""},
                 "usage": None}
        order = ["prompt", "translation", "notes"]

        def feed(content: str):
            """把增量喂进标记状态机，返回 (section, 文本增量) 列表。"""
            out = []
            state["buf"] += content
            while state["buf"]:
                cur = state["section"]
                if cur is None:
                    # 找下一个开始标记
                    best, at = None, -1
                    for name, (start, _) in MARKERS.items():
                        i = state["buf"].find(start)
                        if i >= 0 and (at < 0 or i < at):
                            best, at = name, i
                    if best is None:
                        # 丢掉标记之前的噪声（模型偶尔会先说一句废话）
                        if len(state["buf"]) > 64:
                            state["buf"] = state["buf"][-32:]
                        return out
                    state["buf"] = state["buf"][at + len(MARKERS[best][0]):]
                    state["section"] = best
                    out.append(("section", best))
                else:
                    end = MARKERS[cur][1]
                    i = state["buf"].find(end)
                    if i < 0:
                        # 保留可能是半个结束标记的尾巴
                        keep = max(0, len(state["buf"]) - len(end))
                        chunk, state["buf"] = state["buf"][:keep], state["buf"][keep:]
                        if chunk:
                            state["text"][cur] += chunk
                            out.append((cur, chunk))
                        return out
                    chunk = state["buf"][:i]
                    state["buf"] = state["buf"][i + len(end):]
                    if chunk:
                        state["text"][cur] += chunk
                        out.append((cur, chunk))
                    state["section"] = None
            return out

        try:
            if payload.get("stream"):
                for obj in _iter_stream(resp):
                    if obj.get("usage"):
                        state["usage"] = obj["usage"]
                    content, reasoning = _extract_delta(obj)
                    if reasoning:
                        if run is not None:
                            run.note_thinking(reasoning)
                        yield {"type": "thinking", "text": reasoning}
                    if content:
                        for section, chunk in feed(content):
                            if section == "section":
                                yield {"type": "section", "section": chunk}
                            else:
                                if run is not None:
                                    run.note_delta(section, chunk)
                                yield {"type": "delta", "section": section, "text": chunk}
            else:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    obj = json.loads(raw)
                    content, reasoning = _extract_delta(obj)
                    state["usage"] = obj.get("usage") or state["usage"]
                except json.JSONDecodeError:
                    yield fail("服务端返回的不是合法 JSON", detail=raw[:600])
                    return
                if reasoning:
                    if run is not None:
                        run.note_thinking(reasoning)
                    yield {"type": "thinking", "text": reasoning}
                for section, chunk in feed(content):
                    if section != "section":
                        if run is not None:
                            run.note_delta(section, chunk)
                        yield {"type": "delta", "section": section, "text": chunk}
        except Exception as e:
            yield fail(f"读取响应流失败：{e}")
            return

        text = {k: v.strip() for k, v in state["text"].items()}
        missing = [k for k in order if not text.get(k)]
        if run is not None:
            run.set_usage(state["usage"])
            run.finish("ok", missing=missing)
        yield {"type": "done", "result": text.get("prompt", ""),
               "translation": text.get("translation", ""), "notes": text.get("notes", ""),
               "missing": missing, "usage": state["usage"],
               "refs": refs, "mode": mode_key, "run_id": (run.id if run else None),
               "duration_s": round(int(req.get("num_frames") or 0) / 24.0, 2)}
