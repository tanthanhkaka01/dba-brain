$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
try {
    $os = Get-CimInstance Win32_OperatingSystem
    $cs = Get-CimInstance Win32_ComputerSystem
    $boot = $os.LastBootUpTime
    $uptimeSeconds = [int64]((Get-Date) - $boot).TotalSeconds
    $tz = (Get-TimeZone).Id
    $rows = @(
        New-MetricRow 'os_name' $os.Caption 'text' 'OK' ("os_family=Windows, edition=" + $os.Caption + ", version=" + $os.Version + ", build=" + $os.BuildNumber + ", architecture=" + $os.OSArchitecture)
        New-MetricRow 'hostname' $cs.DNSHostName 'text' 'OK' ("domain=" + $cs.Domain + ", manufacturer=" + $cs.Manufacturer + ", model=" + $cs.Model)
        New-MetricRow 'timezone' $tz 'text' 'OK' ("timezone=" + $tz)
        New-MetricRow 'last_boot_time' ($boot.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')) 'timestamp' 'OK' ("uptime_seconds=" + $uptimeSeconds)
    )
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'os_info' 'UNKNOWN' 'text' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
