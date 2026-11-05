<#
.SYNOPSIS
  Register the librarian MCP server with the Claude desktop app (claude_desktop_config.json).
.DESCRIPTION
  Adds or updates mcpServers.librarian -> <repo>\.venv\Scripts\python.exe -m librarian mcp,
  keeping every other setting. Handles the regular and the Microsoft Store install; backs up first.
  Restart the Claude desktop app afterwards (quit from the tray, not just close the window).
.EXAMPLE
  scripts\install-mcp.ps1
  scripts\install-mcp.ps1 -Remove
#>
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "No .venv: run scripts\bootstrap.ps1 first." }
& $Py -c "import mcp" 2>$null
if ($LASTEXITCODE -ne 0) { throw "The MCP SDK is not installed in .venv: run scripts\bootstrap.ps1 (it installs it)." }

$Configs = @(Join-Path $env:APPDATA "Claude\claude_desktop_config.json")
$Store = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Packages") -Directory -Filter "Claude_*" -ErrorAction SilentlyContinue
foreach ($s in $Store) { $Configs += Join-Path $s.FullName "LocalCache\Roaming\Claude\claude_desktop_config.json" }
$Existing = @($Configs | Where-Object { Test-Path $_ })
if (-not $Existing) {
    $Existing = @($Configs[0])
    New-Item -ItemType Directory -Force -Path (Split-Path $Configs[0]) | Out-Null
    Set-Content -Path $Configs[0] -Value "{}" -Encoding Ascii
}

foreach ($cfgPath in $Existing) {
    $raw = Get-Content $cfgPath -Raw
    if (-not $raw.Trim()) { $raw = "{}" }
    $cfg = $raw | ConvertFrom-Json
    Copy-Item $cfgPath "$cfgPath.bak" -Force
    if (-not $cfg.PSObject.Properties["mcpServers"]) {
        $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
    }
    if ($Remove) {
        $cfg.mcpServers.PSObject.Properties.Remove("librarian")
    } else {
        $entry = [pscustomobject]@{ command = $Py; args = @("-m", "librarian", "mcp") }
        $cfg.mcpServers | Add-Member -NotePropertyName librarian -NotePropertyValue $entry -Force
    }
    $json = $cfg | ConvertTo-Json -Depth 32
    [IO.File]::WriteAllText($cfgPath, $json, (New-Object Text.UTF8Encoding $false))
    Write-Host ("{0} librarian in {1} (backup: .bak)" -f ($(if ($Remove) { "Removed" } else { "Registered" }), $cfgPath)) -ForegroundColor Green
}
Write-Host "Restart the Claude desktop app (quit it from the tray) to load the server." -ForegroundColor Cyan
