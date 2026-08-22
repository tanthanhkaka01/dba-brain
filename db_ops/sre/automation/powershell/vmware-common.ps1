if ([string]::IsNullOrWhiteSpace($DbSrePayloadJsonBase64)) {
  throw "DbSrePayloadJsonBase64 is required. Run this script through the Python db_sre CLI so config.json is resolved by Python."
}

function Set-DbSreConfigFromPayload {
  param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadJsonBase64
  )

  $PayloadJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($PayloadJsonBase64))
  $RawConfig = $PayloadJson | ConvertFrom-Json
  $script:Vmrun = $RawConfig.vmware.vmrun_path
  $script:VmRoot = $RawConfig.vmware.vm_root
  $script:TemplateName = $RawConfig.vmware.template_name
  $script:TemplateDir = Join-Path $script:VmRoot "templates\$script:TemplateName"
  $script:TemplateVmx = Join-Path $script:TemplateDir "$script:TemplateName.vmx"
  $script:TemplateSnapshotName = $RawConfig.vmware.template_snapshot_name

  $script:GuestUser = $RawConfig.credentials.guest_user
  $script:GuestPassword = $RawConfig.credentials.guest_password
  $script:RootUser = $RawConfig.credentials.root_user
  $script:RootPassword = $RawConfig.credentials.root_password
  $script:WindowAdmistratorPass = $RawConfig.credentials.windows_administrator_password

  $rawSshKey = $RawConfig.credentials.ssh_identity_file
  if ($rawSshKey) {
    $expandedKey = ($rawSshKey -replace '^~', $env:USERPROFILE) -replace '/', '\'
    $script:SshIdentityFile = [System.IO.Path]::GetFullPath($expandedKey)
  } else {
    $script:SshIdentityFile = $null
  }

  $rawPlink = $RawConfig.credentials.plink_path
  $script:PlinkExe = if ($rawPlink) { $rawPlink -replace '/', '\' } else { "C:\Program Files\PuTTY\plink.exe" }

  $script:NetInterface = $RawConfig.vmware.net_interface
  $script:Cidr = [int]$RawConfig.vmware.cidr
  $script:Gateway = $RawConfig.vmware.gateway
  $script:DnsServers = @($RawConfig.vmware.dns_servers)

  $script:SharedRoot = Join-Path $script:VmRoot "shared"
  $script:MysqlRoot = Join-Path $script:VmRoot "mysql"
  $script:PostgresqlRoot = Join-Path $script:VmRoot "postgresql"
  $script:SqlServerRoot = Join-Path $script:VmRoot "sqlserver"
  $script:OracleRacRoot = Join-Path $script:VmRoot "oracle_rac"
  $script:OracleDgRoot = Join-Path $script:VmRoot "oracle_dg"

  $script:MysqlClusterName = $RawConfig.database_defaults.mysql.cluster_name
  $script:MysqlClusterAdminUser = $RawConfig.database_defaults.mysql.cluster_admin_user
  $script:MysqlClusterAdminPassword = $RawConfig.database_defaults.mysql.cluster_admin_password
  $script:MysqlClassicPort = [int]$RawConfig.database_defaults.mysql.classic_port

  $script:PostgresqlClusterName = $RawConfig.database_defaults.postgresql.cluster_name
  $script:PostgresqlVersion = [int]$RawConfig.database_defaults.postgresql.version
  $script:PostgresqlPort = [int]$RawConfig.database_defaults.postgresql.port
  $script:PostgresqlSuperuser = $RawConfig.database_defaults.postgresql.superuser
  $script:PostgresqlSuperuserPassword = $RawConfig.database_defaults.postgresql.superuser_password
  $script:PostgresqlReplicationUser = $RawConfig.database_defaults.postgresql.replication_user
  $script:PostgresqlReplicationPassword = $RawConfig.database_defaults.postgresql.replication_password

  $script:MonitoringNodeName = $RawConfig.observability.monitoring_node_name
  $script:MonitoringNodeIp = $RawConfig.observability.monitoring_node_ip
  $script:PrometheusPort = [int]$RawConfig.observability.ports.prometheus
  $script:AlertmanagerPort = [int]$RawConfig.observability.ports.alertmanager
  $script:GrafanaPort = [int]$RawConfig.observability.ports.grafana
  $script:GrafanaAdminUser = $RawConfig.observability.grafana_admin_user
  $script:GrafanaAdminPassword = $RawConfig.observability.grafana_admin_password
  $script:PrometheusUrl = $RawConfig.observability.endpoints.prometheus
  $script:PrometheusTargetsUrl = $RawConfig.observability.endpoints.prometheus_targets
  $script:AlertmanagerUrl = $RawConfig.observability.endpoints.alertmanager
  $script:GrafanaUrl = $RawConfig.observability.endpoints.grafana

  $script:SharedVmMap = @($RawConfig.inventory.groups.shared | ForEach-Object {
    @{
      Name = $_.name
      Ip = $_.ip
      Role = $_.role
    }
  })

  $script:Nodes = @($RawConfig.inventory.groups.mysql | ForEach-Object { $_.name })
  $script:NodeIpMap = @{}
  foreach ($Node in $RawConfig.inventory.groups.mysql) {
    $script:NodeIpMap[$Node.name] = $Node.ip
  }

  $script:PostgresqlNodes = @($RawConfig.inventory.groups.postgresql | ForEach-Object { $_.name })
  $script:PostgresqlNodeIpMap = @{}
  foreach ($Node in $RawConfig.inventory.groups.postgresql) {
    $script:PostgresqlNodeIpMap[$Node.name] = $Node.ip
  }

  $SqlServerInventory = @($RawConfig.inventory.groups.sqlserver)
  $script:SqlServerNodes = @($SqlServerInventory | Where-Object { $_ } | ForEach-Object { $_.name })
  $script:SqlServerNodeIpMap = @{}
  foreach ($Node in ($SqlServerInventory | Where-Object { $_ })) {
    $script:SqlServerNodeIpMap[$Node.name] = $Node.ip
  }

  $OracleRacInventory = @($RawConfig.inventory.groups.oracle_rac)
  $script:OracleRacNodes = @($OracleRacInventory | Where-Object { $_ } | ForEach-Object { $_.name })
  $script:OracleRacNodeIpMap = @{}
  foreach ($Node in ($OracleRacInventory | Where-Object { $_ })) {
    $script:OracleRacNodeIpMap[$Node.name] = $Node.ip
  }

  $OracleDgInventory = @($RawConfig.inventory.groups.oracle_dg)
  $script:OracleDgNodes = @($OracleDgInventory | Where-Object { $_ } | ForEach-Object { $_.name })
  $script:OracleDgNodeIpMap = @{}
  foreach ($Node in ($OracleDgInventory | Where-Object { $_ })) {
    $script:OracleDgNodeIpMap[$Node.name] = $Node.ip
  }

  $script:OracleGridInstallerPath = $RawConfig.oracle.grid_installer_path
  $script:OracleDbInstallerPath   = $RawConfig.oracle.db_installer_path
  $script:OracleInstallStage      = if ($RawConfig.oracle.install_stage) { $RawConfig.oracle.install_stage } else { "/u01/app/stage" }
}

Set-DbSreConfigFromPayload -PayloadJsonBase64 $DbSrePayloadJsonBase64

function Assert-VmrunAvailable {
  if (-not (Test-Path $Vmrun)) {
    throw "vmrun.exe not found at: $Vmrun"
  }
}

function Assert-TemplateVmxAvailable {
  if (-not (Test-Path $TemplateVmx)) {
    throw "Template VMX not found at: $TemplateVmx"
  }
}

function Get-SharedVmInventory {
  foreach ($Vm in $SharedVmMap) {
    [PSCustomObject]@{
      Name       = $Vm.Name
      Role       = $Vm.Role
      ExpectedIp = $Vm.Ip
      RootPath   = $SharedRoot
      VmxPath    = Join-Path (Join-Path $SharedRoot $Vm.Name) "$($Vm.Name).vmx"
    }
  }
}

function Get-MysqlVmInventory {
  foreach ($Node in $Nodes) {
    [PSCustomObject]@{
      Name       = $Node
      Role       = "mysql"
      ExpectedIp = $NodeIpMap[$Node]
      RootPath   = $MysqlRoot
      VmxPath    = Join-Path (Join-Path $MysqlRoot $Node) "$Node.vmx"
    }
  }
}

