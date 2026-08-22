"""Build a dated inventory-health snapshot from collected metrics.

The metrics app collects metric rows into SQLite; this report builds the per-server
health blocks (``database_health``, ``disk_health``, ``backup_by_database``,
``sql_agent_job_health``, ``performance_health``, ``config_warnings``) and writes them to
``<YYYYMMDD_HHMMSS>_database-inventory.json`` as an overlay keyed by ``server_id``/``ip``.

The overlay only contains servers that have metrics; applying it to the canonical
``architecture/database-inventory.json`` updates those servers and leaves every other
server (e.g. lab VMs with no metrics) untouched.
"""

from __future__ import annotations
from db_ops.reports.server_report import QUERY_STORE_CODE  # noqa: F401 - one definition, see that module
from db_ops.common import data_sources
from db_ops.lib.coerce import as_float
from db_ops.lib.inventory_render import (  # moved to common: shared with control
    DISK_CRIT_PCT,
    DISK_WARN_PCT,
    _norm_mount,
    is_database_online,  # noqa: F401 - one definition, re-exported for the report builders
    merged_drives,
    merged_sql_resources,
)

import datetime
import json
import re
from pathlib import Path

from db_ops.lib import backup_policy
from db_ops.lib import health_model
from db_ops.db.metric_store import MetricStore

# Metric codes that feed the inventory health blocks.
HEALTH_CODES = [
    # The engine's own identity: product version, patch level (CU/KB), edition, host, collation
    # and start time. Rendered as the "engine build" facts a DBA needs before judging anything
    # else on the page.
    "INSTANCE_STATUS",
    "DATABASE_STATUS", "DATABASE_CONFIG", "DATABASE_CHECKDB",
    # Index health as COUNTS, never as the ~29k per-index rows behind them. Only the aggregate
    # rows (metric_unit='summary') are read here; build_index_health drops the detail.
    "MAINTENANCE_INDEX_USAGE",
    # Fragmentation is maintenance work, not an incident, so it is collect_only like index usage.
    # Counted here so removing it from the alert reports does not make it invisible.
    "MAINTENANCE_INDEX_FRAGMENTATION",
    # Per-database size and log usage: the numbers that make a database row judgeable
    # (a 99%-full log next to an unchanged data file is a broken log-backup chain).
    "DATABASE_DATA_SIZE", "DATABASE_LOG_SIZE", "LOG_FILE_SPACE",
    # Whether each database is recording query history. Per database, because that is the scope
    # of the answer: a reader deciding where yesterday's slowdown can still be investigated needs
    # it on the database row, not as one instance-wide verdict.
    "QUERY_STORE_COVERAGE",
    # Who can get in, and who is trying. A production instance taking ~10k failed logins a day
    # for one principal is a finding no amount of green performance charts makes up for.
    "SECURITY_FAILED_LOGINS", "SECURITY_LOGIN_HEALTH", "SECURITY_CERTIFICATE_EXPIRY",
    "DATABASE_USER_PERMISSIONS",
    "STORAGE_DISK_FREE_SPACE", "STORAGE_FILE_PLACEMENT",
    "BACKUP_AGE", "BACKUP_LAST_RESULT",
    # The PostgreSQL half of BACKUP_LAST_RESULT (same message contract, docker collector — see
    # backup_policy.BACKUP_LAST_RESULT_CODES). Without it loaded here the Backup column read
    # "No metrics" for every PostgreSQL server, because the overlay never saw the rows at all.
    "POSTGRES_BACKUP_LAST_RESULT",
    # Per-job backup outcome + last run time. The canonical inventory carries a static
    # copy of this that is never refreshed, so without the live metric the report showed
    # job run times months out of date next to current backup evidence.
    "BACKUP_JOB_STATUS",
    "JOB_FAILED", "SQL_AGENT_JOB_INVENTORY", "SQL_AGENT_JOB_RUNTIME",
    "SYSTEM_CPU_MEMORY", "PAGE_LIFE_EXPECTANCY", "LOCK_BLOCKING_SESSIONS",
    "LOCK_DEADLOCK_RECENT", "QUERY_LONG_RUNNING", "INSTANCE_CONNECTIONS",
    "STORAGE_TEMP_SPACE", "LOG_REUSE_WAIT", "PERFORMANCE_WAIT_STATS", "SQL_CONFIGURATION",
    # OS metrics. These are the only signals a host with no database has, and they are
    # collected for database hosts too (cmd collectors run on every target with cmd_access).
    "OS_INFO", "OS_CPU_USAGE", "OS_MEMORY_USAGE", "OS_DISK_USAGE", "OS_NETWORK",
    "OS_UPTIME", "OS_SERVICE_STATUS", "OS_PROCESS_TOP_CPU", "OS_PROCESS_TOP_MEMORY",
    "OS_EVENTLOG_CRITICAL", "OS_REBOOT_PENDING",
]


CONFIG_RULES = {
    "cost threshold for parallelism":
        (lambda v: as_float(v) is not None and as_float(v) <= 5,
         "cost threshold for parallelism={v} is low (default 5); consider 30-50 for DW workloads"),
    "backup compression default":
        (lambda v: v == "0", "backup compression default=0; consider enabling backup compression"),
    "optimize for ad hoc workloads":
        (lambda v: v == "0", "optimize for ad hoc workloads=0; consider enabling to reduce single-use plan cache bloat"),
    "remote admin connections":
        (lambda v: v == "0", "remote admin connections=0; consider enabling remote DAC for production"),
    "blocked process threshold (s)":
        (lambda v: v == "0", "blocked process threshold (s)=0; set 30-60 to capture blocking events"),
}

_SELECT_COLS = ("ip, server_id, db_name, metric_code, metric_item, metric_value, "
                "metric_unit, status, message, collected_at")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _to_int(value):
    n = as_float(value)
    return int(n) if n is not None else 0


def _to_int_or_none(value):
    """Like :func:`_to_int`, but keeps "not collected" distinct from zero.

    Used for `database_id`: a target whose metric has not been re-collected since the id was added
    reports nothing, and folding that to 0 would sort every one of its databases ahead of `master`.
    """
    n = as_float(value)
    return int(n) if n is not None else None


def _num_or_str(value):
    n = as_float(value)
    if n is None:
        return value if value not in (None, "") else 0
    return int(n) if n == int(n) else n


def parse_kv(message):
    out = {}
    for part in (message or "").split(","):
        if "=" in part:
            key, val = part.split("=", 1)
            out[key.strip()] = val.strip()
    return out


def parse_kv_any(message):
    """k=v pairs from a message that separates them with ``;`` **or** ``,``.

    The collectors are not consistent about this: DATABASE_STATUS writes
    ``database=X, state=Y`` while INSTANCE_STATUS writes ``server=X; ip=Y; port=Z``. Neither of
    the existing parsers copes with both - :func:`parse_kv` splits on commas only, and
    :func:`parse_os_kv` lets a value run to the next comma, so on a semicolon-separated message it
    would swallow the entire rest of the line as one value.

    Values therefore terminate at either separator. Leading prose ("SQL Server connection is
    available. ") is skipped because only ``key=value`` tokens are matched.
    """
    return {
        key: value.strip()
        for key, value in re.findall(
            r"(?:^|[;,]\s*|\s)([A-Za-z_][A-Za-z0-9_]*)=([^;,]*)", str(message or "")
        )
    }


def parse_os_kv(message):
    """k=v pairs out of an OS collector message.

    OS messages lead with an operator-readable sentence and then carry the structured values
    ("Memory usage is 43.8 percent. total_gb=64, used_gb=28.03"). :func:`parse_kv` splits on
    commas and would take "Memory usage is 43.8 percent. total_gb" as the key, so the pairs
    are matched on the key token instead. Values run to the next comma."""
    return {
        key: value.strip()
        for key, value in re.findall(r"(?:^|[,\s])([A-Za-z_][A-Za-z0-9_]*)=([^,]*)", str(message or ""))
    }


# --------------------------------------------------------------------------- #
# Load + index metrics
# --------------------------------------------------------------------------- #
def load_metrics(store_source, days: int) -> list[dict]:
    """Health-overlay rows, read through :class:`MetricStore`.

    ``store_source`` is anything a store accepts - a config (follows the declared backend), a
    ``StoreTarget``, or a plain SQLite path (which still means SQLite, so existing callers and tests
    keep working). This used to open SQLite directly, which tied the report to one backend.
    """
    return MetricStore(store_source).fetch_health_metrics(codes=list(HEALTH_CODES), days=int(days))


