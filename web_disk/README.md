# web_disk/ · 夸克网盘 CLI（官方 Skill 包）

这个目录是**第三方工具**，不是本工程写的代码，只是为了「网络慢时把片子从网盘取走」
这个功能才放进来。集成方式见 [`../docs/QUARK.md`](../docs/QUARK.md)，配置在 `config/quark.yaml`。

| 路径 | 说明 |
|---|---|
| `quarkclouddrive-1.0.20.zip` | 官方发的 Skill 包原件（**源头**，保留不动，升级时用它比对） |
| `quarkclouddrive-1.0.20/` | 解压出来的工作副本 —— `config/quark.yaml` 的 `cli` 指的就是这里的 `scripts/quark-drive.cjs` |

## 为什么解压出来放在版本库里

因为后端是**照着路径调它**的：`config/quark.yaml` 的 `cli` 指向
`web_disk/quarkclouddrive-1.0.20/scripts/quark-drive.cjs`。留着解压后的目录，
克隆下来就能跑，不需要额外一步「先解压」。代价是和 zip 有内容重复（约 700 KB）。

## 命令速查（排障用）

在 Windows 侧直接跑（WSL 里没有 Linux node，靠 interop 调 `node.exe`）：

```bash
cd /mnt/d/otherProject/minimax-h3
node.exe web_disk/quarkclouddrive-1.0.20/scripts/quark-drive.cjs --help
```

CLI 会先判断「自己是不是在被支持的 agent 环境里跑」。**直接这么跑会得到**：

```json
{"code":-104,"msg":"无法识别当前 Agent 环境，禁止继续使用","action":"runtime","type":"result","data":{}}
```

带上 `DSH_SESSION_ID`（任意字符串）就好了 —— 后端就是这么做的：

```bash
DSH_SESSION_ID=manual-debug DSH_HOME='C:\Users\<你>\.dsh' \
  WSLENV='DSH_SESSION_ID/w:DSH_HOME/w' \
  node.exe web_disk/quarkclouddrive-1.0.20/scripts/quark-drive.cjs get-user-info
```

看「现在授权的是谁」用 `get-user-info`；没授权会返回 `-103 未登录，请先执行 login 命令完成登录授权`。

## 升级

升级**必须**走官方脚本（它会把 CLI 与 `SKILL.md`/`references/` 一起覆盖，
而 `quark-drive.cjs update` 只换 CLI 本体）：

```bash
cd web_disk/quarkclouddrive-1.0.20 && bash scripts/install.sh
```

升级后 `config/quark.yaml` 一般不用改（命令契约是稳定的）；但如果新版改了出参字段，
`backend/quark.py` 的解析可能要跟着调 —— 改完跑 `python3 webui/tools/test_disk.py` 回归。

## CLI 把自己的配置放在哪

CLI 会在**自己所在目录下**建一个以 agent 渠道命名的配置目录（本项目注入 `QK_AGENT_ID=deepseek`，所以是 `deepseek/`）：

```
web_disk/quarkclouddrive-1.0.20/deepseek/
  config.json     设备标识 / 平台；**授权后这里还会写入 access_token**
  storage/        任务记录等运行数据
```

**这个目录已经在 `.gitignore` 里**（里面有 token，不能进版本库）。
它跟着 CLI 目录走，不是 cwd、也不是用户主目录 —— 所以不管从哪里调用，位置都是确定的。

## 两条自律

- **不读 `scripts/quark-drive.cjs` 的源码**：它是打包产物，Skill 明确禁止读；
  集成用的所有字段都来自 `SKILL.md` + `references/*.md` + 实测。
- **不碰它的授权数据**：access_token 由 CLI 自己维护，本工程既不读也不写。
