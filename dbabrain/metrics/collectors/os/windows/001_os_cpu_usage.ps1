$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{
        metric_item = [string]$Item
        metric_value = [string]$Value
        metric_unit = [string]$Unit
        status = [string]$Status
        message = [string]$Message
    }
}
try {
    $samples = (Get-Counter '\Processor(_Total)\% Processor Time' -SampleInterval 1 -MaxSamples 3).CounterSamples
    $cpuUsage = [math]::Round((($samples | Measure-Object CookedValue -Average).Average), 2)
    $status = if ($cpuUsage -ge 90) { 'CRITICAL' } elseif ($cpuUsage -ge 80) { 'WARN' } else { 'OK' }

    $cpus = @(Get-CimInstance Win32_Processor)
    $model = ($cpus | Select-Object -First 1).Name
    $sockets = $cpus.Count
    $cores = ($cpus | Measure-Object -Property NumberOfCores -Sum).Sum
    $logical = ($cpus | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
    $rows = @(New-MetricRow 'cpu_usage' $cpuUsage 'percent' $status "Average CPU usage is $cpuUsage percent. model=$model, sockets=$sockets, cores=$cores, logical_cpus=$logical")

    # Queue length is the Windows stand-in for load average: sustained > 2 threads per
    # logical CPU means runnable threads are waiting on the scheduler.
    try {
        $queue = [math]::Round([double]((Get-Counter '\System\Processor Queue Length').CounterSamples[0].CookedValue), 2)
        $queueStatus = if ($logical -gt 0 -and $queue -ge (2 * $logical)) { 'WARN' } else { 'OK' }
        $rows += New-MetricRow 'processor_queue_length' $queue 'threads' $queueStatus "Processor queue length is $queue over $logical logical CPUs."
    }
    catch {
        $rows += New-MetricRow 'processor_queue_length' 'UNKNOWN' 'threads' 'UNKNOWN' $_.Exception.Message
    }
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'cpu_usage' 'UNKNOWN' 'percent' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
