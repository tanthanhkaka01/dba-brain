"""How often a restore drill must succeed - its tier - and whether it is overdue (0.35.0, 1.248).

Gap G8 (2026-10-30): the scenario drills daily, weekly and monthly, and dbabrain had no word for
which a drill was. A schedule alone does not say it: a monthly drill is ``from_day: 1, to_day: 1``,
the first Sunday is ``from_day: 1, to_day: 7, weekdays: [7]`` - ``time_window`` already holds both -
but nothing could then say *this drill is late*. So a restore entry states its ``tier``, the run
records it (``restore_runs.tier``), and the reports judge each drill by its tier's deadline.

The deadline is the tier's period plus a margin for a drill that runs a little later than the one
before: a daily drill that succeeded 25 hours ago is fine, at 27 it is overdue.

Values and rules only: ``backup_restore`` writes the tier, ``reports`` judges it, neither imports
the other.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from dbabrain.lib import errors

__all__ = ["TIERS", "TierError", "deadline", "drill_state", "parse_tier"]

#: Each tier and the longest a drill of it may go without a success.
TIERS: dict[str, timedelta] = {
    "daily": timedelta(hours=26),
    "weekly": timedelta(days=7, hours=12),
    "monthly": timedelta(days=32),
}

OK, OVERDUE, NEVER, UNTIERED = "OK", "OVERDUE", "NEVER", "UNTIERED"


class TierError(errors.ConfigError):
    """A ``tier`` that is not one of :data:`TIERS`."""


def parse_tier(value: Any, *, where: str = "tier") -> str | None:
    """The entry's ``tier``, lower-cased; ``None`` when it states none (a drill nobody tiered)."""
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text not in TIERS:
        raise TierError(f"{where} must be one of {', '.join(TIERS)} (or absent); got {value!r}.")
    return text


def deadline(tier: str | None) -> timedelta | None:
    return TIERS.get(str(tier or "").lower())


def drill_state(tier: str | None, last_success: datetime | None, now: datetime) -> str:
    """``OK`` / ``OVERDUE`` / ``NEVER`` (tiered, never succeeded) / ``UNTIERED``."""
    limit = deadline(tier)
    if limit is None:
        return UNTIERED
    if last_success is None:
        return NEVER
    return OK if now - last_success <= limit else OVERDUE
