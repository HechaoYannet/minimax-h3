# MiniMax-H3 · WSL 部署工作流与参数调优

> 目标机器：RTX 5070 Laptop (8 GiB, sm_120) / 32 GiB 宿主内存（WSL2 分到 **22.91 GiB**）/ 24 线程 / WSL2 (Ubuntu)
> 权重与框架位置：`~/source/minimax-h3/`（`models/` + `DiffSynth-Studio/`）
> 本仓库：`D:\otherProject\minimax-h3`（WSL 中为 `/mnt/d/otherProject/minimax-h3`）
> **端到端已跑通**（`workspace/pink-harem-ref2va/`，见 §10 实测记录）。§1~§7 的性能数字是**未跑推理时**的微基准外推，§10 给出实测对照 —— 有几项差得比较远，以 §10 为准。

---

## 0. 结论摘要

| 项目 | 结论 |
|---|---|
| 能否跑 | **能，已实测出片。** 三个环境问题见 §3：torchaudio 已换官方 cu132 版、processor 已就位、**torchcodec 缺 FFmpeg 共享库**（只影响参考视频/音频，已在本工作流内兜底） |
| 推荐起点 | `draft` 预设（640×384×73f，20 步）= **4.7 分钟** 实测出片（预测 2.7 分钟） |
| 交付档 | `standard` 预设（832×480×124f，30 步）= **24 分钟**（按实测 s/step 外推；预测 12.1 分钟） |
| 官方默认档 | 768×1344 + 50 步 ≈ **2.5 小时** 外推，本机不建议（序列长 2.3×，注意力是平方项） |
| 最大瓶颈 | **不是序列长度，也不是显存** —— 是 264 个 NF4 Linear 里只有 25 个能常驻，其余每步从 disk map 重建，吃掉单步 **33%** 的 CPU 时间（§10.2） |
| 显存 | DiT 9.76 GiB 装不进 8 GiB，约 3.5 GiB 常驻 + 6.3 GiB 每步重拷。**实测峰值只用 3.05 GiB / 7.93 GiB** |
| 内存 | WSL 现有 **22.91 GiB**。DiT 常驻 CPU 时 RSS 峰值 **11.08 GiB**（实测），`--dit-onload disk` 降到 **4.75 GiB** |
| 最大的免费优化 | 参考图的 `ref_image_short_edge`：框架默认 2048 对一张 1024²图要多花 **54% 注意力**，改成 1024 只要 12% |
| LoRA 要求 | **euler + beta scheduler**。Euler 框架本来就是；beta 已实现（`scripts/h3_scheduler.py`），挂 LoRA 时自动启用 |

---

## 1. 硬件实测（`scripts/h3_bench.py`）

nvidia-smi 之外，真正决定配置的是这几项：

| 指标 | 实测值 | 对配置的影响 |
|---|---|---|
| 显存 | 7.93 GiB 总 / 启动时可用 **6.84 GiB**（上下文+桌面已占 1.10） | `vram_limit` 的计算基准 |
| 宿主内存 | 32 GiB 物理；WSL2 配额 `memory=25165824000` → 实测 **22.91 GiB 总 / 21.57 GiB 可用**，swap 8 GiB | 决定 DiT 能否常驻 CPU（现已很宽裕） |
| 稠密 bf16 GEMM | **47.1 TFLOP/s** | 时间模型的分子 |
| **NF4 (bitsandbytes) GEMM** | **44.5 TFLOP/s（稠密的 94%）** | NF4 几乎是免费的，不用为此牺牲质量 |
| SDPA 注意力 | **47.2 TFLOP/s**（cuDNN 后端，真实 `[1,H,S,D]` 视图布局） | 注意力≈线性层同级开销 |
| H2D 拷贝（pageable） | **3.60 GB/s** | 框架 `module.to('cuda')` 走这条 |
| H2D 拷贝（pinned） | **28.2 GB/s** | 7.8×，尚未被框架利用（见 §8） |
| 层流式加载 `deepcopy+cuda` | **5.60 GiB/s（= 6.01 GB/s）** 有效 | 显存不足时每步补充代价 |
| 权重盘顺序读 | 2.89 GB/s（冷读 2 GiB） | 磁盘流式可行 |
| 注意力后端 | `torch`（未装 flash_attn / sage / xformers） | 见 §8 建议 |
| torch | 2.14.0+cu132，arch list 含 sm_120 | 正常 |
| torchaudio | 2.11.0+cu132（`test/` 频道；内含 cu130 的 `.so`，见 §3.1） | 原生 import 可用，垫片已降级 |

> 这些数字是在 **WSL 内存扩到 22.91 GiB 之后重测**的。同一张卡两次测量，GEMM / 注意力 / H2D 那几项就差 8%~10%（GPU 时钟与热状态），**不要把全部涨幅归因于内存**。真正超出这个波动范围的只有 `layer streaming`（3.69 → 5.60 GiB/s，+52%），那与内存压力消失是一致的。

---

## 2. 模型文件与架构事实（`scripts/h3_audit.py` + `scripts/h3_validate.py`）

5 个文件全部被框架正确识别（hash = 排序后 `key:shape` 的 md5）：

