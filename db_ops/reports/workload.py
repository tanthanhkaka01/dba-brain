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
in :mod:`db_ops.lib.interval_rates`; what belongs here is which counters exist, what they are
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

from db_ops.lib import interval_rates
from db_ops.lib.coerce import as_float

#: What the builder reads. Also the list the reports fetch from the store — declared once here so a
#: page cannot ask for a code this module does not know how to render.
COUNTER_CODE = "PERFORMANCE_WORKLOAD_COUNTERS"
QUERY_STATS_CODE = "PERFORMANCE_QUERY_STATS_TOTALS"
WAIT_TOTALS_CODE = "PERFORMANCE_WAIT_TOTALS"
WORKLOAD_CODES = [COUNTER_CODE, QUERY_STATS_CODE, WAIT_TOTALS_CODE]

#: Items that are a reading of the moment rather than a running total. Differencing one produces a
#: number that looks like a rate and means nothing, so they are carried through as-is.
GAUGE_ITEMS = {"cached_plans", "cache_baseline_minutes"}

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
    ("CPU", [
        ("cpu_usage_ms", "CPU time", "ms"),
    ]),
    ("Requests", [
        ("batch_requests", "Batch requests", "count"),
        ("transactions", "Transactions", "count"),
        ("sql_compilations", "Compilations", "count"),
        ("sql_recompilations", "Recompilations", "count"),
        ("logins", "Logins", "count"),
    ]),
    ("Logical work", [
        # The instance-wide equivalent of total_logical_reads, and the one that misses nothing:
        # unlike the plan cache it counts uncached and evicted work too.
        ("page_lookups", "Logical reads (page lookups)", "pages"),
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
    ("CPU", "cpu_pct", "CPU used", "pct",
     # Milliseconds of CPU over a period of wall-clock seconds on a known number of cores. This is
     # the single number the raw ms figure is unreadable without: 30,000,000 ms in an hour is every
     # core of an 8-way box and 8% of a 100-way one.
     lambda d, secs, cpus: _ratio(d.get("cpu_usage_ms"), secs * (cpus or 0) * 10.0)),
    ("Physical I/O", "io_mb_read", "Read from disk", "mb",
     lambda d, secs, cpus: _ratio(d.get("io_bytes_read"), _MB)),
    ("Physical I/O", "io_mb_written", "Written to disk", "mb",
     lambda d, secs, cpus: _ratio(d.get("io_bytes_written"), _MB)),
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
        entry = by_stamp.setdefault(stamp, {"collected_at": stamp})
        entry[item] = row.get("metric_value")
        fields = interval_rates.message_fields(row.get("message"))
        if not entry.get("counters_since"):
            entry["counters_since"] = fields.get("counters_since", "")
        if not entry.get("cpu_count"):
            entry["cpu_count"] = fields.get("cpu_count", "")
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
    meta = {"collected_at", "counters_since", "cpu_count"}
    common: set[str] | None = None
    for sample in samples:
        present = {k for k in sample if k not in meta and k not in GAUGE_ITEMS}
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


def _cpu_count(samples: list[dict[str, Any]]) -> float | None:
    for sample in reversed(samples):
        value = as_float(sample.get("cpu_count"))
        if value:
            return value
    return None


def build_workload(rows: list[dict]) -> dict:
    """The Workload block for one server.

    Returns ``{"available": False}`` when the counters have not been collected twice yet. That is
    not a failure and the page says so: two collections 15 minutes apart is the earliest an
    interval can exist, so a freshly onboarded instance has no answer rather than a wrong one.
    """
    counter_samples = _samples(rows, COUNTER_CODE)
    query_samples = _samples(rows, QUERY_STATS_CODE)
    wait_samples = _samples(rows, WAIT_TOTALS_CODE)

    counter_fields = _shared_fields(counter_samples)
    counter_deltas = _deltas(counter_samples, counter_fields)
    query_deltas = _deltas(query_samples, _shared_fields(query_samples), allow_partial=True)
    wait_deltas = _deltas(wait_samples, _shared_fields(wait_samples))

    windows = [
        {"key": w["key"], "label": w["label"], "seconds": counter_deltas[w["key"]]["seconds"]}
        for w in WINDOWS if w["key"] in counter_deltas
    ]
    if not windows:
        return {
            "available": False,
            "asOf": max((s["collected_at"] for s in counter_samples), default=""),
            "samples": len(counter_samples),
        }

    cpus = _cpu_count(counter_samples)
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

    # CPU first, and derived rows interleaved into the group they belong to rather than collected
    # in a section of their own: "Read from disk 2.1 GB" belongs beside the read count it came
    # from, not in a separate table the reader has to join by eye.
    for group, key, label, unit, fn in _DERIVED_ROWS:
        values = {}
        for win_key, delta in counter_deltas.items():
            value = fn(delta["deltas"], float(delta["seconds"]), cpus)
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
    # its total — which is exactly when a reader must not tune from them.
    coverage = {}
    for win_key, delta in query_deltas.items():
        pool = counter_deltas.get(win_key, {}).get("deltas", {}).get("cpu_usage_ms")
        share = _ratio(delta["deltas"].get("query_cpu_ms"), pool)
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
        "asOf": max((s["collected_at"] for s in counter_samples), default=""),
        "countersSince": str(counter_samples[-1].get("counters_since") or ""),
        "cpuCount": cpus,
        "windows": windows,
        "groups": [{"name": name, "rows": groups[name]} for name in order if groups.get(name)],
        "cache": cache,
        "waits": _build_waits(wait_samples, wait_deltas),
        "samples": len(counter_samples),
    }


def _build_waits(samples: list[dict[str, Any]], deltas: dict[str, dict[str, Any]]) -> list[dict]:
    """What the instance waited on over each window, worst first by the one-hour reading.

    Reported as seconds and as a share of every non-benign wait, because a wait type is only
    interesting relative to the others: 30 seconds of WRITELOG is nothing on an instance that
    waited an hour in total and the whole story on one that waited 35 seconds.
    """
    if not deltas or not samples:
        return []
    items = sorted({key for key in samples[-1]
                    if key not in ("collected_at", "counters_since", "cpu_count")})

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
