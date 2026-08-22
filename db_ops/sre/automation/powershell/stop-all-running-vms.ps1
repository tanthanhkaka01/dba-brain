param(
  [string]$DbSrePayloadJsonBase64,

  [int]$SoftShutdownTimeoutSec = 120,
  [int]$SoftRequestTimeoutSec = 20,
  [switch]$SkipHardStop
)

. "$PSScriptRoot\vmware-common.ps1"

if (-not (Test-Path $Vmrun)) {
  throw "vmrun.exe not found at: $Vmrun"
}

function Get-RunningVmList {
  $Output = & $Vmrun list 2>&1
  $ExitCode = $LASTEXITCODE

  if ($ExitCode -ne 0) {
    throw "Failed to list running VMs. ExitCode=$ExitCode`n$($Output -join [Environment]::NewLine)"
  }

  return $Output |
    Select-Object -Skip 1 |
    ForEach-Object { $_.ToString().Trim() } |
    Where-Object { $_ }
}

$RunningVms = Get-RunningVmList

if (-not $RunningVms -or $RunningVms.Count -eq 0) {
  Write-Host "No running VMs found."
  return
}

Write-Host "Found $($RunningVms.Count) running VM(s)."

foreach ($VmxPath in $RunningVms) {
  Write-Host "Requesting graceful shutdown for: $VmxPath"
  $StopJob = Start-Job -ScriptBlock {
    param($VmrunPath, $TargetVmxPath)
    $CommandOutput = & $VmrunPath stop $TargetVmxPath soft 2>&1
    [PSCustomObject]@{
      ExitCode = $LASTEXITCODE
      Output   = @($CommandOutput)
    }
  } -ArgumentList $Vmrun, $VmxPath

  $Completed = Wait-Job -Job $StopJob -Timeout $SoftRequestTimeoutSec

  if ($null -eq $Completed) {
    Write-Warning "Soft shutdown request timed out after $SoftRequestTimeoutSec second(s) for: $VmxPath"
    Stop-Job -Job $StopJob | Out-Null
    Remove-Job -Job $StopJob | Out-Null
    continue
  }

  $Result = Receive-Job -Job $StopJob
  Remove-Job -Job $StopJob | Out-Null
  $ExitCode = $Result.ExitCode
  $Output = @($Result.Output)

  if ($ExitCode -ne 0) {
    Write-Warning "Soft shutdown request failed for: $VmxPath"
    $Output | ForEach-Object { Write-Warning $_ }
  } else {
    Write-Host "Soft shutdown signal sent for: $VmxPath"
  }
}

$Deadline = (Get-Date).AddSeconds($SoftShutdownTimeoutSec)
$StillRunning = @()

do {
  Start-Sleep -Seconds 5
  $StillRunning = Get-RunningVmList
} while ($StillRunning.Count -gt 0 -and (Get-Date) -lt $Deadline)

if ($StillRunning.Count -eq 0) {
  Write-Host "All VMs are powered off."
  return
}

Write-Warning "Still running after $SoftShutdownTimeoutSec second(s):"
$StillRunning | ForEach-Object { Write-Warning $_ }

if ($SkipHardStop) {
  Write-Warning "SkipHardStop is set. Remaining VMs were not force-stopped."
  return
}

foreach ($VmxPath in $StillRunning) {
  Write-Host "Force stopping: $VmxPath"
  $Output = & $Vmrun stop $VmxPath hard 2>&1
  $ExitCode = $LASTEXITCODE

  if ($ExitCode -ne 0) {
    Write-Warning "Hard stop failed for: $VmxPath"
    $Output | ForEach-Object { Write-Warning $_ }
  }
}

$Remaining = Get-RunningVmList

if ($Remaining.Count -gt 0) {
  Write-Warning "Some VMs are still running:"
  $Remaining | ForEach-Object { Write-Warning $_ }
  exit 1
}

Write-Host "All VMs are powered off."
