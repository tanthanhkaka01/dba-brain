"""Reading a collector's message, and differencing two of them into a rate.

A metric row carries one derived number in ``metric_value``; everything else the collector measured
survives only as ``key=value`` pairs inside ``message``. The report re-reads them from there, so
the parser is shared rather than copied — a page that split those fields differently from the
collector that wrote them would disagree with the alert about the same sample.

The second half of this module is the subtraction. SQL Server's throughput, I/O and wait counters
are totals since the engine started, so a single reading answers "what has this instance done since
it booted" — a question nobody asks — and dividing one by the uptime gives an average over months.
On one production instance the engine had been up since 2025-10-27, so a 95.97 ms "current" write latency
was in fact nine months of history: one bad afternoon last winter keeps a tile red forever, and a
problem starting this morning is diluted to invisibility by every good hour behind it.

A rate needs two samples and the difference between them. The store already keeps every sample, so
the only thing missing was the arithmetic — and the three rules that make it honest:

* **A counter that went backwards is a restart, not negative work.** The pair is dropped rather
  than clamped: after a restart the new total starts from zero, so the "delta" across it is the new
  absolute value and would read as a burst of activity that never happened. ``counters_since`` is
  carried in the collector message for exactly this, because a busy instance can pass its previous
  totals within the hour and then nothing about the values themselves says the baseline moved.
* **Two samples too far apart describe an average, not a moment.** A delta over 20 hours is not
  "now"; it is the same lie in a shorter timeframe. Pairs wider than the requested window are
  refused rather than stretched.
* **No pair means no answer.** The caller says "no interval data yet" and must never fall back to
  the cumulative average, which is the very thing this exists to replace.

**Gauges take the other arithmetic, and it is in here too.** ``window_gauge`` summarises several
readings of a *current-state* counter — buffer pool size, memory grants pending, page life
expectancy — into latest / lowest / highest over the same windows. It is not a variant of the
subtraction and must never be mistaken for one: differencing a gauge produces a figure that looks
like a rate and means nothing, and printing one reading of a gauge invites the conclusion that a
single instant is the instance's profile. Both mistakes were made about the same instance on
2026-09-11, which is why the two functions sit here under names that say which is which.

This arithmetic lived here until 2026-08-11 and was withdrawn with the rest of
``PERFORMANCE_IO_LATENCY``'s bespoke *grading* path — per-metric machinery in the collector is what
this codebase does not do. It comes back on the other side of that line: nothing here grades
anything, no collector calls it, and no metric's severity depends on it. It is a report reading
counters that ``PERFORMANCE_WORKLOAD_COUNTERS``, ``PERFORMANCE_QUERY_STATS_TOTALS`` and
``PERFORMANCE_WAIT_TOTALS`` record raw and deliberately leave ungraded.
"""

from __future__ import annotations

import re
from typing import Any

from db_ops.lib.coerce import as_epoch, as_float


#: The ``key=value`` pairs a collector message is built from. Anchored on a comma or whitespace so
#: a value containing ``=`` (a Windows path, an error text) cannot be mistaken for the next key.
_FIELD_RE = re.compile(r"(?:^|[,\s])([A-Za-z_][A-Za-z0-9_]*)=([^,]*)")

#: Widest gap between two samples that still describes "recently" rather than an average, when the
#: caller does not name one. Three hours is twelve collections at the 900-second cadence these
#: counters run on, which is enough slack for a worker that skipped a cycle and not enough to blur
#: a morning into an afternoon.
MAX_PAIR_HOURS = 3.0


def message_fields(message: Any) -> dict[str, str]:
    """The structured fields a collector carried in its message text, keys lower-cased.

    Shared with the report so the page and the alert read the same sample the same way.
    """
    return {
        key.lower(): value.strip()
        for key, value in _FIELD_RE.findall(str(message or ""))
    }


def _usable(samples: list[dict[str, Any]], fields: list[str]) -> list[tuple[float, str, dict]]:
    """``samples`` reduced to the ones that can take part in a subtraction, oldest first.

    A sample missing any requested field is dropped whole rather than defaulted to zero: a
    collection where one counter failed would otherwise show as that counter falling to nothing and
    then leaping back, which reads as a restart on one row and a spike on the next.
    """
    usable = []
    for sample in samples:
        stamp = as_epoch(sample.get("collected_at"))
        if stamp is None:
            continue
        values = {name: as_float(sample.get(name)) for name in fields}
        if any(value is None for value in values.values()):
            continue
        usable.append((stamp, str(sample.get("counters_since") or ""), values))
    usable.sort(key=lambda item: item[0])
    return usable


