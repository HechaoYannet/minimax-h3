MiniMax-H3 · Ref2VA —— 离线镜像 U 盘
=====================================

这里装的是在 WSL 里跑通的 MiniMax-H3 Ref2VA 推理环境，已经固化成一个 Docker 镜像。
镜像的 tar 包超过 4 GB，而 FAT32 不允许单个文件超过 4 GiB，因此切成了若干分片。
在目标机器上运行 join-and-load.ps1 即可校验、合并、导入。

目录
----
  README.txt            本文件
  image\                镜像分片 + SHA256SUMS
  join-and-load.ps1     在目标机器上：校验 → 合并 → docker load
  source\               仓库源码快照（zip）+ 对应提交号

目标机器需要什么
----------------
  1. Windows + Docker Desktop（WSL2 后端），并且已经启动
     · 建议 Docker Desktop 25 及以上：镜像是 Docker 29 用 containerd 镜像存储导出的，
       层数据放在包内的 blobs/ 下（同一个包里既有 manifest.json 也有 OCI 索引），
       更老的 docker load 可能不认这个布局
  2. NVIDIA 显卡驱动。CUDA 运行时随镜像自带，宿主机只要有驱动即可；
     容器通过 "docker run --gpus all" 拿到显卡
  3. 可用磁盘空间 ≈ 镜像大小的两倍（分片和合并出来的 tar 会同时存在）
  4. 27 GB 的 NF4 权重（不在镜像里，见最后一节）

怎么用
------
  # 在 U 盘的这个目录下
  pwsh -File .\join-and-load.ps1

  # 空间紧张时指定工作目录（默认 %TEMP%）
  pwsh -File .\join-and-load.ps1 -WorkDir D:\h3

  # 导入之后
  docker run --rm -it --gpus all -v D:\h3-models:/models minimax-h3:2.1.7-cu132-py3.14 check

镜像里有什么
------------
  Python 3.14.7 + torch 2.14.0+cu132 + torchvision 0.29.0 / torchaudio 2.11.0
  DiffSynth-Studio 2.1.7（固定 commit 50e5efb，editable 安装）
  本仓库的 scripts/ 与 references/（位于 /workspace，可被挂载覆盖）
  FFmpeg 7.1 共享库（torchcodec 靠它解码音频）
  Ref2VA 的 processor/tokenizer（11 MiB，模型卷里没有它会自动兜底）

权重怎么办
----------
27 GB 的 NF4 权重不在镜像里，也不在 GitHub 上：
    minimax-h3-ref2va-pruned-nf4.safetensors              9.8 GB
    minimax-h3-text-encoder-nf4.safetensors                15 GB
    video_vae_nf4.safetensors                             1.6 GB
    audio_vae_nf4.safetensors                             271 MB
    AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors  1.2 GB

这几份是 NF4 量化版；ModelScope 上的官方仓库只有全精度权重，没有脚本能重新拉到
它们，只能从原机器拷贝。

注意：本 U 盘是 FAT32，**放不下**上面任何一个大文件（都超过 4 GB）。
要连权重一起带走，需要把 U 盘重新格式化成 exFAT 或 NTFS，或改用移动硬盘。

拷过去以后按这个结构摆放，再挂到 /models：
    <models>\minimax-h3-ref2va-pruned-nf4.safetensors
    <models>\minimax-h3-text-encoder-nf4.safetensors
    <models>\video_vae_nf4.safetensors
    <models>\audio_vae_nf4.safetensors
    <models>\AfterMidnight_ref2va_h3_softer_rank64_v1.safetensors
    <models>\MiniMax-H3\Ref2VA\processor\     （镜像里有兜底，可以缺）

更多说明见仓库里的 docker/README.md。
