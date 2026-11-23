<# Install native WinSW services and LAN firewall rule. Run in elevated PowerShell.
Uses dedicated passwordless virtual service accounts. Does not change the network profile.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$WinSWExe,
    [Parameter(Mandatory)][string]$CaddyExe,
    [string]$InstallDirectory = "$env:ProgramData\Librarian\Services",
    [string]$StateDirectory = "$env:ProgramData\Librarian\State",
    [string]$Hostname = 'hal5000.fios-router.home',
    [string]$LanAddress = '192.168.1.243',
    [string]$InterfaceAlias = 'Ethernet',
    [string]$ClientNetwork = '192.168.1.0/24',
    [string]$ModelDirectory = 'D:\Models\huggingface',
    [switch]$Resume,
    [ValidateSet('Lan','Bearer')][string]$AuthMode = 'Lan'
)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $Repo '.venv\Scripts\python.exe'
$WinSWExe = (Resolve-Path -LiteralPath $WinSWExe).Path
$CaddyExe = (Resolve-Path -LiteralPath $CaddyExe).Path
foreach ($name in @('LibrarianApi','LibrarianHttps')) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        $installed = Get-CimInstance Win32_Service -Filter "Name='$name'"
        $expected = '"' + (Join-Path $InstallDirectory "$name.exe") + '"'
        if (-not $Resume -or $installed.State -ne 'Stopped' -or $installed.PathName -ine $expected -or $installed.StartName -ine "NT SERVICE\$name") {
            throw "Service already exists or differs from this installer: $name; inspect before resuming"
        }
    }
}
foreach ($path in @($InstallDirectory, $StateDirectory)) {
    if ((Test-Path -LiteralPath $path) -and -not $Resume) { throw "Directory already exists: $path; inspect it before reinstalling" }
}
if (Get-ScheduledTask -TaskName 'Librarian-API','Librarian-HTTPS' -ErrorAction SilentlyContinue) { throw 'Remove the old Librarian scheduled tasks before installing services' }
if (Get-NetFirewallRule -Name 'Librarian-LAN-HTTPS' -ErrorAction SilentlyContinue) { throw 'Existing Librarian firewall rule must be reviewed before installation' }
if (-not (Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 | Where-Object IPAddress -eq $LanAddress)) { throw 'Selected LAN address is not on that interface' }
if (-not $PSCmdlet.ShouldProcess("$InstallDirectory; $LanAddress`:443 from $ClientNetwork", 'Install WinSW services, grant scoped filesystem permissions, and enable LAN HTTPS')) { return }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this installer in an elevated Windows PowerShell window' }

