# OS_CPU_COUNTERS: the CPU this host - and the database process on it - has spent since boot, raw.
#
# OS_CPU_USAGE is three one-second samples once an hour: right for "is it busy now" and no use for
# "how busy was the last hour", which is what the server page's Workload table asks. These are the
# cumulative totals Windows keeps, so the report can subtract two collections and get the interval
# exactly (dbabrain.lib.interval_rates). Nothing here is graded.
#
#   host_busy_ms / host_idle_ms - GetSystemTimes, summed over every logical processor. Kernel time
#     includes idle, so busy is kernel + user - idle.
#   db_cpu_ms - kernel + user time of the process listening on the target's port, when that
#     process is sqlservr.exe. SQL Server is one process on Windows, so this is all of it, on every
#     edition: Standard and Express keep no CPU total of their own (072 says why).
#
# The process is found by port rather than by service name: on a failover cluster the instance
# name in the inventory is the network name, and a passive node runs no sqlservr at all - which is
# the one case db_cpu_ms is left out, with db_process saying so. Only SQL Server: PostgreSQL on
# Windows is a process per connection, and an exited one takes its time with it.
$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
function Get-Instance($Class, $Filter) {
    if ($Filter) { return Get-CimInstance -ClassName $Class -Filter $Filter }
    return Get-CimInstance -ClassName $Class
}
function Format-Stamp($Value) {
    return ([datetime]$Value).ToString('yyyy-MM-dd HH:mm:ss')
}
function Get-ListeningPid([int]$Port) {
    try {
        $listener = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1)
        if ($listener.Count -gt 0) { return [int]$listener[0].OwningProcess }
    }
    catch { }
    # Get-NetTCPConnection is 2012 and later; netstat shows the owning PID on every version without
    # elevation.
    foreach ($line in (netstat -ano -p tcp)) {
        $fields = @($line.Trim() -split '\s+')
        if ($fields.Count -ge 5 -and $fields[3] -eq 'LISTENING' -and $fields[1] -match ":$Port$") {
            return [int]$fields[4]
        }
    }
    return $null
}
try {
    if (-not ('DbabrainSystemTimes' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class DbabrainSystemTimes {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetSystemTimes(out long idle, out long kernel, out long user);
    public static long[] Read() {
        long idle, kernel, user;
        if (!GetSystemTimes(out idle, out kernel, out user)) {
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        }
        return new long[] { idle, kernel, user };
    }
}
'@
    }
    $times = [DbabrainSystemTimes]::Read()
    # 100-nanosecond units; 10,000 of them are a millisecond.
    $idleMs = [math]::Round($times[0] / 10000)
    $busyMs = [math]::Round(($times[1] - $times[0] + $times[2]) / 10000)
    $os = Get-Instance 'Win32_OperatingSystem' $null
    $since = Format-Stamp $os.LastBootUpTime
    $logical = (Get-Instance 'Win32_ComputerSystem' $null).NumberOfLogicalProcessors

    $dbProcess = 'none'
    $processSince = 'none'
    $dbCpuMs = $null
    $port = 0
    [void][int]::TryParse([string]$env:DBABRAIN_DB_PORT, [ref]$port)
    if ($env:DBABRAIN_DB_TYPE -eq 'sqlserver' -and $port -gt 0) {
        $owner = Get-ListeningPid $port
        if (-not $owner) {
            $dbProcess = "none: nothing listens on port $port on this host"
        }
        else {
            $proc = Get-Instance 'Win32_Process' "ProcessId=$owner"
            if (-not $proc) {
                $dbProcess = "none: pid $owner on port $port is gone"
            }
            elseif ($proc.Name -ne 'sqlservr.exe') {
                $dbProcess = "none: port $port belongs to $($proc.Name) pid $owner"
            }
            elseif ($null -eq $proc.KernelModeTime -or $null -eq $proc.UserModeTime) {
                $dbProcess = "none: the CPU times of sqlservr.exe pid $owner are not readable by this login"
            }
            else {
                $dbCpuMs = [math]::Round(([double]$proc.KernelModeTime + [double]$proc.UserModeTime) / 10000)
                $processSince = Format-Stamp $proc.CreationDate
                $dbProcess = "sqlservr.exe pid $owner on port $port"
            }
        }
    }

    $tail = "counters_since=$since, logical_cpus=$logical, process_since=$processSince, db_process=$dbProcess, source=GetSystemTimes, note=cumulative_since_boot_subtract_two_collections"
    $rows = @(
        New-MetricRow 'host_busy_ms' $busyMs 'ms' 'OK' "value=$busyMs, $tail"
        New-MetricRow 'host_idle_ms' $idleMs 'ms' 'OK' "value=$idleMs, $tail"
    )
    if ($null -ne $dbCpuMs) {
        $rows += New-MetricRow 'db_cpu_ms' $dbCpuMs 'ms' 'OK' "value=$dbCpuMs, $tail"
    }
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'host_busy_ms' 'UNKNOWN' 'ms' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
