param(
  [string]$DbSrePayloadJsonBase64,

  [ValidateSet("all", "ubuntu", "oraclelinux", "windows")]
  [string]$Template = "all",

  [ValidateSet("DHCP", "Static")]
  [string]$IpMode = "DHCP",

  [string]$IpAddress = $null,
  [int]$Cidr = 0,
  [string]$Gateway = $null,
  [string[]]$DnsServers = @(),
  [string]$LinuxInterface = "auto",

  [string]$LinuxGuestUser = $null,
  [string]$LinuxGuestPassword = $null,
  [string]$WindowsGuestUser = "Administrator",
  [string]$WindowsGuestPassword = "abc123@",

  [switch]$NoStart,
  [int]$GuestIpTimeoutSec = 180
)

. "$PSScriptRoot\vmware-common.ps1"

if (-not $Cidr) {
  $Cidr = $script:Cidr
}

if ([string]::IsNullOrWhiteSpace($Gateway)) {
  $Gateway = $script:Gateway
}

if (-not $DnsServers -or $DnsServers.Count -eq 0) {
  $DnsServers = $script:DnsServers
}

if ([string]::IsNullOrWhiteSpace($LinuxGuestUser)) {
  $LinuxGuestUser = $script:GuestUser
}

if ([string]::IsNullOrWhiteSpace($LinuxGuestPassword)) {
  $LinuxGuestPassword = $script:GuestPassword
}

function Get-TemplateInventory {
  param(
    [ValidateSet("all", "ubuntu", "oraclelinux", "windows")]
    [string]$Template
  )

  $TemplateRoot = Join-Path $VmRoot "templates"
  $Templates = @(
    [PSCustomObject]@{
      Key       = "ubuntu"
      Name      = "tpl-ubuntu-2404"
      OsType    = "linux"
      LinuxKind = "ubuntu"
      VmxPath   = Join-Path (Join-Path $TemplateRoot "tpl-ubuntu-2404") "tpl-ubuntu-2404.vmx"
    },
    [PSCustomObject]@{
      Key       = "oraclelinux"
      Name      = "tpl-oraclelinux-r9u6"
      OsType    = "linux"
      LinuxKind = "oraclelinux"
      VmxPath   = Join-Path (Join-Path $TemplateRoot "tpl-oraclelinux-r9u6") "tpl-oraclelinux-r9u6.vmx"
    },
    [PSCustomObject]@{
      Key       = "windows"
      Name      = "tpl-win2025"
      OsType    = "windows"
      LinuxKind = $null
      VmxPath   = Join-Path (Join-Path $TemplateRoot "tpl-win2025") "tpl-win2025.vmx"
    }
  )

  if ($Template -eq "all") {
    return @($Templates)
  }

  return @($Templates | Where-Object { $_.Key -eq $Template })
}

function Assert-TemplateNetworkArguments {
  if ($IpMode -eq "Static" -and [string]::IsNullOrWhiteSpace($IpAddress)) {
    throw "Use -IpAddress when -IpMode Static is selected."
  }

  if ($IpMode -eq "Static" -and $Template -eq "all") {
    throw "Static mode accepts one -IpAddress at a time. Use -Template ubuntu|oraclelinux|windows when -IpMode Static is selected."
  }
}

function ConvertTo-WindowsEncodedCommand {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Script
  )

  return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
}

function Start-TemplateIfNeeded {
  param(
    [Parameter(Mandatory = $true)]
    [object]$TemplateItem
  )

  if (-not (Test-Path $TemplateItem.VmxPath)) {
    throw "Template VMX not found for $($TemplateItem.Name): $($TemplateItem.VmxPath)"
  }

  if (Test-VmIsRunning -VmxPath $TemplateItem.VmxPath) {
    Write-Host "$($TemplateItem.Name) is already running."
    return
  }

  if ($NoStart) {
    throw "$($TemplateItem.Name) is not running. Start it first or omit -NoStart."
  }

  Write-Host "Starting $($TemplateItem.Name) ..."
  $Output = & $Vmrun start $TemplateItem.VmxPath nogui 2>&1
  $ExitCode = $LASTEXITCODE

  if ($ExitCode -ne 0) {
    $Output | ForEach-Object { Write-Host $_ }
    throw "Failed to start $($TemplateItem.Name). ExitCode=$ExitCode"
  }
}

