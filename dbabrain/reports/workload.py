"""How much work an instance actually did over an interval, from cumulative counters.

Three collectors record raw totals and grade nothing — ``PERFORMANCE_WORKLOAD_COUNTERS`` (CPU,
requests, logical and physical I/O, log), ``PERFORMANCE_QUERY_STATS_TOTALS`` (the same work as the
plan cache attributes it) and ``PERFORMANCE_WAIT_TOTALS`` (what the instance waited on). Every
number they store is a total since the engine started, which is the only thing a single query on
the target can honestly report. This module is the other half: it subtracts two collections and
turns the difference into "in the last hour, this instance burned 30,000,000 ms of CPU and read
2.5 billion pages".

It is shared by the per-server page and the fleet inventory because both answer that question and
they must not answer it differently. The subtraction itself, and the rules that keep it honest, are
in :mod:`dbabrain.lib.interval_rates`; what belongs here is which counters exist, what they are
called in English, and which ones are worth deriving from the others.

**Three windows, not one.** The same counters read over 15 minutes and over 24 hours answer
different questions — "is it busy now" and "was last night's batch bigger than usual" — and a page
that shows only one of them invites the reader to mistake it for the other. Each window reports the
span it actually measured, because a window that could not be filled is reported as the span there
was, never as the span that was asked for.

**A missing sample drops that reading, not the page.** Every window is resolved independently, so a
gap in the morning's collections costs the 24-hour column and leaves the 15-minute one intact.
"""

from __future__ import annotations

from typing import Any, Callable

from dbabrain.lib import interval_rates
from dbabrain.lib.coerce import as_epoch, as_float

#: What the builder reads. Also the list the reports fetch from the store — declared once here so a
#: page cannot ask for a code this module does not know how to render.
COUNTER_CODE = "PERFORMANCE_WORKLOAD_COUNTERS"
QUERY_STATS_CODE = "PERFORMANCE_QUERY_STATS_TOTALS"
WAIT_TOTALS_CODE = "PERFORMANCE_WAIT_TOTALS"
MEMORY_CODE = "PERFORMANCE_MEMORY_GAUGES"
#: The host's own CPU totals since boot, and the database process's: what the CPU rows fall back
#: to where the engine keeps no CPU total of its own (SQL Server Standard and Express, PostgreSQL).
OS_CPU_CODE = "OS_CPU_COUNTERS"
WORKLOAD_CODES = [COUNTER_CODE, QUERY_STATS_CODE, WAIT_TOTALS_CODE, MEMORY_CODE, OS_CPU_CODE]

#: Items that are a reading of the moment rather than a running total. Differencing one produces a
#: number that looks like a rate and means nothing, so they are carried through as-is. The three
#: ``*_sampled`` / ``cpu_sample_minutes`` items are the scheduler monitor's per-minute CPU readings,
#: averaged by the collector over the minutes since the previous collection.
GAUGE_ITEMS = {"cached_plans", "cache_baseline_minutes",
               "sql_cpu_pct_sampled", "host_cpu_pct_sampled", "cpu_sample_minutes"}

#: Facts a collector carries in each row's message rather than as an item of its own: the restart
#: marker, and what the CPU rows need to be read - how many CPUs the host has and how many the
#: engine may use, whether the engine counts its own CPU, and which process the host figures are of.
MESSAGE_FIELDS = ("counters_since", "cpu_count", "schedulers_online", "pools_account_cpu",
                  "edition", "logical_cpus", "process_since", "db_process")
#: Keys of a pivoted sample that are not counters.
META_FIELDS = frozenset({"collected_at", "sources", *MESSAGE_FIELDS})

#: The intervals every table is reported over. ``min_fraction`` is what a span has to reach before
#: it may be labelled as this window — see :func:`interval_rates.window_delta`. The newest pair has
#: no fraction because it is not claiming to cover a period; it *is* the period.
WINDOWS: list[dict[str, Any]] = [
    {"key": "now", "label": "latest interval", "hours": 1.0, "newest_pair": True},
    {"key": "hour", "label": "last hour", "hours": 1.0, "min_fraction": 0.5},
    {"key": "day", "label": "last 24 hours", "hours": 24.0, "min_fraction": 0.5},
]

#: How far back a report reads these counters. Two days covers the widest interval the
#: pages show (24 hours) with a day of slack for a worker that fell behind, and is
#: deliberately independent of whatever `days` window the report itself was asked for:
#: a seven-day report would otherwise load five days of a 15-minute metric to render
#: the same three columns.
WORKLOAD_DAYS = 2

_MB = 1024.0 * 1024.0


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """``numerator / denominator``, or ``None`` when the denominator says nothing happened.

    Zero operations is not zero latency and zero CPU capacity is not 0% busy; both have to come
    back as "no reading" or the page states a fact it does not have.
    """
    if numerator is None or not denominator:
        return None
    return numerator / denominator