def _pair(newest: tuple, older: tuple, fields: list[str], max_seconds: float, *,
          allow_partial: bool = False) -> dict | None:
    """The two samples differenced, or ``None`` if this pair cannot honestly be subtracted."""
    seconds = newest[0] - older[0]
    if seconds <= 0 or seconds > max_seconds:
        return None
    # An explicit restart marker beats inferring one from the numbers: after a restart a busy
    # instance can pass its previous totals within the hour, and then nothing about the values
    # themselves says the baseline moved.
    if newest[1] and older[1] and newest[1] != older[1]:
        return None
    deltas = {name: newest[2][name] - older[2][name] for name in fields}
    dropped = sorted(name for name, value in deltas.items() if value < 0)
    if dropped:
        # A counter that went backwards means its baseline moved. For an engine counter that is a
        # reset of *all* of them — a restart, or DBCC SQLPERF — so the pair is refused whole.
        #
        # `allow_partial` is for the one source where a baseline moves per row rather than per
        # instance: sys.dm_exec_query_stats loses a plan's totals the moment that plan is evicted,
        # continuously and by design. Measured on a production instance on 2026-09-03, six minutes apart:
        # query_physical_reads fell from 603,890 to 603,681 while logical reads rose by 220
        # million. Refusing that pair blanked all six plan-cache figures because one of them lost a
        # plan — and the five that survived were exactly as meaningful as they ever are, since
        # every one of them is an undercount of unknown size to begin with. So the field that went
        # backwards is dropped and named in `dropped`; the caller says so rather than implying the
        # rest are complete.
        if not allow_partial:
            return None
        deltas = {name: value for name, value in deltas.items() if value >= 0}
        if not deltas:
            return None                 # nothing survived: this is a reset, not an eviction
    return {
        "seconds": round(seconds, 1),
        "from": older[0],
        "to": newest[0],
        "deltas": deltas,
        "dropped": dropped,
        "counters_since": newest[1],
    }


def interval_delta(samples: list[dict[str, Any]], *, fields: list[str],
                   max_pair_hours: float = MAX_PAIR_HOURS,
                   allow_partial: bool = False) -> dict[str, Any] | None:
    """Difference the two newest usable samples — "what is happening right now".

    ``samples`` is ``[{"collected_at": ..., "counters_since": ..., <field>: ...}]``. Walks backwards
    from the newest sample and takes the first partner that can honestly be subtracted, so a single
    bad collection costs precision and not the answer. Returns ``None`` when no such pair exists.
    """
    usable = _usable(samples, fields)
    if len(usable) < 2:
        return None
    newest = usable[-1]
    max_seconds = max_pair_hours * 3600
    for older in reversed(usable[:-1]):
        if newest[0] - older[0] > max_seconds:
            break                       # everything further back is older still
        found = _pair(newest, older, fields, max_seconds, allow_partial=allow_partial)
        if found is not None:
            return found
    return None


def window_delta(samples: list[dict[str, Any]], *, fields: list[str], window_hours: float,
                 min_fraction: float = 0.5,
                 allow_partial: bool = False) -> dict[str, Any] | None:
    """Difference the newest sample against the *oldest* one still inside ``window_hours``.

    This is the "how much did this instance do in the last hour" reading, and it deliberately
    reaches for the widest honest pair rather than the nearest: an hour asked for and a 15-minute
    delta answered is a four-fold understatement of everything on the page.

    ``min_fraction`` is what stops the opposite error. A window whose only partner is two samples
    old covers 30 minutes, and calling that "the last hour" is the averaging mistake this module
    exists to prevent — so a span shorter than that fraction of the window is refused. The span
    actually used is returned in ``seconds``; a caller that labels the figure should label it from
    there, not from what it asked for.
    """
    usable = _usable(samples, fields)
    if len(usable) < 2:
        return None
    newest = usable[-1]
    max_seconds = window_hours * 3600
    floor_seconds = max_seconds * min_fraction
    for older in usable[:-1]:           # oldest first: the first that works is the widest span
        found = _pair(newest, older, fields, max_seconds, allow_partial=allow_partial)
        if found is not None and found["seconds"] >= floor_seconds:
            return found
    return None


def per_second(delta: dict[str, Any] | None) -> dict[str, float]:
    """``{field: units per second}`` for a delta, empty when there is no delta.

    The rate is computed against the *measured* span, never against the cadence the metric is
    configured at: a worker that ran late by a minute would otherwise inflate every rate on the
    page by that minute's worth.
    """
    if not delta or not delta.get("seconds"):
        return {}
    seconds = float(delta["seconds"])
    return {name: value / seconds for name, value in delta["deltas"].items()}


