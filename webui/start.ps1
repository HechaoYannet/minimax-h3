<#
.SYNOPSIS
  MiniMax-H3 创作台 · Windows 侧启动器。

.DESCRIPTION
  它只做 Windows 该做的事：
    1. 找一个没被占用的端口（默认取 config/server.yaml 的 server.port）；
    2. 探测 WSL 里的后端是否已经在跑（/api/health）；
    3. 就绪就把浏览器打开；没就绪就把「在 WSL 里该敲的那条命令」原样打出来。

  为什么不在这个脚本里直接调 wsl.exe 拉起后端：
  当前会话里对外的 wsl.exe 调用会被环境拒绝（Wsl/Service/E_ACCESSDENIED），
  半启动状态反而会留下一个连不上的空端口，比直接报错更难查。
  所以真正的启动动作交给 WSL 终端里的那条命令（或 -InWsl 分支），
  Windows 侧只负责端口与浏览器 —— 两边都不猜对方的状态。

.PARAMETER Port
  端口；0（默认）= 读 config/server.yaml。

.PARAMETER Url
  直接指定要打开的地址（比如 WSL 不是 Mirror 网络时的 http://<wsl-ip>:8765/）。

.PARAMETER Check
  只探测，不打开浏览器；探测失败退出码为 1（方便脚本化）。

.PARAMETER NoBrowser
  只探测与打印，不打开浏览器。

.PARAMETER InWsl
  在 WSL 的 bash 里调用本脚本时用这个：直接拉起后端并前台运行。

.EXAMPLE
  pwsh webui/start.ps1                 # 已就绪直接开页面，否则打印启动命令
  pwsh webui/start.ps1 -Port 8899
  pwsh webui/start.ps1 -Check
  pwsh webui/start.ps1 -InWsl          # 在 WSL 里一键起后端
#>
[CmdletBinding()]
param(
  [int]$Port = 0,
  [string]$Url = "",
  [switch]$Check,
  [switch]$NoBrowser,
  [switch]$InWsl
)

$ErrorActionPreference = "Continue"
$repoWin = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$drive = $repoWin.Substring(0,1).ToLower()
$repoWsl = "/mnt/" + $drive + $repoWin.Substring(2).Replace("\","/")

function Read-ConfigPort {
  $cfg = Join-Path $repoWin "config\server.yaml"
  if (Test-Path $cfg) {
    $m = Select-String -Path $cfg -Pattern "^\s*port:\s*(\d+)" | Select-Object -First 1
    if ($m) { return [int]$m.Matches[0].Groups[1].Value }
  }
  return 8765
}

function Test-Backend([int]$p) {
  try {
    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/api/health" -f $p) -TimeoutSec 3 -UseBasicParsing
    if ($r.StatusCode -eq 200) { return ($r.Content | ConvertFrom-Json) }
  } catch { }
  return $null
}

if ($Port -eq 0) { $Port = Read-ConfigPort }

if ($InWsl) {
  # 从 WSL 的 bash 里跑：直接起后端（这个分支不碰 wsl.exe）
  $inner = "cd " + $repoWsl + " && mkdir -p cache/webui && PY=$(command -v python3 || echo ~/miniconda3/envs/diffsynth/bin/python) && $PY webui/serve.py --host 0.0.0.0 --port $Port"
  Write-Host "[webui] 正在启动后端（Ctrl+C 停止）…" -ForegroundColor Cyan
  bash -lc $inner
  exit $LASTEXITCODE
}

$startCmd = "cd " + $repoWsl + " && python3 webui/serve.py --host 0.0.0.0 --port $Port"
$health = Test-Backend $Port

if (-not $health) {
  $busy = $false
  try { $busy = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop) } catch { }
  Write-Host "[webui] 端口 $Port 上的后端没有响应。" -ForegroundColor Yellow
  if ($busy) {
    Write-Host ("[webui] 注意：该端口已被别的进程占用（后端八成不是它）。换个端口：pwsh webui/start.ps1 -Port " + ($Port + 1)) -ForegroundColor Yellow
  }
  Write-Host ""
  Write-Host "在 WSL(Ubuntu) 终端里执行这一条就能起来：" -ForegroundColor Cyan
  Write-Host ""
  Write-Host "    $startCmd" -ForegroundColor White
  Write-Host ""
  Write-Host "起来之后再跑一次本脚本即可打开页面；期间那个终端要留着（关掉就停服）。" -ForegroundColor DarkGray
  Write-Host "想后台常驻：在该命令后加   > cache/webui/server.log 2>&1 &" -ForegroundColor DarkGray
  Write-Host "停止服务：  pkill -f webui/serve.py" -ForegroundColor DarkGray
  if ($Check) { exit 1 }
  exit 0
}

$okUrl = if ($Url) { $Url } else { "http://127.0.0.1:$Port/" }
Write-Host "[webui] 后端在线：$($health.root)" -ForegroundColor Green
$bad = @($health.checks | Where-Object { -not $_.ok })
if ($bad.Count) {
  Write-Host "[webui] 有 $($bad.Count) 项自检未通过：" -ForegroundColor Yellow
  $bad | ForEach-Object { Write-Host ("   - " + $_.name + "：" + $_.detail) -ForegroundColor DarkYellow }
}
Write-Host "[webui] 页面：$okUrl" -ForegroundColor Green
if (-not $Check -and -not $NoBrowser) { Start-Process $okUrl }