def index_by_server(rows: list[dict]):
    """Return ``{server_id: (ip, {(code, item): row})}`` holding **only the current snapshot**.

    Newest row per ``(code, item)`` first, then everything that is not part of its metric's most
    recent collection is dropped. This overlay is a current-state document — every block it
    builds is rendered as "what is true now" — so an item that did not survive its own metric's
    next run has no business in it. Keeping it is how a rebooted service kept reporting
    NOT_FOUND for a week, an unmounted disk kept its last free %, and a lock row that had already
    cleared kept a production server red (see :mod:`db_ops.lib.health_model`).

    Rows the collector wrote without an item (a failure, or an empty result) stay in the map:
    they are what makes the metric's snapshot timestamp correct, and dropping them is how a
    cleared condition failed to clear.
    """
    servers: dict[str, list] = {}
    for row in rows:  # oldest-first; newer overwrites older
        sid = row.get("server_id") or row.get("ip")
        if not sid:
            continue
        ip, code_map = servers.setdefault(sid, [row.get("ip"), {}])
        if not ip and row.get("ip"):
            servers[sid][0] = row.get("ip")
        code_map[(row["metric_code"], row["metric_item"])] = row
    for sid, (ip, code_map) in servers.items():
        servers[sid] = [ip, current_snapshot(code_map)]
    return servers


def current_snapshot(code_map: dict) -> dict:
    """``code_map`` restricted to each metric's most recent collection."""
    newest = health_model.snapshot_at(code_map.values())
    return {
        key: row for key, row in code_map.items()
        if str(row.get("collected_at") or "") == newest.get(str(row.get("metric_code") or ""), "")
    }


def items_for(code_map, code):
    """Named items of ``code`` in the current snapshot.

    ``index_by_server`` has already reduced ``code_map`` to the latest collection of each metric,
    so this and :func:`latest_items_for` mean the same thing. Both names are kept because the
    distinction used to matter and callers still read either way.
    """
    return [(item, row) for (mc, item), row in code_map.items() if mc == code and item is not None]


def items_for_any(code_map, codes):
    """:func:`items_for` across several metric codes that answer the same question.

    Backup evidence arrives under one code per collector type (SQL for SQL Server and Oracle,
    docker for PostgreSQL) while meaning exactly one thing, so the readers take the set rather
    than a code. Only one code ever produces rows for a given server, so no de-duplication is
    needed — a server is one engine.
    """
    return [pair for code in codes for pair in items_for(code_map, code)]


def latest_items_for(code_map, code):
    """``items_for`` restricted to the most recent collection of that metric.

    Idempotent against a snapshot-reduced ``code_map``, and still correct when a caller builds a
    raw window map itself (the tests do).
    """
    items = items_for(code_map, code)
    if not items:
        return []
    newest = max(str(row.get("collected_at") or "") for _item, row in items)
    return [(item, row) for item, row in items if str(row.get("collected_at") or "") == newest]


def one(code_map, code, item):
    return code_map.get((code, item))


# --------------------------------------------------------------------------- #
# Per-block builders
# --------------------------------------------------------------------------- #
#: Databases that exist on every SQL Server instance and are never dropped. Named here because
#: DATABASE_STATUS excludes them (`database_id > 4`) while other per-database metrics do not, so
#: the report has to know they are legitimately absent from that metric rather than gone.
#: Harmless on other engines, which have no database by these names.
SYSTEM_DATABASE_NAMES = frozenset({"master", "model", "msdb", "tempdb"})


def build_database_health(code_map):
    """Per-database facts an operator judges a database by, all from live metrics.

    Size and log usage live here rather than in the semi-static ``database_sizes`` block so a
    reader gets today's numbers: a log at 99% used with the data file unchanged is the whole
    story of a broken log-backup chain, and a size copied from a manual inventory months ago
    tells it wrong.
    """
    names = sorted({
        item for (mc, item) in code_map
        if mc in ("DATABASE_STATUS", "DATABASE_CONFIG", "DATABASE_CHECKDB",
                  "DATABASE_DATA_SIZE", "DATABASE_LOG_SIZE", "LOG_FILE_SPACE",
                  QUERY_STORE_CODE) and item
    })
    # DATABASE_STATUS is the authority on which databases exist, when it ran: the size metrics are
    # daily and the log-space metric covers only databases with a log to measure, so a database
    # dropped this morning would keep a row on this page until yesterday's size sample aged out.
    #
    # But its authority stops at its own scope. The SQL Server variant selects
    # `WHERE d.database_id > 4`, so it never names master/tempdb/model/msdb — and letting it
    # decide the whole list therefore *deleted* those four from every database table, while
    # DATABASE_CHECKDB went on reporting findings against them. That is how master, model and msdb
    # could each carry a CHECKDB warning with no row anywhere on the page to attach it to.
    # A metric's own scope must never erase another metric's findings.
    current = {item for item, _row in items_for(code_map, "DATABASE_STATUS")}
    if current:
        # System databases are exempt from the drop-detection above for the reason it exists at
        # all: nobody drops master. Keeping them costs nothing and is the only way a finding
        # about one is visible.
        names = sorted(current | {name for name in names if name in SYSTEM_DATABASE_NAMES})
    users = build_database_users(code_map)
    out = []
    for db in names:
        status_row = one(code_map, "DATABASE_STATUS", db)
        cfg_row = one(code_map, "DATABASE_CONFIG", db)
        chk_row = one(code_map, "DATABASE_CHECKDB", db)
        data_row = one(code_map, "DATABASE_DATA_SIZE", db)
        log_row = one(code_map, "DATABASE_LOG_SIZE", db)
        logspace_row = one(code_map, "LOG_FILE_SPACE", db)
        reuse_row = one(code_map, "LOG_REUSE_WAIT", db)
        qs_row = one(code_map, QUERY_STORE_CODE, db)
        cfg_kv = parse_kv(cfg_row["message"]) if cfg_row else {}
        status_kv = parse_kv(status_row["message"]) if status_row else {}
        query_store = build_query_store_entry(qs_row)
        out.append({
            "database_name": db,
            # The order every database list is rendered in. Read from DATABASE_CONFIG first
            # because that metric has no `database_id > 4` filter, so it is the only one carrying
            # an id for master/tempdb/model/msdb — the rows whose position this mostly decides.
            "database_id": _to_int_or_none(
                cfg_kv.get("database_id") or status_kv.get("database_id")),
            "state": (status_row["metric_value"] if status_row else cfg_kv.get("state", "")) or "",
            "recovery_model": (cfg_row["metric_value"] if cfg_row else "") or "",
            # DATABASE_CONFIG is SQL-Server-only, so on any other engine this column was blank.
            # DATABASE_STATUS now carries the same fact for every engine that has one -
            # 'compatibility_level' on SQL Server, 'compatible' on Oracle (its COMPATIBLE
            # parameter) - so fall back to it rather than leaving the column empty.
            "compatibility_level": _num_or_str(
                cfg_kv.get("compatibility_level")
                or status_kv.get("compatibility_level")
                or status_kv.get("compatible")
                or ""
            ),
            "is_read_only": _to_int(status_kv.get("read_only", cfg_kv.get("is_read_only", "0"))),
            "page_verify_option": cfg_kv.get("page_verify", ""),
            "last_good_checkdb": (chk_row["metric_value"] if chk_row else "") or "",
            "data_size_gb": as_float(data_row["metric_value"]) if data_row else None,
            "log_size_gb": as_float(log_row["metric_value"]) if log_row else None,
            "log_used_percent": as_float(logspace_row["metric_value"]) if logspace_row else None,
            # Why the log cannot truncate — LOG_BACKUP here plus a stale log backup age is the
            # broken-chain diagnosis in one row.
            "log_reuse_wait": (reuse_row["metric_value"] if reuse_row else "") or "",
            "user_count": (users.get(db) or {}).get("users"),
            "high_privilege_users": (users.get(db) or {}).get("high_privilege"),
            "db_owners": (users.get(db) or {}).get("owners") or [],
            # Whether this database is capturing query history at all. A reader deciding where a
            # slowdown can still be investigated needs it per database, and until the collector
            # reported the healthy ones too there was nothing here to show.
            "query_store": query_store,
            "as_of": _latest_collected(status_row, cfg_row, chk_row, data_row, log_row,
                                       logspace_row, qs_row),
        })
    return out



