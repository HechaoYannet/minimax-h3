#!/usr/bin/env python3
"""param_docs.py -- 生成参数的中文文档（单一数据源）。

为什么单独放一个模块：同一段说明有三个消费方 ——
  1. 前端参数卡片的「详细说明」折叠区（config/params.spec.json -> /api/config）
  2. 页面「环境自检」里的参数手册表
  3. docs/PARAMETERS.md 这份可搜索的参考文档
三处各写一份必然漂移，所以文案只在这里写一次，由 gen_params.py 带进规格。

每个参数的字段：
  label    短标签（卡片上的名字）
  unit     单位
  brief    一句话说明（卡片上始终可见）
  detail   详细说明：它到底控制什么、对应框架里的哪个东西
  tips     列表：经验做法、实测数字、推荐取值
  risks    列表：调错的后果、什么时候会炸
  ranges   列表：(值/区间, 含义) —— 数值型参数的取值语义
"""

PARAM_DOCS = {
    "width": {
        "label": "宽度", "unit": "px",
        "brief": "画布宽度，必须能被 32 整除。",
        "detail": "决定视频 latent 的空间尺寸：latent_w = width / 16，再按 2x2 patch 打包成行。"
                  "所以宽度翻倍会让空间行数变成 4 倍，而注意力是平方项 —— 代价是 16 倍量级。"
                  "框架的 MiniMaxH3Unit_ShapeChecker 会把不合法的值向上吸附到 32 的倍数，"
                  "并在日志里打印一行 shape ... is off the framework grid；页面在提交前就会把吸附结果告诉你。",
        "tips": [
            "本机实测跑通的画布：640x384（最快）、832x480（交付档）、960x544、1024x576（最高点）。",
            "官方原生档 1344x768 在 8 GiB 卡上单独跑也 OOM —— 不是慢，是跑不了。",
            "宽度与高度共同决定面积；只改一个时要回头检查时长档位是否还合适。",
        ],
        "risks": [
            "不是 32 的倍数会被向上吸附，最终构图比例与你填的不是一回事（日志里有记录）。",
            "与帧数同时拉高会撞显存墙：1024x576 只能配到 124 帧，再往上就 OOM。",
        ],
    },
    "height": {
        "label": "高度", "unit": "px",
        "brief": "画布高度，必须能被 32 整除。",
        "detail": "同宽度：latent_h = height / 16，参与 2x2 patch 打包。"
                  "高度是最容易被低估的杠杆 —— 竖屏 768x1344 的行数是 832x480 的 2.3 倍，注意力代价约 5 倍。",
        "tips": [
            "竖屏内容用 480x832（把 832x480 转过来）比 768x1344 现实得多。",
            "改高度后检查时长档位：画布越大，能跑的帧数越少。",
        ],
        "risks": ["任何一边超过 1024 且帧数超过 124，基本可以直接判定为会 OOM。"],
    },
    "num_frames": {
        "label": "帧数", "unit": "帧",
        "brief": "目标帧数，固定 24 fps；必须满足 num_frames % 17 == 5，且不低于 22。",
        "detail": "整个工程里最反直觉的参数：帧数不是线性成本。"
                  "latent 时间维 = ((frames - 5) // 17) * 5 + 2，也就是每多 17 帧，latent 才多 5 帧；"
                  "但目标视频行数 = latent_t x (latent_h/2) x (latent_w/2)，帧数一涨，平方项跟着涨。"
                  "实测：39 帧比 22 帧多 0.7 秒视频只多花约 10 秒；再往上每多 1 秒视频要 ~40 秒。",
        "tips": [
            "合法且实测能跑的档位：22(0.92s)、39(1.63s)、56(2.33s)、73(3.04s)、90(3.75s)、"
            "107(4.46s)、124(5.17s)、141/158/175/192/209/226/243(10.12s)。",
            "22 帧是试种子/试 prompt 的档（blitz 预设，约 46 秒出片）；73 帧是日常档；"
            "243 帧是 640x384 下的最长档。",
            "先定帧数、再优化提示词：优化功能会把目标时长写进 user message，顺序反了模型会按错的时长裁镜头。",
        ],
        "risks": [
            "低于 22 帧（比如 5 帧）能通过框架的形状检查，但会在 VAE 解码阶段报 "
            "AttributeError: NoneType object has no attribute float —— 这是实测踩到的坑。",
            "832x480 配 243 帧、640x384 配 345 帧都会 OOM；显存墙同时受画布与帧数影响。",
            "一次 OOM 会污染后续所有配置，必须 wsl --shutdown 后重来（README 11.3）。",
        ],
        "ranges": [
            ("22 ~ 124", "常规区间，8~30 步都能在 8 GiB 卡上跑完。"),
            ("141 ~ 243", "只在 640x384 这类小画布下可行，且建议 dit_onload=disk。"),
            ("> 243", "本机没有跑通过；要试请先降到 640x384，并做好 OOM 的心理准备。"),
        ],
    },
    "seconds": {
        "label": "时长", "unit": "秒",
        "brief": "由帧数换算：帧数 / 24。只读派生值，用于与提示词里的时长对齐。",
        "detail": "官方规范要求提示词描述的时长必须与目标视频一致（时间码不得超过它），"
                  "所以这个值会随请求一起发给 DeepSeek，模型据此裁剪镜头数量与切换时间。",
        "tips": ["73 帧 = 3.04 秒；124 帧 = 5.17 秒；243 帧 = 10.12 秒。"],
    },
    "steps": {
        "label": "采样步数", "unit": "步",
        "brief": "去噪步数。总时长约等于 固定开销 + 单步耗时 x 步数。",
        "detail": "采样器固定是一阶 Euler（框架 FlowMatchScheduler.step 就是 "
                  "prev = sample + model_output x (sigma_next - sigma)），"
                  "所以「步数」是唯一的采样质量旋钮。步数在 sigma 轴上的落点由调度器决定："
                  "flow 是均匀位移网格，beta 是 ComfyUI 的 beta(0.6,0.6) 分位数网格。",
        "tips": [
            "试构图/试种子用 4~8 步（blitz 预设就是 8 步）；定稿 20~30 步；挂 LoRA 时建议 >= 30 步。",
            "beta 调度在低 sigma 段更密，加步数的收益比 flow 更明显 —— 音频有瑕疵时先加步数。",
            "短片段（22 帧）有个约 5.6 s/step 的硬底，步数少也不会快到哪去。",
        ],
        "risks": ["挂 LoRA 却用低步数（< 20）容易出现音频瑕疵；AfterMidnight 的要求是 euler + beta + "
                  "足够的低 sigma 步数。"],
    },
    "seed": {
        "label": "随机种子", "unit": None,
        "brief": "同种子 + 同参数 = 同一条视频。换种子是试构图最快的手段。",
        "detail": "种子只影响初始噪声。因为参考图/参考视频是条件输入，它不会改变主体身份，"
                  "只会改变动作、构图细节与音频的随机落点。",
        "tips": [
            "用 blitz 档连跑几个种子挑构图，再用 draft/standard 出片，是最省的流程。",
            "提示词缓存与种子无关：换种子不会触发重新编码，命中缓存省下的 12 秒会一直在。",
        ],
    },
    "ref_image_short_edge": {
        "label": "参考图短边", "unit": "px",
        "brief": "参考图被缩放到短边等于该值后编码；这是最陡的性能杠杆。",
        "detail": "框架默认 2048。一张 1024x1024 的参考图在 480x832 输出下："
                  "短边 2048 -> latent 128x128 -> 4096 行（注意力 +53.9%）；"
                  "1024 -> 64x64 -> 1024 行（+12.4%）。"
                  "换算方式：短边压到该值后按 32 取整，再 /16 得 latent，最后算 (lh/2) x (lw/2) 行。",
        "tips": [
            "预设给的就是性价比取值：blitz/fast 用 256，draft 用 768，standard/quality 用 1024。",
            "做人物/产品一致性、需要保留服装纹理与面部细节时，单独做一次 1024 vs 1536 的 A/B。",
            "参考图为竖构图时短边是它的宽，横构图时短边是高。",
        ],
        "risks": [
            "1536 以上注意力开销迅速上升：2048 比 1024 多约 54%，长片段上会直接顶到 OOM 边缘。",
            "降得过头（<= 256）会丢细节：参考图只提供「像不像」的锚点，纹理糊了就白给了。",
        ],
        "ranges": [
            ("256", "最省，试种子用；细节损失明显。"),
            ("768 ~ 1024", "推荐区间：细节与代价平衡，工程默认落在这里。"),
            ("1536 ~ 2048", "只有必须抠细节时才用，且要同步降帧数。"),
        ],
    },
    "ref_video_short_edge": {
        "label": "参考视频短边", "unit": "px",
        "brief": "参考视频的读取尺寸。参考视频与目标等长，代价远大于参考图。",
        "detail": "参考视频按目标视频的 height/width/num_frames 读入，所以它会贡献与目标同量级的行数。"
                  "同样 832x480 的源：短边 768 会额外加 18870 行（seq 翻到 2.05 倍），"
                  "384 是 1.49 倍，256 是 1.18 倍。有效缩放还受「参考视频像素上限」约束，取两者更严格的那个。",
        "tips": [
            "预设里 draft/preview/text-only/video-edit 用 384，standard/quality/max-native 用 512，"
            "这是工程测过的取值。",
            "参考视频只做「运镜/节奏/时间结构」参考时，256~384 完全够用。",
            "命令行里不给 preset 时的兜底值是 384。",
        ],
        "risks": [
            "768 会让总时长接近翻倍（约 47 min -> 94 min 量级），且更容易顶到显存/内存墙。",
            "参考视频与目标等长，意味着它无法像参考图那样「只加一点点」。",
        ],
    },
    "ref_video_max_pixels": {
        "label": "参考视频像素上限", "unit": "px",
        "brief": "参考视频缩放的另一条约束：总像素不得超过该值，与短边共同决定最终尺寸。",
        "detail": "实际缩放比例 = min(短边比例, sqrt(max_pixels / 原始像素数))，取更严格的那个，"
                  "然后再把宽高各自吸附到 32 的倍数。预设里它等于 短边 x 672。",
        "tips": [
            "只有当参考视频是超宽/超高这类极端画幅时，这条约束才会先于短边生效。",
            "不确定就让它跟随短边：改短边时前端会一起带出对应的上限。",
        ],
    },
    "vram_limit": {
        "label": "显存阈值", "unit": "GiB",
        "brief": "框架的常驻判据：整卡「已用显存」低于该值时更多层允许常驻；不是权重预算。",
        "detail": "框架的 check_free_vram() 比的是 (total - free) < vram_limit，"
                  "里面已经包含 CUDA context、桌面占用和当次前向正在用的激活。"
                  "把 DiT 的 NF4 权重常驻显存后，剩下部分每步都要从 disk map 重建 —— "
                  "实测这是单步 33% 的 CPU 时间来源（264 个 wrapped Linear 里只有 25 个能常驻）。",
        "tips": [
            "默认 4.58 GiB 是工程校准过的：启动可用 6.84 - 激活预留 1.7 - LoRA 0.56。",
            "敏感性（README 6）：4.00 -> 常驻2.90/重拷6.86；4.58 -> 3.48/6.28；5.50 -> 4.40/5.36；"
            "6.00 -> 4.90/4.86；6.50 -> 5.40/4.36；7.00 -> 5.90/3.86（单位 GiB）。",
            "不想直接填这个数就用「激活预留」反推，语义更清楚。",
        ],
        "risks": [
            "抬高几乎不提速：实测 4.58 -> 6.30 时单步 13.17 -> 13.34 s（反而略慢），"
            "但常驻层 25 -> 70、余量只剩不到 1 GiB。",
            "22 帧 5 步的 A/B 也一样：多花 57% 显存换 6% 速度。",
            "设太低（<= 3.5）会让每步重建更多层，明显变慢。",
        ],
    },
    "activation_reserve": {
        "label": "激活预留", "unit": "GiB",
        "brief": "从启动可用显存里给「当次前向的激活」留多少；留完剩下的就是 vram_limit。",
        "detail": "vram_limit = 启动可用 - 激活预留 - LoRA 占用（LoRA 按文件大小的一半算，"
                  "因为它以 F32 存储、以 bf16 持有）。"
                  "为什么需要预留：mlp.fc1 的输出是 seq x 28672 x 2 B，standard 档单个张量就有 0.91 GiB，"
                  "再叠加 qkv（seq x 21504 x 2 B，约 0.68 GiB）与 norm/rope 的副本。",
        "tips": [
            "draft 档的真实突发只有约 1 GiB，取值可以比 1.7 低不少；standard 及以上建议保持 1.7。",
            "它与 vram_limit 互斥：填了它就别再填 vram_limit，否则后者优先。",
        ],
        "risks": ["留得太少会在某一步直接 OOM，而且这类 OOM 常常伪装成 bitsandbytes 的 "
                  "CUDA driver error（README 10.3）。"],
    },
    "dit_onload": {
        "label": "DiT 装载方式", "unit": None,
        "brief": "cpu = 把 9.76 GiB 的 DiT 常驻主机内存；disk = 走 mmap 流式读取。",
        "detail": "框架用 load_models_to_device() 在阶段之间互斥换入换出，这个开关决定 DiT 的去处。"
                  "disk 模式下 onload() 是空操作，权重按需从 mmap 直送 GPU。"
                  "实测（内存扩到 22.9 GiB 后）：cpu 峰值 RSS 11.08 GiB、onload 14.0 s；"
                  "disk 峰值 RSS 4.75 GiB、onload 0.0 s。",
        "tips": [
            "短片段用 cpu（少一次 mmap 抖动）；长片段/大画布一律用 disk。",
            "832x480x73 实测 21.18 s/step（disk），并不比常驻慢，反而给页缓存让了路。",
        ],
        "risks": [
            "cpu + 长片段 + 冷启动文本编码器会顶到 WSL 的 22.9 GiB 配额 —— "
            "工程里曾因此把整个 WSL 发行版打死过一次（连 traceback 都没留下）。",
            "onload_dtype=disk 只能与 onload_device=disk 配对；这个组合由脚本强制纠正，不用你操心。",
        ],
    },
    "sdpa_backend": {
        "label": "注意力后端", "unit": None,
        "brief": "DiT 的 SDPA 后端；实测 cuDNN 最快。",
        "detail": "一条流水线里有两条注意力路径：DiT 走 MiniMaxH3DiT._sdpa_varlen_attention"
                  "（受这个参数控制）；其它（音频 VAE 等）走通用 attention_forward，被固定成 "
                  "FLASH_ATTENTION —— 因为音频 VAE 的因果注意力在 cuDNN 下没有可用 kernel（实测直接 abort）。",
        "tips": [
            "保持 cudnn。本机没有 flash_attn / sageattention / xformers，其它选项只会更慢。",
            "实测 cuDNN SDPA 47.2 TFLOP/s，比 flash 后端快约 4%。",
        ],
        "risks": ["将来若装了 sageattention 或 flash-attn 4，注意 H3 走的是 _sdpa_varlen_attention，"
                  "只有 attention_forward 会被替换 —— 换了实现不一定生效。"],
    },
    "tile_size": {
        "label": "VAE 解码分块", "unit": "px",
        "brief": "VAE 解码时的空间分块大小，避免整帧物化导致显存尖峰。",
        "detail": "解码阶段按 tile 逐步还原画面再拼接。工程实测 draft 档解码 + 封装约 1 秒，"
                  "256/64 的默认组合够用，不需要调。",
        "tips": ["画布越大越值得保底分块；显存余量充足时可以加大分块以减少接缝处理次数。"],
    },
    "tile_overlap": {
        "label": "VAE 分块重叠", "unit": "px",
        "brief": "相邻分块的重叠像素，用于消除拼接接缝。",
        "detail": "重叠越大接缝越不明显，但计算量随重叠比例上升。默认 64 是工程验证过的取值。",
        "tips": ["只有在输出画面里看到网格状接缝时才需要加大；正常情况保持默认。"],
    },
    "no_tiled": {
        "label": "不分块解码", "unit": None,
        "brief": "一次性解码整段 VAE：更快，但显存需求高得多。",
        "detail": "关掉分块后 VAE 解码会在显存里物化完整张量。draft 这类短片可以试，"
                  "standard 及以上基本会顶到墙。",
        "risks": ["解码阶段 OOM 的表现和去噪阶段不同：往往在流水线最后一步突然失败，前面的时间全白花。"],
    },
    "lora": {
        "label": "LoRA 文件", "unit": None,
        "brief": "挂在基座上的 LoRA；挂上后调度器会自动切到 beta。",
        "detail": "加载器只认 lora_down / lora_up / alpha 后缀（GeneralLoRALoader）。"
                  "工程的 h3_generate.load_lora() 用活模型的模块名反查映射，"
                  "并且当 patched == 0 时直接报错退出 —— 不会像原版那样打印 0 tensors are patched "
                  "然后若无其事地跑基座。LoRA 以 bf16 持有，约占文件大小一半的显存。",
        "tips": [
            "本机可用的是 AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors（1.11 GiB，rank 64）。",
            "挂 LoRA 时 vram_limit 会无条件扣掉 0.56 GiB 的 LoRA 预留；不挂时可以把它要回来"
            "（--vram-limit 5.13）。",
        ],
        "risks": [
            "kohya / sd-scripts 格式的 LoRA 会被静默忽略（键名不匹配），必须走工程的映射逻辑。",
            "挂 LoRA 却用 flow 调度器会让音频异常；页面在 scheduler=flow + 有 LoRA 时会醒目警告。",
        ],
    },
    "lora_alpha": {
        "label": "LoRA 权重", "unit": "a",
        "brief": "LoRA 的强度倍率，1.0 = 作者标定值。",
        "detail": "加载时对 LoRA 增量整体缩放。低于 1 会削弱风格化/音色倾向，高于 1 会过冲。",
        "tips": ["0.6~0.8 做轻微风格化，1.0 是作者的意图，1.2 以上通常开始出现瑕疵。"],
    },
    "scheduler": {
        "label": "调度器", "unit": None,
        "brief": "步数在 sigma 轴上的落点：auto / flow / beta。采样器始终是一阶 Euler。",
        "detail": "flow = 框架默认，在位移前的域里均匀取点；beta = ComfyUI 的 beta(0.6,0.6) 分位数网格，"
                  "把步数向两端集中。30 步 / shift 12 时，flow 的最后一个网格 sigma 是 0.2927，"
                  "beta 是 0.0780 —— beta 在低 sigma 段（音频细节被解析出来的地方）步数明显更多；"
                  "音频同理：0.0938 -> 0.0207。",
        "tips": [
            "auto 就是「挂 LoRA 用 beta，否则 flow」，绝大多数情况不用动。",
            "beta 分位数不依赖 shift，所以视频与音频始终取到相同的索引序列、步数一致"
            "（流水线用 progress_id 索引音频时间步，步数不一致会错位）。",
            "实际步数可能略少于请求值：落到同一索引的分位数会被去重合并，日志会打印真实步数。",
        ],
        "risks": ["AfterMidnight 这类 LoRA 的前置要求是 euler + beta，硬用 flow 会导致音频异常。"],
    },
    "beta_alpha": {
        "label": "beta alpha", "unit": None,
        "brief": "beta 分位数分布的形状参数，默认 0.6（与 ComfyUI 一致）。",
        "detail": "alpha 与 beta 一起决定步数如何向两端集中：alpha 越小，网格越偏向高 sigma"
                  "（结构/构图阶段）。",
        "tips": ["除非在复现别人的 ComfyUI 配方，否则保持 0.6。"],
        "risks": ["调歪了不会报错，只会让画面或音频质量悄悄变差 —— 改了记得留档对比。"],
    },
    "beta_beta": {
        "label": "beta beta", "unit": None,
        "brief": "beta 分位数分布的另一个形状参数，默认 0.6。",
        "detail": "beta 越大，网格越偏向低 sigma（细节/音频阶段）。",
        "tips": ["音频仍有瑕疵时的顺序是：先确认调度器是 beta、再加步数，最后才动这里的形状参数。"],
    },
    "text_cache": {
        "label": "复用提示词缓存", "unit": None,
        "brief": "命中缓存时完全跳过 26 B 参数的文本编码器（省约 12 秒）。",
        "detail": "缓存 key 是 sha256(prompt + 参考内容字节 + 分辨率/帧数 + 各 checkpoint 文件大小)。"
                  "所以换种子、换步数、换 LoRA 都不会失效；一旦参考素材或提示词变了就一定失效。",
        "tips": ["调试迭代（换种子、换步数）时开着它，这是收益最直接的 12 秒。"],
    },
    "refresh_text_cache": {
        "label": "强制重算缓存", "unit": None,
        "brief": "忽略已有缓存，重新跑一次文本编码器。",
        "detail": "用来排除「缓存里的嵌入是错的」这一类怀疑，也顺便量一次冷启动的编码耗时。",
        "tips": ["只有怀疑缓存本身有问题时才勾；正常迭代不要勾，会白花 12 秒。"],
    },
}

