"""Pure builder for the Latency percentiles section of server-metrics.html.

Three latency collectors record runtime metrics without grading them: SQL Server Query Store
intervals, Oracle V$EVENT_HISTOGRAM buckets, and PostgreSQL pg_stat_statements totals.
This module evaluates their latency percentiles on demand using the pure arithmetic from
:mod:`dbabrain.lib.latency_percentiles`.

Why each engine block is handled according to its metric nature:
* SQL Server (Query Store): completed intervals per database are aggregated server-side;
  consecutive collections frequently overlap by one interval, and deduplication keeps the newest.
* Oracle (V$EVENT_HISTOGRAM): cumulative duration bucket counts since instance startup.
  Subtracting consecutive collections gives the interval's wait counts. Percentiles are discrete
  bucket upper bounds (<= N ms) rather than interpolations.
* PostgreSQL (pg_stat_statements): cumulative execution totals per queryid. Interval percentiles
  are weighted by executions over per-statement means across consecutive collections.
* States, not failures: unconfigured extensions (NOT_INSTALLED) or single collections without a
  prior baseline are reported as states rather than errors.
"""

from __future__ import annotations

from typing import Any

from dbabrain.lib.interval_rates import message_fields
from dbabrain.lib.latency_percentiles import (
    histogram_interval,
    is_not_installed,
    query_store_intervals,
    statement_interval,
    weighted_percentiles,
)

QUERY_LATENCY_PERCENTILES_CODE = "QUERY_LATENCY_PERCENTILES"
ORACLE_WAIT_HISTOGRAM_CODE = "ORACLE_WAIT_LATENCY_HISTOGRAM"
POSTGRES_STATEMENT_TOTALS_CODE = "POSTGRES_STATEMENT_TOTALS"
#: The connection probe: INSTANCE_STATUS, taken every 30 s since 0.30.0, timed by metric-batch.
PROBE_CODE = "INSTANCE_STATUS"
#: A day of probes - the window the availability SLO is read over.
PROBE_DAYS = 1

LATENCY_CODES = [
    QUERY_LATENCY_PERCENTILES_CODE,
    ORACLE_WAIT_HISTOGRAM_CODE,
    POSTGRES_STATEMENT_TOTALS_CODE,
]

SQLSERVER_NATURE = (
    "SQL Server percentiles are weighted by executions over per-plan means "
    "(the spread inside one plan is not seen)."
)
ORACLE_NATURE = "Oracle's are real at power-of-two bucket resolution."
POSTGRESQL_NATURE = (
    "PostgreSQL percentiles are weighted by calls over per-statement means "
    "(the spread inside one statement is not seen), over the statements in both collections' "
    "top lists - calls covered says how much of the interval that is; the mean is all of it."
)

#: Days of rows fetched for the section, whatever the page's window: an interval is the newest two
#: collections, and Query Store keeps 12 intervals a database - a seven-day fetch of a 15-minute
#: metric would load rows nothing renders (the workload section's cap, for the same reason).
LATENCY_DAYS = 2

PROBE_NATURE = (
    "INSTANCE_STATUS timed from the collector: the connect, a one-row status query and the close, "
    "every 30 s - what a new client of this instance waits, network and login included. Successful "
    "probes only; a failed one is counted, and is a minute the availability SLO reads as down."
)
PROBE_GOOD = frozenset({"OK", "LOGGING"})


