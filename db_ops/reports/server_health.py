"""``server-metrics.html``: the verdict: the health areas, the problems grouped by metric, the timeline, the score.

Split out of ``reports/server_report.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``server_report`` re-exports
every name, so no import changes.
"""

from __future__ import annotations
import datetime
from db_ops.lib import health_model
from db_ops.reports.server_series import SEVERITY_RANK, _is_percent_unit, severity_of


# A collection older than this means the page is describing the past, not the present. The
# densest metrics run every 5 minutes and the sparsest every 2 hours, so nothing being newer
# than 3 hours means collection itself has stopped — which is a finding, not a healthy server.
STALE_AFTER_SECONDS = 3 * 3600

# The health areas, in the order a DBA triages them. ``threshold`` states the rule that decided
# the status; ``report_judged`` marks the two areas the collector deliberately does not alert on
# (their SQL returns 'OK' on every branch), so the page must not pretend the collector agreed.
#
# ``selectors`` names **items**, not just metric codes, and their order is the area's priority.
# Whole-code membership plus "take the largest number" produced four confident lies on one run:
# the CPU tile read `WARNING 75.63 pct` from SYSTEM_CPU_MEMORY/sql_memory while CPU was 5-8%, the
# memory tile read `255314 MB` next to a percentage threshold, and Disk space read `53083 KB/s` —
# disk read throughput — because KB/s is a bigger number than any percentage. An area is a
# question ("how full is the storage"), and only items that answer *that* question belong in it.
#
# Each selector is ``{"code": ..., "items": (...), "exclude": (...), "units": ...}``; ``items``
# absent means every item of the code except ``exclude``, and ``units`` restricts to a unit family
# so a byte-rate row can never stand in for a percentage. ``item_suffix`` matches an item by its
# end, for a collector that names items after something of the estate's - DOCKER_CONTAINER_STATS
# writes ``<container>:cpu``, and the container's name is the operator's.
#
# A database in a container (the 0.26 node's PostgreSQL 5433 and SQL Server MSSQL25) has no host
# login, so no OS_* collector runs for it and its CPU and Memory tiles read "not collected" -
# while the container's own CPU and memory were collected every five minutes beside its SQL
# metrics (one target is one container). They are the last selector of each area: a target with
# OS or SQL readings shows those, a container target shows its container's. A container's CPU is
# percent of ONE core (200% = two cores busy) and the collector judges none of it; its memory is
# percent of the container's limit, WARNING at 90%.
AREAS: list[dict] = [
    {"key": "availability", "label": "Availability",
     "selectors": [{"code": "INSTANCE_STATUS"}, {"code": "DATABASE_STATUS"},
                   {"code": "DATABASE_SUSPECT_PAGES"}],
     "threshold": "instance reachable; every database ONLINE",
     "note": "If this is red the server is down or a database is not usable — nothing else matters first."},
    {"key": "cpu", "label": "CPU",
     # Only the two items that are CPU. OS_CPU_USAGE also carries processor queue length and load
     # average; SYSTEM_CPU_MEMORY also carries two memory percentages.
     "selectors": [{"code": "OS_CPU_USAGE", "items": ("cpu_usage",)},
                   {"code": "SYSTEM_CPU_MEMORY", "items": ("cpu",)},
                   {"code": "DOCKER_CONTAINER_STATS", "item_suffix": (":cpu",), "units": "percent"}],
     "threshold": "WARN ≥ 80% · CRITICAL ≥ 90%",
     "note": "Sustained high CPU makes every query slower; check the top process and the heaviest queries."},
    {"key": "memory", "label": "Memory",
     "selectors": [{"code": "OS_MEMORY_USAGE", "items": ("memory_usage",)},
                   {"code": "SYSTEM_CPU_MEMORY", "items": ("system_memory", "sql_memory")},
                   {"code": "OS_MEMORY_USAGE", "items": ("swap_usage", "pagefile_usage")},
                   {"code": "PAGE_LIFE_EXPECTANCY"},
                   {"code": "DOCKER_CONTAINER_STATS", "item_suffix": (":memory",), "units": "percent"}],
     "threshold": "WARN ≥ 85% · CRITICAL ≥ 95% used · PLE < 2000 s = pressure (report rule)",
     "report_judged": True,
     "note": "Low page life expectancy means the buffer pool is churning: pages are read from disk again and again."},
    {"key": "disk_space", "label": "Disk space",
     # Mount points and file-fullness only. The throughput and queue-length rows of OS_DISK_USAGE
     # are storage *activity*; they have their own area below.
     "selectors": [{"code": "OS_DISK_USAGE",
                    "exclude": ("disk_read_kbps", "disk_write_kbps", "disk_iops", "disk_queue_length")},
                   {"code": "STORAGE_DISK_FREE_SPACE"},
                   {"code": "STORAGE_DATA_FILE_SPACE"},
                   {"code": "LOG_FILE_SPACE"}],
     "threshold": "WARN ≥ 85% used · CRITICAL ≥ 95% used",
     "note": "A full data or log volume stops writes: the database goes read-only or the instance stalls."},
    {"key": "disk_latency", "label": "Disk latency",
     "selectors": [{"code": "PERFORMANCE_IO_LATENCY"}],
     "threshold": "WARN ≥ 20 ms · CRITICAL ≥ 50 ms — report rule, the collector logs only. "
                  "Cumulative since engine start, not interval latency",
     "report_judged": True,
     "note": "Latency above ~20 ms per read means storage, not SQL, is the bottleneck. This value "
             "is an average since SQL Server started, so it describes the history of this "
             "instance's storage, not necessarily its state right now."},
    {"key": "storage_activity", "label": "Storage activity",
     "selectors": [{"code": "OS_DISK_USAGE",
                    "items": ("disk_queue_length", "disk_iops", "disk_read_kbps", "disk_write_kbps")}],
     "threshold": "WARN queue length ≥ 2 per spindle — throughput and IOPS are logged, not judged",
     "note": "How hard the storage is being worked. A high queue with low throughput is a storage "
             "limit; high throughput on its own is just a busy server.",
     "report_judged": True},
    {"key": "blocking", "label": "Blocking",
     "selectors": [{"code": "LOCK_BLOCKING_SESSIONS"}, {"code": "LOCK_DEADLOCK_RECENT"},
                   {"code": "LOCK_TRANSACTION_HOLDERS"}],
     "threshold": "WARN any blocked session · CRITICAL ≥ 10 blocked or blocked ≥ 300 s",
     "note": "One session holding a lock can stall an application entirely; find the head blocker."},
    {"key": "long_queries", "label": "Long-running queries",
     "selectors": [{"code": "QUERY_LONG_RUNNING"},
                   {"code": "QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS"}],
     "threshold": "WARN ≥ 900 s · CRITICAL ≥ 3600 s",
     "note": "A query running for an hour is usually a missing index, a bad plan, or a runaway job."},
    {"key": "backup", "label": "Backup",
     # Deliberately has no selectors: this area is decided by the per-database backup policy in
     # db_ops.lib.backup_policy, not by whichever backup row happens to carry the largest
     # number. "OK 3185 hours_since_last_backup" under a 48-hour threshold is what code-only
     # membership produced here.
     "selectors": [],
     "policy": "backup",
     "threshold": "per-database policy: required FULL/DIFF/LOG age from data/backup_policy.json",
     "note": "Backup age is your worst-case data loss. A stale log backup means point-in-time "
             "recovery is not possible for that database."},
    {"key": "jobs", "label": "Failed jobs",
     "selectors": [{"code": "JOB_FAILED"}, {"code": "SQL_AGENT_JOB_RUNTIME"}],
     "threshold": "WARN any job failed since the last run",
     "note": "A failed maintenance job is a backup, an index rebuild or a purge that silently did not happen."},
    {"key": "tempdb", "label": "TempDB",
     "selectors": [{"code": "STORAGE_TEMP_SPACE"}],
     "threshold": "WARN ≥ 85% used and < 2 GB free · CRITICAL ≥ 95% used and < 1 GB free",
     "note": "TempDB filling up fails sorts, hashes and version store — often a single bad query."},
    {"key": "security", "label": "Security",
     "selectors": [{"code": "SECURITY_FAILED_LOGINS"}, {"code": "SECURITY_LOGIN_HEALTH"},
                   {"code": "SECURITY_CERTIFICATE_EXPIRY"}, {"code": "DATABASE_USER_PERMISSIONS"}],
     "threshold": "no sustained failed logins · no password older than the collector's threshold "
                  "· no certificate expiring",
     "note": "Thousands of failed logins a day is a credential being guessed or an integration "
             "retrying a dead one — and it fills the error log either way."},
    {"key": "ha", "label": "HA / replication",
     "selectors": [{"code": "AVAILABILITY_DATABASE_HEALTH"}, {"code": "POSTGRES_REPLICATION"},
                   {"code": "POSTGRES_REPLICATION_SLOTS"}, {"code": "POSTGRES_WAL_ARCHIVE"}],
     "threshold": "every replica CONNECTED and SYNCHRONIZED / streaming",
     "note": "A replica that stopped synchronising is not a replica: failover would lose data. "
             "AVAILABILITY_DATABASE_HEALTH reads Always On DMVs only — on a Failover Cluster "
             "Instance it reports NOT_CONFIGURED, which is not an FCI health verdict."},
]