#: ``(key, label, unit)`` for the counters rendered straight from their delta, grouped the way a
#: DBA reads them: what was asked of the instance, what that cost in logical work, what reached the
#: disk, and what it queued on.
_COUNTER_ROWS: list[tuple[str, list[tuple[str, str, str]]]] = [
    # Under the percentage, not instead of it: the percentage is what says whether the instance is
    # busy, and the milliseconds are what a capacity conversation is actually held in.
    # CPU is not in this list: its rows come from whichever source this target has, and
    # :func:`_build_cpu` decides which. The group is still first.
    ("CPU", []),
    ("Requests", [
        ("batch_requests", "Batch requests", "count"),
        # Oracle's, from v$sysstat: SQL Server has no row that means the same.
        ("user_calls", "User calls", "count"),
        ("executions", "Executions", "count"),
        ("transactions", "Transactions", "count"),
        ("sql_compilations", "Compilations", "count"),
        ("sql_recompilations", "Recompilations", "count"),
        ("logins", "Logins", "count"),
    ]),
    ("Logical work", [
        # The instance-wide equivalent of total_logical_reads, and the one that misses nothing:
        # unlike the plan cache it counts uncached and evicted work too.
        ("page_lookups", "Logical reads (page lookups)", "pages"),
        # PostgreSQL's tuple counters: what the blocks above were read for.
        ("rows_read", "Rows read", "count"),
        ("rows_written", "Rows written", "count"),
        ("full_scans", "Full scans", "count"),
        ("page_splits", "Page splits", "count"),
        ("workfiles_created", "Workfiles created", "count"),
        ("worktables_created", "Worktables created", "count"),
    ]),
    ("Physical I/O", [
        ("page_reads", "Physical page reads", "pages"),
        ("page_writes", "Physical page writes", "pages"),
        ("readahead_pages", "Read-ahead pages", "pages"),
        ("checkpoint_pages", "Checkpoint pages", "pages"),
        ("lazy_writes", "Lazy writes", "pages"),
        ("io_reads", "File reads", "count"),
        ("io_writes", "File writes", "count"),
        # A sort or a hash that did not fit in work_mem: PostgreSQL's spill to disk.
        ("temp_files", "Temp files created", "count"),
    ]),
    ("Transaction log", [
        ("log_flushes", "Log flushes", "count"),
    ]),
    ("Contention", [
        ("lock_waits", "Lock waits", "count"),
        ("lock_wait_ms", "Time spent locked", "ms"),
        ("deadlocks", "Deadlocks", "count"),
    ]),
]

#: Counters that are only readable once combined with another. ``fn`` receives the window's deltas,
#: its measured length in seconds, and the instance's CPU count.
_DERIVED_ROWS: list[tuple[str, str, str, str, Callable[[dict, float, float | None], float | None]]] = [
    ("Physical I/O", "io_mb_read", "Read from disk", "mb",
     lambda d, secs, cpus: _ratio(d.get("io_bytes_read"), _MB)),
    ("Physical I/O", "io_mb_written", "Written to disk", "mb",
     lambda d, secs, cpus: _ratio(d.get("io_bytes_written"), _MB)),
    ("Physical I/O", "temp_mb_written", "Temp written", "mb",
     lambda d, secs, cpus: _ratio(d.get("temp_bytes"), _MB)),
    # Latency over the interval, which is the figure PERFORMANCE_IO_LATENCY cannot give: its
    # per-file average is taken over the whole uptime.
    ("Physical I/O", "io_read_latency_ms", "Read latency", "ms_avg",
     lambda d, secs, cpus: _ratio(d.get("io_stall_read_ms"), d.get("io_reads"))),
    ("Physical I/O", "io_write_latency_ms", "Write latency", "ms_avg",
     lambda d, secs, cpus: _ratio(d.get("io_stall_write_ms"), d.get("io_writes"))),
    ("Transaction log", "log_mb_flushed", "Log generated", "mb",
     lambda d, secs, cpus: _ratio(d.get("log_bytes_flushed"), _MB)),
]

#: The plan cache's account of the same work. Read against the counters above, not on its own —
#: the collector's message says why, and :func:`build_workload` carries the caveat to the page.
_QUERY_ROWS: list[tuple[str, str, str]] = [
    ("query_cpu_ms", "CPU attributed to cached plans", "ms"),
    ("query_elapsed_ms", "Elapsed time in cached plans", "ms"),
    ("query_logical_reads", "Logical reads", "pages"),
    ("query_logical_writes", "Logical writes", "pages"),
    ("query_physical_reads", "Physical reads", "pages"),
    ("query_executions", "Executions", "count"),
]


