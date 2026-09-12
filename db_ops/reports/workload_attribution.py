"""Who did the work: the transaction log by database, and the statements behind the plan cache.

The Workload section says *how much* an instance did over an interval — CPU, reads, log flushed,
what it waited on. Asked on 2026-09-11, from one instance's page: WRITELOG was 36% of its waits and
the plan cache had run 2.8 million executions in 15 minutes, and the page could not say which
database wrote the log or which statements did the reading. That is workload *shape* without
workload *attribution*, and this module is the second half.

Two collectors feed it, both raw cumulative totals like everything else under Workload, one row per
entity instead of one per counter:

* ``PERFORMANCE_LOG_BY_DATABASE`` — per database: log bytes flushed, flushes, commits that waited
  for a flush, write transactions, and the log files' own write count and write stall.
* ``PERFORMANCE_TOP_QUERIES`` — per ``query_hash``: executions, CPU, elapsed, logical and physical
  reads, logical writes, the per-execution extremes since the plan was cached, and a snippet.

Each entity is differenced against **its own** earlier sample, over the same three windows the
Workload table uses (:data:`workload.WINDOWS`). The rules that keep it honest are the ones
:mod:`db_ops.lib.interval_rates` applies to the instance counters, plus one that only a per-query
series needs:

* **A query with no earlier sample is not ranked** unless its plans were all cached inside the
  window, in which case its whole total happened there and the total *is* the interval. A query
  that was simply not in the previous sample's top set existed and did work that nobody recorded;
  ranking it by its since-cached total would put a month of history beside an hour of work.
  They are counted, and the page says how many were left out.
* **Nothing here is a percentile.** The collector carries min and max elapsed per execution since
  the plan was cached — a spread. No DMV keeps a distribution, so P95/P99 are not derivable and
  nothing is labelled as one.
* **"Log by procedure" does not exist in the engine.** Logical writes per query is the proxy, and
  the page calls it that.

Only the per-server page reads these codes. They are deliberately not in
:data:`workload.WORKLOAD_CODES`, which the fleet overlay also loads for every server: a row per
statement and per database is far wider than the instance counters, and the fleet page renders none
of it.
"""

from __future__ import annotations

from typing import Any

from db_ops.lib import interval_rates
from db_ops.lib.coerce import as_epoch, as_float
# The windows, the MB constant and the "no reading rather than a number" division are the Workload
# table's own — one definition each, so these blocks cannot drift from the totals they break down.
from db_ops.reports.workload import _MB, WINDOWS, _ratio

LOG_BY_DATABASE_CODE = "PERFORMANCE_LOG_BY_DATABASE"
TOP_QUERIES_CODE = "PERFORMANCE_TOP_QUERIES"
#: What the per-server page fetches beside :data:`workload.WORKLOAD_CODES`.
ATTRIBUTION_CODES = [LOG_BY_DATABASE_CODE, TOP_QUERIES_CODE]

#: The cumulative fields of one database's row, in the order the table reads them.
LOG_FIELDS = ["log_bytes_flushed", "log_flushes", "log_flush_waits", "transactions",
              "write_transactions", "log_writes", "log_bytes_written", "log_write_stall_ms"]

#: The cumulative fields of one query's row. Every one is summed over the query's plans.
QUERY_FIELDS = ["executions", "cpu_ms", "elapsed_ms", "logical_reads", "physical_reads",
                "logical_writes"]

#: ``(field, label)`` — the rankings the page offers, in the order a tuning pass goes through them.
RANKINGS: list[tuple[str, str]] = [
    ("cpu_ms", "CPU"),
    ("elapsed_ms", "Duration"),
    ("logical_reads", "Logical reads"),
    ("physical_reads", "Physical reads"),
    ("logical_writes", "Logical writes"),
    ("executions", "Executions"),
]

#: Rows per ranking. The collector keeps 25 per ranking so that the next sample finds this one's
#: rows to subtract from; the page shows 20.
TOP_N = 20

#: Databases listed per window. Sorted by log volume, so the tail is the databases that wrote
#: nothing worth reading about, and it is summarised rather than listed.
MAX_DATABASES = 25

#: Where the statement snippet starts. The collector writes it as the last field precisely so it
#: can be read this way: a statement contains commas, which end a value for ``message_fields``.
_TEXT_MARKER = ", text="


def _text(message: str) -> str:
    index = message.find(_TEXT_MARKER)
    return message[index + len(_TEXT_MARKER):].strip() if index >= 0 else ""


