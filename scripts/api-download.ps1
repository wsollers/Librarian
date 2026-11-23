<# Download pinned official Windows runtime binaries, verifying each download before use. #>
param([string]$Directory = (Join-Path (Split-Path -Parent $PSScriptRoot) '.runtime\windows'))
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $Directory -Force | Out-Null
function Download($url, $file, $algorithm, $expected) {
    if (-not (Test-Path -LiteralPath $file)) { Invoke-WebRequest -UseBasicParsing $url -OutFile $file }
    if ((Get-FileHash -LiteralPath $file -Algorithm $algorithm).Hash -ine $expected) { throw "Checksum mismatch: $file; inspect and remove this file before retrying" }
}
Download 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe' (Join-Path $Directory 'WinSW-x64.exe') SHA256 '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'
$archive = Join-Path $Directory 'caddy_2.11.9_windows_amd64.zip'
Download 'https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_windows_amd64.zip' $archive SHA512 '98753d5d42650383d00619ac70917a217141f9e92314185bf6c1b8b439cfcfdf8393da0ebb4675c8cb3b23d29a19aabe99dc5aca64fa1bfc3a1ba22509b06871'
Expand-Archive -LiteralPath $archive -DestinationPath $Directory -Force
Write-Host "Verified WinSW 2.12.0 and Caddy 2.11.9 in $Directory"
