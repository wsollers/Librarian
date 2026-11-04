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
Push-Location $Repo
try {
    & $Py -m librarian @args
    exit $LASTEXITCODE
} finally { Pop-Location }
