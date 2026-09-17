# 夸克网盘接入（web_disk → WebUI）

> 一句话：**本地网络慢、视频在线播放一直转圈时，把产物原样打包加密、传到夸克网盘，
> 再把分享链接交给看片的人 —— 下载速度由网盘决定，不再受这台机器上行带宽的限制。**
>
> **不重新编码、画质不变**；那层加密 zip 解决的是「防网盘内容抽检」和「统一密码」，
> 不是体积（mp4 早压过了，deflate 只能省 1%）。
>
> 反向也通：浏览 / 搜索网盘，把文件下载回 `workspace/netdisk/` 当参考素材。

本文记录**为什么这么接**、**怎么接的**、**踩过哪些坑**。面向使用者的说明在页面的
「使用说明」页签（`webui/web/assets/guide.md` §五），面向开发的端点表在 `webui/README.md`。

---

## 1. 手上有什么：quarkclouddrive CLI

`web_disk/quarkclouddrive-1.0.20.zip` 是夸克官方的 agent Skill 包，解压后：

```
web_disk/quarkclouddrive-1.0.20/
  SKILL.md                 官方技能说明（命令清单、约束、话术）
  references/*.md          各功能域的字段级文档（upload / share / search / download…）
  scripts/quark-drive.cjs  真正的 CLI（打包过的 Node 程序，约 600 KB）
  scripts/install.sh       「安装/更新」脚本：探环境 + 从服务端拉最新包覆盖
  scripts/uninstall.sh     撤销授权 + 清配置
```

它是 **Node 程序**，通过夸克开放平台 API 操作网盘；账号授权（access_token）由它自己
维护在本地配置目录里，**我们既读不到也不需要读**。

我们只按官方文档的命令行契约调用它，不读它的源码（Skill 里也明确禁止读 `quark-drive.cjs`）。

### 用到的命令

| 命令 | 用在哪 | 关键出参 |
|---|---|---|
| `get-user-info` | 网盘页签的「已授权 / 未授权」探活 | `code=0` 带账号信息；`-103` 未登录 |
| `login` / `login --token <码>` | 浏览器 OAuth 授权、授权码授权 | `data.status` / `data.msg` |
| `upload <路径> [--parent-fid N]` | 上传压缩件 | `progress` 行进度的 `percent`、`list` 行的 `fileId`、`result` 行的 `fids[]` / `fullPath` |
| `share <fid> --url-type N --expired-type N [--title T]` | 建分享链接 | `data.share_url`、私密链接另带 `data.passcode` |
| `download --fid <fid> --output-dir <目录>` | 把网盘文件取回本机 | 逐行进度；文件名从目录快照差分得到（见 §5） |
| `browse --parent-fid N [--all]` | 浏览目录 | `list` 行是 FileVO；`--all` 另出 `artifact`（完整 JSONL） |
| `search --keyword K [--stdout-only]` | 搜索 | `result.data.file_list` 只有 ≤5 条预览，完整结果在 `artifact` |

stdout 是 **NDJSON**：一行一个 JSON，`type` 取 `result` / `progress` / `list` / `artifact`，
失败时顶层 catch 输出一行带负数 `code` 的 `result`。后端就是按这个协议解析的。

---

## 2. 第一道坎：CLI 认「agent 环境」，认不出来直接罢工

直接跑会得到：

```json
{"code":-104,"msg":"无法识别当前 Agent 环境，禁止继续使用","action":"runtime","type":"result","data":{}}
```

实测出来的判定规则（`resolve-agent` 命令可以打印它认到的渠道：`QK_AGENT_ID=deepseek`）：

| 环境变量 | 结果 |
|---|---|
| 什么都不给 | `-104` |
| 只给 `QK_AGENT_ID=deepseek` | **仍然是 -104** |
| 只给 `DSH_HOME`（值随便，哪怕是个不存在的路径） | 通过 |
| 只给 `DSH_SESSION_ID`（任意字符串） | 通过 |
| 只给 `DSH_SHELL` | `-104` |

结论：**必须有 `DSH_HOME` 或 `DSH_SESSION_ID` 之一**，`QK_AGENT_ID` 只是渠道标签。
本项目是 DeepSeek 渠道的创作台，所以在 `config/quark.yaml` 的 `agent` 段注入：

