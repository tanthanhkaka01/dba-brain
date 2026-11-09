"""The report-judged metrics, judged again over 15 minutes, an hour and a day of stored rows.

Two metrics are graded by the report and not by the collector (``health_model.REPORT_JUDGED_CODES``):
``PERFORMANCE_IO_LATENCY`` and ``PAGE_LIFE_EXPECTANCY``. Until 2026-10-26 the report graded the one
row of each metric's newest collection, and for both that row is the wrong question:

* **Disk latency** is ``sys.dm_io_virtual_file_stats`` divided out - the average **since the engine
  started**. On 2026-10-26 the server page of a production SQL Server put a data file at CRITICAL
  72.95 ms, its average since a restart eight days earlier; the same file's counters, differenced
  across the rows the store already kept, read 17.5 ms over the last 24 hours and 2.3 ms over the
  last hour. The stall that made the 72.95 was over, and the page could not say so.
* **Page life expectancy** is one reading of a gauge, taken every ten minutes. A single large scan
  drops it to near zero for a few minutes, and a page that grades that instant calls a server
  short of memory on the strength of one query.

The operator's rule (2026-10-26) draws the line this module sits on: **the collector and the alert
reports alert on the instant and never read the store; the report pages (the inventory and the
server page) read only the store, and may judge a metric differently from the alert.** So nothing
here changes what a collector grades or what an alert says. It takes the rows a report page has
already loaded and reads them over three windows - the latest interval, the last hour and the last
24 hours, the same three the Workload section uses - and **grades on the last hour**: long enough
that one checkpoint or one scan does not decide it, short enough to be about today. The other two
windows are printed beside it, so a spike that is over and a slow day that is not both show.

The arithmetic is :mod:`dbabrain.lib.interval_rates`, with its rules: a pair across a restart is
refused, a window that could not be filled is labelled with the span it covered, and **no pair
means no answer** - never the since-boot average, which is printed for what it is and not graded.

Engine-neutral by its fields: a collector that writes ``reads``, ``writes``, ``io_stall_read_ms``,
``io_stall_write_ms`` and ``counters_since`` into its message gets windows. SQL Server has from the
start; Oracle's two variants write them from 2026-10-26.

**The metrics the collector grades from one sample** - CPU, the disk queue, the wait sample - are
read the same way the same day, with one difference that matters: their thresholds are the
collector's, per metric and per server (``metric_overrides``, ``severity_map``), and a copy of them
here would drift. So a page does not re-grade their values; it reads the severity **the collector
gave each stored sample** and keeps the one that held for **at least half of the last hour** - and
never on one sample of several (an hourly metric's hour holds two). One
1-second CPU reading at 95 % is a moment; half an hour of them is the server. The value printed is
the hour's average, with its peak and the count of samples that were not OK. The wait sample names
a different top wait in each collection, so it is read per collection, across its items, and can
only lower a finding that the hour did not sustain - it has no item of its own to raise.
"""

from __future__ import annotations

from typing import Any

from dbabrain.lib import health_model, interval_rates
from dbabrain.lib.coerce import as_epoch, as_float

IO_LATENCY_CODE = "PERFORMANCE_IO_LATENCY"
PLE_CODE = "PAGE_LIFE_EXPECTANCY"
#: The metrics a report page judges from a window of stored rows instead of the newest row.
WINDOWED_CODES = (IO_LATENCY_CODE, PLE_CODE)
#: Collector-graded items judged by the severity that held over the hour (``held_severity``).
SUSTAINED_ITEMS = frozenset({
    ("OS_CPU_USAGE", "cpu_usage"),
    ("SYSTEM_CPU_MEMORY", "cpu"),
    ("OS_DISK_USAGE", "disk_queue_length"),
})
#: The same, for items named after something of the estate's: a container's CPU share is
#: ``<container>:cpu_share``, and the container's name is the operator's.
SUSTAINED_SUFFIXES = (("DOCKER_CONTAINER_STATS", ":cpu_share"),)
WAIT_SAMPLE_CODE = "PERFORMANCE_WAIT_STATS"
#: Collector-graded metrics whose items change from one collection to the next: judged per
#: collection, across every item of the code.
SAMPLED_CODES = (WAIT_SAMPLE_CODE,)
#: Every code a page loads history for, to judge any of the above.
HISTORY_CODES = (*WINDOWED_CODES,
                 *sorted({code for code, _item in SUSTAINED_ITEMS}
                         | {code for code, _suffix in SUSTAINED_SUFFIXES}),
                 *SAMPLED_CODES)