#: The Memory table: ``(key, label, unit, watch)``, in the order the question is worked through.
#:
#: ``watch`` names the end of the range that is the finding, and it is the whole reason this table
#: is not a column of single readings. For page life expectancy the **lowest** value over the window
#: is what a memory conversation is about; for pending memory grants it is the **highest**, because
#: one query made to wait is the event and the other 95 samples of zero are not. A row with no
#: ``watch`` is a level rather than an extreme — the pool's size is read against its target, not
#: against its own worst minute.
_MEMORY_ROWS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    # First, because it is the question PLE is usually asked in place of, and its answer is
    # almost always a flat zero that settles the matter.
    ("Did anything wait for memory", [
        ("memory_grants_pending", "Memory grants pending", "count", "max"),
        ("memory_grants_outstanding", "Memory grants held", "count", "max"),
        ("brokers_shrinking", "Memory brokers told to shrink", "count", "max"),
    ]),
    # Second: did the pool get what it asked for. Total == Target means nothing outside SQL Server
    # is taking memory away from it, which rules out the operating system as the cause.
    ("Buffer pool", [
        ("total_server_memory_kb", "Total server memory", "kb", ""),
        ("target_server_memory_kb", "Target server memory", "kb", ""),
        ("database_cache_memory_kb", "…of which data pages", "kb", ""),
        ("stolen_server_memory_kb", "…of which stolen", "kb", ""),
        ("free_memory_kb", "…free", "kb", ""),
    ]),
    # Only now PLE, and never alone.
    ("Page life expectancy", [
        ("page_life_expectancy", "Instance", "seconds", "min"),
        ("ple_node_min", "Worst NUMA node", "seconds", "min"),
    ]),
    ("Host memory", [
        ("sql_physical_memory_in_use_kb", "Held by the SQL Server process", "kb", ""),
        ("available_physical_kb", "Available to the host", "kb", "min"),
        ("total_physical_kb", "Installed on the host", "kb", ""),
        ("system_low_memory_signal", "Windows asked for memory back", "flag", "max"),
        ("process_physical_memory_low", "Process reported low memory", "flag", "max"),
    ]),
]

#: Carried beside the table rather than in it: a count, not a measure over time.
_MEMORY_FACTS = ("ple_node_count",)


def _samples(rows: list[dict], code: str) -> list[dict[str, Any]]:
    """The metric's rows pivoted into one record per collection.

    Wide rather than one series per item on purpose: a latency is a stall divided by an operation
    count, and the two are only divisible if they came from the same read of the DMV. Pivoting on
    ``collected_at`` — which every row of one execution shares — is what guarantees that.
    """
    by_stamp: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("metric_code") or "").upper() != code:
            continue
        stamp = str(row.get("collected_at") or "")
        item = str(row.get("metric_item") or "").strip()
        if not stamp or not item:
            continue                    # an itemless row is a failed collection, not a sample
        entry = by_stamp.setdefault(stamp, {"collected_at": stamp, "sources": {}})
        entry[item] = row.get("metric_value")
        fields = interval_rates.message_fields(row.get("message"))
        for name in MESSAGE_FIELDS:
            if not entry.get(name):
                entry[name] = fields.get(name, "")
        # Which view each item came from. One metric can carry the same quantity from different
        # places on different engines - CPU from the resource pools, from v$sys_time_model - and the
        # page has to say which it is showing.
        entry["sources"][item] = fields.get("source", "")
    return sorted(by_stamp.values(), key=lambda s: s["collected_at"])


def _shared_fields(samples: list[dict[str, Any]]) -> list[str]:
    """The counter items present in *every* sample.

    An item that appears in some collections and not others cannot be differenced, and asking for
    it would cost the whole window: :func:`interval_rates.window_delta` drops a sample that is
    missing any requested field, which is the right rule for a partial collection and the wrong
    outcome when one counter simply does not exist on this build (``CXCONSUMER`` before 2016,
    a counter renamed between versions). Taking the intersection means such an item is absent from
    the page rather than emptying it.
    """
    if not samples:
        return []
    common: set[str] | None = None
    for sample in samples:
        present = {k for k in sample if k not in META_FIELDS and k not in GAUGE_ITEMS}
        common = present if common is None else (common & present)
    return sorted(common or ())


def _deltas(samples: list[dict[str, Any]], fields: list[str], *,
            allow_partial: bool = False) -> dict[str, dict[str, Any]]:
    """One resolved delta per window, keyed by window, skipping the ones that have no honest pair.

    ``allow_partial`` is the plan cache's exception and nothing else's — see
    :func:`interval_rates._pair`. An engine counter that goes backwards has been reset and takes
    every other counter with it; a plan-cache total goes backwards every time a plan is evicted,
    which is continuous on a busy instance and would otherwise blank the whole table.
    """
    if not fields or len(samples) < 2:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for window in WINDOWS:
        if window.get("newest_pair"):
            delta = interval_rates.interval_delta(
                samples, fields=fields, max_pair_hours=window["hours"],
                allow_partial=allow_partial)
        else:
            delta = interval_rates.window_delta(
                samples, fields=fields, window_hours=window["hours"],
                min_fraction=window["min_fraction"], allow_partial=allow_partial)
        if delta:
            found[window["key"]] = delta
    return found