```yaml
agent:
  id: deepseek            # -> QK_AGENT_ID
  dsh_home: ""             # 留空则只注入 DSH_SESSION_ID（纯字符串，最省事）
  session_input: MiniMax-H3 创作台
```

### 第二道坎：后端在 WSL，而 Node 在 Windows

本机现状：**WSL(Ubuntu) 里没有 Linux node**（`which node` 空），Windows 侧有 v24。
所以后端走 **WSL interop** 调 `/mnt/c/Program Files/nodejs/node.exe`。
这条路上有两个必须处理的细节：

1. **环境变量不会自动过界。** WSL 起 Windows 进程时，只有登记在 `WSLENV` 里的变量才会传过去
   （flag `/w` 表示「从 WSL 调 Win32 时共享」）。实测：`WSLENV='DSH_SESSION_ID/w'` 才能让 CLI 认出来。
   后端在 `quark.py::QuarkCLI.build_env()` 里**追加**（不是覆盖）这几个变量到已有的 `WSLENV`。
2. **本地路径要翻成 Windows 形式。** `node.exe` 看到的是 `D:\...`，而仓库里一切路径都是 `/mnt/d/...`。
   所以：CLI 入口本身、`upload` 的位置参数、`download --output-dir` 都要翻（`util.wsl_to_windows`）；
   `--keyword` 之类的**文本**参数不能翻 —— `quark.py` 用显式的 `path_values` 区分这两类。
   `browse --all` 落盘的 `artifact` 路径也是 Windows 形式，读之前同样要翻（`server.py::_read_artifact`）。

后端同时支持两种 runner，`config/quark.yaml` 的 `runner` 可选 `auto`（默认）/ `windows` / `linux`：
哪天在 WSL 里装了 Node，`auto` 会自动改用 Linux node，路径翻译随之消失。

---

## 3. 那层 zip 是干什么的：防抽检，不是省体积

先说结论：**默认不重新编码，画质一点不损失**。上传的就是「原件的加密包」——
外面那层 zip 解决的不是体积问题，而是另外两件事：

1. **防抽检**：内容被加密成密文，网盘的自动内容识别扫不到里面是什么；
2. **统一密码**：官方文档写得很清楚 ——

   > 私密链接的提取码不由调用方指定，而是由服务端自动生成后通过 `data.passcode` 字段返回。

   调用方指定不了，那「统一密码 123456」就只能落在**压缩包**上。

（顺带一提：mp4 早就压过了，套 zip 用 deflate 也只能省 1% 左右 —— 所以默认 `level: 0`，
只打包不压缩，秒级完成。这也从侧面说明「压缩」在这里本来就不是为了体积。）

### 3.1 加密 zip（`pack.write_encrypted_zip`，默认开）

```yaml
archive:
  enabled: true
  password: "123456"
  level: 0          # 0=只打包（推荐，视频压不动）；要塞文档/图片再调到 6
  tool: auto        # auto | tar | python，见下
```

**两条实现路径，默认先试快的**：

| 路径 | 速度 | 说明 |
|---|---|---|
| **bsdtar**（快路径） | 实测 **74 MiB/s** | Windows 自带的 `C:\Windows\System32\tar.exe` 就是 libarchive 的 bsdtar 3.8，原生支持 `--format zip --options zip:encryption=zipcrypt,hdrcharset=UTF-8 --passphrase`。从 WSL 经 interop 调用（跟调 node 是同一套路子） |
| **纯 Python**（兜底） | 实测 **2.2 MiB/s** | 本模块自己实现的 ZipCrypto 写入器。逐字节的密钥流是串行的，Python 层没法向量化，所以慢 —— 只作兜底与自测参考 |

为什么两条都要有：WSL 里没有 `zip` / `7z`（`apt` 装要 sudo 密码，不该让一个前端功能依赖系统包），
而 Python 的 `zipfile` **能读** ZipCrypto、却**不能写**加密包。bsdtar 又不是每台机器都保证有，
所以纯 Python 那份得留着兜底。

> 顺带说明「其实也没那么要紧」：H3 的产物最长也就十几秒，实测 73~124 帧的成片是 **4~9.5 MB**，
> 纯 Python 那份跑完也就 2~5 秒（还在后台任务里）。所以快路径属于锦上添花 ——
> 嫌多一条外部依赖，把 `archive.tool` 设成 `python` 即可，行为完全等价。

