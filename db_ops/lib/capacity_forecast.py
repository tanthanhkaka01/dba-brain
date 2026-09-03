"""When does this run out? — turning stored capacity history into a date.

Every report so far answers "how full is it now". The store already holds the rest of the answer:
on 192.0.2.115 the ``L:`` volume fell from 618 GB free to 163 GB in three days while ``SALESDB``'s
allocated log went 21 GB -> 95 GB -> 428 GB. Nothing computed a rate, so a volume days from full
and one that has been flat for a month rendered identically — a number with no direction.

Three rules keep a forecast from being worse than none:

* **Robust slope, not least squares.** Capacity series are punctuated by shrink/regrow cycles and
  file moves, and a single 300 GB step would dominate a least-squares fit and predict the opposite
  of the truth. The slope here is the median of all pairwise slopes (Theil-Sen), which ignores a
  minority of jumps entirely.
* **A reset restarts the history.** Free space jumping *up* means someone shrank, moved or deleted
  something; growth measured across that instant describes two different worlds averaged together.
  Only the segment after the last reset is used, and the forecast says how much history that left.
* **Refuse to guess.** Below :data:`MIN_POINTS` samples or :data:`MIN_SPAN_HOURS` of span the
  result is ``insufficient_history`` — never a confident zero. A "0 GB/day, never full" on two
  samples is the most dangerous output this module could produce.

Policy (thresholds, horizons) lives in ``data/capacity_policy.json``, loaded the same way
:mod:`db_ops.lib.backup_policy` loads its own — config is data, and a report must still render
when the file has not been deployed yet.
"""

from __future__ import annotations

import json
import statistics
from db_ops.lib.coerce import as_epoch, as_float
from typing import Any


#: Fewest samples that can produce a slope worth showing. Two points are a line through noise.
MIN_POINTS = 6
#: ...and they have to span real time: 6 samples five minutes apart say nothing about tomorrow.
MIN_SPAN_HOURS = 12.0
#: A reset is an *outlier* step, judged against how much this series normally moves — not against
#: its range. Range alone is unusable: a nearly-flat volume has a tiny range, so ordinary jitter
#: clears 10% of it and every sample looks like a shrink. Measured on the live estate that trimmed
#: an 8-day history down to "12 resets seen" and `insufficient_history` on volumes that had never
#: been touched. A robust rule is "many times the typical step".
RESET_STEP_MULTIPLE = 8.0
#: ...with an absolute floor, so a perfectly flat series (typical step 0) does not treat its first
#: byte of movement as a reset.
RESET_MIN_ABSOLUTE = 1.0




def rule_for(server_id: str = "", item: str = "", policy: dict | None = None) -> dict[str, Any]:
    """The horizons and reserve that apply to one volume: the first matching override, else the
    defaults. A 4 TB data volume and a 60 GB system volume are not the same problem, so the
    thresholds are addressable per ``(server_id, item)`` without touching code."""
    policy = policy or {}
    rule: dict[str, Any] = dict(policy.get("defaults") or {})
    for override in policy.get("overrides") or []:
        wanted_server = str(override.get("server_id") or "").strip().lower()
        wanted_item = str(override.get("item") or "").strip().lower()
        if wanted_server and wanted_server != str(server_id or "").strip().lower():
            continue
        if wanted_item and wanted_item != str(item or "").strip().lower():
            continue
        rule.update({k: v for k, v in override.items() if not k.startswith("_")})
        break
    return rule


def horizons(policy: dict | None = None, *, server_id: str = "", item: str = "") -> tuple[int, int]:
    """``(critical_days, warning_days)`` — how close to full is worth saying out loud."""
    rule = rule_for(server_id, item, policy)
    return (
        int(rule.get("critical_days_to_full", 30) or 30),
        int(rule.get("warning_days_to_full", 90) or 90),
    )


def reserve_gb(policy: dict | None = None, *, server_id: str = "", item: str = "") -> float:
    """Free space that does not count as usable — "full" is reaching this, not zero."""
    return float(rule_for(server_id, item, policy).get("reserve_gb", 0) or 0)


def _theil_sen_per_day(points: list[tuple[float, float]]) -> float:
    """Median pairwise slope, in units per day.

    Pairwise rather than fitted: a shrink/regrow spike is a minority of the pairs, so the median
    steps straight over it. A least-squares line would be dragged to the spike and could report
    growth on a volume that is actually draining.
    """
    slopes: list[float] = []
    for i in range(len(points)):
        t0, v0 = points[i]
        for j in range(i + 1, len(points)):
            t1, v1 = points[j]
            span = t1 - t0
            if span <= 0:
                continue
            slopes.append((v1 - v0) / span * 86400.0)
    return statistics.median(slopes) if slopes else 0.0