#: ``readonly_reason`` is a bit mask; these are the bits worth naming to a reader. 65536 is the
#: one that matters in practice — Query Store hit its size limit and stopped capturing by itself.
QUERY_STORE_READONLY_REASONS = (
    (1, "database is read-only"),
    (2, "database is in single-user mode"),
    (4, "database is in emergency mode"),
    (8, "database is a secondary replica"),
    (65536, "storage size limit reached"),
    (131072, "internal error"),
)


def describe_query_store_readonly_reason(value) -> str:
    """The bit mask spelled out, e.g. ``storage size limit reached``.

    A page that prints ``readonly_reason=65536`` has told the reader nothing they can act on,
    and that number is exactly the case they most need to act on.
    """
    try:
        mask = int(value)
    except (TypeError, ValueError):
        return ""
    if mask <= 0:
        return ""
    reasons = [text for bit, text in QUERY_STORE_READONLY_REASONS if mask & bit == bit]
    return ", ".join(reasons) or f"reason bits {mask}"


def build_query_store_entry(row) -> dict:
    """One database's Query Store state and settings, from its ``QUERY_STORE_COVERAGE`` row.

    ``state`` is the *actual* state, not the configured one: a Query Store that filled its
    max_storage_size flips itself to READ_ONLY and stops capturing while
    ``sys.databases.is_query_store_on`` still reads 1. Reporting the configured value would call
    that database covered when it has captured nothing since the day it filled up.

    An empty dict when the metric has not run for this database — the caller renders that as
    "unknown", which is honestly different from "off".
    """
    if not row:
        return {}
    fields = parse_kv(str(row.get("message") or ""))
    state = str(row.get("metric_value") or "").strip().upper()
    return {
        "state": state,
        "on": state in ("READ_WRITE", "READ_ONLY"),
        "capturing": state == "READ_WRITE",
        "desired_state": fields.get("desired_state", ""),
        "actual_state": fields.get("actual_state", ""),
        "readonly_reason": as_float(fields.get("readonly_reason")),
        "readonly_reason_desc": describe_query_store_readonly_reason(fields.get("readonly_reason")),
        "current_storage_mb": as_float(fields.get("current_storage_mb")),
        "max_storage_mb": as_float(fields.get("max_storage_mb")),
        "storage_used_pct": as_float(fields.get("storage_used_pct")),
        "capture_mode": fields.get("capture_mode", ""),
        "cleanup_mode": fields.get("cleanup_mode", ""),
        "wait_stats_capture": fields.get("wait_stats_capture", ""),
        "stale_query_threshold_days": as_float(fields.get("stale_query_threshold_days")),
        "interval_length_minutes": as_float(fields.get("interval_length_minutes")),
        "flush_interval_seconds": as_float(fields.get("flush_interval_seconds")),
        "max_plans_per_query": as_float(fields.get("max_plans_per_query")),
        "issue_type": fields.get("issue_type", ""),
        # WHY it is not capturing. "off" alone cannot be acted on: somebody switching Query Store
        # off is a decision, while Query Store switching itself off after an error is a fault with
        # captured data still sitting in the database. SALESDB on 192.0.2.115 is the second kind
        # and read as the first for as long as this field did not exist.
        "off_reason": fields.get("off_reason", ""),
        "off_reason_desc": QUERY_STORE_OFF_REASONS.get(fields.get("off_reason", ""), ""),
        "status": str(row.get("status") or "").strip().upper(),
        "as_of": str(row.get("collected_at") or ""),
    }


#: ``off_reason`` from the collector, in words. The distinction that matters is the first two:
#: one is somebody's decision, the other is Query Store having stopped on its own.
QUERY_STORE_OFF_REASONS = {
    "TURNED_OFF": "switched off",
    "ERROR_STATE": "stopped after an error",
    "SIZE_LIMIT_REACHED": "storage size limit reached",
    "STOPPED_WHILE_ENABLED": "stopped while still enabled",
    "DATABASE_NOT_ONLINE": "database is not online",
    "AG_SECONDARY": "readable only on the primary replica",
    "UNKNOWN": "reason not reported",
}


def _latest_collected(*rows) -> str:
    stamps = [str(r.get("collected_at") or "") for r in rows if r]
    return max(stamps) if stamps else ""


def build_database_users(code_map) -> dict[str, dict]:
    """``{database: {users, high_privilege, owners}}`` from ``DATABASE_USER_PERMISSIONS``.

    Items are ``<database>\\<principal>`` and the message carries the login, its roles and a
    HIGH_PRIVILEGE marker. Counting them per database answers the question a database row
    could not: *who can get into this one, and how many of them own it.*
    """
    out: dict[str, dict] = {}
    for item, row in items_for(code_map, "DATABASE_USER_PERMISSIONS"):
        db, _, principal = str(item).partition("\\")
        if not db or not principal:
            continue
        entry = out.setdefault(db, {"users": 0, "high_privilege": 0, "owners": []})
        entry["users"] += 1
        message = str(row.get("message") or "")
        if "HIGH_PRIVILEGE" in message:
            entry["high_privilege"] += 1
        if "db_owner" in message:
            entry["owners"].append(principal)
    for entry in out.values():
        entry["owners"] = sorted(entry["owners"])
    return out


def build_security_health(code_map) -> dict:
    """Failed logins, password age and certificate expiry — the instance's security posture.

    Collected for a while, reported nowhere: neither the fleet report nor the per-server page
    had a security section, so ~10k failed logins a day against one login was invisible on
    both. Thresholds stay the collector's (they are in its message); this only shapes them.
    """
    failed_total = None
    principals: list[dict] = []
    for item, row in items_for(code_map, "SECURITY_FAILED_LOGINS"):
        value = as_float(row["metric_value"])
        if str(item).startswith("failed_logins"):
            failed_total = value
            continue
        login = str(item).partition("\\")[2] or str(item)
        principals.append({"login": login, "attempts": value,
                           "status": (row.get("status") or "OK").upper()})

    passwords: list[dict] = []
    summary_kv: dict[str, str] = {}
    for item, row in items_for(code_map, "SECURITY_LOGIN_HEALTH"):
        kv = parse_kv(row["message"])
        if str(item).startswith("login_health"):
            summary_kv = kv
            continue
        if str(item).startswith("password_old"):
            passwords.append({
                "login": str(item).partition("\\")[2] or str(item),
                "age_days": _to_int(kv.get("password_age_days")) or as_float(row["metric_value"]),
                "last_set": kv.get("last_set", ""),
                "status": (row.get("status") or "OK").upper(),
            })
        elif failed_total is None and str(item).startswith("failed_login"):
            principals.append({"login": str(item).partition("\\")[2] or str(item),
                               "attempts": as_float(row["metric_value"]),
                               "status": (row.get("status") or "OK").upper()})

    cert_kv = {}
    for _item, row in items_for(code_map, "SECURITY_CERTIFICATE_EXPIRY"):
        cert_kv = parse_kv(row["message"])

    principals.sort(key=lambda p: -(p["attempts"] or 0))
    passwords.sort(key=lambda p: -(p["age_days"] or 0))
    rows = [row for (mc, _i), row in code_map.items()
            if mc in ("SECURITY_FAILED_LOGINS", "SECURITY_LOGIN_HEALTH", "SECURITY_CERTIFICATE_EXPIRY")]
    return {
        "failed_logins_24h": failed_total,
        "failed_login_principals": principals[:10],
        "passwords_over_threshold": passwords[:20],
        "password_threshold_days": _to_int((summary_kv or {}).get("threshold")) or 180,
        "dormant_logins": _to_int((summary_kv or {}).get("dormant")),
        "certificates_expired": _to_int(cert_kv.get("expired")),
        "certificates_expiring_30d": _to_int(cert_kv.get("expiring_30d")),
        "as_of": _latest_collected(*rows),
    }