function Get-PostgresqlVmInventory {
  foreach ($Node in $PostgresqlNodes) {
    [PSCustomObject]@{
      Name       = $Node
      Role       = "postgresql"
      ExpectedIp = $PostgresqlNodeIpMap[$Node]
      RootPath   = $PostgresqlRoot
      VmxPath    = Join-Path (Join-Path $PostgresqlRoot $Node) "$Node.vmx"
    }
  }
}

function Get-SqlServerVmInventory {
  foreach ($Node in $SqlServerNodes) {
    [PSCustomObject]@{
      Name       = $Node
      Role       = "sqlserver"
      ExpectedIp = $SqlServerNodeIpMap[$Node]
      RootPath   = $SqlServerRoot
      VmxPath    = Join-Path (Join-Path $SqlServerRoot $Node) "$Node.vmx"
    }
  }
}

function Get-OracleRacVmInventory {
  foreach ($Node in $OracleRacNodes) {
    [PSCustomObject]@{
      Name       = $Node
      Role       = "oracle_rac"
      ExpectedIp = $OracleRacNodeIpMap[$Node]
      RootPath   = $OracleRacRoot
      VmxPath    = Join-Path (Join-Path $OracleRacRoot $Node) "$Node.vmx"
    }
  }
}

function Get-OracleDgVmInventory {
  foreach ($Node in $OracleDgNodes) {
    [PSCustomObject]@{
      Name       = $Node
      Role       = "oracle_dg"
      ExpectedIp = $OracleDgNodeIpMap[$Node]
      RootPath   = $OracleDgRoot
      VmxPath    = Join-Path (Join-Path $OracleDgRoot $Node) "$Node.vmx"
    }
  }
}

function Get-VmInventory {
  param(
    [ValidateSet("shared", "mysql", "postgresql", "sqlserver", "oracle_rac", "oracle_dg", "all")]
    [string]$Group = "all"
  )

  switch ($Group) {
    "shared"      { return @(Get-SharedVmInventory) }
    "mysql"       { return @(Get-MysqlVmInventory) }
    "postgresql"  { return @(Get-PostgresqlVmInventory) }
    "sqlserver"   { return @(Get-SqlServerVmInventory) }
    "oracle_rac"  { return @(Get-OracleRacVmInventory) }
    "oracle_dg"   { return @(Get-OracleDgVmInventory) }
    "all"         { return @((Get-SharedVmInventory) + (Get-MysqlVmInventory) + (Get-PostgresqlVmInventory) + (Get-SqlServerVmInventory) + (Get-OracleRacVmInventory) + (Get-OracleDgVmInventory)) }
  }
}

function Get-VmInventoryByName {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$VmName
  )

  $AllInventory = @(Get-VmInventory -Group "all")
  $InventoryMap = @{}

  foreach ($Vm in $AllInventory) {
    $InventoryMap[$Vm.Name] = $Vm
  }

  $ResolvedInventory = @()
  $MissingNames = @()

  foreach ($Name in $VmName) {
    if ($InventoryMap.ContainsKey($Name)) {
      $ResolvedInventory += $InventoryMap[$Name]
    } else {
      $MissingNames += $Name
    }
  }

  if ($MissingNames.Count -gt 0) {
    throw "VM name not found in inventory: $($MissingNames -join ', ')"
  }

  return $ResolvedInventory
}

function Get-RunningVmPathMap {
  $RunningVmPaths = @{}
  $VmrunListOutput = & $Vmrun list 2>&1
  $VmrunListExitCode = $LASTEXITCODE

  if ($VmrunListExitCode -eq 0) {
    foreach ($Line in $VmrunListOutput) {
      if ($Line -match '\.vmx$') {
        $RunningVmPaths[$Line.Trim()] = $true
      }
    }
  }

  return @{
    Paths    = $RunningVmPaths
    ExitCode = $VmrunListExitCode
    Output   = $VmrunListOutput
  }
}

function Get-VmMacFromVmx {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VmxPath
  )

  if (-not (Test-Path $VmxPath)) { return $null }
  $Content = Get-Content $VmxPath -Raw -ErrorAction SilentlyContinue
  if (-not $Content) { return $null }

  $Match = [regex]::Match($Content, '(?im)^ethernet\d+\.generatedAddress\s*=\s*"([0-9a-fA-F:]+)"')
  if (-not $Match.Success) {
    $Match = [regex]::Match($Content, '(?im)^ethernet\d+\.address\s*=\s*"([0-9a-fA-F:]+)"')
  }
  if ($Match.Success) { return $Match.Groups[1].Value.ToLower() }
  return $null
}

function Get-VmCurrentIpFromLeases {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VmxPath,
    [string]$LeasesFile = "C:\ProgramData\VMware\vmnetdhcp.leases"
  )

  $Mac = Get-VmMacFromVmx -VmxPath $VmxPath
  if (-not $Mac) { return $null }
  if (-not (Test-Path $LeasesFile)) { return $null }

  $Content = Get-Content $LeasesFile -Raw -ErrorAction SilentlyContinue
  if (-not $Content) { return $null }

  $BestIp    = $null
  $BestStart = [datetime]::MinValue

  $Blocks = [regex]::Matches($Content, '(?s)lease\s+([\d.]+)\s*\{([^}]+)\}')
  foreach ($Block in $Blocks) {
    $Ip   = $Block.Groups[1].Value.Trim()
    $Body = $Block.Groups[2].Value

    $MacMatch = [regex]::Match($Body, '(?i)hardware\s+ethernet\s+([0-9a-fA-F:]+)')
    if (-not $MacMatch.Success) { continue }
    if ($MacMatch.Groups[1].Value.ToLower() -ne $Mac) { continue }

    $StartMatch = [regex]::Match($Body, '(?i)starts\s+\d+\s+(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})')
    if ($StartMatch.Success) {
      $StartStr  = $StartMatch.Groups[1].Value -replace '/', '-'
      $StartTime = [datetime]::ParseExact($StartStr, 'yyyy-MM-dd HH:mm:ss', $null)
      if ($StartTime -gt $BestStart) {
        $BestStart = $StartTime
        $BestIp    = $Ip
      }
    } elseif (-not $BestIp) {
      $BestIp = $Ip
    }
  }

  return $BestIp
}

function Get-GuestIpAddress {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VmxPath,
    [switch]$Wait,
    [int]$TimeoutSec = 120
  )

  $Arguments = @("getGuestIPAddress", $VmxPath)

  if ($Wait) {
    $Arguments += "-wait"
  }

  if ($Wait) {
    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $Vmrun
    $StartInfo.Arguments = ($Arguments | ForEach-Object {
      if ($_ -match '\s') { '"{0}"' -f $_ } else { $_ }
    }) -join ' '
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.CreateNoWindow = $true

    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    $null = $Process.Start()

    if (-not $Process.WaitForExit($TimeoutSec * 1000)) {
      try {
        $Process.Kill()
      } catch {
      }
      return $null
    }

    $IpOutput = @($Process.StandardOutput.ReadToEnd(), $Process.StandardError.ReadToEnd()) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $IpExitCode = $Process.ExitCode
  } else {
    $IpOutput = & $Vmrun @Arguments 2>&1
    $IpExitCode = $LASTEXITCODE
  }

  if ($IpExitCode -ne 0) {
    return $null
  }

  $Candidate = ($IpOutput | Select-Object -Last 1).ToString().Trim()

  if ([string]::IsNullOrWhiteSpace($Candidate)) {
    return $null
  }

  return $Candidate
}

function Wait-GuestIpAddress {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VmxPath,
    [int]$TimeoutSec = 120,
    [int]$PollIntervalSec = 5
  )

  $Deadline = (Get-Date).AddSeconds($TimeoutSec)

  do {
    $Ip = Get-GuestIpAddress -VmxPath $VmxPath

    if (-not [string]::IsNullOrWhiteSpace($Ip)) {
      return $Ip
    }

    Start-Sleep -Seconds $PollIntervalSec
  } while ((Get-Date) -lt $Deadline)

  return $null
}