#: The item key under which a sampled code's rows are grouped across all its items.
ALL_ITEMS = "*"

#: The three readings, in the order printed. ``newest_pair`` is the latest collection interval
#: (15 minutes at the I/O metric's cadence); the others reach for the widest honest pair inside
#: their span and refuse one shorter than half of it (``interval_rates.window_delta``).
WINDOWS: tuple[dict[str, Any], ...] = (
    {"key": "now", "label": "latest interval", "hours": 1.0, "newest_pair": True},
    {"key": "hour", "label": "last hour", "hours": 1.0, "min_fraction": 0.5},
    {"key": "day", "label": "last 24 hours", "hours": 24.0, "min_fraction": 0.5},
)
#: The window a severity is taken from. The latest interval answers when there is no hour yet -
#: the first half hour after a restart - because a fresh interval is still about now.
GRADED_WINDOW = "hour"

#: How much history a page loads to fill the widest window. A day reaches back 24 hours exactly,
#: and ``window_delta`` accepts the oldest sample inside it.
HISTORY_DAYS = 1

IO_FIELDS = ["reads", "writes", "io_stall_read_ms", "io_stall_write_ms"]
#: Fewer I/Os than this in a window and that direction is not graded: three reads that waited on a
#: cold disk are not a latency, they are three reads. The interval collector's own gate
#: (``@min_ios`` in ``080_sqlserver_io_latency_interval.sql``).
MIN_IOS = 20

#: The report rule, unchanged - only the figure it is applied to changed.
IO_THRESHOLDS = "WARN ≥ 20 ms · CRITICAL ≥ 50 ms"
PLE_THRESHOLDS = "WARN < 2000 s · CRITICAL < 300 s"


def _field(row: Any, key: str) -> Any:
    """A column of a store row - a dict or a database row, which answer ``[]`` the same way."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _span(seconds: float | None) -> str:
    """The span a reading actually covered, as a reader says it."""
    if not seconds:
        return ""
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{round(minutes)} min"
    hours = minutes / 60.0
    return f"{hours:.0f} h" if abs(hours - round(hours)) < 0.05 else f"{hours:.1f} h"


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f} ms"


# --------------------------------------------------------------------------- #
# Disk latency - a cumulative counter, differenced
# --------------------------------------------------------------------------- #
def io_samples(rows: list[Any]) -> list[dict[str, Any]]:
    """The counters each stored row carried in its message, with the row's collection time."""
    samples = []
    for row in rows:
        fields = interval_rates.message_fields(_field(row, "message"))
        fields["collected_at"] = _field(row, "collected_at")
        samples.append(fields)
    return samples


def _latency(delta: dict[str, Any] | None) -> dict[str, Any] | None:
    if delta is None:
        return None
    deltas = delta["deltas"]
    reads, writes = deltas["reads"], deltas["writes"]
    return {
        "seconds": delta["seconds"],
        "span": _span(delta["seconds"]),
        "reads": int(reads),
        "writes": int(writes),
        # No I/O is not zero latency: it is no reading.
        "readMs": round(deltas["io_stall_read_ms"] / reads, 2) if reads > 0 else None,
        "writeMs": round(deltas["io_stall_write_ms"] / writes, 2) if writes > 0 else None,
    }


