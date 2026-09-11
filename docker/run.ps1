<#
.SYNOPSIS
  Run the MiniMax-H3 image with the GPU and the model weights attached.

.DESCRIPTION
  Wraps "docker run --gpus all" and does the path wiring the workflow expects:

      D:\otherProject\minimax-h3         ->  /workspace  (scripts + media + cache)
      D:\otherProject\minimax-h3\models  ->  /models     (the 27 GB of weights)
      named volume h3-hf-cache            ->  /root/.cache (modelscope downloads)

  Everything that is not one of the switches below is forwarded to the container
  verbatim, and the entrypoint turns a run_h3.sh verb into "run_h3.sh <verb>",
  so "run.ps1 check" behaves exactly like "./run_h3.sh check".

  This script deliberately has NO param() block.  With one, PowerShell adds the
  common parameters, and then the workflow's own "--out" is ambiguous with
  -OutVariable / -OutBuffer and fails to parse -- while --preset, --prompt-file
  and friends bind fine, which makes it a confusing trap.  A simple script keeps
  every argument in $args untouched.

.EXAMPLE
  pwsh docker/run.ps1 check
  pwsh docker/run.ps1 gen --preset draft --prompt-file workspace/news/prompt.txt --out workspace/news/out.mp4
  pwsh docker/run.ps1                       # interactive shell
  pwsh docker/run.ps1 -NoTty nvidia-smi -L
  pwsh docker/run.ps1 -Models E:\h3-models check
#>

$ErrorActionPreference = 'Stop'

$opt = @{
    Tag         = 'minimax-h3:2.1.7-cu132-py3.14'
    Models      = $null
    Workspace   = $null
    Name        = 'h3'
    CacheVolume = 'h3-hf-cache'
    ShmSize     = '2g'
    NoTty       = $false
    NoGpu       = $false
}

# Split our own switches out of the argument list; everything else is the
# container's command line, untouched.
$pass = New-Object System.Collections.Generic.List[string]
$i = 0
while ($i -lt $args.Count) {
    $a = [string]$args[$i]
    $key = $a.TrimStart('-')
    switch -Regex ($key) {
        '^(?i)notty$'        { $opt.NoTty = $true; $i++; continue }
        '^(?i)nogpu$'        { $opt.NoGpu = $true; $i++; continue }
        '^(?i)tag$'          { $opt.Tag = [string]$args[$i + 1]; $i += 2; continue }
        '^(?i)models$'       { $opt.Models = [string]$args[$i + 1]; $i += 2; continue }
        '^(?i)workspace$'    { $opt.Workspace = [string]$args[$i + 1]; $i += 2; continue }
        '^(?i)name$'         { $opt.Name = [string]$args[$i + 1]; $i += 2; continue }
        '^(?i)shmsize$'      { $opt.ShmSize = [string]$args[$i + 1]; $i += 2; continue }
        '^(?i)cachevolume$'  { $opt.CacheVolume = [string]$args[$i + 1]; $i += 2; continue }
        default              { $pass.Add($a); $i++ }
    }
}

$repo = Split-Path -Parent $PSScriptRoot
if (-not $opt.Models) { $opt.Models = Join-Path $repo 'models' }
if (-not $opt.Workspace) { $opt.Workspace = $repo }

function ConvertTo-WslPath {
    param([string]$WindowsPath)
    $full = (Resolve-Path -LiteralPath $WindowsPath).Path
    $drive = $full.Substring(0, 1).ToLower()
    $rest = $full.Substring(2).Replace('\', '/')
    return "/mnt/$drive$rest"
}

if (-not (Test-Path -LiteralPath $opt.Models)) {
    $wslStage = ConvertTo-WslPath (Join-Path $PSScriptRoot 'stage-models.sh')
    Write-Warning "no model directory at $($opt.Models)"
    Write-Warning "the DiT / text-encoder / VAE weights are 27 GB and stay out of the image"
    Write-Warning "stage them once, from the WSL-side copy:"
    Write-Warning "  wsl -d Ubuntu -u yhc -- bash $wslStage"
    Write-Warning "or pass -Models <dir> pointing at wherever they already live."
}

$dockerArgs = @('run', '--name', $opt.Name, '--shm-size', $opt.ShmSize, '--rm')
if (-not $opt.NoGpu) { $dockerArgs += @('--gpus', 'all') }
if (-not $opt.NoTty) { $dockerArgs += @('-i', '-t') }
$dockerArgs += @(
    '-v', "$($opt.Models):/models",
    '-v', "$($opt.Workspace):/workspace",
    '-v', "$($opt.CacheVolume):/root/.cache",
    $opt.Tag
)
if ($pass.Count -gt 0) { $dockerArgs += $pass.ToArray() }

Write-Host "docker $($dockerArgs -join ' ')" -ForegroundColor DarkGray
& docker @dockerArgs
exit $LASTEXITCODE