def _series(rows: list[dict], code: str) -> dict[str, list[dict[str, Any]]]:
    """One code's rows as ``{entity: [sample, ...]}``, oldest first — one series per database or
    per query, which is what makes each of them subtractable on its own."""
    by_item: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if str(row.get("metric_code") or "").upper() != code:
            continue
        item = str(row.get("metric_item") or "").strip()
        stamp = str(row.get("collected_at") or "")
        if not item or not stamp:
            continue                    # an itemless row is a failed collection, not a sample
        message = str(row.get("message") or "")
        fields = interval_rates.message_fields(message)
        sample: dict[str, Any] = {**fields, "collected_at": stamp,
                                  "counters_since": fields.get("counters_since", "")}
        if code == TOP_QUERIES_CODE:
            # message_fields cut it at the statement's first comma; this is the whole snippet.
            sample["text"] = _text(message)
        by_item.setdefault(item, []).append(sample)
    for samples in by_item.values():
        samples.sort(key=lambda s: s["collected_at"])
    return by_item


def _window_delta(samples: list[dict[str, Any]], fields: list[str],
                  window: dict[str, Any]) -> dict[str, Any] | None:
    if window.get("newest_pair"):
        return interval_rates.interval_delta(samples, fields=fields,
                                             max_pair_hours=window["hours"])
    return interval_rates.window_delta(samples, fields=fields, window_hours=window["hours"],
                                       min_fraction=window["min_fraction"])


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


# --------------------------------------------------------------------------------------------- #
# Transaction log, by database
# --------------------------------------------------------------------------------------------- #
def build_log_by_database(rows: list[dict]) -> dict:
    """Per database, how much log it generated over each window and how long writing it took.

    ``available`` is False until some database has two samples — the same "no interval yet" state
    as the Workload table, never a since-boot total shown in its place.
    """
    series = _series(rows, LOG_BY_DATABASE_CODE)
    if not series:
        return {}
    windows: list[dict[str, Any]] = []
    for window in WINDOWS:
        databases: list[dict[str, Any]] = []
        seconds = 0.0
        for name, samples in series.items():
            # The fields this database's newest sample carries. A counter missing on a build is
            # missing from every sample of it, so asking only for these costs nothing honest.
            fields = [f for f in LOG_FIELDS if as_float(samples[-1].get(f)) is not None]
            if "log_bytes_flushed" not in fields:
                continue
            delta = _window_delta(samples, fields, window)
            if not delta:
                continue                # restarted, restored, or new: no honest pair for it
            d = delta["deltas"]
            secs = float(delta["seconds"])
            seconds = max(seconds, secs)
            log_bytes = d.get("log_bytes_flushed") or 0.0
            databases.append({
                "database": name,
                "logMb": _round(log_bytes / _MB),
                "logMbPerSec": _round(log_bytes / _MB / secs if secs else None, 3),
                "flushes": d.get("log_flushes"),
                "avgFlushKb": _round(_ratio(log_bytes / 1024.0, d.get("log_flushes"))),
                # Commits that had to wait for their log block to reach disk — the per-database
                # count behind the WRITELOG wait.
                "flushWaits": d.get("log_flush_waits"),
                "writeTransactions": d.get("write_transactions"),
                "transactions": d.get("transactions"),
                # Log per committed write transaction: a few KB is an OLTP workload committing
                # row by row, hundreds of KB is batches, and the tuning differs completely.
                "avgTxnKb": _round(_ratio(log_bytes / 1024.0, d.get("write_transactions"))),
                "logWrites": d.get("log_writes"),
                # Three decimals: a quiet database's 23 ms of stall rounded to one decimal read as
                # "0 s" beside a latency of 1.77 ms, which is two cells contradicting each other.
                "writeStallSec": _round((d.get("log_write_stall_ms") or 0.0) / 1000.0, 3)
                if "log_write_stall_ms" in d else None,
                # The latency a committing session waits on. Divided over this window's writes,
                # not over the uptime — the difference PERFORMANCE_IO_LATENCY cannot make.
                "writeLatencyMs": _round(_ratio(d.get("log_write_stall_ms"), d.get("log_writes"))),
            })
        if not databases:
            continue
        total_bytes = sum((db["logMb"] or 0.0) for db in databases)
        total_stall = sum((db["writeStallSec"] or 0.0) for db in databases)
        for db in databases:
            db["sharePct"] = _round(_ratio((db["logMb"] or 0.0) * 100.0, total_bytes), 1)
            db["stallSharePct"] = _round(_ratio((db["writeStallSec"] or 0.0) * 100.0, total_stall), 1)
        databases.sort(key=lambda db: -(db["logMb"] or 0.0))
        shown, rest = databases[:MAX_DATABASES], databases[MAX_DATABASES:]
        windows.append({
            "key": window["key"], "label": window["label"], "seconds": round(seconds, 1),
            "databases": shown,
            "others": {"count": len(rest), "logMb": _round(sum(db["logMb"] or 0.0 for db in rest))}
            if rest else None,
            "totals": {"logMb": _round(total_bytes), "writeStallSec": _round(total_stall, 1),
                       "databases": len(databases)},
        })
    newest = max((s["collected_at"] for samples in series.values() for s in samples), default="")
    if not windows:
        return {"available": False, "asOf": newest,
                "samples": max((len(s) for s in series.values()), default=0)}
    return {"available": True, "asOf": newest, "windows": windows}


