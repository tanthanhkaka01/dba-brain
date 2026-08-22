# CLUSTER_FCI_HEALTH — Windows Failover Cluster (WSFC) health, as seen from this node.
#
# The estate has an FCI (192.0.2.115 / .113, listener SALESCLUSTER) that nothing monitored as
# one. AVAILABILITY_DATABASE_HEALTH queries Always On AG DMVs, so on an FCI it truthfully answers
# "NOT_CONFIGURED" and that was being read as "HA is fine". An FCI's health lives in the cluster,
# not in the SQL instance: nodes, the quorum that lets them form a cluster at all, which node owns
# the SQL role right now, and whether anything failed over recently.
#
# **Role-aware, or it reports a healthy cluster as broken.** In an FCI exactly one node runs SQL
# Server at a time; on the passive node the service is stopped and the resources are offline, and
# that is correct. A naive "is MSSQLSERVER running" check calls the passive node down on every
# single poll. So this script first works out whether THIS node owns the SQL role, then judges
# services against that.
#
# A machine that is not a cluster node is not a fault: it reports NOT_CLUSTERED and OK, the same
# way the AG metric reports NOT_CONFIGURED.

$ErrorActionPreference = 'Stop'
function New-MetricRow($Item, $Value, $Unit, $Status, $Message) {
    [ordered]@{ metric_item = [string]$Item; metric_value = [string]$Value; metric_unit = [string]$Unit; status = [string]$Status; message = [string]$Message }
}