def _segment_after_last_reset(points: list[tuple[float, float]], *, direction: str) -> tuple[list[tuple[float, float]], int]:
    """Drop everything up to the last reset. Returns ``(segment, resets_seen)``.

    For a *free space* series (``direction='down'``) a reset is a jump **up**: space came back, so
    whatever consumed it before is not the same story as what is consuming it now. For a *size*
    series (``direction='up'``) it is the mirror image — a jump down is a shrink.
    """
    if len(points) < 3:
        return points, 0
    steps = [points[i][1] - points[i - 1][1] for i in range(1, len(points))]
    typical_step = statistics.median(abs(step) for step in steps)
    threshold = max(typical_step * RESET_STEP_MULTIPLE, RESET_MIN_ABSOLUTE)
    start = 0
    resets = 0
    for index in range(1, len(points)):
        step = points[index][1] - points[index - 1][1]
        jumped = step > threshold if direction == "down" else step < -threshold
        if jumped:
            start = index
            resets += 1
    return points[start:], resets


def forecast(samples: list[tuple[Any, Any]], *, floor: float = 0.0,
             direction: str = "down", ceiling: float | None = None) -> dict[str, Any]:
    """Project a capacity series to the moment it reaches ``floor`` (or ``ceiling``).

    ``samples`` is ``[(collected_at, value)]`` in any order. ``direction='down'`` is a free-space
    series heading for ``floor``; ``direction='up'`` is a size series heading for ``ceiling``.

    Always returns a dict with ``status``; ``insufficient_history`` is a first-class answer and the
    caller must render it as such rather than as "no growth".
    """
    points = sorted(
        (t, v) for t, v in ((as_epoch(a), as_float(b)) for a, b in samples)
        if t is not None and v is not None
    )
    if len(points) < MIN_POINTS:
        return {"status": "insufficient_history", "reason": f"{len(points)} sample(s), need {MIN_POINTS}",
                "points": len(points)}

    segment, resets = _segment_after_last_reset(points, direction=direction)
    span_hours = (segment[-1][0] - segment[0][0]) / 3600.0 if len(segment) > 1 else 0.0
    if len(segment) < MIN_POINTS or span_hours < MIN_SPAN_HOURS:
        return {
            "status": "insufficient_history",
            "reason": (f"only {len(segment)} sample(s) over {span_hours:.1f}h since the last "
                       f"resize/shrink" if resets else
                       f"{len(segment)} sample(s) over {span_hours:.1f}h, need {MIN_SPAN_HOURS:.0f}h"),
            "points": len(segment), "resets": resets, "span_hours": round(span_hours, 1),
        }

    per_day = _theil_sen_per_day(segment)
    latest = segment[-1][1]
    result: dict[str, Any] = {
        "status": "ok",
        "points": len(segment),
        "span_hours": round(span_hours, 1),
        "resets": resets,
        "latest": round(latest, 2),
        "per_day": round(per_day, 3),
        "days_to_threshold": None,
        "threshold": floor if direction == "down" else ceiling,
    }
    if direction == "down":
        # Only a series actually falling can run out; a flat or growing one has no date.
        if per_day < 0 and latest > floor:
            result["days_to_threshold"] = round((latest - floor) / abs(per_day), 1)
    else:
        if per_day > 0 and ceiling is not None and latest < ceiling:
            result["days_to_threshold"] = round((ceiling - latest) / per_day, 1)
    return result


def severity_for(days_to_threshold: float | None, policy: dict | None = None,
                 *, server_id: str = "", item: str = "") -> str:
    """CRITICAL / WARNING / OK for a projected exhaustion date."""
    if days_to_threshold is None:
        return "OK"
    critical_days, warning_days = horizons(policy, server_id=server_id, item=item)
    if days_to_threshold <= critical_days:
        return "CRITICAL"
    if days_to_threshold <= warning_days:
        return "WARNING"
    return "OK"


def describe(result: dict[str, Any], *, unit: str = "GB") -> str:
    """One human sentence for a forecast, including the refusal case."""
    if result.get("status") != "ok":
        return f"insufficient history — {result.get('reason', 'not enough samples')}"
    per_day = result["per_day"]
    trend = "falling" if per_day < 0 else ("growing" if per_day > 0 else "flat")
    text = f"{trend} {abs(per_day):.2f} {unit}/day (median of {result['points']} samples over {result['span_hours']}h)"
    if result.get("resets"):
        text += f", measured since the last resize/shrink ({result['resets']} seen)"
    if result.get("days_to_threshold") is not None:
        text += f" — reaches {result['threshold']} {unit} in about {result['days_to_threshold']} day(s)"
    return text
