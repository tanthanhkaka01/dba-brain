param(
  [string]$DbSrePayloadJsonBase64,

  [ValidateSet("oracle_rac", "oracle_dg")]
  [string]$Group = "oracle_rac"
)

. "$PSScriptRoot\vmware-common.ps1"

# ── Validate installer paths for the requested group ─────────────────────────
$NeedGrid = $Group -eq "oracle_rac"

if ($NeedGrid -and -not $script:OracleGridInstallerPath) {
  throw "oracle.grid_installer_path is not set in sre_config.json (required for oracle_rac)."
}
if (-not $script:OracleDbInstallerPath) {
  throw "oracle.db_installer_path is not set in sre_config.json."
}
if ($NeedGrid -and -not (Test-Path $script:OracleGridInstallerPath)) {
  throw "Grid installer not found: $script:OracleGridInstallerPath"
}
if (-not (Test-Path $script:OracleDbInstallerPath)) {
  throw "DB installer not found: $script:OracleDbInstallerPath"
}

$GridZipName = if ($NeedGrid) { Split-Path $script:OracleGridInstallerPath -Leaf } else { $null }
$DbZipName   = Split-Path $script:OracleDbInstallerPath -Leaf
$StageDir    = $script:OracleInstallStage

# ── Resolve target VM inventory ───────────────────────────────────────────────
$TargetInventory = if ($Group -eq "oracle_rac") {
  @(Get-OracleRacVmInventory)
} else {
  @(Get-OracleDgVmInventory)
}
if ($TargetInventory.Count -eq 0) {
  throw "No VMs found in inventory for group: $Group"
}

# ── Resolve bastion ───────────────────────────────────────────────────────────
$BastionVm = @(Get-SharedVmInventory) | Where-Object { $_.Name -eq "bastion-01" } | Select-Object -First 1
if (-not $BastionVm) {
  throw "bastion-01 not found in shared inventory."
}
$BastionIp     = $BastionVm.ExpectedIp
$BastionTarget = "$($script:GuestUser)@$BastionIp"
$BastionTmpDir = "/tmp/oracle-installer-stage"

$ConnOpts = @(
  "-o", "ConnectTimeout=30",
  "-o", "StrictHostKeyChecking=no",
  "-o", "UserKnownHostsFile=/dev/null",
  "-o", "BatchMode=yes"
)
if ($script:SshIdentityFile) {
  $ConnOpts += @("-i", $script:SshIdentityFile)
}

$NodeList = ($TargetInventory | ForEach-Object { "$($_.Name) ($($_.ExpectedIp))" }) -join ", "
Write-Host "Oracle installer staging  [group: $Group]"
if ($NeedGrid) {
  Write-Host "  Grid: $($script:OracleGridInstallerPath)"
}
Write-Host "  DB:   $($script:OracleDbInstallerPath)"
Write-Host "  Stage: $StageDir"
Write-Host "  Nodes: $NodeList"
Write-Host ""

# ── Step 1: Windows host -> bastion ──────────────────────────────────────────
Write-Host "Step 1: Uploading installer ZIP(s) to bastion ($BastionIp) ..."

& ssh @ConnOpts $BastionTarget "mkdir -p '$BastionTmpDir'"
if ($LASTEXITCODE -ne 0) { throw "Cannot create $BastionTmpDir on bastion." }

if ($NeedGrid) {
  Write-Host "  $GridZipName ..."
  & scp @ConnOpts $script:OracleGridInstallerPath "${BastionTarget}:${BastionTmpDir}/${GridZipName}"
  if ($LASTEXITCODE -ne 0) { throw "SCP of Grid installer to bastion failed." }
}

Write-Host "  $DbZipName ..."
& scp @ConnOpts $script:OracleDbInstallerPath "${BastionTarget}:${BastionTmpDir}/${DbZipName}"
if ($LASTEXITCODE -ne 0) { throw "SCP of DB installer to bastion failed." }

Write-Host "Bastion upload complete."
Write-Host ""

# ── Step 2: bastion -> each target node ──────────────────────────────────────
foreach ($Vm in $TargetInventory) {
  $NodeName   = $Vm.Name
  $NodeIp     = $Vm.ExpectedIp
  $NodeTarget = "$($script:GuestUser)@$NodeIp"

  Write-Host "=== $NodeName ($NodeIp) ==="

  # Prepare stage dir: temporarily world-writable so scp (running as tuser) can write
  $PrepRemote = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=30" +
    " $NodeTarget 'sudo mkdir -p $StageDir && sudo chmod 0777 $StageDir'"
  & ssh @ConnOpts $BastionTarget $PrepRemote
  if ($LASTEXITCODE -ne 0) { throw "Failed to prepare stage directory on $NodeName." }

  if ($NeedGrid) {
    Write-Host "  Copying Grid installer ..."
    $ScpGrid = "scp -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=30" +
      " $BastionTmpDir/$GridZipName ${NodeTarget}:${StageDir}/${GridZipName}"
    & ssh @ConnOpts $BastionTarget $ScpGrid
    if ($LASTEXITCODE -ne 0) { throw "SCP of Grid installer to $NodeName failed." }
  }

  Write-Host "  Copying DB installer ..."
  $ScpDb = "scp -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=30" +
    " $BastionTmpDir/$DbZipName ${NodeTarget}:${StageDir}/${DbZipName}"
  & ssh @ConnOpts $BastionTarget $ScpDb
  if ($LASTEXITCODE -ne 0) { throw "SCP of DB installer to $NodeName failed." }

  # Fix ownership and restore dir mode
  $FixupCmds = if ($NeedGrid) {
    "sudo chown grid:oinstall $StageDir/$GridZipName && sudo chmod 640 $StageDir/$GridZipName && " +
    "sudo chown oracle:oinstall $StageDir/$DbZipName && sudo chmod 640 $StageDir/$DbZipName && " +
    "sudo chown oracle:oinstall $StageDir && sudo chmod 0775 $StageDir && " +
    "ls -lh $StageDir/$GridZipName $StageDir/$DbZipName"
  } else {
    "sudo chown oracle:oinstall $StageDir/$DbZipName && sudo chmod 640 $StageDir/$DbZipName && " +
    "sudo chown oracle:oinstall $StageDir && sudo chmod 0775 $StageDir && " +
    "ls -lh $StageDir/$DbZipName"
  }
  $FixupRemote = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=30" +
    " $NodeTarget '$FixupCmds'"
  & ssh @ConnOpts $BastionTarget $FixupRemote
  if ($LASTEXITCODE -ne 0) { throw "Failed to set ownership on $NodeName." }

  Write-Host "${NodeName}: OK" -ForegroundColor Green
  Write-Host ""
}

# Clean up bastion temp dir
& ssh @ConnOpts $BastionTarget "rm -rf '$BastionTmpDir'"

Write-Host "Oracle installer staging complete on all $Group nodes."
Write-Host "Stage directory: $StageDir"