# 前端摘要卡片的分组说明（不是参数本身，但同样值得讲清楚）
PANEL_DOCS = {
    "params": {
        "title": "生成参数",
        "brief": "形状、时长、采样，以及参考条件的代价。",
        "tips": ["改任何一个都会即时重算预估（seq / s每步 / 总时长 / 风险）；提交前先看一眼摘要行。"],
    },
    "perf": {
        "title": "性能 / 显存调优",
        "brief": "显存、内存与注意力后端的取舍。",
        "tips": [
            "这一组参数都有反直觉的地方：抬高 vram_limit 几乎不提速，长片段的关键是 dit_onload=disk。",
            "不确定就别动 —— 默认值（4.58 GiB / cpu / cudnn）是工程校准过的起点。",
        ],
    },
    "lora": {
        "title": "LoRA 与采样调度",
        "brief": "挂 LoRA 会改变调度器的默认选择，也会占用显存。",
        "tips": ["挂上 LoRA 后第一件事是确认日志里出现 scheduler auto -> beta。"],
    },
    "cache": {
        "title": "提示词缓存",
        "brief": "文本编码器的一次性开销，命中即省约 12 秒。",
    },
    "vae": {
        "title": "VAE 解码",
        "brief": "解码阶段的分块策略，用来避免显存尖峰。",
        "tips": ["默认 256/64 已实测够用；只在看到接缝或解码 OOM 时才调整。"],
    },
}