| 文件 | 大小 | 识别为 | 量化 |
|---|---|---|---|
| `minimax-h3-ref2va-pruned-nf4.safetensors` | 9.76 GiB | `MiniMaxH3DiTComfyPruned` | bitsandbytes_nf4（208 个 Linear） |
| `minimax-h3-text-encoder-nf4.safetensors` | 14.27 GiB | `MiniMaxH3TextEncoder`（Qwen3-VL，50 层，hidden 5120） | nf4（350 个 Linear） |
| `video_vae_nf4.safetensors` | 1.50 GiB | `MiniMaxH3VideoVAE` | nf4（145 个 Linear） |
| `audio_vae_nf4.safetensors` | 271 MiB | `MiniMaxH3AudioVAE` | nf4（6 个 Linear） |
| `AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors` | 1.14 GiB (F32) | 用户 LoRA，rank 64 | — |

**架构关键数字（决定一切性能）**

- DiT 线性层参数 **20.038 B**（由 meta device 实测，不是估算）：每 block = qkv 5376→21504 + out 5376→**7168** + fc1 5376→28672(gated) + fc2 14336→5376 = 385.3 M；50 blocks + 2 个 token_refiner block。
- AdaLN 投影在 **pruned** 权重里只有 **8→96768**（2688 维时间嵌入被 1025×8 的 `adaln_t_table` 取代），所以每 block 只 0.87 M，不是 260 M。
- NF4 打包 **0.5000 byte/param**，双重量化开销另计（文件/参数 = 0.529）。
- 音频 latent：`round(frames/24*40)` 帧 × 2 声道；每 17 帧对应 5 个 video latent 帧。
- 时间维对齐：`num_frames % 17 == 5`，h/w 必须是 32 的倍数。

**量化一致性强校验**：把模型建在 meta device 上，逐个 Linear 走框架自己的 `_should_quantize()`，与文件里实际打包的 key 对比 —— DiT 208/56、文本编码器 350/116，**双向 0 不匹配**。唯一未命中的排除项是 `time_embedder.proj_in/proj_out`（pruned 变体里该模块已被查表取代），无害。

---

## 3. 三个环境问题的现状

### 3.1 torchaudio：装的是官方 cu132 版，但它其实是 **cu130 的重打包**

**现象**（环境搭建时）：`import torchaudio` 直接崩，整条 diffsynth 链路 import 不进来。

```
RuntimeError: Detected that PyTorch and TorchAudio were compiled with different
CUDA versions. PyTorch has CUDA version 13.2 whereas TorchAudio has CUDA version 13.0.
```

- 成因：搭建时只跑了 `pip3 install torch torchvision --index-url .../whl/cu132`，torchaudio 是随后 `pip install -e ".[all]"` 按**默认 PyPI 源**顺带装进来的（`pyproject.toml:42` 列了它），于是拿到的二进制编译在 CUDA 13.0 上。
- 影响面：`transformers.audio_utils` 会 import torchaudio，`diffsynth.utils.data.audio` 在模块顶层 import 它，而 H3 pipeline 依赖后者 —— **连 pipeline 都构造不出来**。

**正确装法（本机现在就是这个状态）**：cu132 的 torchaudio 在 **test 频道**，稳定频道没有它。

```bash
pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/test/cu132
```

- `https://download.pytorch.org/whl/cu132`（稳定频道）只有 torch + torchvision；在那里加 torchaudio 会直接 `ERROR: Could not find a version that satisfies the requirement torchaudio`。
- `https://download.pytorch.org/whl/test/cu132` 三者齐全，实测解析到 `torch-2.14.0+cu132` / `torchvision-0.29.0+cu132` / `torchaudio-2.11.0+cu132`；cp310~cp314 与 x86_64/aarch64/win_amd64 的 wheel 都有。

**必须知道的坑：`2.11.0+cu132` 不是真正的 CUDA 13.2 编译，而是 cu130 构建的重打包。**

| 检查项 | 结果 |
|---|---|
| 三个 `.so` 的 md5 | 与 cu130 wheel **逐字节相同** |
| `torch.ops._torchaudio.cuda_version()` | **13000**（不是 13020） |
| `torchaudio.__version__`（运行时自报） | **`2.11.0+cu130`** |
| `importlib.metadata` 分发名 | `2.11.0+cu132` |
| `_extension/utils.py` | 检查被换成 `pass  # CUDA version mismatch check disabled by repackage_torchaudio_cu130_to_cu132.py` |

也就是说 **ABI 与 cu130 版完全一致**：你拿到的是「官方替你拆掉了版本门」，而不是一份新编译。旁证：pytorch/pytorch#183336 `[release] Dockerfile: skip torchaudio install when CUDA_PATH=cu132`。

- 因此 `import torchaudio` 现在**原生可用**；`scripts/h3_compat.py` 自动降级为 no-op（返回 `torchaudio already importable (no shim needed)`），保留它作为「万一环境又装回 PyPI cu130 版」的兜底。
- 直接跑 `python scripts/h3_compat.py` 会同时打印**分发名**与**运行时标签**，避免这两个数字被误读成装坏了。
- **上一版文档在这里写错了两处，一并更正**：
  - ❌「不存在 cu132 的 torchaudio」→ ✅ 存在，只是只在 `test/` 频道、且是重打包。
  - ❌「版本检查永远不可能通过」→ ✅ 源码是 `f"{version_str[:-3]}.{version_str[-2]}"`（**单字符**下标，13020 → `"13.2"`），真正匹配的构建是**能通过**的。上一版把它写成 `[-2:]`（→ `"13.20"`）才推出「永远不可能」，那个论证不成立。