def _build_sqlserver_latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract newest Query Store completed intervals per database."""
    if not rows:
        return {
            "collected": False,
            "available": False,
            "state": "no_data",
            "reason": "Nothing was collected for SQL Server Query Store latency percentiles.",
            "nature": SQLSERVER_NATURE,
            "databases": [],
        }

    intervals = query_store_intervals(rows)
    if not intervals:
        return {
            "collected": True,
            "available": False,
            "state": "no_intervals",
            "reason": "No completed Query Store intervals in the window.",
            "nature": SQLSERVER_NATURE,
            "databases": [],
        }

    by_db: dict[str, list[dict[str, Any]]] = {}
    for item in intervals:
        db = str(item.get("database") or "").strip()
        if db:
            by_db.setdefault(db, []).append(item)

    databases: list[dict[str, Any]] = []
    for db in sorted(by_db.keys()):
        # Cap at the newest 12 intervals per database, newest first so latest readings lead.
        db_items = sorted(
            by_db[db],
            key=lambda x: str(x.get("interval_start_utc") or x.get("interval_start") or ""),
            reverse=True,
        )
        capped = db_items[:12]
        databases.append({
            "database": db,
            "name": db,
            "intervals": capped,
        })

    return {
        "collected": True,
        "available": True,
        "state": "ok",
        "reason": "",
        "nature": SQLSERVER_NATURE,
        "databases": databases,
    }


def _build_oracle_latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Difference the newest consecutive Oracle wait event histogram collections."""
    if not rows:
        return {
            "collected": False,
            "available": False,
            "state": "no_data",
            "reason": "Nothing was collected for Oracle wait latency histograms.",
            "nature": ORACLE_NATURE,
            "events": [],
        }

    collections_by_stamp: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        stamp = str(r.get("collected_at") or "")
        collections_by_stamp.setdefault(stamp, []).append(r)

    stamps = sorted(collections_by_stamp.keys())
    if len(stamps) < 2:
        return {
            "collected": True,
            "available": False,
            "state": "needs_two_collections",
            "reason": "An interval needs two collections with no restart between them.",
            "nature": ORACLE_NATURE,
            "events": [],
        }

    older_rows = collections_by_stamp[stamps[-2]]
    newer_rows = collections_by_stamp[stamps[-1]]

    def _counters_since(c_rows: list[dict[str, Any]]) -> str:
        for r in c_rows:
            cs = message_fields(r.get("message")).get("counters_since") or r.get("counters_since")
            if cs:
                return str(cs)
        return ""

    older_cs = _counters_since(older_rows)
    newer_cs = _counters_since(newer_rows)
    if older_cs and newer_cs and older_cs != newer_cs:
        return {
            "collected": True,
            "available": False,
            "state": "restart",
            "reason": "An interval needs two collections with no restart between them.",
            "nature": ORACLE_NATURE,
            "events": [],
        }

    def _event_name(row: dict[str, Any]) -> str:
        fields = message_fields(row.get("message"))
        return str(fields.get("event") or row.get("metric_item") or "").strip()

    older_by_event = {_event_name(r): r for r in older_rows}

    events: list[dict[str, Any]] = []
    for newer_r in newer_rows:
        ev = _event_name(newer_r)
        if not ev or ev not in older_by_event:
            continue
        # A refused event is left out alone: a restart refuses every event, and then none is left.
        interval = histogram_interval(older_by_event[ev], newer_r)
        if interval is not None:
            events.append(interval)

    if not events:
        return {
            "collected": True,
            "available": False,
            "state": "restart",
            "reason": "An interval needs two collections with no restart between them.",
            "nature": ORACLE_NATURE,
            "events": [],
        }

    events.sort(key=lambda e: (-(e.get("waits") or 0), str(e.get("event") or "")))
    seconds = events[0].get("seconds", 0.0) if events else 0.0

    return {
        "collected": True,
        "available": True,
        "state": "ok",
        "reason": "",
        "seconds": seconds,
        "nature": ORACLE_NATURE,
        "events": events,
    }