# --------------------------------------------------------------------------------------------- #
# Top queries
# --------------------------------------------------------------------------------------------- #
def _stamps(series: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, dict[str, Any]]]:
    """``{collected_at: {query_hash: sample}}`` — one collection is one ranking of the cache."""
    by_stamp: dict[str, dict[str, dict[str, Any]]] = {}
    for query_hash, samples in series.items():
        for sample in samples:
            by_stamp.setdefault(sample["collected_at"], {})[query_hash] = sample
    return by_stamp


def _partner(stamps: list[str], markers: dict[str, str], window: dict[str, Any]) -> str | None:
    """The collection the newest one is subtracted from, by the rules of
    :func:`interval_rates.interval_delta` / :func:`interval_rates.window_delta` applied to whole
    collections: never across a restart, never wider than the window, and — for the hour and the
    day — the widest pair that is at least ``min_fraction`` of the window."""
    newest = stamps[-1]
    newest_epoch = as_epoch(newest)
    if newest_epoch is None:
        return None
    max_seconds = window["hours"] * 3600
    candidates = []
    for stamp in stamps[:-1]:
        epoch = as_epoch(stamp)
        if epoch is None:
            continue
        span = newest_epoch - epoch
        if span <= 0 or span > max_seconds:
            continue
        if markers.get(stamp) and markers.get(newest) and markers[stamp] != markers[newest]:
            continue                    # an engine restart between them
        candidates.append((span, stamp))
    if not candidates:
        return None
    if window.get("newest_pair"):
        return min(candidates)[1]
    floor = max_seconds * window["min_fraction"]
    wide = [c for c in candidates if c[0] >= floor]
    return max(wide)[1] if wide else None