function Get-GuestIpAddresses {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VmxPath,
    [switch]$Wait,
    [int]$TimeoutSec = 120
  )

  $Credential = $null
  $ReachableIp = $null

  try {
    $Credential = New-GuestCredential
  } catch {
    $Credential = $null
  }

  $ReachableIp = if ($Wait) {
    Get-GuestIpAddress -VmxPath $VmxPath -Wait -TimeoutSec $TimeoutSec
  } else {
    Get-GuestIpAddress -VmxPath $VmxPath
  }

  if (-not $ReachableIp) {
    return @()
  }

  if ($Credential) {
    $bashScript = @"
ip -o -4 addr show up scope global | awk '{print $4}' | cut -d/ -f1
"@

    $Output = & $Vmrun -gu $Credential.User -gp $Credential.Password runScriptInGuest $VmxPath /bin/bash $bashScript 2>&1
    $ExitCode = $LASTEXITCODE

    if ($ExitCode -eq 0) {
      $Ips = $Output |
        ForEach-Object { $_.ToString().Trim() } |
        Where-Object { $_ -match '^\d{1,3}(\.\d{1,3}){3}$' } |
        Select-Object -Unique

      if ($Ips.Count -gt 0) {
        return @($Ips)
      }
    }
  }

  if ($ReachableIp) {
    return @($ReachableIp)
  }

  return @()
}

function Get-GuestHostname {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VmxPath,
    [int]$TimeoutSec = 5,
    [int]$PollIntervalSec = 1
  )

  $Credential = $null

  try {
    $Credential = New-GuestCredential
  } catch {
    return $null
  }

  $ReachableIp = Get-GuestIpAddress -VmxPath $VmxPath

  if (-not $ReachableIp) {
    return $null
  }

  $Deadline = (Get-Date).AddSeconds($TimeoutSec)
  $HostTempPath = Join-Path $env:TEMP ("db-sre-hostname-{0}.txt" -f ([Guid]::NewGuid().ToString("N")))

  try {
    do {
      if (Test-Path $HostTempPath) {
        Remove-Item -Path $HostTempPath -Force -ErrorAction SilentlyContinue
      }

      $null = & $Vmrun -gu $Credential.User -gp $Credential.Password copyFileFromGuestToHost $VmxPath /etc/hostname $HostTempPath 2>&1
      $ExitCode = $LASTEXITCODE

      if ($ExitCode -eq 0 -and (Test-Path $HostTempPath)) {
        $Candidate = Get-Content -Path $HostTempPath -ErrorAction SilentlyContinue |
          ForEach-Object { $_.ToString().Trim() } |
          Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
          Select-Object -First 1

        if (-not [string]::IsNullOrWhiteSpace($Candidate)) {
          return $Candidate
        }
      }

      Start-Sleep -Seconds $PollIntervalSec
    } while ((Get-Date) -lt $Deadline)

    return $null
  } finally {
    Remove-Item -Path $HostTempPath -Force -ErrorAction SilentlyContinue
  }
}

function Get-GuestInfoStatus {
  param(
    [string]$GuestHostname,
    [string[]]$IpAddresses
  )

  if (-not [string]::IsNullOrWhiteSpace($GuestHostname) -and $IpAddresses.Count -gt 0) {
    return "ok"
  }

  if (-not [string]::IsNullOrWhiteSpace($GuestHostname) -or $IpAddresses.Count -gt 0) {
    return "partial"
  }

  return "unavailable"
}

function Resolve-GuestHostnameValue {
  param(
    [string]$QueriedGuestHostname,
    [string]$FallbackHostname,
    [string[]]$IpAddresses
  )

  if (-not [string]::IsNullOrWhiteSpace($QueriedGuestHostname)) {
    return $QueriedGuestHostname
  }

  return "<unavailable>"
}

function Get-VmdkCapacityGB {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VmdkPath
  )

  if (-not (Test-Path $VmdkPath)) {
    return $null
  }

  $DescriptorLine = Get-Content -Path $VmdkPath -ErrorAction SilentlyContinue |
    Where-Object { $_ -match '^\s*RW\s+\d+\s+\w+\s+' } |
    Select-Object -First 1

  if ($DescriptorLine -and $DescriptorLine -match '^\s*RW\s+(?<Sectors>\d+)\s+\w+\s+') {
    $CapacityBytes = [double]$Matches.Sectors * 512
    return [Math]::Round(($CapacityBytes / 1GB), 2)
  }

  $FileItem = Get-Item -Path $VmdkPath -ErrorAction SilentlyContinue

  if ($FileItem) {
    return [Math]::Round(($FileItem.Length / 1GB), 2)
  }

  return $null
}

function Get-VmHardwareInfo {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VmxPath
  )

  if (-not (Test-Path $VmxPath)) {
    return @{
      CpuCount    = $null
      MemoryMB    = $null
      MemoryGB    = $null
      DiskCount   = 0
      DiskTotalGB = $null
      DiskSummary = "<unavailable>"
      Disks       = @()
    }
  }

  $VmxLines = Get-Content -Path $VmxPath
  $VmxDirectory = Split-Path -Path $VmxPath -Parent
  $CpuCount = $null
  $MemoryMB = $null
  $Disks = @()

  foreach ($Line in $VmxLines) {
    if (-not $CpuCount -and $Line -match '^\s*numvcpus\s*=\s*"(?<Value>\d+)"\s*$') {
      $CpuCount = [int]$Matches.Value
      continue
    }

    if (-not $MemoryMB -and $Line -match '^\s*memsize\s*=\s*"(?<Value>\d+)"\s*$') {
      $MemoryMB = [int]$Matches.Value
      continue
    }

    if ($Line -match '^\s*(?<Controller>(?:scsi|sata|ide|nvme)\d+:\d+)\.fileName\s*=\s*"(?<FileName>[^"]+\.vmdk)"\s*$') {
      $DiskFileName = $Matches.FileName
      $DiskPath = if ([System.IO.Path]::IsPathRooted($DiskFileName)) {
        $DiskFileName
      } else {
        Join-Path $VmxDirectory $DiskFileName
      }

      $DiskCapacityGB = Get-VmdkCapacityGB -VmdkPath $DiskPath

      $Disks += [PSCustomObject]@{
        Controller = $Matches.Controller
        FileName   = $DiskFileName
        Path       = $DiskPath
        CapacityGB = $DiskCapacityGB
      }
    }
  }

  $KnownDiskCapacities = @($Disks | Where-Object { $null -ne $_.CapacityGB } | ForEach-Object { $_.CapacityGB })
  $DiskTotalGB = if ($KnownDiskCapacities.Count -gt 0) {
    [Math]::Round((($KnownDiskCapacities | Measure-Object -Sum).Sum), 2)
  } else {
    $null
  }

  $DiskSummary = if ($Disks.Count -gt 0) {
    ($Disks | ForEach-Object {
      $CapacityLabel = if ($null -ne $_.CapacityGB) { "$($_.CapacityGB)GB" } else { "unknown" }
      "$($_.Controller): $CapacityLabel ($($_.FileName))"
    }) -join "; "
  } else {
    "<none>"
  }

  return @{
    CpuCount    = $CpuCount
    MemoryMB    = $MemoryMB
    MemoryGB    = if ($null -ne $MemoryMB) { [Math]::Round(($MemoryMB / 1024), 2) } else { $null }
    DiskCount   = $Disks.Count
    DiskTotalGB = $DiskTotalGB
    DiskSummary = $DiskSummary
    Disks       = @($Disks)
  }
}

