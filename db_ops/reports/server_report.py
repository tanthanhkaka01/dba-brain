"""Server metric history: one page for the fleet, answering "is this server healthy?".

The fleet inventory report answers "which server is in trouble". This answers the next question
— "what is wrong with this one, and is it getting worse" — from ``metric_results``.

A wall of charts does not answer that. A DBA opening this page has to know, in seconds: is it
healthy, what is broken now, what needs doing, and is it improving. So the page leads with a
verdict (health summary), then the current state of each health area with its threshold, then
the problems in severity order with an action, then a timeline of when things started and
cleared — and only then the charts, with the ones that matter open and the rest folded away.

Two rules the analysis follows, because breaking either produces a confident lie:

* **Status comes from the collector**, which computed it against the thresholds in its own SQL.
  The report never re-decides that a WARNING is really fine.
* Except where the collector deliberately does not judge: PERFORMANCE_IO_LATENCY and
  PAGE_LIFE_EXPECTANCY return 'OK' on every branch (they are logging-only). Their area is
  classified here instead, and the threshold shown says so, so nobody reads a report threshold
  as an alerting one.

**One page, not one page per server.** Stamping a page per server per run cost 6.5 MB every
run (18 servers, the ERP host alone 706 KB): at one inventory run every two hours that is
~2.3 GB a month of near-duplicate HTML. So the page (``server-metrics.html``) and the series
files (``server-metrics_<slug>.json``) have stable names and are overwritten each run, and the
page fetches only the series of the server being looked at.

Charts are hand-drawn SVG — the worker has no internet, so no chart library can be fetched.
"""

from __future__ import annotations
from db_ops.lib.coerce import as_float

import datetime
import json
import math
import re
from pathlib import Path

from db_ops.common import data_sources
from db_ops.lib import report_archive
from db_ops.lib import backup_policy, capacity_forecast
from db_ops.lib import health_model, interval_rates
from db_ops.db.metric_store import MetricStore
from db_ops.db.metric_store import _parse_as_of as metric_store_as_of
#: Declared **above** the ``inventory_health`` import below, not beside the section that uses it.
#: The two modules import each other — this one for the shared message parser, that one for this
#: constant — so whichever is imported first walks into the other mid-initialisation. With the
#: constant defined after the import, importing ``server_report`` first raised ``ImportError:
#: cannot import name 'QUERY_STORE_CODE' from partially initialized module``, which is every one
#: of the six ``test_server_report_*`` files failing to collect when run on its own.
QUERY_STORE_CODE = "QUERY_STORE_COVERAGE"

# Same app, and deliberately the same parser: the fleet page's per-database Query Store column
# and this page's Query Store section must not decode the collector's message two ways.
from db_ops.reports import inventory_health  # noqa: E402 - after QUERY_STORE_CODE, see above
from db_ops.lib.paths import DEFAULT_DATA_DIR

TEMPLATE_HTML = Path(__file__).resolve().parent / "templates" / "server_report.html"

# A metric like LOCK_SLEEPING_OPEN_TRANSACTION keys its rows by session id: on the ERP host
# that is 777 one-off "series" in a 7-day window, none of which is a series at all. An item is
# only charted when it is still present in the most recent collection of its metric — that is
# what makes it a thing that exists (a drive, a database, a service), not a past event.
MAX_ITEMS_PER_METRIC = 24
# Database size is an inventory-like dimension: the report must not silently stop at the
# generic 24-item chart limit when an instance hosts more databases. Keep a high guardrail for
# pathological inputs while showing every database on normal SQL Server estates.
MAX_DATABASE_SIZE_ITEMS = 512
MAX_POINTS = 240  # a 7-day window at one sample/hour is 168 points; denser metrics are bucketed
# Being in the latest collection is not enough on its own: a session id that was seen once is
# also "the latest" of its own metric. A series needs a history to be a series.
MIN_POINTS = 3
# ...unless the metric could not possibly have produced that many samples. Index fragmentation
# runs weekly, so in a 7-day window every index has one sample and the whole metric was dropped
# — 0 of 42 fragmented indexes shown, the worst at 96%. A metric whose configured cadence
# cannot fill MIN_POINTS in the window is exempt: one sample per item *is* its full history.
# The cadence comes from the metric catalog, so nothing here has to guess which metrics those
# are; without the catalog the exemption simply does not apply.
METRIC_DEFINITIONS = DEFAULT_DATA_DIR / "metric_definitions.json"


PAGE_NAME = "server-metrics.html"

# A collection older than this means the page is describing the past, not the present. The
# densest metrics run every 5 minutes and the sparsest every 2 hours, so nothing being newer
# than 3 hours means collection itself has stopped — which is a finding, not a healthy server.
STALE_AFTER_SECONDS = 3 * 3600

# LOGGING is the collector saying "recorded, not an alert" — it is not a problem.
# All four come from db_ops.lib.health_model so this page and the fleet page cannot drift
# apart on what CRITICAL means; they are re-exported under their old names because that is what
# the rest of this module (and its tests) read.
CRITICAL_STATUSES = health_model.CRITICAL_STATUSES
WARNING_STATUSES = health_model.WARNING_STATUSES
SEVERITY_RANK = health_model.SEVERITY_RANK

