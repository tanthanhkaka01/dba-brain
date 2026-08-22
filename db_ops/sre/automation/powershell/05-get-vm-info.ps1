param(
  [string]$DbSrePayloadJsonBase64,

  [ValidateSet("shared", "mysql", "postgresql", "sqlserver", "oracle_rac", "oracle_dg", "all")]
  [string]$Group = "all"
)

. "$PSScriptRoot\vmware-common.ps1"

Assert-VmrunAvailable
Get-VmInfo -Inventory (Get-VmInventory -Group $Group) -WaitForGuestIp -GuestIpTimeoutSec 5