def graded_latency(reading: dict[str, Any] | None) -> float | None:
    """The slower of read and write latency in one window, each counted only with enough I/O.

    ``None`` when the window has no pair, or when neither direction reached :data:`MIN_IOS`.
    """
    if not reading:
        return None
    values = [latency for latency, count in ((reading.get("readMs"), reading.get("reads")),
                                             (reading.get("writeMs"), reading.get("writes")))
              if latency is not None and (count or 0) >= MIN_IOS]
    return max(values) if values else None


def io_windows(rows: list[Any]) -> dict[str, dict[str, Any] | None]:
    """``{window key: reading or None}`` for one file's stored rows."""
    samples = io_samples(rows)
    out: dict[str, dict[str, Any] | None] = {}
    for window in WINDOWS:
        if window.get("newest_pair"):
            delta = interval_rates.interval_delta(samples, fields=IO_FIELDS,
                                                  max_pair_hours=window["hours"])
        else:
            delta = interval_rates.window_delta(samples, fields=IO_FIELDS,
                                                window_hours=window["hours"],
                                                min_fraction=window["min_fraction"])
        out[window["key"]] = _latency(delta)
    return out


def io_since_start(rows: list[Any]) -> dict[str, Any] | None:
    """The newest row's average since the engine started - printed, never graded."""
    if not rows:
        return None
    newest = io_samples(rows[-1:])[0]
    reads, writes = as_float(newest.get("reads")), as_float(newest.get("writes"))
    stall_read, stall_write = as_float(newest.get("io_stall_read_ms")), as_float(newest.get("io_stall_write_ms"))
    if reads is None or writes is None or stall_read is None or stall_write is None:
        return None
    return {
        "since": str(newest.get("counters_since") or ""),
        "readMs": round(stall_read / reads, 2) if reads > 0 else None,
        "writeMs": round(stall_write / writes, 2) if writes > 0 else None,
    }


def io_interval_points(rows: list[Any]) -> list[list[Any]]:
    """``[[epoch, latency ms or None]]``, one per collection interval: what the chart draws.

    The stored ``metric_value`` is the since-boot average, which drifts by hundredths a day and
    draws a flat line through a stall. The differenced pairs draw the stall.
    """
    points = []
    for pair in interval_rates.consecutive_deltas(io_samples(rows), fields=IO_FIELDS):
        points.append([int(pair["to"]), graded_latency(_latency(pair))])
    return points


def io_window_line(windows: dict[str, Any] | None) -> str:
    """The three readings on one line, read / write - what a tile prints under its figure."""
    if not windows:
        return ""
    labels = {"now": "latest", "hour": "1 h", "day": "24 h"}
    parts = []
    for window in WINDOWS:
        reading = windows.get(window["key"])
        if reading is None:
            continue
        read = "—" if reading["readMs"] is None else f"{reading['readMs']:.1f}"
        write = "—" if reading["writeMs"] is None else f"{reading['writeMs']:.1f}"
        parts.append(f"{labels[window['key']]} {read} / {write} ms")
    return " · ".join(parts) + " (read / write)" if parts else ""


def _io_summary(windows: dict[str, Any], since: dict[str, Any] | None) -> str:
    parts = [f"Graded on the last hour, the slower of read and write with at least {MIN_IOS} "
             f"I/Os ({IO_THRESHOLDS})."]
    for window in WINDOWS:
        reading = windows.get(window["key"])
        if reading is None:
            parts.append(f"{window['label'].capitalize()}: no interval yet.")
            continue
        parts.append(f"{window['label'].capitalize()} ({reading['span']}): read "
                     f"{_ms(reading['readMs'])} · write {_ms(reading['writeMs'])} "
                     f"({reading['reads']:,} reads, {reading['writes']:,} writes).")
    if since:
        start = f" ({since['since']})" if since.get("since") else ""
        parts.append(f"Since the engine started{start}, not graded: read {_ms(since['readMs'])} · "
                     f"write {_ms(since['writeMs'])}.")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Page life expectancy - a gauge, summarised
