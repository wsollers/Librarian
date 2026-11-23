<# Generate WinSW 2.x configuration without installing services or changing permissions. #>
param(
    [Parameter(Mandatory)][string]$OutputDirectory,
    [Parameter(Mandatory)][string]$StateDirectory,
    [Parameter(Mandatory)][string]$CaddyExe,
    [string]$Hostname = 'hal5000.fios-router.home',
    [string]$LanAddress = '192.168.1.243',
    [string]$ClientNetwork = '192.168.1.0/24',
    [string]$ModelDirectory = 'D:\Models\huggingface',
    [ValidateSet('Lan','Bearer')][string]$AuthMode = 'Lan'
)
$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
if ($Hostname -notmatch '^[a-zA-Z0-9][a-zA-Z0-9.-]*$') { throw 'Invalid hostname' }
foreach ($value in @($Repo, $StateDirectory, $CaddyExe, $ModelDirectory)) {
    if ($value -match '["\r\n%]') { throw 'Unsupported character in service path' }
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
function Xml($value) { [Security.SecurityElement]::Escape([string]$value) }
foreach ($name in @('LibrarianApi','LibrarianHttps')) {
    $logPath = Join-Path $Repo ".runtime\logs\winsw\$name"
    if ($name -eq 'LibrarianApi') {
        $exe = Join-Path $Repo '.venv\Scripts\python.exe'
        $arguments = '-m librarian api serve --tokens "{0}\auth\tokens.json" --origin "https://{1}"' -f $StateDirectory,$Hostname
        if ($AuthMode -eq 'Lan') { $arguments = '-m librarian api serve --trust-lan --origin "https://{0}"' -f $Hostname }
        $environment = '<env name="HF_HOME" value="{0}"/><env name="PYTHONIOENCODING" value="utf-8"/><env name="PYTHONDONTWRITEBYTECODE" value="1"/>' -f (Xml $ModelDirectory)
        $depend = ''
        $workingDirectory = $Repo
    } else {
        $exe = $CaddyExe
        $arguments = 'run --config "{0}\Caddyfile" --adapter caddyfile' -f $OutputDirectory
        $environment = '<env name="LIBRARIAN_LAN_HOST" value="{0}"/><env name="LIBRARIAN_LAN_IP" value="{1}"/><env name="LIBRARIAN_CLIENT_NETWORK" value="{2}"/><env name="LIBRARIAN_CADDY_DATA" value="{3}"/>' -f (Xml $Hostname),(Xml $LanAddress),(Xml $ClientNetwork),(Xml ((Join-Path $StateDirectory 'caddy').Replace('\','/')))
        $depend = '<depend>LibrarianApi</depend>'
        $workingDirectory = $OutputDirectory
    }
    $config = @"
<service>
  <id>$name</id>
  <name>$name</name>
  <description>Librarian authenticated LAN search ($name)</description>
  <executable>$(Xml $exe)</executable>
  <arguments>$(Xml $arguments)</arguments>
  <workingdirectory>$(Xml $workingDirectory)</workingdirectory>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <stoptimeout>90 sec</stoptimeout>
  <onfailure action="restart" delay="15 sec"/>
  <resetfailure>1 hour</resetfailure>
  <logpath>$(Xml $logPath)</logpath>
  <log mode="roll-by-time"><pattern>yyyyMMdd</pattern></log>
  <serviceaccount><domain>NT SERVICE</domain><user>$name</user></serviceaccount>
  $depend
  $environment
</service>
"@
    [xml]$validated = $config
    [IO.File]::WriteAllText((Join-Path $OutputDirectory "$name.xml"), $validated.OuterXml, [Text.UTF8Encoding]::new($false))
}
Copy-Item -LiteralPath (Join-Path $Repo 'config\Caddyfile.example') -Destination (Join-Path $OutputDirectory 'Caddyfile')
Write-Host "WinSW configuration prepared: $OutputDirectory"
