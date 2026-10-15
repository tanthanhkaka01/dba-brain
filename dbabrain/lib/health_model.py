"""What is true about a target **now**, shared by every page that claims to say so.

Two report pages answer "what is wrong with this server": the fleet page
(``database-inventory.html``) and the per-server page (``server-metrics.html``). They used to
answer it with different code over different rows, so they disagreed about the same server at the
same moment — one listed 17 critical lock results while the other showed blocking at zero, and a
95 ms write-latency finding existed on one page and nowhere on the other.

The disagreement had one root cause: **"current" was never defined.** Keeping the newest row per
``(server, metric_code, metric_item)`` sounds like current state and is not. When a metric's
condition clears, the collector writes a single row with ``metric_item = NULL`` ("SQL returned no
rows", see ``metrics/collector.py``). That row lands in a *different* partition from the item it
cleared, so the old CRITICAL item stayed "newest" — and therefore "current" — for the whole report
window. Blocking that ended on Tuesday was still on the page on Friday.

Current state is defined here as **the complete newest collection snapshot per
``(server, metric_code)``**, NULL-item rows included. A metric either produced items in its last
run or it did not; there is no such thing as an item surviving its own metric's next collection.
History keeps every row — timelines and charts read it — but a verdict never does.

The severity rules and the problem grouping live here for the same reason: one classifier, so the
two pages cannot drift apart again.
"""

from __future__ import annotations

import datetime
import re

# ``ERROR`` is the collector failing, ``CRITICAL`` is the collector succeeding and not liking what
# it found. Both are red on a page: a metric that cannot be read is not a healthy metric.
CRITICAL_STATUSES = {"CRITICAL", "ERROR"}
# UNKNOWN/NO_DATA are warnings, not OK — "we did not find out" is a finding.
WARNING_STATUSES = {"WARNING", "WARN", "UNKNOWN", "NO_DATA"}
SEVERITY_RANK = {"CRITICAL": 3, "WARNING": 2, "OK": 1, "UNKNOWN": 0}

#: The item name given to a row the collector wrote without one — an outright failure, or an
#: empty result. Without it those rows are invisible on the per-server page (its query dropped
#: ``metric_item IS NULL`` outright), which is how a metric that had been failing for two days
#: could still display its last successful value as the current one.
COLLECTOR_ITEM = "__collector__"

#: Metrics whose SQL returns ``OK`` on every branch: they are logging-only by design, so the
#: report has to judge them or the page shows a green tile next to 300 ms reads. The thresholds
#: are stated on the tile so nobody reads a report rule as an alerting one.
REPORT_JUDGED_CODES = ("PERFORMANCE_IO_LATENCY", "PAGE_LIFE_EXPECTANCY")


def severity_of(status: str) -> str:
    upper = str(status or "").upper()
    if upper in CRITICAL_STATUSES:
        return "CRITICAL"
    if upper in WARNING_STATUSES:
        return "WARNING"
    if not upper:
        return "UNKNOWN"
    return "OK"


def report_judged_severity(code: str, value) -> str:
    """Severity for the two logging-only metrics, from their value rather than their status."""
    if value is None:
        return "OK"
    if code == "PERFORMANCE_IO_LATENCY":
        return "CRITICAL" if value >= 50 else "WARNING" if value >= 20 else "OK"
    if code == "PAGE_LIFE_EXPECTANCY":
        return "CRITICAL" if 0 < value < 300 else "WARNING" if 0 < value < 2000 else "OK"
    return "OK"


def current_severity(*, code: str, status: str, value=None) -> str:
    """The severity both pages must show for one current row."""
    if code in REPORT_JUDGED_CODES:
        return report_judged_severity(code, value)
    return severity_of(status)


#: The note ``metrics/collector.py::_apply_severity_override`` appends to a message when
#: ``metrics.metric_overrides`` remaps a status — the only record of what the row graded itself
#: before config lowered it.
_REMAP_NOTE = re.compile(r"severity remapped\s+([A-Z_]+)\s*->\s*([A-Z_]+)\s+by metric_overrides",
                         re.IGNORECASE)


def downgraded_from(message: str) -> str:
    """The severity a ``severity_map`` removed from this row, or ``""`` when it removed nothing.

    A remap is a decision about *alerting*, and a page that shows only its result contradicts
    itself: the CPU tile on ``ACME-192-0-2-249-HOST`` printed ``90.67%`` under its own stated
    rule ``CRITICAL >= 90%`` and coloured itself green, because the stored status was ``LOGGING``
    — the deliberate 2026-08-14 override for that host's one-second CPU sample. Nothing on the
    page said a decision had been made, so the tile read as "90.67% is fine", which is the one
    thing it did not mean.

    Returns the original severity only when it was genuinely *lowered*. A remap that raises a
    status (``WARNING -> CRITICAL``) already shows in the colour and needs no marker.
    """
    match = _REMAP_NOTE.search(str(message or ""))
    if not match:
        return ""
    was, now = severity_of(match.group(1)), severity_of(match.group(2))
    return was if SEVERITY_RANK.get(was, 0) > SEVERITY_RANK.get(now, 0) else ""