#: Every metric code any area selects. ``build_problems`` uses it to find an entry's area.
AREA_CODES: dict[str, list[str]] = {
    spec["key"]: sorted({selector["code"] for selector in spec["selectors"]}) for spec in AREAS
}

# What to do about it. Keyed by metric code; the area falls back to its own note.
ACTIONS: dict[str, str] = {
    "INSTANCE_STATUS": "Check the service is running and the port is reachable from the collector.",
    # Its own action, because since it joined the CPU and Memory areas the fallback would be the
    # CPU note - and a container that stopped or keeps restarting is not a CPU problem.
    "DOCKER_CONTAINER_STATS": "Check the container on its docker host (`docker ps -a`, `docker logs "
                              "<name>`): one that stopped or keeps restarting takes its database "
                              "with it; memory near its limit is the limit to raise or the query "
                              "to find.",
    "QUERY_STORE_COVERAGE": "Query Store is off on these databases, so a slowdown cannot be "
                            "diagnosed after the fact. Turn it on (READ_WRITE) where the workload "
                            "matters; this is a configuration finding, reported once a morning.",
    "OS_TCP_PORT_STATUS": "A configured port is not answering where clients connect. CLOSED means "
                          "nothing is listening — check the service that owns the port. "
                          "LOOPBACK_ONLY means it listens on 127.0.0.1 but not on the host address, "
                          "so it is up and still unreachable: fix the bind address. OPEN with a "
                          "WARNING means the socket accepts but TLS or HTTP did not answer — read "
                          "the probe detail in the message.",
    "LINKED_SERVER_STATUS": "See the Linked servers table below: it says, per server, whether to "
                            "keep, fix or drop it, and which machine the fix belongs on.",
    "DATABASE_STATUS": "A database is not ONLINE: check the error log, then bring it online or restore it.",
    "DATABASE_SUSPECT_PAGES": "Suspect pages mean corruption: restore the affected pages from backup and run CHECKDB.",
    "OS_CPU_USAGE": "Find the top process (below) and the heaviest queries; check for a runaway job.",
    "OS_MEMORY_USAGE": "Check max server memory against host RAM and what else runs on this host.",
    "OS_DISK_USAGE": "Free space or extend the volume before writes stop.",
    "STORAGE_DISK_FREE_SPACE": "Free space or extend the volume before writes stop.",
    "STORAGE_TEMP_SPACE": "Find the query spilling to TempDB; consider more/larger TempDB files.",
    "PERFORMANCE_IO_LATENCY": "Storage is slow: check the datastore, and whether one file is hotter than the rest.",
    "PAGE_LIFE_EXPECTANCY": "Buffer pool is churning: review max server memory, missing indexes and plan-cache bloat.",
    "LOCK_BLOCKING_SESSIONS": "Find the head blocker and decide whether to wait for it or kill it.",
    "LOCK_DEADLOCK_RECENT": "Review the deadlock graph; usually two statements taking locks in a different order.",
    "QUERY_LONG_RUNNING": "Identify the query and its plan; kill it if it is a runaway, index it if it is not.",
    "BACKUP_AGE": "Check the backup job and the recovery model; run a fresh backup and re-establish the log chain.",
    "BACKUP_LAST_RESULT": "The last backup did not succeed: read the job history before trusting any restore.",
    "JOB_FAILED": "Read the job history: a failed job is work that silently did not happen.",
    "AVAILABILITY_DATABASE_HEALTH": "A replica is not synchronising: failover would lose data. Check the endpoint and the log send queue.",
    "POSTGRES_REPLICATION": "A standby is not streaming: check its connection, slot and WAL retention.",
    "OS_SERVICE_STATUS": "Start the service and find out why it stopped (event log, recovery settings).",
    "OS_EVENTLOG_CRITICAL": "Read the errors in the Windows event log — they usually name the cause.",
    "SECURITY_FAILED_LOGINS": "Find the source host of the attempts, then fix or disable the principal. "
                              "Sustained failures are an attack or a dead credential being retried.",
    "SECURITY_LOGIN_HEALTH": "Rotate the logins whose password is past the threshold, starting with sa "
                             "and anything holding db_owner.",
    "SECURITY_CERTIFICATE_EXPIRY": "Renew before it expires: an expired certificate breaks encrypted "
                                   "connections and backup encryption.",
    "DATABASE_USER_PERMISSIONS": "Review who holds db_owner on this database; remove what is not needed.",
    # Without these the row fell back to "Check the metric detail below." — which is what the
    # reader was already doing, and is not an action.
    "MAINTENANCE_STATISTICS_AGE": "Statistics this old give the optimizer a wrong row estimate: "
                                  "run UPDATE STATISTICS, and check whether the statistics job is disabled.",
    "MAINTENANCE_INDEX_FRAGMENTATION": "Rebuild or reorganize the index; if many are drifting, the "
                                       "index-maintenance job is not running.",
    "DATABASE_CONSTRAINT_HEALTH": "An untrusted or disabled constraint no longer guarantees the data "
                                  "and the optimizer stops using it: re-check it WITH CHECK.",
    "LOG_RECENT_CRITICAL": "Read the SQL Server error log around that time — the entry names the cause.",
    "LOG_FILE_SPACE": "The log file is nearly full: back up the log (or check log_reuse_wait) before "
                      "it grows or writes stop.",
    "LOG_REUSE_WAIT": "This is why the log cannot truncate. LOG_BACKUP means no log backup is running "
                      "on a FULL-recovery database.",
    "QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS": "A request stuck waiting or rolling back holds its locks: "
                                               "find what it waits on before killing it — rollback cannot be hurried.",
    "OS_REBOOT_PENDING": "A pending reboot leaves patches half-applied; schedule it with the application owner.",
    "INSTANCE_CONNECTIONS": "Session count is unusually high: check for an application not closing "
                            "connections, or a pool sized larger than the server can serve.",
    "STORAGE_DATA_FILE_SPACE": "The data file is near its allocated size: grow it deliberately rather "
                               "than letting autogrowth stall a transaction.",
    "DATABASE_CHECKDB": "No last-known-good CHECKDB: run DBCC CHECKDB — corruption is only found by looking.",
}