def _build_postgres_latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Difference the newest consecutive pg_stat_statements collections."""
    if not rows:
        return {
            "collected": False,
            "available": False,
            "state": "no_data",
            "reason": "Nothing was collected for PostgreSQL statement totals.",
            "nature": POSTGRESQL_NATURE,
            "interval": None,
        }

    collections_by_stamp: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        stamp = str(r.get("collected_at") or "")
        collections_by_stamp.setdefault(stamp, []).append(r)

    stamps = sorted(collections_by_stamp.keys())

    # An uninstalled extension is a configuration state, not a collection failure - and only the
    # newest collection says whether it is installed now: a cluster enabled this afternoon still
    # has the morning's NOT_INSTALLED rows in the window.
    if is_not_installed(collections_by_stamp[stamps[-1]]):
        return {
            "collected": True,
            "available": False,
            "state": "not_installed",
            "reason": (
                "pg_stat_statements is not configured: requires pg_stat_statements "
                "in shared_preload_libraries and CREATE EXTENSION."
            ),
            "nature": POSTGRESQL_NATURE,
            "interval": None,
        }

    if len(stamps) < 2:
        return {
            "collected": True,
            "available": False,
            "state": "needs_two_collections",
            "reason": "An interval needs two collections with no restart between them.",
            "nature": POSTGRESQL_NATURE,
            "interval": None,
        }

    older_rows = collections_by_stamp[stamps[-2]]
    newer_rows = collections_by_stamp[stamps[-1]]

    interval = statement_interval(older_rows, newer_rows)
    if interval is None:
        return {
            "collected": True,
            "available": False,
            "state": "restart",
            "reason": "An interval needs two collections with no restart between them.",
            "nature": POSTGRESQL_NATURE,
            "interval": None,
        }

    return {
        "collected": True,
        "available": True,
        "state": "ok",
        "reason": "",
        "nature": POSTGRESQL_NATURE,
        "interval": interval,
    }


def build_probe_latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """p50 / p95 / p99 of the connection probe, per instance, from INSTANCE_STATUS's timed rows.

    A real distribution - every sample is one connect and one query, nothing averaged first -
    which no DMV keeps for queries (``workload_attribution``). Recorded, never graded: no
    threshold has been agreed for it.
    """
    per_target: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("metric_code") or "") != PROBE_CODE:
            continue
        target = str(row.get("target_id") or row.get("server_id") or "")
        entry = per_target.setdefault(target, {"times": [], "failed": 0, "untimed": 0})
        if str(row.get("status") or "").upper() not in PROBE_GOOD:
            entry["failed"] += 1
            continue
        try:
            seconds = float(row.get("execution_time"))
        except (TypeError, ValueError):
            entry["untimed"] += 1
            continue
        entry["times"].append(seconds * 1000.0)
    targets = []
    for target, entry in sorted(per_target.items()):
        times = entry["times"]
        quantiles = weighted_percentiles([(ms, 1.0) for ms in times])
        targets.append({
            "target": target,
            "samples": len(times),
            "failed": entry["failed"],
            "untimed": entry["untimed"],
            "p50_ms": quantiles.get(50),
            "p95_ms": quantiles.get(95),
            "p99_ms": quantiles.get(99),
            "max_ms": max(times) if times else None,
        })
    available = any(item["samples"] for item in targets)
    return {
        "collected": bool(targets),
        "available": available,
        "nature": PROBE_NATURE,
        "targets": targets,
        # Rows collected before 0.30.0 hold no time: a page built over them says so.
        "reason": "" if available else "No timed probe yet - INSTANCE_STATUS rows hold a time from 0.30.0 on.",
    }


def build_latency(rows: list[dict[str, Any]], probe_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the Latency percentiles section payload from the store's collected rows.

    Dispatches to engine-specific builders for SQL Server, Oracle, and PostgreSQL,
    producing percentiles from the pure arithmetic in lib/latency_percentiles without
    grading or alerting; and the connection probe, every engine alike.
    """
    sql_rows = [
        r for r in rows
        if str(r.get("metric_code") or r.get("code") or "") == QUERY_LATENCY_PERCENTILES_CODE
    ]
    ora_rows = [
        r for r in rows
        if str(r.get("metric_code") or r.get("code") or "") == ORACLE_WAIT_HISTOGRAM_CODE
    ]
    pg_rows = [
        r for r in rows
        if str(r.get("metric_code") or r.get("code") or "") == POSTGRES_STATEMENT_TOTALS_CODE
    ]

    sql_block = _build_sqlserver_latency(sql_rows)
    ora_block = _build_oracle_latency(ora_rows)
    pg_block = _build_postgres_latency(pg_rows)
    probe_block = build_probe_latency(probe_rows or [])
    blocks = (sql_block, ora_block, pg_block, probe_block)
    return {
        "collected": any(block["collected"] for block in blocks),
        # Opens the fold: at least one engine has an interval to show.
        "available": any(block["available"] for block in blocks),
        "sqlserver": sql_block,
        "oracle": ora_block,
        "postgresql": pg_block,
        "probe": probe_block,
    }
