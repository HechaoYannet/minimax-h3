## 结构规范：base 模式（t2va，无参考画面）

没有参考图、只凭文字从头构建整条时间线（有音频参考但无画面参考时也走这里）。
输出 **三个核心字段，顺序固定，段间空一行**，第一行直接就是第一个字段：

```
integrated_multimodal_description: [Shot 1] ...

overall_soundscape: ...

non_diegetic_music: ...
```

可以补充与用户意图一致的场景、角色、动作、声音细节（因为没有参考画面需要对齐）。
其余写法同下面的「共用书写规则」。

---

## 共用书写规则（三种结构都适用）

### 镜头与切换

- 第一个镜头**不加时间码**。后续镜头以 `[Shot 2]`、`[Shot 3]` …… 递增，并在句首给出严格递增的切换时间：
  `[Shot 2] At 00:03.500, the camera cuts to ...`
- 常规切换用 `the camera cuts to` / `the shot cuts to` / `the shot transitions to` /
  `the shot changes to` / `the shot switches to`；用户明确要求时才用 cross-dissolve / fade / wipe。
- 切换必须带来**新信息**（主体、空间、状态、视角或时间的变化）。只想改变景别或轻微角度时，优先用运镜。

### 开场写法

在 `[Shot 1]` 开头交代整体风格与初始构图。风格可以是 `Cinematic` / `live-action` /
`2D-animated` / `3D CG` / `claymation` / `watercolor` / `vintage film` 等；
有参考图时风格从参考图推导，t2va 时从用户文字里选。例：
`[Shot 1] Live-action, cinematic, a medium-wide shot frames ...`

### 运镜：类型 + 幅度 + 速度

- 类型：`Zoom In / Zoom Out`、`Push In / Pull Out`、`Pan Left / Pan Right`、`Truck Left / Truck Right`、
  `Tilt Up / Tilt Down`、`Pedestal Up / Pedestal Down`、`Arc Shot`、`Tracking Shot`、`Static Shot`、
  `Shake Slightly / Shake Strongly`、`POV`、`Roll Clockwise / Roll Counterclockwise`
- 幅度：`with small amplitude` / `with large amplitude`（中等幅度可省略）
- 速度：`at slow speed` / `at fast speed`（常速可省略）
- 写成句子里的自然动作，不要堆在句尾当标签：
  `The camera pushes in with small amplitude at slow speed toward the folded letter in her hands.`

### 说话人、台词与演唱

- 会说话/唱歌/发出画外人声的角色用稳定 ID `(S1)` `(S2)`；多人同时发声用复合 ID `(S1,S2)`。
  同一角色跨镜头 ID 不变；完全不发声的角色不给 ID。
- 说话人首次出现时，要在画外音内容之外给足可辨识信息（角色类型、年龄、性别、是否在画面内、
  音高、音色、语速、口音等）。
- 台词块写法：`角色描述 (S1) says: <d>[English] 原文台词</d>`
  块**外**放识别短语、ID、动作与表演方式；块**内**只放语言标签 + 用户给的原文，**逐字逐标点照抄，不翻译不改写**。
- 画外音必须用固定短语 `says in an off-screen voiceover`，并在紧跟的 `<d>` 块之后说明该角色**嘴唇始终闭合**。
- 台词/歌词跨剪辑点时，在两段连接处用 `<scenetrans>`，并明确说明音频跨越剪辑继续；被视频结尾截断时用 `<cutoff>`。
  连续性可用 `continues seamlessly across the cut` / `carries over from the previous shot` 等表达。

### 画面可见文字

招牌、标签、字幕、霓虹字等真实出现在画面里的文字，用英文双引号包住并**原样保留**、不翻译：
`A red neon sign reading "营业中" glows above the doorway.`

### overall_soundscape

1~4 句英文，一个连续段落，概括全片环境音、动作音效、非语言人声（风、雨、车流、脚步、衣物摩擦、
撞击、呼吸、笑声、喘息……）。台词/演唱/画内音乐不在这里重复。只有用户明确要求全程静音才写 `N/A`。

### non_diegetic_music

1~3 句英文，描述角色听不到、只有观众能听到的背景音乐，聚焦配器、速度、节奏、强弱变化；
不写抽象情绪词，不解释配乐的情绪功能。角色能听到的音乐属于画内事件，写进 multimodal 描述。没有就写 `N/A`。
