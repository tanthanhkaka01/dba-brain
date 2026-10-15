param(
  [string]$DbSrePayloadJsonBase64,

  [string[]]$VmName,
  [ValidateSet("shared", "mysql", "postgresql", "sqlserver", "oracle_rac", "oracle_dg", "all")]
  [string]$Group = "all",
  [switch]$SkipHardStop,
  [int]$SoftShutdownTimeoutSec = 120
)

. "$PSScriptRoot\vmware-common.ps1"

Assert-VmrunAvailable

if ($VmName -and $VmName.Count -gt 0) {
  $Inventory = Get-VmInventoryByName -VmName $VmName
} else {
  $Inventory = Get-VmInventory -Group $Group
}

$ToStop = @($Inventory | Where-Object { Test-VmIsRunning -VmxPath $_.VmxPath })

if ($ToStop.Count -eq 0) {
  Write-Host "No running VMs found in the specified group/list."
  exit 0
}

Write-Host "Requesting graceful shutdown for $($ToStop.Count) VM(s)..."

$SoftStopFailed = @()
foreach ($Vm in $ToStop) {
  Write-Host "Soft stopping $($Vm.Name) ..."
  # vmrun stop soft can hang for minutes when VMware Tools is absent — cap at 30s.
  $proc = [System.Diagnostics.Process]::new()
  $proc.StartInfo.FileName = $Vmrun
  $proc.StartInfo.Arguments = "stop `"$($Vm.VmxPath)`" soft"
  $proc.StartInfo.UseShellExecute = $false
  $proc.StartInfo.RedirectStandardError = $true
  $proc.StartInfo.RedirectStandardOutput = $true
  $null = $proc.Start()
  $finished = $proc.WaitForExit(30000)
  if (-not $finished -or $proc.ExitCode -ne 0) {
    if (-not $finished) { try { $proc.Kill() } catch {} }
    $errText = $proc.StandardError.ReadToEnd().Trim()
    if ($errText) { Write-Warning "Soft stop request failed for $($Vm.Name): $errText" }
    else          { Write-Warning "Soft stop request failed for $($Vm.Name) (timed out or non-zero exit)" }
    $SoftStopFailed += $Vm.Name
  }
  $proc.Dispose()
}

# Skip the graceful-shutdown wait when ALL soft stops failed immediately (e.g. no VMware Tools).
# In that case, waiting $SoftShutdownTimeoutSec seconds is pointless — go straight to force stop.
if ($SoftStopFailed.Count -lt $ToStop.Count) {
  $Deadline = (Get-Date).AddSeconds($SoftShutdownTimeoutSec)
  do {
    Start-Sleep -Seconds 5
    $ToStop = @($ToStop | Where-Object { Test-VmIsRunning -VmxPath $_.VmxPath })
  } while ($ToStop.Count -gt 0 -and (Get-Date) -lt $Deadline)
}

if ($ToStop.Count -eq 0) {
  Write-Host "All targeted VMs are powered off." -ForegroundColor Green
  exit 0
}

if ($SkipHardStop) {
  Write-Warning "SkipHardStop is set. Some VMs may still be running: $($ToStop.Name -join ', ')"
  exit 1
}

foreach ($Vm in $ToStop) {
  Write-Host "Force stopping $($Vm.Name) ..."
  $Output = & $Vmrun stop $Vm.VmxPath hard 2>&1
  $ExitCode = $LASTEXITCODE
  if ($ExitCode -ne 0) {
    Write-Warning "Hard stop failed for $($Vm.Name): $($Output -join ' ')"
  }
}

$StillRunning = @($Inventory | Where-Object { Test-VmIsRunning -VmxPath $_.VmxPath })
if ($StillRunning.Count -gt 0) {
  Write-Warning "VMs still running: $($StillRunning.Name -join ', ')"
  exit 1
}

Write-Host "All targeted VMs are powered off." -ForegroundColor Green
