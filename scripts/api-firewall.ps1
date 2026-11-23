<# Run elevated. Scoped to the LAN interface, address and private client IPs/subnets. #>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$LanAddress,
    [Parameter(Mandatory)][string]$InterfaceAlias,
    [Parameter(Mandatory)][string[]]$ClientAddress
)
$ErrorActionPreference = "Stop"
foreach ($value in @($LanAddress) + $ClientAddress) {
    $parts = $value.Split('/')
    if ($parts.Count -gt 2 -or ($parts.Count -eq 2 -and ($parts[1] -notmatch '^\d+$' -or [int]$parts[1] -lt 24 -or [int]$parts[1] -gt 32))) { throw "Use private IPs or /24 through /32 subnets: $value" }
    $ip = [Net.IPAddress]::Parse($parts[0])
    $b = $ip.GetAddressBytes()
    if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        -not ($b[0] -eq 10 -or ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or
            ($b[0] -eq 192 -and $b[1] -eq 168))) { throw "Use private LAN IPv4 addresses or subnets: $value" }
}
$address = Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 | Where-Object IPAddress -eq $LanAddress
if (-not $address) { throw "LAN address does not belong to that interface" }
if (Get-NetFirewallRule -Name 'Librarian-LAN-HTTPS' -ErrorAction SilentlyContinue) { throw "Rule already exists; review it before changing access" }
if ($PSCmdlet.ShouldProcess($LanAddress, 'Allow HTTPS from specified LAN clients')) {
    New-NetFirewallRule -Name 'Librarian-LAN-HTTPS' -DisplayName 'Librarian LAN HTTPS' `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort 443 -LocalAddress $LanAddress `
        -RemoteAddress $ClientAddress -InterfaceAlias $InterfaceAlias -Profile Any | Out-Null
}
Write-Host 'Review other inbound allow rules: a broad existing rule can still permit access.'