function Get-VmInfo {
  param(
    [Parameter(Mandatory = $true)]
    [object[]]$Inventory,
    [switch]$WaitForGuestIp,
    [int]$GuestIpTimeoutSec = 120
  )

  $RunningVmState = Get-RunningVmPathMap

  if ($RunningVmState.ExitCode -ne 0) {
    Write-Warning "Unable to query running VM list from vmrun. PowerState will be reported as Unknown."
  }

  foreach ($Vm in $Inventory) {
    $ResolvedVmxPath = $null
    $PowerState = "Missing"
    $CurrentIp = "<unavailable>"
    $GuestHostname = "<unavailable>"
    $GuestInfoStatus = "unavailable"
    $HardwareInfo = @{
      CpuCount    = $null
      MemoryMB    = $null
      MemoryGB    = $null
      DiskCount   = 0
      DiskTotalGB = $null
      DiskSummary = "<unavailable>"
      Disks       = @()
    }

    if (-not (Test-Path $Vm.VmxPath)) {
      [PSCustomObject]@{
        VmName          = $Vm.Name
        GuestHostname   = $GuestHostname
        Role            = $Vm.Role
        CpuCount        = $HardwareInfo.CpuCount
        MemoryGB        = $HardwareInfo.MemoryGB
        MemoryMB        = $HardwareInfo.MemoryMB
        DiskCount       = $HardwareInfo.DiskCount
        DiskTotalGB     = $HardwareInfo.DiskTotalGB
        DiskSummary     = $HardwareInfo.DiskSummary
        Disks           = $HardwareInfo.Disks
        CurrentIp       = $CurrentIp
        GuestInfoStatus = $GuestInfoStatus
        PowerState      = $PowerState
        Vmx             = $Vm.VmxPath
      }
      continue
    }

    $ResolvedVmxPath = (Resolve-Path $Vm.VmxPath).Path
    $HardwareInfo = Get-VmHardwareInfo -VmxPath $Vm.VmxPath

    if ($RunningVmState.ExitCode -eq 0) {
      if ($RunningVmState.Paths.ContainsKey($ResolvedVmxPath)) {
        $PowerState = "Running"
      } else {
        $PowerState = "Stopped"
      }
    } else {
      $PowerState = "Unknown"
    }

    if ($PowerState -eq "Running") {
      $Ips = Get-GuestIpAddresses -VmxPath $Vm.VmxPath -Wait:$WaitForGuestIp -TimeoutSec $GuestIpTimeoutSec
      $QueriedGuestHostname = Get-GuestHostname -VmxPath $Vm.VmxPath
      $GuestHostname = Resolve-GuestHostnameValue -QueriedGuestHostname $QueriedGuestHostname -FallbackHostname $Vm.Name -IpAddresses $Ips

      if ($Ips.Count -gt 0) {
        $CurrentIp = $Ips -join ", "
      }

      $GuestInfoStatus = Get-GuestInfoStatus -GuestHostname $GuestHostname -IpAddresses $Ips
    }

    [PSCustomObject]@{
      VmName          = $Vm.Name
      GuestHostname   = $GuestHostname
      Role            = $Vm.Role
      CpuCount        = $HardwareInfo.CpuCount
      MemoryGB        = $HardwareInfo.MemoryGB
      MemoryMB        = $HardwareInfo.MemoryMB
      DiskCount       = $HardwareInfo.DiskCount
      DiskTotalGB     = $HardwareInfo.DiskTotalGB
      DiskSummary     = $HardwareInfo.DiskSummary
      Disks           = $HardwareInfo.Disks
      CurrentIp       = $CurrentIp
      GuestInfoStatus = $GuestInfoStatus
      PowerState      = $PowerState
      Vmx             = $ResolvedVmxPath
    }
  }
}

function Set-VmxDisplayName {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VmxPath,
    [Parameter(Mandatory = $true)]
    [string]$DisplayName
  )

  if (-not (Test-Path $VmxPath)) {
    throw "VMX not found: $VmxPath"
  }

  $Lines = Get-Content -Path $VmxPath

  if ($Lines -match '^\s*displayName\s*=') {
    $Lines = $Lines -replace '^\s*displayName\s*=\s*".*"$', "displayName = `"$DisplayName`""
  } else {
    $Lines += "displayName = `"$DisplayName`""
  }

  Set-Content -Path $VmxPath -Value $Lines
}

function Start-VmInventory {
  param(
    [Parameter(Mandatory = $true)]
    [object[]]$Inventory
  )

  $StartFailures = @()

  foreach ($Vm in $Inventory) {
    if (-not (Test-Path $Vm.VmxPath)) {
      Write-Warning "VMX not found for $($Vm.Name): $($Vm.VmxPath)"
      continue
    }

    Write-Host "Starting $($Vm.Name) ..."
    $Output = & $Vmrun start $Vm.VmxPath nogui 2>&1
    $ExitCode = $LASTEXITCODE

    if ($ExitCode -ne 0) {
      Write-Warning "Failed to start $($Vm.Name)."
      $Output | ForEach-Object { Write-Warning $_ }
      $StartFailures += $Vm.Name
    }
  }

  return $StartFailures
}

function Clone-VmInventory {
  param(
    [Parameter(Mandatory = $true)]
    [object[]]$Inventory,
    [switch]$Force
  )

  foreach ($Vm in $Inventory) {
    $NodeDir = Join-Path $Vm.RootPath $Vm.Name

    if (Test-Path $NodeDir) {
      if ($Force) {
        Write-Host "Removing existing directory: $NodeDir"
        Remove-Item -Recurse -Force $NodeDir
      } else {
        Write-Host "Skipping existing directory: $NodeDir"
        continue
      }
    }

    New-Item -ItemType Directory -Force -Path $NodeDir | Out-Null
    Write-Host "Cloning $($Vm.Name) from $TemplateVmx using snapshot '$TemplateSnapshotName' ..."

    $Output = & $Vmrun clone $TemplateVmx $Vm.VmxPath full "-snapshot=$TemplateSnapshotName" 2>&1
    $ExitCode = $LASTEXITCODE

    if ($ExitCode -ne 0) {
      Write-Host "vmrun output:" -ForegroundColor Yellow
      $Output | ForEach-Object { Write-Host $_ }
      throw "Clone failed for $($Vm.Name). ExitCode=$ExitCode"
    }

    Set-VmxDisplayName -VmxPath $Vm.VmxPath -DisplayName $Vm.Name

    # Prevent "Did you move or copy this VM?" dialog when starting with vmrun nogui
    $VmxLines = Get-Content -Path $Vm.VmxPath
    if (-not ($VmxLines -match 'uuid\.action')) {
      Add-Content -Path $Vm.VmxPath -Value "uuid.action = `"create`""
    }

    Write-Host "Created: $($Vm.VmxPath)"
  }
}

function Test-VmIsRunning {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VmxPath
  )

  $RunningVmState = Get-RunningVmPathMap

  if ($RunningVmState.ExitCode -ne 0) {
    throw "Unable to query running VM list from vmrun."
  }

  $ResolvedVmxPath = if (Test-Path $VmxPath) { (Resolve-Path $VmxPath).Path } else { $VmxPath }
  return $RunningVmState.Paths.ContainsKey($ResolvedVmxPath)
}

function Set-VmResourcesForInventory {
  param(
    [Parameter(Mandatory = $true)]
    [object[]]$Inventory,
    [int]$CpuCount,
    [int]$MemoryGB,
    [int]$MemoryMB
  )

  if (-not $CpuCount -and -not $MemoryGB -and -not $MemoryMB) {
    throw "Specify at least one of -CpuCount, -MemoryGB, or -MemoryMB."
  }

  if ($MemoryGB -and $MemoryMB) {
    throw "Use either -MemoryGB or -MemoryMB, not both."
  }

  if ($MemoryGB) {
    $MemoryMB = $MemoryGB * 1024
  }

  foreach ($Vm in $Inventory) {
    if (-not (Test-Path $Vm.VmxPath)) {
      Write-Warning "VMX not found for $($Vm.Name): $($Vm.VmxPath)"
      continue
    }

    if (Test-VmIsRunning -VmxPath $Vm.VmxPath) {
      throw "VM must be powered off before changing CPU/RAM: $($Vm.Name)"
    }

    $Lines = Get-Content $Vm.VmxPath

    if ($CpuCount) {
      if ($Lines -match '^numvcpus = ') {
        $Lines = $Lines -replace '^numvcpus = ".*"$', "numvcpus = `"$CpuCount`""
      } else {
        $Lines += "numvcpus = `"$CpuCount`""
      }
    }

    if ($MemoryMB) {
      if ($Lines -match '^memsize = ') {
        $Lines = $Lines -replace '^memsize = ".*"$', "memsize = `"$MemoryMB`""
      } else {
        $Lines += "memsize = `"$MemoryMB`""
      }
    }

    Set-Content -Path $Vm.VmxPath -Value $Lines

    [PSCustomObject]@{
      VmName    = $Vm.Name
      CpuCount  = if ($CpuCount) { $CpuCount } else { $null }
      MemoryMB  = if ($MemoryMB) { $MemoryMB } else { $null }
      Vmx       = $Vm.VmxPath
    }
  }
}

function New-NetplanYamlDnsBlock {
  return ($DnsServers | ForEach-Object { "          - $_" }) -join "`n"
}

function ConvertTo-BashSingleQuotedLiteral {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Value
  )

  return $Value -replace "'", "'`"'" 
}

function ConvertTo-Base64Utf8 {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Value
  )

  # Normalize CRLF to LF so the decoded script runs correctly in bash on Linux.
  $normalized = $Value -replace "`r`n", "`n"
  return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($normalized))
}