function PrivateDirectory($path) {
    if ($Resume -and (Test-Path -LiteralPath $path)) { return }
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $path -AclObject $acl
}
function Grant($path, $account, $rights, $directory = $true) {
    $acl = Get-Acl -LiteralPath $path
    if (-not $acl.AreAccessRulesCanonical -and $rights -eq 'ReadAndExecute') {
        foreach ($rule in $acl.Access) {
            try { $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } catch { continue }
            if ($sid -eq 'S-1-5-11' -and $rule.AccessControlType -eq 'Allow' -and
                ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -eq 0 -and
                ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadAndExecute) -eq [Security.AccessControl.FileSystemRights]::ReadAndExecute) {
                Write-Host "Preserving noncanonical ACL with existing authenticated-user read access: $path"
                return
            }
        }
    }
    $inherit = if ($directory) { 'ContainerInherit,ObjectInherit' } else { 'None' }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($account, $rights, $inherit, 'None', 'Allow'))
    Set-Acl -LiteralPath $path -AclObject $acl
}
PrivateDirectory $InstallDirectory
PrivateDirectory $StateDirectory
$logRoot = Join-Path $Repo '.runtime\logs'
PrivateDirectory $logRoot
Grant $logRoot $identity.Name 'FullControl'
Copy-Item -LiteralPath $CaddyExe -Destination (Join-Path $InstallDirectory 'caddy.exe')
foreach ($name in @('LibrarianApi','LibrarianHttps')) {
    Copy-Item -LiteralPath $WinSWExe -Destination (Join-Path $InstallDirectory "$name.exe")
    PrivateDirectory (Join-Path $logRoot "winsw\$name")
}
PrivateDirectory (Join-Path $StateDirectory 'caddy')
PrivateDirectory (Join-Path $StateDirectory 'auth')
& (Join-Path $PSScriptRoot 'api-service-config.ps1') -OutputDirectory $InstallDirectory -StateDirectory $StateDirectory `
    -CaddyExe (Join-Path $InstallDirectory 'caddy.exe') -Hostname $Hostname -LanAddress $LanAddress -ClientNetwork $ClientNetwork -ModelDirectory $ModelDirectory -AuthMode $AuthMode
foreach ($name in @('LibrarianApi','LibrarianHttps')) {
    if (-not (Get-Service -Name $name -ErrorAction SilentlyContinue)) {
        & (Join-Path $InstallDirectory "$name.exe") install
        if ($LASTEXITCODE -ne 0) { throw "WinSW install failed: $name; services have not been started" }
    }
    $service = Get-CimInstance Win32_Service -Filter "Name='$name'"
    if ($service.StartName -ine "NT SERVICE\$name") { throw "Unexpected service identity for $name; refusing to start" }
    Grant $InstallDirectory "NT SERVICE\$name" 'ReadAndExecute'
    Grant $StateDirectory "NT SERVICE\$name" 'ReadAndExecute' $false
    Grant (Join-Path $logRoot "winsw\$name") "NT SERVICE\$name" 'Modify'
}
Grant (Join-Path $StateDirectory 'caddy') 'NT SERVICE\LibrarianHttps' 'Modify'
Grant (Join-Path $StateDirectory 'auth') 'NT SERVICE\LibrarianApi' 'ReadAndExecute'
Grant $logRoot 'NT SERVICE\LibrarianApi' 'Modify'
Grant (Join-Path $logRoot 'winsw\LibrarianHttps') 'NT SERVICE\LibrarianApi' 'Modify'
Grant $Repo 'NT SERVICE\LibrarianApi' 'ReadAndExecute'
$basePython = & $python -c 'import sys; print(sys.base_prefix)'
if ($LASTEXITCODE -ne 0) { throw 'Cannot locate base Python' }
Grant $basePython.Trim() 'NT SERVICE\LibrarianApi' 'ReadAndExecute'
Grant $ModelDirectory 'NT SERVICE\LibrarianApi' 'ReadAndExecute'
Push-Location $Repo
try {
    $dataPath = & $python -c "from librarian.mcp_server import load_env; from pathlib import Path; load_env(Path('.env')); from librarian.config import load; print(load().data)"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate library data' }
    $dataPath = $dataPath.Trim()
    Grant $dataPath 'NT SERVICE\LibrarianApi' 'ReadAndExecute'
    foreach ($folder in @('logs','cache')) {
        $path = Join-Path $dataPath $folder
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        Grant $path 'NT SERVICE\LibrarianApi' 'Modify'
    }
    if ($AuthMode -eq 'Bearer' -and -not (Test-Path -LiteralPath (Join-Path $StateDirectory 'auth\tokens.json'))) {
        & $python -m librarian api token-add --tokens (Join-Path $StateDirectory 'auth\tokens.json') --client zarathustra --output (Join-Path $StateDirectory 'zarathustra.token')
        if ($LASTEXITCODE -ne 0) { throw 'Client provisioning failed' }
    }
} finally { Pop-Location }
& (Join-Path $PSScriptRoot 'api-firewall.ps1') -LanAddress $LanAddress -InterfaceAlias $InterfaceAlias -ClientAddress $ClientNetwork
Start-Service LibrarianApi
Start-Service LibrarianHttps
Write-Host "Services started in $AuthMode mode. Public CA: $StateDirectory\caddy\pki\authorities\local\root.crt"
Write-Host "Check https://$Hostname/v1/status for model readiness; service Running alone does not imply readiness."
