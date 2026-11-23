<# Create a private credential directory. Run as the account that will run the API. #>
param([string]$Directory = "$env:LOCALAPPDATA\LibrarianApi")
$ErrorActionPreference = "Stop"
$resolved = [IO.Path]::GetFullPath($Directory)
if (Test-Path -LiteralPath $resolved) { throw "Directory already exists; inspect its ACL rather than overwriting it: $resolved" }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($identity, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
New-Item -ItemType Directory -Path $resolved | Out-Null
Set-Acl -LiteralPath $resolved -AclObject $acl
Write-Host "Private directory ready: $resolved"
