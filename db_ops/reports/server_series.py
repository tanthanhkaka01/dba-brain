"""``server-metrics.html``: the metric catalog as this page reads it, one server's chartable series, and how fresh each metric is.

Split out of ``reports/server_report.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``server_report`` re-exports
every name, so no import changes.
"""

from __future__ import annotations
from db_ops.lib.coerce import as_float
import datetime
import json
import math
import re
from pathlib import Path
from db_ops.lib import data_sources
from db_ops.lib import health_model, interval_rates
from db_ops.db.metric_store import MetricStore
from db_ops.lib.paths import DEFAULT_DATA_DIR
from db_ops.lib.time_window import window_of


#: The Query Store coverage metric. Here, in the one server-page module that imports nothing else of
#: the page, because ``inventory_health`` both reads it and is read by the sections: while it lived
#: in ``server_report`` the two imported each other, and whichever came first met the other half
#: initialised - ``ImportError: cannot import name 'QUERY_STORE_CODE' from partially initialized
#: module``, every ``test_server_report_*`` file failing to collect when run on its own. A leaf has
#: no such order (2026-10-03, when the page was split).
QUERY_STORE_CODE = "QUERY_STORE_COVERAGE"


def _cadence(definition: dict, code: str) -> int | None:
    """A metric definition's collection interval, as the scheduler reads it (rules R20)."""
    window = window_of(definition, context=code or "metric_definition")
    return window.repeat_interval if window else None

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
            every = _cadence(definition, code)
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
            # A variant that says `"supported": false` is the catalog stating the engine has no
            # such metric (Query Store on Oracle, CHECKDB on PostgreSQL). Counting it made every
            # Oracle and PostgreSQL page report ~40 SQL Server metrics as "not collected" - 26/66
            # on the one Oracle instance, 22/66 on the PostgreSQL one (2026-10-02).
            db_types = {str(variant.get("db_type") or "").lower()
                        for variant in (definition.get("variants") or [])
                        if variant.get("db_type") and variant.get("supported", True) is not False}
            declared = str(definition.get("db_type") or "").lower()
            if declared and declared != "multi":
                db_types.add(declared)
            catalog.append({
                "code": code,
                "active": bool(definition.get("active", True)),
                "collector_type": str(definition.get("collector_type") or ""),
                "db_types": sorted(db_types),
                "cadence": _cadence(definition, code),
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