function Invoke-SshCommand {
  param(
    [Parameter(Mandatory = $true)]
    [string]$TargetHost,
    [Parameter(Mandatory = $true)]
    [string]$Command,
    [string]$User = $script:GuestUser
  )

  $sshArgs = @(
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=2",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "BatchMode=yes"
  )
  if ($script:SshIdentityFile) {
    $sshArgs += @("-i", $script:SshIdentityFile)
  }
  $sshArgs += @("$User@$TargetHost", $Command)

  $Output = & ssh @sshArgs 2>&1
  return @{
    Output   = $Output
    ExitCode = $LASTEXITCODE
  }
}

function Invoke-PlinkCommand {
  param(
    [Parameter(Mandatory = $true)]
    [string]$TargetHost,
    [Parameter(Mandatory = $true)]
    [string]$RemoteCommand,
    [string]$User = $script:GuestUser,
    [string]$Password = $script:GuestPassword,
    [int]$TimeoutSec = 120
  )

  if (-not (Test-Path $script:PlinkExe)) {
    throw "plink.exe not found: $($script:PlinkExe). Install PuTTY or set credentials.plink_path in config."
  }

  $escapedPw = $Password -replace '"', '\"'

  # Resolve -hostkey argument. When the host key is not in PuTTY's registry and
  # stdin is a pipe (non-interactive), plink writes the prompt to stderr but reads
  # the answer from the console — not from the pipe — causing it to hang
  # indefinitely. Work around this by probing with -batch to extract the SHA256
  # fingerprint from stderr, then passing -hostkey explicitly so plink accepts
  # the key without any interactive prompt.
  $hostKeyArgs = @()
  $regPath = "HKCU:\Software\SimonTatham\PuTTY\SshHostKeys"
  $regProps = Get-ItemProperty -Path $regPath -ErrorAction SilentlyContinue
  $isCached = $regProps -and ($regProps.PSObject.Properties | Where-Object { $_.Name -like "*@22:$TargetHost" })
  if (-not $isCached) {
    $psiProbe = New-Object System.Diagnostics.ProcessStartInfo
    $psiProbe.FileName = $script:PlinkExe
    $psiProbe.Arguments = "-batch -pw `"$escapedPw`" `"$User@$TargetHost`" exit"
    $psiProbe.UseShellExecute = $false
    $psiProbe.RedirectStandardInput = $true
    $psiProbe.RedirectStandardOutput = $true
    $psiProbe.RedirectStandardError = $true
    $psiProbe.CreateNoWindow = $true
    $pProbe = New-Object System.Diagnostics.Process
    $pProbe.StartInfo = $psiProbe
    $null = $pProbe.Start()
    $pProbe.StandardInput.Close()
    # ReadToEnd blocks until plink exits (small output, no deadlock risk here).
    $probeStderr = $pProbe.StandardError.ReadToEnd()
    $null = $pProbe.WaitForExit(10000)
    if ($probeStderr -match 'SHA256:([A-Za-z0-9+/=]+)') {
      $hostKeyArgs = @('-hostkey', "SHA256:$($Matches[1])")
    }
  }

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $script:PlinkExe
  $psi.Arguments = ($hostKeyArgs + @('-batch', "-pw `"$escapedPw`"", "`"$User@$TargetHost`"", "`"$RemoteCommand`"")) -join ' '
  $psi.UseShellExecute = $false
  $psi.RedirectStandardInput = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.CreateNoWindow = $true

  $proc = New-Object System.Diagnostics.Process
  $proc.StartInfo = $psi
  $null = $proc.Start()
  $proc.StandardInput.Close()

  # Read stdout and stderr asynchronously to prevent the deadlock that occurs
  # when both streams are redirected and one fills its buffer while the caller
  # is blocked in WaitForExit.
  $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
  $stderrTask  = $proc.StandardError.ReadToEndAsync()

  if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
    try { $proc.Kill() } catch {}
    return @{ Output = @("plink timed out after ${TimeoutSec}s"); ExitCode = -1 }
  }

  $stdout = $stdoutTask.GetAwaiter().GetResult()
  $stderr  = $stderrTask.GetAwaiter().GetResult()
  $allOutput = @($stdout, $stderr) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

  return @{ Output = $allOutput; ExitCode = $proc.ExitCode }
}

function Wait-SshAvailable {
  param(
    [Parameter(Mandatory = $true)]
    [string]$TargetHost,
    [string]$User = $script:GuestUser,
    [string]$Password = $script:GuestPassword,
    [int]$TimeoutSec = 180,
    [int]$PollIntervalSec = 10
  )

  $Deadline = (Get-Date).AddSeconds($TimeoutSec)
  Write-Host "  Waiting for SSH on $TargetHost (up to ${TimeoutSec}s) ..."

  do {
    Start-Sleep -Seconds $PollIntervalSec
    $result = Invoke-PlinkCommand -TargetHost $TargetHost -User $User -Password $Password -RemoteCommand "echo ssh-ok" -TimeoutSec 15
    if ($result.ExitCode -eq 0 -and ($result.Output -join '') -match 'ssh-ok') {
      return $true
    }
  } while ((Get-Date) -lt $Deadline)

  return $false
}