def series_severity(entry: dict) -> str:
    return health_model.current_severity(
        code=entry["code"], status=entry.get("status"), value=entry.get("last"))


def series_downgraded(entry: dict) -> str:
    """The severity ``metrics.metric_overrides`` took off this entry, ``""`` when none was."""
    return health_model.downgraded_from(str(entry.get("message") or ""))


def _value_text(entry: dict) -> str:
    if entry.get("numeric") and entry.get("last") is not None:
        value = entry["last"]
        number = f"{value:.0f}" if abs(value) >= 100 else f"{value:g}"
        unit = str(entry.get("unit") or "")
        suffix = "%" if unit.lower().startswith("percent") else (f" {unit}" if unit else "")
        return f"{number}{suffix}"
    # A collector-failure row has no value at all — that is what it is saying. "—" on the tile
    # reads as "nothing to report", which is the opposite of the truth.
    if entry.get("item") == health_model.COLLECTOR_ITEM and not entry.get("lastText"):
        return "collector failed"
    return str(entry.get("lastText") or "—")


def _selector_matches(selector: dict, entry: dict) -> bool:
    if entry["code"] != selector["code"]:
        return False
    item = str(entry.get("item") or "")
    items = selector.get("items")
    if items is not None and item not in items:
        return False
    suffixes = selector.get("item_suffix")
    if suffixes is not None and not item.endswith(tuple(suffixes)):
        return False
    if item in (selector.get("exclude") or ()):
        return False
    if selector.get("units") == "percent" and not _is_percent_unit(entry.get("unit")):
        return False
    return True