# docs/PARAMETERS.md 的补充章节
GENERAL_NOTES = [
    ("形状规则（不合法的值会被框架自动吸附，日志里会打印）", [
        "width / height 必须是 32 的倍数，否则向上吸附到下一个 32 的倍数。",
        "num_frames 必须满足 frames % 17 == 5（22、39、56、73 ...），否则向上吸附；"
        "此外实测下限是 22 帧，比它小的值能过形状检查但会在 VAE 解码阶段失败。",
        "吸附之后的形状才是真正运行的形状 —— 页面预估会先显示吸附结果，再算 seq 与耗时。",
    ]),
    ("序列长度（seq）是怎么来的", [
        "目标视频行数 = latent_t x (latent_h / 2) x (latent_w / 2)，其中 "
        "latent_t = ((frames - 5) // 17) x 5 + 2，latent_h = height / 16，latent_w = width / 16。",
        "参考图行数 = (lh / 2) x (lw / 2)：参考图先按短边缩放到 32 的倍数，再 /16 得 latent。",
        "参考视频行数 = lt x (lh / 2) x (lw / 2)：lt 由参考视频被裁到的帧数决定"
        "（与目标等长时就是目标帧数）。",
        "音频行数 = round(frames / 24 x 40) x 2（两个声道）；文本固定约 1100 行。",
        "全部相加后向上对齐到 64 的倍数就是 seq。线性层成本与 seq 成正比，"
        "注意力成本与 seq 的平方成正比 —— 长片段为什么贵，答案在这一行。",
    ]),
    ("显存与主机内存的互换关系", [
        "DiT 是 9.76 GiB 的 NF4 权重，装不进 8 GiB 显存，所以永远有一部分待在显存外。",
        "dit_onload=cpu：权重常驻内存（峰值 RSS 11.08 GiB），每步从内存补充显存。",
        "dit_onload=disk：权重走 mmap（峰值 RSS 4.75 GiB），每步从页缓存补充，实测并不更慢。",
        "vram_limit 决定常驻多少层；剩下的每步重建，实测占单步 33% 的 CPU 时间。",
        "长片段会同时吃满内存与显存：243 帧跑到一半时 ram free 只剩 144 MiB，而显存还剩 3 GiB。",
    ]),
    ("一次 OOM 会污染同一台机器上后续所有配置", [
        "实测：1344x768 OOM 之后，紧接着一个已知能跑的 832x480x124 也立刻报同样的错。",
        "所以批量试验时，任何可能超限的配置都要单独验，或者 OOM 后先 wsl --shutdown 再继续。",
        "页面在错误归因里会直接给出这条建议，而不是只把 traceback 丢给你。",
    ]),
    ("参数组合的几条经验", [
        "试种子：blitz（640x384 x 22 帧 / 8 步，约 46 秒）+ 参考图短边 256。",
        "日常出片：draft（640x384 x 73 帧 / 20 步，实测 4.7 分钟）+ 参考图短边 768~1024。",
        "交付：standard（832x480 x 124 帧 / 30 步，实测约 17 分钟）+ 参考图短边 1024。",
        "想要更长：走 640x384 + 最多 243 帧，并把 dit_onload 切到 disk。",
        "想要更清晰：1024x576 是实测跑通的最高画布（30 步 25 分钟，峰值 5.94 GiB），"
        "但帧数只能到 124。",
        "挂了 LoRA：调度器 beta + 步数 >= 30；显存压力大时先降帧数，不要动 vram_limit。",
    ]),
]

SECTION_ORDER = [
    ("形状与时长", ["width", "height", "num_frames", "seconds", "steps", "seed"]),
    ("参考条件的代价", ["ref_image_short_edge", "ref_video_short_edge", "ref_video_max_pixels"]),
    ("显存 / 内存 / 速度", ["vram_limit", "activation_reserve", "dit_onload", "sdpa_backend"]),
    ("VAE 解码", ["tile_size", "tile_overlap", "no_tiled"]),
    ("LoRA", ["lora", "lora_alpha"]),
    ("调度器 / 采样器", ["scheduler", "beta_alpha", "beta_beta"]),
    ("提示词缓存", ["text_cache", "refresh_text_cache"]),
]
