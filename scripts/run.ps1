<#
.SYNOPSIS
  Run librarian inside the project .venv, with .env loaded (OPENAI_API_KEY etc.).
  Every stage writes a detailed log to <data>\logs\.
.EXAMPLE
  scripts\run.ps1 run --select priority            # inventory, extract, pagemap, index
  scripts\run.ps1 extract --select all --llm-ocr   # whole library, OpenAI OCR for bad pages
  scripts\run.ps1 search "construction sequence"
#>
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "No .venv: run scripts\bootstrap.ps1 first." }
$EnvFile = Join-Path $Repo ".env"
if (Test-Path $EnvFile) {
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$' -and $Matches[2] -ne "") {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim('"', "'"), "Process")
        }
    }
}
# UTF-8 console I/O; load cached Hugging Face models without asking the Hub every run
# (set HF_HUB_OFFLINE=0 in .env when a new model must be downloaded).
$env:PYTHONIOENCODING = "utf-8"
if (-not $env:HF_HUB_OFFLINE) { $env:HF_HUB_OFFLINE = "1" }
# Quiet model loading: no progress bars or Hub notices (errors still show).
foreach ($v in @("HF_HUB_DISABLE_PROGRESS_BARS=1", "HF_HUB_VERBOSITY=error", "TRANSFORMERS_VERBOSITY=error",
                 "TRANSFORMERS_NO_ADVISORY_WARNINGS=1", "TQDM_DISABLE=1")) {
    $k, $val = $v -split "=", 2
    if (-not [Environment]::GetEnvironmentVariable($k, "Process")) { [Environment]::SetEnvironmentVariable($k, $val, "Process") }
}
Push-Location $Repo
try {
    & $Py -m librarian @args
    exit $LASTEXITCODE
} finally { Pop-Location }