def area_members(spec: dict, series: list[dict]) -> list[tuple[int, dict]]:
    """``(priority, entry)`` for every series entry this area selects.

    ``priority`` is the selector's position in the area: the first selector is what the area is
    *about*, the rest are supporting evidence. It decides which value the tile shows when nothing
    is wrong, so a healthy CPU area shows CPU rather than whichever of its members happens to
    carry the largest number.
    """
    return [
        (priority, entry)
        for priority, selector in enumerate(spec.get("selectors") or [])
        for entry in series
        if _selector_matches(selector, entry)
    ]


def build_areas(series: list[dict], *, backup: dict | None = None,
                freshness: dict | None = None) -> list[dict]:
    """Current state of each health area: the worst thing in it, its value, and the rule.

    Selection is ``(severity, downgraded severity, selector priority, value)`` in that order.
    Severity first because an area exists to say whether something is wrong; the severity a
    ``severity_map`` removed second, so a silenced finding is not hidden by a healthy neighbour;
    priority third so the tile answers its own question; value last so that among equals the
    fullest disk wins. Comparing raw values *first* is what let 53083 KB/s decide an area measured
    in percent.
    """
    stale_codes = {row["code"] for row in (freshness or {}).get("metrics", [])
                   if row.get("state") in ("LATE", "FAILED")}

    areas = []
    for spec in AREAS:
        base = {key: spec[key] for key in ("key", "label", "threshold", "note")}
        base["reportJudged"] = bool(spec.get("report_judged"))
        if spec.get("policy") == "backup":
            areas.append({**base, **_backup_area_state(backup)})
            continue
        members = area_members(spec, series)
        if not members:
            areas.append({**base, "status": "UNKNOWN", "value": "not collected", "detail": "",
                          "sourceCode": "", "sourceItem": "", "collectedAt": None, "stale": False,
                          "downgradedFrom": ""})
            continue
        # Severity, then the severity config *removed*, then priority, then value. The second key
        # is what keeps a silenced finding from being hidden by a genuinely healthy neighbour: a
        # CPU area holding a downgraded-from-CRITICAL cpu_usage and an OK load_average must show
        # the one somebody decided not to alert on, not the one that had nothing to say.
        priority, worst = max(
            members,
            key=lambda pair: (SEVERITY_RANK[series_severity(pair[1])],
                              SEVERITY_RANK.get(series_downgraded(pair[1]), 0), -pair[0],
                              pair[1].get("last") or 0),
        )
        status = series_severity(worst)
        not_ok = sum(1 for _priority, entry in members if series_severity(entry) != "OK")
        detail = f"{worst['label']} · {worst['item']}"
        if not_ok > 1:
            detail += f" (+{not_ok - 1} more not OK)"
        # An area whose own metric is late or failing does not get to report OK from the last
        # sample that worked. It reports what it is: unknown, and why.
        stale = sorted(stale_codes.intersection(
            selector["code"] for selector in spec["selectors"]))
        if stale and status == "OK":
            status = "UNKNOWN"
            detail = f"{', '.join(stale)} is late or failing — this area is not current"
        areas.append({
            **base, "status": status, "value": _value_text(worst), "detail": detail,
            "sourceCode": worst["code"], "sourceItem": worst["item"],
            "unit": str(worst.get("unit") or ""), "collectedAt": worst.get("lastAt"),
            "stale": bool(stale), "priority": priority,
            # Not part of the status: the tile stays the colour config asked for. It is the
            # missing half of the sentence — "this value crossed the rule printed above it, and
            # somebody decided that is not an alert here".
            "downgradedFrom": series_downgraded(worst),
        })
    return areas


