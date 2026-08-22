$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
try {
    # LastBootUpTime, not [Environment]::TickCount64: inside a remote PSRP runspace the tick
    # counter reports 0 (observed on A1AAOS01), which reads as "just rebooted".
    $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
    $uptime = [int64]((Get-Date) - $boot).TotalSeconds
    ConvertTo-Json -InputObject @((New-MetricRow 'uptime' $uptime 'seconds' 'OK' "System uptime is $uptime seconds. last_boot_time=$($boot.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))")) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'uptime' 'UNKNOWN' 'seconds' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