### 3.2 缺少 processor/tokenizer —— 任何 prompt 都无法编码

框架要 `MiniMax/MiniMax-H3` 的 `Ref2VA/processor/`（**11 MiB**：tokenizer.json / vocab.json / merges.txt / tokenizer_config.json / chat_template.json / 两个 preprocessor 配置）。原环境完全没有（无 HF/modelscope 缓存）。

- 已一次性拉取到 `~/source/minimax-h3/models/MiniMax-H3/Ref2VA/`（HuggingFace 在本网络不可达，走 ModelScope 镜像）。**没有下载任何其他文件**，权重仍用本地 NF4。
- 复现命令：`./run_h3.sh fetch`（`scripts/h3_fetch_processor.py`，幂等）。

### 3.3 torchcodec 加载不了 FFmpeg —— 框架的**音频**读取器在本机是坏的

**症状**：`diffsynth.utils.data.audio.read_audio()`（以及 `torchaudio.load`）对**任何**音频文件都抛错：

```
RuntimeError: Could not load libtorchcodec. Likely causes:
  1. FFmpeg is not properly installed in your environment. ...
OSError: libavutil.so.61: cannot open shared object file: No such file or directory
```

- 根因：torchcodec 需要 **FFmpeg 的共享库**（`libavutil` / `libavcodec` / `libavformat` …），而本机一个都没有 —— `ldconfig -p | grep libav` 是空的；`torchcodec.libs/` 里只有图像编解码（libavif / libjpeg / libnvjpeg / libpng / libwebp）；唯一的 ffmpeg 是 `imageio-ffmpeg` 带的**独立可执行文件**，不提供 `.so`。
- 影响面比看上去大：
  - `torchaudio.load` 从 2.9 起已改为转发 torchcodec，所以它同样失效 —— 这和 §3.1 的 CUDA 版本检查是**两件独立的事**，换 cu132 版解决不了它。
  - `read_video_audio()` 把 `read_audio` 包在**裸 `except`** 里，失败时静默返回 `waveform=None`；官方示例那种引用会变成 `{"type": "video_audio", "audio": None}`，然后在框架的 `_require()` 上断言失败 —— **报错信息完全指不到真正的原因**。
- **视频不受影响**：`VideoData` 走的是 `imageio.get_reader` → imageio 自带的 ffmpeg 可执行文件，实测 320×240/24fps/3s 的源能正常解出 72 帧。

**本工作流的处理**（`scripts/h3_generate.py`）：

- 新增 `decode_audio()`：先试框架的 `read_audio`，失败则用 **imageio 那个 ffmpeg 可执行文件**解成 32-bit float WAV，再交给 `soundfile` 读入（mp3 / wav / flac 与 mp4 音轨走同一条路）。只提示一次，不刷屏。
- `--ref-video-audio` 在框架返回 `None` 时用同一路径补解码，并**复刻框架的对齐规则**（把音频裁到保留帧数覆盖的时长）。实测 72 帧 = 3.00 s ↔ 132300 采样 @ 44.1 kHz = 3.00 s。
- 失败时给的是能看懂的提示（`no decodable audio track found; use --ref-video ...`），而不是 torchcodec 的加载栈。

**想根治**（之后可以撤掉上面的兜底）：装 FFmpeg 共享库，例如 `sudo apt install -y ffmpeg` 或 `conda install -c conda-forge ffmpeg`。装完 `read_audio` / `torchaudio.load` 就都能用，而兜底是 try-first 的，会自动不再触发。

---

## 4. 工作流

```
scripts/h3_compat.py            torchaudio 垫片（现已无需生效，留作兜底；仍必须先 import）
scripts/h3_bench.py             硬件微基准 → cache/bench.json
scripts/h3_validate.py          前端静态校验（不加载权重）→ cache/validate.json
scripts/h3_audit.py             文件清点 + 预设 + 显存/内存方案 → cache/plan.json
scripts/h3_scheduler.py         euler/beta 调度：替换 sigma 网格落点
scripts/h3_generate.py          生成主程序（读 plan.json 作为默认值）
scripts/h3_fetch_processor.py   一次性拉取 processor/tokenizer
env/h3_env.sh                   环境变量（source 它）
run_h3.sh                       统一入口
```

### 四个阶段

| 阶段 | 模型 | 显存/内存 | 说明 |
|---|---|---|---|
| ① 参考编码 | video_vae + audio_vae | RAM 1.8 GiB | 参考图/视频 → latent anchor |
| ② Prompt 编码 | text_encoder (14.27 GiB) | 纯磁盘流式（22.9 GiB 下也可改常驻 CPU，见 §6） | 26 B 参数，单次前向 |
| ③ 去噪 | dit (9.76 GiB) | GPU 常驻 ~3.5 + CPU 9.5 | 每步一个前向（cfg_scale=1.0） |
| ④ 解码 | video_vae + audio_vae | 分块 tiled | 151→124 帧 |

框架用 `load_models_to_device()` 在阶段间互斥换入换出，阶段③开始时 DiT 全部 onload 到 CPU。

### 常用命令