def build_disk_health(code_map):
    drives = {}
    for drive, row in items_for(code_map, "STORAGE_DISK_FREE_SPACE"):
        kv = parse_kv(row["message"])
        free_gb = as_float(row["metric_value"])
        total_gb = as_float(kv.get("total_gb"))
        free_pct = round(100.0 * free_gb / total_gb, 2) if (free_gb is not None and total_gb) else None
        if free_pct is None:
            status = row["status"] or "UNKNOWN"
        elif free_pct < DISK_CRIT_PCT:
            status = "CRITICAL"
        elif free_pct < DISK_WARN_PCT:
            status = "WARNING"
        else:
            status = "OK"
        drives[drive] = {"total_gb": total_gb, "free_gb": free_gb,
                         "free_percent": free_pct, "status": status}

    data_drives, log_drives, tempdb_drives, backup_paths = set(), set(), set(), set()
    rows_by_db, log_by_db = {}, {}
    for item, row in items_for(code_map, "STORAGE_FILE_PLACEMENT"):
        drive = row["metric_value"]
        if item.startswith("BACKUP |"):
            if row["message"]:
                backup_paths.add(row["message"])
            continue
        db, _, ftype = item.partition(" | ")
        if db == "tempdb":
            tempdb_drives.add(drive)
        if ftype == "ROWS":
            data_drives.add(drive)
            rows_by_db.setdefault(db, set()).add(drive)
        elif ftype == "LOG":
            log_drives.add(drive)
            log_by_db.setdefault(db, set()).add(drive)

    colocated = any(
        rows_by_db.get(db) and log_by_db.get(db) and (rows_by_db[db] & log_by_db[db])
        for db in set(rows_by_db) | set(log_by_db)
    )
    return {
        "drives": drives,
        "data_file_drives": sorted(data_drives),
        "log_file_drives": sorted(log_drives),
        "tempdb_drives": sorted(tempdb_drives),
        "backup_path": sorted(backup_paths),
        "log_on_same_disk_as_data": colocated if (rows_by_db and log_by_db) else None,
    }


def build_backup_policy(code_map, server_id: str = ""):
    """Per-database compliance against ``data/backup_policy.json``.

    See :mod:`db_ops.lib.backup_policy`. This is the block both the fleet Backup column and
    the per-server Backup tile read; neither derives its own verdict from raw backup rows any
    more, which is how one healthy database used to answer for a whole instance.
    """
    rows = [row for (mc, _item), row in code_map.items()
            if mc in (*backup_policy.BACKUP_LAST_RESULT_CODES, "BACKUP_AGE")]
    return backup_policy.evaluate_backup_policy(
        rows, server_id=server_id, policy=data_sources.load_backup_policy())


def build_backup_by_database(code_map, policy_result=None):
    """Per-database backup ages, one row per database and one column per backup type.

    The age of **each** type is kept, not just the newest backup of any type: a daily FULL with a
    log backup 121 days behind is an RPO violation, and a single "last backup" number reports that
    as healthy. ``status`` is the policy verdict, not the old ``NO_FULL_BACKUP``-or-``OK`` pair
    that could only ever say one thing about a database with a 124-day-old log backup.
    """
    by_database = {record["database"]: record
                   for record in (policy_result or {}).get("databases", [])}
    out = []
    age_by_db = {item: row for item, row in items_for(code_map, "BACKUP_AGE")}
    last_by_db = {}
    for item, row in items_for_any(code_map, backup_policy.BACKUP_LAST_RESULT_CODES):
        db = (item.split(" / ")[0]).strip()
        last_by_db.setdefault(db, []).append(row)
    for db in sorted(set(age_by_db) | set(last_by_db)):
        finish = {"FULL": None, "DIFF": None, "LOG": None}
        ages = {"FULL": None, "DIFF": None, "LOG": None}
        recovery = ""
        for row in last_by_db.get(db, []):
            kv = parse_kv(row["message"])
            recovery = kv.get("recovery_model", recovery)
            btype = _BACKUP_TYPE_MAP.get(str(kv.get("backup_type", "")).upper())
            if not btype:
                continue
            when = kv.get("backup_finish_date")
            if when in (None, "NULL"):
                when = None
            finish[btype] = when or finish[btype]
            # metric_value of BACKUP_LAST_RESULT is hours_since_last_backup for that type.
            age = as_float(row["metric_value"])
            if age is not None:
                ages[btype] = age
        age_row = age_by_db.get(db)
        verdict = by_database.get(db) or {}
        types = verdict.get("types") or {}
        out.append({
            "database_name": db,
            "recovery_model": recovery or verdict.get("recovery_model") or "",
            "last_full_backup": finish["FULL"] or "",
            "last_diff_backup": finish["DIFF"] or "",
            "last_log_backup": finish["LOG"] or "",
            "full_age_hours": ages["FULL"] if ages["FULL"] is not None else (
                as_float(age_row["metric_value"]) if age_row else None),
            "diff_age_hours": ages["DIFF"],
            "log_age_hours": ages["LOG"],
            "status": verdict.get("status") or (
                "NO_FULL_BACKUP" if (age_row is None and not finish["FULL"]) else "OK"),
            "reason": verdict.get("reason") or "",
            # Which types this database is actually required to have. A blank DIFF column on a
            # database whose policy does not require one is not a gap, and the page must be able
            # to tell the reader that instead of leaving them to guess.
            "required_types": sorted(name for name, slot in types.items() if slot.get("required")),
            "as_of": _latest_collected(age_row, *last_by_db.get(db, [])),
        })
    return out


_BACKUP_TYPE_MAP = {"D": "FULL", "DATABASE": "FULL", "FULL": "FULL",
                    "I": "DIFF", "DIFF": "DIFF", "DIFFERENTIAL": "DIFF",
                    "L": "LOG", "LOG": "LOG"}


def build_database_rows(health_block, backup_block, *, static_sizes=None) -> list[dict]:
    """One row per database, joining :func:`build_database_health` with
    :func:`build_backup_by_database`: what the database is, how big, and how well protected.

    Shared because two reports render this same table — the fleet page's Server Detail and the
    per-server page's Databases section — and a second copy of the join is a second set of
    columns that can disagree about the same database.

    ``static_sizes`` (``{name: size_mb}``) is the fleet report's fallback to the semi-static
    canonical inventory for a size the live metrics did not carry; the per-server page has no such
    file and passes nothing. A row filled from it is marked ``staticSize`` so a stale number is
    visible as stale instead of passing for today's.
    """
    health = {str(d.get("database_name")): d for d in (health_block or [])}
    backup = {str(b.get("database_name")): b for b in (backup_block or [])}
    static_sizes = static_sizes or {}

    def in_catalog_order(name):
        """`database_id` order — master, tempdb, model, msdb, then user databases as created.

        The order a DBA reads a database list in, and the order every other tool shows it in;
        alphabetical put `SALESDB` above `master` and scattered the system four through the middle.

        A database whose id has not been collected yet sorts *after* the ones that have, by name.
        That is the honest place for it: a target that has not re-run the metric since the id was
        added should not have its databases interleaved with another's on a guessed key.
        """
        database_id = (health.get(name) or {}).get("database_id")
        return (database_id is None, database_id if database_id is not None else 0, name)

    rows = []
    for name in sorted(set(health) | set(backup), key=in_catalog_order):
        h, b = health.get(name, {}), backup.get(name, {})
        data_gb = as_float(h.get("data_size_gb"))
        static_size = False
        if data_gb is None and static_sizes.get(name) is not None:
            data_gb = round(static_sizes[name] / 1024.0, 2)
            static_size = True
        rows.append({
            "name": name,
            "databaseId": h.get("database_id"),
            "state": h.get("state") or "",
            "recovery": h.get("recovery_model") or b.get("recovery_model") or "",
            "dataGB": data_gb,
            "logGB": as_float(h.get("log_size_gb")),
            "logUsedPct": as_float(h.get("log_used_percent")),
            "logReuseWait": h.get("log_reuse_wait") or "",
            "compat": h.get("compatibility_level"),
            "pageVerify": h.get("page_verify_option") or "",
            "checkdb": h.get("last_good_checkdb") or "",
            "users": h.get("user_count"),
            "highPrivUsers": h.get("high_privilege_users"),
            # Query Store per database: whether a slowdown on this one can still be investigated
            # afterwards. `state` is the ACTUAL state — a Query Store that filled its size limit
            # reads as on while capturing nothing, which is the case worth seeing here.
            "queryStore": (h.get("query_store") or {}).get("state") or "",
            "queryStoreCapturing": (h.get("query_store") or {}).get("capturing"),
            "queryStoreDetail": h.get("query_store") or {},
            "owners": list(h.get("db_owners") or []),
            "fullAgeHours": as_float(b.get("full_age_hours")),
            "diffAgeHours": as_float(b.get("diff_age_hours")),
            "logAgeHours": as_float(b.get("log_age_hours")),
            "backupStatus": b.get("status") or "",
            "backupReason": b.get("reason") or "",
            "requiredTypes": b.get("required_types"),
            "staticSize": static_size,
            "asOf": h.get("as_of") or b.get("as_of") or "",
            # A composite row is only as current as its *oldest* input. Stamping it with the
            # newest is how a daily 21 GB allocated-log sample sat beside a 15-minute 99.77%
            # log-used value and read as one measurement.
            "oldestAsOf": min([x for x in (h.get("as_of"), b.get("as_of")) if x], default=""),
        })
    return rows


