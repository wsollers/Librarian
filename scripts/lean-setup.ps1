# Lean 4 + Mathlib for the leancheck pass, and the lean-lsp-mcp server for Codex.
# Needs elan and uv installed (open a NEW terminal after installing so PATH is current).
# Log: D:\Readings\librarian\lean\setup.log
param([string]$Project = "D:\Readings\librarian\lean", [switch]$SkipCodex)
$ErrorActionPreference = "Continue"
Start-Transcript -Path (Join-Path $Project "setup.log") -Force | Out-Null
function Step($m) { Write-Host "`n=== $m" -ForegroundColor Cyan }

Step "tools"
foreach ($t in "elan", "lake", "lean", "uv", "uvx", "codex", "git") {
    $c = Get-Command $t -ErrorAction SilentlyContinue
    if ($c) { Write-Host ("{0,-6} {1}" -f $t, $c.Source) } else { Write-Host "$t MISSING" -ForegroundColor Red }
}
& elan --version; & uv --version; & codex --version

Push-Location $Project
Step "lake update (clones Mathlib and its dependencies; first time takes a while)"
& lake update 2>&1 | Select-Object -Last 15
Step "lake exe cache get (prebuilt Mathlib, ~5 GB)"
& lake exe cache get 2>&1 | Select-Object -Last 8
Step "lake build"
& lake build 2>&1 | Select-Object -Last 5
Step "compile Smoke.lean (expect one error: unknown identifier in smoke_bad)"
$sw = [Diagnostics.Stopwatch]::StartNew()
& lake env lean Smoke.lean 2>&1
Write-Host ("compile took {0:n0}s" -f $sw.Elapsed.TotalSeconds)
Pop-Location

if (-not $SkipCodex) {
    Step "register lean-lsp-mcp with Codex"
    & codex mcp remove lean 2>&1 | Out-Null
    & codex mcp add lean --env "LEAN_PROJECT_PATH=$Project" -- uvx lean-lsp-mcp 2>&1
    & codex mcp list 2>&1
    Step "Codex smoke test through the MCP server"
    $prompt = @"
Use the tools of the 'lean' MCP server (not the shell) to get the diagnostics of the file Smoke.lean in the Lean project $Project.
Report: which MCP tools you called, every error with its line and message, and the correct Mathlib name for anything unknown (use the server's search tools to find it).
"@
    $out = Join-Path $Project "smoke-codex.txt"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $prompt | & codex exec --skip-git-repo-check --sandbox read-only -C $Project --output-last-message $out - 2>&1 | Select-Object -Last 40
    Write-Host ("codex took {0:n0}s" -f $sw.Elapsed.TotalSeconds)
    Step "codex answer"
    if (Test-Path $out) { Get-Content $out } else { Write-Host "no answer file" -ForegroundColor Red }
}
Stop-Transcript | Out-Null
