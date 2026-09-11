<#
.SYNOPSIS
  Reassemble the image tar from its FAT32-sized pieces and load it into Docker.

.DESCRIPTION
  Run this from the USB stick (or a folder the pieces were copied into) on the
  machine that should receive the image.  It verifies every piece against
  SHA256SUMS, concatenates them back into one tar, and hands it to docker load.

  Needs roughly twice the image size in free disk space: the pieces plus the
  reassembled tar exist at the same time.

.EXAMPLE
  pwsh -File .\join-and-load.ps1
  pwsh -File .\join-and-load.ps1 -WorkDir D:\h3 -SkipVerify
#>
[CmdletBinding()]
param(
    [string]$ImageDir,
    [string]$WorkDir = $env:TEMP,
    [string]$ExpectedTag = 'minimax-h3:2.1.7-cu132-py3.14',
    [switch]$SkipVerify,
    [switch]$KeepTar
)

$ErrorActionPreference = 'Stop'
if (-not $ImageDir) {
    # On the stick the pieces live in an "image" subdirectory next to this
    # script; copied loose into a folder, they sit right beside it.
    $nested = Join-Path $PSScriptRoot 'image'
    $ImageDir = if (Test-Path -LiteralPath $nested) { $nested } else { $PSScriptRoot }
}
if (-not (Test-Path -LiteralPath $ImageDir)) { throw "no such directory: $ImageDir" }

$server = docker version --format '{{.Server.Version}}' 2>&1
if ($LASTEXITCODE -ne 0) { throw "docker engine not reachable - start Docker Desktop first ($server)" }

$parts = @(Get-ChildItem -Path $ImageDir -Filter 'image.tar.part-*' | Sort-Object Name)
if ($parts.Count -eq 0) {
    throw "no image.tar.part-* files in $ImageDir -- pass -ImageDir <folder holding the pieces>"
}
$totalBytes = ($parts | Measure-Object Length -Sum).Sum
Write-Host "== pieces ==" -ForegroundColor Cyan
Write-Host "  $($parts.Count) parts, $([math]::Round($totalBytes/1GB,2)) GB"

# --- verify -------------------------------------------------------------------
$sums = Join-Path $ImageDir 'SHA256SUMS'
if (-not $SkipVerify -and (Test-Path -LiteralPath $sums)) {
    Write-Host "== verify ==" -ForegroundColor Cyan
    $expected = @{}
    foreach ($line in Get-Content -LiteralPath $sums) {
        if ($line -match '^([0-9a-f]{64})\s+\*?(.+)$') { $expected[$Matches[2].Trim()] = $Matches[1] }
    }
    foreach ($p in $parts) {
        if (-not $expected.ContainsKey($p.Name)) { Write-Warning "  $($p.Name): not listed in SHA256SUMS"; continue }
        $actual = (Get-FileHash -LiteralPath $p.FullName -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $expected[$p.Name]) { throw "checksum mismatch on $($p.Name) - copy it off the stick again" }
        Write-Host "  $($p.Name) ok"
    }
} elseif (-not $SkipVerify) {
    Write-Warning "no SHA256SUMS next to the pieces - skipping verification"
}

# --- join ---------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$tar = Join-Path $WorkDir 'h3-image.tar'
Write-Host "== join ==" -ForegroundColor Cyan
Write-Host "  -> $tar"
$out = [System.IO.File]::Create($tar)
try {
    $i = 0
    foreach ($p in $parts) {
        $i++
        Write-Progress -Activity 'joining' -Status $p.Name -PercentComplete (100 * $i / $parts.Count)
        $in = [System.IO.File]::OpenRead($p.FullName)
        try { $in.CopyTo($out, 16MB) } finally { $in.Dispose() }
    }
} finally { $out.Dispose() }
Write-Progress -Activity 'joining' -Completed

$joined = (Get-Item -LiteralPath $tar).Length
if ($joined -ne $totalBytes) { throw "joined size $joined != pieces size $totalBytes" }

# --- load ---------------------------------------------------------------------
Write-Host "== docker load ==" -ForegroundColor Cyan
docker load -i $tar
if ($LASTEXITCODE -ne 0) { throw "docker load failed" }
if (-not $KeepTar) { Remove-Item -LiteralPath $tar -Force }

Write-Host ""
Write-Host "== done ==" -ForegroundColor Green
docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' | Select-String -Pattern 'minimax-h3' | ForEach-Object { $_.Line }
Write-Host ""
Write-Host "next: mount the weights and run the checks"
Write-Host "  docker run --rm -it --gpus all -v <models-dir>:/models $ExpectedTag check"
Write-Host "the 27 GB of weights are NOT in the image - see README.txt on the stick."