def _cell(total: float | None, seconds: float, *, rate: bool = True) -> dict[str, Any] | None:
    """One table cell: the total over the window and, where it means something, the rate."""
    if total is None:
        return None
    cell: dict[str, Any] = {"total": round(total, 2)}
    if rate and seconds:
        cell["perSecond"] = round(total / seconds, 3)
    return cell


def _latest(samples: list[dict[str, Any]], name: str) -> str:
    """The newest non-empty value of a carried message field."""
    for sample in reversed(samples):
        value = str(sample.get(name) or "").strip()
        if value:
            return value
    return ""


# --------------------------------------------------------------------------------------------- #
# CPU
#
# The one group whose rows do not come from a single counter. "CPU used 0.0%" sat on a busy 2019
# Standard ERP instance for weeks (2026-10-16): the resource pools it was read from count nothing
# outside Enterprise and Developer, and the page differenced a flat zero. Where the engine keeps no
# total of its own the CPU is still knowable - the host counts the process, the scheduler monitor
# samples it every minute - so each row takes the best source this target has, and names it.
# --------------------------------------------------------------------------------------------- #

#: Where a database-CPU figure came from, by the ``source`` its collector wrote.
_DB_SOURCES = {
    "dm_resource_governor_resource_pools": "resource pools (sys.dm_resource_governor_resource_pools)",
    "v$sys_time_model": "v$sys_time_model: DB CPU + background cpu time",
    "v$sysstat": "v$sysstat: CPU used by this session - counted when each call ends",
}
_SAMPLED_SOURCE = "scheduler monitor, one sample a minute (sys.dm_os_ring_buffers)"


def _engine(counter_samples: list[dict[str, Any]], os_samples: list[dict[str, Any]]) -> str:
    """Which engine the samples describe, from what they carry - the builder is not told."""
    if _latest(counter_samples, "pools_account_cpu"):
        return "SQL Server"
    sources = {src for sample in counter_samples for src in (sample.get("sources") or {}).values()}
    if any(str(src).startswith("v$") for src in sources):
        return "Oracle"
    if any(str(src).startswith("pg_") for src in sources):
        return "PostgreSQL"
    process = _latest(os_samples, "db_process")
    if process.startswith("postgres"):
        return "PostgreSQL"
    if process.startswith("sqlservr"):
        return "SQL Server"
    return ""


def _limit_reason(engine: str, edition: str, online: int | None, total: int | None) -> str:
    """Why the engine runs on fewer CPUs than the host has, in the words a licence states it."""
    if not online or not total or online >= total:
        return ""
    if engine == "SQL Server":
        if "Express" in edition:
            return "SQL Server Express uses at most the lesser of 1 socket or 4 cores"
        if "Web" in edition:
            return "SQL Server Web uses at most the lesser of 4 sockets or 16 cores"
        if "Standard" in edition:
            return "SQL Server Standard uses at most the lesser of 4 sockets or 24 cores"
        return "the instance's affinity mask leaves the other CPUs to the host"
    if engine == "Oracle":
        if "Standard Edition" in edition:
            return "Oracle Standard Edition 2 runs on at most 16 CPU threads"
        if "Free" in edition or "Express" in edition:
            return "Oracle Free and Express run on at most 2 CPU threads"
        return "the instance's cpu_count parameter caps it"
    return ""


def cpu_facts(counter_samples: list[dict[str, Any]],
              os_samples: list[dict[str, Any]]) -> dict[str, Any]:
    """How many CPUs the host has, how many the engine may use, and why they differ.

    ``online`` is the denominator of the database's CPU percentage: 100% is the engine saturated.
    Taken of the host's count instead, an instance limited to 48 of 60 CPUs reads 80% at full load
    - measured on a 2019 Standard instance, where the page called a saturated engine comfortable.
    """
    total_text = _latest(counter_samples, "cpu_count") or _latest(os_samples, "logical_cpus")
    total = as_float(total_text)
    online = as_float(_latest(counter_samples, "schedulers_online")) or total
    if total and online and online > total:
        online = total                  # an Oracle cpu_count set above the host's NUM_CPUS
    engine = _engine(counter_samples, os_samples)
    edition = _latest(counter_samples, "edition")
    total_i = int(total) if total else None
    online_i = int(online) if online else None
    # "SQL Server Standard Edition (64-bit)", "Oracle Database 19c Standard Edition 2 ...": the name
    # a sentence can start with. SERVERPROPERTY('Edition') does not say which product it is.
    label = edition if not engine or edition.startswith(engine.split()[0]) else f"{engine} {edition}"
    return {
        "total": total_i,
        "online": online_i,
        "engine": engine,
        "edition": edition,
        "label": label.strip() or engine,
        "limit": _limit_reason(engine, edition, online_i, total_i),
    }


