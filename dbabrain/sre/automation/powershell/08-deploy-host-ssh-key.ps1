param(
  [string]$DbSrePayloadJsonBase64,

  [string[]]$VmName,
  [ValidateSet("shared", "mysql", "postgresql", "oracle_rac", "oracle_dg", "all")]
  [string]$Group = "all",
  [string]$GuestUser = $null,
  [string]$GuestPassword = $null
)

. "$PSScriptRoot\vmware-common.ps1"

Assert-VmrunAvailable

if ($VmName -and $VmName.Count -gt 0) {
  $Inventory = Get-VmInventoryByName -VmName $VmName
} else {
  $Inventory = Get-VmInventory -Group $Group
}

$OracleGroups = @("oracle_rac", "oracle_dg")
if ($OracleGroups -contains $Group) {
  $Credential = New-GuestCredential -GuestUser $GuestUser -GuestPassword $GuestPassword
  Invoke-SshDeployHostKey -Inventory $Inventory -GuestUser $Credential.User -GuestPassword $Credential.Password
} else {
  Deploy-HostSshKeyToInventory -Inventory $Inventory
}