function Invoke-SshGuestIdentityFix {
  param(
    [Parameter(Mandatory = $true)]
    [object[]]$Inventory,
    [Parameter(Mandatory = $true)]
    [hashtable]$CurrentIpMap,
    [string]$GuestUser = $script:GuestUser,
    [string]$GuestPassword = $script:GuestPassword,
    [switch]$SkipReboot,
    # Disable kdump and multipathd before reboot. Required for oracle_rac: kdump's kexec
    # call panics the kernel when the lsilogic shared SCSI bus is attached.
    [switch]$DisableKdumpMultipath
  )

  $DnsString = $DnsServers -join ' '
  $EscapedGuestPassword = ConvertTo-BashSingleQuotedLiteral -Value $GuestPassword

  # Validate all inventory entries before waiting
  foreach ($Vm in $Inventory) {
    if (-not $CurrentIpMap.ContainsKey($Vm.Name)) {
      throw "No current IP provided for $($Vm.Name). Ensure -CurrentIpMapJson contains an entry for this VM."
    }
    if (-not $Vm.ExpectedIp) {
      throw "No target IP configured for $($Vm.Name) in inventory."
    }
  }

  # Poll all VMs in round-robin until SSH is reachable on each or the deadline expires.
  # A shared deadline means VMs that boot in parallel are detected together instead of
  # paying up to N seconds sequentially per VM (critical for oracle_rac with shared SCSI
  # disks, which delay full boot significantly past the ARP detection that ends start-vms).
  $SshWaitSec = 1200
  $SshDeadline = (Get-Date).AddSeconds($SshWaitSec)
  $SshReadyMap = @{}
  Write-Host "Waiting for SSH on $($Inventory.Count) VM(s) (up to ${SshWaitSec}s) ..."
  do {
    foreach ($Vm in $Inventory) {
      if ($SshReadyMap[$Vm.Name]) { continue }
      $Ip = $CurrentIpMap[$Vm.Name]
      $r = Invoke-PlinkCommand -TargetHost $Ip -User $GuestUser -Password $GuestPassword -RemoteCommand "echo ssh-ok" -TimeoutSec 10
      if ($r.ExitCode -eq 0 -and ($r.Output -join '') -match 'ssh-ok') {
        $SshReadyMap[$Vm.Name] = $true
        Write-Host "  SSH ready: $($Vm.Name) at $Ip"
      }
    }
    $pendingCount = @($Inventory | Where-Object { -not $SshReadyMap[$_.Name] }).Count
    if ($pendingCount -eq 0 -or (Get-Date) -ge $SshDeadline) { break }
    Start-Sleep -Seconds 10
  } while ($true)

  foreach ($Vm in $Inventory) {
    if (-not $SshReadyMap[$Vm.Name]) {
      throw "SSH not reachable on $($CurrentIpMap[$Vm.Name]) after ${SshWaitSec}s for $($Vm.Name). VM may still be booting."
    }
  }

  $DisableKdumpMultipathVal = if ($DisableKdumpMultipath) { '1' } else { '0' }

  foreach ($Vm in $Inventory) {
    $CurrentIp = $CurrentIpMap[$Vm.Name]
    $TargetIp  = $Vm.ExpectedIp
    $VmName    = $Vm.Name

    Write-Host "Fixing guest identity for ${VmName}: $CurrentIp -> $TargetIp ..."

    $bashScript = @"
set -euo pipefail
SUDO_PASSWORD='$EscapedGuestPassword'

log() { printf '[db-sre-fix] %s\n' "`$*"; }
sudo_cmd() { printf '%s\n' "`$SUDO_PASSWORD" | sudo -S -p '' -- "`$@"; }

log 'Setting hostname to $VmName'
sudo_cmd hostnamectl set-hostname '$VmName'

log 'Refreshing machine-id'
sudo_cmd truncate -s 0 /etc/machine-id
sudo_cmd rm -f /var/lib/dbus/machine-id
sudo_cmd systemd-machine-id-setup 2>/dev/null || \
  sudo_cmd sh -c 'head -c 16 /dev/urandom | od -An -tx1 | tr -d " \n" > /etc/machine-id && chmod 444 /etc/machine-id'
log "machine-id: `$(cat /etc/machine-id)"

TARGET_IFACE='$NetInterface'
if ! ip link show "`$TARGET_IFACE" >/dev/null 2>&1; then
  TARGET_IFACE="`$(ip route show default 2>/dev/null | awk '/default/ { print `$5; exit }')"
fi
log "Interface: `$TARGET_IFACE"

if command -v nmcli &>/dev/null; then
  log 'Using NetworkManager (nmcli)'
  CON_NAME="`$(nmcli -t -f NAME,DEVICE con show | grep ":`$TARGET_IFACE`$" | cut -d: -f1 | head -1)"
  [ -z "`$CON_NAME" ] && CON_NAME="`$(nmcli -t -f NAME con show --active | head -1)"
  log "Connection profile: `$CON_NAME"
  sudo_cmd nmcli con mod "`$CON_NAME" \
    ipv4.method manual \
    ipv4.addresses '$TargetIp/$Cidr' \
    ipv4.gateway '$Gateway' \
    ipv4.dns '$DnsString' \
    connection.autoconnect yes
  log 'Static IP configured: $TargetIp/$Cidr'
elif command -v netplan &>/dev/null; then
  log 'Using netplan'
  sudo_cmd mkdir -p /etc/netplan
  sudo_cmd sh -c 'for f in /etc/netplan/*.yaml; do [ -f "`$f" ] && mv "`$f" /tmp/; done; true'
  printf 'network:\n  version: 2\n  renderer: networkd\n  ethernets:\n    %s:\n      optional: true\n      dhcp4: false\n      addresses:\n        - $TargetIp/$Cidr\n      routes:\n        - to: default\n          via: $Gateway\n      nameservers:\n        addresses: [$DnsString]\n' "`$TARGET_IFACE" | sudo_cmd tee /etc/netplan/99-db-sre-static.yaml >/dev/null
  sudo_cmd netplan generate
  log 'Static IP configured: $TargetIp/$Cidr'
else
  echo 'ERROR: no supported network manager (nmcli or netplan) found' >&2
  exit 1
fi

log "hostname: `$(hostname)"
log "machine-id: `$(cat /etc/machine-id)"
if [ '$DisableKdumpMultipathVal' = '1' ]; then
  log 'Disabling kdump and multipathd to prevent kernel panic with shared SCSI bus...'
  for svc in kdump multipathd; do
    systemctl is-enabled "`$svc" 2>/dev/null && sudo_cmd systemctl disable "`$svc" || true
  done
  # Remove crashkernel= from grub. Disabling the kdump service stops the
  # userspace daemon but does not remove the kernel's memory reservation for
  # the capture kernel. On Oracle Linux 9 UEK + shared LSILOGIC SCSI, loading
  # that capture kernel (kexec) at boot panics the kernel. Removing the boot
  # parameter prevents the reservation and the panic.
  if command -v grubby &>/dev/null; then
    sudo_cmd grubby --update-kernel=ALL --remove-args='crashkernel=auto'
    sudo_cmd grubby --update-kernel=ALL --remove-args='crashkernel'
    log 'Removed crashkernel from grub cmdline'
  fi
fi
log 'Scheduling reboot...'
# Use systemd-run so the reboot timer is owned by systemd, not the SSH session.
# Background reboot via shell & keeps the SSH session open (sshd waits for child
# processes in the session), causing the reboot to kill sshd before plink gets
# a clean exit status.
printf '%s\n' "`$SUDO_PASSWORD" | sudo -S -p '' -- systemd-run --no-block --on-active=2 /usr/bin/systemctl reboot
log 'Reboot scheduled via systemd (t+2s). Exiting cleanly.'
exit 0
"@

    $Encoded = ConvertTo-Base64Utf8 -Value $bashScript
    $RemoteCmd = "printf '%s' '$Encoded' | base64 -d | bash 2>&1"

    Write-Host "  Connecting via SSH to $CurrentIp ..."
    $result = Invoke-PlinkCommand -TargetHost $CurrentIp -User $GuestUser -Password $GuestPassword -RemoteCommand $RemoteCmd -TimeoutSec 120
    $result.Output | ForEach-Object { Write-Host "  $_" }

    if ($result.ExitCode -ne 0) {
      throw "Guest identity fix failed for $VmName (SSH $CurrentIp). ExitCode=$($result.ExitCode)"
    }

    if (-not $SkipReboot) {
      Write-Host "  $VmName is rebooting to $TargetIp ..."
      $ready = Wait-SshAvailable -TargetHost $TargetIp -User $GuestUser -Password $GuestPassword -TimeoutSec 180
      if ($ready) {
        Write-Host "  $VmName SSH ready on $TargetIp" -ForegroundColor Green
      } else {
        Write-Warning "  $VmName SSH not reachable on $TargetIp after 180s. Check VM manually."
      }
    }
  }
}

function Invoke-SshDeployHostKey {
  param(
    [Parameter(Mandatory = $true)]
    [object[]]$Inventory,
    [string]$GuestUser = $script:GuestUser,
    [string]$GuestPassword = $script:GuestPassword
  )

  if (-not $script:SshIdentityFile) {
    throw "ssh_identity_file not configured in sre_config.json credentials."
  }
  $SshPubKeyPath = "$($script:SshIdentityFile).pub"
  if (-not (Test-Path $SshPubKeyPath)) {
    throw "SSH public key not found: $SshPubKeyPath"
  }
  $PubKey = (Get-Content $SshPubKeyPath -Raw).Trim()
  $EscapedPassword = ConvertTo-BashSingleQuotedLiteral -Value $GuestPassword
  $PubKeyEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($PubKey))

  # Wait for SSH on all VMs in parallel before deploying keys (VMs may have just rebooted).
  $SshWaitSec = 600
  $SshDeadline = (Get-Date).AddSeconds($SshWaitSec)
  $SshReadyMap = @{}
  Write-Host "Waiting for SSH on $($Inventory.Count) VM(s) before key deployment (up to ${SshWaitSec}s) ..."
  do {
    foreach ($Vm in $Inventory) {
      if ($SshReadyMap[$Vm.Name]) { continue }
      $Ip = $Vm.ExpectedIp
      if (-not $Ip) { throw "No ExpectedIp for $($Vm.Name). Ensure fix-guest-identity ran first." }
      $r = Invoke-PlinkCommand -TargetHost $Ip -User $GuestUser -Password $GuestPassword -RemoteCommand "echo ssh-ok" -TimeoutSec 10
      if ($r.ExitCode -eq 0 -and ($r.Output -join '') -match 'ssh-ok') {
        $SshReadyMap[$Vm.Name] = $true
        Write-Host "  SSH ready: $($Vm.Name) at $Ip"
      }
    }
    $pendingCount = @($Inventory | Where-Object { -not $SshReadyMap[$_.Name] }).Count
    if ($pendingCount -eq 0 -or (Get-Date) -ge $SshDeadline) { break }
    Start-Sleep -Seconds 10
  } while ($true)
  foreach ($Vm in $Inventory) {
    if (-not $SshReadyMap[$Vm.Name]) {
      throw "SSH not reachable on $($Vm.ExpectedIp) after ${SshWaitSec}s for $($Vm.Name)."
    }
  }

  foreach ($Vm in $Inventory) {
    $TargetIp = $Vm.ExpectedIp
    if (-not $TargetIp) {
      throw "No ExpectedIp for $($Vm.Name). Ensure fix-guest-identity ran first."
    }

    Write-Host "Deploying host SSH key + NOPASSWD sudo to $($Vm.Name) ($TargetIp) via SSH ..."

    $bashScript = @"
set -euo pipefail
mkdir -p ~/.ssh && chmod 700 ~/.ssh
PUBKEY=`$(printf '%s' '$PubKeyEncoded' | base64 -d)
grep -qxF -- "`$PUBKEY" ~/.ssh/authorized_keys 2>/dev/null || echo "`$PUBKEY" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
printf '%s\n' '$EscapedPassword' | sudo -S bash -c "echo 'tuser ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/tuser-nopasswd && chmod 440 /etc/sudoers.d/tuser-nopasswd"
echo "deployed on `$(hostname)"
"@

    $Encoded = ConvertTo-Base64Utf8 -Value $bashScript
    $RemoteCmd = "printf '%s' '$Encoded' | base64 -d | bash 2>&1"

    $result = Invoke-PlinkCommand -TargetHost $TargetIp -User $GuestUser -Password $GuestPassword -RemoteCommand $RemoteCmd -TimeoutSec 60
    $result.Output | ForEach-Object { Write-Host "  $_" }

    if ($result.ExitCode -ne 0) {
      throw "SSH key deployment failed on $($Vm.Name) ($TargetIp). ExitCode=$($result.ExitCode)"
    }
    Write-Host "$($Vm.Name): OK" -ForegroundColor Green
  }
}

