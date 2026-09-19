# webui/ · MiniMax-H3 操作前端

一个仿「即梦」的操作界面，套在现有工程外面：**写中文意图 → DeepSeek 改写成官方规范的英文提示词 → 调参数 → 提交生成 → 看进度和硬件占用**。
生成仍然走原来的 `run_h3.sh gen`，流水线一行没改。

另有一个「网盘」页签（可选能力）：网络慢、视频在线播放卡的时候，把产物**原样打包加密（密码 123456）→ 传到夸克网盘**，
拿分享链接去下载，速度走网盘而不是这台机器的上行带宽。**不重新编码，画质不变**；套加密 zip 是为了防网盘内容抽检
（同时把「统一密码」落在压缩包上 —— 分享链接的提取码是服务端生成的，指定不了）。反向也能把网盘文件下载回本机当参考素材。
用的是 `web_disk/` 下的夸克官方 CLI，设计与踩坑见 [`docs/QUARK.md`](../docs/QUARK.md)。

```bash
# 1) 在 WSL(Ubuntu) 终端里起后端（前台运行，Ctrl+C 停；关掉终端即停服）
cd /mnt/d/otherProject/minimax-h3 && python3 webui/serve.py

# 2) 在 Windows 侧打开页面（后端已就绪就直接开浏览器，否则打印上面那条命令）
pwsh webui/start.ps1
```

> 两条命令是分开的，因为它本来就是这个架构：后端住在 WSL 里，浏览器住在 Windows 上。
> `start.ps1` 只负责探端口 + 开浏览器，不在 Windows 侧去拉 WSL 进程 ——
> 半启动状态会留下一个连不上的空端口，比直接报错更难查。
> 想一条命令搞定：Windows 侧 `pwsh webui/start.ps1 -InWsl`（需要能调 wsl.exe）——
> 它会转成 WSL 里的 `bash -lc` 前台启动后端；本机当前会话里 wsl.exe 被环境拒绝，所以默认路径是两条命令。

打开后是 http://127.0.0.1:8765/ 。页面里「使用说明」页签有一份面向使用者的文档（`webui/web/assets/guide.md`）。

---

## 1. 目录

```
config/                        ← 全部配置（唯一需要改的地方）
  deepseek.yaml                提示词优化：url / model / thinking / key / system_prompt
  server.yaml                  前端服务：监听、并发、上限、默认值
  quark.yaml                   夸克网盘：「网盘」页签的压缩/密码/分享/runner（见 docs/QUARK.md）
  params.spec.json             参数规格（由 webui/tools/gen_params.py 从工程本体生成，不要手改）
  prompts/
    system.md                  提示词优化的 system prompt
    format-ref2va.md           六段式结构规范（有视频/音频/多参考时）
    format-oneref.md           单图/关键帧结构规范（I2VA / FL2VA / L2VA）
    format-base.md             三核心字段结构规范（T2VA）+ 三种结构共用的书写规则

webui/
  serve.py                     后端入口（python3 webui/serve.py）
  start.ps1                    Windows 侧启动器（探端口 + 开浏览器；-InWsl 可直接拉起后端）
  backend/
    server.py                  HTTP 路由（REST + SSE）
    jobs.py                    作业排队/执行/进度转发/取消/错误归因
    promptopt.py               DeepSeek 调用 + 中文回译的标记流解析 + LLM 调用记录
    media.py                   参考图/视频抽帧 -> OpenAI 兼容的多模态 image_url 块（纯标准库 + ffmpeg）
    runlog.py                  运行日志系统（结构化/落盘/订阅）+ LLM 调用记录仓库
    estimate.py                序列长度/耗时/风险预估（复用 scripts/h3_audit.py 的算法）
    telemetry.py               硬件遥测（nvidia-smi + /proc）
    quark.py                   夸克网盘 CLI 封装（node 探测 / WSLENV / 路径翻译 / 任务管理）
    pack.py                    上传前的压缩：ffmpeg 转码 + 纯标准库加密 zip（密码 123456）
    config.py, util.py         配置读取与工具
  web/                         静态前端（无构建步骤，改完刷新即可）
    index.html  assets/app.css  assets/app.js  assets/guide.md
  tools/
    gen_params.py              从 cache/plan.json + scripts/h3_generate.py 生成参数规格
    param_docs.py              参数中文文档（一句话/详细/经验/风险/取值语义）—— 文案的唯一来源
    mock_deepseek.py           本地假 DeepSeek 服务，用于离线验证优化链路
    test_logging.py            运行日志 / LLM 记录 / 日志端点的离线自测
    test_log_render.js         日志行渲染的桩测试（node，不需要浏览器）
    test_docs_render.js        参数文档渲染的桩测试（node，不需要浏览器）
    test_disk.py               夸克网盘接入的离线自测（压缩包往返 / 转码 / 全部 /api/disk/*）
    test_disk_ui.js            网盘页签的静态自检（node：id 对账 / 按钮绑定 / 端点对账）
    test_prompt_flow.js        「优化出的英文有没有进流水线」的静态自检（node：提交路径 / 四条分支）
    test_session_ui.js         「对话（会话）管理 + 左栏折叠态 + 页签切换」的静态/行为自检（node）
    test_restore_job.js        「作品库 -> 恢复完整会话/参数」的静态 + 行为自检（node）
    test_restore.py            后端：恢复上下文写进作业记录、重启后仍能读回（python，离线）

docs/PARAMETERS.md             由 gen_params.py --write-docs 生成的参数参考手册
docs/QUARK.md                  夸克网盘接入的设计与踩坑记录（CLI 环境判定、加密 zip、约束对齐）
```

