$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}

# Probe order is the whole point of this collector, so it is stated once here.
#
# The check used to test 127.0.0.1 only, which cannot tell "nothing is listening" from "listening,
# but bound to the address clients do not use". Both answers read CLOSED, and on a front door only
# one of them is an outage. So the NIC address is probed first (DB_OPS_TARGET_HOST, injected from
# the inventory) and loopback is the fallback that *explains* a failure rather than causing one:
#
#   host open                  -> OPEN           OK        clients can reach it
#   host closed, loopback open -> LOOPBACK_ONLY  WARNING   serving, but not where clients knock
#   both closed                -> CLOSED         CRITICAL  nothing is listening
#
# LOOPBACK_ONLY is deliberately not CRITICAL: a service intentionally bound to loopback (a local
# agent, a tunnelled port) is configured that way, not broken, and turning that into a 🚨 on every
# such host is how a real CLOSED gets ignored.
function Test-PortOn($TargetHost, $Port) {
    try { return [bool](Test-NetConnection -ComputerName $TargetHost -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue) }
    catch { return $false }
}

# TCP says a socket accepted the connection. It does not say the listener is serving: an AOS that
# accepts on 443 and answers 503 to every request is indistinguishable from a healthy one at this
# layer. `443/https` adds a TLS handshake and one GET; `443/tls` stops after the handshake.
#
# A failure here is WARNING, never CRITICAL, and that is on purpose. TCP is the signal this metric
# has always been right about; TLS and HTTP bring their own ways to fail that have nothing to do
# with the service being down (client-certificate requirements, SNI, a proxy in the path, a
# protocol floor the collector host does not offer). Raise it once a given endpoint has proven the
# probe is reliable against it.
#
# The request is written down the same TLS stream by hand rather than through Invoke-WebRequest:
# `-SkipCertificateCheck` is PowerShell 6+, these hosts run Windows PowerShell 5.1, and the whole
# point of the probe is that a self-signed or expired certificate must not stop it from reporting
# what the listener answered. One connection, no cmdlet, no global certificate policy to restore.
function Test-PortHealth($TargetHost, $Port, $Scheme) {
    $result = [ordered]@{ ok = $true; detail = '' }
    $client = $null
    $stream = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        if (-not $client.ConnectAsync($TargetHost, $Port).Wait(5000)) {
            $result.ok = $false; $result.detail = 'tls_connect_timeout'
            return $result
        }
        $stream = New-Object System.Net.Security.SslStream($client.GetStream(), $false, { $true })
        $stream.AuthenticateAsClient($TargetHost)
        $expiry = ''
        if ($stream.RemoteCertificate) {
            $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($stream.RemoteCertificate)
            $expiry = ', cert_expires=' + $cert.NotAfter.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        }
        $result.detail = "tls_ok$expiry"
        if ($Scheme -ne 'https') { return $result }

        $stream.ReadTimeout = 10000
        $stream.WriteTimeout = 10000
        $request = "GET / HTTP/1.1`r`nHost: $TargetHost`r`nUser-Agent: db_ops-tcp-port-check`r`nConnection: close`r`n`r`n"
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($request)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::ASCII)
        $statusLine = $reader.ReadLine()
        if (-not $statusLine) {
            $result.ok = $false; $result.detail += '; http_no_response'
            return $result
        }
        # Any HTTP status proves the listener is serving - a 302 to a sign-in page or a 401 is a
        # working front door. Only 5xx says it accepted the request and could not answer it.
        $code = 0
        if ($statusLine -match '^HTTP/\d\.\d\s+(\d{3})') { $code = [int]$Matches[1] }
        if (-not $code) {
            $result.ok = $false; $result.detail += '; http_unparsable_status: ' + $statusLine.Trim()
            return $result
        }
        $result.detail += ", http_status=$code"
        if ($code -ge 500) { $result.ok = $false; $result.detail += ' (server error)' }
        return $result
    }
    catch {
        $result.ok = $false
        if (-not $result.detail) { $result.detail = 'tls_failed: ' + $_.Exception.Message }
        else { $result.detail += '; http_failed: ' + $_.Exception.Message }
        return $result
    }
    finally {
        if ($stream) { $stream.Dispose() }
        if ($client) { $client.Dispose() }
    }
}

try {
    $ports = @($env:OS_TCP_PORTS -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if (-not $ports) {
        # No ports configured for this target => nothing to check. Report a benign OK (not
        # UNKNOWN, which now surfaces as a WARNING) so an unconfigured target stays quiet. Set
        # metrics.collector_env.OS_TCP_PORTS (e.g. "1433") on the target to enable the check.
        ConvertTo-Json -InputObject @((New-MetricRow 'tcp_port_status' 'not_configured' 'status' 'OK' 'No OS_TCP_PORTS configured; TCP port check skipped for this target.')) -Depth 4 -Compress
        exit 0
    }
    $defaultHost = if ($env:DB_OPS_TARGET_HOST) { $env:DB_OPS_TARGET_HOST.Trim() } else { '127.0.0.1' }
    $rows = @()
    foreach ($entry in $ports) {
        # [host:]port[/scheme] - scheme is tcp (default), tls, or https.
        $spec = $entry
        $scheme = 'tcp'
        if ($spec -match '^(.*)/(tcp|tls|https)$') {
            $spec = $Matches[1]
            $scheme = $Matches[2]
        }
        $hostName = $defaultHost
        $portText = $spec
        if ($spec -match '^(.+):(\d+)$') {
            $hostName = $Matches[1]
            $portText = $Matches[2]
        }
        $port = [int]$portText

        $onHost = Test-PortOn $hostName $port
        $onLoopback = $false
        if (-not $onHost -and $hostName -ne '127.0.0.1') { $onLoopback = Test-PortOn '127.0.0.1' $port }

        if ($onHost) { $value = 'OPEN'; $status = 'OK' }
        elseif ($onLoopback) { $value = 'LOOPBACK_ONLY'; $status = 'WARNING' }
        else { $value = 'CLOSED'; $status = 'CRITICAL' }

        $message = "TCP port $hostName`:$port is $value."
        if ($value -eq 'LOOPBACK_ONLY') {
            $message = "TCP port $port is listening on 127.0.0.1 but not on $hostName, so nothing outside this host can reach it."
        }
        if ($onHost -and $scheme -ne 'tcp') {
            $health = Test-PortHealth $hostName $port $scheme
            $message += " probe=$scheme, " + $health.detail
            if (-not $health.ok) {
                # The socket is open, so this is not CLOSED - the value stays OPEN and only the
                # status moves. A reader sees "open but not serving", which is the finding.
                $status = 'WARNING'
            }
        }
        $rows += New-MetricRow "$hostName`:$port" $value 'status' $status $message
    }
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'tcp_port_status' 'UNKNOWN' 'status' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
