param(
  [string]$DbSrePayloadJsonBase64,

  [string[]]$VmName,
  [ValidateSet("shared", "mysql", "postgresql", "sqlserver", "oracle_rac", "oracle_dg", "all")]
  [string]$Group = "all",
  [string]$GuestUser = $null,
  [string]$GuestPassword = $null,
  [switch]$SkipReboot,

  # JSON object mapping VM name to its current DHCP IP, e.g. '{"rac01":"192.168.18.141","rac02":"192.168.18.135"}'
  # When provided, identity fix runs over SSH (plink) instead of VMware Tools.
  [string]$CurrentIpMapJson = $null,

  # Disable kdump and multipathd before reboot. Required for oracle_rac because
  # kdump's kexec call panics the kernel when the lsilogic shared SCSI bus is present.
  [switch]$DisableKdumpMultipath
)

. "$PSScriptRoot\vmware-common.ps1"

Assert-VmrunAvailable
if ($VmName -and $VmName.Count -gt 0) {
  $Inventory = Get-VmInventoryByName -VmName $VmName
} else {
  $Inventory = Get-VmInventory -Group $Group
}

$Credential = New-GuestCredential -GuestUser $GuestUser -GuestPassword $GuestPassword

# oracle_rac/oracle_dg VMs are cloned from Oracle Linux and do not have open-vm-tools.
# Auto-discover their current DHCP IPs from the VMware lease file so we can use the SSH path.
# Scoped to oracle groups only — shared/mysql/postgresql/sqlserver use the VMware Tools path.
if (-not $CurrentIpMapJson -and $Group -in @("oracle_rac", "oracle_dg")) {
  Write-Host "No -CurrentIpMapJson provided; attempting DHCP lease auto-discovery..."
  $DiscoveredMap = @{}
  foreach ($Vm in $Inventory) {
    $DiscoveredIp = Get-VmCurrentIpFromLeases -VmxPath $Vm.VmxPath
    if ($DiscoveredIp) {
      $DiscoveredMap[$Vm.Name] = $DiscoveredIp
      Write-Host "  $($Vm.Name): $DiscoveredIp (from DHCP lease)"
    } else {
      Write-Warning "  $($Vm.Name): no DHCP lease entry found"
    }
  }
  if ($DiscoveredMap.Count -gt 0 -and $DiscoveredMap.Count -eq $Inventory.Count) {
    $CurrentIpMapJson = $DiscoveredMap | ConvertTo-Json -Compress
    Write-Host "All VMs discovered via DHCP; using SSH identity fix path."
  }
}

# oracle_rac requires disabling kdump/multipathd to prevent kernel panic with shared SCSI bus
if ($Group -in @("oracle_rac", "oracle_dg")) {
  $DisableKdumpMultipath = [switch]$true
}

if ($CurrentIpMapJson) {
  $CurrentIpRaw = $CurrentIpMapJson | ConvertFrom-Json
  $CurrentIpMap = @{}
  $CurrentIpRaw.PSObject.Properties | ForEach-Object { $CurrentIpMap[$_.Name] = $_.Value }

  Invoke-SshGuestIdentityFix -Inventory $Inventory -CurrentIpMap $CurrentIpMap `
    -GuestUser $Credential.User -GuestPassword $Credential.Password -SkipReboot:$SkipReboot `
    -DisableKdumpMultipath:$DisableKdumpMultipath
} else {
  Invoke-VmGuestIdentityFix -Inventory $Inventory -GuestUser $Credential.User -GuestPassword $Credential.Password -SkipReboot:$SkipReboot
}

if ($SkipReboot) {
  Write-Host "Collecting VM status after guest identity fix..."
} else {
  Write-Host "Waiting for VMs to come back after reboot and collecting VM status..."
}

Get-VmInfo -Inventory $Inventory -WaitForGuestIp
