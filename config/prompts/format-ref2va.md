## 结构规范：ref2va（全参考模式）

当参考素材里出现 **视频**、**音轨**，或多张图片混合使用时，走本结构。
输出 **六个分段，顺序固定**，段名单独成行、后跟冒号，段与段之间空一行：

1. `subject_definitions`
2. `summary`
3. `retention_analysis`
4. `detailed_description`
5. `overall_soundscape`
6. `non_diegetic_music`

### 标签规则

| 标签 | 含义 |
| --- | --- |
| `<Subject N>` | 从参考素材里抽象出来、会在目标视频里真正复用的可见内容（人、动物、物件、场景、服装、道具、风格、动作、表情、姿势） |
| `<Picture N>` | 作为具体目标帧 / 分镜锚点使用的参考图 |
| `<Video N>` | 作为剪辑源、续写起点、或整体时间结构参考的参考视频 |
| `<Audio N>` | 被复制或被参考的音频信号 |

- 一个 `<Subject N>` 可以由多个素材共同定义，说明各自提供什么：
  `<Subject 1> is the woman whose appearance comes from <Picture 1> and whose walking motion comes from <Video 1>.`
- 图片**只用来定义人物/场景/服装/风格**时，不要单独开 `<Picture N>` 条目，
  在对应 `<Subject N>` 定义里引用它即可。
- `<Video N>` 与 `<Audio N>` **各自独立编号**：`<Video 1>` 与 `<Audio 2>` 完全可能来自同一个文件。
- 参考视频自带音轨不代表自动产生 `<Audio N>`；只有当这段音频真的被复用/被参考时才写。
- `<Audio N>` 若对应某个说话人，复用该说话人的全局 ID：`<Audio 1> is the voice-timbre reference for <Subject 1> (S1).`

### 各段写法

**subject_definitions**：每个需要单独跟踪的素材一项、各占一行，说明标签指代什么、参考角色是什么、
要保留哪些主要特征。例：
```
<Subject 1> is the young woman in <Picture 1>, with long dark hair, a blue cardigan, and a thin silver necklace.
<Picture 2> is the first frame of [Shot 1], showing a woman seated beside a café window.
```

**summary**：一段英文，以方括号任务类型前缀开头，用 ` + ` 连接多个类型，不重复：
`keyframe completion` / `reference generation` / `video editing` / `video continuation` / `audio reuse` / `audio reference`。
例：`[video editing + audio reuse] The target video is an edited version of <Video 1>. ...`
本段不得引入新标签。

**retention_analysis**：每个标签一行，说明它在目标视频里是如何被保留/迁移/复用/参考的。
可见内容用固定标记：`fully_preserved` / `partially_preserved` / `attribute_transfer` / `weak_reference`。格式：
```
<Subject 1> (appears in [Shot 1], [Shot 3]): fully_preserved - ...
<Picture 2> ([Shot 1] first frame): fully_preserved - ...
```

**detailed_description**：按播放顺序写画面、动作、镜头、声音、台词。**要尽可能详细**：
每个镜头交代构图、主体外貌与位置、环境与光线、动作与状态变化、运镜、当前声音，
以及**参考内容真正出现或生效的时间点**。不要退化成剧情梗概或参考关系清单。
镜头写法与 base 模式一致（见下「共用书写规则」）。

**overall_soundscape**：1~4 句英文，一段连续文字，概括环境音、动作音效、非语言人声；
台词/演唱/画内音乐属于 detailed_description，不要在这里重复。只有用户明确要求全程静音时才写 `N/A`。

**non_diegetic_music**：1~3 句英文，描述角色听不到、只有观众能听到的背景音乐：
配器、速度、节奏、强弱变化。不要写抽象的情绪词，也不要解释配乐的情绪功能。
角色能听到的音乐（演唱、乐器、收音机、电视、手机）是画内事件，写到 detailed_description 里。
没有非画内音乐时写 `N/A`。
