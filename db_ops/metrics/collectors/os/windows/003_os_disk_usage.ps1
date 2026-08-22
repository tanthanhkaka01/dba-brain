$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}
try {
    $rows = @()
    foreach ($disk in [System.IO.DriveInfo]::GetDrives()) {
        if (-not $disk.IsReady) { continue }
        if ($disk.DriveType -ne [System.IO.DriveType]::Fixed) { continue }
        $size = [double]$disk.TotalSize
        $free = [double]$disk.AvailableFreeSpace
        if ($size -le 0) { continue }
        $usedPercent = [math]::Round((($size - $free) / $size) * 100, 2)
        $status = if ($usedPercent -ge 95) { 'CRITICAL' } elseif ($usedPercent -ge 85) { 'WARN' } else { 'OK' }
        $totalGb = [math]::Round($size / 1GB, 2)
        $freeGb = [math]::Round($free / 1GB, 2)
        $usedGb = [math]::Round(($size - $free) / 1GB, 2)
        $freePercent = [math]::Round(($free / $size) * 100, 2)
        $driveName = $disk.Name.TrimEnd('\')
        $rows += New-MetricRow $driveName $usedPercent 'percent' $status ("$driveName usage is $usedPercent percent. total_gb=$totalGb, used_gb=$usedGb, free_gb=$freeGb, free_percent=$freePercent, filesystem=" + $disk.DriveFormat + ", label=" + $disk.VolumeLabel)
    }
    if (-not $rows) { $rows = @(New-MetricRow 'disk_usage' 'UNKNOWN' 'percent' 'UNKNOWN' 'No fixed disks were found.') }

    # How busy the storage is, not just how full. Read from the raw perf counters and
    # differenced over one second: Get-Counter's cooked values are what failed outright on
    # A1AAOS01 ("the data in one of the performance counter samples is not valid"), and the raw
    # class is the same data without that fragility.
    try {
        # Every physical disk instance, not just "_Total": the per-disk queue lengths are what
        # make the total interpretable, and their count is what the queue threshold scales by.
        $before = Get-CimInstance Win32_PerfRawData_PerfDisk_PhysicalDisk
        $watch = [System.Diagnostics.Stopwatch]::StartNew()
        Start-Sleep -Seconds 1
        $after = Get-CimInstance Win32_PerfRawData_PerfDisk_PhysicalDisk
        $watch.Stop()
        $seconds = [math]::Max($watch.Elapsed.TotalSeconds, 0.001)

        $beforeTotal = @($before | Where-Object { $_.Name -eq '_Total' })[0]
        $afterTotal = @($after | Where-Object { $_.Name -eq '_Total' })[0]
        if (-not $afterTotal) { throw "PhysicalDisk _Total instance not found." }

        $readKbps = [math]::Round((([double]$afterTotal.DiskReadBytesPersec - [double]$beforeTotal.DiskReadBytesPersec) / $seconds) / 1KB, 2)
        $writeKbps = [math]::Round((([double]$afterTotal.DiskWriteBytesPersec - [double]$beforeTotal.DiskWriteBytesPersec) / $seconds) / 1KB, 2)
        if ($readKbps -lt 0) { $readKbps = 0 }
        if ($writeKbps -lt 0) { $writeKbps = 0 }
        $rows += New-MetricRow 'disk_read_kbps' $readKbps 'KB/s' 'OK' "Disk read throughput is $readKbps KB/s (all physical disks)."
        $rows += New-MetricRow 'disk_write_kbps' $writeKbps 'KB/s' 'OK' "Disk write throughput is $writeKbps KB/s (all physical disks)."

        $perDisk = @($after | Where-Object { $_.Name -ne '_Total' } |
            Sort-Object -Property @{ Expression = { [double]$_.CurrentDiskQueueLength } } -Descending)
        $diskCount = [math]::Max($perDisk.Count, 1)
        $queue = [math]::Round([double]$afterTotal.CurrentDiskQueueLength, 2)
        $avgPerDisk = [math]::Round($queue / $diskCount, 2)

        # The counter is a SUM over every physical disk, so a fixed number cannot be right for
        # every host: 16 outstanding requests is normal on an 8-disk array and a stalled host on
        # a single disk. The rule of thumb is ~2 outstanding requests per physical disk, so the
        # threshold scales with the disk count instead. Reported alongside the value, because
        # "queue = 16" tells an operator nothing without the disk count it was summed over.
        $warnAt = 2 * $diskCount
        $criticalAt = 4 * $diskCount
        $queueStatus = if ($queue -ge $criticalAt) { 'CRITICAL' } elseif ($queue -ge $warnAt) { 'WARN' } else { 'OK' }

        $breakdown = if ($perDisk.Count -gt 0) {
            ($perDisk | ForEach-Object { "$($_.Name)={0}" -f [math]::Round([double]$_.CurrentDiskQueueLength, 2) }) -join '; '
        } else { 'unavailable' }
        $rows += New-MetricRow 'disk_queue_length' $queue 'requests' $queueStatus (
            "Total outstanding IO requests across all $diskCount physical disk(s) is $queue " +
            "(avg $avgPerDisk per disk); one instantaneous sample, not an average over time. " +
            "Per disk: $breakdown. " +
            "physical_disks=$diskCount, avg_per_disk=$avgPerDisk, warn_at=$warnAt, critical_at=$criticalAt")
    }
    catch {
        # "Invalid class" is not this metric failing: it is the Windows performance-counter
        # registry on that host missing PerfDisk, which happens after a corrupted counter set and
        # is repaired once with `lodctr /R` (then restart the WMI service). Reporting it as
        # UNKNOWN made 192.0.2.113 raise the same warning on every poll for something no
        # amount of retrying will change, so it is named for what it is and logged rather than
        # alerted. Throughput and queue are genuinely unavailable until it is fixed - the row
        # says so instead of implying zero.
        $message = [string]$_.Exception.Message
        if ($message -match 'Invalid class|not found|0x80041010') {
            $rows += New-MetricRow 'disk_perf_counters' 'NOT_REGISTERED' 'status' 'LOGGING' (
                "The PerfDisk performance-counter class is not registered on this host, so disk " +
                "throughput and queue length cannot be read (volume free space above is " +
                "unaffected). This is a MONITORING gap on the host, not a disk fault: repair it " +
                "with 'lodctr /R' from an elevated prompt and restart the WMI service. " +
                "wmi_class=Win32_PerfRawData_PerfDisk_PhysicalDisk, error=$message")
        }
        else {
            $rows += New-MetricRow 'disk_queue_length' 'UNKNOWN' 'requests' 'UNKNOWN' $message
        }
    }
    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'disk_usage' 'UNKNOWN' 'percent' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
