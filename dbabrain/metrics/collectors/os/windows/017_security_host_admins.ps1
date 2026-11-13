# SECURITY_HOST_ADMINS (Windows): who can be an administrator on this host.
#
# Why: a database's own files open on another machine, so whoever gets onto the host as an
# administrator can take them. This reads who that is, with the login dbabrain already has,
# changing nothing:
#
#   group:Administrators:<account>          members of the local Administrators group
#   group:Remote Desktop Users:<account>    members of the local Remote Desktop Users group
#   check:domain_admins_in_administrators   yes / no - is the domain's Domain Admins group a member
#   rdp:enabled                             yes / no - does the host accept Remote Desktop at all
#
# Every item is "<section>:<name>". A section that cannot be read is ONE row with metric_unit
# "unreadable", the section as its item and the reason as its message - never a missing row: the
# change alert skips a section that was unreadable on either read.
#
# The groups are found by their well-known SIDs (S-1-5-32-544, S-1-5-32-555), so a localized
# Windows whose group is not called "Administrators" is read the same; the item keeps the English
# name so one estate reads one way. Members are read through ADSI's WinNT provider rather than
# Get-LocalGroupMember: that cmdlet fails outright on a group holding an orphaned SID (a deleted
# domain account), which is exactly the host an access review most needs to read.
#
# Status is always OK: an inventory. What alerts is a CHANGE between two reads (the metric's
# change_alert) - an account added to Administrators, Remote Desktop turned on.
$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = 'OK'; message = [string]$Message }
}
$rows = New-Object System.Collections.ArrayList
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
[void]$rows.Add((New-MetricRow 'read_as' $me 'summary' ("account=" + $me)))

function Get-Property($Object, $Name) {
    $Object.GetType().InvokeMember($Name, 'GetProperty', $null, $Object, $null)
}

$domainAdmins = $null
$sidUnknown = $false
foreach ($group in @(@{ Sid = 'S-1-5-32-544'; Label = 'Administrators' }, @{ Sid = 'S-1-5-32-555'; Label = 'Remote Desktop Users' })) {
    $section = 'group:' + $group.Label
    try {
        $local = (New-Object System.Security.Principal.SecurityIdentifier($group.Sid)).Translate([System.Security.Principal.NTAccount]).Value.Split('\')[-1]
        $adsi = [ADSI]("WinNT://" + $env:COMPUTERNAME + "/" + $local + ",group")
        $found = $false
        foreach ($member in @($adsi.Invoke('Members'))) {
            $path = [string](Get-Property $member 'ADsPath')
            $class = [string](Get-Property $member 'Class')
            # WinNT://DOMAIN/name, or WinNT://DOMAIN/MACHINE/name for a local account.
            $parts = $path.Substring('WinNT://'.Length).Split('/')
            if ($parts.Length -ge 3 -and $parts[1] -ieq $env:COMPUTERNAME) { $account = $parts[1] + '\' + $parts[2]; $source = 'local' }
            elseif ($parts.Length -ge 2) { $account = $parts[0] + '\' + $parts[1]; $source = 'domain' }
            else { $account = $parts[0]; $source = 'unresolved' }
            # The member's SID: from the object itself, else by asking Windows for the account's.
            # Typed as byte[] before the constructor sees it - passed as it comes back from COM,
            # PowerShell unrolls the array into separate arguments and the constructor fails.
            $sid = ''
            try {
                [byte[]]$sidBytes = Get-Property $member 'objectSid'
                $sid = (New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $sidBytes, 0).Value
            } catch { }
            if (-not $sid) {
                try { $sid = (New-Object System.Security.Principal.NTAccount($account)).Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { }
            }
            if (-not $sid -and $account -match '^S-1-') { $sid = $account }
            if (-not $sid -and $group.Sid -eq 'S-1-5-32-544') { $sidUnknown = $true }
            [void]$rows.Add((New-MetricRow ($section + ':' + $account) $class 'account' ("group=" + $group.Label + ", local_name=" + $local + ", account=" + $account + ", class=" + $class + ", source=" + $source + ", sid=" + $sid)))
            # Domain Admins is the domain's RID 512, whatever the domain calls it.
            if ($group.Sid -eq 'S-1-5-32-544' -and $sid -match '^S-1-5-21-.*-512$') { $found = $true }
        }
        if ($group.Sid -eq 'S-1-5-32-544') { $domainAdmins = $found }
    }
    catch {
        [void]$rows.Add((New-MetricRow $section 'unreadable' 'unreadable' ("as " + $me + ": " + $_.Exception.Message)))
    }
}
if ($null -eq $domainAdmins) {
    [void]$rows.Add((New-MetricRow 'check' 'unreadable' 'unreadable' 'local Administrators could not be read, so whether Domain Admins is in it is not known'))
}
elseif (-not $domainAdmins -and $sidUnknown) {
    # "no" would be a guess: Domain Admins is known by its SID, and one member's was not read.
    [void]$rows.Add((New-MetricRow 'check' 'unreadable' 'unreadable' 'the SID of a member of local Administrators could not be read, so whether one of them is Domain Admins (RID 512) is not known'))
}
else {
    $text = 'no'
    if ($domainAdmins) { $text = 'yes' }
    [void]$rows.Add((New-MetricRow 'check:domain_admins_in_administrators' $text 'setting' 'the domain group with RID 512 (Domain Admins) among the members of local Administrators'))
}

try {
    $deny = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections).fDenyTSConnections
    $text = 'yes'
    if ([int]$deny -ne 0) { $text = 'no' }
    [void]$rows.Add((New-MetricRow 'rdp:enabled' $text 'setting' ("fDenyTSConnections=" + $deny)))
}
catch {
    [void]$rows.Add((New-MetricRow 'rdp' 'unreadable' 'unreadable' ("as " + $me + ": " + $_.Exception.Message)))
}

ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