def _os_deltas(samples: list[dict[str, Any]], fields: list[str], *, marker: str) -> dict:
    """:func:`_deltas` of the OS totals, against the baseline ``marker`` names.

    The host's totals restart at boot and the process's when the service restarts, and one
    collection carries both: differencing the process across a service restart is the same lie as
    differencing an engine counter across one, while the host's figures are still good.
    """
    rebased = [dict(sample, counters_since=sample.get(marker) or "") for sample in samples]
    return _deltas(rebased, fields)


def _sampled(samples: list[dict[str, Any]], item: str) -> dict[str, dict[str, float]]:
    """A per-collection average (``item``, over ``cpu_sample_minutes``) summarised per window.

    Weighted by the minutes each collection covered, and refused below the fraction of the window
    :data:`WINDOWS` asks of a counter - an hour built from two collections is half an hour.
    Collections are kept when they fall *after* the window opens: each one averages the minutes
    before it, so the collection at the window's start describes the quarter before the window.
    """
    stamped = []
    for sample in samples:
        stamp = as_epoch(sample.get("collected_at"))
        value = as_float(sample.get(item))
        minutes = as_float(sample.get("cpu_sample_minutes"))
        if stamp is not None and value is not None and minutes:
            stamped.append((stamp, value, minutes))
    if not stamped:
        return {}
    stamped.sort()
    newest = stamped[-1][0]
    found: dict[str, dict[str, float]] = {}
    for window in WINDOWS:
        if window.get("newest_pair"):
            inside = stamped[-1:]
        else:
            inside = [entry for entry in stamped if entry[0] > newest - window["hours"] * 3600]
        minutes = sum(entry[2] for entry in inside)
        if not window.get("newest_pair") and minutes < window["hours"] * 60 * window["min_fraction"]:
            continue
        found[window["key"]] = {
            "avg": sum(entry[1] * entry[2] for entry in inside) / minutes,
            "minutes": minutes,
        }
    return found


def _build_cpu(counter_samples: list[dict[str, Any]], counter_deltas: dict,
               os_samples: list[dict[str, Any]], facts: dict[str, Any]) -> dict[str, Any]:
    """The CPU group's rows, each from the best source this target has, and why one is missing.

    Database CPU, in order: the engine's own cumulative total (the resource pools on SQL Server
    Enterprise, v$sys_time_model on Oracle) - the host's account of the database process
    (OS_CPU_COUNTERS) - the scheduler monitor's per-minute samples, which every SQL Server edition
    keeps. The first two are exact over the window; the third is a sample of every minute in it,
    and its cells say so. Host CPU takes the host's own totals, then Oracle's v$osstat, then the
    scheduler monitor. A row whose source is missing is absent with ``note`` saying why - never 0.
    """
    online = facts.get("online")
    total = facts.get("total")
    windows_seconds: dict[str, float] = {}

    db: dict[str, dict[str, Any]] = {}
    db_source = ""
    for win_key, delta in counter_deltas.items():
        ms = delta["deltas"].get("cpu_usage_ms")
        if ms is not None:
            db[win_key] = {"ms": ms, "seconds": float(delta["seconds"])}
    if db:
        source = (counter_samples[-1].get("sources") or {}).get("cpu_usage_ms", "")
        db_source = _DB_SOURCES.get(source, source)

    process = _latest(os_samples, "db_process")
    if not db:
        for win_key, delta in _os_deltas(os_samples, ["db_cpu_ms"], marker="process_since").items():
            db[win_key] = {"ms": delta["deltas"]["db_cpu_ms"], "seconds": float(delta["seconds"])}
        if db:
            db_source = f"the host's account of the process: {process}"

    sampled_db = _sampled(counter_samples, "sql_cpu_pct_sampled")
    if not db and sampled_db and total:
        for win_key, reading in sampled_db.items():
            seconds = reading["minutes"] * 60
            # The sample is a share of the whole host; as work it is that share of every CPU.
            db[win_key] = {"ms": reading["avg"] / 100.0 * total * seconds * 1000.0,
                           "seconds": seconds, "estimate": True,
                           "sub": f"{reading['minutes']:.0f} min sampled"}
        db_source = _SAMPLED_SOURCE

    rows: list[dict[str, Any]] = []
    if db:
        pct_values, ms_values = {}, {}
        for win_key, entry in db.items():
            windows_seconds.setdefault(win_key, entry["seconds"])
            extra = {key: entry[key] for key in ("estimate", "sub") if entry.get(key)}
            pct = _ratio(entry["ms"], entry["seconds"] * (online or 0) * 10.0)
            if pct is not None:
                pct_values[win_key] = {"total": round(pct, 2), **extra}
            cell = _cell(entry["ms"], entry["seconds"])
            if cell:
                ms_values[win_key] = {**cell, **extra}
        if pct_values:
            rows.append({"key": "cpu_pct", "label": "Database CPU", "unit": "pct",
                         "source": f"% of the {online} CPUs it may use · {db_source}",
                         "values": pct_values})
        rows.append({"key": "cpu_usage_ms", "label": "Database CPU time", "unit": "ms",
                     "source": db_source, "values": ms_values})

    host: dict[str, dict[str, Any]] = {}
    host_source = ""
    for deltas, label in (
            (_os_deltas(os_samples, ["host_busy_ms", "host_idle_ms"], marker="counters_since"),
             "the host's own totals (" + ((os_samples[-1].get("sources") or {}).get("host_busy_ms", "")
                                         if os_samples else "") + ")"),
            ({k: v for k, v in counter_deltas.items()
              if "host_busy_ms" in v["deltas"] and "host_idle_ms" in v["deltas"]},
             "v$osstat: BUSY_TIME / (BUSY_TIME + IDLE_TIME)")):
        for win_key, delta in deltas.items():
            pct = _ratio(delta["deltas"]["host_busy_ms"] * 100.0,
                         delta["deltas"]["host_busy_ms"] + delta["deltas"]["host_idle_ms"])
            if pct is not None:
                host[win_key] = {"total": round(pct, 2)}
                windows_seconds.setdefault(win_key, float(delta["seconds"]))
        if host:
            host_source = label
            break
    if not host:
        for win_key, reading in _sampled(counter_samples, "host_cpu_pct_sampled").items():
            host[win_key] = {"total": round(reading["avg"], 2), "estimate": True,
                             "sub": f"{reading['minutes']:.0f} min sampled"}
            windows_seconds.setdefault(win_key, reading["minutes"] * 60)
        if host:
            host_source = _SAMPLED_SOURCE
    if host:
        rows.append({"key": "host_cpu_pct", "label": "Host CPU", "unit": "pct",
                     "source": f"% of all {total} CPUs, every process · {host_source}"
                               if total else host_source,
                     "values": host})

    return {"rows": rows, "seconds": windows_seconds,
            "note": _cpu_note(facts, db_source=db_source, process=process,
                              has_os=bool(os_samples), counter_samples=counter_samples)}


