$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
try {
    # Physical memory (what an operator reads as "RAM used"), not committed bytes.
    $os = Get-CimInstance Win32_OperatingSystem
    $totalGb = [math]::Round([double]$os.TotalVisibleMemorySize / 1MB, 2)
    $availableGb = [math]::Round([double]$os.FreePhysicalMemory / 1MB, 2)
    $usedGb = [math]::Round($totalGb - $availableGb, 2)
    $usedPercent = if ($totalGb -gt 0) { [math]::Round(($usedGb / $totalGb) * 100, 2) } else { 0 }
    $status = if ($usedPercent -ge 95) { 'CRITICAL' } elseif ($usedPercent -ge 85) { 'WARN' } else { 'OK' }
    $rows = @(New-MetricRow 'memory_usage' $usedPercent 'percent' $status "Memory usage is $usedPercent percent. total_gb=$totalGb, used_gb=$usedGb, available_gb=$availableGb")

    # The percentage alone cannot be charted against capacity: 60% of 8 GB and 60% of 512 GB are
    # different problems. The absolute figure is its own row so it has its own series.
    $usedMb = [math]::Round($usedGb * 1024, 0)
    $totalMb = [math]::Round($totalGb * 1024, 0)
    $rows += New-MetricRow 'memory_used_mb' $usedMb 'MB' 'OK' "Memory used is $usedMb MB of $totalMb MB."

    # Pagefile is the Windows equivalent of swap.
    try {
        $page = @(Get-CimInstance Win32_PageFileUsage)
        if ($page.Count -gt 0) {
            $pageTotalGb = [math]::Round((($page | Measure-Object -Property AllocatedBaseSize -Sum).Sum) / 1KB, 2)
            $pageUsedGb = [math]::Round((($page | Measure-Object -Property CurrentUsage -Sum).Sum) / 1KB, 2)
            $pagePercent = if ($pageTotalGb -gt 0) { [math]::Round(($pageUsedGb / $pageTotalGb) * 100, 2) } else { 0 }
            $pageStatus = if ($pagePercent -ge 90) { 'WARN' } else { 'OK' }
            $rows += New-MetricRow 'pagefile_usage' $pagePercent 'percent' $pageStatus "Pagefile usage is $pagePercent percent. swap_total_gb=$pageTotalGb, swap_used_gb=$pageUsedGb"
        }
        else {
            $rows += New-MetricRow 'pagefile_usage' '0' 'percent' 'OK' 'swap_total_gb=0, swap_used_gb=0; no pagefile is configured.'
        }
    }
    catch {
        $rows += New-MetricRow 'pagefile_usage' 'UNKNOWN' 'percent' 'UNKNOWN' $_.Exception.Message
    }
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'memory_usage' 'UNKNOWN' 'percent' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
