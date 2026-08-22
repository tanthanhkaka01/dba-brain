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

$StartFailures = Start-VmInventory -Inventory $Inventory
Write-Host "Waiting for guest info and collecting VM status..."
Get-VmInfo -Inventory $Inventory -WaitForGuestIp

if ($StartFailures.Count -gt 0) {
  exit 1
}
