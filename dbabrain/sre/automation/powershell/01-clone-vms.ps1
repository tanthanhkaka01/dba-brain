param(
  [string]$DbSrePayloadJsonBase64,

  [string[]]$VmName,
  [ValidateSet("shared", "mysql", "postgresql", "sqlserver", "oracle_rac", "oracle_dg", "all")]
  [string]$Group = "all",
  [switch]$Force,

  [string]$Template
)

. "$PSScriptRoot\vmware-common.ps1"

Assert-VmrunAvailable

if ($Template) {
  $script:TemplateName = $Template
  $script:TemplateDir = Join-Path $script:VmRoot "templates\$script:TemplateName"
  $script:TemplateVmx = Join-Path $script:TemplateDir "$script:TemplateName.vmx"
}

Assert-TemplateVmxAvailable
New-Item -ItemType Directory -Force -Path $SharedRoot | Out-Null
New-Item -ItemType Directory -Force -Path $MysqlRoot | Out-Null
New-Item -ItemType Directory -Force -Path $PostgresqlRoot | Out-Null
New-Item -ItemType Directory -Force -Path $SqlServerRoot | Out-Null
New-Item -ItemType Directory -Force -Path $OracleRacRoot | Out-Null
New-Item -ItemType Directory -Force -Path $OracleDgRoot | Out-Null

if ($VmName -and $VmName.Count -gt 0) {
  $Inventory = Get-VmInventoryByName -VmName $VmName
} else {
  $Inventory = Get-VmInventory -Group $Group
}
Clone-VmInventory -Inventory $Inventory -Force:$Force
