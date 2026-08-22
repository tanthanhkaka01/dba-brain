param(
  [string]$DbSrePayloadJsonBase64,

  [string[]]$VmName,
  [ValidateSet("shared", "mysql", "postgresql", "sqlserver", "oracle_rac", "oracle_dg", "all")]
  [string]$Group = "all",
  [int]$CpuCount,
  [int]$MemoryGB,
  [int]$MemoryMB
)

. "$PSScriptRoot\vmware-common.ps1"

Assert-VmrunAvailable
if ($VmName -and $VmName.Count -gt 0) {
  $Inventory = Get-VmInventoryByName -VmName $VmName
} else {
  $Inventory = Get-VmInventory -Group $Group
}
Set-VmResourcesForInventory -Inventory $Inventory -CpuCount $CpuCount -MemoryGB $MemoryGB -MemoryMB $MemoryMB
