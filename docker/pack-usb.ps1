<#
.SYNOPSIS
  Export the image and put it on a USB stick, split into FAT32-sized pieces.

.DESCRIPTION
  docker save produces one big tar (this image: 3.6 GB) and FAT32 refuses any
  single file over 4 GiB, so the tar is cut into numbered pieces here and
  reassembled on the far machine by join-and-load.ps1.  SHA256SUMS is computed
  while writing -- no second pass over the stick -- plus a source snapshot and a
  README.

  The split happens in PowerShell rather than in WSL on purpose: a USB stick
  plugged in after WSL started is not mounted at /mnt/<letter>, so the WSL route
  needs a sudo mount first, and this has to work from a plain Windows session.

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
    [switch]$KeepTar,
    [switch]$SkipSource
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $WorkDir) { $WorkDir = Join-Path $PSScriptRoot 'out' }

function Complete-Part {
    param($Stream, $Hasher, $List, $Name)
    $null = $Hasher.TransformFinalBlock((New-Object byte[] 0), 0, 0)
    $hex = ([BitConverter]::ToString($Hasher.Hash) -replace '-', '').ToLower()
    $List.Add([pscustomobject]@{ Name = $Name; Hash = $hex })
    $Stream.Dispose()
    $Hasher.Dispose()
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
Get-ChildItem -Path $imageDir -Filter 'image.tar.part-*' -ErrorAction SilentlyContinue | Remove-Item -Force

# --- 2. docker save -----------------------------------------------------------
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$tar = Join-Path $WorkDir 'h3-image.tar'
if (Test-Path -LiteralPath $tar) { Remove-Item -LiteralPath $tar -Force }
Write-Host "== docker save ==" -ForegroundColor Cyan
Write-Host "  $Tag -> $tar"
docker save -o $tar $Tag
if ($LASTEXITCODE -ne 0) { throw "docker save failed" }
$tarGB = [math]::Round((Get-Item -LiteralPath $tar).Length / 1GB, 2)
Write-Host "  $tarGB GB"

if ($vol -and ($vol.SizeRemaining / 1GB) -lt ($tarGB + 1)) {
    throw "not enough room on $($letter): need about $tarGB GB, $freeGB GB free"
}

# --- 3. split onto the stick, hashing as we go --------------------------------
Write-Host "== split ==" -ForegroundColor Cyan
$partBytes = [int64]$PartMiB * 1MB
$buffer = New-Object byte[] (8MB)
$input = [System.IO.File]::OpenRead($tar)
$out = $null
$crypto = $null
$written = 0
$index = 0
$parts = New-Object System.Collections.Generic.List[object]
try {
    while (($read = $input.Read($buffer, 0, $buffer.Length)) -gt 0) {
        if ($null -eq $out -or $written -ge $partBytes) {
            if ($null -ne $out) { Complete-Part $out $crypto $parts (Split-Path -Leaf $out.Name) }
            $partName = 'image.tar.part-{0:D2}' -f $index
            $index++
            Write-Host "  $partName"
            $out = [System.IO.File]::Create((Join-Path $imageDir $partName))
            $crypto = [System.Security.Cryptography.SHA256]::Create()
            $written = 0
        }
        $out.Write($buffer, 0, $read)
        $null = $crypto.TransformBlock($buffer, 0, $read, $null, 0)
        $written += $read
    }
    if ($null -ne $out) { Complete-Part $out $crypto $parts (Split-Path -Leaf $out.Name) }
} finally {
    if ($null -ne $out) { $out.Dispose() }
    $input.Dispose()
}

$sumFile = Join-Path $imageDir 'SHA256SUMS'
($parts | ForEach-Object { "$($_.Hash)  $($_.Name)" }) | Set-Content -LiteralPath $sumFile -Encoding ASCII
Write-Host "  $($parts.Count) parts + SHA256SUMS"

# --- 4. the pieces that make it usable on the far side ------------------------
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'join-and-load.ps1') -Destination (Join-Path $dest 'join-and-load.ps1') -Force
$readmeSrc = Join-Path $PSScriptRoot 'usb-readme.txt'
$readme = Join-Path $dest 'README.txt'
$utf8Bom = New-Object System.Text.UTF8Encoding $true
[System.IO.File]::WriteAllText($readme, (Get-Content -LiteralPath $readmeSrc -Raw -Encoding UTF8), $utf8Bom)

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