---

## 2. 架构：为什么这样解耦

```
┌─────────────── Windows ───────────────┐      ┌──────────────── WSL (Ubuntu) ─────────────────┐
│  浏览器  http://127.0.0.1:8765/       │      │                                               │
│    │  静态页面 + fetch/EventSource     │      │  serve.py（纯标准库 HTTP 服务，不 import torch）│
│    └──────────── HTTP / SSE ──────────┼──────┼─► ├── 作业管理：subprocess.Popen(run_h3.sh gen) │
│                                       │      │   ├── 遥测：nvidia-smi / /proc/meminfo         │
└───────────────────────────────────────┘      │   └── 优化：urllib -> api.deepseek.com          │
                                               │            │                                    │
                                               │            ▼ stdout 上带 @@H3@@ 前缀的结构化行    │
                                               │   scripts/h3_generate.py（原工程，行为未改）      │
                                               └─────────────────────────────────────────────────┘
```

第四条链路（可选，网盘）：后端在 WSL 里用 `cmd`/interop 拉起 **Windows 侧的 `node.exe`** 跑夸克 CLI，
产物原样打包加密后上传，页面拿到分享链接；配置见 `config/quark.yaml`，设计见 [`docs/QUARK.md`](../docs/QUARK.md)。

三条边界：

1. **前端 ↔ 后端**：只有 REST + SSE，没有共享代码。前端就是三个静态文件。
2. **后端 ↔ 生成系统**：只有 subprocess 与 `run_h3.sh` 的命令行契约。后端**不 import torch、不 import diffsynth**；
   生成脚本崩了只会让那个作业失败，服务本身不受影响。
3. **生成脚本 → 后端**：唯一的方向是 stdout 上一行 `@@H3@@ {json}`。这是 `h3_generate.py` 里唯一为前端加的钩子
   （约 25 行，见该文件的 `report()`），由环境变量 `H3_PROGRESS_JSONL` 触发。
   **不设这个变量时它是彻底的 no-op**，终端用法、`--bench`、`--dry-run` 的输出与以前完全一致。

> 验证过：`./run_h3.sh dry --preset draft --prompt x` 的输出与加钩子之前逐字相同。

### 参数文案为什么也只有一个源

每个参数的「一句话 / 详细说明 / 经验 / 风险 / 取值语义」全部写在 `webui/tools/param_docs.py` 一处，
由 `gen_params.py` 带进 `config/params.spec.json`，三个消费方共用：

| 消费方 | 呈现方式 |
|---|---|
| 页面上每个参数卡片 | 一句话常显；「详细说明」折叠区放 detail / 经验 / 风险 / 取值语义表；再加一行常量（默认值·范围·可选值·对应命令行开关） |
| 页面「环境自检」页签 | 完整参数手册（按分组铺开，含 5 段补充说明）+ 规格表 |
| `docs/PARAMETERS.md` | 可搜索、可提交的参考文档（`gen_params.py --write-docs` 生成，约 480 行） |

改文案只改 `param_docs.py`，跑一次 `python3 webui/tools/gen_params.py --write-docs`，三处同时更新。
漏写的参数会被 `params_undocumented` 列出来并打印在生成日志里 —— 以后新增参数不会静默没文档。

### 为什么用标准库写后端

WSL 只有 22.9 GiB 内存，而生成作业峰值就要 ~11 GiB（DiT 常驻 CPU 时）。后端每多占 100 MiB，
长片段就多一分被 OOM kill 的风险。所以后端只用 `http.server` + `threading`（外加可选 PyYAML，
conda 环境里本来就有），不引 FastAPI/uvicorn。实测后端常驻内存 < 60 MiB。

### 为什么不需要端口转发

`~/.wslconfig` 里是 `networkingMode=Mirrored`，WSL 监听 `0.0.0.0:8765` 时 Windows 侧
`127.0.0.1:8765` 直接可见（已实测 200）。如果哪天关掉 Mirror 模式，两种方式任选：

- `config/server.yaml` 的 `host` 保持 `0.0.0.0`，浏览器访问 `http://<wsl-ip>:8765/`（`wsl hostname -I`）；
- 或 `netsh interface portproxy add v4tov4 listenport=8765 connectaddress=<wsl-ip> connectport=8765`。

---

## 3. 通信协议

### 3.1 作业事件流（SSE，`GET /api/jobs/<id>/stream?from=<seq>`）

每条事件形如 `event: <type>` + `data: <json>`，空行分隔。`seq` 单调递增，断线后用 `from` 续传
（先回放磁盘上已有的事件，再接实时）。