def build_backup_evidence(code_map, policy_result=None):
    """Server-level latest FULL/DIFF/LOG backup evidence from ``BACKUP_LAST_RESULT`` (the
    freshest collected backup, including DIFF). Stored as a health block so the report's
    Backup posture reflects live metrics instead of the semi-static ``backup.latest_by_type``
    carried in the canonical inventory.

    ``latest_status`` used to be the literal string ``"OK"`` for every type, whatever the age —
    which is how a page could print a 3185-hour backup age beside a green status. It now carries
    the policy's verdict for that type across the databases that require it, and
    ``database_count`` is joined by ``required``/``compliant`` so "Full+Diff+Log" can no longer be
    claimed from evidence covering one database out of six.
    """
    agg: dict[str, dict] = {}
    for _item, row in items_for_any(code_map, backup_policy.BACKUP_LAST_RESULT_CODES):
        kv = parse_kv(row["message"])
        finish = kv.get("backup_finish_date")
        if finish in (None, "", "NULL"):
            continue
        btype = _BACKUP_TYPE_MAP.get((kv.get("backup_type") or "").strip().upper())
        if not btype:
            continue
        entry = agg.setdefault(btype, {"latest_finish": None, "dbs": set()})
        if entry["latest_finish"] is None or finish > entry["latest_finish"]:
            entry["latest_finish"] = finish
        if kv.get("database"):
            entry["dbs"].add(kv["database"])
    now = datetime.datetime.now()
    by_type = ((policy_result or {}).get("summary") or {}).get("byType") or {}
    out = {}
    for btype, entry in agg.items():
        finish = entry["latest_finish"]
        try:
            age_hours = round((now - datetime.datetime.fromisoformat(finish)).total_seconds() / 3600, 1)
        except (TypeError, ValueError):
            age_hours = None
        policy_state = (by_type.get(btype) or {}).get("state") or ""
        out[btype] = {
            "latest_finish": finish,
            "latest_status": {"OK": "OK", "VIOLATED": "CRITICAL",
                              "NOT_REQUIRED": "NOT_REQUIRED"}.get(policy_state, "UNKNOWN"),
            "latest_age_hours": age_hours,
            "database_count": len(entry["dbs"]),
            "required_databases": (by_type.get(btype) or {}).get("required"),
            "compliant_databases": (by_type.get(btype) or {}).get("compliant"),
        }
    return out


_SQLCFG_KEYS = {
    "max degree of parallelism": ("sql_cpu", "max_degree_of_parallelism"),
    "cost threshold for parallelism": ("sql_cpu", "cost_threshold_for_parallelism"),
    "max server memory (MB)": ("memory", "max_server_memory_mb"),
    "min server memory (MB)": ("memory", "min_server_memory_mb"),
    "backup compression default": ("important_config", "backup_compression_default"),
    "optimize for ad hoc workloads": ("important_config", "optimize_for_ad_hoc_workloads"),
    "remote admin connections": ("important_config", "remote_admin_connections"),
    "blocked process threshold (s)": ("important_config", "blocked_process_threshold_s"),
    "clr enabled": ("important_config", "clr_enabled"),
    "xp_cmdshell": ("important_config", "xp_cmdshell"),
    "max worker threads": ("important_config", "max_worker_threads"),
}


def build_sql_governance(code_map):
    """Fresh SQL governance from live metrics: ``SQL_CONFIGURATION`` (sp_configure: MAXDOP,
    cost threshold, max/min server memory, config flags), ``SYSTEM_CPU_MEMORY`` (host RAM +
    SQL memory in use), and ``STORAGE_TEMP_SPACE`` (tempdb size). Stored as a health block so
    the report's governance reflects live metrics, not the semi-static ``sqlserver_resources``.
    Fields with no metric (visible-CPU/scheduler counts, tempdb file count, database sizes)
    are intentionally absent here and keep coming from ``sqlserver_resources``."""
    out: dict = {"sql_cpu": {}, "memory": {}, "important_config": {}, "tempdb": {}}
    seen = False
    for item, row in items_for(code_map, "SQL_CONFIGURATION"):
        mapping = _SQLCFG_KEYS.get((item or "").strip())
        if not mapping:
            continue
        seen = True
        out[mapping[0]][mapping[1]] = _num_or_str(row["metric_value"])
    for item in ("sql_memory", "system_memory"):
        row = one(code_map, "SYSTEM_CPU_MEMORY", item)
        if not row:
            continue
        kv = parse_kv(row["message"])
        total_mb = as_float(kv.get("total_physical_memory_mb"))
        if total_mb and not out["memory"].get("host_physical_memory_gb"):
            out["memory"]["host_physical_memory_gb"] = round(total_mb / 1024, 2)
        if item == "sql_memory":
            in_use = as_float(kv.get("sql_physical_memory_in_use_mb"))
            if in_use is not None:
                out["memory"]["sql_committed_mb"] = in_use
                seen = True
    tdb = one(code_map, "STORAGE_TEMP_SPACE", "tempdb")
    if tdb:
        total_mb = as_float(parse_kv(tdb["message"]).get("total_mb"))
        if total_mb is not None:
            out["tempdb"]["total_mb"] = total_mb
            seen = True
    return out if seen else {}


def build_backup_jobs(code_map):
    """Per-job backup outcome and last run time, from BACKUP_JOB_STATUS.

    The same facts the canonical inventory carries under ``backup.jobs`` - but collected, so the
    times are current. The static copy is only a fallback now (see inventory_report._build_detail).
    """
    jobs = {}
    for name, row in items_for(code_map, "BACKUP_JOB_STATUS"):
        kv = parse_kv(row.get("message"))
        last_run = kv.get("last_run_datetime", "")
        # One row per run lands in the window; keep the most recent per job.
        if name in jobs and jobs[name]["last_run"] >= last_run:
            continue
        jobs[name] = {
            "job": name,
            "last_status": kv.get("last_status") or row.get("metric_value") or "",
            "last_run": last_run,
            "status": row.get("status") or "",
            "as_of": row.get("collected_at") or "",
        }
    return [jobs[name] for name in sorted(jobs)]


def build_sql_agent_job_health(code_map):
    failed = one(code_map, "JOB_FAILED", "sql_agent")
    disabled = [item for item, row in items_for(code_map, "SQL_AGENT_JOB_INVENTORY")
                if (row["metric_value"] or "") == "DISABLED"]
    long_running = [item for item, row in items_for(code_map, "SQL_AGENT_JOB_RUNTIME")
                    if (row["metric_value"] or "").startswith("RUNNING")]
    return {
        "failed_jobs_24h": _num_or_str(failed["metric_value"]) if failed else 0,
        "disabled_jobs": sorted(disabled),
        "long_running_jobs": sorted(long_running),
    }