**写完一律验一遍**：用标准库 `zipfile` 按密码解出第一个条目，校验字节不对就退回纯 Python 重写。
宁可慢，也不交付一个收件人打不开的包。

纯 Python 那份的两个实现细节（踩过才知道）：

1. **12 字节加密头的最后一个字节**：加密流的前 12 字节里，前 11 个随机，第 12 个是密码校验字节。
   APPNOTE / Info-ZIP / Python `zipfile` 的现行规则是：
   **通用标志位 bit3 未置位时用 CRC 的高字节**（置位时才用 DOS 时间高字节）。
   我们写的是 bit3 未置位 + 本地头里就有 CRC，所以校验字节取 `crc >> 24`。
2. **要读两遍源文件**：CRC 必须在写数据之前算出来（本地头里要写它），
   所以先扫一遍算 CRC/大小，再扫一遍加密写出。全程流式，内存占用与文件大小无关。

正确性怎么保证？`webui/tools/test_disk.py` **两条路径都测**：用标准库 zipfile 把包解回来逐字节比对、
验证包内中文文件名仍是 UTF-8 原文、错误密码必须被拒 —— 标准库能解开，7-Zip / WinRAR / Bandizip 也就能解开。

> **一个要知道的边界**：zip 格式本身不加密文件名，被加密的只有**内容**。
> 所以网盘那边仍然看得到一个 `xxx.mp4` 的文件名（看不到画面、也解不开内容）。
> 真要在文件名上也做文章，得换 7z 的 `-mhe`（加密头）—— 当前不引这个依赖。

### 3.2 重新编码（`pack.transcode`，默认关，有损）

想省流量/省网盘空间时才打开（`compress.enabled: true`，或页面上针对单次上传临时勾）。
它就是普通的有损转码：

```
ffmpeg -y -i in.mp4 -map 0:v:0 -map 0:a? \
  -c:v libx264 -preset veryfast -crf 26 -pix_fmt yuv420p \
  -vf "scale='min(1280,iw)':-2" -c:a aac -b:a 96k \
  -movflags +faststart out.mp4
```

- `scale='min(N,iw)':-2` **只缩不放**（原片本来就窄就保持原样），`-2` 保证高是偶数（yuv420p 要求）；
- `-map 0:a?` 的 `?` 让没有音轨的片子也能过；
- `-progress pipe:1` 输出机器可读的进度，页面上的百分比就是它；
- 实测：822 KiB 的 22 帧试片 → 45 KiB（这类测试片很静态，比例偏夸张）。

**画质优先就别开它** —— 这也是它默认关闭的原因。

### 3.3 一个已知取舍

- zip 里只有一个文件；文件 > 4 GiB 会明确报错（不写 Zip64 头），实际产物远小于这个量级。
- `share.url_type` 默认 **1（公开链接）**：链接拿到即可下载，密码统一在压缩包上，只有一层密码，
  和「密码统一为 123456」的诉求一致。想更保守就改成 `2`（私密链接），夸克会另给一个提取码，
  页面上会把两个都列出来。

---

## 4. 后端结构

```
config/quark.yaml           全部可调项（热读取，改完刷新页面即生效）
webui/backend/pack.py       压缩：ffmpeg 转码 + 纯标准库加密 zip（+ 自测用解密）
webui/backend/quark.py      CLI 封装（node 探测 / WSLENV / 路径翻译 / NDJSON 解析）+ 任务管理
webui/backend/server.py     /api/disk/* 路由（薄壳：参数校验 + 任务提交 + SSE 转发）
webui/web/                  页签 UI（index.html / app.js / app.css）
webui/tools/test_disk.py    离线自测：压缩包往返、转码、配置、CLI 环境、全部 HTTP 端点
webui/tools/test_disk_ui.js 前端静态自检：JS 里的 id 与 HTML 对账、按钮绑定、端点对账
```

### 任务模型

上传/下载都是长任务，**不能占着 HTTP 线程跑**。做法与生成作业（`jobs.py`）一致：

```
提交 -> DiskTask（后台线程）-> 事件（stage/progress/log/result/exit）-> SSE 推给页面
                              \-> task.json + events.jsonl 落盘（重启后还能看到上次的分享链接）
```

阶段：

- `publish`：`probe → transcode → archive → upload → share → done`
- `fetch`：`resolve → download → done`
- `login`：`login → done`