| 事件 | 含义 | 关键字段 |
|---|---|---|
| `snapshot` | 连接建立时的完整作业状态 | `job`（含 request / estimate / progress） |
| `log` | 子进程原始输出的一行 | `line` |
| `stage` | 阶段切换 | `stage` / `stage_zh` / `info` |
| `step` | 去噪进度 | `i`（已完成步数）/ `total` / `s_step` / `eta_s` / `peak_vram_gib` |
| `result` | 产物信息 | `out` / `frames` / `seconds_out` / `size_bytes` |
| `warn` / `error` | 需要人看的消息 | `message` / `hint` |
| `exit` | 作业结束（流随之关闭） | `code` / `status` / `error` |

阶段取值：`queued → prepare → build → text → denoise → decode → done`。
`text` 阶段在命中提示词缓存时会直接跳过，这是正常的（缓存命中就是 0 s）。

### 3.2 提示词优化流（SSE，`POST /api/optimize`）

请求体 = 生成参数 + `chinese`（用户原话）+ `refs`（带 label / path 的参考清单）。
返回事件：`open` `meta` `thinking` `section` `delta` `done` `warn` `error`。

**多模态**：开启了 `multimodal` 时，后端会用 `refs[*].path` 读本地参考图，按服务端约束缩放/转码后
以 base64 附进 **user message 的 content 块数组**（OpenAI / DeepSeek 兼容格式）：

```json
{"role": "user", "content": [
  {"type": "text", "text": "……（中文意图 + 参数 + 清单 + 附图编号）"},
  {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,...", "detail": "high"}}
]}
```

没有附图时 content 仍是**字符串**，请求体与旧版逐字节一致。`meta` 事件新增 `multimodal` 字段
（`images` / `detail` / `attachments` / `notes`），前端据此显示「附图 N 张」，
调试页签展示每张图的名称、尺寸、缩放与字节数。图片 base64 **不会**写进 LLM 调用记录
（`/api/llm/runs/<id>` 只存附件元数据），日志里也只看得到 `images=n`。

模型被要求在正文外包裹三段标记：

```
<<<H3_PROMPT>>>  英文提示词（真正送进流水线的那一段）  <<<END_H3_PROMPT>>>
<<<H3_ZH>>>      中文回译（给人核对）                  <<<END_H3_ZH>>>
<<<H3_NOTES>>>   结构说明                              <<<END_H3_NOTES>>>
```

`promptopt.py` 用一个小状态机把流式增量切成三段，前端分页签实时渲染。
模型漏段时 `done.missing` 会列出来，页面给警告（而不是假装成功）。

**哪一份提示词进流水线**：`POST /api/jobs` 的 `prompt` 字段由前端的 `effectivePrompt()` 决定 ——
有**新鲜**的英文优化结果（`S.optimize.source` 与输入框当前内容一致）就用英文，否则回落输入框里的中文原文；
输入框下面一直写着本次会用哪一份。优化之后又改了输入框，英文会被判为「已失效」而**不会**被送进流水线
（而不是悄悄用旧英文）；「用中文原文重来」等价于显式选择中文原文。
`test_prompt_flow.js` 钉住这条链路：`buildSubmitBody` 曾经直接读输入框，于是优化出的英文只停在页面上、
提交给 `run_h3.sh` 的却是中文原文 —— 页面显示的和实际生成的是两份东西。

### 3.3 对话（会话）管理（纯前端）

左栏的「对话」列表把「一次创作的完整上下文」存成了可回切的记录：中文原文、参考素材、
优化结果（英文 / 中文回译 / 结构说明 / 调试全文）、生成参数与编辑模式。**全部在浏览器
`localStorage` 里，后端不参与** —— 刷新、关掉再打开都不丢，也不需要给后端加一套存储。

- 新建 / 切换 / 重命名（✎）/ 删除（✕）都在左栏；当前对话高亮，标题默认取提示词首行（可重命名覆盖）。
- 输入框、参考素材、参数、预设、优化结果任一改动都会**防抖自动保存**；关页面前再兜一次底。
- 老版本只存了一份草稿（`h3ui.session`），首次加载会自动收编成第一个对话。
- 存储写满时优先丢弃历史对话的「调试全文」（system / user / 思考），提示词与参数一定保住。

同一套 localStorage 还存界面偏好：左栏折叠状态、右侧监控面板显隐（`h3ui.ui`）。

### 3.4 从作品库恢复完整会话（失败也能改参数重跑）

任务失败（最典型的是爆显存）在「作品库」里同样是一条记录，而且**不只是留一个墓碑**：
每行操作列的「恢复参数」会把这条作业变回一个**新的对话** —— 中文原文、优化结果
（英文 / 回译 / 结构说明 / `runId`）、参考素材、模式与生成/编辑页签、全部生成参数一项不落地
铺回「创作」页，并停在这个作业当时的形状上，改完直接重新生成。

- **为什么能恢复**：提交时后端把 `prompt_zh`（中文原文）/ `prompt_source` / `mode` /
  `mode_tab` / `optimize` 一并写进 `job.json`（见 `backend/jobs.py` 的 `clean_optimize`）。
  只存送进流水线的那一份英文，事后恢复出来就是一个无法继续调整的孤本 ——
  而「刚才是哪个参数把它撑爆的」恰恰是恢复时最想知道的事。
  `system` / `user` / 思考全文不进 `job.json`（它们有自己的 LLM 调用记录），只留 `runId` 当入口。
