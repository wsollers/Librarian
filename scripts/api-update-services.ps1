<# Update existing WinSW configuration/log locations. Run elevated; preserves tokens and CA keys. #>
param(
    [string]$InstallDirectory = "$env:ProgramData\Librarian\Services",
    [string]$StateDirectory = "$env:ProgramData\Librarian\State",
    [ValidateSet('Preserve','Lan','Bearer')][string]$AuthMode = 'Preserve'
)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run elevated' }
[xml]$api = Get-Content -LiteralPath (Join-Path $InstallDirectory 'LibrarianApi.xml')
[xml]$proxy = Get-Content -LiteralPath (Join-Path $InstallDirectory 'LibrarianHttps.xml')
$envValues = @{}
foreach ($entry in $proxy.service.env) { $envValues[$entry.name] = $entry.value }
$modelDirectory = ($api.service.env | Where-Object name -eq HF_HOME).value
if ($AuthMode -eq 'Preserve') { $AuthMode = if ($api.service.arguments -match '--trust-lan') { 'Lan' } else { 'Bearer' } }
foreach ($name in @('LibrarianApi','LibrarianHttps')) {
    $svc = Get-CimInstance Win32_Service -Filter "Name='$name'"
    if ($svc.StartName -ine "NT SERVICE\$name" -or $svc.PathName -ine ('"' + (Join-Path $InstallDirectory "$name.exe") + '"')) { throw "Unexpected service identity/path: $name" }
}
Stop-Service LibrarianHttps
Stop-Service LibrarianApi
$root = [IO.Path]::GetFullPath((Join-Path $Repo '.runtime\logs'))
New-Item -ItemType Directory -Path $root -Force | Out-Null
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($identity.User, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'), [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new('NT SERVICE\LibrarianApi', 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
Set-Acl -LiteralPath $root -AclObject $acl
foreach ($name in @('LibrarianApi','LibrarianHttps')) {
    $path = Join-Path $root "winsw\$name"
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    $acl = Get-Acl -LiteralPath $path
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new("NT SERVICE\$name", 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    Set-Acl -LiteralPath $path -AclObject $acl
}
& (Join-Path $PSScriptRoot 'api-service-config.ps1') -OutputDirectory $InstallDirectory -StateDirectory $StateDirectory `
    -CaddyExe (Join-Path $InstallDirectory 'caddy.exe') -Hostname $envValues.LIBRARIAN_LAN_HOST `
    -LanAddress $envValues.LIBRARIAN_LAN_IP -ClientNetwork $envValues.LIBRARIAN_CLIENT_NETWORK -ModelDirectory $modelDirectory -AuthMode $AuthMode

# Move individual service log files only. All resolved targets must stay under the log root.
function MoveLogs($source, $filter, $destination) {
    if (-not (Test-Path -LiteralPath $source)) { return }
    $sourceRoot = [IO.Path]::GetFullPath($source).TrimEnd('\') + '\'
    $destRoot = [IO.Path]::GetFullPath((Join-Path $root $destination))
    if (-not $destRoot.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Log destination outside retention root' }
    New-Item -ItemType Directory -Path $destRoot -Force | Out-Null
    foreach ($file in Get-ChildItem -LiteralPath $source -Filter $filter -File) {
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        if (-not $file.FullName.StartsWith($sourceRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Log source outside expected directory' }
        $target = Join-Path $destRoot $file.Name
        if (Test-Path -LiteralPath $target) { throw "Log destination already exists: $target" }
        Move-Item -LiteralPath $file.FullName -Destination $target
    }
}
foreach ($name in @('LibrarianApi','LibrarianHttps')) {
    MoveLogs (Join-Path $StateDirectory "logs\$name") '*.log*' "legacy\$name"
}
Push-Location $Repo
try {
    $dataPath = & .venv/Scripts/python.exe -c "from librarian.mcp_server import load_env; from pathlib import Path; load_env(Path('.env')); from librarian.config import load; print(load().data)"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate previous API logs' }
    MoveLogs (Join-Path $dataPath.Trim() 'logs') '*-api.log' 'legacy\api'
} finally { Pop-Location }
Start-Service LibrarianApi
Start-Service LibrarianHttps
Write-Host "Services restarted. Logs: $root (90 days; startup and request cleanup). Poll readiness before searching."