function Deploy-HostSshKeyToInventory {
  param(
    [Parameter(Mandatory = $true)]
    [object[]]$Inventory
  )

  if (-not $script:SshIdentityFile) {
    throw "ssh_identity_file not configured in sre_config.json credentials."
  }
  $SshPubKeyPath = "$($script:SshIdentityFile).pub"
  if (-not (Test-Path $SshPubKeyPath)) {
    throw "SSH public key not found: $SshPubKeyPath"
  }
  $PubKey = (Get-Content $SshPubKeyPath -Raw).Trim()
  $Credential = New-GuestCredential
  $EscapedPassword = ConvertTo-BashSingleQuotedLiteral -Value $Credential.Password

  foreach ($Vm in $Inventory) {
    if (-not (Test-Path $Vm.VmxPath)) {
      Write-Warning "VMX not found, skipping $($Vm.Name): $($Vm.VmxPath)"
      continue
    }

    Write-Host "Deploying SSH key and NOPASSWD sudo to $($Vm.Name) ..."

    $PubKeyEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($PubKey))
    $bashScript = @"
set -euo pipefail
mkdir -p ~/.ssh && chmod 700 ~/.ssh
PUBKEY=`$(printf '%s' '$PubKeyEncoded' | base64 -d)
grep -qF -- "`$PUBKEY" ~/.ssh/authorized_keys 2>/dev/null || echo "`$PUBKEY" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
echo '$EscapedPassword' | sudo -S bash -c "echo 'tuser ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/tuser-nopasswd && chmod 440 /etc/sudoers.d/tuser-nopasswd"
echo "deployed on `$(hostname)"
"@

    $Encoded = ConvertTo-Base64Utf8 -Value $bashScript
    $RunScript = "printf '%s' '$Encoded' | base64 -d | /bin/bash > /tmp/db-sre-deploy-key.log 2>&1"

    $RunResult = Invoke-VmrunWithOutput -Arguments @(
      "-gu", $Credential.User, "-gp", $Credential.Password,
      "runScriptInGuest", $Vm.VmxPath, "/bin/bash", $RunScript
    )

    $LocalLog = [System.IO.Path]::GetTempFileName()
    $CopyResult = Invoke-VmrunWithOutput -Arguments @(
      "-gu", $Credential.User, "-gp", $Credential.Password,
      "copyFileFromGuestToHost", $Vm.VmxPath, "/tmp/db-sre-deploy-key.log", $LocalLog
    )
    if ($CopyResult.ExitCode -eq 0 -and (Test-Path $LocalLog)) {
      Get-Content $LocalLog | ForEach-Object { Write-Host "  $_" }
      Remove-Item $LocalLog -ErrorAction SilentlyContinue
    }

    if ($RunResult.ExitCode -ne 0) {
      $RunResult.Output | ForEach-Object { Write-Warning $_ }
      throw "SSH key deployment failed on $($Vm.Name). ExitCode=$($RunResult.ExitCode)"
    }
    Write-Host "$($Vm.Name): OK" -ForegroundColor Green
  }
}

function Invoke-VmrunWithOutput {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$Arguments
  )

  $Output = & $Vmrun @Arguments 2>&1
  $ExitCode = $LASTEXITCODE

  return @{
    Output   = $Output
    ExitCode = $ExitCode
  }
}

function Invoke-VmrunWithProgress {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$Arguments,
    [int]$ProgressIntervalSec = 30
  )

  $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
  $StartInfo.FileName = $Vmrun
  $StartInfo.Arguments = ($Arguments | ForEach-Object {
    if ($_ -match '\s') { '"{0}"' -f ($_ -replace '"', '\"') } else { $_ }
  }) -join ' '
  $StartInfo.UseShellExecute = $false
  $StartInfo.RedirectStandardOutput = $true
  $StartInfo.RedirectStandardError = $true
  $StartInfo.CreateNoWindow = $true

  $Process = New-Object System.Diagnostics.Process
  $Process.StartInfo = $StartInfo
  $null = $Process.Start()

  $StartedAt = Get-Date
  $NextPrintAt = $StartedAt.AddSeconds($ProgressIntervalSec)

  while (-not $Process.WaitForExit(5000)) {
    if ((Get-Date) -ge $NextPrintAt) {
      $ElapsedSec = [int]((Get-Date) - $StartedAt).TotalSeconds
      Write-Host "  ... still running (${ElapsedSec}s elapsed)"
      $NextPrintAt = (Get-Date).AddSeconds($ProgressIntervalSec)
    }
  }

  $Output = @(
    $Process.StandardOutput.ReadToEnd()
    $Process.StandardError.ReadToEnd()
  ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

  return @{
    Output   = $Output
    ExitCode = $Process.ExitCode
  }
}