# `to_number` lived here until 2026-08-16 and was called by nothing — one of three identical
# copies of "a float, or None when it is not one" inside `lib`. The one that survived is
# `lib.coerce.as_float`, which says in one place why the fallback is None and not zero.


# --------------------------------------------------------------------------- #
# Current snapshot
# --------------------------------------------------------------------------- #
def snapshot_at(rows, *, code_key: str = "metric_code", time_key: str = "collected_at") -> dict[str, str]:
    """``{metric_code: newest collected_at}`` over ``rows``."""
    newest: dict[str, str] = {}
    for row in rows:
        code = str(row.get(code_key) or "")
        stamp = str(row.get(time_key) or "")
        if stamp > newest.get(code, ""):
            newest[code] = stamp
    return newest


def latest_snapshot(rows: list[dict], *, code_key: str = "metric_code",
                    time_key: str = "collected_at") -> list[dict]:
    """Only the rows belonging to each metric's most recent collection.

    Every row of one metric execution carries the same ``collected_at`` (the collector stamps it
    once per metric per target), so equality against the metric's newest stamp is exactly "this
    row is part of the latest run" — no tolerance window needed, and none wanted: a tolerance is
    how an item from the *previous* run sneaks back into the present.
    """
    newest = snapshot_at(rows, code_key=code_key, time_key=time_key)
    return [row for row in rows
            if str(row.get(time_key) or "") == newest.get(str(row.get(code_key) or ""), "")]


# --------------------------------------------------------------------------- #
# Problem grouping — the one list both pages render
# --------------------------------------------------------------------------- #
def age_hint(text: str, *, now: int) -> str:
    """``"2026-05-10 16:17"`` -> ``"80 days ago"``. A bare timestamp is not a finding on its own."""
    match = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?", str(text or ""))
    if not match:
        return ""
    year, month, day = (int(match.group(i)) for i in (1, 2, 3))
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    try:
        stamp = datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)
    except ValueError:
        return ""
    days = (now - int(stamp.timestamp())) / 86400.0
    if days < 0:
        return ""
    if days < 1:
        return f"{round(days * 24)}h ago"
    return f"{round(days)} days ago"


def group_findings(findings: list[dict], *, now: int) -> list[dict]:
    """Group per-item findings by metric code, worst first, each saying what to do.

    Flat, one row per item, this list was 48 near-identical lines on the ERP host — 24 of them a
    statistics object name and a date. Grouping collapses those into one line with the count and
    the worst example, and carries the collector's own message so a row says *why* it is a problem
    instead of only naming the object.

    A finding is ``{code, label, item, value, severity, message, collectedAt, action, lastText}``.
    Both callers build that shape: the server page from its chart series, the fleet page from the
    store's current-snapshot query. ``collectedAt`` rides along because a fleet card that does not
    say when the sample was taken is how a two-day-old incident passed for a present one.
    """
    groups: dict[str, dict] = {}
    for finding in findings:
        severity = str(finding.get("severity") or "")
        if severity in ("OK", "UNKNOWN", ""):
            continue
        code = str(finding.get("code") or "")
        group = groups.setdefault(code, {
            "code": code,
            "label": finding.get("label") or code,
            "severity": severity,
            "action": finding.get("action") or "Check the metric detail below.",
            "items": [],
        })
        if SEVERITY_RANK[severity] > SEVERITY_RANK[group["severity"]]:
            group["severity"] = severity
        group["items"].append({
            "item": finding.get("item") or "",
            "value": finding.get("value") or "—",
            "age": age_hint(str(finding.get("lastText") or ""), now=now),
            "severity": severity,
            "detail": finding.get("message") or "",
            # Left as the caller gave it: the server page keys its rows by epoch (its charts do),
            # the fleet page by the store's ISO text. Both sort correctly against themselves,
            # which is all this needs — it is never compared across the two.
            "collectedAt": finding.get("collectedAt"),
        })

    out = []
    for group in groups.values():
        group["items"].sort(key=lambda row: (-SEVERITY_RANK[row["severity"]], row["item"]))
        group["count"] = len(group["items"])
        worst = group["items"][0]
        group["headline"] = (
            f"{worst['item']} · {worst['value']}" if group["count"] == 1
            else f"{group['count']} items · worst: {worst['item']} · {worst['value']}"
        )
        if worst["age"]:
            group["headline"] += f" ({worst['age']})"
        # The newest sample behind the group. A card built from this must print it: severity
        # without a timestamp is what let a 48-hour-old error log finding read as "now".
        stamps = [row["collectedAt"] for row in group["items"] if row["collectedAt"] is not None]
        group["collectedAt"] = max(stamps, key=str) if stamps else None
        out.append(group)
    out.sort(key=lambda group: (-SEVERITY_RANK[group["severity"]], -group["count"], group["label"]))
    return out
