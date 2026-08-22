$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
try {
    $top = Get-Process | Sort-Object CPU -Descending | Select-Object -First 5 Id, ProcessName, CPU, WorkingSet64
    $payload = @($top | ForEach-Object {
        [ordered]@{
            pid = $_.Id
            name = $_.ProcessName
            cpu_seconds = [math]::Round([double]($_.CPU), 2)
            memory_mb = [math]::Round([double]($_.WorkingSet64) / 1MB, 2)
        }
    }) | ConvertTo-Json -Depth 5 -Compress
    ConvertTo-Json -InputObject @((New-MetricRow 'top_processes' $payload 'json' 'OK' 'Top processes by accumulated CPU seconds.')) -Depth 5 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'top_processes' 'UNKNOWN' 'json' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
