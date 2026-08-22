$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
try {
    $names = @($env:OS_SERVICE_NAMES -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if (-not $names) { $names = @('WinRM', 'W32Time') }
    $rows = @()
    foreach ($name in $names) {
        # A configured entry may be the service name (AOS60$01), the display name, or a
        # wildcard pattern ("*Dynamics AX Object Server*") — an operator knows the display
        # name, and instance-numbered services are only addressable by pattern. Every match
        # becomes its own row, so a host running several AOS instances reports each one.
        $services = @(Get-Service -Name $name -ErrorAction SilentlyContinue)
        if ($services.Count -eq 0) {
            $services = @(Get-Service -DisplayName $name -ErrorAction SilentlyContinue)
        }
        if ($services.Count -eq 0) {
            $rows += New-MetricRow $name 'NOT_FOUND' 'status' 'WARN' "Service $name was not found."
            continue
        }
        foreach ($service in $services) {
            $status = if ($service.Status -eq 'Running') { 'OK' } else { 'CRITICAL' }
            $item = if ($service.DisplayName) { $service.DisplayName } else { $service.Name }
            $rows += New-MetricRow $item $service.Status 'status' $status "Service $item status is $($service.Status). service_name=$($service.Name), configured_as=$name"
        }
    }
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'service_status' 'UNKNOWN' 'status' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