def _backup_area_state(backup: dict | None) -> dict:
    """The Backup tile, from the per-database policy rather than from a metric row.

    See :mod:`db_ops.lib.backup_policy`. The old rule took the largest number out of mixed
    FULL/DIFF/LOG/job rows, which on the ERP FCI produced ``OK 3185 hours_since_last_backup``
    under a stated 48-hour threshold — the number was the finding and the status was the wrong
    row's.
    """
    if not backup or not backup.get("databases"):
        return {"status": "UNKNOWN", "value": "not collected", "detail": "",
                "sourceCode": "", "sourceItem": "", "collectedAt": None, "stale": False,
                "downgradedFrom": ""}
    summary = backup.get("summary") or {}
    return {
        "status": str(summary.get("status") or "UNKNOWN"),
        "value": f"{summary.get('compliant', 0)}/{summary.get('eligible', 0)} compliant",
        "detail": str(summary.get("reason") or ""),
        "sourceCode": "BACKUP_LAST_RESULT", "sourceItem": "policy",
        "collectedAt": summary.get("collectedAt"), "stale": False,
        # The backup tile is decided by policy, not by a metric row, so no severity_map ever
        # reaches it. Stated rather than left absent, so every tile has the same shape.
        "downgradedFrom": "",
    }


_age_hint = health_model.age_hint