```bash
cd /mnt/d/otherProject/minimax-h3

./run_h3.sh check                      # 全量体检（不加载权重、不推理）
./run_h3.sh dry --preset draft         # 打印某个请求的完整解析结果
./run_h3.sh loadcheck --dit-onload cpu # 真加载全部模型并换入换出，不做任何推理
./run_h3.sh loadcheck --lora ~/source/minimax-h3/models/AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors

# 预编码 prompt（会跑文本编码器，一次约几十秒），之后反复换种子就不必再跑
./run_h3.sh textcache --prompt-file prompt.txt --ref-image ref.png --preset standard

# 正式推理（本轮未执行）
./run_h3.sh gen --preset standard --prompt-file prompt.txt --ref-image ref.png \
    --lora ~/source/minimax-h3/models/AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors \
    --out outputs/test.mp4
```

# 参考视频编辑（官方 tav2va 用法）：参考视频 + 它的音轨 + 一段音色参考
./run_h3.sh gen --preset video-edit --prompt-file vedit.txt \
    --ref-video-audio ref_video.mp4 --ref-audio voice.mp3 \
    --out outputs/vedit.mp4
```

### 参考输入（四个旗标，顺序有意义）

| 旗标 | 产出的 reference | 说明 |
|---|---|---|
| `--ref-image PATH` | `{"type": "image"}` | 参考图（可重复） |
| `--ref-video PATH` | `{"type": "video"}` | **静音**参考视频；带音轨的文件请用下一个 |
| `--ref-video-audio PATH` | `{"type": "video_audio"}` | 参考视频**连同它自己的音轨** —— 官方 tav2va 用的就是这个 |
| `--ref-audio PATH` | `{"type": "audio"}` | 纯音频参考（例如音色） |

- **四个旗标写同一个有序列表**，argparse 按命令行出现的顺序派发，所以跨旗标的顺序被完整保留。这个顺序不是装饰：`MiniMaxH3Unit_PromptEmbedder.preprocess_ref_blocks` 按列表顺序给 `<Image n>` / `<Video n>` / `<Audio n>` 编号，`MiniMaxH3Unit_ReferenceEncoder` 也按同样顺序拼接 `ref_visual_anchor` / `ref_audio_anchor`。
- `--dry-run` 会把最终顺序打出来，跑之前先核对一眼。
- 视频按目标的 `height` / `width` / `num_frames` 读入（与官方示例一致），代价由 `--ref-video-short-edge` / `--ref-video-max-pixels` 控制。
- 音频一律按**文件自身的采样率**读入：框架会在 `_encode_audio_ref` 里自己重采样到 `pipe.audio_vae.sample_rate`，所以这里既不用预设采样率，也不需要在建 pipeline 之前就知道它。
- 音频解码在本机要走 §3.3 的兜底路径。

---

## 5. 预设与时间预算

时间模型（两项，都用实测 TFLOP/s 校准；seq 已与框架 `PackedSequenceBuilder` 逐一对齐，误差 0）：

```
线性层 = 2 × seq × 20.04e9 / 44.5e12
注意力 = 50 × 4×56×seq²×128 / 47.2e12      ← 每个 block 一次全注意力，非因果
```

| 预设 | 分辨率×帧 | seq | 线性 s | 注意力 s | s/step | 步数 | 去噪 | 相对时长 |
|---|---|---|---|---|---|---|---|---|
| `draft` | 640×384×73 | 7232 | 6.51 | 1.59 | 8.10 | 20 | **2.7 min** | 53× |
| `preview` | 832×480×73 | 11008 | 9.91 | 3.68 | 13.59 | 30 | 6.8 min | 134× |
| `standard` | 832×480×124 | 17024 | 15.33 | 8.80 | 24.13 | 30 | **12.1 min** | 140× |
| `text-only` | 832×480×124（无参考） | 16000 | 14.41 | 7.77 | 22.18 | 30 | 11.1 min | 129× |
| `quality` | 1024×576×124 | 23872 | 21.50 | 17.30 | 38.80 | 40 | 25.9 min | 300× |
| `video-edit` | 832×480×124 + 参考视频 | 25728 | 23.17 | 20.09 | 43.26 | 30 | 21.6 min | 251× |
| `max-native` | 768×1344×124（官方默认） | 39872 | 35.91 | 48.26 | 84.17 | 50 | 70.1 min | 815× |

（以上不含 VAE 解码与阶段①②的固定开销，未实测，属首次跑推理时要量的部分。）

**`standard` 的 token 构成**：目标视频 14430 (85%) + prompt 1100 (6.5%) + 参考图 1024 (6%) + 目标音频 414 (2.4%)。

### 参考条件的代价（最容易被忽视的杠杆）

一张 1024×1024 参考图，在 480×832 输出下（框架默认 `ref_image_short_edge=2048`）：

| short_edge | 参考 latent | rows | seq 增幅 | **注意力增幅** |
|---|---|---|---|---|
| 2048（框架默认） | 128×128 | 4096 | +24.1% | **+53.9%** |
| 1536 | 96×96 | 2304 | +13.5% | +28.9% |
| 1024（本工作流默认） | 64×64 | 1024 | +6.0% | +12.4% |
| 768 | 48×48 | 576 | +3.4% | +6.9% |
| 512 | 32×32 | 256 | +1.5% | +3.0% |

参考**视频**更夸张（与目标等长、832×480 源）：`ref_video_short_edge=768` 会加 18870 rows，seq 翻到 **2.05×**（≈47 min→翻倍）；降到 384 是 1.49×，256 是 1.18×。**本工作流把框架默认的 768 降到 384~512**：`standard` / `quality` / `max-native` 用 **512**，`draft` / `preview` / `text-only` / `video-edit` 用 **384**；完全不指定 preset 时 CLI 兜底才是 384（`h3_generate.py:368`）。

---

## 6. 显存 / 内存方案

### `vram_limit` 是"整卡已用量"阈值，不是"权重预算"

框架的 `check_free_vram()` 比的是 `(total - free) < vram_limit`，其中已经包含 CUDA context、桌面占用、以及**当次前向正在用的激活**。所以：

```
vram_limit = 启动可用显存(6.84) − 激活预留(1.7) − LoRA(0.56) = 4.58 GiB
  → 常驻 NF4 权重 ≈ 3.48 GiB（DiT 的 36%）
  → 每步重拷 6.28 GiB，按实测 5.60 GiB/s ≈ 1.12 s/step（standard 单步的 4.6%）