- **恢复非破坏性**：当前对话先落盘，恢复出来的作业另开一个对话，左栏随时切回。
- **创作页顶部有提示条**：写明原作业号、原状态与失败原因，并给「看原作业日志」直达。
- **老记录**（本次改动之前提交的）没有中文原文与优化结果：恢复时把当时实际使用的那一份提示词
  放回输入框，并如实标成「旧记录未标注来源」，不会伪造一份中文原文。
- 作品库还有「只看未完成 / 失败」开关与「详情」：详情能看到提示词来源、形状、参数与失败原因，
  恢复按钮就在详情弹窗里。

> 顺带修掉一个让作品库「看不出内容」的老 bug：`Job.to_dict(include_request=False)` 从空字典里
> 摘字段，列表里的 width / height / steps / seed 一直是 `None`（页面上显示成问号）。
> 现在从 `self.request` 摘。

### 3.5 其余端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 自检：仓库、脚本、参数规格、DeepSeek Key、ffprobe、nvidia-smi |
| GET | `/api/config` | 参数规格 + 预设 + 服务配置（**不含 API Key**，只报有没有配） |
| GET | `/api/telemetry?history=N` | 当前 GPU/内存/CPU/磁盘 + 最近 N 个采样点 |
| POST | `/api/estimate` | 形状吸附 + seq + 耗时预估 + 风险清单（前端改参数就调一次） |
| POST | `/api/preview` | 干跑：返回最终命令行与解析后的请求（不加载权重） |
| POST | `/api/jobs` | 提交；带致命风险时返回 409 + `need_force`，前端弹二次确认 |
| GET | `/api/jobs` | 作业列表 + 队列状态 |
| GET | `/api/jobs/<id>` `/log` `/events` `/result` | 详情 / 原始日志 / 事件 / 产物路径（含 Windows 视角）。详情返回**完整 request**（中文原文 / 优化结果 / 参数），作品库的「恢复参数」靠它还原会话 |
| POST | `/api/jobs/<id>/cancel` | 取消：先 SIGINT（当前步结束后退出），超时再 SIGKILL 整个进程组 |
| POST | `/api/upload` | 上传参考素材，返回 WSL 内路径（并顺手 ffprobe 出尺寸/帧数） |
| POST | `/api/paths` | 把 `D:\...` 之类的路径翻成 `/mnt/d/...` 并校验存在性 |
| GET | `/api/file?path=` | 把 WSL 里的媒体发给浏览器（**仅限仓库目录与模型目录**，越界 403） |
| GET | `/api/lora` | 扫描 `$H3_MODELS` 里可用的 LoRA |
| GET | `/api/logs` | 运行日志查询：`level` / `source` / `q` / `n` / `offset` / `since_seq`，返回 `records` + `stats` |
| GET | `/api/logs/stream` | SSE 运行日志实时流（先 `snapshot` 再 `log`，过滤参数同上） |
| GET | `/api/logs/download` | 导出当前筛选为 `txt`（默认）或 `json` |
| POST | `/api/logs/clear` | 清空内存日志；`{"files":true}` 连磁盘文件一起清 |
| POST | `/api/logs/level` | 运行时切换等级：`{"level":"debug"}` |
| POST | `/api/logs/client` | 浏览器端上报日志（前端异常等），单次 ≤ 50 条 / 64 KiB |
| GET | `/api/llm/runs` | LLM 调用记录列表（`limit` / `status` / `q`） |
| GET | `/api/llm/runs/<id>` | 单次调用的完整记录：请求 / system / user / 输出 / usage / 事件 |

**夸克网盘**（见 [`docs/QUARK.md`](../docs/QUARK.md)）。除 `status` 外都返回 `{ok, task}`，
进度走 SSE，与生成作业同一套事件模型：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/disk/status` | CLI / node / 授权状态。`probe=0` 不发网络请求，只给「上次已知」；`probe=1` 真去问一次（12s 内复用缓存）。**没查过时返回 `auth_known: false` 且不带 `logged_in`** —— 页面对应显示「加载中…」，绝不把「不知道」画成「未授权」 |
| GET | `/api/disk/files` | 浏览目录：`parent_fid` / `page_size` / `all=1`（读 CLI 的 Artifact 拿全量） |
| GET | `/api/disk/search` | 搜索：`keyword` / `size` / `search_type` / `parent_fid` |
| POST | `/api/disk/login` | 发起授权：`{}` 走浏览器 OAuth，`{token}` 走授权码 |
| POST | `/api/disk/publish` | **压缩 + 加密打包 + 上传 + 建分享链接**；`dry_run=true` 只压不传 |
| POST | `/api/disk/fetch` | 把网盘文件下载回 `workspace/netdisk/`（`fid` 或 `fids`） |
| GET | `/api/disk/tasks` `/tasks/<id>` | 网盘任务列表 / 单个任务 |
| GET | `/api/disk/tasks/<id>/stream` | SSE：任务事件流（`stage` / `progress` / `cli` / `exit` …） |
| GET | `/api/disk/tasks/<id>/log` | 任务原始输出尾部 |
| POST | `/api/disk/tasks/<id>/cancel` | 取消（先 SIGINT，再 SIGKILL 进程组） |

> 未授权时这几条会返回 **401 + `need_login: true`**（body 里是 CLI 的 `msg` 原文），
> 页面据此直接把授权入口顶出来，而不是丢一句「失败」。

---

## 4. 配置怎么改

### 换模型 / 开思考模式

`config/deepseek.yaml`：

```yaml
api:
  url: "https://api.deepseek.com/chat/completions"
  key: ""                      # 留空则读环境变量 DEEPSEEK_API_KEY
