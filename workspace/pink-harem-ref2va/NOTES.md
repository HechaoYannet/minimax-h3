# pink-harem-ref2va · 推理调试笔记

## 状态：**端到端已跑通**

| 项 | 结果 |
|---|---|
| 提示词 | 完成（`prompt.txt`） |
| 端到端 | ✅ `outputs/pink-harem.mp4`（seed 42）与 `outputs/pink-harem-seed1234.mp4`（seed 1234） |
| 输出规格 | 640x384 · 73 帧 · 3.04 s · h264 + aac 32 kHz 立体声 |
| 单次耗时 | 文本编码 12.1 s（冷）/ 0 s（缓存）→ 去噪 4.6 min → 解码+封装 ~1 s |
| 峰值显存 | 3.05 GiB / 7.93 GiB |

复现命令：

```bash
./run_h3.sh gen --preset draft \
  --prompt-file workspace/pink-harem-ref2va/prompt.txt \
  --ref-image workspace/pink-harem-ref2va/materials/ref-pink-harem.jpg \
  --lora ~/source/minimax-h3/models/AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors \
  --out workspace/pink-harem-ref2va/outputs/pink-harem.mp4
```

---

## 1. 上一轮的两条结论都是错的

上一轮把崩溃归因于「`AutoWrappedQuantizedModule` 在 GPU 路径上每层泄漏 ~238 MiB」，
并把整个文本编码器钉到 CPU。**两条都推翻了。**

### 1.1 根本没有泄漏

`scripts/h3_te_probe.py` 用真实 pipeline 逐块量了 free VRAM 和模型在卡上的实际字节数：

```
embed_tokens        free 5.30   live_gpu 1.45 GiB   <- 一次性载入
visual blocks 1-27  free 3.78   live_gpu 2.56 GiB   <- 第 1 块之后完全平
lang layer 1        free 3.18   live_gpu 2.72 GiB   <- +0.17 之后平
lang layers 2-50    free 3.02   （一点都不涨）
FORWARD OK: (3170, 5120) embeds, peak 3.64 GiB, 之后仍剩 3.02 GiB
```

框架的 disk 流式加载是对的。当时的「每层 238 MiB」是 **Windows 共享显存**还没有关的时候
量的 —— 卡满了以后 CUDA 往主机内存溢写，驱动报 `Failed to create GPU mapping`，看起来像泄漏。

### 1.2 真正的根因：`compute_text_embedding` 没有 `torch.no_grad()`

`MiniMaxH3Pipeline.__call__` 带 `@torch.no_grad()`，但 `compute_text_embedding()` 是直接驱动
`pipe.unit_runner` 的，**什么都不继承**。于是 Qwen3-VL 的前向会建 autograd 图，把 27 层 vision
block + 50 层 language layer 的每个中间激活全部钉住：

```
[trace] vis  1: free 3.921  alloc 2.810
[trace] vis 10: free 2.690  alloc 4.040     <- 每块 +0.137 GiB
[trace] vis 17: free 0.000  alloc 6.846
RuntimeError: CUDA driver error: device not ready
              Failed to create GPU mapping
```

**language layer 一层都还没跑到就死了**，这就解释了为什么现象总出现在「文本编码器」上。

为什么难查：
- bitsandbytes 抛的是驱动错误，**不是 `torch.OutOfMemoryError`**；
- 整个 pipeline 都是 `requires_grad=False` 载入的，**看起来完全不像是训练**；
- 换成 CPU 就「好了」—— 因为 CPU 路径下 autograd 图存在主机内存里，GPU 不再涨。

**判定实验**：`h3_te_probe.py --grad` 单独就能复现（`vis 17` 崩），去掉 `--grad` 则 77 个 block
全部跑完。

### 1.3 修法

`scripts/h3_generate.py`：

```python
@torch.no_grad()
def compute_text_embedding(pipe, prompt, references, height, width, num_frames, edges):
    ...
    pipe.load_models_to_device(["text_encoder"])   # 把 ReferenceEncoder 刚用过的 VAE 权重交还
```

同时**删掉** `force_text_encoder_to_cpu()` 和 `patch_cpu_vram_gate()` 的调用点。
（`patch_cpu_vram_gate` 作为兜底保留定义，正常路径不再调用。）

---

## 2. 性能：瓶颈不在显存，在 264 个被包装的 Linear

逐层计时（`vram_limit=4.58`，draft，13.4 s/step）：

```
每步 264 次 wrapped Linear 调用
  常驻（state 2，直接返回 self.module）      25 层
  临时（state 1，每步从 disk map 重建）     239 层
  computation_module() 的 CPU 时间           4.4 s
  wrapped Linear forward 总时间             12.1 s
```

**把 `vram_limit` 提到 6.30 没有用**（实测 13.17 s → 13.34 s/step）：常驻层从 25 涨到 70，
但少掉的 45 层 × ~18 ms 被别处的开销吃掉了，净收益为零。默认 4.58 保持不动。

（新增的 `--activation-reserve` 仍然留着，配合 `--vram-limit` 可以在别的分辨率/预设下快速试。）

NF4 本身不是瓶颈：实测 bnb 4bit GEMM 在 draft 形状下 40~45 TFLOP/s，**和稠密 bf16 只差 3%~17%**。

---

## 3. 其他已确认的事实

| 项 | 结论 |
|---|---|
| LoRA 加载 | 好。600/600 keys 映射，200 模块生效。 |
| 调度器 | 好。挂 LoRA 后 auto -> beta(0.6,0.6)，euler sampler。 |
| 文本嵌入缓存 | 好。第二次跑 `cache hit`，完全跳过 26 B 编码器。 |
| 参考图解码 | 好。1200x822 正确读入。 |
| 显存 | 峰值 3.05 GiB / 7.93 GiB，余量充足。 |
| H2D 带宽 | pageable 12.4 GB/s，pinned 29.0 GB/s（都比 README 里旧的 3.60 GB/s 高很多）。 |

---

## 4. 本目录的文件

| 文件 | 内容 |
|---|---|
| `prompt.txt` | 成品提示词 |
| `materials/ref-pink-harem.jpg` | 参考图 |
| `outputs/pink-harem.mp4` | **成品**（seed 42） |
| `outputs/pink-harem-seed1234.mp4` | 成品（seed 1234，验证缓存路径） |
| `outputs/contact-sheet.png` | 抽帧拼图，用来肉眼验收 |
| `run.log` ~ `run5.log` | 上一轮（共享显存未关）的失败记录 |
| `run6`/`run7`.log | 去掉 CPU 钉死之后仍然崩 —— 真正的 autograd 问题 |
| `run8`/`run9`.log | 带 `H3_TRACE_ENCODER=1` 的逐块显存轨迹（定位用） |
| `run10.log` / `run11.log` | **两次成功的端到端运行** |

诊断工具：`scripts/h3_te_probe.py`（复现/证伪泄漏假说）。