```

激活预留为什么是 1.7 GiB：`mlp.fc1` 输出是 `seq × 28672 × 2 B`，standard 档就是 **0.91 GiB** 单个张量，叠加 attention 的 qkv（`seq × 21504 × 2 B` = 0.68 GiB）与 norm/rope 副本。

> LoRA 那 0.56 GiB 是**无条件**扣掉的（`h3_audit.py` 里 `res = activation_reserve + lora_gb`），不挂 `--lora` 时其实可以到 5.13。想要那 0.55 GiB 常驻就 `--vram-limit 5.13`。

敏感性（想冒险时看这张表，对应 `--activation-reserve`）：

| vram_limit | 常驻 | 每步重拷 | s/step | 显存余量 |
|---|---|---|---|---|
| 4.00 | 2.90 | 6.86 | 1.23 | 3.93 |
| 4.58（默认） | 3.48 | 6.28 | 1.12 | 3.35 |
| 5.50 | 4.40 | 5.36 | 0.96 | 2.43 |
| 6.00 | 4.90 | 4.86 | 0.87 | 1.93 |
| 6.50 | 5.40 | 4.36 | 0.78 | 1.43 |
| 7.00 | 5.90 | 3.86 | 0.69 | 0.93 |

### 内存：WSL 配额提到 22.91 GiB 后，两种模式都很宽裕

WSL2 默认只给宿主的 50%（32 GiB → 15.31 GiB）。在 `~/.wslconfig` 里显式给配额，改完 `wsl --shutdown` 生效：

```ini
[wsl2]
memory=25165824000      # 23.4 GiB 配额 -> 内核实际可用 22.91 GiB
swap=8589934592         # 8 GiB
```

`./run_h3.sh loadcheck` 真实加载结果（扩内存后重测）：

| 模式 | pipeline ready | DiT onload | RSS 峰值 | DiT 常驻参数 | 全部换出后 RSS |
|---|---|---|---|---|---|
| `--dit-onload cpu`（默认） | 5.5 s | 14.0 s | **11.08 GiB** | 9.47 GiB | 4.36 GiB |
| `--dit-onload disk` | 9.7 s | 0.0 s | **4.75 GiB** | 0 | 4.55 GiB |

两种模式的框架状态都干净地走完 `{1: …}` → `{0: 553}`，并以 `LOAD-ONLY OK` 结束。

- 峰值 11.08 GiB 对 22.91 GiB 还有约 11.8 GiB 余量，**默认的 `--dit-onload cpu` 现在完全够用**，`disk` 不再是内存吃紧时的必需品。
- `disk` 仍然是「给页缓存让路」的选项（DiT 每步从 mmap 流式读取），但它现在只值 1.12 s/step —— 占 `standard` 12.1 分钟的 **4.6%**，不值得为它牺牲内存带宽。

### 文本编码器：默认仍走 `onload_device='disk'`，但现在可以改

它 14.27 GiB。`onload_dtype/device` 都设成 `disk` 后，`onload()` 是空操作、每层按需直送 GPU（官方 low-VRAM 示例也是这么配的）；实测 onload 只用 0.2 s、常驻参数 0.00 GiB。配合 **prompt 嵌入缓存**，热路径上完全不需要加载它。

- 这个选择原本是 15.31 GiB 内存下的**硬约束**。现在 22.91 GiB 装得下 14.27 GiB，`onload_dtype='bfloat16'` + `onload_device='cpu'` 变成可行（`Re2VA.py` 就是这么配的），改 `cache/plan.json` 里 `resolved.vram_config.text_encoder` 即可。
- **但不是无脑升级**：常驻的 14.27 GiB 会和 DiT 的 mmap 页缓存抢内存，而缓存命中时文本编码器根本不会被调用。想换的话先 `./run_h3.sh textcache --refresh-text-cache ...` 量一次冷启动的编码耗时再决定。

---

## 6.5 LoRA 的 sampler/scheduler 要求（euler + beta）

AfterMidnight Ref2VA LoRA 的前置要求是 **euler sampler + beta scheduler**，否则音频会出现异常。

**"euler" 这一半框架本来就满足。** `FlowMatchScheduler.step()` 就是显式一阶 Euler：

```python
prev_sample = sample + model_output * (sigma_next - sigma)
```

和 ComfyUI `sample_euler` 的积分器同形。**需要改的是 sigma 网格的落点**，这才是 "beta scheduler" 的含义。

### 框架默认（flow）

`set_timesteps_minimax_h3` 在**位移前的域里均匀取点**：

```python
u     = linspace(1, 0, n+1)[:-1]          # 1, 1-1/n, ..., 1/n
sigma = shift * u / (1 + (shift-1) * u)   # 标准 flow SNR shift
```

### ComfyUI 的 beta（逐行对照 `comfy/samplers.py`）

```python
def beta_scheduler(model_sampling, steps, alpha=0.6, beta=0.6):
    total_timesteps = (len(model_sampling.sigmas) - 1)
    ts = 1 - numpy.linspace(0, 1, steps, endpoint=False)
    ts = numpy.rint(scipy.stats.beta.ppf(ts, alpha, beta) * total_timesteps)
    sigs, last_t = [], -1
    for t in ts:
        if t != last_t: sigs += [float(model_sampling.sigmas[int(t)])]
        last_t = t
    sigs += [0.0]
    return torch.FloatTensor(sigs)
