<#
.SYNOPSIS
  File the PDFs under their primary subject (config/subjects.yaml): <sources>\<Area>\<Subject>\<file>.
.DESCRIPTION
  Reads <data>\reports\move-plan.csv (scripts\run.ps1 subjects --plan writes it). Without -Apply it
  only shows what would move. With -Apply it moves the files, logs every move to
  <data>\reports\move-log-<time>.csv, and removes the folders the move left empty (only those).
  -Undo <log> moves them back.
  Rows marked 'conflict' (target name taken) are never moved.
  Each move is appended to the log as it happens, so the log is complete even if the run stops;
  a file that cannot be moved (open in a viewer, say) is logged as 'error: ...' and skipped.
  After -Apply / -Undo, path patterns in config\*.txt (--select lists) are rewritten to the new
  folders (python -m librarian.relink); patterns it cannot map are printed to check by hand.

  Afterwards run:  scripts\run.ps1 inventory
  Inventory finds each moved file by its SHA-256 and updates its path; pages, sections and vectors
  are keyed by SHA-256, so nothing is re-extracted or re-embedded.
.EXAMPLE
  scripts\run.ps1 subjects --plan
  scripts\move-to-subjects.ps1                 # dry run
  scripts\move-to-subjects.ps1 -Apply
  scripts\move-to-subjects.ps1 -Undo D:\...\reports\move-log-20261105-1200.csv
#>
param([switch]$Apply, [string]$Undo, [string]$Plan)
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot

