# SQL Server backup on WINDOWS - one script covering FULL, DIFF and LOG, chosen by $BACKUP_LEVEL.
#
# The Windows half of mssql_backup_database.sh. Same levels, same directory layout, same file
# names, same encryption material, same retention rule, same RESULT=ok receipt - because the
# restore side reads a set without being told which script wrote it, and two layouts would mean
# two restore paths, of which one would eventually be the untested one.
#
# Runs ON the Windows host, reached over WinRM (db_ops.common.hostcmd with access=winrm). There is
# no container here: the instance is local to the machine the script runs on.
#
# THIS SCRIPT OWNS THE BACKUP CHAIN. It writes ordinary FULL/DIFF/LOG backups, not COPY_ONLY, so a
# FULL taken here resets the differential base for the whole instance. On an instance that still
# has its own Agent backup jobs that splits the chain across two locations, and a restore then
# needs both. Disable the native jobs before enabling this against an instance - that is an
# operational decision, not a config toggle.
#
# Layout, one file per backup (identical to the Linux script):
#   $BACKUP_DIR\<DB>\FULL\<DB>_FULL_<UTC timestamp>.bak
#   $BACKUP_DIR\<DB>\DIFF\<DB>_DIFF_<UTC timestamp>.bak
#   $BACKUP_DIR\<DB>\LOG\<DB>_LOG_<UTC timestamp>.trn
#   $BACKUP_DIR\_cert\<CERT_NAME>.cer + .pvk        the encryption certificate, exported once
#
# ENCRYPTION. With $BACKUP_ENCRYPTION_PASSWORD set, every backup is written
# `WITH ENCRYPTION (ALGORITHM = AES_256, SERVER CERTIFICATE = ...)`. SQL Server needs a certificate
# for that, so the script creates one (and the master key it hangs off) on first run and exports it
# next to the backups - a backup encrypted with a certificate that exists only inside the source
# instance cannot be restored anywhere else.
#
# THE PATHS ARE THE SQL SERVER SERVICE ACCOUNT'S, NOT THIS SCRIPT'S. BACKUP TO DISK is executed by
# the engine, so $BACKUP_DIR must be writable by the service account - a path this script can see
# and the service cannot fails with "Operating system error 5(Access is denied)" naming a
# directory that plainly exists. That is why the directories are created through the engine's own
# xp_create_subdir rather than with New-Item.
#
# Env: BACKUP_DIR (required), BACKUP_LEVEL (required: full|diff|log),
#      MSSQL_SERVER (default '.'; a named instance or FCI network name goes here),
#      MSSQL_USER + MSSQL_PASSWORD (optional; omitted = Windows auth as the WinRM account),
#      MSSQL_DATABASES (optional comma list; default = every online user database),
#      BACKUP_ENCRYPTION_PASSWORD (optional, from env_secrets; absent = unencrypted),
#      BACKUP_CERT_NAME (default db_ops_backup_cert), RETENTION_DAYS (default 14).
# Exit: 0 on success, non-zero on failure. Prints RESULT=ok only on a completed run.

$ErrorActionPreference = 'Stop'

# Written straight to the error stream, never through Write-Error. With
# $ErrorActionPreference = 'Stop' — which everything else here relies on — Write-Error raises a
# terminating error, so `Write-Error ...; $failed = 1; continue` would abandon the loop instead of
# recording one failed database and going on to the next. That is the difference between "one
# database could not be backed up" and "the other eleven were never attempted".
function Write-Stderr($text) { $Host.UI.WriteErrorLine($text) }

function Die($reason) {
    Write-Stderr "RESULT=error reason=$reason"
    exit 1
}

$backupDir  = $env:BACKUP_DIR
$level      = "$($env:BACKUP_LEVEL)".ToLower()
$server     = if ($env:MSSQL_SERVER) { $env:MSSQL_SERVER } else { '.' }
$mssqlUser  = $env:MSSQL_USER
$mssqlPass  = $env:MSSQL_PASSWORD
$databasesCsv = $env:MSSQL_DATABASES
$encPassword  = $env:BACKUP_ENCRYPTION_PASSWORD
$certName   = if ($env:BACKUP_CERT_NAME) { $env:BACKUP_CERT_NAME } else { 'db_ops_backup_cert' }
$retentionDays = if ($env:RETENTION_DAYS) { $env:RETENTION_DAYS } else { '14' }