model:
  name: "deepseek-reasoner"
  thinking:
    enabled: true
    style: "reasoning_effort"  # reasoning_effort | thinking | both
    reasoning_effort: "high"   # minimal | low | medium | high | max
```

`thinking.style` 是刻意做成显式的：各家网关对思考参数的字段名不统一
（`reasoning_effort` 还是 `thinking: {type: enabled}`），这里不猜，选了哪个就只发哪个，
`both` 用于兼容性不明确的网关。被服务端拒绝时，报错原文会**原样返回给页面**，方便对着文档改。

**配置是热读取的**：每次点「优化」都会重新解析这两个 yaml，改完刷新页面即可，不用重启服务。

### 多模态附图（让模型真的看到参考图）

`deepseek-flash` 能看图。开了 `multimodal` 后，参考图会随请求一起发过去，
模型写人物/服装/配色/场景时会**以图为准**，而不是只根据你清单里的文件名想象。默认开：

```yaml
multimodal:
  enabled: true          # false = 退回纯文本请求（与旧版逐字节一致）
  images: true           # 附参考图
  video_frames: 0        # 参考视频抽几帧当图片（0=不抽；1~4 有助于运镜/动作）
  max_images: 8
  detail: "high"         # low(服务端按 512x512) | high | original | auto
  max_edge: 1280         # 附件长边上限；服务端 >约1300px 也会缩，再大只是浪费请求体
  max_mb_per_image: 20   # 服务端硬上限 32
  max_mb_total: 40       # 服务端请求体硬上限 48
```

- **怎么做的**：`webui/backend/media.py` 按**文件内容**判格式（不看扩展名），
  用系统 `ffmpeg` 缩放/转码（`force_original_aspect_ratio` 会放大，所以改用 `min(iw,cap)` 钉死上界），
  认不出的格式先用 `ffprobe` 确认 `codec_name` 属于图片，避免把二进制垃圾当图片发出去。
- **失败不阻断**：某张图过大/格式不认/读不到时只记 `notes` 并跳过，
  优化照常进行，前端会把这些 note 弹成警告。
- **换到不支持图片的模型 / 网关**：把 `enabled` 设成 `false`；服务端拒绝时报错会原样返回页面。
- 环境自检页会检查 `ffmpeg`；缺失时退化成「原图直传」（受 32 MiB 限制），视频抽帧不可用。

### 改提示词规范

- `config/prompts/system.md`：角色、硬性约束、输出标记协议；
- `config/prompts/format-*.md`：三种结构规范。它们是从 `references/base-en.md` 与 `references/ref-en.md`
  提炼的**可配置摘要**（原文 40 KB，作为 system prompt 太贵），保留了全部字段名、标签规则、时间码格式、
  镜头/运镜/台词写法；
- `prompt.extra_instructions`：追加在 system 末尾的自定义要求。

> 想知道当前实际发了什么：页面上「调试」页签会显示完整的 system / user / 思考过程。

### 网盘（夸克）：让慢网络下也能把片子取走

配置只有一个文件：`config/quark.yaml`（**热读取**，改完刷新页面即生效）。完整设计见
[`docs/QUARK.md`](../docs/QUARK.md)，这里只列最常动的几项：

```yaml
runner: auto            # auto | windows | linux —— 后端在 WSL，node 在 Windows 时走 interop
archive:
  enabled: true         # 打包成加密 zip：内容加密防抽检 + 统一密码
  password: "123456"    # 分享链接的提取码是服务端生成的、指定不了，所以密码落在压缩包上
  level: 0              # 0=只打包（视频压不动，deflate 白烧 CPU）；要塞文档再调 6
  tool: auto            # 打包实现：auto=优先 bsdtar（Windows 自带 tar.exe，74 MiB/s）
                        # 没它就退回纯 Python（2.2 MiB/s）；也可强制 tar / python
compress:
  enabled: false        # 默认关：重新编码是**有损**的，画质优先就别开
  crf: 26               # 要省流量时再开：越大越小越糊；页面上也能临时勾
  max_edge: 1280        # 只缩不放
share:
  enabled: true
  url_type: 1           # 1=公开链接（只有压缩包一层密码） 2=私密链接（夸克另给提取码）
  expired_type: 1       # 1=永久 2=1天 3=7天 4=30天 …