def build_index_health(code_map):
    """Index counts for one instance, from the MAINTENANCE_INDEX_USAGE aggregate rows.

    Only rows with ``metric_unit = 'summary'`` are read. The same metric also emits one row per
    index — around 29,000 for a single large database — and pulling those into an inventory page
    would be both unreadable and pointlessly expensive. The counts answer what the page is for:
    how much of this instance's index footprint is dead weight.

    ``droppable`` is deliberately narrower than ``cold``: nonclustered, not unique, not a primary
    key or unique constraint, and never read. Everything excluded from it is excluded because
    dropping it would break something, not because it is in use.
    """
    per_db = []
    totals = {"indexes_total": 0, "used": 0, "unused": 0, "cold": 0,
              "disabled": 0, "disabled_clustered": 0, "droppable": 0}
    for item, row in items_for(code_map, "MAINTENANCE_INDEX_USAGE"):
        if str(row["metric_unit"] or "").lower() != "summary":
            continue
        fields = parse_kv(row["message"] or "")
        counts = {k: _to_int(fields.get(k)) or 0 for k in totals}
        # The instance-wide row is the one without a db= field; the rest are per database.
        if not fields.get("db"):
            totals.update(counts)
            continue
        entry = {"database": fields.get("db") or item}
        entry.update(counts)
        per_db.append(entry)

    # The instance-wide row is one row among thousands; if it is missing, unparsed, or lost to a
    # row cap, the per-database rows still carry the same information. Summing them is not a
    # fallback nicety - it is what keeps the totals honest when the single row that stated them
    # is the one thing that went missing.
    if not any(totals.values()) and per_db:
        for key in totals:
            totals[key] = sum(entry.get(key, 0) for entry in per_db)

    # Fragmented indexes: this metric emits one row per index needing a REBUILD and has no
    # aggregate row of its own, so the count is taken here. Rows carrying a collection error
    # rather than a percentage are not fragmentation findings and must not inflate it.
    fragmented = 0
    for _item, row in items_for(code_map, "MAINTENANCE_INDEX_FRAGMENTATION"):
        message = str(row["message"] or "")
        if "action=REBUILD" in message or "action=REORGANIZE" in message:
            fragmented += 1
    if fragmented:
        totals["fragmented"] = fragmented

    if not per_db and not any(totals.values()):
        return {}
    # Worst first: a page that leads with the databases carrying the most dead indexes.
    per_db.sort(key=lambda e: (e.get("droppable", 0), e.get("cold", 0)), reverse=True)
    return {"totals": totals, "databases": per_db[:20]}


def _blocking(code_map) -> dict:
    """Blocked sessions across the whole instance, not only the literal item ``server``.

    ``009_sqlserver_blocking_sessions.sql`` emits ``server = 0`` **only when nothing is blocked**;
    the moment something is, the rows are keyed by database instead. Reading the ``server`` item
    alone therefore reported zero exactly when the answer was not zero: the fleet page said
    ``blocking_count = 0`` for 192.0.2.115 while its own page showed 42 blocked sessions in
    SALESDB. Summing the current snapshot's items is the count; the collector's worst status is the
    severity (it already weighs both session count and wait duration).
    """
    rows = items_for(code_map, "LOCK_BLOCKING_SESSIONS")
    by_database = {}
    total = 0.0
    max_wait = None
    worst = "OK"
    for item, row in rows:
        count = as_float(row.get("metric_value")) or 0
        if item != "server" or count:
            by_database[item] = _num_or_str(count)
        total += count
        wait = as_float(parse_kv(row.get("message")).get("max_wait_seconds"))
        if wait is not None and (max_wait is None or wait > max_wait):
            max_wait = wait
        state = str(row.get("status") or "").upper()
        if state in ("CRITICAL", "ERROR"):
            worst = "CRITICAL"
        elif state == "WARNING" and worst != "CRITICAL":
            worst = "WARNING"
    return {
        "count": _num_or_str(total),
        "status": worst if rows else "",
        "by_database": dict(sorted(by_database.items(), key=lambda kv: -(as_float(kv[1]) or 0))),
        "max_wait_seconds": max_wait,
    }


def build_performance_health(code_map):
    def val(code, item):
        row = one(code_map, code, item)
        return row["metric_value"] if row else None

    def status(code, item):
        row = one(code_map, code, item)
        return row["status"] if row else ""

    log_waits = sorted({
        row["metric_value"] for _, row in items_for(code_map, "LOG_REUSE_WAIT")
        if row["metric_value"] and row["metric_value"] != "NOTHING"
    })
    top_waits = [{"wait_type": item, "wait_seconds": as_float(row["metric_value"])}
                 for item, row in items_for(code_map, "PERFORMANCE_WAIT_STATS")][:5]
    conn = one(code_map, "INSTANCE_CONNECTIONS", "server")
    conn_kv = parse_kv(conn["message"]) if conn else {}
    blocking = _blocking(code_map)
    return {
        "cpu_status": status("SYSTEM_CPU_MEMORY", "cpu"),
        "memory_status": status("SYSTEM_CPU_MEMORY", "memory"),
        "page_life_expectancy": _num_or_str(val("PAGE_LIFE_EXPECTANCY", "page_life_expectancy") or 0),
        "blocking_count": blocking["count"],
        "blocking_status": blocking["status"],
        "blocking_by_database": blocking["by_database"],
        "blocking_max_wait_seconds": blocking["max_wait_seconds"],
        "deadlock_count_24h": _num_or_str(val("LOCK_DEADLOCK_RECENT", "deadlock") or 0),
        "long_running_query_count": _num_or_str(val("QUERY_LONG_RUNNING", "server") or 0),
        "active_session_count": _num_or_str(conn_kv.get("active_sessions", val("INSTANCE_CONNECTIONS", "server") or 0)),
        "tempdb_usage_status": status("STORAGE_TEMP_SPACE", "tempdb"),
        "log_reuse_wait": ", ".join(log_waits),
        "top_waits": top_waits,
    }


# SQL Server major version -> the database compatibility level that engine ships as native.
# A database below its engine's native level runs on an older query optimizer and cannot use
# features introduced since, which is usually an upgrade nobody finished rather than a decision.
SQLSERVER_NATIVE_COMPAT = {
    "10": 100,   # 2008 / 2008 R2
    "11": 110,   # 2012
    "12": 120,   # 2014
    "13": 130,   # 2016
    "14": 140,   # 2017
    "15": 150,   # 2019
    "16": 160,   # 2022
    "17": 170,   # 2025
}


def build_config_warnings(code_map):
    warnings = []
    for name, row in items_for(code_map, "SQL_CONFIGURATION"):
        rule = CONFIG_RULES.get(name)
        if rule and rule[0](row["metric_value"]):
            warnings.append(rule[1].format(v=row["metric_value"]))
    warnings.extend(_compat_level_warnings(code_map))
    return sorted(warnings)


def _compat_level_warnings(code_map):
    """Databases left below their engine's native compatibility level.

    Both halves are already collected - the engine build from INSTANCE_STATUS, the per-database
    level from DATABASE_STATUS/DATABASE_CONFIG - but nothing compared them, so a database still on
    compatibility level 80 (SQL Server 2000 semantics) under a 2008 R2 engine looked entirely
    healthy on the report. It is flagged as one warning per level rather than per database, because
    the finding is "these were never raised after an upgrade", not a per-database fault.
    """
    instance = build_instance_health(code_map)
    version = instance.get("product_version") or ""
    native = SQLSERVER_NATIVE_COMPAT.get(version.split(".")[0])
    if not native:
        return []

    behind = {}
    for _name, row in items_for(code_map, "DATABASE_STATUS"):
        kv = parse_kv(row.get("message"))
        level = kv.get("compatibility_level")
        if level and str(level).isdigit() and int(level) < native:
            behind.setdefault(int(level), []).append(row.get("metric_item") or "")

    return [
        f"compatibility level {level} is below the engine native {native} "
        f"({len(names)} database{'s' if len(names) != 1 else ''}: "
        f"{', '.join(sorted(n for n in names if n)[:6])}"
        f"{', ...' if len(names) > 6 else ''})"
        for level, names in sorted(behind.items())
    ]


def build_inventory_status(code_map):
    """Auto-derive the ``inventory_status`` block from collected metrics, replacing the old
    hand-maintained value for any server that has metrics (servers without metrics keep their
    manual baseline because the overlay never includes them). ``last_checked`` follows the
    real collection time instead of a frozen date."""
    rows = list(code_map.values())
    times = sorted(r["collected_at"] for r in rows if r.get("collected_at"))
    last_checked = times[-1][:10] if times else ""

    db_rows = [row for (mc, _item), row in code_map.items()
               if mc in ("DATABASE_STATUS", "DATABASE_CONFIG", "DATABASE_CHECKDB")]
    db_ok = any((r.get("status") or "").upper() not in ("ERROR", "UNKNOWN") for r in db_rows)
    has_os = any(mc in ("STORAGE_DISK_FREE_SPACE", "OS_DISK_USAGE", "OS_INFO") for (mc, _item) in code_map)
    has_sql_res = any(mc in ("SYSTEM_CPU_MEMORY", "SQL_CONFIGURATION") for (mc, _item) in code_map)
    # No database rows at all (not even an error row) means no database collector ran against
    # this host: it has no database, so "no db metadata" is the expected state, not a gap.
    db_metadata = "ok" if db_ok else ("failed" if db_rows else ("not_applicable" if has_os else "no_metrics"))

    status = {
        "last_checked": last_checked,
        "source": "db_ops metrics (auto)",
        "db_metadata": db_metadata,
        "os_resources": "collected_remote" if has_os else "not_collected",
        "sql_resource_governance": "ok" if has_sql_res else "not_collected",
        "metric_rows": len(rows),
    }
    if not db_ok:
        err = next((r.get("message") for r in db_rows
                    if (r.get("status") or "").upper() in ("ERROR", "UNKNOWN") and r.get("message")), "")
        if err:
            status["reason"] = str(err)[:240]
    return status


