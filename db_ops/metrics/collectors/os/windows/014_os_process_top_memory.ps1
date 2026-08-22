$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
try {
    $topN = 5
    if ($env:OS_TOP_N) { $topN = [int]$env:OS_TOP_N }
    $totalKb = [double](Get-CimInstance Win32_OperatingSystem).TotalVisibleMemorySize
    $totalMb = if ($totalKb -gt 0) { $totalKb / 1KB } else { 0 }

    # Group by process name: an application that runs many worker processes (an AOS host is
    # the case in point) is one line, not N lines each below the reporting threshold.
    $grouped = Get-Process | Group-Object ProcessName | ForEach-Object {
        [pscustomobject]@{
            Name = $_.Name
            MemoryMb = [math]::Round((($_.Group | Measure-Object -Property WorkingSet64 -Sum).Sum) / 1MB, 2)
            Count = $_.Count
        }
    }
    $rows = @()
    foreach ($proc in ($grouped | Sort-Object MemoryMb -Descending | Select-Object -First $topN)) {
        $memoryPercent = if ($totalMb -gt 0) { [math]::Round(($proc.MemoryMb / $totalMb) * 100, 2) } else { 0 }
        $status = if ($memoryPercent -ge 60) { 'WARN' } else { 'OK' }
        $rows += New-MetricRow $proc.Name $proc.MemoryMb 'MB' $status ("process=" + $proc.Name + ", memory_mb=" + $proc.MemoryMb + ", memory_percent=$memoryPercent, process_count=" + $proc.Count)
    }
    if (-not $rows) { $rows = @(New-MetricRow 'top_memory' '0' 'MB' 'OK' 'No process was returned.') }
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'top_memory' 'UNKNOWN' 'MB' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