```

其中 `model_sampling.sigmas` 由 `ModelSamplingDiscreteFlow.set_parameters` 生成 —— `arange(1,1001)/1000` 经同一个 shift 变换，**升序**（`sigmas[0]==sigma_min`）。所以结果是一个**降序、以 0.0 结尾**的列表，正好是 `FlowMatchScheduler.step()` 需要的排布。

`scripts/h3_scheduler.py` 完整复刻了它，并且**保留 pipeline 自己传入的 shift**（视频 12.0 / 音频 3.0），所以只改步长落点，不动任何其他约定。

### 实际差别（30 步，视频 shift=12）

| | 步数 | sigma 上界 | 最后一个网格 sigma | 末步跨度 |
|---|---|---|---|---|
| flow（框架默认） | 30 | 1.0000 | **0.2927** | 0.2927 |
| beta(0.6, 0.6) | 30 | 1.0000 | **0.0780** | 0.0780 |
| 音频 flow（shift=3） | 30 | 1.0000 | 0.0938 | 0.0938 |
| 音频 beta（shift=3） | 30 | 1.0000 | **0.0207** | 0.0207 |

flow 会把最后一个 sigma 从 0.2927 一步砍到 0，音频尤其吃亏；beta 把步数向两端集中，低 sigma 段的步数明显更多（音频末 sigma 降至 0.0207，**约 1/4.5**）。这与"音频会出问题"的现象一致。

### 用法

```bash
./run_h3.sh gen --lora ~/source/minimax-h3/models/AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors ...
#   -> scheduler auto -> 'beta' (LoRA attached)