def build_top_queries(rows: list[dict], *,
                      instance_cpu_ms_per_sec: dict[str, float] | None = None) -> dict:
    """The statements behind the plan cache's work, ranked six ways over each window.

    ``instance_cpu_ms_per_sec`` is the Workload table's CPU rate per window, from the resource
    pools. A query's share is computed as a ratio of the two *rates*, because the query samples
    (every 30 minutes) and the counter samples (every 15) are not taken at the same instants, and
    two rates over nearly the same hour compare where two totals over different spans would not.
    """
    series = _series(rows, TOP_QUERIES_CODE)
    if not series:
        return {}
    by_stamp = _stamps(series)
    stamps = sorted(by_stamp)
    markers = {stamp: next((s.get("counters_since") or "" for s in by_stamp[stamp].values()), "")
               for stamp in stamps}
    newest = stamps[-1]
    current = by_stamp[newest]
    instance_cpu = instance_cpu_ms_per_sec or {}

    windows: list[dict[str, Any]] = []
    for window in WINDOWS:
        partner = _partner(stamps, markers, window)
        if partner is None:
            continue
        seconds = (as_epoch(newest) or 0.0) - (as_epoch(partner) or 0.0)
        partner_epoch = as_epoch(partner) or 0.0
        previous = by_stamp[partner]

        candidates: list[dict[str, Any]] = []
        unranked = 0
        for query_hash, now in current.items():
            totals = {f: as_float(now.get(f)) for f in QUERY_FIELDS}
            if any(v is None for v in totals.values()):
                continue
            first_cached = as_epoch(now.get("first_cached_utc"))
            before = previous.get(query_hash)
            if first_cached is not None and first_cached >= partner_epoch:
                # Every plan of this statement was cached inside the window, so its whole total
                # was earned there. Also right when it *was* in the earlier sample: those totals
                # belonged to plans that have since been evicted and recompiled.
                delta, basis = totals, "cached_in_window"
            elif before is not None:
                earlier = {f: as_float(before.get(f)) for f in QUERY_FIELDS}
                if any(v is None for v in earlier.values()):
                    unranked += 1
                    continue
                delta = {f: totals[f] - earlier[f] for f in QUERY_FIELDS}
                if any(v < 0 for v in delta.values()):
                    # One of its plans was evicted and the remaining total fell below the old one:
                    # what it did in the window is unknowable, not negative.
                    unranked += 1
                    continue
                basis = "delta"
            else:
                unranked += 1           # existed, was not in the earlier ranking: no baseline
                continue
            if not delta["executions"]:
                continue                # cached, but did nothing in this window
            candidates.append({"hash": query_hash, "now": now, "delta": delta, "basis": basis})

        ranks: dict[str, dict[str, int]] = {}
        for field, _label in RANKINGS:
            ordered = sorted((c for c in candidates if c["delta"][field] > 0),
                             key=lambda c: -c["delta"][field])[:TOP_N]
            for position, cand in enumerate(ordered, start=1):
                ranks.setdefault(cand["hash"], {})[field] = position

        rate = instance_cpu.get(window["key"])
        queries = []
        for cand in candidates:
            if cand["hash"] not in ranks:
                continue
            d, now = cand["delta"], cand["now"]
            executions = d["executions"]
            cpu_rate = d["cpu_ms"] / seconds if seconds else None
            queries.append({
                "hash": cand["hash"],
                "database": now.get("db_name") or "",
                "object": now.get("object_name") or "",
                "text": now.get("text") or "",
                "basis": cand["basis"],
                "ranks": ranks[cand["hash"]],
                "executions": executions,
                "cpuMs": _round(d["cpu_ms"], 1),
                "elapsedMs": _round(d["elapsed_ms"], 1),
                "logicalReads": d["logical_reads"],
                "physicalReads": d["physical_reads"],
                "logicalWrites": d["logical_writes"],
                "avgCpuMs": _round(_ratio(d["cpu_ms"], executions), 3),
                "avgElapsedMs": _round(_ratio(d["elapsed_ms"], executions), 3),
                "avgLogicalReads": _round(_ratio(d["logical_reads"], executions), 1),
                # Since the plan was cached, not over the window: the collector can only see the
                # extremes the engine kept. A spread, never a percentile.
                "minElapsedMs": as_float(now.get("min_elapsed_ms")),
                "maxElapsedMs": as_float(now.get("max_elapsed_ms")),
                "plans": as_float(now.get("plans")),
                "firstCached": now.get("first_cached_utc") or "",
                "lastExecution": now.get("last_execution_utc") or "",
                "cpuSharePct": _round(_ratio((cpu_rate or 0.0) * 100.0, rate), 1)
                if cpu_rate is not None and rate else None,
            })
        queries.sort(key=lambda q: -(q["cpuMs"] or 0.0))
        windows.append({
            "key": window["key"], "label": window["label"], "seconds": round(seconds, 1),
            "queries": queries, "unranked": unranked, "considered": len(current),
        })

    if not windows:
        return {"available": False, "asOf": newest, "samples": len(stamps)}
    return {
        "available": True,
        "asOf": newest,
        "rankings": [{"key": field, "label": label} for field, label in RANKINGS],
        "topN": TOP_N,
        "windows": windows,
    }


def instance_cpu_rates(workload: dict) -> dict[str, float]:
    """The Workload table's CPU per window, as ms per second — what a query's share is taken of."""
    rates: dict[str, float] = {}
    for group in (workload or {}).get("groups") or []:
        for row in group.get("rows") or []:
            if row.get("key") != "cpu_usage_ms":
                continue
            for win_key, cell in (row.get("values") or {}).items():
                value = as_float((cell or {}).get("perSecond"))
                if value:
                    rates[win_key] = value
    return rates


def build_attribution(rows: list[dict], *, workload: dict | None = None) -> dict:
    """Both blocks for one server. Each is empty when its metric has no rows — an Oracle or
    PostgreSQL page, or an instance where the metric is switched off, renders nothing."""
    return {
        "log": build_log_by_database(rows),
        "queries": build_top_queries(rows, instance_cpu_ms_per_sec=instance_cpu_rates(workload or {})),
    }
