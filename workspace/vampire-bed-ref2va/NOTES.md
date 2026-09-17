# vampire-bed-ref2va · 推理调试笔记

## 状态：**端到端已跑通 —— `outputs/vampire-bed-quick.mp4`**

| 项 | 结果 |
|---|---|
| 模式 | Ref2VA（全参考） |
| 提示词 | 完成（`prompt.txt`，六段式：subject_definitions / summary / retention_analysis / detailed_description / overall_soundscape / non_diegetic_music） |
| 参考图 | `materials/ref-vampire-bed.jpg`（源图 966x661） |
| 时长规格 | 124 帧 = 5.17 s（符合 `num_frames % 17 == 5` 对齐，最接近你要的 5 秒） |
| 目标预设 | `standard`（832x480x124f，30 步，约 17 分钟）配 124 帧 |
| 输出 | `outputs/vampire-bed-quick.mp4` · 832x480 · 124 帧 · 5.175 s · h264 + aac 32 kHz 立体声 |
| 实测耗时 | 文本编码 20.5 s（冷）→ 去噪 6.1 min（30.7 s/step × 12）→ 解码+封装 ~2 s |
| 峰值显存 | 3.89 GiB / 7.93 GiB |
| 验收 | `outputs/contact-quick.png`（0/25/50/75/100/123 帧拼图） |

---

## 1. 提示词要点

- **<Subject 1>**：银发吸血鬼少女（短发银紫、红瞳重睑、尖耳 + 小黑蝙蝠翼、白色荷叶边睡裙 + 红色领结、白色过膝袜）。
- **<Subject 2>**：第一人称男性双手（托腿 + 卷袜口），无面孔。
- **<Subject 3>**：粉色菱形绗缝床头板、品红缎面床品、红色荷叶枕头、左侧蓝色霓虹灯带。
- **<Subject 4>**：暖色柔光 + 品红主调 + 左侧冷蓝轮廓光。
- **<Picture 1>**：[Shot 1] 的构图/姿势/手部位置锚点。

时间轴：Shot 1（0.00s 起，占位构图 → 推近），Shot 2（00:02.600 切，近景面部 + 袜子继续下卷到结尾 5.17s）。

---

## 2. 复现命令

在 WSL 终端执行（PowerShell 里无法直接调 WSL，会 E_ACCESSDENIED）：

```bash
cd /mnt/d/otherProject/minimax-h3

# 先做一次 dry-run 检查解析
./run_h3.sh dry --preset standard \
  --prompt-file workspace/vampire-bed-ref2va/prompt.txt \
  --ref-image workspace/vampire-bed-ref2va/materials/ref-vampire-bed.jpg \
  --num-frames 124

# 正式生成（5.17s，约 17 分钟）
./run_h3.sh gen --preset standard \
  --prompt-file workspace/vampire-bed-ref2va/prompt.txt \
  --ref-image workspace/vampire-bed-ref2va/materials/ref-vampire-bed.jpg \
  --num-frames 124 \
  --lora ~/source/minimax-h3/models/AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors \
  --out workspace/vampire-bed-ref2va/outputs/vampire-bed.mp4

# 想快：blitz 档（0.92s）或 fast 档（1.62s）
./run_h3.sh gen --preset blitz \
  --prompt-file workspace/vampire-bed-ref2va/prompt.txt \
  --ref-image workspace/vampire-bed-ref2va/materials/ref-vampire-bed.jpg \
  --out workspace/vampire-bed-ref2va/outputs/blitz.mp4
```

---

## 3. 本目录的文件

| 文件 | 内容 |
|---|---|
| `prompt.txt` | 成品提示词（Ref2VA 六段式） |
| `materials/ref-vampire-bed.jpg` | 参考图 |
| `outputs/` | 生成结果（待填） |
| `quality/` | 步数扫档结果（待填） |
| `NOTES.md` | 本文件 |


---

## 4. 续写片段（`vampire-bed-cont.mp4`）

在第一段的**尾帧**上继续生成，采用 **I2VA 续接**方案：把 `outputs/vampire-bed-quick.mp4`
的最后一帧抽成 `materials/last-frame.jpg`，作为第二段的 `<Picture 1>` 首帧锚点，画面完全对得上。

### 抽帧命令

```bash
ffmpeg -sseof -0.05 -i outputs/vampire-bed-quick.mp4 -frames:v 1 materials/last-frame.jpg -y
```

### 复现命令

```bash
./run_h3.sh gen --preset standard \
  --prompt-file workspace/vampire-bed-ref2va/prompt-cont.txt \
  --ref-image workspace/vampire-bed-ref2va/materials/last-frame.jpg \
  --num-frames 124 --steps 12 --ref-image-short-edge 256 \
  --lora ~/source/minimax-h3/models/AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors \
  --out workspace/vampire-bed-ref2va/outputs/vampire-bed-cont.mp4
```

### 实测

| 项 | 结果 |
|---|---|
| 输出 | 832x480 · 124 帧 · 5.175 s · h264 + aac 32 kHz 立体声 |
| 耗时 | 文本编码 20.5 s → 去噪 **5.9 min**（29.6 s/step × 12）→ 解码封装 ~1 s |
| 峰值显存 | 3.75 GiB / 7.93 GiB |
| 文本嵌入 | 684 embeds / 114 vision tokens（比第一段的 1361 更轻，因为尾帧 vision token 更少） |
| 验收 | `outputs/contact-cont.png`（抽帧）、`outputs/stitch-check.png`（尾帧 + 续写前 3 帧拼接对比） |

### 提示词结构差异

续写段用 **I2VA** 而非 Ref2VA：首行是
`For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.`
后接三段核心字段。`prompt-cont.txt` 里 Shot 1 从尾帧状态出发（同样的姿势、手部位置、光照），
Shot 2 在 00:03.000 切近景收尾。

### 备选方案（未跑）

若需要严格的 `[video continuation]` 语义，可改用 Ref2VA + `<Video 1>` 标签，把整段
`vampire-bed-quick.mp4` 作为参考视频喂入（`--ref-video`），代价是参考视频使序列长度接近翻倍，
单次成本显著上升。本轮选择 I2VA 是为在观感无缝的前提下控制成本。

### 两段汇总

| 文件 | 时长 | 说明 |
|---|---|---|
| `outputs/vampire-bed-quick.mp4` | 5.175 s | 第一段（原参考图 Ref2VA，12 步） |
| `outputs/vampire-bed-cont.mp4` | 5.175 s | 第二段（第一段尾帧 I2VA 续写，12 步） |

两段连播合计 10.35 s。

```bash
# 直接拼成一条 10.35 s 的连续视频
cd workspace/vampire-bed-ref2va
printf "file 'outputs/vampire-bed-quick.mp4'\nfile 'outputs/vampire-bed-cont.mp4'\n" > /tmp/list.txt
ffmpeg -f concat -safe 0 -i /tmp/list.txt -c copy outputs/vampire-bed-full.mp4 -y
```