upload:
  parent_fid: ""        # 留空 = 用下面的 dir_name 自动建/复用一个目录（不碰根目录 "0"）
  dir_name: MiniMax-H3  # 【实测坑】CLI 的「默认目录」是空的：省略 --parent-fid 会直接报
                        #   参数错误: [upload dir blank]，所以留空时我们按这个名字先建/复用目录
download:
  dir: workspace/netdisk
```

**两条容易踩的**：

1. **CLI 认「agent 环境」**，认不出来会直接返回 `{"code":-104,"msg":"无法识别当前 Agent 环境"}`。
   实测必须带 `DSH_HOME` 或 `DSH_SESSION_ID` 之一（只给 `QK_AGENT_ID` 没用）；
   Windows runner 下这几个变量要经 `WSLENV` 才能过界。这些都封装在 `backend/quark.py`，
   一般不用动 `agent` 段。
2. **改了 `webui/backend/` 下的代码要重启后端**（配置不用）。重启会触发一次「孤儿进程清理」，
   **正在跑的生成作业会被杀掉** —— 等作业跑完再重启。

### 改参数上限与默认值

`config/server.yaml` 的 `limits` 与 `defaults`。改了 `scripts/h3_generate.py` 的参数
（新增 `--xxx`）之后，跑一次 `python3 webui/tools/gen_params.py` 重新生成 `config/params.spec.json`，
再在 gen_params.py 里补一行中文标签即可。

---

## 5. 预估是怎么算的（以及为什么可以信）

- **形状吸附**：直接调用 `scripts/h3_audit.align_shape()`，与运行时同一份逻辑。
- **序列长度**：调用 `h3_audit.ref_image_rows` / `ref_video_rows` / `align_shape`，逐项算 rows 再对齐。
  **已对全部 10 个预设逐项校验，与 `cache/plan.json` 的 `seq_len` 完全一致**（见下）。
- **单步耗时**：不用解析模型（那套公式在实测面前差 65%），而是在 `config/params.spec.json` 的实测点上做
  log-log 插值；落在点之间标 `interpolated`，超出范围标 `extrapolated`，页面直接显示置信度标签。
- **风险判定**：基于工程里量过的墙 —— 显存墙（最高跑通点 `1024x576x124`，峰值 5.94 GiB）、
  内存墙（`dit_onload=cpu` + 文本编码器 mmap 顶到 22.9 GiB 配额）、参考视频代价、
  `vram_limit` 的反直觉性、帧数实测下限。

```
preset        shape                mine    plan
blitz         640x384x22           2944    2944
fast          640x384x39           4224    4224
draft         640x384x73           7232    7232
preview       832x480x73          11008   11008
standard      832x480x124         17024   17024
quality       1024x576x124        23872   23872
text-only     832x480x124         16000   16000
video-edit    832x480x124         25728   25728
max-native    1344x768x124        39872   39872
max-long      832x480x243         31040   31040
```

---

## 6. 防呆与鲁棒性（都是被真实事故逼出来的）

| 机制 | 对应的事故 |
|---|---|
| 帧数下限抬到 22 | 实测 5 帧能过框架形状检查，但 `decode_video` 返回 None，报 `'NoneType' object has no attribute 'float'` |
| 非法形状即时提示「会被吸附成什么」 | 曾经 328 帧的非法值被静默吸附成 345 帧，直接 OOM |
| 致命配置二次确认（409 + force） | `1344x768x124` 是本机跑不了的档，不该手滑点下去 |
| 单作业串行 + 排队 | 8 GiB 卡上并发必然 OOM |
| 错误归因（把日志尾巴翻译成人话） | `Failed to create GPU mapping` 其实是 OOM；`device not ready` 是上一次 OOM 的后遗症 |
| 服务重启后清理孤儿进程 | 后端被强杀后，生成子进程会继续吃 11 GiB 内存，让下一次生成莫名 OOM |
| 取消走 SIGINT → 超时 SIGKILL 进程组 | `run_h3.sh` 会 spawn 出 python 子进程，只杀父进程会留下孤儿 |
| `/api/file` 限定目录 | 本地工具也不该变成任意文件读取的口子 |
| 上传大小/扩展名白名单 | 手滑拖进一个 8 GB 的 mkv 不该把磁盘写满 |
| 网盘任务单并发 + 可取消 | 上传占的是上行带宽，和生成抢网卡没意义；一次发一个片子才看得清 |
| 上传前**强制**压缩 + 加密打包 | 「网络慢」这个前提下的产物必须足够小，且有统一密码；不提供「原样直传」的默认路径 |

---

## 7. 已知限制

- **前端没有做鉴权**：服务默认绑 `0.0.0.0`，同局域网内可访问。单人本机工具够用；
  如果这台机器在不可信网络里，把 `config/server.yaml` 的 `host` 改成 `127.0.0.1`。
- **进度到「步」为止**：`step` 事件在每个去噪步之后发出，步内没有更细的粒度
  （框架的 `progress_bar_cmd` 就是逐步回调）。
- **参考素材尺寸靠 ffprobe**：读不到就退化成按常见尺寸估算，预估面板会标 `estimated`。
- **一次 OOM 仍然会污染后续配置**（这是工程本身的性质）：页面只能把 `wsl --shutdown` 的建议送到你面前。
- **已对真实 `deepseek-flash` 端点联调过**：纯文本与多模态（`{"type":"image_url","image_url":{"url":"data:...","detail":"high"}}`）
  都返回 200，模型能正确读出图中内容。离线回归仍走 `webui/tools/mock_deepseek.py`
  （本次新增：content 数组解析、图片计数、base64 可解性校验），SSE 解析 / 三段标记 / 错误分支 / 无 Key 分支都有覆盖。
  若某个中转网关对 `thinking` 字段名不一致，把 `thinking.style` 换成 `both` 或 `thinking` 即可。
- **网盘页签依赖 Windows 侧的 Node**（WSL 里没装 node 时经 interop 跑 `node.exe`）；
  哪天 interop 被禁，在 WSL 里装个 node 即可，`runner: auto` 会自动切过去。
- **分享链接的提取码是服务端生成的，改不了**，所以「统一密码」落在压缩包上；
  压缩包用的是 ZipCrypto（不是 AES），强度只够「防误传」，详见 `docs/QUARK.md` §9。
- **多模态附图的三个服务端约束**（超出会 400）：单图 base64 ≤ 32 MiB、请求体 ≤ 48 MiB、单请求 ≤ 600 张。
  `media.py` 默认按远低于这些值的预算工作（20 / 40 MiB），超预算的图片跳过并在日志里说明。
  `refs` 里没有 `path` 的客户端（例如直接 curl `/api/optimize`）不会附图，只会在 `notes` 里提示。

---

## 8. 运行日志与 LLM 调用记录（调试用）

日志不是「顺便打印几行」，它是排查三类问题的**证据链**：LLM 到底发了什么、回了什么；
生成作业在哪个阶段失败；产物的路径/大小对不对。所以记录是结构化的，而且落盘。

### 8.1 两条通道

| 通道 | 内容 | 位置 |
|---|---|---|
| 内存环形缓冲 | 最近 `logging.capacity` 条（默认 2000） | 页面实时流、过滤查询 |
| JSONL 文件 | 同样的记录，按大小轮转 | `cache/webui/logs/webui.jsonl`（+`webui.N.jsonl`） |

页面「运行日志」页签提供等级 / 来源 / 关键字过滤、跟随最新、自动换行、下载、清空，
以及下面说的 LLM 调用记录表。右侧监控面板的小日志窗是同一份数据的 `info+` 视图。

> 服务启动时会把 `webui.jsonl` 尾部读回内存（seq 从历史最大值继续），所以**重启后页面里
> 仍然能看到上一次运行的日志**，不必去命令行 `tail`。

### 8.2 一条记录长什么样

```json
{"seq": 12, "t": 1757.0, "iso": "2026-09-11 20:57:25.499", "level": "info",
 "source": "llm", "event": "llm.request", "msg": "promptopt: 请求 DeepSeek",
 "run": "20260911-205725-0001", "model": "deepseek-flash", "mode": "base",
 "system_chars": 4651, "user_chars": 364}
