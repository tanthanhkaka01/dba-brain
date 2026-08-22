param(
  [string]$DbSrePayloadJsonBase64,

  [int]$OcrDiskGb = 10,
  [int]$DataDiskGb = 50,
  [int]$FraDiskGb = 30,

  [switch]$Force
)

. "$PSScriptRoot\vmware-common.ps1"

Assert-VmrunAvailable

$VdiskManager = Join-Path (Split-Path $Vmrun -Parent) "vmware-vdiskmanager.exe"
if (-not (Test-Path $VdiskManager)) {
  throw "vmware-vdiskmanager.exe not found at: $VdiskManager"
}

$SharedDiskRoot = Join-Path $script:VmRoot "oracle_rac_shared"
New-Item -ItemType Directory -Force -Path $SharedDiskRoot | Out-Null
Write-Host "Shared disk directory: $SharedDiskRoot"

$Disks = @(
  @{ Name = "ocr01"; SizeGb = $OcrDiskGb;  ScsiTarget = 0 },
  @{ Name = "ocr02"; SizeGb = $OcrDiskGb;  ScsiTarget = 1 },
  @{ Name = "ocr03"; SizeGb = $OcrDiskGb;  ScsiTarget = 2 },
  @{ Name = "data01"; SizeGb = $DataDiskGb; ScsiTarget = 3 },
  @{ Name = "fra01";  SizeGb = $FraDiskGb;  ScsiTarget = 4 }
)

foreach ($Disk in $Disks) {
  $VmdkPath = Join-Path $SharedDiskRoot "$($Disk.Name).vmdk"

  if (Test-Path $VmdkPath) {
    if ($Force) {
      Write-Host "Removing existing VMDK: $VmdkPath"
      Remove-Item -Force $VmdkPath -ErrorAction SilentlyContinue
      Remove-Item -Force ($VmdkPath -replace '\.vmdk$', '-flat.vmdk') -ErrorAction SilentlyContinue
    } else {
      Write-Host "Skipping existing VMDK: $VmdkPath"
      continue
    }
  }

  $SizeMb = $Disk.SizeGb * 1024
  Write-Host "Creating shared VMDK: $VmdkPath ($($Disk.SizeGb)GB) ..."

  $Output = & $VdiskManager -c -s "${SizeMb}MB" -a lsilogic -t 2 $VmdkPath 2>&1
  $ExitCode = $LASTEXITCODE

  if ($ExitCode -ne 0) {
    Write-Host "vmware-vdiskmanager output:" -ForegroundColor Yellow
    $Output | ForEach-Object { Write-Host $_ }
    throw "Failed to create VMDK: $VmdkPath. ExitCode=$ExitCode"
  }

  Write-Host "Created: $VmdkPath"
}

$RacInventory = @(Get-OracleRacVmInventory)
if ($RacInventory.Count -eq 0) {
  throw "No oracle_rac VMs found in inventory. Ensure config.json has oracle_rac group."
}

foreach ($Vm in $RacInventory) {
  if (-not (Test-Path $Vm.VmxPath)) {
    Write-Warning "VMX not found for $($Vm.Name): $($Vm.VmxPath) -- skipping"
    continue
  }

  if (Test-VmIsRunning -VmxPath $Vm.VmxPath) {
    throw "VM $($Vm.Name) must be powered off before adding shared disks."
  }

  Write-Host "Adding shared disks to $($Vm.Name) ..."
  $Lines = [System.Collections.Generic.List[string]](Get-Content $Vm.VmxPath)

  if (-not ($Lines -match '^\s*scsi1\.present\s*=')) {
    $Lines.Add('scsi1.present = "TRUE"')
    $Lines.Add('scsi1.virtualDev = "lsilogic"')
    $Lines.Add('scsi1.sharedBus = "virtual"')
    Write-Host "  Added SCSI controller 1"
  }

  # Suppress VMware dialogs so vmrun nogui doesn't get canceled
  if (-not ($Lines -match '^\s*msg\.autoAnswer\s*=')) {
    $Lines.Add('msg.autoAnswer = "TRUE"')
  }

  # Shared SCSI bus (sharedBus = "virtual") requires disk locking to be disabled
  if (-not ($Lines -match '^\s*disk\.locking\s*=')) {
    $Lines.Add('disk.locking = "FALSE"')
  }

  foreach ($Disk in $Disks) {
    $VmdkPath = Join-Path $SharedDiskRoot "$($Disk.Name).vmdk"
    $RelativePath = $VmdkPath.Replace('\', '/')
    $SlotKey = "scsi1:$($Disk.ScsiTarget)"

    if (-not ($Lines -match "^\s*${SlotKey}\.present\s*=")) {
      $Lines.Add("${SlotKey}.present = `"TRUE`"")
      $Lines.Add("${SlotKey}.fileName = `"$RelativePath`"")
      $Lines.Add("${SlotKey}.deviceType = `"disk`"")
      $Lines.Add("${SlotKey}.mode = `"independent-persistent`"")
      $Lines.Add("${SlotKey}.sharing = `"multi-writer`"")
      $Lines.Add("${SlotKey}.redo = `"`"")
      Write-Host "  Added $SlotKey -> $RelativePath"
    } else {
      Write-Host "  $SlotKey already present -- skipping"
    }
  }

  Set-Content -Path $Vm.VmxPath -Value $Lines
  Write-Host "Updated VMX: $($Vm.VmxPath)"
}

Write-Host ""
Write-Host "Shared disk setup complete."
Write-Host "Start the RAC VMs and verify disks appear as /dev/sdb-sdf before running oracle-rac-os-prep.yml"