**并发**：单并发 + 排队。上传占的是上行带宽，跟生成抢网卡没意义；排队也更符合「一次发一个片子」的用法。
取消：给子进程发 `SIGINT`，不退出再 `SIGKILL` 整个进程组（Windows 进程经 interop 起，`os.killpg` 同样有效）。

### 失败分类

CLI 用负数 `code` 表达一切。后端把「未授权」单独拎出来（`code ∈ {-103,-104,-118,-1408}`
或 msg 含「未登录/未授权/未完成授权/授权已过期」）：

- HTTP 层返回 **401 + `need_login: true`**（而不是含混的 500），页面据此直接把「去授权」区域顶出来；
- 任务层把 `need_login` 标在任务卡上，并提示「授权完成后重新提交」。

---

## 5. 下载回本机：文件名怎么来的

官方文档里 `download` 那章的字段表在本版本包里缺失（`SKILL.md` 指向的章节没有内容），
所以这里**不去猜它的输出字段**，改用确定性做法：

1. 记下输出目录的快照（文件名 → 大小+mtime）；
2. 跑 `download --fid ... --output-dir ...`；
3. 再取一次快照，**差分出来的就是本次新增/变化的文件**。

这样即使 CLI 换了输出格式，下载依然可用；代价是同一秒内被覆盖写入的同名同大小文件可能漏判 ——
在「往干净目录里取素材」这个用法下不是问题。

---

## 6. 与 CLI 官方约束的对齐

Skill 文档里有几条硬约束，实现时刻意遵守了：

| 官方约束 | 这里怎么做的 |
|---|---|
| `upload --parent-fid` / `saveas --to-pdir-fid` 选填，用户没指定就**禁止**自动填 `"0"` | `upload.parent_fid` 默认为空字符串；空的时候不是「省略参数」，而是按 `upload.dir_name` **先建/复用一个同名目录**再用它的 FID 上传（见下） |
| `--session-id` 必须是 `{timestamp}-{random6}`，同一对话复用，禁止语义化命名 | 服务启动时生成一个（`QuarkCLI.new_session_id`），本次运行内所有命令复用 |
| 所有子命令必须带 `--session-input`（用户原始提问） | 页面提交时会带上对应的中文原始意图；缺省用 `config/quark.yaml` 的 `agent.session_input` |
| 未授权时先展示 `msg`，再引导 `login`，禁止重复重试原命令 | 401 的 body 就是 CLI 的 `msg` 原文，页面直接展示并给登录入口 |
| 禁止读 `quark-drive.cjs` 源码 | 只调用、不读；集成细节全部来自 `SKILL.md` + `references/` + 实测 |
| 升级 Skill 必须走 `scripts/install.sh`，不能用 `update` | 本集成不自动升级 CLI；要升级就在 `web_disk/` 下跑 `bash quarkclouddrive-1.0.20/scripts/install.sh` |

### 6.1 实测纠正：`upload` 的「默认目录」是空的

官方文档说 `--parent-fid` 选填、「不传时由 CLI 内部决定默认行为」。**实测不成立**：

```json
{"code":-204,"msg":"参数错误: [upload dir blank]","action":"upload","type":"result","data":{}}
```

不传目录参数时，SDK 解析出来的默认上传目录是空的，接口直接拒绝。所以 `upload.parent_fid` 留空时，
我们改成：**用 `upload.dir_name`（默认 `MiniMax-H3`）先 `create-folder` 拿到目录 FID，再用它上传**。
`create-folder` 对同名目录是幂等的（返回已存在的 FID），所以每次都复用同一个目录，
既不会在网盘里堆一堆同名文件夹，也**始终不碰根目录 `"0"`**（那是官方约束里明确不许自动填的）。
`dir_name` 也留空才会真的省略目录参数 —— 那条路会撞上上面这个报错，只留作对照排查。

真机跑通的形态（2026-09-17 实测）：

```
打包: 16.6 MiB → 16.6 MiB（原画质，未重新编码）| tool = bsdtar
上传: fid = 0dd6ae9f... | 17,419,218 B | 网盘目录 = 夸克网盘/MiniMax-H3
分享: https://pan.quark.cn/s/ba3154ff10e6（公开链接，无提取码）
总耗时: 43.8 s（16.6 MiB，上行带宽决定）
```

### 6.2 实测纠正：包内条目名不能沿用压缩包的名字