if (-not $backupDir) { Die 'BACKUP_DIR is not set.' }
if ($level -notin @('full', 'diff', 'log')) { Die "BACKUP_LEVEL must be full, diff or log: '$($env:BACKUP_LEVEL)'." }
if ($retentionDays -notmatch '^\d+$') { Die "RETENTION_DAYS must be a whole number of days: '$retentionDays'." }
$retentionDays = [int]$retentionDays
# A SQL login needs both halves. One without the other silently falls back to Windows auth and
# backs up as whoever WinRM connected as - which may have rights the operator did not intend.
if ($mssqlUser -and -not $mssqlPass) { Die 'MSSQL_USER is set without MSSQL_PASSWORD.' }
if ($mssqlPass -and -not $mssqlUser) { Die 'MSSQL_PASSWORD is set without MSSQL_USER.' }

$backupDir = $backupDir.TrimEnd('\', '/')

# --------------------------------------------------------------------------- #
# Talking to the instance.
# --------------------------------------------------------------------------- #
# sqlcmd.exe rather than Invoke-Sqlcmd: the cmdlet lives in the SqlServer/SQLPS module, which is
# not installed on every SQL Server host, and discovering that on a backup night is not the moment.
# sqlcmd ships with the engine.
$sqlcmd = (Get-Command sqlcmd.exe -ErrorAction SilentlyContinue)
if (-not $sqlcmd) {
    foreach ($candidate in @(
        "$env:ProgramFiles\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\sqlcmd.exe",
        "$env:ProgramFiles\Microsoft SQL Server\Client SDK\ODBC\130\Tools\Binn\sqlcmd.exe",
        "${env:ProgramFiles(x86)}\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\sqlcmd.exe")) {
        if (Test-Path -LiteralPath $candidate) { $sqlcmd = Get-Item -LiteralPath $candidate; break }
    }
}
if (-not $sqlcmd) { Die 'sqlcmd.exe not found on this host.' }
# Not `??`: null-coalescing is PowerShell 7+ and these hosts run Windows PowerShell 5.1, where it
# is a parser error that kills the whole script before a single line of it runs.
$sqlcmdPath = if ($sqlcmd.Source) { $sqlcmd.Source } else { $sqlcmd.FullName }

# `-C` (trust the server certificate) exists in sqlcmd 18+ and is REQUIRED there, because 18
# defaults to Encrypt=yes and a local instance usually has a self-signed certificate. Older
# sqlcmd rejects the flag outright. Probed once rather than assumed: guessing wrong fails every
# statement with a message about encryption that reads like a server problem.
$script:trustFlag = $null
function Get-TrustFlag {
    if ($null -ne $script:trustFlag) { return $script:trustFlag }
    foreach ($attempt in @(@('-C'), @())) {
        $sqlArgs = @('-S', $server, '-b', '-h', '-1', '-W', '-Q', 'SET NOCOUNT ON; SELECT 1') + $attempt
        if ($mssqlUser) { $sqlArgs += @('-U', $mssqlUser, '-P', $mssqlPass) } else { $sqlArgs += '-E' }
        & $sqlcmdPath @sqlArgs > $null 2>&1
        if ($LASTEXITCODE -eq 0) { $script:trustFlag = $attempt; return $script:trustFlag }
    }
    Die "cannot log in to '$server' (tried with and without -C)."
}

function Invoke-Sql($query) {
    $sqlArgs = @('-S', $server, '-b', '-Q', $query) + (Get-TrustFlag)
    if ($mssqlUser) { $sqlArgs += @('-U', $mssqlUser, '-P', $mssqlPass) } else { $sqlArgs += '-E' }
    $output = & $sqlcmdPath @sqlArgs 2>&1
    return @{ ok = ($LASTEXITCODE -eq 0); output = ($output -join "`n") }
}

function Get-SqlRows($query) {
    $sqlArgs = @('-S', $server, '-b', '-h', '-1', '-W', '-Q', "SET NOCOUNT ON; $query") + (Get-TrustFlag)
    if ($mssqlUser) { $sqlArgs += @('-U', $mssqlUser, '-P', $mssqlPass) } else { $sqlArgs += '-E' }
    $output = & $sqlcmdPath @sqlArgs 2>&1
    if ($LASTEXITCODE -ne 0) { Die "query failed: $($output -join ' ')" }
    return @($output | ForEach-Object { "$_".Trim() } |
             Where-Object { $_ -and $_ -notmatch '^\(\d+ rows affected\)$' })
}

function Get-SqlEscaped($value) { return "$value".Replace("'", "''") }

# --------------------------------------------------------------------------- #
# Encryption material: a master key + certificate, created once and exported so the
# backups can be restored on another instance.
# --------------------------------------------------------------------------- #
$encryptClause = ''
if ($encPassword) {
    $escPw   = Get-SqlEscaped $encPassword
    $escCert = Get-SqlEscaped $certName
    $certDir = "$backupDir\_cert"
    $result = Invoke-Sql "EXEC master.dbo.xp_create_subdir '$(Get-SqlEscaped $certDir)';"
    if (-not $result.ok) { Die "cannot create $certDir (the SQL Server service account must be able to write there): $($result.output)" }

    $result = Invoke-Sql @"
IF NOT EXISTS (SELECT 1 FROM sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##')
    CREATE MASTER KEY ENCRYPTION BY PASSWORD = '$escPw';
IF NOT EXISTS (SELECT 1 FROM sys.certificates WHERE name = '$escCert')
    CREATE CERTIFICATE [$certName] WITH SUBJECT = 'db_ops backup encryption';
"@
    if (-not $result.ok) { Die "could not create the backup master key/certificate: $($result.output)" }

    # Export once. Without the .cer/.pvk pair beside the backups, an encrypted backup is
    # restorable only on the instance that wrote it - which defeats the point of taking it.
    # Asked of the engine, not of this script: the file is written by the service account and may
    # sit on a share this session cannot read.
    # Asked through xp_fileexist rather than Test-Path: the file was written by the SQL Server
    # service account and may sit on a share this WinRM session cannot read, in which case
    # Test-Path says "no" and the certificate is exported again over the top of itself.
    $certExists = (Get-SqlRows "DECLARE @e int; EXEC master.dbo.xp_fileexist '$(Get-SqlEscaped "$certDir\$certName.cer")', @e OUTPUT; SELECT @e;")
    if (@($certExists)[0] -ne '1') {
        $result = Invoke-Sql @"
BACKUP CERTIFICATE [$certName]
    TO FILE = '$(Get-SqlEscaped "$certDir\$certName.cer")'
    WITH PRIVATE KEY (
        FILE = '$(Get-SqlEscaped "$certDir\$certName.pvk")',
        ENCRYPTION BY PASSWORD = '$escPw'
    );
"@
        if (-not $result.ok) { Die "could not export the backup certificate to ${certDir}: $($result.output)" }
        "exported backup certificate: $certDir\$certName.cer"
    }
    $encryptClause = ", ENCRYPTION (ALGORITHM = AES_256, SERVER CERTIFICATE = [$certName])"
}

# --------------------------------------------------------------------------- #
# Which databases.
# --------------------------------------------------------------------------- #
if ($databasesCsv) {
    $databases = @($databasesCsv -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
} else {
    # Online user databases only. tempdb is never backed up; model/msdb/master are excluded
    # because restoring them onto a different instance is a different operation entirely.
    # A LOG backup additionally needs FULL recovery - a SIMPLE database has no log chain and
    # BACKUP LOG on it fails, so it is filtered out rather than allowed to fail the job.
    $recoveryFilter = if ($level -eq 'log') { "AND recovery_model_desc <> 'SIMPLE'" } else { '' }
    $databases = Get-SqlRows @"
SELECT name FROM sys.databases
 WHERE database_id > 4 AND state_desc = 'ONLINE' AND is_read_only = 0
   $recoveryFilter
 ORDER BY name;
"@
}
if (-not $databases -or $databases.Count -eq 0) {
    "no database to back up at level=$level"
    'RESULT=ok'
    exit 0
}

# A DIFF with no FULL behind it cannot be restored; SQL Server would silently promote it to a full
# ("base backup not found" only appears at restore time on some paths). Refuse instead.
if ($level -eq 'diff') {
    foreach ($db in $databases) {
        $count = @(Get-SqlRows "SELECT COUNT(*) FROM msdb.dbo.backupset WHERE database_name = '$(Get-SqlEscaped $db)' AND type = 'D';")[0]
        if ($count -notmatch '^\d+$') { Die "cannot read backup history for $db." }
        if ([int]$count -eq 0) { Die "no FULL backup exists for $db; a DIFF would have nothing to restore onto. Run the full job first." }
    }
}

# --------------------------------------------------------------------------- #
# Back up.
# --------------------------------------------------------------------------- #
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd_HHmmss')
$failed = 0
foreach ($db in $databases) {
    switch ($level) {
        'full' { $sub = 'FULL'; $ext = 'bak'; $clause = '' }
        'diff' { $sub = 'DIFF'; $ext = 'bak'; $clause = ', DIFFERENTIAL' }
        'log'  { $sub = 'LOG';  $ext = 'trn'; $clause = '' }
    }
    $targetDir  = "$backupDir\$db\$sub"
    $targetFile = "$targetDir\${db}_${sub}_$stamp.$ext"

    $result = Invoke-Sql "EXEC master.dbo.xp_create_subdir '$(Get-SqlEscaped $targetDir)';"
    if (-not $result.ok) {
        Write-Stderr "ERROR mkdir failed: $targetDir - $($result.output)"
        $failed = 1
        continue
    }

    $escFile = Get-SqlEscaped $targetFile
    if ($level -eq 'log') {
        $statement = "BACKUP LOG [$db] TO DISK = '$escFile' WITH INIT, CHECKSUM, COMPRESSION$encryptClause;"
    } else {
        $statement = "BACKUP DATABASE [$db] TO DISK = '$escFile' WITH INIT, CHECKSUM, COMPRESSION$clause$encryptClause;"
    }

    "-- $db $level -> $targetFile"
    $result = Invoke-Sql $statement
    if ($result.ok) {
        # CHECKSUM on the way in is only half of it: VERIFYONLY re-reads what landed on disk,
        # which is the difference between "the command returned" and "the file is restorable".
        $verify = Invoke-Sql "RESTORE VERIFYONLY FROM DISK = '$escFile';"
        if (-not $verify.ok) {
            Write-Stderr "ERROR verify failed: $targetFile - $($verify.output)"
            $failed = 1
        }
    } else {
        Write-Stderr "ERROR backup failed: $db ($level) - $($result.output)"
        $failed = 1
    }
}

# --------------------------------------------------------------------------- #
# Retention: age-based, but never past the newest FULL.
# --------------------------------------------------------------------------- #
# Deleting by age alone can remove the FULL that every retained DIFF/LOG restores onto, leaving a
# backup set that looks present and cannot be used. The rule is the Linux script's: keep everything
# at or newer than the newest FULL, whatever its age, and apply the age cut only below that.
$cutoff = (Get-Date).AddDays(-1 * $retentionDays)
foreach ($db in $databases) {
    $dbDir = "$backupDir\$db"
    if (-not (Test-Path -LiteralPath $dbDir)) { continue }
    $newestFull = Get-ChildItem -LiteralPath "$dbDir\FULL" -Filter '*.bak' -File -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newestFull) { continue }   # nothing to anchor retention to; keep everything
    Get-ChildItem -LiteralPath $dbDir -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @('.bak', '.trn') } |
        Where-Object { $_.LastWriteTime -lt $cutoff -and $_.LastWriteTime -lt $newestFull.LastWriteTime } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
}

if ($failed -ne 0) { Die "one or more $level backups failed." }
$encrypted = if ($encPassword) { 'yes' } else { 'no' }
"RESULT=ok level=$level databases=$($databases -join ',') encrypted=$encrypted"
