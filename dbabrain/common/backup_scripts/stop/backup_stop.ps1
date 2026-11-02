# Stop one running backup on this Windows host (0.34.0, 1.238) - common.cli backup-stop runs it.
#
# Order matters. On a production SQL Server (2026-10-29) the engine session doing the backup was killed first;
# the script driving it reconnected a second later and backed up the next database, 548 GB, for
# another hour. It was stopped by ending the run's WinRM host (wsmprovhost) and its sqlcmd. So:
#   1. the run's own process tree - under the pid the backup script wrote to
#      %TEMP%\dbabrain_runs\<tag>.pid - so nothing is left to start the next database;
#   2. then, for SQL Server, every session the run opened: its sqlcmd named each one
#      host_name = dbabrain:<tag>, so a native job's backup on the same instance is never touched;
#   3. then what is left, said.
#
# A pid is ended only when that process started before the pid file was written: a file left behind
# by a run that died must not end whatever process has that number now. PowerShell 2.0 (Server 2008
# R2) has no Get-CimInstance; Win32_Process through Get-WmiObject works on every version here.
#
# Env: DBABRAIN_RUN_TAG (required), DB_TYPE, and for sqlserver MSSQL_SERVER, MSSQL_USER,
# MSSQL_PASSWORD (Windows authentication when MSSQL_USER is empty) - the run's own login.
# Prints STOP<TAB>key=value lines, then RESULT=ok.

function Die($reason) {
    $Host.UI.WriteErrorLine("RESULT=error reason=$reason")
    exit 2
}

$tag = "$($env:DBABRAIN_RUN_TAG)"
if (-not $tag -or $tag -notmatch '^[A-Za-z0-9_.-]+$') {
    Die 'DBABRAIN_RUN_TAG is missing or holds a character outside A-Z a-z 0-9 _ . -'
}

# Children first, then the parent: a parent ended first would orphan its children.
function Get-Tree($id) {
    $tree = @()
    foreach ($child in @(Get-WmiObject Win32_Process -Filter "ParentProcessId=$id" -ErrorAction SilentlyContinue)) {
        $tree += Get-Tree $child.ProcessId
    }
    return $tree + @($id)
}

$runFile = Join-Path (Join-Path ([IO.Path]::GetTempPath()) 'dbabrain_runs') "$tag.pid"
if (Test-Path -LiteralPath $runFile) {
    $text = "$(Get-Content -LiteralPath $runFile | Select-Object -First 1)".Trim()
    $runPid = 0
    [void][int]::TryParse($text, [ref]$runPid)
    "STOP`tpid=$runPid"
    $written = (Get-Item -LiteralPath $runFile).LastWriteTime
    $proc = if ($runPid -gt 0 -and $runPid -ne $PID) { Get-Process -Id $runPid -ErrorAction SilentlyContinue } else { $null }
    # A start time that cannot be read is not "before": PowerShell holds $null -le <date> true, and
    # a process this login cannot read (pid 4, System) went down that branch in the first test.
    if ($proc -and $proc.StartTime -and $proc.StartTime -le $written) {
        foreach ($p in (Get-Tree $runPid)) {
            if ($p -eq $PID) { continue }
            & taskkill.exe /PID $p /F > $null 2>&1
            if ($LASTEXITCODE -eq 0) { "STOP`tkilled=$p" }
        }
        Start-Sleep -Seconds 2
        if (Get-Process -Id $runPid -ErrorAction SilentlyContinue) { "STOP`talive=1" } else { "STOP`talive=0" }
    } else {
        # Not running, or the number belongs to another process now.
        "STOP`talive=0"
    }
    Remove-Item -LiteralPath $runFile -Force -ErrorAction SilentlyContinue
} else {
    "STOP`tpid_file=missing"
}

if ("$($env:DB_TYPE)" -eq 'sqlserver') {
    $server = if ($env:MSSQL_SERVER) { $env:MSSQL_SERVER } else { '.' }
    if ($env:MSSQL_PASSWORD) { $env:SQLCMDPASSWORD = $env:MSSQL_PASSWORD }
    $login = if ($env:MSSQL_USER) { @('-U', $env:MSSQL_USER) } else { @('-E') }
    $sqlcmd = Get-Command sqlcmd.exe -ErrorAction SilentlyContinue
    if (-not $sqlcmd) {
        foreach ($candidate in @(
            "$env:ProgramFiles\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\sqlcmd.exe",
            "$env:ProgramFiles\Microsoft SQL Server\Client SDK\ODBC\130\Tools\Binn\sqlcmd.exe",
            "${env:ProgramFiles(x86)}\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\sqlcmd.exe")) {
            if (Test-Path -LiteralPath $candidate) { $sqlcmd = Get-Item -LiteralPath $candidate; break }
        }
    }
    if (-not $sqlcmd) { Die 'sqlcmd.exe not found: the run''s sessions cannot be ended.' }
    $sqlcmdPath = if ($sqlcmd.Source) { $sqlcmd.Source } else { $sqlcmd.FullName }

    # -C exists in sqlcmd 18+ and is required there; older sqlcmd refuses it. Tried both ways.
    function Invoke-Rows($query) {
        foreach ($trust in @(@('-C'), @())) {
            $sqlArgs = @('-S', $server, '-b', '-h', '-1', '-W', '-Q', "SET NOCOUNT ON; $query") + $trust + $login
            $out = & $sqlcmdPath @sqlArgs 2>&1
            if ($LASTEXITCODE -eq 0) { return @($out | ForEach-Object { "$_".Trim() } | Where-Object { $_ }) }
        }
        Die "cannot log in to '$server' to end the run's sessions."
    }
    $find = "SELECT CAST(s.session_id AS varchar(10)) + '|' + ISNULL(DB_NAME(r.database_id), '') " +
            "FROM sys.dm_exec_sessions s LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id " +
            "WHERE s.host_name = 'dbabrain:$tag' AND s.session_id <> @@SPID;"
    foreach ($row in (Invoke-Rows $find)) {
        $spid, $db = $row.Split('|', 2)
        if ($spid -notmatch '^\d+$') { continue }
        [void](Invoke-Rows "KILL $spid;")
        "STOP`tsession_killed=$spid`tdb=$db"
    }
    Start-Sleep -Seconds 3
    foreach ($row in (Invoke-Rows $find)) {
        $spid, $db = $row.Split('|', 2)
        if ($spid -notmatch '^\d+$') { continue }
        "STOP`tsession_left=$spid`tdb=$db"
    }
}

'RESULT=ok'