# Metric codes are what the collector calls things. A DBA scanning a page should read English.
METRIC_LABELS: dict[str, str] = {
    "INSTANCE_STATUS": "Instance up",
    "DATABASE_STATUS": "Database state",
    "DATABASE_CONFIG": "Database settings",
    "DATABASE_CHECKDB": "Last CHECKDB",
    "DATABASE_SUSPECT_PAGES": "Suspect pages",
    "INSTANCE_CONNECTIONS": "Sessions",
    "QUERY_LONG_RUNNING": "Long-running queries",
    "QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS": "Waiting / rolling back",
    "QUERY_STORE_QUERY_ISSUES": "Query Store — heavy queries",
    "QUERY_STORE_COVERAGE": "Query Store — not capturing",
    "LOCK_BLOCKING_SESSIONS": "Blocking",
    "LOCK_DEADLOCK_RECENT": "Deadlocks",
    "LOCK_TRANSACTION_HOLDERS": "Open transactions",
    "LOCK_SLEEPING_OPEN_TRANSACTION": "Sleeping sessions with open transaction",
    "BACKUP_AGE": "Backup age",
    "BACKUP_LAST_RESULT": "Last backup result",
    "BACKUP_JOB_STATUS": "Backup jobs",
    "JOB_FAILED": "Failed jobs",
    "SQL_AGENT_JOB_INVENTORY": "SQL Agent jobs",
    "SQL_AGENT_JOB_RUNTIME": "SQL Agent job runtime",
    "STORAGE_DISK_FREE_SPACE": "Disk free space",
    "STORAGE_DATA_FILE_SPACE": "Data file space",
    "LOG_FILE_SPACE": "Log file space",
    "DATABASE_DATA_SIZE": "Database data size",
    "DATABASE_LOG_SIZE": "Database log size",
    "STORAGE_TEMP_SPACE": "TempDB space",
    "STORAGE_FILE_PLACEMENT": "File placement",
    "LOG_REUSE_WAIT": "Log reuse wait",
    "LOG_RECENT_CRITICAL": "Recent error log entries",
    "LINKED_SERVER_STATUS": "Linked servers",
    "PERFORMANCE_IO_LATENCY": "Disk latency",
    "PERFORMANCE_WAIT_STATS": "Wait statistics",
    "PAGE_LIFE_EXPECTANCY": "Page life expectancy",
    "SYSTEM_CPU_MEMORY": "CPU & memory (engine)",
    "SQL_CONFIGURATION": "Instance configuration",
    "AVAILABILITY_DATABASE_HEALTH": "Availability group",
    "POSTGRES_REPLICATION": "Replication",
    "POSTGRES_REPLICATION_SLOTS": "Replication slots",
    "POSTGRES_WAL_ARCHIVE": "WAL archiving",
    "POSTGRES_LONG_TRANSACTIONS": "Long transactions",
    "POSTGRES_VACUUM_HEALTH": "Vacuum",
    "POSTGRES_XID_WRAPAROUND": "Transaction ID wraparound",
    "POSTGRES_DATABASE_HEALTH": "Database health",
    "POSTGRES_INVALID_INDEXES": "Invalid indexes",
    "POSTGRES_IDENTITY_ROLE": "Replication role",
    "TABLESPACE_FREE_SPACE": "Tablespace free space",
    "PROCESS_LIMIT": "Process limit",
    "SHARED_POOL_FREE": "Shared pool",
    "LIBRARY_CACHE": "Library cache",
    "BUFFER_CACHE_HIT": "Buffer cache hit ratio",
    "TOP_DISK_READ_SQL": "Top disk-read SQL",
    "OS_INFO": "OS",
    "OS_CPU_USAGE": "CPU",
    "OS_MEMORY_USAGE": "Memory",
    "OS_DISK_USAGE": "Disk usage",
    "OS_NETWORK": "Network",
    "OS_UPTIME": "Uptime",
    "OS_SERVICE_STATUS": "Services",
    "OS_PROCESS_TOP_CPU": "Top process by CPU",
    "OS_PROCESS_TOP_MEMORY": "Top process by memory",
    "OS_EVENTLOG_CRITICAL": "Event log errors",
    "OS_REBOOT_PENDING": "Pending reboot",
    "OS_TIME_SYNC": "Time sync",
    "OS_TCP_PORT_STATUS": "TCP ports",
}

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
# so a byte-rate row can never stand in for a percentage.
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
                   {"code": "SYSTEM_CPU_MEMORY", "items": ("cpu",)}],
     "threshold": "WARN ≥ 80% · CRITICAL ≥ 90%",
     "note": "Sustained high CPU makes every query slower; check the top process and the heaviest queries."},
    {"key": "memory", "label": "Memory",
     "selectors": [{"code": "OS_MEMORY_USAGE", "items": ("memory_usage",)},
                   {"code": "SYSTEM_CPU_MEMORY", "items": ("system_memory", "sql_memory")},
                   {"code": "OS_MEMORY_USAGE", "items": ("swap_usage", "pagefile_usage")},
                   {"code": "PAGE_LIFE_EXPECTANCY"}],
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

# What is worth looking at first, second, and only when digging. Anything not listed is 'detail'.
PRIMARY_CODES = {
    # OS_CPU_USAGE (% CPU), OS_MEMORY_USAGE (% and MB used), OS_DISK_USAGE (% full plus
    # read/write KB/s) and OS_NETWORK (send/receive Mbps) are the four numbers a DBA reads
    # first on any host, so they open with the page.
    "OS_CPU_USAGE", "OS_MEMORY_USAGE", "OS_DISK_USAGE", "OS_NETWORK", "SYSTEM_CPU_MEMORY",
    "STORAGE_DISK_FREE_SPACE", "STORAGE_TEMP_SPACE", "PERFORMANCE_IO_LATENCY",
    "INSTANCE_CONNECTIONS", "LOCK_BLOCKING_SESSIONS", "BACKUP_AGE",
    "AVAILABILITY_DATABASE_HEALTH", "POSTGRES_REPLICATION",
}
DIAGNOSTIC_CODES = {
    "PAGE_LIFE_EXPECTANCY", "PERFORMANCE_WAIT_STATS", "QUERY_LONG_RUNNING",
    "QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS", "LOCK_DEADLOCK_RECENT", "LOCK_TRANSACTION_HOLDERS",
    "STORAGE_DATA_FILE_SPACE", "LOG_FILE_SPACE", "LOG_REUSE_WAIT", "JOB_FAILED",
    "OS_PROCESS_TOP_CPU", "OS_PROCESS_TOP_MEMORY", "OS_EVENTLOG_CRITICAL",
    "QUERY_STORE_QUERY_ISSUES", "QUERY_STORE_COVERAGE",
    "POSTGRES_VACUUM_HEALTH", "POSTGRES_LONG_TRANSACTIONS",
    "POSTGRES_XID_WRAPAROUND", "POSTGRES_REPLICATION_SLOTS", "POSTGRES_WAL_ARCHIVE",
    "BUFFER_CACHE_HIT", "LIBRARY_CACHE", "SHARED_POOL_FREE", "TABLESPACE_FREE_SPACE",
}
DATABASE_SIZE_CODES = {"DATABASE_DATA_SIZE", "DATABASE_LOG_SIZE"}

# What to do about it. Keyed by metric code; the area falls back to its own note.
ACTIONS: dict[str, str] = {
    "INSTANCE_STATUS": "Check the service is running and the port is reachable from the collector.",
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


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-") or "server"


def series_file_name(server_id: str) -> str:
    return f"server-metrics_{_slug(server_id)}.json"


def index_usage_file_name(server_id: str) -> str:
    """The index report published for this server, named from the same slug this page uses."""
    return f"index-usage_{_slug(server_id)}.html"


def page_href(server_id: str) -> str:
    return f"{PAGE_NAME}?server={_slug(server_id)}"




def _epoch(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


def _message_kv(message: str) -> dict[str, str]:
    """Extract the structured ``key=value`` fields carried in collector messages.

    One parser, shared with the collector: the page and the alert must read the same sample the
    same way, or they describe the same server differently.
    """
    return interval_rates.message_fields(message)


def _is_percent_unit(unit: str) -> bool:
    """Recognise every percentage spelling used by the collectors.

    Several SQL metrics use ``pct`` rather than ``percent``. The old browser-only check missed
    those units, so Data file, Log file, TempDB, and SQL memory charts were scaled to their
    seven-day observed min/max instead of the real 0..100 domain.
    """
    raw = str(unit or "").strip().lower()
    compact = re.sub(r"[^a-z0-9%]+", "_", raw).strip("_")
    return ("%" in raw or "percent" in compact or compact == "pct"
            or compact.startswith("pct_") or compact.endswith("_pct"))


def _series_capacity(code: str, item: str, unit: str, message: str) -> float | None:
    """Return a physical ceiling when the collector supplies one.

    Capacity exists for free disk GB and the absolute host-memory MB series. Percentage series
    are handled separately as a fixed 0..100 range. Metrics such as latency, sessions, IOPS,
    and throughput have no physical maximum and deliberately return ``None``.
    """
    kv = _message_kv(message)
    capacity = None
    if code == "STORAGE_DISK_FREE_SPACE":
        capacity = as_float(kv.get("total_gb"))
    elif code == "OS_MEMORY_USAGE" and item == "memory_used_mb":
        capacity = as_float(kv.get("total_mb"))
        if capacity is None:
            total_gb = as_float(kv.get("total_gb"))
            capacity = total_gb * 1024 if total_gb is not None else None
        if capacity is None:
            # Current OS collectors use "Memory used is 28672 MB of 65536 MB."
            match = re.search(r"\bof\s+([0-9]+(?:\.[0-9]+)?)\s*MB\b", str(message or ""), re.I)
            capacity = as_float(match.group(1)) if match else None
    return capacity if capacity is not None and capacity > 0 else None


def _nice_ceiling(value: float) -> float:
    """Round an unbounded series ceiling upward with headroom, never to its observed max."""
    if value <= 0:
        return 1.0
    target = value * 1.10
    magnitude = 10 ** math.floor(math.log10(target))
    normalized = target / magnitude
    for step in (1, 1.25, 1.5, 2, 2.5, 5, 7.5, 10):
        if normalized <= step:
            ceiling = step * magnitude
            break
    else:  # pragma: no cover - the final step always matches
        ceiling = 10 * magnitude
    return round(ceiling, 6)


def _series_scale(code: str, item: str, unit: str, message: str,
                  values: list[float]) -> tuple[float | None, float | None, str, float | None]:
    """Return ``(scale_min, scale_max, kind, capacity)`` for a numeric chart.

    Percentage uses fixed 0..100, capacity uses 0..physical total, and an unbounded metric uses
    a zero-based nice auto-scale with headroom. Observed window statistics remain separate and
    never define the SVG axes.
    """
    if not values:
        return None, None, "none", None
    observed_min, observed_max = min(values), max(values)
    if _is_percent_unit(unit) and observed_min >= 0 and observed_max <= 100:
        return 0.0, 100.0, "fixed", None

    capacity = _series_capacity(code, item, unit, message)
    if capacity is not None and observed_min >= 0 and observed_max <= capacity:
        rounded = round(capacity, 6)
        return 0.0, rounded, "capacity", rounded

    scale_min = 0.0 if observed_min >= 0 else -_nice_ceiling(abs(observed_min))
    scale_max = 0.0 if observed_max <= 0 else _nice_ceiling(observed_max)
    if scale_max <= scale_min:
        scale_max = scale_min + 1.0
    return scale_min, scale_max, "auto", round(capacity, 6) if capacity is not None else None


def _downsample(points: list, limit: int = MAX_POINTS) -> list:
    """Keep at most ``limit`` points by bucket-averaging numeric values (the last status of a
    bucket wins). Straight decimation would drop the spike that made the chart worth looking at;
    averaging keeps the shape and the page small."""
    if len(points) <= limit:
        return points
    bucket = len(points) / limit
    out = []
    for index in range(limit):
        chunk = points[int(index * bucket):int((index + 1) * bucket)] or [points[min(int(index * bucket), len(points) - 1)]]
        values = [p[1] for p in chunk if p[1] is not None]
        out.append([
            chunk[-1][0],
            round(sum(values) / len(values), 2) if values else None,
            chunk[-1][2],
        ])
    return out


def _database_size_rows(rows: list) -> list[dict]:
    """Derive per-database allocated data/log sizes from the existing SQL Server metrics.

    ``STORAGE_DATA_FILE_SPACE`` emits one row per ROWS file and carries ``database`` and
    ``size_mb`` in its message, so MDF/NDF sizes are summed per collection. ``LOG_FILE_SPACE``
    already carries the total allocated LDF size for a database in ``log_size_mb``. These
    synthetic GB series deliberately have status OK: they describe capacity history; the
    source percentage metrics remain responsible for space alerts.

    The derivation happens while reading the worker SQLite history. It therefore provides the
    full retained chart immediately after a report rebuild without adding a new collector or
    waiting for new samples.
    """
    data_files: dict[tuple[str, str], dict[str, float]] = {}
    log_totals: dict[tuple[str, str], float] = {}

    for row in rows:
        code = str(row["metric_code"] or "")
        if code not in {"STORAGE_DATA_FILE_SPACE", "LOG_FILE_SPACE"}:
            continue
        fields = _message_kv(str(row["message"] or ""))
        database = str(fields.get("database") or "").strip()
        collected_at = str(row["collected_at"] or "").strip()
        if not database or not collected_at:
            continue

        if code == "STORAGE_DATA_FILE_SPACE":
            size_mb = as_float(fields.get("size_mb"))
            if size_mb is None or size_mb < 0:
                continue
            # De-duplicate an accidental repeat of the same file in one collection rather than
            # doubling the database. A real additional MDF/NDF has a distinct metric_item.
            file_key = str(row["metric_item"] or "")
            files = data_files.setdefault((database, collected_at), {})
            files[file_key] = max(size_mb, files.get(file_key, 0.0))
        else:
            size_mb = as_float(fields.get("log_size_mb"))
            if size_mb is None or size_mb < 0:
                continue
            # The SQL metric is already SUM(size) across all LDFs. Duplicate rows must not be
            # added together.
            key = (database, collected_at)
            log_totals[key] = max(size_mb, log_totals.get(key, 0.0))

    derived: list[dict] = []
    for (database, collected_at), files in data_files.items():
        size_gb = sum(files.values()) / 1024.0
        derived.append({
            "metric_code": "DATABASE_DATA_SIZE",
            "metric_item": database,
            "metric_value": round(size_gb, 4),
            "metric_unit": "GB",
            "status": "OK",
            "message": "allocated MDF/NDF data size",
            "collected_at": collected_at,
        })
    for (database, collected_at), size_mb in log_totals.items():
        derived.append({
            "metric_code": "DATABASE_LOG_SIZE",
            "metric_item": database,
            "metric_value": round(size_mb / 1024.0, 4),
            "metric_unit": "GB",
            "status": "OK",
            "message": "allocated LDF log size",
            "collected_at": collected_at,
        })
    return derived


def _metric_item_limit(code: str) -> int:
    return MAX_DATABASE_SIZE_ITEMS if code in DATABASE_SIZE_CODES else MAX_ITEMS_PER_METRIC


_METRIC_INTERVALS: dict[str, int] | None = None


def metric_intervals(path: Path | None = None) -> dict[str, int]:
    """``{metric_code: repeat_interval_seconds}`` from the metric catalog, cached.

    Best-effort: a missing or unreadable catalog yields ``{}``, which only means the
    long-cadence exemption below never fires.
    """
    global _METRIC_INTERVALS
    if _METRIC_INTERVALS is not None and path is None:
        return _METRIC_INTERVALS
    source = Path(path or METRIC_DEFINITIONS)
    intervals: dict[str, int] = {}
    try:
        # One reader for metric_definitions.json (common.data_sources) since 2026-08-15. The
        # best-effort contract below is unchanged: a missing catalog still yields {}.
        definitions = data_sources.load_metric_definition_records(source)
        for definition in definitions:
            code = str(definition.get("metric_code") or "")
            every = (definition.get("time_window") or {}).get("repeat_interval")
            if code and isinstance(every, (int, float)) and every > 0:
                intervals[code] = int(every)
    except (OSError, ValueError, AttributeError):
        intervals = {}
    if path is None:
        _METRIC_INTERVALS = intervals
    return intervals


def _is_low_cadence(code: str, *, days: int, intervals: dict[str, int]) -> bool:
    """True when a single sample per item is all this metric can be expected to have.

    Two different reasons produce the same answer, and both must be honoured or the metric
    disappears from the page:

    * **Cadence** — the metric cannot physically produce ``MIN_POINTS`` samples in the window.
    * **Sparse items** — the metric emits a row only while a condition holds, so its items come
      and go regardless of how often it runs. Index fragmentation reports an index only while it
      is above 30%, so an index that crosses the threshold on one night and not the next two
      never accumulates three samples.

    The second reason used to be covered by accident: fragmentation ran weekly, so the cadence
    test caught it. Moving it to a 20-hour interval (so the 3-day report window actually contains
    a run) removed that accident and would have re-opened the original bug — 0 of 42 fragmented
    indexes shown, the worst at 96% — which is why the property is now declared rather than
    inferred. See ``report_policy.sparse_items`` in ``data/metric_definitions.json``.
    """
    if code in sparse_item_metric_codes():
        return True
    every = intervals.get(code)
    return bool(every and every >= (int(days) * 86400) / MIN_POINTS)


_SPARSE_ITEM_CODES: set[str] | None = None


def sparse_item_metric_codes(path: Path | None = None) -> set[str]:
    """Metrics whose items are intermittent by design, from ``report_policy.sparse_items``."""
    global _SPARSE_ITEM_CODES
    if _SPARSE_ITEM_CODES is not None and path is None:
        return _SPARSE_ITEM_CODES
    codes = {
        entry["code"].upper() for entry in _catalog_entries(path)
        if bool((entry["definition"].get("report_policy") or {}).get("sparse_items"))
    }
    if path is None:
        _SPARSE_ITEM_CODES = codes
    return codes


def _catalog_entries(path: Path | None = None) -> list[dict]:
    """``[{code, definition}]`` from the metric catalog. Best-effort: an unreadable catalog
    yields nothing, which only means the policies read from it never apply."""
    try:
        data = json.loads(Path(path or METRIC_DEFINITIONS).read_bytes().decode("utf-8-sig"))
        definitions = data.get("metrics") if isinstance(data, dict) else data
        definitions = definitions if isinstance(definitions, list) else list((definitions or {}).values())
    except (OSError, ValueError, AttributeError):
        return []
    return [{"code": str(item.get("metric_code") or ""), "definition": item}
            for item in definitions if isinstance(item, dict) and item.get("metric_code")]


_METRIC_CATALOG: list[dict] | None = None


def metric_catalog(path: Path | None = None) -> list[dict]:
    """``[{code, active, collector_type, db_types, cadence}]`` from the metric catalog, cached.

    Only what "should this target have this metric?" needs. Best-effort like
    :func:`metric_intervals`: without the catalog the coverage section simply reports nothing
    rather than reporting a wrong expectation.
    """
    global _METRIC_CATALOG
    if _METRIC_CATALOG is not None and path is None:
        return _METRIC_CATALOG
    source = Path(path or METRIC_DEFINITIONS)
    catalog: list[dict] = []
    try:
        definitions = data_sources.load_metric_definition_records(source)
        for definition in definitions:
            code = str(definition.get("metric_code") or "")
            if not code:
                continue
            db_types = {str(variant.get("db_type") or "").lower()
                        for variant in (definition.get("variants") or [])
                        if variant.get("db_type")}
            declared = str(definition.get("db_type") or "").lower()
            if declared and declared != "multi":
                db_types.add(declared)
            catalog.append({
                "code": code,
                "active": bool(definition.get("active", True)),
                "collector_type": str(definition.get("collector_type") or ""),
                "db_types": sorted(db_types),
                "cadence": (definition.get("time_window") or {}).get("repeat_interval"),
            })
    except (OSError, ValueError, AttributeError):
        catalog = []
    if path is None:
        _METRIC_CATALOG = catalog
    return catalog


# A metric is late once this many of its own cadences have passed without an attempt. Three,
# because one missed run is a busy scheduler and two is bad luck; three in a row is the collector
# not reaching this metric. The floor stops a 60-second metric from flapping LATE on clock skew.
LATE_CADENCE_MULTIPLE = 3
LATE_FLOOR_SECONDS = 1800


def build_freshness(rows: list[dict], *, now: int, days: int) -> dict:
    """Per-metric recency and coverage for one server — the answer the page-wide "data age" is not.

    ``server-metrics.html`` reported an overall age near three minutes for 192.0.2.250 while
    ``LOG_RECENT_CRITICAL`` was 48 hours old and ``QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS`` 37
    hours, both on a five-minute cadence. A maximum over every series cannot say that: the
    freshest metric hides every late one behind it, and two metrics that produced no rows at all
    did not make the target look stale in the slightest.

    Each metric therefore reports its own ``last_attempt`` / ``last_success`` / age against its
    configured cadence, plus the error it currently returns. ``notCollected`` lists catalog
    metrics that fit this target's engine but produced nothing in the window — deliberately
    phrased as "no evidence", because a metric can also be switched off for one target through
    ``/spbot_metric_toggle``, and the report cannot see that from the store.
    """
    intervals = metric_intervals()
    seen: dict[str, dict] = {}
    db_types: set[str] = set()
    collector_types: set[str] = set()
    for row in rows:
        code = str(row.get("metric_code") or "")
        if not code:
            continue
        db_types.add(str(row.get("db_type") or "").lower())
        collector_types.add(str(row.get("collector_type") or "").lower())
        last_attempt = str(row.get("last_attempt") or "")
        last_success = str(row.get("last_success") or "")
        attempted_at = _epoch(last_attempt)
        age = None if attempted_at is None else max(0, now - attempted_at)
        cadence = intervals.get(code)
        late_after = max(int(cadence or 0) * LATE_CADENCE_MULTIPLE, LATE_FLOOR_SECONDS)
        if not last_success:
            state = "FAILED"
        elif last_success < last_attempt:
            # The metric still runs; its most recent run did not produce a usable result.
            state = "FAILED"
        elif age is not None and age > late_after:
            state = "LATE"
        else:
            state = "OK"
        seen[code] = {
            "code": code,
            "label": metric_label(code),
            "lastAttempt": last_attempt,
            "lastSuccess": last_success,
            "ageSeconds": age,
            "cadenceSeconds": cadence,
            "lateAfterSeconds": late_after,
            "state": state,
            "status": str(row.get("status") or ""),
            "error": str(row.get("message") or "") if state == "FAILED" else "",
            "rows": int(row.get("rows_in_window") or 0),
        }

    expected = [
        entry["code"] for entry in metric_catalog()
        if entry["active"]
        and (
            (entry["collector_type"] == "sql" and db_types.intersection(entry["db_types"]))
            # A cmd/docker metric applies when this target is collected that way at all; which of
            # them are enabled per target lives in db_instances.json, not in the store.
            or (entry["collector_type"] in ("cmd", "docker")
                and entry["collector_type"] in collector_types)
        )
    ]
    not_collected = sorted(set(expected) - set(seen))
    metrics = sorted(seen.values(), key=lambda entry: (
        {"FAILED": 0, "LATE": 1, "OK": 2}.get(entry["state"], 3), entry["label"]))
    return {
        "metrics": metrics,
        "seen": len(seen),
        "expected": len(set(expected) | set(seen)),
        "notCollected": [{"code": code, "label": metric_label(code)} for code in not_collected],
        "failed": [entry["code"] for entry in metrics if entry["state"] == "FAILED"],
        "late": [entry["code"] for entry in metrics if entry["state"] == "LATE"],
        "windowDays": int(days),
    }


def _worst_items(live: dict) -> list[str]:
    """Item names ordered so the cap keeps the ones worth seeing.

    The cap used to take the first 24 item names alphabetically, which for a list-shaped
    metric is arbitrary: 24 of 100 stale statistics chosen by name, and the index at 96%
    fragmentation kept only if its table sorts early. Order by severity, then by the latest
    value, so whatever the cap drops is the least alarming end of the list.
    """
    def rank(item: str):
        last = live[item][-1]
        severity = SEVERITY_RANK[severity_of(str(last["status"] or ""))]
        value = as_float(last["metric_value"])
        return (-severity, -(value if value is not None else float("-inf")), item.casefold())

    return sorted(live, key=rank)


def _drop_collect_only(rows: list[dict]) -> list[dict]:
    """Remove inventory metrics from a per-server chart series.

    ``fetch_server_series`` deliberately reads every metric for the server, which is right for
    chartable signals and wrong for an inventory: MAINTENANCE_INDEX_USAGE emits one row per index —
    ~29,000 for a single large database — so a 7-day window would pull hundreds of thousands of rows
    into memory to chart nothing, since a per-index inventory has no time series to plot.

    Keyed on ``report_policy.chart_summary_only``, NOT on ``collect_only``: a metric can be
    maintenance work rather than an alert (fragmentation) while still being small enough to chart
    per item. Only the metrics that emit tens of thousands of rows lose their detail here.
    """
    from db_ops.reports.metrics_reports import _chart_summary_only_metric_codes

    codes = _chart_summary_only_metric_codes()
    if not codes:
        return rows
    # Drop the per-index DETAIL, keep the aggregate. The counts (total / disabled / cold /
    # droppable) are exactly what a server report should show for indexes; the ~29k rows behind
    # them are what must never be loaded to draw it. Summary rows carry metric_unit='summary',
    # set in the metric SQL for this purpose.
    return [
        row for row in rows
        if str(row["metric_code"] or "").upper() not in codes
        or str(row["metric_unit"] or "").lower() == "summary"
    ]


def load_server_series(sqlite_path: str | Path, *, server_id: str, days: int,
                       as_of: str | None = None) -> tuple[list[dict], list[dict]]:
    """Chartable series for one server, plus notes about anything deliberately left out."""
    # Read through the store layer rather than opening SQLite here. The window used to be applied
    # with strftime('...','now',?) - the modifier arrived as a *bound parameter*, so no dialect
    # rewrite could have reached it; MetricStore computes the cutoff in Python instead.
    rows = MetricStore(sqlite_path).fetch_server_series(server_id=server_id, days=int(days),
                                                        as_of=as_of)
    rows = _drop_collect_only(rows)

    # Build database-size history from the raw rows before the generic grouping/chart path.
    # Keep the source rows too: their used-percentage charts answer a different question.
    rows = list(rows) + _database_size_rows(rows)

    by_code: dict[str, dict[str, list]] = {}
    newest_at: dict[str, str] = {}
    for row in rows:
        code, item = row["metric_code"], row["metric_item"]
        by_code.setdefault(code, {}).setdefault(item, []).append(row)
        collected = str(row["collected_at"] or "")
        if collected > newest_at.get(code, ""):
            newest_at[code] = collected

    series: list[dict] = []
    omitted: list[dict] = []
    intervals = metric_intervals()
    for code, items in by_code.items():
        item_limit = _metric_item_limit(code)
        low_cadence = _is_low_cadence(code, days=days, intervals=intervals)
        # An OK collector row is "SQL returned no rows" — the condition clearing. Its whole job
        # was setting this metric's newest timestamp above, which is what drops the items it
        # cleared; charting it would add a flat empty series per metric per server.
        items = {item: item_rows for item, item_rows in items.items()
                 if item != health_model.COLLECTOR_ITEM
                 or severity_of(str(item_rows[-1]["status"] or "")) != "OK"}
        live = {
            item: item_rows for item, item_rows in items.items()
            if str(item_rows[-1]["collected_at"] or "") == newest_at.get(code)
            # A collector-*failure* row is exempt from MIN_POINTS. It is not a series and never
            # will be — it is the metric saying it cannot run — and dropping it for having too
            # few samples is how a whole health area went back to reading "not collected"
            # instead of naming the credential that broke it.
            and (len(item_rows) >= MIN_POINTS or low_cadence
                 or item == health_model.COLLECTOR_ITEM)
        }
        dropped = len(items) - len(live)
        shown = min(len(live), item_limit)
        if dropped or len(live) > item_limit:
            omitted.append({
                "code": code,
                "label": metric_label(code),
                "dropped": dropped + max(0, len(live) - item_limit),
                "shown": shown,
            })
        for item in _worst_items(live)[:item_limit]:
            item_rows = live[item]
            points = []
            for row in item_rows:
                epoch = _epoch(row["collected_at"])
                if epoch is None:
                    continue
                points.append([epoch, as_float(row["metric_value"]), str(row["status"] or "")])
            if not points:
                continue
            values = [p[1] for p in points if p[1] is not None]
            numeric = len(values) >= max(2, len(points) // 2)  # a value column that is really a number
            last_row = item_rows[-1]
            texts = {str(row["metric_value"] or "") for row in item_rows}
            observed_min = round(min(values), 2) if values else None
            observed_max = round(max(values), 2) if values else None
            scale_min, scale_max, scale_kind, capacity = _series_scale(
                code, item, str(last_row["metric_unit"] or ""), str(last_row["message"] or ""),
                values if numeric else [],
            )
            entry = {
                "code": code,
                "label": metric_label(code),
                "item": item,
                "unit": str(last_row["metric_unit"] or ""),
                "status": str(last_row["status"] or ""),
                "numeric": numeric,
                # A value that is a word and never changed (ONLINE, RUNNING, NOT_CONFIGURED) has
                # no shape to plot: charting it draws a flat bar that says nothing. It becomes a
                # status card instead. A word that *did* change (Running -> Stopped) is exactly
                # what a strip chart is for, so that one keeps its chart.
                "static": not numeric and len(texts) <= 1,
                "last": as_float(last_row["metric_value"]) if numeric else None,
                "lastText": str(last_row["metric_value"] or ""),
                # The collector already explains itself — "password_age_days=1335 last_set=…
                # (threshold=180d)", "page_count=5645 | action=REBUILD". Dropping it left rows
                # reading `Maintenance statistics age — SALESDB\\X._WA_Sys_0001 · 2026-05-10`,
                # which says nothing about what is wrong or why it matters.
                "message": str(last_row["message"] or ""),
                "lastAt": points[-1][0],
                # min/max are the chart domain. Window statistics remain explicit so the UI
                # never presents a seven-day high as a physical maximum.
                "min": scale_min,
                "max": scale_max,
                "observedMin": observed_min,
                "observedMax": observed_max,
                "scaleKind": scale_kind,
                "capacity": capacity,
                "avg": round(sum(values) / len(values), 2) if values else None,
                "points": _downsample(points),
            }
            entry["tier"] = ("database_size" if code in DATABASE_SIZE_CODES
                             else "primary" if code in PRIMARY_CODES
                             else "diagnostics" if code in DIAGNOSTIC_CODES else "detail")
            entry["lowCadence"] = low_cadence
            series.append(entry)
    return series, omitted


def metric_label(code: str) -> str:
    return METRIC_LABELS.get(code) or code.replace("_", " ").capitalize()


severity_of = health_model.severity_of


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


LINKED_SERVER_CODE = "LINKED_SERVER_STATUS"

#: What to do about one linked server, keyed by (is it usable, does any code call it).
#: The pair is the whole point: neither half is actionable alone. A dead linked server nothing
#: references is cleanup; the same server with a procedure behind it is an outage waiting for
#: that procedure to run. A healthy one nothing references is the opposite question — why is it
#: still configured, with credentials, pointing at a host somebody has to keep alive?
_LINKED_VERDICTS = {
    (False, True): ("FIX", "critical",
                    "Unreachable and code calls it — this fails the next time that code runs."),
    (False, False): ("DROP", "warning",
                     "Unreachable and nothing references it: dead configuration, safe to remove."),
    (True, True): ("KEEP", "ok", "Answers, and code depends on it."),
    (True, False): ("REVIEW", "warning",
                    "Answers, but nothing references it — remove it or find out who uses it "
                    "outside the database."),
}

#: A failure state names the machine to go to, which is the part an operator gets wrong most
#: often. CREDENTIAL_UNREADABLE especially: the remote host is usually fine.
_LINKED_FAILURE_FIX = {
    "CREDENTIAL_UNREADABLE": "LOCAL fix — this instance cannot decrypt the stored remote login "
                             "(service master key changed, or it was restored elsewhere). "
                             "Re-enter the linked server login here; the remote host is likely fine.",
    "LOGIN_REJECTED": "The remote login is rejected (bad, expired, or must-change password). "
                      "Renew the credential on the remote server, then re-enter it here.",
    "UNREACHABLE": "The remote host did not answer — check that it is up, that the instance is "
                   "listening, and that the network/firewall path still exists.",
    "ERROR": "sp_testlinkedserver failed for another reason; the error text is on the row.",
}


def build_linked_servers(rows_in: list[dict]) -> list[dict]:
    """One row per linked server: is it usable, does anything call it, and so what to do.

    Its own section rather than a chart or a status chip. A linked server is not a time series —
    it is reachable or it is not — and the fleet's real question about one is never "what was it
    doing on Tuesday" but "should this still exist". Answering that needs reachability and usage
    on the same row, which is exactly what the metric already reports and what a chip listing
    ``REACHABLE`` could not show.

    Built from the **raw store rows**, like ``backup`` and ``freshness`` and for the same reason:
    the chart pipeline drops anything with fewer than ``MIN_POINTS`` samples, which is right for a
    session id and wrong for this. A linked server that has only been collected twice is not a
    thin series to be discarded — it is a linked server, and the two 2008 R2 hosts whose metric
    had been failing for days had exactly one sample each the moment it was fixed. Passing
    ``series`` here showed 1 linked server across the estate where there are 21.
    """
    rows: list[dict] = []
    for entry in rows_in:
        if str(entry.get("metric_code") or entry.get("code") or "") != LINKED_SERVER_CODE:
            continue
        name = str(entry.get("metric_item") or entry.get("item") or "").strip()
        # A failed collection is stored as a row of this metric with no item. It is a monitoring
        # problem (already reported as one), not a nameless linked server to recommend dropping.
        if not name:
            continue
        fields = _message_kv(entry.get("message", ""))
        usable = str(fields.get("usable", "")).strip().lower() == "yes"
        # The message's own `failure=` decides, not the displayed value: it is the field the SQL
        # sets deliberately (CREDENTIAL_UNREADABLE / LOGIN_REJECTED / UNREACHABLE / ERROR), and it
        # is what picks which machine the operator is sent to. lastText is only a fallback for a
        # row whose message predates that field.
        failure = str(fields.get("failure") or "").strip().upper()
        # The SQL appends a human explanation straight after the state on CREDENTIAL_UNREADABLE
        # ("CREDENTIAL_UNREADABLE (LOCAL problem: ...)"), and _message_kv reads to the next comma,
        # so the whole sentence arrived as the state. Keep the token, drop the prose.
        failure = failure.split("(")[0].split()[0] if failure else ""
        if failure in ("", "NONE"):
            failure = str(entry.get("metric_value") or entry.get("lastText") or "").strip().upper()
        state = "REACHABLE" if usable else (failure or "ERROR")
        procs = _int_or_zero(fields.get("referenced_by_procedures"))
        objects = _int_or_zero(fields.get("referenced_by_objects"))
        # "Referenced" counts every object kind, not only procedures: a view over a four-part
        # name breaks exactly as loudly as a procedure does. The metric's own severity uses the
        # procedure count alone, which is why a view-only reference read as droppable.
        referenced = max(procs, objects) > 0
        verdict, level, why = _LINKED_VERDICTS[(usable, referenced)]
        # CREDENTIAL_UNREADABLE means the test could not be RUN - this instance cannot decrypt the
        # stored remote login - not that the target is dead. Never recommend dropping something
        # that was never actually tested: on 192.0.2.111 one service-master-key problem made
        # all 8 linked servers unusable at once, and five of them read as "safe to remove".
        if not usable and failure == "CREDENTIAL_UNREADABLE":
            verdict = "FIX"
            level = "critical" if referenced else "warning"
            why = ("Not tested: this instance could not decrypt the stored remote login, so "
                   "whether the target answers is unknown. Do not drop it on this evidence.")
        rows.append({
            "name": name,
            "usable": usable,
            "state": state,
            "verdict": verdict,
            "level": level,
            "why": why + ("" if usable else " " + _LINKED_FAILURE_FIX.get(
                state, _LINKED_FAILURE_FIX["ERROR"])),
            "procedures": procs,
            "objects": objects,
            "databases": _int_or_zero(fields.get("databases_referencing")),
            "product": str(fields.get("product") or "").strip() or "?",
            "provider": str(fields.get("provider") or "").strip() or "?",
            "dataSource": str(fields.get("data_source") or "").strip() or "?",
            "sample": str(fields.get("sample") or "").strip(),
            "error": str(fields.get("error") or "").strip(),
            "collectedAt": entry.get("collected_at") or entry.get("lastAt"),
        })
    # Worst first, then the ones that cost the most to keep: a DROP with 40 objects behind it is
    # a bigger decision than a DROP with none.
    order = {"FIX": 0, "DROP": 1, "REVIEW": 2, "KEEP": 3}
    rows.sort(key=lambda r: (order[r["verdict"]], -r["objects"], r["name"].casefold()))
    return rows


def _int_or_zero(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _int_or_none(value) -> int | None:
    """Like :func:`_int_or_zero`, but keeps "not stated" distinct from zero.

    A Windows login has no password age at all; reporting it as 0 days would put it at the top of
    a table sorted by staleness, which is the opposite of true.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


#: Volume free space, in GB — the series a "when does this run out" question is answered from.
CAPACITY_CODE = "STORAGE_DISK_FREE_SPACE"

#: The OS view of the volumes, and the engine's. Both are collected, and each covers a host the
#: other does not: ``OS_DISK_USAGE`` is the only source on a host with no database on it (the
#: Ubuntu worker, the four Service Fabric nodes), and ``STORAGE_DISK_FREE_SPACE`` is the only one
#: on an instance whose ``disabled_collector_types`` turns ``cmd`` off (192.0.2.253).
OS_VOLUME_CODE = "OS_DISK_USAGE"
VOLUME_SECTION_CODES = [OS_VOLUME_CODE, CAPACITY_CODE]

#: ``OS_DISK_USAGE`` items that are storage *activity*, not a volume. They share the metric code
#: with the mount points and would otherwise arrive in the volume table as rows with no size.
_NOT_A_VOLUME = {"disk_read_kbps", "disk_write_kbps", "disk_iops", "disk_queue_length",
                 "disk_perf_counters", "disk_usage"}


def _volume_key(name: str) -> str:
    """``C:\\`` and ``C:`` are one volume; ``/`` and ``/boot`` are two.

    The two collectors spell the same Windows drive differently — ``sys.dm_os_volume_stats``
    returns the mount point ``C:\\`` while ``[System.IO.DriveInfo]`` is trimmed to ``C:`` — so
    without this the table lists every Windows volume twice, once per source. Only a *trailing*
    separator is dropped, and never the whole name: on Linux the root mount **is** ``/``.
    """
    trimmed = str(name or "").strip().rstrip("\\/")
    return trimmed or str(name or "").strip()


def _volume_from_os_row(row: dict) -> dict | None:
    """One mount point as the OS sees it: size, use, filesystem, and the device behind it."""
    item = str(row.get("metric_item") or "").strip()
    if not item or item in _NOT_A_VOLUME:
        return None
    fields = _message_kv(str(row.get("message") or ""))
    total = as_float(fields.get("total_gb"))
    if total is None or total <= 0:
        # No size means this is not a volume row after all (an UNKNOWN placeholder, or an item a
        # future collector adds). Guessing a size here is how a table of capacities gets a row
        # that is not a capacity.
        return None
    free = as_float(fields.get("free_gb"))
    used = as_float(fields.get("used_gb"))
    if used is None and free is not None:
        used = total - free
    if free is None and used is not None:
        free = total - used
    return {
        "name": item,
        "source": "os",
        "status": str(row.get("status") or "OK"),
        "totalGB": round(total, 2),
        "usedGB": round(used, 2) if used is not None else None,
        "freeGB": round(free, 2) if free is not None else None,
        "usedPct": as_float(row.get("metric_value")),
        "freePct": as_float(fields.get("free_percent")),
        "filesystem": str(fields.get("filesystem") or ""),
        # Windows names the volume, Linux names the device behind it. One column, because the
        # question both answer is the same: which physical thing am I looking at.
        "device": str(fields.get("device") or fields.get("label") or ""),
        "collectedAt": str(row.get("collected_at") or ""),
    }


def _volume_from_engine_row(row: dict) -> dict | None:
    """One volume as the database engine sees it — the fallback where the OS metric cannot run.

    The size is allowed to be unknown here, and the row still counts. On a SQL Server too old for
    ``sys.dm_os_volume_stats`` the metric falls back to ``xp_fixeddrives``, which returns free
    megabytes and nothing else (``total_gb=unknown`` — 192.0.2.253 reports all three of its
    volumes that way). Dropping those rows for having no total would leave that host with no
    volume table at all, having thrown away the one column that decides anything: free space.
    """
    fields = _message_kv(str(row.get("message") or ""))
    item = str(row.get("metric_item") or fields.get("drive") or "").strip()
    total = as_float(fields.get("total_gb"))
    if total is not None and total <= 0:
        total = None
    free = as_float(fields.get("free_gb"))
    if free is None:
        free = as_float(row.get("metric_value"))
    if not item or (total is None and free is None):
        return None
    used = total - free if (total is not None and free is not None) else None
    used_pct = as_float(fields.get("used_pct"))
    if used_pct is None and used is not None and total:
        used_pct = used / total * 100
    return {
        "name": item,
        "source": "engine",
        "status": str(row.get("status") or "OK"),
        "totalGB": round(total, 2) if total is not None else None,
        "usedGB": round(used, 2) if used is not None else None,
        "freeGB": round(free, 2) if free is not None else None,
        "usedPct": round(used_pct, 2) if used_pct is not None else None,
        "freePct": as_float(fields.get("free_pct")),
        # The engine reads free space, not the file system it sits on.
        "filesystem": "",
        "device": "",
        "collectedAt": str(row.get("collected_at") or ""),
    }


def build_volumes(rows: list[dict]) -> dict:
    """How large each volume is and how full — the numbers the Disk space tile reduces to one.

    The tile answers "is anything nearly full"; a machine with a 2 TB data volume at 28% and a
    200 GB system volume at 14% got one green tile reading ``28%`` and no way to see either size.
    Every figure here was already collected — ``total_gb``, ``used_gb``, ``free_gb`` ride in the
    OS collector's message on both Windows and Linux — and none of it was on the page: the
    percentage was charted and the capacity it was a percentage *of* was thrown away.

    Both sources are read and merged per volume (see :data:`VOLUME_SECTION_CODES`), OS first
    because it carries the file system and the device. Hosts that have only one of the two are
    the normal case, not the exception.
    """
    volumes: dict[str, dict] = {}
    for parse, code in ((_volume_from_os_row, OS_VOLUME_CODE),
                        (_volume_from_engine_row, CAPACITY_CODE)):
        for row in rows:
            if str(row.get("metric_code") or "") != code:
                continue
            volume = parse(row)
            if volume is None:
                continue
            volumes.setdefault(_volume_key(volume["name"]), volume)

    listed = sorted(volumes.values(),
                    key=lambda v: (-(v["usedPct"] or 0), v["name"].casefold()))
    if not listed:
        return {"volumes": [], "summary": {}}
    # Free space is summed over every volume that reported one — it is the host's actual headroom
    # and is meaningful whether or not the sizes are known. The *ratio* is not: a host where one
    # volume reports its size and two do not has no honest "58% used", so used/total are stated
    # only when nothing is missing, and ``unsized`` says how many were left out.
    unsized = [v for v in listed if v["totalGB"] is None]
    total = sum(v["totalGB"] for v in listed if v["totalGB"] is not None)
    free = sum(v["freeGB"] for v in listed if v["freeGB"] is not None)
    complete = not unsized and total > 0
    return {
        "volumes": listed,
        "summary": {
            "count": len(listed),
            "unsized": len(unsized),
            "freeGB": round(free, 2),
            "totalGB": round(total, 2) if complete else None,
            "usedGB": round(total - free, 2) if complete else None,
            "usedPct": round((total - free) / total * 100, 1) if complete else None,
            "fullest": listed[0]["name"],
            "fullestPct": listed[0]["usedPct"],
            "warning": sum(1 for v in listed if severity_of(v["status"]) == "WARNING"),
            "critical": sum(1 for v in listed if severity_of(v["status"]) == "CRITICAL"),
        },
    }


def build_capacity(rows: list[dict], *, server_id: str = "") -> list[dict]:
    """Per volume: how fast it is draining and when it runs out.

    Reads the raw store rows rather than the charted series for the same reason ``backup`` and
    ``linkedServers`` do — the chart pipeline drops and caps series for display, and a forecast
    wants every sample it can get.

    The analysis itself lives in :mod:`db_ops.lib.capacity_forecast`, so the fleet page and this
    page cannot disagree about which volume is about to fill.
    """
    by_item: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        if str(row.get("metric_code") or "") != CAPACITY_CODE:
            continue
        if str(row.get("metric_unit") or "").strip().upper() != "GB":
            continue
        item = str(row.get("metric_item") or "").strip()
        if not item:
            continue
        by_item.setdefault(item, []).append((row.get("collected_at"), row.get("metric_value")))

    # The read is data_sources' (the one reader of data/); the forecasting is lib's.
    policy = data_sources.load_capacity_policy()
    out: list[dict] = []
    for item, samples in by_item.items():
        reserve = capacity_forecast.reserve_gb(policy, server_id=server_id, item=item)
        result = capacity_forecast.forecast(samples, floor=reserve)
        days = result.get("days_to_threshold")
        out.append({
            "item": item,
            "status": capacity_forecast.severity_for(days, policy, server_id=server_id, item=item),
            "freeGb": result.get("latest"),
            "perDay": result.get("per_day"),
            "daysToFull": days,
            "reserveGb": reserve,
            "points": result.get("points", 0),
            "spanHours": result.get("span_hours", 0),
            "resets": result.get("resets", 0),
            "enough": result.get("status") == "ok",
            "text": capacity_forecast.describe(result),
        })
    # Soonest to run out first; volumes with no date (flat, growing, or unknown) after them.
    out.sort(key=lambda r: (r["daysToFull"] is None, r["daysToFull"] if r["daysToFull"] is not None else 0))
    return out



def build_query_store(rows: list[dict]) -> dict:
    """Per-database Query Store state and settings for this server.

    Its own section, not a status chip, for the reason the access and linked-server tables have
    one: the question an operator brings here is "can I still investigate yesterday's slowdown on
    this database", and the answer is a per-database table with the settings beside it — a chip
    saying WARNING cannot carry it.

    **The state shown is the actual one.** A Query Store that reaches its ``max_storage_size``
    flips itself to READ_ONLY and stops capturing, while ``sys.databases.is_query_store_on`` still
    reads 1: reporting the configured value calls that database covered when it has captured
    nothing since the day it filled up. ``storagePct`` is what predicts the next one.

    Built from the raw store rows like the access and linked-server sections: this is an
    inventory collected once a day, so the chart pipeline drops it for having too few samples.
    """
    databases: list[dict] = []
    for entry in rows:
        if str(entry.get("metric_code") or entry.get("code") or "") != QUERY_STORE_CODE:
            continue
        name = str(entry.get("metric_item") or entry.get("item") or "").strip()
        if not name:
            continue                  # a failed collection is stored itemless; not a database
        detail = inventory_health.build_query_store_entry(entry)
        databases.append({
            "name": name,
            "state": detail.get("state") or "",
            "on": bool(detail.get("on")),
            "capturing": bool(detail.get("capturing")),
            "desiredState": detail.get("desired_state") or "",
            "actualState": detail.get("actual_state") or "",
            "readonlyReason": detail.get("readonly_reason"),
            "readonlyReasonDesc": detail.get("readonly_reason_desc") or "",
            "storageMB": detail.get("current_storage_mb"),
            "maxStorageMB": detail.get("max_storage_mb"),
            "storagePct": detail.get("storage_used_pct"),
            "captureMode": detail.get("capture_mode") or "",
            "cleanupMode": detail.get("cleanup_mode") or "",
            "waitStatsCapture": detail.get("wait_stats_capture") or "",
            "staleQueryThresholdDays": detail.get("stale_query_threshold_days"),
            "intervalLengthMinutes": detail.get("interval_length_minutes"),
            "flushIntervalSeconds": detail.get("flush_interval_seconds"),
            "maxPlansPerQuery": detail.get("max_plans_per_query"),
            "issueType": detail.get("issue_type") or "",
            "offReason": detail.get("off_reason") or "",
            "offReasonDesc": detail.get("off_reason_desc") or "",
            "status": detail.get("status") or "OK",
            "asOf": detail.get("as_of") or "",
        })

    # Not capturing first: the table exists to answer "where can I not investigate", and on an
    # instance with 13 databases that answer must not be somewhere in the middle of the list.
    databases.sort(key=lambda r: (r["capturing"], r["name"].casefold()))
    return {
        "databases": databases,
        "summary": {
            "databases": len(databases),
            "capturing": sum(1 for r in databases if r["capturing"]),
            "off": sum(1 for r in databases if r["state"] == "OFF"),
            # Switched on and still not recording — the failure the configured flag hides.
            "onButNotCapturing": sum(
                1 for r in databases if r["on"] and not r["capturing"]),
            # Not capturing because it broke, rather than because somebody turned it off. The
            # two need different actions, so the header counts them apart.
            "stoppedOnItsOwn": sum(
                1 for r in databases
                if r["offReason"] in ("ERROR_STATE", "SIZE_LIMIT_REACHED", "STOPPED_WHILE_ENABLED")),
            "asOf": max((r["asOf"] for r in databases if r["asOf"]), default=""),
        },
    }


#: What the per-database section reads. The same codes the fleet page's Server Detail uses, so the
#: two tables cannot disagree about the same database — the rows are built by the same function.
DATABASE_SECTION_CODES = [
    "DATABASE_STATUS", "DATABASE_CONFIG", "DATABASE_CHECKDB",
    "DATABASE_DATA_SIZE", "DATABASE_LOG_SIZE", "LOG_FILE_SPACE", "LOG_REUSE_WAIT",
    QUERY_STORE_CODE, "DATABASE_USER_PERMISSIONS",
    "BACKUP_AGE", *backup_policy.BACKUP_LAST_RESULT_CODES,
]


def build_databases(code_map, backup_result=None) -> dict:
    """The instance's databases, one row each.

    The page had every per-database fact — size, log usage, recovery model, CHECKDB age, backup
    ages, Query Store — and no table that put them on one line per database. A reader asking "what
    is on this server and how is each one doing" had to read six sections and join them by eye.

    **System databases are included.** They are the reason this section was asked for: master,
    model and msdb each carried a CHECKDB warning that no row on any page could be attached to,
    because DATABASE_STATUS excludes them and the database list was built from it alone (see
    :data:`inventory_health.SYSTEM_DATABASE_NAMES`).
    """
    rows = inventory_health.build_database_rows(
        inventory_health.build_database_health(code_map),
        inventory_health.build_backup_by_database(code_map, backup_result),
    )
    protected = sum(1 for row in rows if (row.get("backupStatus") or "") == "OK")
    return {
        "databases": rows,
        "summary": {
            "count": len(rows),
            # The same definition the fleet page counts with. Oracle reports its open_mode
            # ("READ WRITE"), never "ONLINE", so a literal comparison here said 0 of 1 online on
            # an instance that was open and serving.
            "online": sum(1 for row in rows if inventory_health.is_database_online(row["state"])),
            # "never" and "" both mean no known-good CHECKDB has ever been recorded.
            "neverCheckdb": sum(1 for row in rows if row["checkdb"] in ("", "never")),
            "backupOk": protected,
            "backupGraded": sum(1 for row in rows if row.get("backupStatus")),
            "asOf": max((row["asOf"] for row in rows if row["asOf"]), default=""),
        },
    }


JOB_INVENTORY_CODE = "SQL_AGENT_JOB_INVENTORY"

#: The keys every engine's job inventory writes, in the order they appear. Used as delimiters:
#: a value ends where the next key of this set begins, which is what lets ``command`` hold the
#: commas and equals signs that a job step is made of.
_JOB_FIELDS = (
    "enabled", "last_outcome", "category", "owner", "schema", "job_class", "steps",
    "schedule", "next_run", "last_run", "max_duration_seconds_7d", "last_duration",
    "runs_7d", "succeeded_7d", "failed_7d", "runs_total", "failed_total",
    "consecutive_failures", "total_runtime_seconds", "broken", "restartable", "command",
)


def _job_fields(message) -> dict[str, str]:
    """``key=value`` fields from a job inventory message, where a value may contain commas.

    Not :func:`_message_kv`: that one ends a value at the first comma, and the field this section
    exists for is ``command`` — ``EXEC dbo.p @a = 1, @b = 2`` would arrive as ``EXEC dbo.p @a = 1``
    and the page would show a job running something it does not run. Here a value runs to the next
    **known key**, so only a job step that literally contains ``, command=`` could confuse it, and
    ``command`` is written last by every variant precisely so nothing follows it to lose.
    """
    text = str(message or "")
    names = "|".join(re.escape(name) for name in _JOB_FIELDS)
    fields: dict[str, str] = {}
    # DOTALL, because a value may span lines: an Oracle DBMS_JOB stores its `what` as the PL/SQL
    # block it was submitted with, newlines and all. Without it the pattern could not reach the
    # end of the value, the whole match failed, and `command` came back **missing** rather than
    # truncated — the field the section exists for, silently absent on the first real 8i job
    # (1.236, run 28790). Whitespace is collapsed so a multi-line block renders as one line.
    for match in re.finditer(rf"(?:^|,\s*)({names})=(.*?)(?=,\s*(?:{names})=|$)", text, re.DOTALL):
        fields[match.group(1)] = " ".join(match.group(2).split())
    return fields


def build_jobs(rows: list[dict]) -> dict:
    """The instance's scheduled jobs: what runs, when, and how it has been going.

    Its own section for the same reason the linked-server and access tables are: a job is not a
    time series. "Which jobs run on this box, what do they actually execute, and which of them has
    been failing" is a question the chart grid cannot answer at all — the metric is collected once
    a night and the chart pipeline drops it for having too few samples, so this reads the store
    rows directly like the other inventory sections.

    **Only enabled jobs are listed.** A disabled job is not a schedule, it is a note; the count of
    them is reported instead, and the inventory report already has a `disabled_jobs` block built
    from the same metric. Keeping them out is what makes this table the answer to "what runs here".

    One table for every engine: SQL Server fills it from msdb, Oracle from dba_jobs (8i) or
    dba_scheduler_jobs (10g+). The engines disagree about what a run counter means and the section
    does not paper over it — SQL Server's counts are the last 7 days, Oracle's scheduler counters
    are for the life of the job, so each row carries the window its numbers belong to.
    """
    jobs: list[dict] = []
    disabled = 0
    for entry in rows:
        if str(entry.get("metric_code") or entry.get("code") or "") != JOB_INVENTORY_CODE:
            continue
        name = str(entry.get("metric_item") or entry.get("item") or "").strip()
        if not name:
            continue                      # a failed collection is stored itemless; not a job
        state = str(entry.get("metric_value") or entry.get("lastText") or "").strip().upper()
        if state == "DISABLED":
            disabled += 1
            continue
        fields = _job_fields(entry.get("message"))
        runs = _int_or_none(fields.get("runs_7d"))
        failed = _int_or_none(fields.get("failed_7d"))
        window = "7d"
        if runs is None and failed is None:
            runs, failed, window = (_int_or_none(fields.get("runs_total")),
                                    _int_or_none(fields.get("failed_total")), "total")
        succeeded = _int_or_none(fields.get("succeeded_7d"))
        if succeeded is None and runs is not None and failed is not None:
            succeeded = max(0, runs - failed)
        jobs.append({
            "name": name,
            "owner": fields.get("owner", "") or "",
            "category": fields.get("category") or fields.get("job_class") or "",
            "lastOutcome": (fields.get("last_outcome", "") or "").upper(),
            "schedule": fields.get("schedule", "") or "",
            "nextRun": fields.get("next_run", "") or "",
            "lastRun": fields.get("last_run", "") or "",
            "command": fields.get("command", "") or "",
            "steps": _int_or_none(fields.get("steps")),
            "runs": runs,
            "succeeded": succeeded,
            "failed": failed,
            "window": window,
            # Oracle stops running a job after 16 consecutive failures; the count is the warning
            # before that happens, and it has no SQL Server equivalent.
            "consecutiveFailures": _int_or_none(fields.get("consecutive_failures")),
            # Three engines, three different things they can say about how long a run takes, and
            # none of them convertible into the others: SQL Server has the worst run of the last
            # 7 days in seconds, Oracle 10g+ has the last run as an INTERVAL, 8i has only a
            # lifetime total. The page shows whichever arrived and labels it, rather than picking
            # one and leaving the other two engines with an empty column.
            "maxDurationSeconds": _int_or_none(fields.get("max_duration_seconds_7d")),
            "lastDuration": fields.get("last_duration", "") or "",
            "totalRuntimeSeconds": _int_or_none(fields.get("total_runtime_seconds")),
            # 8i's schema_user: whose objects the job's PL/SQL resolves against, which is not
            # necessarily who submitted it (owner = log_user). A job that suddenly cannot see a
            # table is usually this column disagreeing with the one beside it.
            "schema": fields.get("schema", "") or "",
            # Oracle only, and only worth a word when it is on: a restartable job is retried after
            # a failure, so its failure count does not mean what it means on a job that is not.
            "restartable": (fields.get("restartable", "") or "").upper() in ("TRUE", "Y", "YES"),
            "asOf": str(entry.get("collected_at") or entry.get("asOf") or ""),
        })
    # Failing first: the table exists to be read top-down when something is wrong.
    jobs.sort(key=lambda job: (-(job["failed"] or 0),
                               0 if job["lastOutcome"] == "FAILED" else 1,
                               job["name"].casefold()))
    return {
        "jobs": jobs,
        "summary": {
            "enabled": len(jobs),
            "disabled": disabled,
            "failing": sum(1 for job in jobs if (job["failed"] or 0) > 0
                           or job["lastOutcome"] == "FAILED"),
            "neverRun": sum(1 for job in jobs if job["lastRun"] in ("", "never")),
            "asOf": max((job["asOf"] for job in jobs if job["asOf"]), default=""),
        },
    }


#: Oracle's storage unit. The tablespace is the object an operator grows, moves and runs out of;
#: the datafile is where that growth physically lands. Nothing else on the page carries either.
TABLESPACE_CODE = "TABLESPACE_FREE_SPACE"
DATAFILE_CODE = "STORAGE_DATA_FILE_SPACE"
TEMP_SPACE_CODE = "STORAGE_TEMP_SPACE"

#: What the storage section reads. ``STORAGE_DATA_FILE_SPACE`` is deliberately included even
#: though every engine writes it: only the Oracle variant names a ``tablespace=``, which is what
#: :func:`build_tablespaces` keys the file list on, so the SQL Server and PostgreSQL rows fall out
#: on their own rather than needing the server's engine to be threaded down here.
TABLESPACE_SECTION_CODES = [TABLESPACE_CODE, DATAFILE_CODE, TEMP_SPACE_CODE]


def _leading_float(value) -> float | None:
    """The number a field starts with, ignoring what the collector appended to it.

    ``effective_free_mb=65418.97 (99.8% of max)`` is one field carrying two facts, and
    :func:`as_float` reads the whole string and returns ``None`` — so every free-space figure on
    the page rendered as an em dash while the store held the number.

    The leading digit is optional because Oracle's ``TO_CHAR`` writes a value below one without
    it: the large pool reported ``free_mb=.59`` and a pattern requiring a digit first read the
    smallest pool on the instance as "not collected".
    """
    match = re.match(r"\s*(-?(?:\d+(?:\.\d+)?|\.\d+))", str(value or ""))
    return float(match.group(1)) if match else None


def _datafiles_by_tablespace(rows: list[dict]) -> dict[str, list[dict]]:
    """The datafile rows of ``STORAGE_DATA_FILE_SPACE``, grouped by the tablespace they belong to.

    Only rows that state a ``tablespace=`` are taken. The same metric code carries three unrelated
    shapes — SQL Server writes ``database=/file=/used_pct=``, PostgreSQL writes ``database=/size=``
    (a database size, not a file at all) — and grouping those under a tablespace heading would
    invent Oracle storage on servers that have none.
    """
    files: dict[str, list[dict]] = {}
    for row in rows:
        if str(row.get("metric_code") or row.get("code") or "") != DATAFILE_CODE:
            continue
        fields = _message_kv(row.get("message"))
        tablespace = str(fields.get("tablespace") or "").strip()
        path = str(fields.get("file") or "").strip()
        if not tablespace or not path:
            continue
        files.setdefault(tablespace, []).append({
            "file": path,
            "sizeMB": _leading_float(fields.get("size_mb")),
            "status": str(row.get("status") or "OK"),
            "asOf": str(row.get("collected_at") or ""),
        })
    for entries in files.values():
        entries.sort(key=lambda entry: (-(entry["sizeMB"] or 0), entry["file"].casefold()))
    return files


def _temp_usage_by_tablespace(rows: list[dict]) -> dict[str, dict]:
    """Sort usage per temporary tablespace, from the Oracle variant of ``STORAGE_TEMP_SPACE``.

    ``max_used_mb`` is the high-water mark, and it is the number ORA-01652 is measured against —
    current usage is near zero between sorts, so reporting only that would call a temp tablespace
    that failed a report last night completely idle.
    """
    usage: dict[str, dict] = {}
    for row in rows:
        if str(row.get("metric_code") or row.get("code") or "") != TEMP_SPACE_CODE:
            continue
        fields = _message_kv(row.get("message"))
        # The Oracle variant writes ``temp tablespace=NAME``; the shared parser keys that on the
        # last word, so ``tablespace`` is the key here. SQL Server's tempdb variant writes no
        # tablespace name at all, which is what keeps tempdb out of this section.
        name = str(fields.get("tablespace") or "").strip()
        if not name:
            continue
        usage[name] = {
            "tempUsedMB": _leading_float(fields.get("used_mb")),
            "tempMaxUsedMB": _leading_float(fields.get("max_used_mb")),
            "tempTotalMB": _leading_float(fields.get("total_mb")),
            "currentSorts": _int_or_none(fields.get("current_sorts")),
            "status": str(row.get("status") or "OK"),
        }
    return usage


def build_tablespaces(rows: list[dict]) -> dict:
    """Oracle storage: one row per tablespace, with its datafiles folded underneath.

    The one thing an Oracle DBA looks up first had no place on this page. Every number was in the
    store — ``TABLESPACE_FREE_SPACE`` has been collecting on 192.0.2.236 since 13 August — and
    all of it rendered as unreadable chart cards: 10 sparklines called "Tablespace free space"
    with the tablespace name only in a tooltip, and the 15 datafiles as 15 more cards saying
    ``15000 MB`` with no indication of which tablespace they extend.

    Three fields decide the row, and each answers a question the others cannot:

    - **Effective free** (``effective_free_mb``) is free space *plus* autoextend headroom — what
      the tablespace can still absorb before ORA-01653. Reporting ``free_now_mb`` alone calls a
      2 GB tablespace with 63 GB of headroom nearly full, which is the reading that gets a
      datafile added that was never needed.
    - **Largest free extent** is what an allocation actually has to fit in. A tablespace can hold
      4 GB of free space in fragments too small for a 100 MB extent and still raise ORA-01653, so
      the number is carried per row rather than derived from the percentage.
    - **Autoextending files vs total files** says whether the headroom is real. Headroom counted
      from files that cannot grow is a promise the database will not keep.

    Built from the raw store rows like ``jobs`` and ``access``: this is an inventory of what
    exists, so only the newest collection counts, and the chart pipeline's series would drop the
    datafile rows for having too few samples anyway.
    """
    files = _datafiles_by_tablespace(rows)
    temp = _temp_usage_by_tablespace(rows)

    tablespaces: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if str(row.get("metric_code") or row.get("code") or "") != TABLESPACE_CODE:
            continue
        name = str(row.get("metric_item") or row.get("item") or "").strip()
        if not name or name in seen:
            continue                  # itemless rows are a failed collection, not a tablespace
        seen.add(name)
        fields = _message_kv(row.get("message"))
        allocated = _leading_float(fields.get("allocated_mb"))
        maximum = _leading_float(fields.get("max_mb"))
        effective_free = _leading_float(fields.get("effective_free_mb"))
        # Percent used against the ceiling the tablespace can reach, not against what is allocated
        # today: on an autoextending file those two differ by the whole headroom, and only the
        # first one predicts the error. The collector states its own "(99.8% of max)" free figure
        # inside the same field; that one is preferred, so the page and the alert cannot round to
        # two different numbers from the same sample.
        stated_free_pct = re.search(r"effective_free_mb=[\d.]+\s*\(([\d.]+)%\s*of max\)",
                                    str(row.get("message") or ""))
        if stated_free_pct:
            used_pct = round(max(0.0, 100.0 - float(stated_free_pct.group(1))), 2)
        elif maximum:
            used_pct = round(max(0.0, (maximum - (effective_free or 0.0)) / maximum * 100.0), 2)
        else:
            used_pct = None
        entry = {
            "name": name,
            "status": str(row.get("status") or "OK"),
            "effectiveFreeMB": effective_free,
            "freeNowMB": _leading_float(fields.get("free_now_mb")),
            "autoextendHeadroomMB": _leading_float(fields.get("autoextend_headroom_mb")),
            "allocatedMB": allocated,
            "maxMB": maximum,
            "usedPct": used_pct,
            "largestFreeExtentMB": _leading_float(fields.get("largest_free_extent_mb")),
            "files": files.get(name, []),
            "fileCount": len(files.get(name, [])),
            "autoextendFiles": None,
            "temp": name in temp,
            "asOf": str(row.get("collected_at") or ""),
        }
        # "datafiles=2 (autoextend=2)" — the count the metric states, which covers files whose own
        # row was capped out of the datafile list.
        declared = re.search(r"datafiles=(\d+)\s*\(autoextend=(\d+)\)", str(row.get("message") or ""))
        if declared:
            entry["declaredFiles"] = int(declared.group(1))
            entry["autoextendFiles"] = int(declared.group(2))
        else:
            entry["declaredFiles"] = entry["fileCount"]
        entry.update(temp.get(name, {}))
        entry["status"] = str(row.get("status") or "OK")
        tablespaces.append(entry)

    # Fullest first: the table is opened to find what is about to run out, and on an instance with
    # 12 tablespaces that answer must not be somewhere in the middle of an alphabetical list.
    tablespaces.sort(key=lambda r: (-SEVERITY_RANK.get(r["status"], 0),
                                    -(r["usedPct"] if r["usedPct"] is not None else -1),
                                    r["name"].casefold()))
    # Datafiles whose tablespace produced no row of its own. Dropping them would hide storage that
    # exists — a tablespace missing from TABLESPACE_FREE_SPACE is itself worth seeing.
    orphan_files = [dict(entry, tablespace=name)
                    for name, entries in sorted(files.items()) if name not in seen
                    for entry in entries]
    return {
        "tablespaces": tablespaces,
        "orphanFiles": orphan_files,
        "summary": {
            "tablespaces": len(tablespaces),
            "datafiles": sum(t["declaredFiles"] for t in tablespaces) + len(orphan_files),
            "temp": sum(1 for t in tablespaces if t["temp"]),
            "warning": sum(1 for t in tablespaces if t["status"] in WARNING_STATUSES),
            "critical": sum(1 for t in tablespaces if t["status"] in CRITICAL_STATUSES),
            # Headroom that only exists on paper: no file in the tablespace can autoextend, so
            # "effective free" is whatever is already allocated and nothing more.
            "noAutoextend": sum(1 for t in tablespaces if t["autoextendFiles"] == 0),
            "totalAllocatedMB": round(sum(t["allocatedMB"] or 0 for t in tablespaces), 2),
            "totalMaxMB": round(sum(t["maxMB"] or 0 for t in tablespaces), 2),
            "asOf": max((t["asOf"] for t in tablespaces if t["asOf"]), default=""),
        },
    }


#: The Oracle-only metrics, grouped by the question they answer. Every one of them was already
#: collecting and had no table to be read in: they rendered as chart cards whose item name lives
#: in a tooltip, which is how a CRITICAL shared pool sat on the page as an unlabelled sparkline.
#:
#: All four builders read the raw store rows, not the charted series. The chart pipeline caps a
#: metric at :data:`MAX_ITEMS_PER_METRIC` items — it dropped 18 of the 28 top-SQL rows — and drops
#: whole metrics for having too few samples, which is right for a trend and wrong for an inventory.
ORACLE_POOL_CODE = "SHARED_POOL_FREE"
ORACLE_LIBRARY_CACHE_CODE = "LIBRARY_CACHE"
ORACLE_BUFFER_CACHE_CODE = "BUFFER_CACHE_HIT"
ORACLE_PROCESS_LIMIT_CODE = "PROCESS_LIMIT"
ORACLE_INSTANCE_CODES = [ORACLE_POOL_CODE, ORACLE_LIBRARY_CACHE_CODE,
                         ORACLE_BUFFER_CACHE_CODE, ORACLE_PROCESS_LIMIT_CODE]

ORACLE_INVALID_OBJECTS_CODE = "INVALID_OBJECTS"
ORACLE_INDEX_UNUSABLE_CODE = "INDEX_UNUSABLE"
ORACLE_EXTENT_LIMIT_CODE = "SEGMENT_EXTENT_LIMIT"
ORACLE_TOP_SEGMENT_CODE = "TOP_SEGMENT_SIZE"
ORACLE_ROLLBACK_CODE = "ROLLBACK_SEGMENT_CONTENTION"
ORACLE_OBJECT_CODES = [ORACLE_INVALID_OBJECTS_CODE, ORACLE_INDEX_UNUSABLE_CODE,
                       ORACLE_EXTENT_LIMIT_CODE, ORACLE_TOP_SEGMENT_CODE, ORACLE_ROLLBACK_CODE]

#: Redo and archiving. On Oracle ``LOG_FILE_SPACE`` is not log-file fullness at all — it carries
#: the archive log mode, the archive destinations and the unarchived redo backlog. The SQL Server
#: variant of the same code is per-database log usage, which the databases table already renders;
#: the two are told apart by the items, not by the engine.
ORACLE_REDO_CODE = "LOG_FILE_SPACE"

ORACLE_TOP_DISK_SQL_CODE = "TOP_DISK_READ_SQL"
ORACLE_TOP_GETS_SQL_CODE = "TOP_BUFFER_GETS_SQL"
ORACLE_TOP_SQL_CODES = [ORACLE_TOP_DISK_SQL_CODE, ORACLE_TOP_GETS_SQL_CODE]

ORACLE_SECTION_CODES = [*ORACLE_INSTANCE_CODES, *ORACLE_OBJECT_CODES,
                        ORACLE_REDO_CODE, *ORACLE_TOP_SQL_CODES]


def _tail_field(message, key: str, *, stop: str = "") -> str:
    """Everything a message says after ``key=``, commas included.

    :func:`_message_kv` ends a value at the first comma, and both the SQL text and the
    invalid-object example list are full of them — ``sql=SELECT a, b FROM t`` arrived as
    ``SELECT a``, which is not the statement anybody would recognise. The collectors write these
    fields last for exactly this reason, so reading to the end of the message (or to ``stop``,
    where a trailing explanation follows) is what recovers them whole.
    """
    text = str(message or "")
    marker = f"{key}="
    at = text.find(marker)
    if at < 0:
        return ""
    tail = text[at + len(marker):]
    if stop:
        cut = tail.find(stop)
        if cut >= 0:
            tail = tail[:cut]
    return tail.strip()


def _rows_of(rows: list[dict], code: str) -> list[dict]:
    """The rows of one metric code, from a mixed bag of store rows."""
    return [row for row in rows
            if str(row.get("metric_code") or row.get("code") or "") == code]


def _item_of(row: dict) -> str:
    return str(row.get("metric_item") or row.get("item") or "").strip()


def build_oracle_instance(rows: list[dict]) -> dict:
    """Oracle instance health: the SGA pools, the caches, and how close the instance is to its
    process and session ceilings.

    These four metrics decide whether the instance can keep taking work, and none of them had a
    row anywhere. ``SHARED_POOL_FREE`` on 192.0.2.236 has been CRITICAL at 9.14 MB free — the
    instance's *only* critical finding — while rendering as one nameless sparkline among thirty.

    Two of the numbers are ratios the collector does not compute, and both are the point of their
    metric: library cache **hit ratio** (``gethits/gets``) says whether SQL is being re-parsed,
    and the **percent of limit** on processes/sessions says how much headroom is left before
    ORA-00020. Reporting reloads and a raw session count instead leaves the reader to divide.
    """
    pools = [{
        "pool": _item_of(row).split(":", 1)[0],
        "name": _message_kv(row.get("message")).get("name") or "",
        "freeMB": _leading_float(_message_kv(row.get("message")).get("free_mb")),
        "status": str(row.get("status") or "OK"),
        "asOf": str(row.get("collected_at") or ""),
    } for row in _rows_of(rows, ORACLE_POOL_CODE) if _item_of(row)]
    pools.sort(key=lambda r: (-SEVERITY_RANK.get(r["status"], 0), r["freeMB"] or 0))

    caches: list[dict] = []
    for row in _rows_of(rows, ORACLE_LIBRARY_CACHE_CODE):
        namespace = _item_of(row)
        if not namespace:
            continue
        fields = _message_kv(row.get("message"))
        gets = _int_or_none(fields.get("gets"))
        gethits = _int_or_none(fields.get("gethits"))
        caches.append({
            "namespace": namespace,
            "gets": gets,
            "getHits": gethits,
            # A namespace nothing asked for has no hit ratio. Folding that to 0% would rank the
            # untouched namespaces as the worst ones on the instance.
            "hitPct": round(gethits / gets * 100.0, 2) if gets else None,
            "reloads": _int_or_none(fields.get("reloads")),
            "invalidations": _int_or_none(fields.get("invalidations")),
            "status": str(row.get("status") or "OK"),
        })
    caches.sort(key=lambda r: (-(r["reloads"] or 0), r["namespace"]))

    buffer_cache = None
    for row in _rows_of(rows, ORACLE_BUFFER_CACHE_CODE):
        fields = _message_kv(row.get("message"))
        buffer_cache = {
            "hitPct": as_float(row.get("metric_value")),
            "dbBlockGets": _int_or_none(fields.get("db_block_gets")),
            "consistentGets": _int_or_none(fields.get("consistent_gets")),
            "physicalReads": _int_or_none(fields.get("physical_reads")),
            "status": str(row.get("status") or "OK"),
        }
        break

    limits: list[dict] = []
    for row in _rows_of(rows, ORACLE_PROCESS_LIMIT_CODE):
        resource = _item_of(row)
        if not resource:
            continue
        fields = _message_kv(row.get("message"))
        current = _int_or_none(fields.get("current"))
        # 8i pads the limit with spaces ("limit=       550"), so the field is a string with
        # leading whitespace rather than a number.
        limit = _int_or_none(str(fields.get("limit") or "").strip())
        peak = _int_or_none(fields.get("max_seen"))
        limits.append({
            "resource": resource,
            "current": current,
            "peak": peak,
            "limit": limit,
            "usedPct": round(current / limit * 100.0, 1) if limit and current is not None else None,
            "peakPct": round(peak / limit * 100.0, 1) if limit and peak is not None else None,
            "status": str(row.get("status") or "OK"),
        })
    limits.sort(key=lambda r: -(r["usedPct"] or 0))

    worst = max((SEVERITY_RANK.get(entry["status"], 0)
                 for entry in [*pools, *caches, *limits] + ([buffer_cache] if buffer_cache else [])),
                default=0)
    return {
        "pools": pools,
        "libraryCache": caches,
        "bufferCache": buffer_cache,
        "limits": limits,
        "summary": {
            "pools": len(pools),
            "worstStatus": next((name for name, rank in SEVERITY_RANK.items() if rank == worst),
                                "OK") if worst else "OK",
            "criticalPools": sum(1 for p in pools if p["status"] in CRITICAL_STATUSES),
            "reloads": sum(c["reloads"] or 0 for c in caches),
            "invalidations": sum(c["invalidations"] or 0 for c in caches),
            "asOf": max((p["asOf"] for p in pools if p["asOf"]), default=""),
        },
    }


def build_oracle_objects(rows: list[dict]) -> dict:
    """Segments and objects: what is broken, what is about to break, and what used the space.

    Four different failures live here and each is invisible to the tablespace numbers above:

    - **INVALID objects** raise ORA-04068 at *call* time, not when they broke. 73 INVALID views
      in LTR is a list of things that will fail on next use, and the page never said so.
    - **UNUSABLE indexes** are not slow indexes, they are absent ones: queries silently full-scan
      and DML raises ORA-01502.
    - **Segments near MAX_EXTENTS** fail with ORA-01631 while the tablespace still shows
      gigabytes free — the one 8i failure a capacity percentage cannot predict.
    - **Rollback segment contention** is the 8i throughput ceiling, and no other metric sees it.

    ``TOP_SEGMENT_SIZE`` is the counterweight: it answers "what used the room" where the
    tablespace table answers "is there room left".
    """
    invalid: list[dict] = []
    for row in _rows_of(rows, ORACLE_INVALID_OBJECTS_CODE):
        fields = _message_kv(row.get("message"))
        owner = str(fields.get("owner") or "").strip()
        if not owner:
            continue
        invalid.append({
            "owner": owner,
            "objectType": str(fields.get("object_type") or "").strip(),
            "count": _int_or_none(fields.get("invalid")),
            # The example list is comma-separated and ends where the explanation begins.
            "examples": [part.strip() for part
                         in _tail_field(row.get("message"), "examples", stop=";").split(",")
                         if part.strip()],
            "status": str(row.get("status") or "OK"),
        })
    invalid.sort(key=lambda r: (-(r["count"] or 0), r["owner"], r["objectType"]))

    unusable = [{
        "index": _item_of(row),
        "status": str(row.get("status") or "CRITICAL"),
        "detail": str(row.get("message") or ""),
        # dba_ind_partitions rows are keyed OWNER.INDEX:PARTITION — the partition half is what
        # says the whole index is not broken, only one partition of it.
        "partition": _item_of(row).split(":", 1)[1] if ":" in _item_of(row) else "",
    } for row in _rows_of(rows, ORACLE_INDEX_UNUSABLE_CODE) if _item_of(row)]
    unusable.sort(key=lambda r: r["index"])

    extents: list[dict] = []
    for row in _rows_of(rows, ORACLE_EXTENT_LIMIT_CODE):
        segment = _item_of(row)
        if not segment:
            continue
        fields = _message_kv(row.get("message"))
        used, _, limit = str(fields.get("extents") or "").partition("/")
        extents.append({
            "segment": segment,
            "type": str(fields.get("type") or "").strip(),
            "tablespace": str(fields.get("tablespace") or "").strip(),
            "extents": _int_or_none(used),
            "maxExtents": _int_or_none(limit.split("(")[0]),
            "usedPct": as_float(row.get("metric_value")),
            "status": str(row.get("status") or "WARNING"),
        })
    extents.sort(key=lambda r: -(r["usedPct"] or 0))

    segments: list[dict] = []
    for row in _rows_of(rows, ORACLE_TOP_SEGMENT_CODE):
        segment = _item_of(row)
        if not segment:
            continue
        fields = _message_kv(row.get("message"))
        segments.append({
            "segment": segment,
            "type": str(fields.get("type") or "").strip(),
            "tablespace": str(fields.get("tablespace") or "").strip(),
            "sizeMB": _leading_float(fields.get("size_mb")),
            "extents": _int_or_none(fields.get("extents")),
        })
    segments.sort(key=lambda r: -(r["sizeMB"] or 0))

    rollback: list[dict] = []
    for row in _rows_of(rows, ORACLE_ROLLBACK_CODE):
        name = _item_of(row)
        if not name:
            continue
        fields = _message_kv(row.get("message"))
        rollback.append({
            "name": name,
            "waits": _int_or_none(fields.get("waits")),
            "gets": _int_or_none(fields.get("gets")),
            "waitPct": as_float(fields.get("wait_ratio_pct")),
            "activeTransactions": _int_or_none(fields.get("active_transactions")),
            "extents": _int_or_none(fields.get("extents")),
            "shrinks": _int_or_none(fields.get("shrinks")),
            "extends": _int_or_none(fields.get("extends")),
            "sizeMB": _leading_float(fields.get("size_mb")),
            "status": str(row.get("status") or "OK"),
        })
    rollback.sort(key=lambda r: (-(r["waitPct"] or 0), r["name"]))

    return {
        "invalidObjects": invalid,
        "unusableIndexes": unusable,
        "extentLimits": extents,
        "topSegments": segments,
        "rollbackSegments": rollback,
        "summary": {
            "invalidObjects": sum(entry["count"] or 0 for entry in invalid),
            "invalidOwners": len({entry["owner"] for entry in invalid}),
            "unusableIndexes": len(unusable),
            "extentLimits": len(extents),
            "topSegments": len(segments),
            "largestSegmentMB": segments[0]["sizeMB"] if segments else None,
            "rollbackSegments": len(rollback),
            # A rollback segment that had to grow back after being shrunk is the contention the
            # wait ratio understates: the work happened, it just did not have to wait for a slot.
            "rollbackResizes": sum((entry["shrinks"] or 0) + (entry["extends"] or 0)
                                   for entry in rollback),
        },
    }


#: The ``LOG_FILE_SPACE`` items only Oracle writes. Anything else under that code is SQL Server's
#: per-database log usage, which the databases table already renders — so the redo section is
#: selected by item, never by engine.
_ORACLE_REDO_ITEMS = ("log_mode", "unarchived_logs", "redo_logs", "fast_recovery_area")


def build_oracle_redo(rows: list[dict]) -> dict:
    """Redo and archiving — whether this instance can be recovered to a point in time at all.

    ``log_mode=NOARCHIVELOG`` means no archiving, so the only restore possible is to the moment of
    the last full backup. That is the single most consequential fact about 192.0.2.236 and no
    page stated it: the value sat inside a ``LOG_FILE_SPACE`` chart card titled "Log file space",
    a name that on every other server means something entirely different.

    The unarchived-log count is reported **with its mode**, because it means opposite things in
    each: a backlog in ARCHIVELOG is archiving falling behind and heading for a frozen instance,
    while in NOARCHIVELOG no group is ever archived and the same number is normal.
    """
    log_mode = ""
    destinations: list[dict] = []
    unarchived = None
    recovery_area = None
    redo_total_mb = None
    status = "OK"
    as_of = ""

    for row in _rows_of(rows, ORACLE_REDO_CODE):
        item = _item_of(row)
        fields = _message_kv(row.get("message"))
        value = str(row.get("metric_value") or "").strip()
        if item.startswith("archive_dest_"):
            destinations.append({
                "id": item.replace("archive_dest_", ""),
                "destination": str(fields.get("destination") or "").strip(),
                "state": value,
                "binding": str(fields.get("binding") or "").strip(),
                "error": str(fields.get("error") or "").strip(),
                "status": str(row.get("status") or "OK"),
            })
        elif item == "log_mode":
            log_mode = value
        elif item == "unarchived_logs":
            unarchived = _int_or_none(value)
        elif item == "redo_logs":
            redo_total_mb = _leading_float(value)
        elif item == "fast_recovery_area":
            recovery_area = {
                "name": str(fields.get("name") or "").strip(),
                "usedPct": as_float(fields.get("used_pct")),
                "reclaimablePct": as_float(fields.get("reclaimable_pct")),
                "unreclaimablePct": as_float(fields.get("unreclaimable_pct")),
                "spaceLimitMB": _leading_float(fields.get("space_limit_mb")),
                "spaceUsedMB": _leading_float(fields.get("space_used_mb")),
                "files": _int_or_none(fields.get("files")),
                "status": str(row.get("status") or "OK"),
            }
        else:
            continue                  # SQL Server's per-database log usage; not this section
        if SEVERITY_RANK.get(str(row.get("status") or "OK"), 0) > SEVERITY_RANK.get(status, 0):
            status = str(row.get("status") or "OK")
        as_of = max(as_of, str(row.get("collected_at") or ""))

    destinations.sort(key=lambda r: r["id"])
    archiving = bool(log_mode) and log_mode.upper() != "NOARCHIVELOG"
    return {
        "logMode": log_mode,
        "archiving": archiving,
        "destinations": destinations,
        "unarchivedLogs": unarchived,
        "recoveryArea": recovery_area,
        "redoTotalMB": redo_total_mb,
        "summary": {
            # The verdict this section exists for. Stated as its own field so the page does not
            # have to re-derive "can this be restored to a point in time" from the mode string.
            "pointInTimeRecovery": archiving,
            "destinations": len(destinations),
            "failedDestinations": sum(1 for entry in destinations
                                      if entry["state"].upper() != "VALID"),
            "status": status,
            "asOf": as_of,
        },
    }


def build_oracle_top_sql(rows: list[dict]) -> dict:
    """The heaviest statements on the instance, by disk reads and by buffer gets.

    Both lists exist because they answer different questions. Disk reads name the statement that
    makes storage the bottleneck; buffer gets name the one burning CPU on logical I/O — and a
    statement can top one list while being absent from the other. On 192.0.2.236 the worst
    buffer-gets statement runs 4.4 million times at 3.5 gets each and does no disk I/O at all,
    which the disk-read list cannot show.

    **Reads per execution is the column to sort a fix by**, not the total: a statement with
    378,431 disk reads over 3 executions is one bad plan, while the same total over 4 million
    executions is a statement doing its job. Both are in the table and the ranking is by total,
    because that is what the instance is actually paying.

    Uncapped, from the raw rows: the chart pipeline's :data:`MAX_ITEMS_PER_METRIC` dropped 18 of
    the 28 statements collected, and a top-SQL list missing two thirds of its entries is worse
    than no list — the reader believes they have seen the worst.
    """
    def statements(code: str, value_key: str) -> list[dict]:
        out: list[dict] = []
        for row in _rows_of(rows, code):
            fields = _message_kv(row.get("message"))
            # sql= is written last by both variants precisely so it can be read to end-of-message:
            # a statement is full of commas and the shared parser stops at the first one.
            text = _tail_field(row.get("message"), "sql")
            out.append({
                "sqlId": _item_of(row),
                "value": as_float(row.get("metric_value")),
                "executions": _int_or_none(fields.get("executions")),
                "perExecution": as_float(fields.get(value_key)),
                "diskReads": _int_or_none(fields.get("disk_reads")),
                "rowsProcessed": _int_or_none(fields.get("rows_processed")),
                "sql": text,
                "status": str(row.get("status") or "OK"),
            })
        out.sort(key=lambda r: -(r["value"] or 0))
        return out

    by_disk = statements(ORACLE_TOP_DISK_SQL_CODE, "reads_per_exec")
    by_gets = statements(ORACLE_TOP_GETS_SQL_CODE, "gets_per_exec")
    return {
        "byDiskReads": by_disk,
        "byBufferGets": by_gets,
        "summary": {
            "byDiskReads": len(by_disk),
            "byBufferGets": len(by_gets),
            "worstDiskReads": by_disk[0]["value"] if by_disk else None,
            "worstBufferGets": by_gets[0]["value"] if by_gets else None,
        },
    }


SERVER_PRINCIPALS_CODE = "SECURITY_SERVER_PRINCIPALS"
DATABASE_USERS_CODE = "DATABASE_USER_PERMISSIONS"

#: Roles and permissions the report marks as privileged. Kept here rather than trusted from the
#: metric's own HIGH_PRIVILEGE marker alone, so the page can still sort a bundle collected by an
#: older metric build that did not set it.
_HIGH_SERVER_ROLES = {"sysadmin", "securityadmin", "serveradmin", "setupadmin"}
_HIGH_DATABASE_ROLES = {"db_owner", "db_securityadmin", "db_accessadmin", "db_ddladmin"}


def _pipe_fields(message) -> dict[str, str]:
    """``key=value`` fields from a message whose segments are separated by ``|`` as well as ``,``.

    Both security metrics write ``login=x | roles=[a,b] | HIGH_PRIVILEGE``. :func:`_message_kv`
    reads a value to the next **comma**, so ``login`` swallowed the rest of the line — and ``roles``
    was then never seen at all, because ``re.findall`` resumes after the previous match and the
    previous match had eaten the string. Splitting on the pipe first makes each segment an ordinary
    comma-delimited fragment the shared parser handles correctly, without changing that parser for
    the dozen metrics that do not use pipes.
    """
    fields: dict[str, str] = {}
    for segment in str(message or "").split("|"):
        fields.update(_message_kv(segment))
    return fields


def _bracket_list(message, key: str) -> list[str]:
    """The members of a ``key=[a,b,c]`` field, read straight off the message.

    Not through :func:`_message_kv`: that parser ends a value at the first comma, which is exactly
    the character separating the members here. ``roles=[db_datareader,db_datawriter,db_ddladmin]``
    arrived as ``[db_datareader``, so a user holding db_ddladmin rendered as a plain reader — the
    one row in that table an operator would stop on, shown as the one kind that is harmless.
    """
    match = re.search(rf"{re.escape(key)}=\[(.*?)\]", str(message or ""))
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def build_access(rows: list[dict]) -> dict:
    """Who can connect to this instance, and what each principal holds.

    Its own section rather than more status chips, for the reason the linked-server table exists:
    a login is not a time series. The question an operator brings to this page is "who has
    sysadmin here" or "which user in this database has no login behind it", and neither is
    answerable from a chip that says OK.

    Two tables from two metrics, because SQL Server keeps the answer in two scopes and the join
    between them is the interesting part. A database user carries the *source* instance's SID
    after a restore, so it resolves to nothing until the login is recreated with that SID: the
    2026-08-10 migration onto 192.0.2.11 landed 13 databases whose 55 users all pointed at
    logins that did not exist, and the page had no way to show it. ``login`` on a user row is
    exactly that mapping — ``<orphaned/none>`` from the metric means the user cannot be reached by
    anyone.

    Built from the **raw store rows** like the linked-server and backup sections: both metrics are
    inventories collected once a night, so the chart pipeline drops them for having too few
    samples.
    """
    logins: list[dict] = []
    users: list[dict] = []
    for entry in rows:
        code = str(entry.get("metric_code") or entry.get("code") or "")
        name = str(entry.get("metric_item") or entry.get("item") or "").strip()
        if not name:
            continue                       # a failed collection is stored itemless; not a principal
        message = str(entry.get("message") or "")
        fields = _pipe_fields(message)

        if code == SERVER_PRINCIPALS_CODE:
            roles = _bracket_list(message, "server_roles")
            perms = _bracket_list(message, "server_perms")
            high = ("HIGH_PRIVILEGE" in message
                    or any(role.lower() in _HIGH_SERVER_ROLES for role in roles))
            logins.append({
                "name": name,
                "type": str(entry.get("metric_value") or entry.get("lastText") or "").strip(),
                "disabled": str(fields.get("disabled", "")).strip().lower() == "yes",
                "defaultDatabase": fields.get("default_db", "") or "",
                "roles": roles,
                "permissions": perms,
                "passwordAgeDays": _int_or_none(fields.get("password_age_days")),
                "checkPolicy": fields.get("check_policy", "") or "",
                "created": fields.get("created", "") or "",
                "high": high,
            })
            continue

        if code == DATABASE_USERS_CODE:
            # metric_item is "<database>\<user>"; a database name may itself contain a backslash,
            # so the split is from the right.
            database, _, user = name.rpartition("\\")
            roles = _bracket_list(message, "roles")
            login = str(fields.get("login", "") or "").strip()
            orphaned = login.lower() in ("<orphaned/none>", "", "-")
            high = ("HIGH_PRIVILEGE" in message
                    or any(role.lower() in _HIGH_DATABASE_ROLES for role in roles))
            users.append({
                "database": database or "(unknown)",
                "name": user or name,
                "type": str(entry.get("metric_value") or entry.get("lastText") or "").strip(),
                "login": "" if orphaned else login,
                "orphaned": orphaned,
                "roles": roles,
                "high": high,
                "status": str(entry.get("status") or "OK").strip().upper(),
                "note": message.split("|")[0].strip() if "guest" in message else "",
            })

    # Worst first in both tables, then alphabetical: a page opened to answer "who has sysadmin"
    # must not need scrolling to find out, and an orphaned user is the thing a restore leaves
    # behind that nobody notices.
    logins.sort(key=lambda r: (not r["high"], r["name"].casefold()))
    users.sort(key=lambda r: (not r["orphaned"], not r["high"],
                              r["database"].casefold(), r["name"].casefold()))
    return {
        "logins": logins,
        "databaseUsers": users,
        "summary": {
            "logins": len(logins),
            "highPrivilegeLogins": sum(1 for r in logins if r["high"]),
            "disabledLogins": sum(1 for r in logins if r["disabled"]),
            "databaseUsers": len(users),
            "databases": len({r["database"] for r in users}),
            "orphanedUsers": sum(1 for r in users if r["orphaned"]),
            "highPrivilegeUsers": sum(1 for r in users if r["high"]),
        },
    }


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


def build_payload(series: list[dict], omitted: list[dict], *, now: int | None = None,
                  backup: dict | None = None, freshness: dict | None = None,
                  linked_rows: list[dict] | None = None,
                  capacity_rows: list[dict] | None = None, server_id: str = "",
                  access_rows: list[dict] | None = None,
                  query_store_rows: list[dict] | None = None,
                  job_rows: list[dict] | None = None,
                  tablespace_rows: list[dict] | None = None,
                  volume_rows: list[dict] | None = None,
                  oracle_rows: list[dict] | None = None,
                  database_code_map: dict | None = None) -> dict:
    """Everything the page renders for one server.

    ``backup`` and ``freshness`` are computed from the raw store rows rather than from ``series``
    (see :func:`load_server_context`): both need rows the chart pipeline deliberately drops — the
    per-database backup evidence is not a time series, and a metric that produced no rows at all
    has no series to be late.
    """
    now = now if now is not None else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    problems = build_problems(series, now=now)
    # Worst first inside every group: with 23 database files a "disk latency" section is 23
    # charts, and the one at 94 ms must not be the twentieth one down the page.
    def worst_first(entry):
        if entry["code"] in DATABASE_SIZE_CODES:
            kind = 0 if entry["code"] == "DATABASE_DATA_SIZE" else 1
            return (-SEVERITY_RANK[series_severity(entry)], 1, entry["item"].casefold(), kind)
        return (-SEVERITY_RANK[series_severity(entry)], 0, entry["label"], entry["item"])

    charts = sorted((entry for entry in series if not entry["static"]), key=worst_first)
    # Linked servers get their own table, so they are kept out of the generic status chips: a
    # chip saying "PRODSRV: REACHABLE" beside a table row that says REACHABLE / KEEP / 14
    # procedures is the same fact twice, and the chip is the useless half.
    cards = sorted((entry for entry in series
                    if entry["static"] and entry["code"] != LINKED_SERVER_CODE), key=worst_first)
    return {
        "health": build_health(series, problems, now=now, freshness=freshness),
        "areas": build_areas(series, backup=backup, freshness=freshness),
        "problems": problems,
        "timeline": build_timeline(series),
        "cards": cards,
        "linkedServers": build_linked_servers(linked_rows or []),
        "capacity": build_capacity(capacity_rows or [], server_id=server_id),
        "volumes": build_volumes(volume_rows or []),
        "access": build_access(access_rows or []),
        "queryStore": build_query_store(query_store_rows or []),
        "databases": build_databases(database_code_map or {}, backup),
        "tablespaces": build_tablespaces(tablespace_rows or []),
        # Four Oracle sections. Each renders nothing when its metrics produced no rows, which is
        # how a SQL Server or PostgreSQL page stays exactly as it was without the builder being
        # told which engine it is looking at.
        "oracleInstance": build_oracle_instance(oracle_rows or []),
        "oracleObjects": build_oracle_objects(oracle_rows or []),
        "oracleRedo": build_oracle_redo(oracle_rows or []),
        "oracleTopSql": build_oracle_top_sql(oracle_rows or []),
        "jobs": build_jobs(job_rows or []),
        "series": charts,
        "omitted": omitted,
        "backup": backup or {},
        "freshness": freshness or {},
    }


def render_page(*, servers: list[dict], company: str, snapshot_date: str, stamp: str,
                days: int, inventory_href: str) -> str:
    """The shared page. It carries only the server index; the series are fetched per server."""
    html = TEMPLATE_HTML.read_text(encoding="utf-8")
    replacements = {
        "__COMPANY__": company,
        "__SNAPSHOT_DATE__": snapshot_date,
        "__WINDOW_DAYS__": str(int(days)),
        "__STAMP__": stamp,
        "__INVENTORY_HREF__": inventory_href,
        "__SERVERS__": json.dumps(servers, ensure_ascii=False, separators=(",", ":")),
    }
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html


def build_server_pages(*, sqlite_path: str | Path, models: list[dict], output_dir: str | Path,
                       stamp: str, snapshot_date: str, days: int, inventory_href: str,
                       as_of: str | None = None,
                       archive_only: bool = False) -> dict[str, str]:
    """Write the shared page plus one series file per server, all with stable names (they are
    overwritten each run, so the report directory does not grow with every build).

    Returns {server_id: href} so the inventory report can link each server row to its charts.
    Keyed by server_id, not by IP: the three PostgreSQL instances share one host, and keying
    on the IP would give all three the same link."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Two fleet-wide queries instead of two per server: both answer questions about rows the
    # chart pipeline drops (per-database backup evidence is not a series; a metric that returned
    # nothing at all has no series to be late), and neither is worth a round trip per server.
    store = MetricStore(sqlite_path)
    freshness_rows = store.fetch_metric_freshness(days=int(days), as_of=as_of)
    backup_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=["BACKUP_LAST_RESULT", "BACKUP_AGE"],
                                          days=int(days), as_of=as_of):
        backup_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Linked servers, same shape and same reason: they are dropped by the chart pipeline, so the
    # section reads the store rows directly. Only the newest collection per server counts — a
    # linked server that was removed last week must not linger in the table.
    linked_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[LINKED_SERVER_CODE], days=int(days), as_of=as_of):
        linked_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Capacity wants the whole history, not the latest snapshot: a slope needs samples.
    capacity_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[CAPACITY_CODE], days=int(days), as_of=as_of):
        capacity_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Logins and database users: inventories, so only the newest collection counts. A login
    # dropped last week must not linger in the table any more than a removed linked server does.
    access_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[SERVER_PRINCIPALS_CODE, DATABASE_USERS_CODE],
                                          days=int(days), as_of=as_of):
        access_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Query Store coverage: a once-a-day inventory of every database, so the newest collection is
    # the whole truth and a database dropped last week must not linger in the table.
    query_store_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[QUERY_STORE_CODE], days=int(days), as_of=as_of):
        query_store_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Scheduled jobs: a nightly inventory, so the newest collection is the whole truth — a job
    # deleted last week must not linger in the table any more than a removed linked server does.
    job_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=[JOB_INVENTORY_CODE], days=int(days), as_of=as_of):
        job_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Oracle tablespaces and their datafiles: an inventory of what exists, so the newest
    # collection is the whole truth — a datafile added this morning must appear, and one dropped
    # last week must not linger.
    tablespace_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=TABLESPACE_SECTION_CODES, days=int(days),
                                          as_of=as_of):
        tablespace_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # Host storage: what volumes exist, how large they are, how full. An inventory of the
    # current state, so the newest collection is the whole truth — a volume unmounted this
    # morning must stop being listed, and a new one must appear.
    volume_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=VOLUME_SECTION_CODES, days=int(days), as_of=as_of):
        volume_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # The Oracle-only sections: pools and limits, segments and objects, redo and archiving, top
    # SQL. All four are inventories of the current state, so the newest collection is the whole
    # truth — an object made valid this morning must stop being listed.
    oracle_rows: dict[str, list[dict]] = {}
    for row in store.fetch_health_metrics(codes=ORACLE_SECTION_CODES, days=int(days), as_of=as_of):
        oracle_rows.setdefault(str(row.get("server_id") or ""), []).append(row)
    # The per-database section. Indexed by the same helper the fleet overlay uses, which reduces
    # each metric to its newest collection per server — a database table must be a snapshot, not
    # a week of samples stacked up (the mistake the fragmentation list made on 2026-08-13).
    database_index = inventory_health.index_by_server(
        store.fetch_health_metrics(codes=DATABASE_SECTION_CODES, days=int(days), as_of=as_of))
    # "Now" is the moment the report describes. Rebuilding 1 August against today's clock would
    # mark every one of that day's collections stale by two days and paint the whole fleet
    # UNKNOWN — a rebuilt past report has to be judged by its own date.
    now = int((metric_store_as_of(as_of) or datetime.datetime.now(datetime.timezone.utc)).timestamp())

    index: list[dict] = []
    links: dict[str, str] = {}
    for model in models:
        server_id = str(model.get("server_id") or "")
        if not server_id:
            continue
        series, omitted = load_server_series(sqlite_path, server_id=server_id, days=days,
                                             as_of=as_of)
        freshness = build_freshness(freshness_rows.get(server_id, []), now=now, days=int(days))
        backup = backup_policy.evaluate_backup_policy(
            health_model.latest_snapshot(backup_rows.get(server_id, [])), server_id=server_id,
            policy=data_sources.load_backup_policy())
        payload = build_payload(series, omitted, now=now, backup=backup, freshness=freshness,
                                linked_rows=health_model.latest_snapshot(
                                    linked_rows.get(server_id, [])),
                                capacity_rows=capacity_rows.get(server_id, []),
                                server_id=server_id,
                                access_rows=health_model.latest_snapshot(
                                    access_rows.get(server_id, [])),
                                query_store_rows=health_model.latest_snapshot(
                                    query_store_rows.get(server_id, [])),
                                job_rows=health_model.latest_snapshot(
                                    job_rows.get(server_id, [])),
                                tablespace_rows=health_model.latest_snapshot(
                                    tablespace_rows.get(server_id, [])),
                                volume_rows=health_model.latest_snapshot(
                                    volume_rows.get(server_id, [])),
                                oracle_rows=health_model.latest_snapshot(
                                    oracle_rows.get(server_id, [])),
                                database_code_map=database_index.get(server_id, [None, {}])[1])
        file_name = series_file_name(server_id)
        # archive_only is a backfill: it produces the dated copy of a past day and must leave the
        # live file alone. Writing the stable name would publish 1 August's data as "now".
        _write(out_dir, file_name,
               json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
               stamp=stamp, archive_only=archive_only)
        index.append({
            "slug": _slug(server_id),
            # server_id, not the role name: role names are not unique. The FCI pair
            # 192.0.2.115 / .113 both report server_name SALESCLUSTER, so the picker had two
            # identical buttons and the page title could not say which node you were reading.
            "name": server_id,
            "role": str(model.get("role") or ""),
            "ip": str(model.get("ip") or ""),
            "platform": str(model.get("platform") or ""),
            # The picker dot is the server's own verdict, not the fleet report's: this page is
            # what the DBA is looking at, so the two must not disagree.
            "status": payload["health"]["status"],
            "score": payload["health"]["score"],
            "file": file_name,
            # Only advertise the page when it is actually on disk. The index report is published
            # by the same workflow, but a server whose index metric has not run yet has no page,
            # and a link to a 404 is worse than no link at all.
            "index_usage_file": (
                index_usage_file_name(server_id)
                if (out_dir / index_usage_file_name(server_id)).exists() else ""
            ),
        })
        links[server_id] = page_href(server_id)

    _write(out_dir, PAGE_NAME,
           render_page(
               servers=index,
               company=str((models[0].get("company") if models else "") or ""),
               snapshot_date=snapshot_date, stamp=stamp, days=days, inventory_href=inventory_href,
           ),
           stamp=stamp, archive_only=archive_only)
    return links


def _write(out_dir: Path, name: str, text: str, *, stamp: str, archive_only: bool) -> None:
    """Publish one file under its live name and its day-stamped one.

    The dated copy is what makes `?date=` reach these pages at all: unlike the fleet inventory
    they keep a stable name and are overwritten every run, so without it there is no history to
    serve (see :mod:`db_ops.lib.report_archive` for why this is daily and not per run).
    """
    dated = out_dir / report_archive.archive_name(stamp, name)
    dated.write_text(text, encoding="utf-8")
    if not archive_only:
        (out_dir / name).write_text(text, encoding="utf-8")