def metric_action(code: str) -> str:
    """What to do about ``code``, falling back to its area's note. Shared with the fleet page."""
    area = next((spec for spec in AREAS if code in AREA_CODES[spec["key"]]), None)
    return ACTIONS.get(code) or (area or {}).get("note") or "Check the metric detail below."


def build_problems(series: list[dict], *, now: int | None = None) -> list[dict]:
    """What is not OK right now, **grouped by metric**, worst first, each saying what to do.

    The grouping itself lives in :func:`db_ops.lib.health_model.group_findings`, because the
    fleet page's Priority Attention is built from exactly the same structure — that is what stops
    the two pages describing the same server differently.
    """
    now = now if now is not None else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    return health_model.group_findings([
        {
            "code": entry["code"],
            "label": entry["label"],
            "item": entry["item"],
            "value": _value_text(entry),
            "severity": series_severity(entry),
            "message": entry.get("message", ""),
            "lastText": entry.get("lastText", ""),
            "action": metric_action(entry["code"]),
            "collectedAt": entry.get("lastAt"),
        }
        for entry in series
    ], now=now)


def build_timeline(series: list[dict], *, limit: int = 14) -> list[dict]:
    """When each problem started, and whether it has cleared.

    Answers "is this getting better or worse" — the one question a table of current values
    cannot. Built from the status of every stored sample, so a warning that came and went at
    03:00 is still visible at 09:00.
    """
    incidents = []
    for entry in series:
        # A weekly metric has one sample per item, so every fragmented index would open its
        # own "ongoing incident" and bury the events this section exists for. Its state is
        # already in Problems; an incident needs a before and an after.
        if entry.get("lowCadence"):
            continue
        open_incident = None
        for epoch, _value, status in entry["points"]:
            severity = severity_of(status)
            if severity in ("CRITICAL", "WARNING"):
                if open_incident is None:
                    open_incident = {"label": entry["label"], "item": entry["item"],
                                     "severity": severity, "start": epoch, "end": None, "samples": 1}
                else:
                    open_incident["samples"] += 1
                    if SEVERITY_RANK[severity] > SEVERITY_RANK[open_incident["severity"]]:
                        open_incident["severity"] = severity   # it got worse while it was open
            elif open_incident is not None:
                open_incident["end"] = epoch
                incidents.append(open_incident)
                open_incident = None
        if open_incident is not None:
            incidents.append(open_incident)      # still open at the end of the window

    # Ongoing and cleared get their own share of the list. Sorting "ongoing first, then by
    # start" filled all 14 slots on the ERP host with the same 14 standing security warnings,
    # so every incident that came *and went* — a TempDB volume hitting 100% — was cut, and the
    # section meant to show "better or worse" showed only "still bad".
    ongoing = sorted((i for i in incidents if i["end"] is None), key=lambda i: -i["start"])
    cleared = sorted((i for i in incidents if i["end"] is not None), key=lambda i: -i["end"])
    half = max(1, limit // 2)
    keep_ongoing = ongoing[:max(half, limit - len(cleared))]
    keep_cleared = cleared[:max(half, limit - len(keep_ongoing))]
    return keep_ongoing + keep_cleared


def build_health(series: list[dict], problems: list[dict], *, now: int,
                 freshness: dict | None = None) -> dict:
    """The verdict. UNKNOWN when the data is too old to describe the present.

    The score is a blunt instrument on purpose: a critical costs 20 and a warning 5, so one
    critical can never be hidden by a wall of green. It is a shorthand for "how bad", not a
    measurement.
    """
    last_at = max((entry["lastAt"] for entry in series), default=None)
    age = None if last_at is None else max(0, now - last_at)
    stale = last_at is None or age > STALE_AFTER_SECONDS

    # Counted per failing item, not per group: grouping is how the page is *read*, but "3
    # databases offline" must not score the same as one.
    items = [row for problem in problems for row in problem["items"]]
    critical = sum(1 for row in items if row["severity"] == "CRITICAL")
    warning = sum(1 for row in items if row["severity"] == "WARNING")

    if stale:
        status = "UNKNOWN"
    elif critical:
        status = "CRITICAL"
    elif warning:
        status = "WARNING"
    else:
        status = "HEALTHY"

    score = max(0, 100 - min(60, 20 * critical) - min(30, 5 * warning))
    if stale:
        score = min(score, 40)   # a server nobody is collecting from is not a healthy server

    failed = list((freshness or {}).get("failed") or [])
    late = list((freshness or {}).get("late") or [])
    return {
        "status": status,
        "score": score,
        "critical": critical,
        "warning": warning,
        "lastCollected": last_at,
        "ageSeconds": age,
        "stale": stale,
        "staleAfterSeconds": STALE_AFTER_SECONDS,
        "seriesCount": len(series),
        # Monitoring's own health, kept separate from the server's. A page saying HEALTHY while
        # four of its metrics have not returned in two days is describing what it can still see,
        # and it has to say how much that is.
        "failedMetrics": failed,
        "lateMetrics": late,
        "metricsSeen": (freshness or {}).get("seen"),
        "metricsExpected": (freshness or {}).get("expected"),
        "metricsNotCollected": [entry["code"] for entry in (freshness or {}).get("notCollected") or []],
    }
