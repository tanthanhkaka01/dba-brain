$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
try {
    # Resolve w32tm by absolute path instead of relying on PATH: some remoting/SSH shells run
    # with a stripped environment (no PATH to System32, and even $env:SystemRoot empty), so a
    # bare 'w32tm' is "not recognized" and Join-Path on an empty env var fails. Use the .NET
    # OS API [Environment]::SystemDirectory (never empty) as the primary source, with a
    # hard-coded fallback and Sysnative for a 32-bit host process.
    $sysDir = [Environment]::SystemDirectory
    if (-not $sysDir) { $sysDir = 'C:\Windows\System32' }
    $w32tm = Join-Path $sysDir 'w32tm.exe'
    if (-not (Test-Path $w32tm -ErrorAction SilentlyContinue)) {
        $alt = 'C:\Windows\Sysnative\w32tm.exe'
        if (Test-Path $alt -ErrorAction SilentlyContinue) { $w32tm = $alt } else { $w32tm = 'w32tm' }
    }
    $output = & $w32tm /query /status 2>&1
    $exit = $LASTEXITCODE
    $status = if ($exit -eq 0) { 'OK' } else { 'WARN' }
    # Take the FIRST match and force it to a string before trimming.
    #
    # On Server-TAP this metric failed every two hours with "Method invocation failed because
    # [System.Object[]] does not contain a method named 'Trim'". The old expression trimmed
    # whatever Where-Object returned, and that is an array as soon as more than one line matches
    # — and `$output` here is a *mixed* Object[] because `2>&1` merges stderr in as ErrorRecord
    # objects, not strings. The precise shape that host produced was not reproducible from here,
    # so this does not try to handle one case: it removes the assumption entirely. Exactly one
    # element, coerced to string, then trimmed — no array can reach .Trim() whatever w32tm emits.
    $sourceLine = @($output | Where-Object { $_ -match '^Source:' } | Select-Object -First 1)
    $source = ([string]($sourceLine -join '')) -replace '^Source:\s*', ''
    $source = $source.Trim()
    if (-not $source) { $source = 'UNKNOWN' }
    # When w32tm fails there is no status block to parse, and reporting a bare
    # "time source is UNKNOWN" throws away the one thing that explains why — the tool's own
    # error text, which is already in $output because of the 2>&1 merge. Without it this metric
    # says "something is wrong with time sync" and leaves the operator to go and run w32tm by
    # hand to find out that, say, the service is stopped or the binary is not on PATH in a
    # WinRM session.
    if ($exit -ne 0 -or $source -eq 'UNKNOWN') {
        $detail = (($output | ForEach-Object { [string]$_ }) -join ' ').Trim()
        if ($detail.Length -gt 400) { $detail = $detail.Substring(0, 400) }
        if (-not $detail) { $detail = "w32tm produced no output (exit $exit)." }
        $message = "Windows time source is $source. w32tm exit=$exit; output: $detail"
    }
    else {
        $message = "Windows time source is $source."
    }
    ConvertTo-Json -InputObject @((New-MetricRow 'time_sync' $source 'status' $status $message)) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'time_sync' 'UNKNOWN' 'status' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