第一版把 `write_encrypted_zip` 的成员名写成了**压缩包自己的 basename**（`xxx.zip`），
结果是一个叫 `xxx.zip` 的包里装着另一个叫 `xxx.zip` 的东西（其实是 mp4）——
解压出来一脸问号。**这个 bug 是 bsdtar 快路径逼出来的**：

- bsdtar 是「`-C` 切到源目录，再按这个名字找文件」，名字对不上直接 `Couldn't visit directory`；
- 纯 Python 写入器按 `src` 读、按 `arc` 写，两者可以不一致，所以它**不会报错**
  —— 也就是说这个错在兜底路径上会一直静默地产出「名字错位」的包。

修法有三道：成员名一律用被放进去的那个文件的 basename；`_tar_write` 发现两者不一致就退兜底
（不砸快路径的缓存）；打包完成后 `zip_readable(..., expect=[...])` **连包内条目名一起核对**，
对不上就重写。测试里也钉住了这条断言（`test_disk.py` 的「包内条目名不是压缩包自己的名字」）。

---

## 7. 安全边界

- 后端**不接触 access_token**：授权状态存在 CLI 自己的配置目录里，页面只通过 `get-user-info` 问「现在是谁」。
  实测（别想当然）：那个目录是 **skill 目录下的 `<agent id>/`**，也就是仓库里的
  `web_disk/quarkclouddrive-1.0.20/deepseek/config.json`（含 `accessToken` / `refreshToken`）。
  **已在 `.gitignore` 里**，且 `git ls-files` 确认未被追踪 —— 这是接入时必须先钉死的一条。
- `/api/disk/*` 只能操作「上传仓库里的文件」和「把网盘文件下到配置好的目录」，
  不能用来读任意本地路径（`_resolve_source` 会拒绝指向网盘任务中间目录的路径）。
- 前端本来就没有鉴权（见 `webui/README.md` §7），所以这个页签**不要暴露到不可信网络**；
  它拿着你的网盘授权。要把 `server.host` 改成 `127.0.0.1`。
- 压缩包密码写在 `config/quark.yaml` 里，**不是密文**，页面也会明文显示它 —— 这是「统一密码」的代价，
  它本来就只管到「防止链接被顺手点开」这一层。

---

## 8. 怎么验证

```bash
# 离线全链路（不碰真实缓存、不真的上传）：压缩包往返 / 转码 / 配置 / CLI 环境 / 全部 HTTP 端点
python3 webui/tools/test_disk.py

# 前端静态自检（node）：id 对账 / 按钮绑定 / 端点对账
node webui/tools/test_disk_ui.js

# 只看环境探得准不准
node web_disk/quarkclouddrive-1.0.20/scripts/quark-drive.cjs --version   # Windows 侧直接跑
```

`test_disk.py` 会起一个**临时**实例（端口随机、cache 指向 `/tmp`）打全部 `/api/disk/*` 端点，
其中「上传」用 `dry_run=true` 只压缩打包、**不会真的往网盘传东西**；
登录路径用乱码授权码验证失败分支（不会弹浏览器）。

> 为什么一定要用临时 cache：真实的 `cache/webui/child_pids.txt` 里记着正在跑的生成作业 pid，
> `JobManager` 启动时会把这些 pid 当孤儿 `SIGKILL` 掉。测试碰真目录 = 杀掉正在跑的生成。

---

## 9. 已知限制

- **分享链接的提取码改不了**（服务端生成），所以「统一密码」只在压缩包上；页面会把两者分开列清楚。
- **ZipCrypto 的强度**：它挡的是「网盘的自动内容抽检」和「链接被路过的人顺手点开」——
  这两者都不会去暴力破解一个压缩包。但它**不是 AES**，对存心破解的人不设防
  （要换 AES 得引 `pyzipper` 或装 `p7zip`，当前刻意不引依赖）。
- **zip 的文件名是明文**（格式如此），被加密的只有内容；网盘那边仍看得到包内文件名。
- **单文件 zip，不支持 > 4 GiB**（不写 Zip64 头），超限时明确报错。
- **依赖 Windows 侧的 Node**：WSL 里没装 node 时靠 interop。若哪天 interop 被禁，
  在 WSL 里装个 node 即可（`runner: auto` 会自动切过去）。
- **上传/下载进度只有 CLI 给的粒度**（按字节汇总，或按文件），没有更细的分片进度。