def build_instance_health(code_map):
    """Engine build/identity facts from INSTANCE_STATUS.

    ``001_sqlserver_instance_status.sql`` reports the patch level in two parts that are easy to
    confuse: ``level`` is the service-pack-era ProductLevel ("RTM"), ``CU`` is
    ProductUpdateLevel ("CU26") and ``update`` is the KB article ProductUpdateReference
    ("KB5093420"). All three are kept because "RTM" alone reads as unpatched when the instance is
    in fact 26 cumulative updates in.

    Engine-neutral on purpose: the same keys are populated from whatever an engine's
    INSTANCE_STATUS message provides, so Oracle/PostgreSQL rows fill in what they have and leave
    the rest blank rather than inventing it.
    """
    rows = latest_items_for(code_map, "INSTANCE_STATUS")
    if not rows:
        return {}
    # One instance per server: take the most recently collected row.
    item, row = max(rows, key=lambda pair: str(pair[1].get("collected_at") or ""))
    kv = parse_kv_any(row.get("message"))

    build = " ".join(part for part in (kv.get("version"), kv.get("level")) if part)
    patch = " ".join(part for part in (kv.get("CU"), kv.get("update")) if part and part != "N/A")
    return {
        "instance": kv.get("instance") or item or "",
        "status": row.get("metric_value") or "",
        "state": row.get("status") or "",
        "product_version": kv.get("version", ""),
        "product_level": kv.get("level", ""),
        # ProductUpdateLevel, e.g. "CU26" - the number that actually says how patched this is.
        "cumulative_update": _blank_na(kv.get("CU")),
        # ProductUpdateReference, e.g. "KB5093420".
        "update_reference": _blank_na(kv.get("update")),
        "build": build,
        "patch": patch,
        "edition": kv.get("edition", ""),
        "engine_edition": kv.get("engine", ""),
        "machine_name": kv.get("machine", ""),
        "physical_name": _blank_na(kv.get("physical")),
        "clustered": kv.get("clustered", ""),
        "collation": kv.get("collation", ""),
        "started_at": kv.get("started", ""),
        "listen_ip": _blank_na(kv.get("ip")),
        "listen_port": _blank_na(kv.get("port")),
        "auth_scheme": _blank_na(kv.get("auth")),
        "transport": _blank_na(kv.get("transport")),
        "process_id": kv.get("pid", ""),
        "as_of": row.get("collected_at") or "",
    }


def _blank_na(value):
    """The collectors write 'N/A' where a property is unavailable; the report shows nothing."""
    text = str(value or "").strip()
    return "" if text.upper() in ("N/A", "NULL", "NONE") else text


def build_os_health(code_map):
    """Host-level health from the OS (cmd) collectors.

    This is what a host with no database has instead of ``database_health`` /
    ``performance_health`` / ``backup_evidence``, and it is collected for database hosts as
    well. Values that the collectors put in the message as ``k=v`` pairs (memory totals,
    CPU topology, disk sizes) are lifted out here so the report never re-parses messages.
    """
    info = {}
    for item, row in latest_items_for(code_map, "OS_INFO"):
        kv = parse_os_kv(row["message"])
        if item == "os_name":
            info.update({
                "os_family": kv.get("os_family", ""),
                "os_name": row["metric_value"] or "",
                "edition": kv.get("edition", ""),
                "version": kv.get("version", ""),
                "build": kv.get("build", ""),
                "architecture": kv.get("architecture", ""),
            })
        elif item == "hostname":
            info["hostname"] = row["metric_value"] or ""
        elif item == "timezone":
            info["timezone"] = row["metric_value"] or ""
        elif item == "last_boot_time":
            info["last_boot_time"] = row["metric_value"] or ""
            info["uptime_seconds"] = _to_int(kv.get("uptime_seconds"))

    uptime_row = one(code_map, "OS_UPTIME", "uptime")
    if uptime_row and not info.get("uptime_seconds"):
        info["uptime_seconds"] = _to_int(uptime_row["metric_value"])

    cpu = {}
    cpu_row = one(code_map, "OS_CPU_USAGE", "cpu_usage")
    if cpu_row:
        kv = parse_os_kv(cpu_row["message"])
        cpu = {
            "usage_percent": as_float(cpu_row["metric_value"]),
            "status": cpu_row["status"] or "",
            "model": kv.get("model", ""),
            "sockets": _to_int(kv.get("sockets")),
            "cores": _to_int(kv.get("cores")),
            "logical_cpus": _to_int(kv.get("logical_cpus")),
        }
    load_row = one(code_map, "OS_CPU_USAGE", "load_average")
    if load_row:
        cpu["load_average_1m"] = as_float(load_row["metric_value"])
    queue_row = one(code_map, "OS_CPU_USAGE", "processor_queue_length")
    if queue_row:
        cpu["processor_queue_length"] = as_float(queue_row["metric_value"])

    memory = {}
    memory_row = one(code_map, "OS_MEMORY_USAGE", "memory_usage")
    if memory_row:
        kv = parse_os_kv(memory_row["message"])
        memory = {
            "usage_percent": as_float(memory_row["metric_value"]),
            "status": memory_row["status"] or "",
            "total_gb": as_float(kv.get("total_gb")),
            "used_gb": as_float(kv.get("used_gb")),
            "available_gb": as_float(kv.get("available_gb")),
        }
    for swap_item in ("swap_usage", "pagefile_usage"):
        swap_row = one(code_map, "OS_MEMORY_USAGE", swap_item)
        if swap_row:
            kv = parse_os_kv(swap_row["message"])
            memory.update({
                "swap_usage_percent": as_float(swap_row["metric_value"]),
                "swap_total_gb": as_float(kv.get("swap_total_gb")),
                "swap_used_gb": as_float(kv.get("swap_used_gb")),
            })

    # OS_DISK_USAGE carries the host's IO rates alongside its drives: they are rows of the same
    # metric, not mount points. Charting them is right; listing them as drives is not.
    disks = {}
    for mount, row in latest_items_for(code_map, "OS_DISK_USAGE"):
        if mount in ("disk_queue_length", "disk_iops", "disk_usage",
                     "disk_read_kbps", "disk_write_kbps"):
            continue
        kv = parse_os_kv(row["message"])
        free_pct = as_float(kv.get("free_percent"))
        disks[mount] = {
            "total_gb": as_float(kv.get("total_gb")),
            "free_gb": as_float(kv.get("free_gb")),
            "free_percent": free_pct,
            "status": row["status"] or "UNKNOWN",
            "file_system_type": kv.get("filesystem", ""),
            "logical_volume_name": kv.get("label") or kv.get("device") or "",
        }

    # OS_NETWORK also carries "<iface> send" / "<iface> receive" throughput rows. They are rates,
    # not interfaces: reading them as one would list an interface whose "IP address" is 0.04.
    throughput = {}
    for item, row in latest_items_for(code_map, "OS_NETWORK"):
        for suffix, key in ((" send", "send_mbps"), (" receive", "receive_mbps")):
            if item.endswith(suffix):
                throughput.setdefault(item[: -len(suffix)], {})[key] = as_float(row["metric_value"])

    network = [
        {
            "interface": item,
            "ip_address": row["metric_value"] or "",
            "speed_mbps": _to_int(parse_os_kv(row["message"]).get("speed_mbps")),
            "send_mbps": throughput.get(item, {}).get("send_mbps"),
            "receive_mbps": throughput.get(item, {}).get("receive_mbps"),
            "bytes_sent": _to_int(parse_os_kv(row["message"]).get("bytes_sent")),
            "bytes_received": _to_int(parse_os_kv(row["message"]).get("bytes_received")),
            "errors": _to_int(parse_os_kv(row["message"]).get("errors")),
            "dropped": _to_int(parse_os_kv(row["message"]).get("dropped")),
            "status": row["status"] or "",
        }
        for item, row in sorted(latest_items_for(code_map, "OS_NETWORK"))
        if item != "network" and not item.endswith((" send", " receive"))
    ]

    services = [
        {"name": item, "state": row["metric_value"] or "", "status": row["status"] or ""}
        for item, row in sorted(latest_items_for(code_map, "OS_SERVICE_STATUS"))
    ]

    def processes(code, rank_key):
        out = []
        for item, row in latest_items_for(code_map, code):
            kv = parse_os_kv(row["message"])
            out.append({
                "process": item,
                "cpu_percent": as_float(kv.get("cpu_percent")),
                "memory_mb": as_float(kv.get("memory_mb")),
                "memory_percent": as_float(kv.get("memory_percent")),
                "process_count": _to_int(kv.get("process_count")) or None,
            })
        out.sort(key=lambda entry: (entry.get(rank_key) is None, -(entry.get(rank_key) or 0)))
        return out

    events = [
        {"log": item, "count": _to_int(row["metric_value"]), "status": row["status"] or "",
         "detail": parse_os_kv(row["message"]).get("top", "")}
        for item, row in sorted(latest_items_for(code_map, "OS_EVENTLOG_CRITICAL"))
    ]

    reboot_row = one(code_map, "OS_REBOOT_PENDING", "reboot_pending")
    pending_reboot = (reboot_row["metric_value"] if reboot_row else "") or ""

    return {
        "os_info": info,
        "cpu": cpu,
        "memory": memory,
        "disks": disks,
        "network": network,
        "services": services,
        "top_cpu": processes("OS_PROCESS_TOP_CPU", "cpu_percent"),
        "top_memory": processes("OS_PROCESS_TOP_MEMORY", "memory_mb"),
        "events": events,
        "pending_reboot": pending_reboot,
    }