```

`source`：`llm`（模型调用）/ `job`（生成作业）/ `http`（请求与异常）/
`server` / `sys` / `ui`（浏览器上报）。
`event` 是稳定事件名（`llm.start` / `llm.finish` / `llm.error` / `http.request` /
`job.start` / `server.start` …）：过滤、写告警请按它匹配，别匹配会变的中文文案。

### 8.3 LLM 调用一次一份完整记录

`GET /api/llm/runs/<id>` 返回：请求参数、**system / user 全文**、流式输出
（英文提示词 / 中文回译 / 结构说明）、**模型原始输出（解析前全文）**、思考过程、`usage`、
耗时、错误、重试与中断事件。
文件落在 `cache/webui/logs/llm/<run_id>.json`，只保留最近 `llm_max_runs` 份。

- `llm_capture`：`full`（默认，存全文）/ `summary`（只存长度与 tokens）/ `off`。
- 页面「优化提示词」完成后，状态行有「查看本次 LLM 调用记录」直达；
  历史记录在「运行日志 → LLM 调用记录」，可下载 JSON。
- 客户端中途断开（关页面）会把这次调用标成 `aborted`，而不是假装成功。

### 8.4 模型不按格式输出时，排查靠这些字段

提示词优化用的是固定标记协议（`<<<H3_PROMPT>>>` / `<<<H3_ZH>>>` / `<<<H3_NOTES>>>`）。
模型不听话**不算调用失败**（HTTP 200、tokens 照扣），但必须能事后查 —— 所以记录里除了解析
结果，还留着解析前的现场：

| 字段 | 含义 |
|---|---|
| `response.raw` | 模型原始输出（解析前全文；`capture=full` 时才有正文） |
| `response.raw_chars` | 原始输出总长度；`summary` 模式也留，>0 说明模型确实说了话 |
| `response.stray` | 落在标记之外、被解析器丢掉的文字（前言 / 尾注 / 整段自然语言） |
| `response.markers` | 实际见到过哪些开始标记；`[]` = 完全没按协议走 |
| `response.format_ok` | 三段是否都拿到了；`false` 时页面状态显示 `ok·缺段` |
| `response.bad_chunks` | SSE 里解析不了的行数（被代理/网关改写成 HTML 报错页时会 >0） |
| `events[].kind=unterminated` | 某段漏写结束标记：正文按原文收下，另记一条事件说明 |

运行日志里按事件名同样筛得到：`llm.format`（调用成功但缺标记段，warn）、
`llm.bad_stream`（一行 SSE 都解析不出来）、`llm.abort`（前端断开）、`llm.error`。

页面路径：「优化提示词」状态行 →「查看本次 LLM 调用记录」→「原始输出（模型原文，未经解析）」；
历史记录在「运行日志 → LLM 调用记录」，可下载整份 JSON 存档。

> 排障顺序建议：先看 `markers`（一个都没有 = 模型没理协议，问题在 system prompt）、
> 再看 `missing`（只缺后半段 = 被 max_tokens 截断）、再看 `bad_chunks`（= 根本没连到
> 真正的模型端点）。三种原因的修法完全不同，别混在一起猜。

### 8.5 配置与等级

见 `config/server.yaml` 的 `logging` 段（等级、容量、目录、轮转、是否暴露 API、
LLM 记录开关与保留份数）。**等级可以在页面上运行时就改**（`POST /api/logs/level`），
不用重启服务；想看每个 API 请求的耗时与状态码，把等级调到 `debug`。

> 日志目录在 `cache/webui/` 下，已被 `.gitignore` 忽略 —— 提示词与产物路径不会误进版本库。

### 8.6 客户端中途断开（Connection reset by peer）

浏览器关标签页 / 刷新 / 丢弃 keep-alive 连接 / 局域网掉线时，对端会发 RST，服务端在
`handle_one_request` 读请求行就会抛 `ConnectionResetError`。`http.server` 只捕
`socket.timeout`，异常会一路冒到 `socketserver.handle_error` —— 那里**不经过**
`Handler.log_error`，会直接把整段 traceback（带 `----` 分隔线）刷到 stderr，把真正的
日志淹没。

`backend/server.py` 的 `Server`（继承 `ThreadingHTTPServer`）重写了 `handle_error`：
`ConnectionError` 一族（Reset / Aborted / BrokenPipe）只记一条 **debug** 记录
（`source=http`、`event=http.abort`、带 `ip`），其余异常照旧交给 `socketserver`
打印 traceback。效果：

- 页面按 `event=http.abort` 能筛出「谁在什么时候甩了连接」，但不会污染默认视图；
- stderr 里留下的 traceback 一定是真异常，不再被网络噪声淹没。

回归用例：`webui/tools/test_logging.py::ServerEndpointTest` 的
`test_client_abort_is_not_a_traceback` / `test_real_server_error_still_prints_traceback`。

---

## 9. 离线自测

```bash
# 0) 重新生成参数规格与文档（改了 param_docs.py 或 gen_params.py 之后）
python3 webui/tools/gen_params.py --write-docs

