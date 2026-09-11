# Docker 封装：把 WSL 侧那套环境固化成一个镜像

这台机器上 Docker 只装在 **Windows 侧**（Docker Desktop）。WSL 里能看到一个 `docker`
命令，那是 Docker Desktop 转发出来的壳，而本发行版并没有开 WSL 集成：

```
$ docker --version
The command 'docker' could not be found in this WSL 2 distro.
We recommend to activate the WSL integration in Docker Desktop settings.
```

所以整套东西是**在 Windows 侧构建、Windows 侧运行**的，WSL 只负责提供构建上下文
（本仓库就在 `D:\` 上，WSL 里是 `/mnt/d/`，**同一份文件**）。

---

## 1. 设计时撞到的硬约束（都实测过）

| 约束 | 实测现象 | 处理方式 |
|---|---|---|
| **Docker Hub 不可达** | `docker pull alpine:3.20` → `dial tcp 103.42.176.244:443: connectex: No connection could be made` | 基础镜像走 `docker.m.daocloud.io` 镜像站（实测可达）。`BASE_REGISTRY` 是可覆盖的 ARG |
| **Debian 官方源极慢** | 容器内 13 MB 的 `Packages.gz`：deb.debian.org **0.16 MB/s**、nju 0.27、aliyun 0.60、腾讯 2.55、**USTC 2.96 MB/s** | `APT_MIRROR` 默认 `mirrors.ustc.edu.cn`（装 ffmpeg 那一堆包从"十几分钟"变成"一分钟"） |
| **PyPI / PyTorch 源极慢** | 容器内 20 MB 实测：files.pythonhosted.org **0.13 MB/s**、aliyun pypi 0.47 MB/s、download.pytorch.org ~0.9 MB/s（WSL 侧同量级） | **不从网上重建环境**：conda env 直接打成 tar 进镜像。torch + CUDA + triton 一共 ~6 GB 的 wheel，按这个速度要几个小时 |
| **构建沙箱里 GitHub 不可达** | 容器内 `github.com` SSL 握手超时，而 pypi / pytorch / modelscope 都是 200 | DiffSynth-Studio 以 tar 形式进构建上下文（`prepare-context.sh` 在 WSL 侧生成） |
| **Docker Desktop 挂不了 WSL 里的路径** | `-v \\\\wsl.localhost\\Ubuntu\\...` → `accessing specified distro mount service: stat /run/guest-services/distro-services/ubuntu.sock` | 权重从 WSL 拷一份到 `D:\otherProject\minimax-h3\models`（`stage-models.sh`，27 GB，一次），容器挂这一份 |
| **U 盘是 FAT32** | `E:` 57.7 GB，但**单文件上限 4 GiB**，而镜像 tar 远大于它 | `pack-usb.ps1` 切成 3.5 GB 分片 + `SHA256SUMS`，目标机器用 `join-and-load.ps1` 合并 |

第 3 条决定了整个镜像的做法：**这是一个"拷贝"，不是一个"重建"** ——
把 WSL 里那个已经跑通的环境原样打包，而不是在容器里重新 `pip install` 一遍。

---

## 2. 镜像里的路径就是 WSL 的路径

镜像**故意复刻 WSL 的绝对路径**，这样一个字都不用改：

| 镜像内路径 | 是什么 |
|---|---|
| `/home/yhc/miniconda3/envs/diffsynth` | conda 环境原样（`ADD conda-env.tar` 解包） |
| `/home/yhc/source/minimax-h3/DiffSynth-Studio` | 固定 commit `50e5efbc` 的源码 |
| `/opt/h3/processor` | processor/tokenizer 兜底副本（11 MiB） |
| `/workspace` | 本仓库（`run_h3.sh` / `env/` / `scripts/` / `references/` / `cache/*.json`） |
| `/models` | 挂载点：27 GB 权重（**不在镜像里**） |

为什么路径必须一样：conda 环境里有**几千条绝对路径**——`bin/*` 的 shebang、
`__editable__.diffsynth-2.1.7.pth` 指向的源码目录、`conda-meta` 记录。路径一致时这些
全部原样可用；一旦换到 `/opt/conda` 之类的位置，就得逐个去修。

只有两个路径"搬家"，因为它们是挂载点：

| 变量 | WSL | 容器 |
|---|---|---|
| `H3_ROOT` | `/home/yhc/source/minimax-h3` | 同左（不变） |
| `H3_REPO` | `$H3_ROOT/DiffSynth-Studio` | 同左（不变） |
| `H3_MODELS` | `$H3_ROOT/models` | `/models` |
| `H3_WORKSPACE` | `/mnt/d/otherProject/minimax-h3` | `/workspace` |

为此 `env/h3_env.sh` 改了两处（WSL 上行为完全不变）：

1. 所有路径写成 `\${VAR:-默认值}`，容器的 entrypoint 先导出、这里就不再覆盖；
2. conda 激活加了 `[ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]` 判定 —— 容器里没有
   跑的 conda（环境是"死"的，直接进 PATH），镜像的系统 python 由 PATH 顶上。

---

## 3. 目录

```
docker/Dockerfile                 镜像定义（唯一的构建入口）
docker/entrypoint.sh              容器侧环境变量 + run_h3.sh 动词分发
docker/prepare-context.sh         WSL 侧：生成 vendor/（conda 环境 tar、源码 tar、processor）
docker/stage-models.sh            WSL 侧：把 27 GB 权重拷到 Windows 可见路径
docker/build.ps1                  Windows 侧：准备上下文 + docker build
docker/run.ps1                    Windows 侧：docker run（GPU + 权重 + 工作区）
docker/pack-usb.ps1               Windows 侧：docker save + 切片 + 校验和 + 源码快照 → U 盘
docker/join-and-load.ps1          目标机器：校验分片 → 合并 → docker load
docker/split-for-usb.sh           切片实现（pack-usb.ps1 调用，也可单独用）
docker/usb-readme.txt             拷进 U 盘的说明（中文，带 BOM）
docker/requirements.lock          conda 环境 `pip freeze` 的 109 行清单（备用重建路线，见 §9）
docker/requirements.optional.txt  可选依赖（deepspeed / cuda-toolkit），默认不装
docker/vendor/                    构建上下文产物（git 忽略，prepare-context.sh 重建）
docker/netprobe.py                网络实测脚本（就是 §1 那些数字的来源）
```

---

## 4. 构建

```powershell
pwsh docker/build.ps1
```

三步：WSL 里生成 `docker/vendor/`（≈7 GB，第一次约 5~10 分钟）→ `docker build` → 打印镜像
与冒烟命令。

常用开关：

```powershell
pwsh docker/build.ps1 -SkipPrepare            # vendor/ 已就绪
pwsh docker/build.ps1 -NoCache                # 完全重来
pwsh docker/build.ps1 -Tag minimax-h3:dev
pwsh docker/build.ps1 -BaseRegistry docker.io/library          # 有 Docker Hub 直连时
pwsh docker/build.ps1 -AptMirror mirrors.cloud.tencent.com     # USTC 挂了时换一个
```

注意：**每次构建都会把 ~7 GB 的上下文送进 daemon**（`ADD` 那个 conda tar），
这是正常的，不是卡住了。改了 `scripts/` 也要重建时才需要重走一遍。

---

## 5. 怎么跑

```powershell
pwsh docker/run.ps1 check          # == ./run_h3.sh check
pwsh docker/run.ps1                # 交互式 shell
pwsh docker/run.ps1 dry --preset draft --prompt-file workspace/xxx/prompt.txt
pwsh docker/run.ps1 -NoTty nvidia-smi -L
```

`run.ps1` 接三样东西：

| 宿主 | 容器 | 说明 |
|---|---|---|
| `D:\otherProject\minimax-h3` | `/workspace` | 脚本、素材、输出、`cache/plan.json`。挂上去就覆盖镜像里那份，改脚本不用重建 |
| `D:\otherProject\minimax-h3\models` | `/models` | 27 GB 权重 + processor |
| 命名卷 `h3-hf-cache` | `/root/.cache` | modelscope 下载缓存 |

不经过封装脚本：

```powershell
docker run --rm -it --gpus all --shm-size 2g `
  -v D:\otherProject\minimax-h3:/workspace `
  -v D:\otherProject\minimax-h3\models:/models `
  minimax-h3:2.1.7-cu132-py3.14 check
```

---

## 6. 镜像里有什么

| 组件 | 版本 / 来源 |
|---|---|
| 基础镜像 | `python:3.14-slim`（Debian 13 trixie），经 daocloud 镜像站拉取 |
| Python | **3.14.7**，来自 conda 环境（基础镜像自带的那个只是凑个 userland，PATH 里排在后面） |
| torch / torchvision / torchaudio | 2.14.0+cu132 / 0.29.0+cu132 / 2.11.0+cu132 |
| 其他依赖 | conda 环境里那 109 个包，一个不少（含 bitsandbytes 0.50.2、torchao、torchcodec、triton、nvidia-* 3.1 GB） |
| DiffSynth-Studio | 2.1.7，固定 commit `50e5efbc`，editable 安装（路径与 WSL 一致，所以直接可用） |
| FFmpeg | 发行版 7.1（`libavutil.so.59`）。torchcodec 0.16 自带 core4~core9 六个变体，对应 libavutil 56~61，导入时按现有 FFmpeg 挑一个，所以不用手编 |
| 工作流 | `/workspace` |
| processor | `/opt/h3/processor` |

构建的最后一步会真的 import 一遍 `torch / torchvision / torchaudio / torchcodec /
bitsandbytes / transformers / diffsynth / MiniMaxH3Pipeline`，导入不过构建就失败 ——
这样"镜像坏了"不会拖到运行时才发现。

---

## 7. 权重：必须单独搬

| 文件 | 大小 |
|---|---|
| `minimax-h3-ref2va-pruned-nf4.safetensors` | 9.8 GB |
| `minimax-h3-text-encoder-nf4.safetensors` | 15 GB |
| `video_vae_nf4.safetensors` | 1.6 GB |
| `audio_vae_nf4.safetensors` | 271 MB |
| `AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors` | 1.2 GB |

ModelScope 上的 `MiniMax/MiniMax-H3` 只有**全精度**权重（transformer 13 分片 × 5 GB、
text encoder 14 分片 × 4.6 GB），没有这几份 NF4 —— **没有脚本能重新拉到它们**，只能从原机器拷。
这也是"一个 U 盘装不下整套"的根本原因（见 §8）。

唯一可再生的是 processor/tokenizer（11 MiB）：`run_h3.sh fetch`，容器里同理
（`docker run ... <tag> fetch`），走 ModelScope。

---

## 8. 拷贝到 U 盘

```powershell
pwsh docker/pack-usb.ps1                 # 默认 E:\，切成 3500 MiB 分片
pwsh docker/pack-usb.ps1 -UsbRoot F:\ -PartMiB 3000
```

U 盘上的结构：

```
E:\minimax-h3\
  README.txt              中文说明（docker/usb-readme.txt + 本次导出的元信息）
  join-and-load.ps1       目标机器上跑这个
  image\h3-image.tar.part-00 …  SHA256SUMS
  source\minimax-h3-src.zip + COMMIT.txt
```

目标机器：

```powershell
pwsh -File .\join-and-load.ps1                       # 校验 → 合并 → docker load
pwsh -File .\join-and-load.ps1 -WorkDir D:\h3       # 指定合并的工作目录
```

要点：

- **FAT32 单文件 4 GiB 上限**是切片的原因，不是镜像坏了；合并出来还是同一个 tar。
- 合并需要 **≈ 镜像大小 × 2** 的空闲空间（分片 + 合并结果同时存在）。
- 权重搬不过去：U 盘上**放不下任何一个 >4 GB 的文件**。要连权重一起带走，得把 U 盘格成
  exFAT/NTFS，或改用移动硬盘。U 盘上的 `README.txt` 也写了这一条。

---

## 9. 排查与维护

| 现象 | 原因 | 处理 |
|---|---|---|
| `failed to resolve reference "docker.io/..."` | Docker Hub 不可达 | 用默认的 `-BaseRegistry docker.m.daocloud.io/library` |
| `docker build` 在 `apt-get install` 卡住 | Debian 官方源在这里只有 0.16 MB/s | `-AptMirror mirrors.ustc.edu.cn`（默认就是它） |
| `accessing specified distro mount service` | 想挂 WSL 里的路径 | 先 `stage-models.sh` 拷到 `D:\`，再挂 Windows 路径 |
| 容器里 `torch.cuda.is_available()` 为 False | 没加 `--gpus all`，或宿主驱动太旧 | `docker run --gpus all ... nvidia-smi -L` 自检 |
| `bash\r: No such file or directory` | `.sh` 被 CRLF 化了 | 仓库有 `.gitattributes` 锁 `eol=lf`；Dockerfile 里还有一道 `sed` |
| `Could not load libtorchcodec` | 镜像里 ffmpeg 没装上 | `docker run --rm <tag> ldconfig -p \| grep libav` 应有 `libavutil.so.59` |
| 分片校验失败 | U 盘拷贝坏块 | 重新拷那一个分片；`-SkipVerify` 只用于救急 |
| `check` 之后 `git status` 多了 `cache/plan.json` | `h3_audit` 重写了**描述性**的 `path` 字段（容器路径 vs WSL 路径） | 无害：`h3_generate.py` 与 `h3_validate.py` 都不读它，模型路径一律由 `H3_MODELS` 现算。`git checkout -- cache/plan.json` 还原 |
| `docker run <tag> bash -c "..."` 没执行 | 早期版本的 entrypoint 对裸 `bash` 直接 `exec bash`，吃掉了后面的参数 | 已修：除 run_h3.sh 动词外的参数一律 `exec "$@"` |

不需要权重就能跑的冒烟测试（`docker/smoke.py` 随仓库挂进 `/workspace`）：

```powershell
pwsh docker/run.ps1 -NoTty python docker/smoke.py
```

它检查 GPU 直通、torchaudio、ffmpeg 共享库这三件最容易在容器里坏掉的事。

**改依赖**：在 WSL 的 conda 环境里改完之后，删掉 `docker/vendor/conda-env.tar` 重新
`build.ps1`（`prepare-context.sh` 会比较 tar 与 `conda-meta/history` 的时间戳，
环境更新过就会自动重打）。

**换 DiffSynth commit**：`DIFFSYNTH_PIN=<sha> bash docker/prepare-context.sh`（先删掉
`vendor/diffsynth.tar`），再重建。

**改了工作流脚本**：不用重建镜像 —— `run.ps1` 把整个仓库挂到 `/workspace`。

**想在一台网络好的机器上从零重建**（不依赖 conda 环境的 tar）：基础镜像换成
`python:3.14-slim`，依次

```bash
pip install --index-url https://download.pytorch.org/whl/test/cu132 \
    torch==2.14.0+cu132 torchvision==0.29.0+cu132 torchaudio==2.11.0+cu132
pip install -r docker/requirements.lock          # 109 行，见文件头部的排除说明
pip install -e /opt/h3/DiffSynth-Studio --no-deps # 源码用 vendor/diffsynth.tar
```

注意 torchaudio 只在 **test** 频道有 cu132 版，稳定频道没有（主 README §3.1）。
这条路线**没在本机验证过** —— 本网络到 PyPI 只有 0.13 MB/s，跑不完。
