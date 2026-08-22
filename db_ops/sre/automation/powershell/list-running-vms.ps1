param(
  [string]$DbSrePayloadJsonBase64
)

. "$PSScriptRoot\vmware-common.ps1"

if (-not (Test-Path $Vmrun)) {
  throw "vmrun.exe not found at: $Vmrun"
}

function Get-InventoryLookup {
  $Lookup = @{}

  foreach ($Vm in $SharedVmMap) {
    $Lookup[$Vm.Name] = @{
      Role = $Vm.Role
    }
  }

  foreach ($Node in $Nodes) {
    $Lookup[$Node] = @{
      Role = "mysql"
    }
  }

  foreach ($Node in $PostgresqlNodes) {
    $Lookup[$Node] = @{
      Role = "postgresql"
    }
  }

  return $Lookup
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

$InventoryLookup = Get-InventoryLookup
$RunningVms = Get-RunningVmList

if (-not $RunningVms -or $RunningVms.Count -eq 0) {
  Write-Host "No running VMs found."
  return
}

$Results = foreach ($VmxPath in $RunningVms) {
  $ResolvedVmxPath = $VmxPath

  if (Test-Path $VmxPath) {
    $ResolvedVmxPath = (Resolve-Path $VmxPath).Path
  }

  $VmName = [System.IO.Path]::GetFileNameWithoutExtension($ResolvedVmxPath)
  $Inventory = $InventoryLookup[$VmName]
  $QueriedGuestHostname = Get-GuestHostname -VmxPath $ResolvedVmxPath
  $Ips = Get-GuestIpAddresses -VmxPath $ResolvedVmxPath
  $GuestHostname = "<unavailable>"
  $CurrentIp = "<unavailable>"
  $GuestInfoStatus = "unavailable"

  if ($Ips.Count -gt 0) {
    $CurrentIp = $Ips -join ", "
  }

  $GuestHostname = Resolve-GuestHostnameValue -QueriedGuestHostname $QueriedGuestHostname -FallbackHostname $VmName -IpAddresses $Ips
  $GuestInfoStatus = Get-GuestInfoStatus -GuestHostname $GuestHostname -IpAddresses $Ips

  [PSCustomObject]@{
    VmName          = $VmName
    GuestHostname   = $GuestHostname
    Role            = if ($Inventory) { $Inventory.Role } else { $null }
    CurrentIp       = $CurrentIp
    GuestInfoStatus = $GuestInfoStatus
    PowerState      = "Running"
    Vmx             = $ResolvedVmxPath
  }
}

$Results | Sort-Object VmName