./run_h3.sh gen --scheduler beta ...      # 强制 beta（不挂 LoRA 时也可用）
./run_h3.sh gen --scheduler flow ...      # 回到框架默认
./run_h3.sh gen --beta-alpha 0.6 --beta-beta 0.6   # 默认值，可调
```

- 默认 `--scheduler auto`：**挂了 LoRA 就自动用 beta**，否则用 flow。若显式用 `--scheduler flow` 且同时挂了 LoRA，会打印醒目告警（不静默）。
- **视频与音频始终同步**：beta 分位数不依赖 shift，两者取到相同的索引序列，去重后步数一致（pipeline 用 `progress_id` 索引 `scheduler_audio.timesteps`，步数不一致会错位）。`h3_generate` 会在启动前断言这一点。
- **实际步数可能略少于请求值**：ComfyUI 也会去重合并落到同一索引的分位数（`if t != last_t`）。启动日志会打印真实步数，例如 `30 steps` 请求下仍是 30，步数少时不要以为是 bug。
- 输出日志会打印实测的 shift、真实步数、首/末 sigma，便于核对。

---

## 7. 已实现的优化

0. **beta scheduler**（挂 LoRA 时自动启用，见 §6.5）—— 这是 LoRA 的硬性前置条件。
1. **`cfg_scale=1.0`** —— 框架里 `cfg_scale != 1.0` 才会跑负向分支。默认就是 1.0，等于单步只做一次前向。本机绝对不要抬高，想加强条件请用 text-side 手段。
2. **Prompt 嵌入磁盘缓存** —— `scripts/h3_generate.py` 的 key 是 (prompt + 参考内容字节 + 分辨率/帧数 + 各 checkpoint 文件大小) 的 sha256。命中时完全跳过 26 B 参数编码器。
   - 注意：框架自带的 `export_text_embedding()` / `text_embedding=` 路径只支持**无参考**的 t2va；本实现的替换单元会**同时回放真实的 `text_token_tags`**，因为 `PackedSequenceBuilder` 会把它们写进 `packed['token_tags'][text_pos]`，而框架的早退分支把它们硬编码成了全 1 —— 对 ref2va 是错的。
3. **参考尺寸收紧**（§5）：图 2048→1024，视频 768→384/512。
4. **每模型独立 `vram_config`**：DiT/VAE 常驻 CPU，文本编码器纯流式。
5. **cuDNN SDPA 后端**：实测比 flash 后端快 4%，`--sdpa-backend cudnn` 默认开启。
6. **`expandable_segments:True`**：权重每步进出造成大量分配抖动。
7. **逐步遥测**：每步打印 s/step、ETA、alloc/reserved/peak，便于现场调 `vram_limit`。
8. **参考输入入口**：`--ref-image` / `--ref-video` / `--ref-video-audio` / `--ref-audio` 四个旗标共用一个**有序**列表，跨旗标顺序被保留（见 §4）。音频解码带 ffmpeg 兜底（见 §3.3），失败时的报错是可读的而不是 torchcodec 的加载栈。

### 两个必须记住的框架坑（都已在本工作流内修正/防御）

- **`onload_dtype='disk'` 只能和 `onload_device='disk'` 配对**。`AutoWrappedLinear.onload()` 把 dtype 直接喂给 `Tensor.to(dtype=...)`，`("disk","cpu")` 会在模型里第一个非量化 Linear 上抛 `TypeError`。官方示例永远是 `(disk,disk)` 或 `(bfloat16,cpu)`。`h3_generate.normalise_vram_config()` 现在会强制纠正。
- **kohya/sd-scripts 格式 LoRA 会被静默忽略**。加载器 `GeneralLoRALoader` 只认后缀 `lora_down/lora_up/alpha`，**不会**把 `lora_unet_blocks_0_attn_qkv_proj` 翻成 `blocks.0.attn.qkv_proj`；`MiniMaxH3LoRAConverter` 只处理 lightx2v 格式。原样调用 `pipe.load_lora()` 会打印 "**0 tensors are patched**" 然后若无其事地跑基础模型。`h3_generate.load_lora()` 用**活模型的模块名**反查映射（600/600 keys 全部命中，200 个模块生效），并且当 patched==0 时**直接报错退出**。

---

## 8. 首次跑推理时的检查清单（§10 已给出实测答案）

1. ~~**VAE 解码耗时未知**~~ → 实测 draft 档解码+封装 **~1 s**，`tile_size=256/overlap=64` 够用，不用调。
2. **`vram_limit` 实测校准 —— 已校准过，结论是「不用调」**。实测峰值 3.05 GiB / 7.93 GiB，看起来余量巨大，但把 `--vram-limit` 从 4.58 提到 6.30 之后常驻层从 25 涨到 70、**单步时间反而没变**（13.17 → 13.34 s/step，§10.2）。默认 4.58 保持不变。真要试就先用 `--activation-reserve` 反推：`vram_limit = 启动可用 − reserve − LoRA`。
3. **注意力后端仍有空间**。当前是 torch SDPA/cuDNN 的 47.2 TFLOP/s。注意力在 standard 档已占 36%、quality 档 44%。装 `sageattention`（sm_120）或 flash-attn 4 (`flash_attn.cute`) 后，把 `DIFFSYNTH_ATTENTION_IMPLEMENTATION` 换成对应值即可 —— 但注意 MiniMax-H3 走的是 `_sdpa_varlen_attention`，只有 `attention_forward` 会被替换。
4. **pinned 内存这个杠杆仍然没被利用，但实测基础带宽比原来记的高得多**：pageable H2D **12.39 GB/s**（不是 3.60），pinned **28.97 GB/s**（不是 28.2）。而且每步真正的开销不是拷贝本身而是 `load_state_dict`+`unflatten_state_dict` 的重建，所以 pinned 化能省的比例比想象中更小。
5. **`torch.compile` 未启用**。bitsandbytes 后端声明 `is_compileable: False`，收益不确定，先不碰。
5b. **beta 的 alpha/beta 参数可以调**。默认 0.6/0.6 是 ComfyUI 的值，也是 AfterMidnight 作者所说的 "beta"。若仍听到音频异常，先确认日志里是 `scheduler 'beta'` 且步数符合预期，再考虑把步数提高到 40（beta 在低 sigma 段更密，步数收益比 flow 时更明显）。
6. **`ref_image_short_edge` 不是免费的**：它直接决定参考图细节保留程度。上面给的是"性价比推荐"，不是"最佳质量"。做人物/产品一致性时建议单独做一次 1024 vs 1536 的 A/B。
7. **参考视频/音频的端到端仍未跑过**。帧解码与音频兜底都单独验证过（§3.3）：72 帧 ↔ 3.00 s 音频对齐正确，`--ref-audio` 的 mp3 也能读。本节验证的是 `--ref-image` 的 Ref2VA 全链路，参考视频/音频的完整生成仍待首次执行。

---

## 9. 目录

```
env/h3_env.sh                    环境变量
run_h3.sh                        统一入口
scripts/h3_compat.py             torchaudio 垫片（现为兜底，不生效）
scripts/h3_scheduler.py          euler/beta 调度
scripts/h3_bench.py              硬件微基准
scripts/h3_validate.py           前端 + 量化一致性校验（11/11 通过）
scripts/h3_audit.py              清点 + 预设 + 显存方案
scripts/h3_generate.py           生成主程序
scripts/h3_te_probe.py           文本编码器单独复现/显存取证（§10.3）
scripts/h3_fetch_processor.py    一次性 processor 拉取
cache/bench.json                 基准结果
cache/validate.json              校验结果（含框架真实 seq 表）
cache/plan.json                  最终方案（h3_generate 的默认值来源）
workspace/<video-project-name>/  素材、提示词和输出视频存放地；工作目录
references/                      提示词准写准则（严格执行）                       
```

---

## 10. 端到端实测记录（本轮）

### 10.1 两次成功的生成

```bash
./run_h3.sh gen --preset draft \
  --prompt-file workspace/pink-harem-ref2va/prompt.txt \
  --ref-image workspace/pink-harem-ref2va/materials/ref-pink-harem.jpg \
  --lora ~/source/minimax-h3/models/AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors \
  --out workspace/pink-harem-ref2va/outputs/pink-harem.mp4
