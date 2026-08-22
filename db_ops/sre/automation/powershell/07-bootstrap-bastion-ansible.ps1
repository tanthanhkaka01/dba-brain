param(
  [string]$DbSrePayloadJsonBase64,

  [string]$VmName = "bastion-01",
  [string]$GuestUser = $null,
  [string]$GuestPassword = $null,
  [ValidateSet("mysql", "postgresql", "sqlserver", "oracle_rac", "oracle_dg", "monitoring")]
  [string]$TargetGroup = "mysql"
)

. "$PSScriptRoot\vmware-common.ps1"

Assert-VmrunAvailable
$Credential = New-GuestCredential -GuestUser $GuestUser -GuestPassword $GuestPassword
$Inventory = Get-VmInventoryByName -VmName $VmName
$Vm = $Inventory | Select-Object -First 1

if (-not (Test-Path $Vm.VmxPath)) {
  throw "VMX not found for $($Vm.Name): $($Vm.VmxPath)"
}

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ArchiveName = "db-sre-repo-sync.tar"
$HostArchivePath = Join-Path $env:TEMP $ArchiveName
$GuestArchivePath = "/tmp/$ArchiveName"

if (Test-Path $HostArchivePath) {
  Remove-Item -Path $HostArchivePath -Force -ErrorAction SilentlyContinue
}

Write-Host "Packaging repository content for bastion repo sync..."
$TarExe = "$env:SystemRoot\System32\tar.exe"
& $TarExe --exclude=.git --exclude=.venv --exclude=.pytest_cache --exclude=.mypy_cache --exclude=node_modules --exclude=runtime --exclude=logs -cf $HostArchivePath -C $RepoRoot .
if ($LASTEXITCODE -ne 0) {
  throw "Failed to package repository archive for bastion sync."
}

$bashScript = @"
set -euo pipefail
TARGET_USER='$($Credential.User)'
REPO_ROOT='/opt/db-sre/repo'
ARCHIVE_PATH='$GuestArchivePath'

echo '[db-sre] Preparing repository workspace...'
printf '%s\n' '$($Credential.Password)' | sudo -S -p '' install -d -m 0755 /opt/db-sre
printf '%s\n' '$($Credential.Password)' | sudo -S -p '' rm -rf "`$REPO_ROOT"
printf '%s\n' '$($Credential.Password)' | sudo -S -p '' install -d -m 0755 "`$REPO_ROOT"
printf '%s\n' '$($Credential.Password)' | sudo -S -p '' tar -xf "`$ARCHIVE_PATH" -C "`$REPO_ROOT"
printf '%s\n' '$($Credential.Password)' | sudo -S -p '' chown -R "`$TARGET_USER":"`$TARGET_USER" /opt/db-sre

echo '[db-sre] Stripping Windows CRLF line endings from scripts...'
find "`$REPO_ROOT/automation/bash" -name '*.sh' -exec sed -i 's/\r//' {} \;
find "`$REPO_ROOT/automation/ansible" \( -name '*.yml' -o -name '*.yaml' -o -name '*.cfg' -o -name '*.j2' \) -exec sed -i 's/\r//' {} \;

echo '[db-sre] Bastion repo sync completed.'
echo '[db-sre] Repo path: /opt/db-sre/repo'
echo '[db-sre] Operator user: $($Credential.User)'
"@

$EncodedBashScript = ConvertTo-Base64Utf8 -Value $bashScript
$GuestExecScript = "printf '%s' '$EncodedBashScript' | base64 -d | /bin/bash > /tmp/db-sre-bootstrap.log 2>&1"