# --------------------------------------------------------------------------- #
def ple_windows(rows: list[Any]) -> dict[str, dict[str, Any] | None]:
    """``{window key: {latest, min, avg, max, samples, span} or None}`` over the stored readings."""
    samples = [{"collected_at": _field(row, "collected_at"), "ple": _field(row, "metric_value")}
               for row in rows]
    out: dict[str, dict[str, Any] | None] = {}
    for window in WINDOWS:
        summary = interval_rates.window_gauge(samples, fields=["ple"], window_hours=window["hours"],
                                              newest_only=bool(window.get("newest_pair")))
        reading = (summary or {}).get("readings", {}).get("ple")
        if not reading:
            out[window["key"]] = None
            continue
        out[window["key"]] = {
            "seconds": summary["seconds"],
            "span": _span(summary["seconds"]),
            "samples": reading["count"],
            "latest": reading["latest"],
            "min": reading["min"],
            "avg": round(reading["avg"]),
            "max": reading["max"],
        }
    return out


def _ple_summary(windows: dict[str, Any]) -> str:
    parts = [f"Graded on the last hour's average ({PLE_THRESHOLDS})."]
    now = windows.get("now")
    if now:
        parts.append(f"Latest reading: {now['latest']:.0f} s.")
    for key in ("hour", "day"):
        reading = windows.get(key)
        label = next(window["label"] for window in WINDOWS if window["key"] == key).capitalize()
        if reading is None:
            parts.append(f"{label}: no reading.")
            continue
        span = f" ({reading['span']})" if reading["span"] else ""
        parts.append(f"{label}{span}: lowest {reading['min']:.0f} s · average {reading['avg']:.0f} s"
                     f" · highest {reading['max']:.0f} s ({reading['samples']} readings).")
    return " ".join(parts)