```

| | run10（seed 42） | run11（seed 1234） |
|---|---|---|
| 参考解码 | 8 s | 8 s |
| 文本编码 | **12.1 s**（冷） | **0 s**（`cache hit`） |
| 去噪 | 4.7 min（13.4 s/step × 20） | 4.8 min（14.3 s/step × 20） |
| 解码+封装 | ~1 s | ~1 s |
| 峰值显存 | 2.87 GiB | 3.05 GiB |
| 产物 | 640×384 / 73 帧 / 3.04 s / h264+aac | 同左 |

产物人工验收：抽帧（`outputs/contact-sheet.png`）画面连贯、角色一致、有运镜；音频 RMS 0.0043 非静音。

### 10.2 实测 vs §5 的预测

| 项 | §5 预测 | 实测 | 差 |
|---|---|---|---|
| draft s/step | 8.10 | **13.4** | **+65%** |
| draft 总时长 | 2.7 min | **4.7 min** | +74% |
| 参考图 latent | 1024（预设写 768） | 2506 embeds / 842 vision tokens | 预设生效的是 768 |

为什么慢：**序列长度不是瓶颈，权重重建才是。** 逐层计时（draft，13.4 s/step）：

```
每步 264 次 wrapped Linear 调用
  常驻（state 2，直接返回 self.module）      25 层
  临时（state 1，每步从 disk map 重建）     239 层
  computation_module() 的 CPU 时间           4.4 s   <- 就是这 239 次重建
  wrapped Linear forward 总时间             12.1 s
```

- **NF4 本身几乎免费**：实测 bnb 4bit GEMM 在 draft 形状下 **40~45 TFLOP/s**，和稠密 bf16 只差 3%~17%（`qkv` 1.03×、`fc1` 1.17×）。§5 的线性模型没有把「每步重建 239 层的 packed 权重」算进去。
- **H2D 也不是瓶颈**：实测 pageable 12.39 GB/s，6.3 GiB 重拷 ≈ 0.5 s/step，占 4%。
- **提高 `vram_limit` 无效**：4.58 → 6.30 让常驻层从 25 涨到 70，但单步 13.17 → 13.34 s，净收益为零。默认值保持 4.58。

### 10.3 本轮修掉的两个坑（都会伪装成「显存不足」）

**坑 1：`compute_text_embedding()` 少了 `@torch.no_grad()`。**

`MiniMaxH3Pipeline.__call__` 带这个装饰器，但这个函数是直接驱动 `pipe.unit_runner` 的，什么都不继承。于是 Qwen3-VL 前向会建 autograd 图，把 27 层 vision + 50 层 language 的中间激活全钉住：

```
[trace] vis  1: free 3.921  alloc 2.810
[trace] vis 10: free 2.690  alloc 4.040     每块 +0.137 GiB
[trace] vis 17: free 0.000  alloc 6.846
RuntimeError: CUDA driver error: device not ready
              Failed to create GPU mapping   <- bitsandbytes 不抛 torch.OutOfMemoryError
```

**language layer 一层都没跑到就死了**，所以现象总是出现在「文本编码器」上。整个 pipeline 都是 `requires_grad=False` 载入的，看上去完全不像是训练 —— 这就是它难查的原因。加上装饰器后同一趟 77 个 block 全程平在 2.53 GiB live / 3.02 GiB free。

复现/证伪工具：`scripts/h3_te_probe.py`（加 `--grad` 单独就能复现，去掉就通过）。

**坑 2：Windows「共享 GPU 显存」。**

打开时卡满以后 CUDA 往主机内存溢写，驱动报 `Failed to create GPU mapping` 而不是干净的 OOM；这也正是上一轮把 `AutoWrappedQuantizedModule` 误判成「每层泄漏 238 MiB」的原因。**关掉它**之后：

```
embed_tokens        free 5.30   live_gpu 1.45 GiB
visual blocks 1-27  free 3.78   live_gpu 2.56 GiB   <- 第 1 块之后完全平
lang layers 1-50    free 3.02   live_gpu 2.72 GiB   <- 一点都不涨
```

框架的 disk 流式加载是对的，**没有任何泄漏**。

### 10.4 新增的诊断开关

| 开关 | 作用 |
|---|---|
| `H3_TRACE_ENCODER=1` | 逐 unit / 逐 vision block / 逐 language layer 打印 free / alloc / reserved / 模型在卡上的字节数 |
| `scripts/h3_te_probe.py [--grad] [--no-dit] [--no-lora]` | 单独复现文本编码器，用来证伪「泄漏」假说 |
| `--activation-reserve GiB` | 直接按公式反推 `vram_limit = 启动可用 − reserve − LoRA` |

### 10.5 仍未做的

- `--ref-video` / `--ref-video-audio` / `--ref-audio` 的完整生成（§8.7）。
- `standard` 及以上档位的实跑（本轮的 24 min 是按实测 13.4~14.3 s/step 对 17024 seq 线性外推）。
- 把 239 层的重建开销压下去（方向：让更多层常驻 + 避免每步 `unflatten_state_dict`；但实测单纯抬高 `vram_limit` 无效）。