def build_server_overlay(server_id, ip, code_map, severity=None, problems=None, freshness=None):
    # When these blocks were true. Everything below is derived from metrics, so one stamp
    # covers them all — and a server the overlay never reaches keeps its old blocks with no
    # stamp at all, which is exactly how the report tells "live" from "last known".
    #
    # health_as_of is the *newest* stamp, which is the right answer to "when was this server last
    # heard from" and the wrong answer to "is every block on this page current". metric_freshness
    # answers the second one per metric, because the newest metric otherwise hides every late one
    # behind it (see MetricStore.fetch_metric_freshness).
    stamps = [str(r.get("collected_at") or "") for r in code_map.values() if r.get("collected_at")]
    policy_result = build_backup_policy(code_map, server_id)
    return {
        "server_id": server_id,
        "ip": ip,
        "health_as_of": max(stamps) if stamps else "",
        "health_oldest_as_of": min(stamps) if stamps else "",
        # The collectors' own verdict for this server, across every metric they ran - not just
        # the ones this report builds blocks from. This is what keeps the fleet page and the
        # per-server detail page saying the same thing about the same server.
        "metric_severity": severity or {},
        # The same grouped problem list the per-server page renders, so Priority Attention can be
        # built from what is actually wrong rather than re-derived from a hand-picked subset.
        "metric_problems": problems or [],
        "metric_freshness": freshness or {},
        "instance_health": build_instance_health(code_map),
        "database_health": build_database_health(code_map),
        "disk_health": build_disk_health(code_map),
        "backup_policy": policy_result,
        "backup_by_database": build_backup_by_database(code_map, policy_result),
        "backup_evidence": build_backup_evidence(code_map, policy_result),
        "sql_governance": build_sql_governance(code_map),
        "sql_agent_job_health": build_sql_agent_job_health(code_map),
        "backup_jobs": build_backup_jobs(code_map),
        "performance_health": build_performance_health(code_map),
        "index_health": build_index_health(code_map),
        "security_health": build_security_health(code_map),
        "config_warnings": build_config_warnings(code_map),
        "os_health": build_os_health(code_map),
        "inventory_status": build_inventory_status(code_map),
    }


# --------------------------------------------------------------------------- #
# Report entry point (called by reports CLI)
# --------------------------------------------------------------------------- #
def _error_summary(message, *, limit: int = 130) -> str:
    """The readable head of a collector error.

    Driver messages run to several hundred characters of ODBC state codes and nested "failed:"
    prefixes, so this keeps the head rather than a clause: the first line of
    ``sqlserver connect to 192.0.2.250:1433 failed: SQL Server pymssql connect failed:
    (18456, ...)`` already says which host, which port and which kind of failure. The full text
    stays in the finding's ``detail``.
    """
    text = " ".join(str(message or "").split())
    if not text:
        return "collector returned no result"
    head = text.split(". ", 1)[0]
    return head if len(head) <= limit else text[:limit].rstrip() + "…"


def _freshness_block(rows: list[dict], *, now: int, days: int) -> dict:
    """Per-metric freshness for the overlay, built by the same code the server page uses."""
    from db_ops.reports.server_report import build_freshness  # noqa: PLC0415 - same app, avoids a cycle

    return build_freshness(rows, now=now, days=days)


def build_metric_problems(rows: list[dict], *, now: int | None = None) -> list[dict]:
    """The grouped problem list for one server, from the store's current-snapshot rows.

    Same shape, same severities and same actions as the per-server page's ``problems`` — see
    :func:`db_ops.lib.health_model.group_findings`. The fleet page renders this instead of
    re-deriving findings from a hand-picked subset of signals, which is what left 42 current
    blocked sessions and a 95 ms latency finding with no Priority Attention card of their own.
    """
    from db_ops.reports.server_report import metric_action, metric_label  # noqa: PLC0415 - same app, avoids a cycle

    now = now if now is not None else int(
        datetime.datetime.now(datetime.timezone.utc).timestamp())
    findings = []
    for row in rows:
        code = str(row.get("metric_code") or "")
        value = as_float(row.get("metric_value"))
        text = str(row.get("metric_value") or "")
        unit = str(row.get("metric_unit") or "")
        item = str(row.get("metric_item") or health_model.COLLECTOR_ITEM)
        findings.append({
            "code": code,
            "label": metric_label(code),
            "item": item,
            # A collector-failure row has no value and no item — its message is the whole
            # finding, so it becomes the value. Without this the card read
            # "__collector__ · —", which names the problem's shape and not the problem.
            "value": (_error_summary(row.get("message")) if item == health_model.COLLECTOR_ITEM
                      else (f"{text} {unit}".strip() if text else "—")),
            "severity": health_model.current_severity(
                code=code, status=row.get("status"), value=value),
            "message": str(row.get("message") or ""),
            "lastText": text,
            "action": metric_action(code),
            "collectedAt": str(row.get("collected_at") or ""),
        })
    return health_model.group_findings(findings, now=now)


def build_inventory_health(*, sqlite_path, config=None, output_dir=None, days=2,
                           date=None, logger=None) -> dict:
    rows = load_metrics(sqlite_path, int(days))
    servers = index_by_server(rows)
    store = MetricStore(sqlite_path)
    severity = store.fetch_severity_by_server(days=int(days))
    problem_rows = store.fetch_current_problems(days=int(days))
    freshness_rows = store.fetch_metric_freshness(days=int(days))
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    overlay_servers = [
        build_server_overlay(
            sid, ip, code_map, severity.get(sid),
            problems=build_metric_problems(problem_rows.get(sid, []), now=now),
            freshness=_freshness_block(freshness_rows.get(sid, []), now=now, days=int(days)),
        )
        for sid, (ip, code_map) in sorted(servers.items())
    ]

    stamp = date or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(output_dir) if output_dir else (config.runtime_dir if config else Path("."))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{stamp}_database-inventory.json"

    overlay = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "db_ops metrics",
        "days_window": int(days),
        "servers": overlay_servers,
    }
    out_path.write_text(json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "SUCCESS",
        "file": str(out_path),
        "file_name": out_path.name,
        "servers": len(overlay_servers),
        "metric_rows": len(rows),
        "days_window": int(days),
    }