try {
  Write-Host "Copying repository archive to $($Vm.Name) ..."
  $ArchiveCopyResult = Invoke-VmrunWithOutput -Arguments @(
    "-gu", $Credential.User,
    "-gp", $Credential.Password,
    "copyFileFromHostToGuest",
    $Vm.VmxPath,
    $HostArchivePath,
    $GuestArchivePath
  )

  if ($ArchiveCopyResult.ExitCode -ne 0) {
    $ArchiveCopyResult.Output | ForEach-Object { Write-Warning $_ }
    throw "Unable to copy repository archive to $($Vm.Name). ExitCode=$($ArchiveCopyResult.ExitCode)"
  }

  Write-Host "Extracting repository on $($Vm.Name) ..."
  $RunResult = Invoke-VmrunWithOutput -Arguments @(
    "-gu", $Credential.User,
    "-gp", $Credential.Password,
    "runScriptInGuest",
    $Vm.VmxPath,
    "/bin/bash",
    $GuestExecScript
  )

  $LocalLogFile = [System.IO.Path]::GetTempFileName()
  $CopyResult = Invoke-VmrunWithOutput -Arguments @(
    "-gu", $Credential.User, "-gp", $Credential.Password,
    "copyFileFromGuestToHost",
    $Vm.VmxPath, "/tmp/db-sre-bootstrap.log", $LocalLogFile
  )
  if ($CopyResult.ExitCode -eq 0 -and (Test-Path $LocalLogFile)) {
    Write-Host "--- Bootstrap log: $($Vm.Name) ---"
    Get-Content $LocalLogFile | ForEach-Object { Write-Host $_ }
    Remove-Item $LocalLogFile -ErrorAction SilentlyContinue
  }

  if ($RunResult.ExitCode -ne 0) {
    $RunResult.Output | ForEach-Object { Write-Warning $_ }
    throw "Bastion repo sync failed on $($Vm.Name). ExitCode=$($RunResult.ExitCode)"
  }

  Write-Host "Bastion repo sync completed successfully on $($Vm.Name)." -ForegroundColor Green

  # Generate bastion SSH key via VMware Tools — no SSH from host required at this stage
  Write-Host "Generating bastion SSH key and distributing to $TargetGroup nodes..."

  $GenKeyScript = "mkdir -p ~/.ssh && chmod 700 ~/.ssh && [ ! -f ~/.ssh/id_ed25519 ] && ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519 || true; cat ~/.ssh/id_ed25519.pub > /tmp/db-sre-bastion-pubkey.txt"
  $GenKeyResult = Invoke-VmrunWithOutput -Arguments @(
    "-gu", $Credential.User, "-gp", $Credential.Password,
    "runScriptInGuest", $Vm.VmxPath, "/bin/bash", $GenKeyScript
  )
  if ($GenKeyResult.ExitCode -ne 0) {
    $GenKeyResult.Output | ForEach-Object { Write-Warning $_ }
    throw "Failed to generate bastion SSH key via VMware Tools. ExitCode=$($GenKeyResult.ExitCode)"
  }

  $PubKeyFile = [System.IO.Path]::GetTempFileName()
  $CopyKeyResult = Invoke-VmrunWithOutput -Arguments @(
    "-gu", $Credential.User, "-gp", $Credential.Password,
    "copyFileFromGuestToHost", $Vm.VmxPath, "/tmp/db-sre-bastion-pubkey.txt", $PubKeyFile
  )
  if ($CopyKeyResult.ExitCode -ne 0 -or -not (Test-Path $PubKeyFile)) {
    throw "Failed to read bastion public key from guest via VMware Tools."
  }
  $BastionPubKey = (Get-Content $PubKeyFile | Where-Object { $_ -match '^ssh-' } | Select-Object -First 1).Trim()
  Remove-Item $PubKeyFile -ErrorAction SilentlyContinue
  if (-not $BastionPubKey) {
    throw "Could not parse bastion public key from guest output."
  }
  Write-Host "Bastion public key: $BastionPubKey"

  $TargetNodes = switch ($TargetGroup) {
    "mysql"      { @(Get-MysqlVmInventory) }
    "postgresql" { @(Get-PostgresqlVmInventory) }
    "sqlserver"  { @(Get-SqlServerVmInventory) }
    "oracle_rac" { @(Get-OracleRacVmInventory) }
    "oracle_dg"  { @(Get-OracleDgVmInventory) }
    default      { @() }
  }

  foreach ($Node in $TargetNodes) {
    Write-Host "Deploying bastion key to $($Node.Name) ($($Node.ExpectedIp)) ..."

    # All DB node access runs FROM bastion-01 via VMware Tools on bastion-01.
    # Windows never touches DB VMs after initial identity setup.
    $DeployBash = @"
set -euo pipefail
NODE_IP='$($Node.ExpectedIp)'
NODE_USER='$($Credential.User)'
NODE_PASS='$($Credential.Password)'

mkdir -p ~/.ssh && chmod 700 ~/.ssh

# Install sshpass if not present (bastion may be freshly cloned)
command -v sshpass >/dev/null 2>&1 || {
  echo '[db-sre] Installing sshpass...'
  printf '%s\n' "`$NODE_PASS" | sudo -S apt-get install -y -q sshpass
}

# Scan and cache the node host key so Ansible can connect without StrictHostKeyChecking=no later
ssh-keyscan -T 30 -H "`$NODE_IP" >> ~/.ssh/known_hosts 2>/dev/null || true

# Deploy bastion public key to the node (known_hosts is set so strict check passes)
sshpass -p "`$NODE_PASS" ssh-copy-id -i ~/.ssh/id_ed25519.pub "`$NODE_USER@`$NODE_IP"

# Verify passwordless SSH works end-to-end
ssh -o BatchMode=yes "`$NODE_USER@`$NODE_IP" echo ssh-ok

echo "deployed and verified: `$NODE_IP"
"@
    $DeployEncoded = ConvertTo-Base64Utf8 -Value $DeployBash
    $RunScript = "printf '%s' '$DeployEncoded' | base64 -d | /bin/bash > /tmp/db-sre-deploy-key.log 2>&1"
    $DeployResult = Invoke-VmrunWithOutput -Arguments @(
      "-gu", $Credential.User, "-gp", $Credential.Password,
      "runScriptInGuest", $Vm.VmxPath, "/bin/bash", $RunScript
    )

    $LocalLog = [System.IO.Path]::GetTempFileName()
    $LogResult = Invoke-VmrunWithOutput -Arguments @(
      "-gu", $Credential.User, "-gp", $Credential.Password,
      "copyFileFromGuestToHost", $Vm.VmxPath, "/tmp/db-sre-deploy-key.log", $LocalLog
    )
    if ($LogResult.ExitCode -eq 0 -and (Test-Path $LocalLog)) {
      Get-Content $LocalLog | ForEach-Object { Write-Host "  $_" }
      Remove-Item $LocalLog -ErrorAction SilentlyContinue
    }

    if ($DeployResult.ExitCode -ne 0) {
      $DeployResult.Output | ForEach-Object { Write-Warning $_ }
      throw "Failed to deploy bastion key to $($Node.Name). ExitCode=$($DeployResult.ExitCode)"
    }
    Write-Host "$($Node.Name): OK" -ForegroundColor Green
  }

  Write-Host ""
  Write-Host "Next step:"
  Write-Host "  python -m db_ops.sre.cli run-bastion-script bootstrap-bastion-ansible -- $TargetGroup"
}
finally {
  Remove-Item -Path $HostArchivePath -Force -ErrorAction SilentlyContinue
}