def _cpu_note(facts: dict[str, Any], *, db_source: str, process: str, has_os: bool,
              counter_samples: list[dict[str, Any]]) -> str:
    """The sentence the page prints when the database's CPU is estimated or cannot be shown."""
    engine = facts.get("engine") or "This engine"
    edition = facts.get("label") or engine
    no_pools = _latest(counter_samples, "pools_account_cpu") == "0"
    host_reason = (f" The host could not say which process is the database: {process.removeprefix('none: ')}."
                   if has_os and process.startswith("none") else
                   "" if has_os else " This target does not run host collectors (OS_CPU_COUNTERS), "
                                     "which would count the process exactly.")
    if db_source == _SAMPLED_SOURCE:
        return (f"{edition} keeps no CPU total of its own - Resource Governor, which counts it on "
                f"Enterprise and Developer, is not in this edition - so Database CPU is the "
                f"scheduler monitor's reading, taken every minute of the window: close, but a sample, "
                f"hence ≈.{host_reason}")
    if db_source:
        return ""
    if engine == "PostgreSQL" or (not counter_samples and has_os):
        return ("PostgreSQL keeps no CPU total of its own; it is counted from the host, as the "
                f"postmaster's process tree.{host_reason}")
    if no_pools:
        return (f"{edition} keeps no CPU total of its own and no CPU sample was collected."
                f"{host_reason}")
    return ""


