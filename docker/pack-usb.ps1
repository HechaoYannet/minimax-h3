<#
.SYNOPSIS
  Export the image and put it on a USB stick, split into FAT32-sized pieces.

.DESCRIPTION
  docker save produces one enormous tar (well over 4 GiB) and FAT32 refuses any
  single file that big, so the tar is cut into numbered pieces by
  docker/split-for-usb.sh and reassembled on the far machine by join-and-load.ps1.
  A SHA256SUMS file rides along, plus a source snapshot and a README.

.EXAMPLE
  pwsh docker/pack-usb.ps1
  pwsh docker/pack-usb.ps1 -UsbRoot F:\ -PartMiB 3000
#>
[CmdletBinding()]
param(
    [string]$Tag = 'minimax-h3:2.1.7-cu132-py3.14',
    [string]$UsbRoot = 'E:\',
    [string]$Folder = 'minimax-h3',
    [int]$PartMiB = 3500,
    [string]$WorkDir,
    [string]$Distro = 'Ubuntu',
    [string]$WslUser = 'yhc',
    [switch]$KeepTar,
    [switch]$SkipSource
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $WorkDir) { $WorkDir = Join-Path $PSScriptRoot 'out' }

function ConvertTo-WslPath {
    param([string]$WindowsPath)
    $full = (Resolve-Path -LiteralPath $WindowsPath).Path
    $drive = $full.Substring(0, 1).ToLower()
    $rest = $full.Substring(2).Replace('\', '/')
    return "/mnt/$drive$rest"
}

# --- 1. the stick -------------------------------------------------------------
if (-not (Test-Path -LiteralPath $UsbRoot)) { throw "no such drive: $UsbRoot" }
$letter = $UsbRoot.TrimEnd('\', ':')
$vol = Get-Volume -DriveLetter $letter -ErrorAction SilentlyContinue
$freeGB = 0
if ($vol) {
    $freeGB = [math]::Round($vol.SizeRemaining / 1GB, 1)
    Write-Host "== usb ==" -ForegroundColor Cyan
    Write-Host "  $($letter): $($vol.FileSystemLabel) $($vol.FileSystem) $freeGB GB free"
    if ($vol.FileSystem -eq 'FAT32') {
        Write-Host "  FAT32: pieces capped at 4 GiB, splitting at $PartMiB MiB" -ForegroundColor DarkGray
    }
}

$dest = Join-Path $UsbRoot $Folder
$imageDir = Join-Path $dest 'image'
New-Item -ItemType Directory -Force -Path $imageDir | Out-Null

# --- 2. docker save -----------------------------------------------------------
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$tar = Join-Path $WorkDir 'h3-image.tar'
Write-Host "== docker save ==" -ForegroundColor Cyan
Write-Host "  $Tag -> $tar"
docker save -o $tar $Tag
if ($LASTEXITCODE -ne 0) { throw "docker save failed" }
$tarGB = [math]::Round((Get-Item -LiteralPath $tar).Length / 1GB, 2)
Write-Host "  $tarGB GB"

if ($vol -and ($vol.SizeRemaining / 1GB) -lt ($tarGB + 1)) {
    throw "not enough room on $($letter): need about $tarGB GB, $freeGB GB free"
}

# --- 3. split onto the stick --------------------------------------------------
Write-Host "== split ==" -ForegroundColor Cyan
$wslSplit = (ConvertTo-WslPath (Join-Path $PSScriptRoot 'split-for-usb.sh'))
$wslTar = ConvertTo-WslPath $tar
$wslDest = ConvertTo-WslPath $imageDir
wsl.exe -d $Distro -u $WslUser -- bash $wslSplit $wslTar $wslDest "$($PartMiB)M"
if ($LASTEXITCODE -ne 0) { throw "split failed" }

# --- 4. the pieces that make it usable on the far side ------------------------
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'join-and-load.ps1') -Destination (Join-Path $dest 'join-and-load.ps1') -Force
$readmeSrc = Join-Path $PSScriptRoot 'usb-readme.txt'
$readme = Join-Path $dest 'README.txt'
$utf8Bom = New-Object System.Text.UTF8Encoding $true
[System.IO.File]::WriteAllText($readme, (Get-Content -LiteralPath $readmeSrc -Raw -Encoding UTF8), $utf8Bom)

$parts = @(Get-ChildItem -Path $imageDir -Filter 'image.tar.part-*')
$stamp = @(
    '',
    '本次导出 / this export',
    '-----------------------',
    "  镜像 tag      : $Tag",
    "  导出时间      : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "  镜像大小      : $tarGB GB",
    "  分片          : $($parts.Count) 个 x $PartMiB MiB",
    "  合并所需空间  : 约 $tarGB GB（分片本身还要占同样的空间）"
)
Add-Content -LiteralPath $readme -Value ($stamp -join "`r`n") -Encoding UTF8

# --- 5. an offline copy of the source ----------------------------------------
if (-not $SkipSource) {
    $srcDir = Join-Path $dest 'source'
    New-Item -ItemType Directory -Force -Path $srcDir | Out-Null
    $zip = Join-Path $srcDir 'minimax-h3-src.zip'
    Push-Location $repo
    try {
        git archive --format=zip -o $zip HEAD
        git log -1 --format='%H%n%ad%n%s' | Set-Content -LiteralPath (Join-Path $srcDir 'COMMIT.txt') -Encoding ASCII
    } finally { Pop-Location }
    Write-Host "== source snapshot ==" -ForegroundColor Cyan
    Write-Host "  $zip"
}

if (-not $KeepTar) { Remove-Item -LiteralPath $tar -Force }

# --- 6. summary ---------------------------------------------------------------
$totalGB = [math]::Round(((Get-ChildItem -Path $dest -Recurse -File | Measure-Object Length -Sum).Sum) / 1GB, 2)
Write-Host ""
Write-Host "== done ==" -ForegroundColor Green
Write-Host "  $dest  ($totalGB GB)"
Write-Host "  on the target machine:  pwsh -File .\join-and-load.ps1"