def ple_window_line(windows: dict[str, Any] | None) -> str:
    """The readings on one line, for a tile."""
    if not windows:
        return ""
    parts = []
    if windows.get("now"):
        parts.append(f"latest {windows['now']['latest']:.0f} s")
    for key, label in (("hour", "1 h"), ("day", "24 h")):
        reading = windows.get(key)
        if reading:
            parts.append(f"{label} avg {reading['avg']:.0f} s, lowest {reading['min']:.0f} s")
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# Collector-graded samples - the severity that held
# --------------------------------------------------------------------------- #
def held_severity(statuses: list[Any]) -> str:
    """The highest severity at least half of ``statuses`` reached - ``OK`` when none did.

    The statuses are the collector's own, as stored: a ``severity_map`` that lowered a sample to
    ``LOGGING`` lowered it here too, which is what keeps a page from un-silencing a decision.
    """
    severities = [health_model.severity_of(str(status or "")) for status in statuses]
    if not severities:
        return "OK"
    # Half, and never one sample of several: CPU is read once an hour, so "the last hour" holds two
    # readings a few seconds under 60 minutes apart, and one 1-second 90 % from an hour ago made the
    # host CRITICAL beside a newest reading of 74 % OK (2026-11-06). A lone sample still decides.
    need = max((len(severities) + 1) // 2, min(2, len(severities)))
    for severity in ("CRITICAL", "WARNING"):
        rank = health_model.SEVERITY_RANK[severity]
        if sum(1 for found in severities if health_model.SEVERITY_RANK.get(found, 0) >= rank) >= need:
            return severity
    return "OK"


def _inside(samples: list[Any], hours: float, *, newest_only: bool = False) -> list[Any]:
    if not samples:
        return []
    if newest_only:
        return samples[-1:]
    newest = as_epoch(_field(samples[-1], "collected_at")) or 0.0
    return [sample for sample in samples
            if (as_epoch(_field(sample, "collected_at")) or 0.0) >= newest - hours * 3600]


def _span_of(samples: list[Any]) -> float:
    if len(samples) < 2:
        return 0.0
    return ((as_epoch(_field(samples[-1], "collected_at")) or 0.0)
            - (as_epoch(_field(samples[0], "collected_at")) or 0.0))


def _not_ok(status: Any) -> bool:
    return health_model.severity_of(str(status or "")) not in ("OK", "UNKNOWN")


def sustained_windows(rows: list[Any]) -> dict[str, dict[str, Any] | None]:
    """``{window: {samples, avg, max, notOk, severity, span}}`` over one item's stored samples."""
    out: dict[str, dict[str, Any] | None] = {}
    for window in WINDOWS:
        inside = _inside(rows, window["hours"], newest_only=bool(window.get("newest_pair")))
        if not inside:
            out[window["key"]] = None
            continue
        values = [value for value in (as_float(_field(row, "metric_value")) for row in inside)
                  if value is not None]
        seconds = _span_of(inside)
        out[window["key"]] = {
            "seconds": seconds,
            "span": _span(seconds),
            "samples": len(inside),
            "avg": round(sum(values) / len(values), 2) if values else None,
            "max": round(max(values), 2) if values else None,
            "notOk": sum(1 for row in inside if _not_ok(_field(row, "status"))),
            "severity": held_severity([_field(row, "status") for row in inside]),
            "latestStatus": str(_field(inside[-1], "status") or ""),
        }
    return out


def _collections(rows: list[Any]) -> list[dict[str, Any]]:
    """One sample per collection of a code: its worst status and the item that carried it.

    Every row of one execution carries the same ``collected_at`` (``health_model.latest_snapshot``
    relies on the same fact), so the stamp is the collection.
    """
    rank = health_model.SEVERITY_RANK
    by_stamp: dict[str, dict[str, Any]] = {}
    for row in rows:
        stamp = str(_field(row, "collected_at") or "")
        severity = health_model.severity_of(str(_field(row, "status") or ""))
        held = by_stamp.setdefault(stamp, {"collected_at": stamp, "status": "OK", "item": ""})
        if rank.get(severity, 0) > rank.get(held["status"], 0):
            held.update(status=severity, item=str(_field(row, "metric_item") or ""))
    return sorted(by_stamp.values(), key=lambda sample: as_epoch(sample["collected_at"]) or 0.0)


def sampled_windows(rows: list[Any]) -> dict[str, dict[str, Any] | None]:
    """``{window: {samples, notOk, severity, span}}`` per collection of a sampled code."""
    collections = _collections(rows)
    out: dict[str, dict[str, Any] | None] = {}
    for window in WINDOWS:
        inside = _inside(collections, window["hours"], newest_only=bool(window.get("newest_pair")))
        if not inside:
            out[window["key"]] = None
            continue
        seconds = _span_of(inside)
        out[window["key"]] = {
            "seconds": seconds,
            "span": _span(seconds),
            "samples": len(inside),
            "notOk": sum(1 for sample in inside if _not_ok(sample["status"])),
            "severity": held_severity([sample["status"] for sample in inside]),
            "latestStatus": inside[-1]["status"],
            "latestItem": inside[-1]["item"],
        }
    return out


def _number(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    text = f"{value:.0f}" if abs(value) >= 100 else f"{value:g}"
    if str(unit or "").lower().startswith(("percent", "pct")):
        return f"{text}%"
    return f"{text} {unit}" if unit else text


def _label(key: str) -> str:
    return next(window["label"] for window in WINDOWS if window["key"] == key).capitalize()


def _sustained_summary(windows: dict[str, Any], unit: str) -> str:
    parts = ["Graded on the last hour: the severity the collector gave at least half of its "
             "samples, by its own thresholds and this server's remaps."]
    now = windows.get("now")
    if now:
        parts.append(f"Latest: {_number(now['avg'], unit)} ({now['latestStatus'] or 'no status'}).")
    for key in ("hour", "day"):
        reading = windows.get(key)
        if reading is None:
            continue
        span = f" ({reading['span']})" if reading["span"] else ""
        parts.append(f"{_label(key)}{span}: average {_number(reading['avg'], unit)}, peak "
                     f"{_number(reading['max'], unit)}, {reading['notOk']} of {reading['samples']} "
                     f"samples not OK - held {reading['severity']}.")
    return " ".join(parts)


def sustained_window_line(windows: dict[str, Any] | None, unit: str) -> str:
    """The readings on one line, for a tile."""
    if not windows:
        return ""
    parts = []
    for key, label in (("hour", "1 h"), ("day", "24 h")):
        reading = windows.get(key)
        if reading:
            parts.append(f"{label} avg {_number(reading['avg'], unit)}, peak "
                         f"{_number(reading['max'], unit)}, "
                         f"{reading['notOk']}/{reading['samples']} not OK")
    return " · ".join(parts)


def _sampled_summary(windows: dict[str, Any]) -> str:
    parts = ["Graded on the last hour: a finding stays only when at least half of the hour's "
             "samples found pressure, by the collector's own rule."]
    now = windows.get("now")
    if now:
        named = f", {now['latestItem']}" if now.get("latestItem") else ""
        parts.append(f"Latest sample: {now['latestStatus']}{named}.")
    for key in ("hour", "day"):
        reading = windows.get(key)
        if reading is None:
            continue
        span = f" ({reading['span']})" if reading["span"] else ""
        parts.append(f"{_label(key)}{span}: {reading['notOk']} of {reading['samples']} samples "
                     f"not OK - held {reading['severity']}.")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# One entry point for both pages
# --------------------------------------------------------------------------- #
def is_sustained(code: str, item: str) -> bool:
    """Whether this ``(code, item)`` is a collector-graded sample judged by the severity that held."""
    return (code, item) in SUSTAINED_ITEMS or any(
        code == sustained_code and str(item or "").endswith(suffix)
        for sustained_code, suffix in SUSTAINED_SUFFIXES)


def is_judged(code: str, item: str) -> bool:
    """Whether a page judges this ``(code, item)`` from its history rather than its newest row."""
    return code in WINDOWED_CODES or code in SAMPLED_CODES or is_sustained(code, item)


def rejudge(code: str, rows: list[Any], *, item: str = "") -> dict[str, Any] | None:
    """What a report page grades for one ``(server, code, item)``, from its stored rows.

    ``rows`` are that item's rows - for a sampled code, every row of the code. Returns ``None`` for
    a ``(code, item)`` this module does not judge. Otherwise ``value`` is the figure to print and,
    for the report-judged codes, to grade (``None``: nothing to grade); ``status`` the severity the
    hour held, for the collector-graded ones (``None`` where the value decides); ``lower_only``
    when that status may only lower the newest row's; ``text`` what to print where there is no
    number; ``unit``; ``windows`` the three readings; ``summary`` the sentence that says which
    window decided and what the others read; ``line`` the readings for a tile.
    """
    rows = sorted(rows, key=lambda row: as_epoch(_field(row, "collected_at")) or 0.0)
    if code == IO_LATENCY_CODE:
        windows = io_windows(rows)
        reading = windows.get(GRADED_WINDOW) or windows.get("now")
        value = graded_latency(reading)
        text = ("no interval yet" if reading is None
                else f"under {MIN_IOS} I/Os in {reading['span']}" if value is None else "")
        return {"value": value, "status": None, "lower_only": False, "text": text, "unit": "ms",
                "windows": windows, "summary": _io_summary(windows, io_since_start(rows)),
                "line": io_window_line(windows)}
    if code == PLE_CODE:
        windows = ple_windows(rows)
        reading = windows.get(GRADED_WINDOW) or windows.get("now")
        value = reading["avg"] if reading else None
        return {"value": value, "status": None, "lower_only": False,
                "text": "" if reading else "no reading", "unit": "seconds",
                "windows": windows, "summary": _ple_summary(windows),
                "line": ple_window_line(windows)}
    if code in SAMPLED_CODES:
        windows = sampled_windows(rows)
        reading = windows.get(GRADED_WINDOW) or windows.get("now")
        return {"value": None, "status": reading["severity"] if reading else None,
                "lower_only": True, "text": "", "unit": "", "windows": windows,
                "summary": _sampled_summary(windows),
                "line": (f"1 h: {reading['notOk']}/{reading['samples']} samples not OK"
                         if reading else "")}
    if is_sustained(code, item):
        windows = sustained_windows(rows)
        reading = windows.get(GRADED_WINDOW) or windows.get("now")
        unit = str(_field(rows[-1], "metric_unit") or "") if rows else ""
        return {"value": reading["avg"] if reading else None,
                "status": reading["severity"] if reading else None, "lower_only": False,
                "text": "" if reading and reading["avg"] is not None else "no reading",
                "unit": unit, "windows": windows, "summary": _sustained_summary(windows, unit),
                "line": sustained_window_line(windows, unit)}
    return None


def judged_status(newest_status: Any, judged: dict[str, Any]) -> str:
    """The status a page shows: the hour's, or for a sampled code the lower of the two."""
    held = judged.get("status")
    if held is None:
        return str(newest_status or "")
    if not judged.get("lower_only"):
        return held
    rank = health_model.SEVERITY_RANK
    newest = health_model.severity_of(str(newest_status or ""))
    return held if rank.get(held, 0) < rank.get(newest, 0) else str(newest_status or "")


def history_by_item(rows: list[Any]) -> dict[tuple[str, str, str], list[Any]]:
    """``{(server_id, metric_code, metric_item): rows}`` for what a page judges, oldest first.

    A sampled code's rows are grouped under :data:`ALL_ITEMS` instead, because it is read per
    collection across its items.
    """
    grouped: dict[tuple[str, str, str], list[Any]] = {}
    for row in rows:
        code = str(_field(row, "metric_code") or "")
        item = str(_field(row, "metric_item") or "")
        if not is_judged(code, item):
            continue
        server = str(_field(row, "server_id") or _field(row, "ip") or "")
        grouped.setdefault((server, code, ALL_ITEMS if code in SAMPLED_CODES else item),
                           []).append(row)
    return grouped


def history_for(history: dict[tuple[str, str, str], list[Any]], server: str, code: str,
                item: str) -> list[Any]:
    """The rows :func:`rejudge` needs for one ``(server, code, item)``."""
    return history.get((server, code, ALL_ITEMS if code in SAMPLED_CODES else item), [])


def rejudged_row(row: Any, history: list[Any]) -> dict[str, Any]:
    """A current-snapshot row judged on its history - its value, its status, and its message
    opened by the summary - for a page whose findings are built from rows, not from series.

    A row of anything else comes back as a plain copy. A row with no history is judged on itself
    alone, which for disk latency is no interval - never its since-boot average.
    """
    copy = dict(row)
    code, item = str(copy.get("metric_code") or ""), str(copy.get("metric_item") or "")
    judged = rejudge(code, history or [row], item=item)
    if judged is None:
        return copy
    if code not in SAMPLED_CODES:
        value = judged["value"]
        copy["metric_value"] = "" if value is None else f"{value:.2f}".rstrip("0").rstrip(".")
        copy["metric_unit"] = judged["unit"] or copy.get("metric_unit")
    copy["status"] = judged_status(copy.get("status"), judged)
    copy["message"] = f"{judged['summary']} | {copy.get('message') or ''}".rstrip(" |")
    copy["window_text"] = judged["text"]
    return copy