function Get-ConfigPath([string]$key, [string]$default) {
    $toml = Join-Path $Repo "config\local.toml"
    $envv = [Environment]::GetEnvironmentVariable("LIBRARIAN_$($key.ToUpper())")
    if ($envv) { return $envv }
    if (Test-Path $toml) {
        foreach ($line in Get-Content $toml) {
            if ($line -match "^\s*$key\s*=\s*'([^']*)'") { return $Matches[1] }
            if ($line -match "^\s*$key\s*=\s*`"([^`"]*)`"") { return $Matches[1] }
        }
    }
    return $default
}
$Sources = Get-ConfigPath "sources" "D:\Readings\Sources"
$Data = Get-ConfigPath "data" "D:\Readings\librarian"
$Reports = Join-Path $Data "reports"
$Py = Join-Path $Repo ".venv\Scripts\python.exe"

function Move-Logged([string]$from, [string]$to, [string]$sha, [string]$log) {
    try { $res = Move-One $from $to } catch {
        $res = "error: $($_.Exception.Message)"
        # Move-One never moves onto an existing name, so a destination that exists now while the
        # source is still there is a partial copy from the failed move: remove it.
        $src = Join-Path $Sources ($from -replace "/", "\")
        $dst = Join-Path $Sources ($to -replace "/", "\")
        if ((Test-Path -LiteralPath $src) -and (Test-Path -LiteralPath $dst)) {
            Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue
            Remove-EmptyDirs @($to)
            $res += " (partial copy removed)"
        }
    }
    $row = [pscustomobject]@{ result = $res; from = $from; to = $to; sha = $sha }
    $row | Export-Csv -NoTypeInformation -Encoding UTF8 -Append -Path $log
    return $row
}

function Update-Lists([string]$log, [switch]$Reverse) {
    if (-not (Test-Path $Py)) { Write-Host "No .venv: run later: python -m librarian.relink $log" -ForegroundColor Yellow; return }
    $relinkArgs = @("-m", "librarian.relink", $log, "--dir", (Join-Path $Repo "config"))
    if ($Reverse) { $relinkArgs += "--reverse" }
    & $Py @relinkArgs
}

function Move-One([string]$from, [string]$to) {
    $src = Join-Path $Sources ($from -replace "/", "\")
    $dst = Join-Path $Sources ($to -replace "/", "\")
    if (-not (Test-Path -LiteralPath $src)) { return "missing" }
    if (Test-Path -LiteralPath $dst) { return "exists" }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
    Move-Item -LiteralPath $src -Destination $dst
    return "moved"
}

function Remove-EmptyDirs([string[]]$relFiles) {
    # Only the folders this run emptied: the old folders of moved files, and their parents up to Sources.
    $root = (Resolve-Path -LiteralPath $Sources).Path.TrimEnd("\")
    $dirs = $relFiles | ForEach-Object { Split-Path -Parent (Join-Path $Sources ($_ -replace "/", "\")) } | Sort-Object -Unique
    foreach ($d in ($dirs | Sort-Object Length -Descending)) {
        while ($d -and $d.TrimEnd("\") -ne $root -and (Test-Path -LiteralPath $d) -and
               -not (Get-ChildItem -LiteralPath $d -Force)) {
            # A folder still open somewhere (Explorer, a shell, a sync client) can't be removed: report
            # it and carry on, so the summary and the list relink still run.
            try { Remove-Item -LiteralPath $d -ErrorAction Stop } catch {
                Write-Host "  could not remove empty folder (delete it by hand): $d" -ForegroundColor Yellow
                break
            }
            $d = Split-Path -Parent $d
        }
    }
}

if ($Undo) {
    $rows = @(Import-Csv $Undo | Where-Object { $_.result -eq "moved" })
    [array]::Reverse($rows)
    $undoLog = $Undo -replace "\.csv$", "-undo-$(Get-Date -Format 'yyyyMMdd-HHmm').csv"
    $back = foreach ($r in $rows) { Move-Logged $r.to $r.from $r.sha $undoLog }
    $done = @($back | Where-Object { $_.result -eq "moved" })
    Remove-EmptyDirs ($done | ForEach-Object { $_.from })
    Write-Host "Moved $($done.Count) of $($rows.Count) files back. Log: $undoLog" -ForegroundColor Green
    @($back | Where-Object { $_.result -ne "moved" }) | ForEach-Object { Write-Host "  not moved back ($($_.result)): $($_.from)" -ForegroundColor Yellow }
    if ($done.Count) { Update-Lists $undoLog }
    Write-Host "Now run: scripts\run.ps1 inventory" -ForegroundColor Cyan
    return
}

if (-not $Plan) { $Plan = Join-Path $Reports "move-plan.csv" }
if (-not (Test-Path $Plan)) { throw "No plan at $Plan : run scripts\run.ps1 subjects --plan first." }
$rows = @(Import-Csv $Plan)
$moves = @($rows | Where-Object { $_.status -eq "move" })
$conflicts = @($rows | Where-Object { $_.status -ne "move" })
Write-Host "Sources: $Sources"
Write-Host ("{0} files to move, {1} conflicts (skipped)" -f $moves.Count, $conflicts.Count)
$moves | Group-Object subject | Sort-Object Count -Descending | Select-Object -First 15 |
    ForEach-Object { Write-Host ("  {0,4}  -> {1}" -f $_.Count, $_.Name) }
foreach ($c in $conflicts) { Write-Host "  conflict: $($c.from) -> $($c.to)" -ForegroundColor Yellow }

if (-not $Apply) {
    Write-Host "Dry run: nothing moved. Re-run with -Apply to move the files." -ForegroundColor Cyan
    return
}

$log = Join-Path $Reports ("move-log-{0}.csv" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
Write-Host "Logging each move to $log" -ForegroundColor Cyan
$out = foreach ($r in $moves) { Move-Logged $r.from $r.to $r.sha $log }
Remove-EmptyDirs (@($out | Where-Object { $_.result -eq "moved" }) | ForEach-Object { $_.from })
$moved = @($out | Where-Object { $_.result -eq "moved" }).Count
Write-Host "Moved $moved of $($moves.Count) files. Log (for -Undo): $log" -ForegroundColor Green
@($out | Where-Object { $_.result -ne "moved" }) | ForEach-Object { Write-Host "  not moved ($($_.result)): $($_.from)" -ForegroundColor Yellow }
if ($moved) { Update-Lists $log }
Write-Host "Now run: scripts\run.ps1 inventory" -ForegroundColor Cyan
