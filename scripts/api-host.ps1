<# Foreground entry point, also used by the startup tasks. No secrets in command arguments. #>
param(
    [Parameter(Mandatory)][ValidateSet('api','proxy')][string]$Mode,
    [Parameter(Mandatory)][string]$StateDirectory,
    [Parameter(Mandatory)][string]$Hostname,
    [string]$LanAddress,
    [string]$CaddyExe,
    [string]$ClientNetwork = '192.168.1.0/24'
)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
if ($Hostname -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*$') { throw 'Invalid hostname' }
$statePath = (Resolve-Path -LiteralPath $StateDirectory).Path
if ($Mode -eq 'api') {
    & (Join-Path $Repo 'scripts\run.ps1') api serve --tokens (Join-Path $statePath 'tokens.json') --origin "https://$Hostname"
} else {
    $ip = [Net.IPAddress]::Parse($LanAddress)
    $b = $ip.GetAddressBytes()
    if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        -not ($b[0] -eq 10 -or ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or
            ($b[0] -eq 192 -and $b[1] -eq 168))) {
        throw 'Use the specific private LAN IPv4 address'
    }
    $env:LIBRARIAN_LAN_HOST = $Hostname
    $env:LIBRARIAN_LAN_IP = $LanAddress
    $env:LIBRARIAN_CLIENT_NETWORK = $ClientNetwork
    $env:LIBRARIAN_CADDY_DATA = (Join-Path $statePath 'caddy').Replace('\','/')
    & $CaddyExe run --config (Join-Path $Repo 'config\Caddyfile.example') --adapter caddyfile
    exit $LASTEXITCODE
}
