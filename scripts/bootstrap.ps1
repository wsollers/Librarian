<#
.SYNOPSIS
  Create or refresh the project .venv: the only supported run environment.

.DESCRIPTION
  * Picks Python 3.12, else 3.13, else 3.14 via the py launcher (override with -Python).
  * Installs CUDA PyTorch from the PyTorch index (-Cuda, default cu126; RTX 40xx is fine).
  * First run: installs librarian[embed,dev] and freezes requirements.lock.
    Later runs: installs exactly requirements.lock (commit it; -Relock to refresh).
  * Checks external tools (poppler required; djvulibre, docker, ollama, nvidia-smi optional).
#>
param(
    [string]$Python = "",
    [string]$Cuda = "cu126",
    [switch]$Relock
)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
$Venv = Join-Path $Repo ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Lock = Join-Path $Repo "requirements.lock"
$TorchIndex = "https://download.pytorch.org/whl/$Cuda"

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Ok($m)   { Write-Host "  ok $m" -ForegroundColor Green }

if (-not (Test-Path $Py)) {
    if (-not $Python) {
        $installed = (& py -0p 2>&1 | Out-String)
        foreach ($v in "3.12", "3.13", "3.14") {
            if ($installed -match "-V:$([regex]::Escape($v))\b|-$([regex]::Escape($v))-64") { $Python = $v; break }
        }
        if (-not $Python) { throw "No Python 3.12-3.14 found via the py launcher." }
    }
    Step "Creating .venv with Python $Python"
    & py "-$Python" -m venv $Venv
}
& $Py -c "import sys; print('  python', sys.version.split()[0], sys.prefix)"

Step "Upgrading pip"
& $Py -m pip install --quiet --upgrade pip

if ((Test-Path $Lock) -and -not $Relock) {
    Step "Installing pinned environment from requirements.lock"
    & $Py -m pip install --quiet -r $Lock --extra-index-url $TorchIndex
    & $Py -m pip install --quiet --no-deps -e .
} else {
    Step "Installing CUDA PyTorch ($Cuda)"
    & $Py -m pip install --quiet torch --index-url $TorchIndex
    Step "Installing librarian[embed,dev]"
    & $Py -m pip install --quiet -e ".[embed,dev]"
    Step "Freezing requirements.lock"
    & $Py -m pip freeze --exclude-editable | Set-Content -Encoding utf8 $Lock
    Warn "commit requirements.lock so every machine gets the same environment"
}
if ($LASTEXITCODE -ne 0) { throw "pip failed" }

Step "Checking tools"
foreach ($t in "pdfinfo", "pdftotext", "pdftoppm", "pdfimages") {
    if (Get-Command $t -ErrorAction SilentlyContinue) { Ok $t } else { Warn "$t missing: install poppler (choco install poppler) - REQUIRED" }
}
foreach ($t in "djvutxt", "djvused") {
    if (Get-Command $t -ErrorAction SilentlyContinue) { Ok $t } else { Warn "$t missing: DjVu books (2) will not extract (choco install djvulibre)" }
}
if (Get-Command docker -ErrorAction SilentlyContinue) { Ok "docker (nougat)" } else { Warn "docker missing: --nougat unavailable" }
if (Get-Command ollama -ErrorAction SilentlyContinue) { Ok "ollama" } else { Warn "ollama missing: only needed for --embed ollama:..." }
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpu = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader) -join "; "
    Ok "GPU: $gpu"
}
& $Py -c "import torch; print('  torch', torch.__version__, 'cuda', torch.cuda.is_available())"
& $Py -c "import sqlite3, sqlite_vec; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); sqlite_vec.load(c); print('  sqlite-vec', c.execute('select vec_version()').fetchone()[0])"

foreach ($pair in @(@("config\example.toml", "config\local.toml"), @(".env.example", ".env"))) {
    if (-not (Test-Path $pair[1])) { Copy-Item $pair[0] $pair[1]; Warn "created $($pair[1]) from $($pair[0]): edit it" }
}
Step "Done. Run: scripts\run.ps1 status"
