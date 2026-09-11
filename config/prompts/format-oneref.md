## 结构规范：单图 / 关键帧模式（i2va / fl2va / l2va）

参考素材只有图片、且图片被当作目标视频的具体帧来用时，走本结构。
输出 **两部分**：第一行是指令，空一行后是三个核心字段。

### 第一部分：指令（必须是第一行，之后空一行）

**I2VA**（一张图当首帧，例：`<Picture 1>` 是第 1 张参考图）：
```
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.
```

**FL2VA**（首帧 + 尾帧，两张图）：
```
How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the S.SS-second mark of the target video.
```
`N` 是实际最后一个镜头的编号，`S.SS` 是无歧义的目标时长（两位小数），必须等于 `duration_s`。

**L2VA**（一张图当尾帧）：
```
How the reference pictures align with the target video — <Picture 1> (from [Shot N]) aligns with the S.SS-second mark of the target video.
```

### 第二部分：三个核心字段（顺序固定，段间空一行）

```
integrated_multimodal_description: [Shot 1] ...

overall_soundscape: ...

non_diegetic_music: ...
```

写法要点：
- **I2VA**：`<Picture 1>` 就是 0.00 秒的真实首帧，属于 `[Shot 1]`。先立住图里的风格、主体、构图、场景锚点，
  再写接下来的动作。推荐结构：**首帧锚定 → 动作起势 → 连续发展 → 结果或反应**。
  人物身份、服装、颜色、关键物件、空间关系必须与图一致。
- **FL2VA**：Picture 1 是开头、Picture 2 是结尾，重点是**两者之间的运动路径**：
  主体怎么动、姿势怎么变、物件怎么被操作、构图如何演化、场景/光线如何过渡。
  推荐结构：**首帧状态 → 可观察的中间变化 → 差异逐步收窄 → 尾帧状态**。
  除非用户明确要求，否则**优先单镜头**，让模型从首帧连续过渡到尾帧；尾帧必须在视频结束时由最后一个 `[Shot N]` 抵达。
- **L2VA**：`<Picture 1>` 是视频的**最后一帧**，属于最后一个 `[Shot N]`，它天然不属于 Shot 1。
  从用户意图与尾帧反推一个合理的前置状态，再写角色/物件/镜头如何逐步逼近参考图。
  推荐结构：**合理前置状态 → 明确动作与过渡路径 → 末镜头逐步收敛 → 落在尾帧**。
