$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}

function Get-EventExcerpt($Event) {
    <#
      A short piece of what the event actually SAYS, so an alert names the fault instead of only
      counting it.

      `.Message` is empty for a classic event source whose message-resource DLL is not registered
      on the host - Event Viewer shows "The description for Event ID N cannot be found", and so did
      we. `Dynamics Server Azure` (the Dynamics AX AOS) is one: 26,000 error events on
      192.0.2.116-119 in 24h and the alert could say nothing about any of them. The text is
      still there, in EventData, which is what the fallback reads.

      Longest property under the cap, not the first: those events carry
      P0="Object Server Azure:" (a label), P1=the ODBC error, P2=the entire failing SQL statement
      (~10 KB), P3=the session. The error is the longest field that is still a sentence, and the
      cap is what keeps P2 out - one metric row must never carry a 10 KB query.

      Commas are stripped because a metric message is `key=value` pairs separated by commas
      (db_ops.common.interval_rates.message_fields); free text with commas in it invents fields.
    #>
    $text = [string]$Event.Message
    if (-not $text) {
        $candidates = @($Event.Properties |
            ForEach-Object { [string]$_.Value } |
            Where-Object { $_ -and $_.Trim().Length -ge 10 -and $_.Length -le 400 })
        if ($candidates.Count -gt 0) {
            $text = ($candidates | Sort-Object { $_.Length } -Descending | Select-Object -First 1)
        }
    }
    if (-not $text) { return 'none' }
    $text = ($text -replace '\s+', ' ') -replace ',', ';'
    $text = $text.Trim()
    if ($text.Length -gt 180) { $text = $text.Substring(0, 180) + '...' }
    return $text
}
try {
    $hours = 24
    if ($env:OS_EVENTLOG_HOURS) { $hours = [int]$env:OS_EVENTLOG_HOURS }
    # How many events to pull back per log. Everything below is derived from three facts - the
    # count, the newest timestamp, and the top 3 event ids - so reading the whole window was only
    # ever a way of getting them, never something the metric needed.
    #
    # Unbounded, it could not survive the case it exists for. On 2026-08-09 the four AX servers
    # 192.0.2.116-119 logged ~26,000 `Dynamics Server Azure` event 117 records in 24 hours;
    # `@(Get-WinEvent ...)` materialised all of them, `Group-Object` and `Sort-Object` walked them,
    # and the whole thing had to finish inside this metric's 60s timeout. It did not: 22 consecutive
    # `WinRM command timed out after 60 seconds` per host, and the event log metric went blind
    # precisely while the event log was screaming. It recovered only when the storm aged out of the
    # window.
    $maxEvents = 500
    if ($env:OS_EVENTLOG_MAX_EVENTS) { $maxEvents = [int]$env:OS_EVENTLOG_MAX_EVENTS }
    $logs = @('System', 'Application')
    if ($env:OS_EVENTLOG_NAMES) {
        $logs = @($env:OS_EVENTLOG_NAMES -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
    $since = (Get-Date).AddHours(-1 * $hours)
    $rows = @()
    foreach ($log in $logs) {
        # Level 1 = Critical, 2 = Error. Warnings are deliberately out of scope.
        # -MaxEvents returns the NEWEST first, so a truncated read still carries the most recent
        # events - the ones an operator is looking at - rather than an arbitrary slice.
        $events = @(Get-WinEvent -FilterHashtable @{ LogName = $log; Level = 1, 2; StartTime = $since } -MaxEvents $maxEvents -ErrorAction SilentlyContinue)
        $count = $events.Count
        $truncated = $count -ge $maxEvents
        $status = if ($count -eq 0) { 'OK' } elseif ($count -ge 20) { 'WARN' } else { 'LOGGING' }
        if ($count -eq 0) {
            $rows += New-MetricRow $log '0' 'events' 'OK' "No critical or error events in $log over the last $hours hours."
        }
        else {
            $top = $events | Group-Object Id | Sort-Object Count -Descending | Select-Object -First 3
            $detail = ($top | ForEach-Object {
                $sample = $_.Group[0]
                "event_id=" + $_.Name + " count=" + $_.Count + " source=" + $sample.ProviderName +
                    " text=" + (Get-EventExcerpt $sample)
            }) -join '; '
            $latest = $events[0].TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
            # `metric_value` stays a plain number, because that is what the chart and any
            # `warning_threshold` override parse - ">=500" would be dropped by both. The truncation
            # is announced beside it instead, the same split `execute_cursor_batches` uses for a
            # capped result set: the value is usable, and "500" is never mistaken for the whole
            # window because `truncated=yes` says otherwise in the message the reports quote.
            # It also warns that `top=` ranks the newest $maxEvents rather than the window.
            $note = if ($truncated) { ", truncated=yes, cap=$maxEvents, count_is_a_floor=yes (top ranks the newest $maxEvents only)" } else { ", truncated=no" }
            $rows += New-MetricRow $log $count 'events' $status "window_hours=$hours, latest=$latest, top=$detail$note"
        }
    }
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'eventlog' 'UNKNOWN' 'events' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