def build_workload(rows: list[dict]) -> dict:
    """The Workload block for one server.

    Returns ``{"available": False}`` when the counters have not been collected twice yet. That is
    not a failure and the page says so: two collections 15 minutes apart is the earliest an
    interval can exist, so a freshly onboarded instance has no answer rather than a wrong one.

    ``cpu`` is always returned, available or not: how many CPUs the host has and how many the engine
    may use is a fact of one collection, and the page heads the section with it.
    """
    counter_samples = _samples(rows, COUNTER_CODE)
    os_samples = _samples(rows, OS_CPU_CODE)
    query_samples = _samples(rows, QUERY_STATS_CODE)
    wait_samples = _samples(rows, WAIT_TOTALS_CODE)
    # Resolved before the early return below, and returned from it: a gauge needs **one**
    # collection to say something, while an interval needs two. A freshly onboarded instance has
    # its memory profile half an hour before it has a workload rate, and blanking the first
    # because the second is not ready yet would be the page keeping a fact it already holds.
    memory = _build_memory(_samples(rows, MEMORY_CODE))

    counter_fields = _shared_fields(counter_samples)
    counter_deltas = _deltas(counter_samples, counter_fields)
    query_deltas = _deltas(query_samples, _shared_fields(query_samples), allow_partial=True)
    wait_deltas = _deltas(wait_samples, _shared_fields(wait_samples))
    facts = cpu_facts(counter_samples, os_samples)
    cpu = _build_cpu(counter_samples, counter_deltas, os_samples, facts)

    # A window exists when the counters have one, or - on an engine whose only workload reading is
    # its CPU from the host, PostgreSQL - when the CPU rows do. Each states the span it measured.
    windows = []
    for window in WINDOWS:
        seconds = (counter_deltas.get(window["key"]) or {}).get("seconds")             or cpu["seconds"].get(window["key"])
        if seconds:
            windows.append({"key": window["key"], "label": window["label"], "seconds": seconds})
    as_of = max((s["collected_at"] for s in counter_samples + os_samples), default="")
    if not windows:
        return {
            "available": False,
            "asOf": as_of,
            "samples": len(counter_samples) + len(os_samples),
            "cpu": facts,
            "cpuNote": cpu["note"],
            "memory": memory,
        }

    groups: dict[str, list[dict]] = {}
    # Seeded from the counter rows, not from whichever row is built first. The derived rows are
    # computed before the plain ones (a percentage belongs above the milliseconds it came from),
    # and letting them create their groups put "Physical I/O" above "Requests" — the reverse of
    # the order a workload question is actually worked through.
    order: list[str] = [name for name, _ in _COUNTER_ROWS]

    def add(group: str, row: dict) -> None:
        if group not in groups:
            groups[group] = []
            if group not in order:
                order.append(group)
        groups[group].append(row)

    for row in cpu["rows"]:
        add("CPU", row)

    # Derived rows interleaved into the group they belong to rather than collected in a section of
    # their own: "Read from disk 2.1 GB" belongs beside the read count it came from, not in a
    # separate table the reader has to join by eye.
    for group, key, label, unit, fn in _DERIVED_ROWS:
        values = {}
        for win_key, delta in counter_deltas.items():
            value = fn(delta["deltas"], float(delta["seconds"]), facts.get("online"))
            # A percentage and an average latency are already normalised over the window; a rate
            # per second on top of them would be arithmetic with no meaning.
            cell = _cell(value, float(delta["seconds"]), rate=unit not in ("pct", "ms_avg"))
            if cell:
                values[win_key] = cell
        if values:
            add(group, {"key": key, "label": label, "unit": unit, "values": values})

    for group, specs in _COUNTER_ROWS:
        for key, label, unit in specs:
            if key not in counter_fields:
                continue                # not collected on this build; say nothing rather than 0
            values = {win: cell for win, delta in counter_deltas.items()
                      if (cell := _cell(delta["deltas"].get(key), float(delta["seconds"])))}
            if values:
                add(group, {"key": key, "label": label, "unit": unit, "values": values})

    # The plan cache's version of the same hour. Its own group, because the caveat applies to the
    # whole of it and not to any one row.
    cache_rows = []
    for key, label, unit in _QUERY_ROWS:
        values = {win: cell for win, delta in query_deltas.items()
                  if (cell := _cell(delta["deltas"].get(key), float(delta["seconds"])))}
        if values:
            cache_rows.append({"key": key, "label": label, "unit": unit, "values": values})

    # How much of the instance's CPU the visible plans account for. Far below 100% means the
    # expensive work is not in the cache, and the rows above are a sample of the hour rather than
    # its total — which is exactly when a reader must not tune from them. Taken of an exact CPU
    # total only: a share of an estimate would be a ratio of two approximations, quoted to 0.1%.
    db_ms = next((row["values"] for row in cpu["rows"] if row["key"] == "cpu_usage_ms"), {})
    coverage = {}
    for win_key, delta in query_deltas.items():
        cell = db_ms.get(win_key) or {}
        if cell.get("estimate"):
            continue
        share = _ratio(delta["deltas"].get("query_cpu_ms"), cell.get("total"))
        if share is not None:
            coverage[win_key] = round(share * 100.0, 1)

    latest_query = query_samples[-1] if query_samples else {}
    cache = {
        "rows": cache_rows,
        "coveragePct": coverage,
        "plans": as_float(latest_query.get("cached_plans")),
        "baselineMinutes": as_float(latest_query.get("cache_baseline_minutes")),
        # Which figures lost a plan to eviction inside the window. Named rather than silently
        # blank: "this counter went backwards because the cache dropped a plan" is a different
        # statement from "not collected", and only the first one is normal.
        "evicted": sorted({name for delta in query_deltas.values()
                           for name in delta.get("dropped") or ()}),
    } if cache_rows else {}

    return {
        "available": True,
        "asOf": as_of,
        "countersSince": _latest(counter_samples, "counters_since"),
        # Kept for the readers of the old key (the fleet page): the host's CPU count.
        "cpuCount": facts.get("total"),
        "cpu": facts,
        "cpuNote": cpu["note"],
        "windows": windows,
        "groups": [{"name": name, "rows": groups[name]} for name in order if groups.get(name)],
        "cache": cache,
        "waits": _build_waits(wait_samples, wait_deltas),
        "memory": memory,
        "samples": len(counter_samples),
    }


