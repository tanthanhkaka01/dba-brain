$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
try {
    $rows = @()
    # BytesSentPersec/BytesReceivedPersec in the raw class are *cumulative counters*, not a rate,
    # despite the name. Two samples a second apart turn them into the throughput an operator
    # actually wants — a lifetime byte count says nothing about what the link is doing now.
    $before = @{}
    foreach ($stat in (Get-CimInstance Win32_PerfRawData_Tcpip_NetworkInterface)) {
        $before[$stat.Name] = $stat
    }
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    Start-Sleep -Seconds 1
    $watch.Stop()
    $seconds = [math]::Max($watch.Elapsed.TotalSeconds, 0.001)

    $stats = @{}
    foreach ($stat in (Get-CimInstance Win32_PerfRawData_Tcpip_NetworkInterface)) {
        $stats[$stat.Name] = $stat
    }
    # IPv4 address per interface index. Get-NetIPAddress is not reliable in every remote
    # runspace (it returned nothing on A1AAOS01, leaving the address empty), so the CIM
    # configuration class is the source and Get-NetIPAddress is only a fallback.
    $ipByIndex = @{}
    foreach ($config in (Get-CimInstance Win32_NetworkAdapterConfiguration | Where-Object { $_.IPEnabled })) {
        $ipv4 = @($config.IPAddress | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' -and $_ -notlike '169.254.*' })
        if ($ipv4.Count -gt 0) { $ipByIndex[[int]$config.InterfaceIndex] = ($ipv4 -join ' ') }
    }
    # Every adapter that is up, not only -Physical ones: where NICs are teamed (A1AAOS01) the
    # IP lives on the team adapter while the traffic counters live on its physical members, so
    # filtering to physical adapters reports the host as having no IP address at all.
    foreach ($adapter in (Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and $_.InterfaceType -ne 24 })) {
        $name = $adapter.Name
        $addresses = ''
        if ($ipByIndex.ContainsKey([int]$adapter.ifIndex)) {
            $addresses = $ipByIndex[[int]$adapter.ifIndex]
        }
        if (-not $addresses) {
            $addresses = (Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -ExpandProperty IPAddress) -join ' '
        }
        if (-not $addresses) { $addresses = 'unknown' }
        $speedMbps = if ($adapter.LinkSpeed) { [math]::Round([double]$adapter.Speed / 1000000, 0) } else { 0 }
        # Perf counter instance names replace '#', '/' and '\' in the adapter description.
        $key = ($adapter.InterfaceDescription -replace '[#\\/]', '_')
        $stat = $stats[$key]
        $sent = if ($stat) { [int64]$stat.BytesSentPersec } else { 0 }
        $received = if ($stat) { [int64]$stat.BytesReceivedPersec } else { 0 }
        $errors = if ($stat) { [int64]($stat.PacketsOutboundErrors + $stat.PacketsReceivedErrors) } else { 0 }
        $discards = if ($stat) { [int64]($stat.PacketsOutboundDiscarded + $stat.PacketsReceivedDiscarded) } else { 0 }
        $status = if ($errors -gt 0) { 'WARN' } else { 'OK' }
        $rows += New-MetricRow $name $addresses 'text' $status ("link=Up, speed_mbps=$speedMbps, bytes_sent=$sent, bytes_received=$received, errors=$errors, dropped=$discards")

        # Throughput now, from the one-second delta of those cumulative counters. Its own rows,
        # so each is a chartable series (the row above carries an IP address as its value).
        $old = $before[$key]
        if ($stat -and $old) {
            $sendMbps = [math]::Round(((([double]$stat.BytesSentPersec - [double]$old.BytesSentPersec) / $seconds) * 8) / 1000000, 2)
            $recvMbps = [math]::Round(((([double]$stat.BytesReceivedPersec - [double]$old.BytesReceivedPersec) / $seconds) * 8) / 1000000, 2)
            if ($sendMbps -lt 0) { $sendMbps = 0 }
            if ($recvMbps -lt 0) { $recvMbps = 0 }
            $rows += New-MetricRow "$name send" $sendMbps 'Mbps' 'OK' "$name is sending $sendMbps Mbps (link speed $speedMbps Mbps)."
            $rows += New-MetricRow "$name receive" $recvMbps 'Mbps' 'OK' "$name is receiving $recvMbps Mbps (link speed $speedMbps Mbps)."
        }
    }
    if (-not $rows) { $rows = @(New-MetricRow 'network' 'UNKNOWN' 'text' 'UNKNOWN' 'No network adapter is up.') }
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'network' 'UNKNOWN' 'text' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