function Invoke-LinuxTemplateBootstrap {
  param(
    [Parameter(Mandatory = $true)]
    [object]$TemplateItem
  )

  if ([string]::IsNullOrWhiteSpace($LinuxGuestUser) -or [string]::IsNullOrWhiteSpace($LinuxGuestPassword)) {
    throw "Linux guest credentials are required. Set credentials.guest_user/credentials.guest_password in config.json or pass -LinuxGuestUser/-LinuxGuestPassword."
  }

  Write-Host "Waiting for $($TemplateItem.Name) VMware Tools to accept commands (up to ${GuestIpTimeoutSec}s) ..."
  $Deadline = (Get-Date).AddSeconds($GuestIpTimeoutSec)
  $ToolsReady = $false
  while ((Get-Date) -lt $Deadline) {
    $ProbeResult = Invoke-VmrunWithOutput -Arguments @(
      "-gu", $LinuxGuestUser, "-gp", $LinuxGuestPassword,
      "runScriptInGuest", $TemplateItem.VmxPath,
      "/bin/bash", "exit 0"
    )
    if ($ProbeResult.ExitCode -eq 0) {
      $ToolsReady = $true
      break
    }
    Start-Sleep -Seconds 10
  }
  if (-not $ToolsReady) {
    throw @"
$($TemplateItem.Name) VMware Tools did not respond within ${GuestIpTimeoutSec}s.
Possible causes:
  1. open-vm-tools is not installed in the template.
     Fix: open the VM console and run: sudo apt install -y open-vm-tools
  2. The VM is still booting. Try again in a minute, or increase -GuestIpTimeoutSec.
  3. Guest credentials are wrong. Check credentials.guest_user/guest_password in sre_config.json.
"@
  }
  Write-Host "$($TemplateItem.Name) VMware Tools is ready."

  $DnsCsv = $DnsServers -join ","
  $EscapedPassword = ConvertTo-BashSingleQuotedLiteral -Value $LinuxGuestPassword

  $BashScript = @'
set -euxo pipefail
trap 'echo "[db-sre-template-bootstrap] FAILED at line $LINENO, command: $BASH_COMMAND, exit=$?" >&2' ERR

SUDO_PASSWORD='__SUDO_PASSWORD__'
LINUX_KIND='__LINUX_KIND__'
IP_MODE='__IP_MODE__'
IP_ADDRESS='__IP_ADDRESS__'
CIDR='__CIDR__'
GATEWAY='__GATEWAY__'
DNS_CSV='__DNS_CSV__'
REQUESTED_IFACE='__LINUX_INTERFACE__'

log() {
  printf '[db-sre-template-bootstrap] %s\n' "$*"
}

sudo_cmd() {
  printf '%s\n' "$SUDO_PASSWORD" | sudo -S -p '' -- "$@"
}

detect_iface() {
  if [ "$REQUESTED_IFACE" != "auto" ] && ip link show "$REQUESTED_IFACE" >/dev/null 2>&1; then
    printf '%s\n' "$REQUESTED_IFACE"
    return 0
  fi

  local iface
  iface="$(ip route show default 2>/dev/null | awk '/default/ { print $5; exit }')"
  if [ -n "$iface" ] && ip link show "$iface" >/dev/null 2>&1; then
    printf '%s\n' "$iface"
    return 0
  fi

  # No default route yet (fresh template with no networking configured) — fall back to
  # the first non-loopback interface reported by the kernel.
  ip link show 2>/dev/null | awk -F': ' '/^[0-9]+:/ { split($2,a,"@"); if (a[1] != "lo" && a[1] != "") { print a[1]; exit } }'
}

write_ubuntu_netplan() {
  target_iface="$1"
  sudo_cmd mkdir -p /etc/netplan

  if [ "$IP_MODE" = "DHCP" ]; then
    cat > /tmp/99-db-sre-template-bootstrap.yaml <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    $target_iface:
      optional: true
      dhcp4: true
EOF
  else
    dns_yaml="$(printf '%s\n' "$DNS_CSV" | tr ',' '\n' | awk '{ printf "          - %s\n", $1 }')"
    cat > /tmp/99-db-sre-template-bootstrap.yaml <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    $target_iface:
      optional: true
      dhcp4: false
      addresses:
        - $IP_ADDRESS/$CIDR
      routes:
        - to: default
          via: $GATEWAY
      nameservers:
        addresses:
$dns_yaml
EOF
  fi

  sudo_cmd install -m 600 /tmp/99-db-sre-template-bootstrap.yaml /etc/netplan/99-db-sre-template-bootstrap.yaml
  sudo_cmd netplan generate
  sudo_cmd netplan apply
}

configure_network_manager() {
  target_iface="$1"
  if ! command -v nmcli >/dev/null 2>&1; then
    log "nmcli not found; skipping NetworkManager IP configuration"
    return 0
  fi

  connection_name="$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v iface="$target_iface" '$2 == iface { print $1; exit }')"
  if [ -z "$connection_name" ]; then
    connection_name="$target_iface"
  fi

  if [ "$IP_MODE" = "DHCP" ]; then
    sudo_cmd nmcli connection modify "$connection_name" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns ""
  else
    sudo_cmd nmcli connection modify "$connection_name" ipv4.method manual ipv4.addresses "$IP_ADDRESS/$CIDR" ipv4.gateway "$GATEWAY" ipv4.dns "$DNS_CSV"
  fi

  sudo_cmd nmcli connection up "$connection_name" || true
}


fix_ubuntu_apt_sources() {
  log "Repairing Ubuntu apt sources and cache"

  if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
    sudo_cmd cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.db-sre-bak || true
    sudo_cmd sed -i \
      -e 's|http://vn.archive.ubuntu.com/ubuntu/?|https://archive.ubuntu.com/ubuntu/|g' \
      -e 's|http://archive.ubuntu.com/ubuntu/?|https://archive.ubuntu.com/ubuntu/|g' \
      -e 's|http://security.ubuntu.com/ubuntu/?|https://security.ubuntu.com/ubuntu/|g' \
      /etc/apt/sources.list.d/ubuntu.sources
  fi

  if [ -f /etc/apt/sources.list ]; then
    sudo_cmd cp /etc/apt/sources.list /etc/apt/sources.list.db-sre-bak || true
    sudo_cmd sed -i \
      -e 's|http://vn.archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g' \
      -e 's|http://archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g' \
      -e 's|http://security.ubuntu.com/ubuntu|https://security.ubuntu.com/ubuntu|g' \
      /etc/apt/sources.list
  fi

  cat > /tmp/99-db-sre-apt-retry.conf <<EOF
Acquire::Retries "3";
Acquire::ForceIPv4 "true";
Acquire::http::No-Cache "true";
Acquire::https::No-Cache "true";
EOF
  sudo_cmd install -m 644 /tmp/99-db-sre-apt-retry.conf /etc/apt/apt.conf.d/99-db-sre-apt-retry.conf

  sudo_cmd apt clean || true
  sudo_cmd rm -rf /var/lib/apt/lists/* || true
}

apt_update_with_repair() {
  if sudo_cmd apt update; then
    return 0
  fi

  log "apt update failed; trying mirror/source repair"
  fix_ubuntu_apt_sources

  if sudo_cmd apt update; then
    return 0
  fi

  log "apt update still failed after repair"
  echo "==== /etc/apt/sources.list.d/ubuntu.sources ===="
  cat /etc/apt/sources.list.d/ubuntu.sources 2>/dev/null || true
  echo "==== network ===="
  ip route || true
  resolvectl status 2>/dev/null || true
  echo "==== apt proxy config ===="
  grep -RHi proxy /etc/apt/apt.conf.d /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null || true
  return 100
}


target_iface="$(detect_iface)"
if [ -z "$target_iface" ] || ! ip link show "$target_iface" >/dev/null 2>&1; then
  echo "Unable to detect a usable network interface" >&2
  exit 1
fi

log "Using interface: $target_iface"

if [ "$LINUX_KIND" = "ubuntu" ]; then
  _need_apt=0
  dpkg -s openssh-server >/dev/null 2>&1 || _need_apt=1
  dpkg -s open-vm-tools  >/dev/null 2>&1 || _need_apt=1
  if [ "$_need_apt" = "1" ]; then
    log "openssh-server or open-vm-tools missing — running apt install"
    apt_update_with_repair || { echo "apt update failed after mirror/source repair"; exit 100; }
    sudo_cmd env DEBIAN_FRONTEND=noninteractive apt install -y openssh-server open-vm-tools || { echo "apt install failed"; exit 101; }
  else
    log "openssh-server and open-vm-tools already installed, skipping apt"
  fi
  sudo_cmd systemctl enable ssh
  sudo_cmd systemctl restart ssh
  sudo_cmd systemctl enable open-vm-tools
  systemctl is-active --quiet open-vm-tools || sudo_cmd systemctl start open-vm-tools
  write_ubuntu_netplan "$target_iface"
else
  _need_dnf=0
  rpm -q openssh-server >/dev/null 2>&1 || _need_dnf=1
  rpm -q open-vm-tools  >/dev/null 2>&1 || _need_dnf=1
  rpm -q NetworkManager >/dev/null 2>&1 || _need_dnf=1
  if [ "$_need_dnf" = "1" ]; then
    log "one or more packages missing — running dnf install"
    sudo_cmd dnf install -y openssh-server open-vm-tools NetworkManager
  else
    log "openssh-server, open-vm-tools, and NetworkManager already installed, skipping dnf"
  fi
  sudo_cmd systemctl enable sshd
  sudo_cmd systemctl restart sshd
  sudo_cmd systemctl enable vmtoolsd
  systemctl is-active --quiet vmtoolsd || sudo_cmd systemctl start vmtoolsd
  sudo_cmd systemctl enable NetworkManager
  sudo_cmd systemctl restart NetworkManager
  configure_network_manager "$target_iface"
fi

log "Remote access bootstrap completed"
hostnamectl || true
ip -4 addr show "$target_iface" || true
ip route || true
if command -v systemctl >/dev/null 2>&1; then
  systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || true
fi
'@

  $BashScript = $BashScript.
    Replace("__SUDO_PASSWORD__", $EscapedPassword).
    Replace("__LINUX_KIND__", $TemplateItem.LinuxKind).
    Replace("__IP_MODE__", $IpMode).
    Replace("__IP_ADDRESS__", $IpAddress).
    Replace("__CIDR__", [string]$Cidr).
    Replace("__GATEWAY__", $Gateway).
    Replace("__DNS_CSV__", $DnsCsv).
    Replace("__LINUX_INTERFACE__", $LinuxInterface)

  $EncodedBashScript = ConvertTo-Base64Utf8 -Value $BashScript
  $GuestExecScript = "printf '%s' '$EncodedBashScript' | base64 -d > /tmp/db-sre-bootstrap.sh && chmod +x /tmp/db-sre-bootstrap.sh && /tmp/db-sre-bootstrap.sh > /tmp/db-sre-bootstrap.log 2>&1; exit `$?"

  Write-Host "Bootstrapping SSH and network for $($TemplateItem.Name) ... (this may take several minutes)"
  $Result = Invoke-VmrunWithProgress -Arguments @(
    "-gu", $LinuxGuestUser,
    "-gp", $LinuxGuestPassword,
    "runScriptInGuest",
    $TemplateItem.VmxPath,
    "/bin/bash",
    $GuestExecScript
  )

  $LocalLog = [System.IO.Path]::GetTempFileName()
  $CopyResult = Invoke-VmrunWithOutput -Arguments @(
    "-gu", $LinuxGuestUser, "-gp", $LinuxGuestPassword,
    "copyFileFromGuestToHost", $TemplateItem.VmxPath,
    "/tmp/db-sre-bootstrap.log", $LocalLog
  )
  if ($CopyResult.ExitCode -eq 0 -and (Test-Path $LocalLog)) {
    Write-Host "--- Bootstrap log: $($TemplateItem.Name) ---"
    Get-Content $LocalLog | ForEach-Object { Write-Host $_ }
    Remove-Item $LocalLog -ErrorAction SilentlyContinue
  }

  if ($Result.ExitCode -ne 0) {
    if ($CopyResult.ExitCode -ne 0) {
      Write-Host "vmrun output:" -ForegroundColor Yellow
      $Result.Output | ForEach-Object { Write-Host $_ }
    }
    throw "Linux template bootstrap failed for $($TemplateItem.Name). ExitCode=$($Result.ExitCode)"
  }
}

function Invoke-WindowsTemplateBootstrap {
  param(
    [Parameter(Mandatory = $true)]
    [object]$TemplateItem
  )

  if ([string]::IsNullOrWhiteSpace($WindowsGuestUser) -or [string]::IsNullOrWhiteSpace($WindowsGuestPassword)) {
    throw "Windows guest credentials are required. Pass -WindowsGuestUser/-WindowsGuestPassword."
  }

  $DnsLiteral = "@(" + (($DnsServers | ForEach-Object { "'$_'" }) -join ", ") + ")"
  $WindowsScript = @"
`$ErrorActionPreference = 'Stop'

function Write-Step {
  param([string]`$Message)
  Write-Host "[db-sre-template-bootstrap] `$Message"
}

`$Password = ConvertTo-SecureString '$WindowsGuestPassword' -AsPlainText -Force
Enable-LocalUser -Name 'Administrator'
Set-LocalUser -Name 'Administrator' -Password `$Password

Write-Step 'Configuring network adapter'
`$Adapter = Get-NetAdapter | Where-Object { `$_.Status -eq 'Up' } | Select-Object -First 1
if (-not `$Adapter) {
  throw 'No active network adapter found.'
}

if ('$IpMode' -eq 'DHCP') {
  Set-NetIPInterface -InterfaceIndex `$Adapter.ifIndex -Dhcp Enabled
  Set-DnsClientServerAddress -InterfaceIndex `$Adapter.ifIndex -ResetServerAddresses
} else {
  `$Existing = Get-NetIPAddress -InterfaceIndex `$Adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { `$_.PrefixOrigin -ne 'WellKnown' }
  foreach (`$Address in `$Existing) {
    Remove-NetIPAddress -InterfaceIndex `$Adapter.ifIndex -IPAddress `$Address.IPAddress -Confirm:`$false -ErrorAction SilentlyContinue
  }
  New-NetIPAddress -InterfaceIndex `$Adapter.ifIndex -IPAddress '$IpAddress' -PrefixLength $Cidr -DefaultGateway '$Gateway'
  Set-DnsClientServerAddress -InterfaceIndex `$Adapter.ifIndex -ServerAddresses $DnsLiteral
}

Set-NetConnectionProfile -InterfaceIndex `$Adapter.ifIndex -NetworkCategory Private

Write-Step 'Enabling Remote Desktop'
Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Value 0
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'

Write-Step 'Installing and enabling OpenSSH Server'
`$Capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if (`$Capability.State -ne 'Installed') {
  Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
}
Set-Service -Name sshd -StartupType Automatic
Start-Service -Name sshd

if (-not (Get-NetFirewallRule -Name 'sshd' -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name 'sshd' -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
} else {
  Enable-NetFirewallRule -Name 'sshd'
}

Write-Step 'Enabling WinRM'
Enable-PSRemoting -Force
winrm quickconfig -q
Set-Service -Name WinRM -StartupType Automatic

Write-Step 'Remote access bootstrap completed'
hostname
ipconfig
Get-Service -Name sshd,WinRM | Select-Object Name,Status,StartType
"@

  $EncodedCommand = ConvertTo-WindowsEncodedCommand -Script $WindowsScript

  Write-Host "Bootstrapping OpenSSH/RDP/WinRM and network for $($TemplateItem.Name) ..."
  $Result = Invoke-VmrunWithOutput -Arguments @(
    "-gu", $WindowsGuestUser,
    "-gp", $WindowsGuestPassword,
    "runProgramInGuest",
    $TemplateItem.VmxPath,
    "-activeWindow",
    "-interactive",
    "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-EncodedCommand",
    $EncodedCommand
  )

  if ($Result.ExitCode -ne 0) {
    Write-Host "vmrun output:" -ForegroundColor Yellow
    $Result.Output | ForEach-Object { Write-Host $_ }
    throw "Windows template bootstrap failed for $($TemplateItem.Name). ExitCode=$($Result.ExitCode)"
  }

  $Result.Output | ForEach-Object { Write-Host $_ }
}

function Test-TemplateSshPort {
  param(
    [Parameter(Mandatory = $true)]
    [object]$TemplateItem
  )

  $Ip = Get-GuestIpAddress -VmxPath $TemplateItem.VmxPath -Wait -TimeoutSec $GuestIpTimeoutSec
  if ([string]::IsNullOrWhiteSpace($Ip)) {
    Write-Warning "Unable to read guest IP for $($TemplateItem.Name)."
    return
  }

  $TcpTest = Test-NetConnection -ComputerName $Ip -Port 22 -InformationLevel Quiet
  [PSCustomObject]@{
    Template = $TemplateItem.Name
    Ip       = $Ip
    SshPort  = if ($TcpTest) { "open" } else { "closed" }
    Putty    = "putty.exe $Ip -P 22"
  }
}

Assert-VmrunAvailable
Assert-TemplateNetworkArguments

$TemplateItems = Get-TemplateInventory -Template $Template

foreach ($TemplateItem in $TemplateItems) {
  Start-TemplateIfNeeded -TemplateItem $TemplateItem

  if ($TemplateItem.OsType -eq "linux") {
    Invoke-LinuxTemplateBootstrap -TemplateItem $TemplateItem
  } else {
    Invoke-WindowsTemplateBootstrap -TemplateItem $TemplateItem
  }
}

Write-Host "Collecting PuTTY connection info..."
$TemplateItems | ForEach-Object { Test-TemplateSshPort -TemplateItem $_ } | Format-Table -AutoSize

# Refresh the named snapshot so clones inherit the bootstrapped state (sshd + vmtoolsd enabled).
foreach ($TemplateItem in $TemplateItems) {
  if ($TemplateItem.OsType -ne "linux") { continue }

  Write-Host "Refreshing '$($script:TemplateSnapshotName)' snapshot for $($TemplateItem.Name)..."

  # Graceful ACPI shutdown — no credentials needed, no VMware Tools dependency
  $null = & $Vmrun stop $TemplateItem.VmxPath soft 2>&1
  $OffDeadline = (Get-Date).AddSeconds(60)
  while ((Get-Date) -lt $OffDeadline -and (Test-VmIsRunning -VmxPath $TemplateItem.VmxPath)) {
    Start-Sleep -Seconds 3
  }
  if (Test-VmIsRunning -VmxPath $TemplateItem.VmxPath) {
    Write-Host "  Forcing power off..."
    $null = & $Vmrun stop $TemplateItem.VmxPath hard 2>&1
    Start-Sleep -Seconds 3
  }

  # Delete old snapshot; errors are non-fatal (snapshot may not exist on first run)
  $DelOut  = & $Vmrun deleteSnapshot $TemplateItem.VmxPath $script:TemplateSnapshotName 2>&1
  $DelExit = $LASTEXITCODE
  if ($DelExit -ne 0) {
    Write-Warning "  Could not delete old snapshot '$($script:TemplateSnapshotName)': $($DelOut -join ' ')"
  }

  # Take a fresh snapshot of the now-bootstrapped state
  $SnapOut  = & $Vmrun snapshot $TemplateItem.VmxPath $script:TemplateSnapshotName 2>&1
  $SnapExit = $LASTEXITCODE
  if ($SnapExit -ne 0) {
    throw "Failed to take '$($script:TemplateSnapshotName)' snapshot for $($TemplateItem.Name). Exit=$SnapExit. $($SnapOut -join ' ')"
  }
  Write-Host "  '$($script:TemplateSnapshotName)' snapshot refreshed for $($TemplateItem.Name)." -ForegroundColor Green
}