def io_latency_interval(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Read/write latency over the last interval, in ms, from the raw stall counters.

    Returns ``None`` when there is no honest pair. A file with no I/O in the interval reports
    ``None`` latency rather than 0 — "nothing happened" is not "instant".
    """
    result = interval_delta(
        samples, fields=["reads", "writes", "io_stall_read_ms", "io_stall_write_ms"])
    if result is None:
        return None
    deltas = result["deltas"]
    reads, writes = deltas["reads"], deltas["writes"]
    return {
        "seconds": result["seconds"],
        "reads": int(reads),
        "writes": int(writes),
        "readLatencyMs": round(deltas["io_stall_read_ms"] / reads, 2) if reads > 0 else None,
        "writeLatencyMs": round(deltas["io_stall_write_ms"] / writes, 2) if writes > 0 else None,
        "readIops": round(reads / result["seconds"], 1) if result["seconds"] else None,
        "writeIops": round(writes / result["seconds"], 1) if result["seconds"] else None,
        "countersSince": result["counters_since"],
    }


def window_gauge(samples: list[dict[str, Any]], *, fields: list[str], window_hours: float,
                 newest_only: bool = False) -> dict[str, Any] | None:
    """Summarise **gauge** readings over a window: latest, lowest, highest and mean.

    The counterpart to :func:`window_delta`, and deliberately not a variant of it. A cumulative
    counter only means something once two readings are subtracted; a gauge means something on its
    own and means nothing at all subtracted — the difference between two page-life-expectancy
    readings is a number that looks like a rate and is not one. Which arithmetic a metric takes is
    a property of the metric, so the two live side by side under names that say which is which.

    **Why a range and not a reading.** Asked on 2026-09-11, when an instance was called short of
    memory because page life expectancy read 54 seconds — and the same counter read 447 shortly
    afterwards, with its five NUMA nodes spread over 378 to 721. Neither number was wrong and
    neither was the instance's memory profile; a gauge sampled once is the value at one instant,
    and a page that prints one instant invites exactly that conclusion. ``min`` and ``max`` are
    what make the reader see the spread, and for most of these rows one of the two ends is the
    finding: the lowest page life expectancy, the highest number of pending memory grants.

    ``newest_only`` is the "latest interval" column — the most recent reading alone, where min,
    max and mean are all that one sample and the span is zero. It is a column about *now* and
    stating a range for it would be inventing one.

    ``restarted`` marks a window whose samples do not all share a ``counters_since``. A gauge
    survives a restart in a way a counter does not, so the window is still reported — but page
    life expectancy starts near zero after one and climbs, so a minimum drawn from across a
    restart is the restart rather than memory pressure, and the page has to be able to say so.

    Returns ``None`` when the window holds no reading of any requested field. The mean is over
    samples, not weighted by time: these gauges are collected on a fixed cadence, so the two agree
    except where collections were missed — and a window that missed collections reports the span
    it actually covered, which is the honest place for that to show.
    """
    stamped: list[tuple[float, str, dict[str, Any]]] = []
    for sample in samples:
        stamp = as_epoch(sample.get("collected_at"))
        if stamp is None:
            continue
        stamped.append((stamp, str(sample.get("counters_since") or ""), sample))
    if not stamped:
        return None
    stamped.sort(key=lambda item: item[0])

    newest_stamp = stamped[-1][0]
    if newest_only:
        inside = stamped[-1:]
    else:
        floor = newest_stamp - window_hours * 3600
        inside = [item for item in stamped if item[0] >= floor]
    if not inside:
        return None

    readings: dict[str, dict[str, Any]] = {}
    for name in fields:
        # Per field, not per sample: a collection where one item failed costs that item's reading
        # and not the whole window. `window_delta` drops the sample whole for the opposite reason —
        # a counter missing from one end of a subtraction has no difference at all.
        values = [as_float(item[2].get(name)) for item in inside]
        values = [value for value in values if value is not None]
        if not values:
            continue
        latest = next((as_float(item[2].get(name)) for item in reversed(inside)
                       if as_float(item[2].get(name)) is not None), None)
        readings[name] = {
            "latest": latest,
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
            "count": len(values),
        }
    if not readings:
        return None

    markers = {item[1] for item in inside if item[1]}
    return {
        "seconds": round(newest_stamp - inside[0][0], 1),
        "from": inside[0][0],
        "to": newest_stamp,
        "samples": len(inside),
        "readings": readings,
        "restarted": len(markers) > 1,
        "counters_since": stamped[-1][1],
    }