def _build_memory(samples: list[dict[str, Any]]) -> dict:
    """The Memory block: gauges summarised over the same three windows, never differenced.

    Written on 2026-09-11 because the page had workload, I/O, waits and CPU and no memory at all,
    so every memory question about this estate was answered by running a DMV by hand — and the
    conclusion drawn that way ("page life expectancy is 54 seconds, this instance is short of
    memory") did not survive reading the same counter again.

    Two things stop that happening here. Each cell states the **range** over its window rather than
    a reading, so a value that swung by a factor of three cannot be quoted as the profile of the
    day; and the rows that settle the question — was anything actually made to wait for memory,
    did the pool reach its target — are **above** page life expectancy rather than below it,
    because the order the table is read in is the order the question should be worked through.
    """
    if not samples:
        return {}

    present = {key for sample in samples for key in sample if key not in META_FIELDS}
    fields = sorted(present)
    if not fields:
        return {}

    windows: list[dict[str, Any]] = []
    summaries: dict[str, dict[str, Any]] = {}
    for window in WINDOWS:
        summary = interval_rates.window_gauge(
            samples, fields=fields, window_hours=window["hours"],
            newest_only=bool(window.get("newest_pair")))
        if not summary:
            continue
        summaries[window["key"]] = summary
        windows.append({"key": window["key"], "label": window["label"],
                        "seconds": summary["seconds"], "samples": summary["samples"],
                        "restarted": bool(summary["restarted"])})
    if not windows:
        return {}

    groups = []
    for name, specs in _MEMORY_ROWS:
        rows = []
        for key, label, unit, watch in specs:
            if key not in present:
                continue        # not collected on this build — a non-NUMA box has no node rows
            values = {}
            for win_key, summary in summaries.items():
                reading = summary["readings"].get(key)
                if reading is None:
                    continue
                values[win_key] = {
                    "latest": round(reading["latest"], 2) if reading["latest"] is not None else None,
                    "min": round(reading["min"], 2),
                    "max": round(reading["max"], 2),
                    "avg": round(reading["avg"], 2),
                    "count": reading["count"],
                }
            if values:
                rows.append({"key": key, "label": label, "unit": unit,
                             "watch": watch, "values": values})
        if rows:
            groups.append({"name": name, "rows": rows})
    if not groups:
        return {}

    latest = samples[-1]
    facts = {key: as_float(latest.get(key)) for key in _MEMORY_FACTS
             if as_float(latest.get(key)) is not None}

    # The one comparison worth making for the reader rather than leaving to the eye: a pool at its
    # target is not being squeezed from outside, whatever page life expectancy says.
    total = as_float(latest.get("total_server_memory_kb"))
    target = as_float(latest.get("target_server_memory_kb"))
    at_target = None
    if total is not None and target:
        # Within 1%: the two counters are updated independently and a few MB apart is the normal
        # reading of a pool that has finished growing, not a pool being held back.
        at_target = abs(total - target) / target <= 0.01

    return {
        "available": True,
        "asOf": max((s["collected_at"] for s in samples), default=""),
        "windows": windows,
        "groups": groups,
        "facts": facts,
        "atTarget": at_target,
        "samples": len(samples),
    }


def _build_waits(samples: list[dict[str, Any]], deltas: dict[str, dict[str, Any]]) -> list[dict]:
    """What the instance waited on over each window, worst first by the one-hour reading.

    Reported as seconds and as a share of every non-benign wait, because a wait type is only
    interesting relative to the others: 30 seconds of WRITELOG is nothing on an instance that
    waited an hour in total and the whole story on one that waited 35 seconds.
    """
    if not deltas or not samples:
        return []
    items = sorted({key for key in samples[-1] if key not in META_FIELDS})

    waits = []
    for item in items:
        if item in ("all_waits",):
            continue                      # the denominator, not a row
        values = {}
        for win_key, delta in deltas.items():
            seconds_waited = delta["deltas"].get(item)
            if seconds_waited is None:
                continue
            total = delta["deltas"].get("all_waits")
            values[win_key] = {
                "seconds": round(seconds_waited / 1000.0, 1),
                "sharePct": (round(seconds_waited * 100.0 / total, 1) if total else None),
            }
        # Every watched type is emitted every collection, zero included, so a row that waited for
        # nothing all day would otherwise fill the table with zeros.
        if values and any((v.get("seconds") or 0) > 0 for v in values.values()):
            waits.append({"item": item, "values": values})

    rank_key = "hour" if "hour" in deltas else next(iter(deltas))
    waits.sort(key=lambda w: -((w["values"].get(rank_key) or {}).get("seconds") or 0))
    return waits[:12]
