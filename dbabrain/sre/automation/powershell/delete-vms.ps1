param(
  [string]$DbSrePayloadJsonBase64,

  [string[]]$VmName,
  [ValidateSet("shared", "mysql", "postgresql", "sqlserver", "oracle_rac", "oracle_dg", "all")]
  [string]$Group = "all"
)

. "$PSScriptRoot\vmware-common.ps1"

Assert-VmrunAvailable

if ($VmName -and $VmName.Count -gt 0) {
  $Inventory = Get-VmInventoryByName -VmName $VmName
} else {
  $Inventory = Get-VmInventory -Group $Group
}

$ToProcess = @($Inventory | Where-Object { Test-Path $_.VmxPath })

if ($ToProcess.Count -eq 0) {
  Write-Host "No VM directories found for the specified group/list."
  exit 0
}

foreach ($Vm in $ToProcess) {
  if (Test-VmIsRunning -VmxPath $Vm.VmxPath) {
    Write-Host "Force stopping $($Vm.Name) ..."
    $null = & $Vmrun stop $Vm.VmxPath hard 2>&1
  }

  $VmDir = Split-Path -Path $Vm.VmxPath -Parent
  Write-Host "Deleting $($Vm.Name) at $VmDir ..."
  Remove-Item -Recurse -Force -Path $VmDir
  Write-Host "$($Vm.Name): deleted" -ForegroundColor Green
}