try {
    $rows = @()

    if (-not (Get-Module -ListAvailable -Name FailoverClusters)) {
        # No cluster tooling installed. Saying so plainly beats an error: most targets are not
        # cluster nodes and must not alert about it.
        ConvertTo-Json -InputObject @((New-MetricRow 'cluster' 'NOT_CLUSTERED' 'state' 'OK' 'The FailoverClusters module is not installed: this host is not a WSFC node.')) -Depth 4 -Compress
        exit 0
    }
    Import-Module FailoverClusters -ErrorAction Stop

    $cluster = $null
    try { $cluster = Get-Cluster -ErrorAction Stop } catch { $cluster = $null }
    if (-not $cluster) {
        ConvertTo-Json -InputObject @((New-MetricRow 'cluster' 'NOT_CLUSTERED' 'state' 'OK' 'The FailoverClusters module is present but this host is not joined to a cluster.')) -Depth 4 -Compress
        exit 0
    }

    $thisNode = $env:COMPUTERNAME

    # A cluster has ONE set of groups, resources and quorum, and every node can see all of them.
    # Reporting them from each node duplicates every finding by the number of nodes: the first
    # version produced "Available Storage Offline" and "SQL Server PartialOnline" from both
    # A1ASQL01 and A1ASQL02, four times an hour each, for two conditions.
    #
    # So cluster-wide facts are reported by ONE node - the owner of the core "Cluster Group",
    # which is a single, stable, self-selecting answer that needs no configuration. Every node
    # still reports what only it can know: its own node state and its own services. If that node
    # is unreachable the cluster-wide rows go missing rather than being wrong, and the surviving
    # node's own row still says the cluster lost a member.
    $coreOwner = ''
    try { $coreOwner = [string](Get-ClusterGroup -Name 'Cluster Group' -ErrorAction Stop).OwnerNode.Name } catch { $coreOwner = '' }
    $reportsClusterWide = (-not $coreOwner) -or ($coreOwner -eq $thisNode)

    # ------------------------------------------------------------------ quorum
    # Quorum is the cluster's ability to stay alive through a node loss. A cluster can be fully
    # "up" and one failure away from stopping entirely, which no node/resource row would show.
    try {
        if (-not $reportsClusterWide) { throw 'reported by the core-group owner' }
        $q = Get-ClusterQuorum -ErrorAction Stop
        $witness = if ($q.QuorumResource) { $q.QuorumResource.Name } else { '<none>' }
        $witnessState = if ($q.QuorumResource) { [string]$q.QuorumResource.State } else { 'None' }
        $qStatus = if (-not $q.QuorumResource) { 'WARNING' } elseif ($witnessState -ne 'Online') { 'CRITICAL' } else { 'OK' }
        $rows += New-MetricRow 'quorum' $q.QuorumType 'state' $qStatus "cluster=$($cluster.Name), quorum_type=$($q.QuorumType), witness=$witness, witness_state=$witnessState$(if (-not $q.QuorumResource) { ' - no witness configured: an even-node cluster cannot survive losing one node' })"
    } catch {
        if ($reportsClusterWide) {
            $rows += New-MetricRow 'quorum' 'UNKNOWN' 'state' 'WARNING' "Could not read quorum configuration: $($_.Exception.Message)"
        }
    }

    # ------------------------------------------------------------------ nodes
    $nodes = @(Get-ClusterNode -ErrorAction SilentlyContinue)
    $upNodes = @($nodes | Where-Object { $_.State -eq 'Up' })
    foreach ($n in $nodes) {
        # Only this node's own row. Every node can see every other node, and reporting all of them
        # from all of them is the same duplication as the cluster-wide rows above. A node that is
        # down cannot report itself - which is why its absence is what the fleet notices, and why
        # nodes_up=x/y rides along on the row that IS reported.
        if ($n.Name -ne $thisNode) { continue }
        # Paused is deliberate (maintenance) and not the same as Down; Down on a two-node FCI
        # means the survivor is a single point of failure.
        $status = switch ([string]$n.State) {
            'Up'     { 'OK' }
            'Paused' { 'WARNING' }
            default  { 'CRITICAL' }
        }
        $rows += New-MetricRow "node:$($n.Name)" $n.State 'state' $status "cluster=$($cluster.Name), node=$($n.Name), state=$($n.State), is_local=$([bool]($n.Name -eq $thisNode)), nodes_up=$($upNodes.Count)/$($nodes.Count)"
    }

    # ------------------------------------------------------------------ resources, gathered first
    # Group severity depends on WHAT is offline inside the group, so the resources have to be
    # known before any group is judged.
    $ancillaryPattern = 'CEIP|Telemetry|Customer Experience'
    $offlineResources = @(Get-ClusterResource -ErrorAction SilentlyContinue | Where-Object { $_.State -ne 'Online' })
    $realOfflineByGroup = @{}
    foreach ($r in $offlineResources) {
        $isAncillary = ($r.Name -match $ancillaryPattern) -or ([string]$r.ResourceType -match $ancillaryPattern)
        if (-not $isAncillary) {
            $g = [string]$r.OwnerGroup
            $realOfflineByGroup[$g] = 1
        }
    }

    # ------------------------------------------------------------------ groups (roles) + owner
    $sqlOwnerNodes = @()
    foreach ($g in @(Get-ClusterGroup -ErrorAction SilentlyContinue)) {
        $isSql = $g.Name -match 'SQL'
        if ($isSql) { $sqlOwnerNodes += [string]$g.OwnerNode.Name }
        # Two things a first version got wrong, and both produced a permanent false warning on a
        # cluster that was serving perfectly:
        #
        #   * PartialOnline only matters if something REAL is offline inside the group. The SQL
        #     group sits at PartialOnline whenever the CEIP telemetry resource is off - which is a
        #     deliberate setting on most installs, and is already reported as LOGGING below.
        #   * "Available Storage" is the built-in group holding disks not assigned to any role.
        #     Offline is its normal state once every disk belongs to a role; it is not a finding.
        $hasRealOffline = $realOfflineByGroup.ContainsKey([string]$g.Name)
        $isAvailableStorage = ($g.Name -eq 'Available Storage')
        $status = switch ([string]$g.State) {
            'Online'        { 'OK' }
            'PartialOnline' { if ($hasRealOffline) { 'CRITICAL' } else { 'LOGGING' } }
            'Offline'       { if ($isSql) { 'CRITICAL' } elseif ($isAvailableStorage) { 'LOGGING' } else { 'WARNING' } }
            default         { if ($isSql) { 'CRITICAL' } else { 'WARNING' } }
        }
        if ($reportsClusterWide) {
            $rows += New-MetricRow "group:$($g.Name)" $g.State 'state' $status "cluster=$($cluster.Name), group=$($g.Name), state=$($g.State), owner_node=$($g.OwnerNode.Name), is_sql_role=$isSql"
        }
    }
    $ownsSql = $sqlOwnerNodes -contains $thisNode

    # ------------------------------------------------------------------ resources that are not online
    # Only the ones that are not Online: a healthy FCI has dozens of resources and listing them all
    # would bury the one that matters.
    # Anything that actually holds the role up - the SQL instance, its agent, the network name,
    # the IP, the shared disks - stays CRITICAL. Telemetry does not.
    foreach ($r in $offlineResources) {
        $ancillary = ($r.Name -match $ancillaryPattern) -or ([string]$r.ResourceType -match $ancillaryPattern)
        $status = if ($ancillary) { 'LOGGING' } else { 'CRITICAL' }
        $note = if ($ancillary) { ' - ancillary (telemetry) resource, not part of serving the role' } else { '' }
        if ($reportsClusterWide) {
            $rows += New-MetricRow "resource:$($r.Name)" $r.State 'state' $status "cluster=$($cluster.Name), resource=$($r.Name), state=$($r.State), owner_group=$($r.OwnerGroup), owner_node=$($r.OwnerNode.Name), resource_type=$($r.ResourceType)$note"
        }
    }

    # ------------------------------------------------------------------ recent failover
    # A failover that already completed leaves everything Online, so nothing above would mention
    # it — yet "this role moved at 03:14 last night" is usually the answer to why anything else
    # looks odd.
    try {
        $since = (Get-Date).AddHours(-24)
        $moves = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-FailoverClustering'; Id = 1201, 1204, 1069; StartTime = $since } -ErrorAction SilentlyContinue)
        $status = if ($moves.Count -gt 0) { 'WARNING' } else { 'OK' }
        $newest = if ($moves.Count -gt 0) { $moves[0].TimeCreated.ToString('yyyy-MM-dd HH:mm:ss') } else { 'none' }
        $rows += New-MetricRow 'failover_events_24h' $moves.Count 'events' $status "cluster=$($cluster.Name), failover_or_resource_events_24h=$($moves.Count), newest=$newest, window_hours=24"
    } catch {
        $rows += New-MetricRow 'failover_events_24h' 'UNKNOWN' 'events' 'OK' "Could not read the cluster event log: $($_.Exception.Message)"
    }

    # ------------------------------------------------------------------ role-aware services
    # The whole reason this is not OS_SERVICE_STATUS. On the passive node MSSQLSERVER is stopped
    # BY DESIGN; flagging it is how a healthy two-node FCI reports a permanent false critical.
    # ClusSvc is the exception - it must run on every node, active or passive, or that node is not
    # part of the cluster at all.
    $serviceExpectations = @(
        @{ Name = 'ClusSvc';        MustRun = $true },
        @{ Name = 'MSSQLSERVER';    MustRun = $ownsSql },
        @{ Name = 'SQLSERVERAGENT'; MustRun = $ownsSql }
    )
    foreach ($spec in $serviceExpectations) {
        $svc = Get-Service -Name $spec.Name -ErrorAction SilentlyContinue
        if (-not $svc) { continue }
        $running = ($svc.Status -eq 'Running')
        if ($spec.MustRun) {
            $status = if ($running) { 'OK' } else { 'CRITICAL' }
            $note = if ($running) { 'expected to run on this node' } else { 'expected to run on this node and is not' }
        } else {
            # Not expected to run here. Running anyway is not an error either — report it, do not
            # alert on it.
            $status = 'OK'
            $note = if ($running) { 'running on the passive node (informational)' } else { 'stopped on the passive node, which is correct for an FCI' }
        }
        $rows += New-MetricRow "service:$($spec.Name)" $svc.Status 'status' $status "cluster=$($cluster.Name), node=$thisNode, service=$($spec.Name), state=$($svc.Status), owns_sql_role=$ownsSql, expectation=$note"
    }

    ConvertTo-Json -InputObject @($rows) -Depth 4 -Compress
}
catch {
    ConvertTo-Json -InputObject @((New-MetricRow 'cluster' 'UNKNOWN' 'state' 'UNKNOWN' $_.Exception.Message)) -Depth 4 -Compress
}
