$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
try {
    $topN = 5
    if ($env:OS_TOP_N) { $topN = [int]$env:OS_TOP_N }
    $logical = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
    if (-not $logical -or $logical -lt 1) { $logical = 1 }

    # CPU% per process from a 1-second delta of each process's accumulated CPU time, divided
    # by the logical CPU count — the same 0-100 scale Task Manager shows.
    #
    # Not Get-Counter '\Process(*)\% Processor Time': on A1AAOS01 it fails outright ("The data
    # in one of the performance counter samples is not valid"), and per-process perf counters
    # are fragile across locales and stale instance names.
    # Not Get-Process.CPU on its own either: that is cumulative seconds since process start, so
    # a long-lived process ranks first regardless of what it is doing now.
    $before = @{}
    foreach ($proc in (Get-Process)) {
        if ($proc.CPU -ne $null) { $before[$proc.Id] = [double]$proc.CPU }
    }
    $elapsed = [System.Diagnostics.Stopwatch]::StartNew()
    Start-Sleep -Seconds 1
    $elapsed.Stop()
    $seconds = [math]::Max($elapsed.Elapsed.TotalSeconds, 0.001)

    $samples = @()
    foreach ($proc in (Get-Process)) {
        if ($proc.CPU -eq $null) { continue }
        $previous = if ($before.ContainsKey($proc.Id)) { $before[$proc.Id] } else { 0 }
        $delta = [double]$proc.CPU - $previous
        if ($delta -lt 0) { $delta = 0 }
        $samples += [pscustomobject]@{
            Name = $proc.ProcessName
            Id = $proc.Id
            CpuPercent = [math]::Round(($delta / $seconds / $logical) * 100, 2)
            MemoryMb = [math]::Round([double]$proc.WorkingSet64 / 1MB, 2)
        }
    }

    $rows = @()
    foreach ($sample in ($samples | Sort-Object CpuPercent -Descending | Select-Object -First $topN)) {
        $status = if ($sample.CpuPercent -ge 90) { 'WARN' } else { 'OK' }
        $rows += New-MetricRow $sample.Name $sample.CpuPercent 'percent' $status ("process=" + $sample.Name + ", pid=" + $sample.Id + ", cpu_percent=" + $sample.CpuPercent + ", memory_mb=" + $sample.MemoryMb + ", logical_cpus=$logical")
    }
    if (-not $rows) { $rows = @(New-MetricRow 'top_cpu' '0' 'percent' 'OK' 'No process is consuming CPU.') }
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'top_cpu' 'UNKNOWN' 'percent' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
