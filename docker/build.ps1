<#
.SYNOPSIS
  Build the MiniMax-H3 Ref2VA image with Windows-side Docker Desktop.

.DESCRIPTION
  Docker only exists on the Windows side on this box, so the build is driven from
  here.  Three steps:

    1. stage the build context in WSL  (docker/prepare-context.sh)
       -- DiffSynth-Studio at the pinned commit + the processor, both of which a
          container cannot fetch: GitHub is unreachable from the build sandbox.
    2. docker build with the context rooted at the repository
    3. report the result and the command to run it

.EXAMPLE
  pwsh docker/build.ps1
  pwsh docker/build.ps1 -Tag minimax-h3:dev -NoCache
#>
[CmdletBinding()]
param(
    [string]$Tag = 'minimax-h3:2.1.7-cu132-py3.14',
    [string]$BaseRegistry = 'docker.m.daocloud.io/library',
    [string]$AptMirror = 'mirrors.ustc.edu.cn',
    [string]$Distro = 'Ubuntu',
    [string]$WslUser = 'yhc',
    [switch]$SkipPrepare,
    [switch]$NoCache
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function ConvertTo-WslPath {
    param([string]$WindowsPath)
    $full = (Resolve-Path -LiteralPath $WindowsPath).Path
    $drive = $full.Substring(0, 1).ToLower()
    $rest = $full.Substring(2).Replace('\', '/')
    return "/mnt/$drive$rest"
}

Write-Host "== docker ==" -ForegroundColor Cyan
$server = docker version --format '{{.Server.Version}}' 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "docker engine not reachable - is Docker Desktop running? ($server)"
}
Write-Host "  engine $server"

if (-not $SkipPrepare) {
    Write-Host "== build context (WSL) ==" -ForegroundColor Cyan
    $wslScript = (ConvertTo-WslPath $PSScriptRoot) + '/prepare-context.sh'
    wsl.exe -d $Distro -u $WslUser -- bash $wslScript
    if ($LASTEXITCODE -ne 0) { throw "prepare-context.sh failed" }
}

$artifacts = @('docker/vendor/conda-env.tar', 'docker/vendor/diffsynth.tar', 'docker/vendor/processor')
foreach ($f in $artifacts) {
    $p = Join-Path $repo $f
    if (-not (Test-Path -LiteralPath $p)) {
        throw "missing build-context artifact: $p (run without -SkipPrepare)"
    }
}
$ctxGB = [math]::Round(((Get-ChildItem -Path (Join-Path $repo 'docker/vendor') -Recurse -File |
    Measure-Object Length -Sum).Sum) / 1GB, 2)
Write-Host "  build context: $ctxGB GB (mostly the conda env tar, sent on every build)" -ForegroundColor DarkGray

Write-Host "== docker build ==" -ForegroundColor Cyan
$buildArgs = @(
    'build',
    '--file', (Join-Path $repo 'docker/Dockerfile'),
    '--tag', $Tag,
    '--build-arg', "BASE_REGISTRY=$BaseRegistry",
    '--build-arg', "APT_MIRROR=$AptMirror"
)
if ($NoCache) { $buildArgs += '--no-cache' }
$buildArgs += $repo

Write-Host "  docker $($buildArgs -join ' ')"
& docker @buildArgs
if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

Write-Host "== result ==" -ForegroundColor Cyan
docker images $Tag --format '  {{.Repository}}:{{.Tag}}  {{.Size}}  (created {{.CreatedSince}})'
Write-Host ""
Write-Host "smoke test (needs the weights):" -ForegroundColor Yellow
Write-Host "  pwsh docker/run.ps1 check"
Write-Host "or, without weights, just the GPU + audio stack:"
Write-Host "  pwsh docker/run.ps1 python -c `"import torch, torchaudio, torchcodec; print(torch.__version__, torch.cuda.get_device_name(0), torchaudio.__version__)`""