# 1) 后端自检（不起服务）
python3 webui/serve.py --check

# 2) 起服务 + 假 DeepSeek，验证优化链路
python3 webui/tools/mock_deepseek.py --port 8799 &
#   把 config/deepseek.yaml 的 api.url 指向 http://127.0.0.1:8799/chat/completions，
#   api.key 随便填一个非空值，然后在页面上点「优化提示词」

# 3) 参数解析干跑（不加载权重）
curl -s -X POST http://127.0.0.1:8765/api/preview -H 'Content-Type: application/json' \
  -d '{"prompt":"测试","width":640,"height":384,"num_frames":22,"steps":4,"preset":"blitz"}'

# 4) 运行日志 / LLM 记录 / 日志端点自测（纯离线；内部起临时服务 + 假 DeepSeek）
python3 webui/tools/test_logging.py -v
node webui/tools/test_log_render.js webui/web/assets/app.js   # 日志行渲染（可选）

# 4.5) 网盘接入自测（纯离线：临时端口 + 临时 cache，不会真的上传、不会弹浏览器）
python3 webui/tools/test_disk.py                               # 压缩包往返 / 转码 / 全部 /api/disk/*
node webui/tools/test_disk_ui.js                               # 网盘页签静态自检（可选）
node webui/tools/test_prompt_flow.js                           # 「优化 -> 提交」提示词链路自检（可选）
node webui/tools/test_session_ui.js                            # 对话管理 / 折叠态 / 页签切换自检（可选）
node webui/tools/test_restore_job.js                           # 作品库「恢复会话/参数」链路自检（可选）
python3 webui/tools/test_restore.py -v                         # 后端：恢复上下文写进作业记录 + 重启后读回（可选，离线）

# 5) 真跑一次最便宜的档（约 1 分钟，含模型装载）
curl -s -X POST http://127.0.0.1:8765/api/jobs -H 'Content-Type: application/json' \
  -d '{"prompt":"[Shot 1] rain on a window","width":640,"height":384,
       "num_frames":22,"steps":4,"preset":"blitz"}'
```

`webui/tools/gen_params.py` 与 `webui/tools/mock_deepseek.py` 都可以独立运行，方便排查。