function Invoke-VmGuestIdentityFix {
  param(
    [Parameter(Mandatory = $true)]
    [object[]]$Inventory,
    [Parameter(Mandatory = $true)]
    [string]$GuestUser,
    [Parameter(Mandatory = $true)]
    [string]$GuestPassword,
    [switch]$SkipReboot
  )

  foreach ($Vm in $Inventory) {
    if (-not (Test-Path $Vm.VmxPath)) {
      Write-Warning "VMX not found for $($Vm.Name): $($Vm.VmxPath)"
      continue
    }

    if (-not $Vm.ExpectedIp) {
      throw "No IP mapping found for $($Vm.Name)."
    }

    $DnsYaml    = New-NetplanYamlDnsBlock
    $DnsString  = $DnsServers -join ','
    $EscapedGuestPassword = ConvertTo-BashSingleQuotedLiteral -Value $GuestPassword

    Write-Host "Applying guest identity and static IP fix for $($Vm.Name) -> $($Vm.ExpectedIp) ..."

    $bashScript = @"
set -euo pipefail
SUDO_PASSWORD='$EscapedGuestPassword'
NETPLAN_BACKUP_DIR=""

log() {
  printf '[db-sre-fix] %s\n' "`$*"
}

sudo_cmd() {
  printf '%s\n' "`$SUDO_PASSWORD" | sudo -S -p '' -- "`$@"
}

backup_netplan_files() {
  NETPLAN_BACKUP_DIR="/tmp/db-sre-netplan-backup-`$(date +%s)"
  sudo_cmd mkdir -p "`$NETPLAN_BACKUP_DIR"

  while IFS= read -r file_path; do
    [ -n "`$file_path" ] || continue
    sudo_cmd mv "`$file_path" "`$NETPLAN_BACKUP_DIR/"
  done < <(sudo find /etc/netplan -maxdepth 1 -type f -name '*.yaml' 2>/dev/null | sort)
}

restore_netplan_backup() {
  [ -n "`${NETPLAN_BACKUP_DIR:-}" ] || return 0
  sudo test -d "`$NETPLAN_BACKUP_DIR" || return 0

  log "Restoring previous netplan files from `$NETPLAN_BACKUP_DIR"
  sudo_cmd rm -f /etc/netplan/99-db-sre-static.yaml

  while IFS= read -r file_path; do
    [ -n "`$file_path" ] || continue
    sudo_cmd mv "`$file_path" /etc/netplan/
  done < <(sudo find "`$NETPLAN_BACKUP_DIR" -maxdepth 1 -type f -name '*.yaml' 2>/dev/null | sort)
}

on_error() {
  rc="`$?"
  log "Guest identity fix failed with exit code `$rc"
  restore_netplan_backup || true
  exit "`$rc"
}

trap on_error ERR

log "Detecting target interface"
TARGET_IFACE="$NetInterface"
if ! ip link show "`$TARGET_IFACE" >/dev/null 2>&1; then
  TARGET_IFACE="`$(ip route show default 2>/dev/null | awk '/default/ { print `$5; exit }')"
fi
if [ -z "`$TARGET_IFACE" ] || ! ip link show "`$TARGET_IFACE" >/dev/null 2>&1; then
  echo "Unable to detect a usable network interface" >&2
  exit 1
fi
log "Using interface: `$TARGET_IFACE"

log "Setting hostname to $($Vm.Name)"
sudo_cmd hostnamectl set-hostname $($Vm.Name)

log "Refreshing machine-id"
sudo_cmd truncate -s 0 /etc/machine-id
sudo_cmd rm -f /var/lib/dbus/machine-id
# systemd-machine-id-setup exits 2 on Ubuntu 24.04+ when the ID is already committed;
# fall back to generating directly from urandom.
sudo_cmd systemd-machine-id-setup 2>/dev/null || \
  sudo_cmd sh -c "head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' > /etc/machine-id && chmod 444 /etc/machine-id"

log "Preparing cloud-init override"
sudo_cmd mkdir -p /etc/cloud/cloud.cfg.d
cat <<'EOF' > /tmp/99-disable-network-config.cfg
network: {config: disabled}
EOF
sudo_cmd install -m 600 /tmp/99-disable-network-config.cfg /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg

if command -v nmcli &>/dev/null; then
  log "Using NetworkManager (nmcli)"
  CON_NAME="`$(nmcli -t -f NAME,DEVICE con show | grep ":`$TARGET_IFACE`$" | cut -d: -f1 | head -1)"
  [ -z "`$CON_NAME" ] && CON_NAME="`$(nmcli -t -f NAME con show --active | head -1)"
  log "Connection profile: `$CON_NAME"
  sudo_cmd nmcli con mod "`$CON_NAME" \
    ipv4.method manual \
    ipv4.addresses '$($Vm.ExpectedIp)/$Cidr' \
    ipv4.gateway '$Gateway' \
    ipv4.dns '$DnsString' \
    connection.autoconnect yes
  log "Static IP configured via nmcli: $($Vm.ExpectedIp)/$Cidr"
elif command -v netplan &>/dev/null; then
  log "Preparing netplan directories and backup"
  sudo_cmd mkdir -p /etc/netplan
  backup_netplan_files

  log "Writing temporary static netplan file"
  cat <<EOF > /tmp/99-db-sre-static.yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    `$TARGET_IFACE:
      optional: true
      dhcp4: false
      addresses:
        - $($Vm.ExpectedIp)/$Cidr
      routes:
        - to: default
          via: $Gateway
      nameservers:
        addresses:
$DnsYaml
EOF

  sudo_cmd install -m 600 /tmp/99-db-sre-static.yaml /etc/netplan/99-db-sre-static.yaml

  log "Generated /etc/netplan/99-db-sre-static.yaml:"
  sudo_cmd cat /etc/netplan/99-db-sre-static.yaml

  log "Running netplan generate"
  sudo_cmd netplan generate

  log "Running netplan apply"
  sudo_cmd netplan apply
  log "Static IP configured via netplan: $($Vm.ExpectedIp)/$Cidr"
else
  echo "ERROR: no supported network manager (nmcli or netplan) found" >&2
  exit 1
fi

log "Bringing interface up"
sudo_cmd ip link set "`$TARGET_IFACE" up

log "Guest identity fix completed"
hostnamectl
cat /etc/machine-id
ip a show "`$TARGET_IFACE" || true
ip route
sudo_cmd sync
"@

    $EncodedBashScript = ConvertTo-Base64Utf8 -Value $bashScript
    $GuestExecScript = "printf '%s' '$EncodedBashScript' | base64 -d | /bin/bash > /tmp/db-sre-fix.log 2>&1"

    $RunResult = Invoke-VmrunWithOutput -Arguments @(
      "-gu", $GuestUser,
      "-gp", $GuestPassword,
      "runScriptInGuest",
      $Vm.VmxPath,
      "/bin/bash",
      $GuestExecScript
    )

    $Output = $RunResult.Output
    $ExitCode = $RunResult.ExitCode

    $LocalLogFile = [System.IO.Path]::GetTempFileName()
    $CopyResult = Invoke-VmrunWithOutput -Arguments @(
      "-gu", $GuestUser, "-gp", $GuestPassword,
      "copyFileFromGuestToHost",
      $Vm.VmxPath, "/tmp/db-sre-fix.log", $LocalLogFile
    )
    if ($CopyResult.ExitCode -eq 0 -and (Test-Path $LocalLogFile)) {
      Write-Host "--- Guest fix log: $($Vm.Name) ---"
      Get-Content $LocalLogFile | ForEach-Object { Write-Host $_ }
      Remove-Item $LocalLogFile -ErrorAction SilentlyContinue
    }

    if ($ExitCode -ne 0) {
      Write-Host "vmrun output:" -ForegroundColor Yellow
      $Output | ForEach-Object { Write-Host $_ }
      throw "Guest identity and IP fix failed for $($Vm.Name). ExitCode=$ExitCode"
    }

    if (-not $SkipReboot) {
      # Use vmrun reset soft: triggers ACPI graceful restart so the guest OS flushes
      # filesystem caches before cycling power.  Hard reset can lose recently written
      # files whose dirty pages haven't been flushed to disk yet.
      Write-Host "Resetting $($Vm.Name) via vmrun reset soft ..."
      $RebootResult = Invoke-VmrunWithOutput -Arguments @("reset", $Vm.VmxPath, "soft")
      if ($RebootResult.ExitCode -ne 0) {
        Write-Host "vmrun output:" -ForegroundColor Yellow
        $RebootResult.Output | ForEach-Object { Write-Host $_ }
        throw "Reset failed for $($Vm.Name). ExitCode=$($RebootResult.ExitCode)"
      }
    }
  }
}

function New-GuestCredential {
  param(
    [string]$GuestUser,
    [string]$GuestPassword
  )

  if (-not $GuestUser) {
    $GuestUser = $script:GuestUser
  }

  if (-not $GuestPassword) {
    $GuestPassword = $script:GuestPassword
  }

  if (-not $GuestUser) {
    throw "Guest user is required. Set credentials.guest_user in config.json or pass -GuestUser."
  }

  if (-not $GuestPassword) {
    throw "Guest password is required. Set credentials.guest_password in config.json, or pass -GuestPassword explicitly."
  }

  return @{
    User     = $GuestUser
    Password = $GuestPassword
  }
}

function New-VmSnapshotForInventory {
  param(
    [Parameter(Mandatory = $true)]
    [object[]]$Inventory,
    [Parameter(Mandatory = $true)]
    [string]$SnapshotName
  )

  $SnapshotFailures = @()

  foreach ($Vm in $Inventory) {
    if (-not (Test-Path $Vm.VmxPath)) {
      Write-Warning "VMX not found for $($Vm.Name): $($Vm.VmxPath)"
      continue
    }

    Write-Host "Creating snapshot '$SnapshotName' for $($Vm.Name) ..."
    $StartedAt = Get-Date
    $Output = & $Vmrun snapshot $Vm.VmxPath $SnapshotName 2>&1
    $ExitCode = $LASTEXITCODE
    $Elapsed = (Get-Date) - $StartedAt

    if ($ExitCode -ne 0) {
      Write-Warning "Failed to create snapshot '$SnapshotName' for $($Vm.Name). ExitCode=$ExitCode"
      $Output | ForEach-Object { Write-Warning $_ }
      $SnapshotFailures += $Vm.Name
      continue
    }

    Write-Host ("Snapshot complete for {0} in {1:N1}s" -f $Vm.Name, $Elapsed.TotalSeconds)
  }

  return $SnapshotFailures
}
