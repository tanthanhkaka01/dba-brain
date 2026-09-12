"""How long a directory of backups keeps them, in seconds, spelled one way everywhere.

There were two fields and two units for one idea:

* ``retention_days`` on a **backup job** — how long the directory that job *writes to* keeps its
  own files, which decides how far back a recovery can reach;
* ``target_retention_seconds`` on a **restore entry** — how long the target keeps the copies
  *staged* for restoring from, which only has to cover the next restore.

The two numbers genuinely differ, and should: a source may hold fourteen days of recovery while the
restore target only needs the newest full and the chain hanging off it. But two names and two units
for "how long are files kept" is one concept wearing two costumes, and it reads as an inconsistency
because it is one. Asked on 2026-09-11: *"why both?"*

So: **one name, `cleanup_retention`, and seconds — the unit db_ops uses for every interval.** Both
sides read the same field and act on it differently, which is the honest shape: backup prunes its
own output after backing up, restore prunes its staging after restoring.

The old spellings are still accepted so a config written before this does not stop loading, and
``days`` is converted rather than guessed at. Seconds also removes a rounding that was silently
changing configured values: the restore engine spoke hours and the conversion was
``max(1, seconds // 3600)``, so anything under an hour became an hour and nothing said so.
"""

from __future__ import annotations

from typing import Any

#: The canonical field name. One spelling, both halves of the app.
FIELD = "cleanup_retention"

#: Older spellings, with the factor that turns each into seconds. Read but never written.
LEGACY_FIELDS: dict[str, int] = {
    "target_retention_seconds": 1,
    "retention_days": 24 * 3600,
}

#: 8 days. The full backup is weekly, so an 8-day window always keeps the newest full and every
#: incremental chained to it — a margin rather than a tight fit. Quoted in the error below so an
#: operator does not have to go and find it.
DEFAULT_SECONDS = 8 * 24 * 3600


class CleanupRetentionError(ValueError):
    """The retention setting is missing or is not a number of seconds."""


def parse(entry: dict[str, Any], *, context: str, required: bool = True) -> int:
    """Seconds of retention for one backup job or restore entry.

    Required by default, and that is the point rather than strictness for its own sake: an absent
    field is indistinguishable from a considered one, and on 2026-09-11 six of this estate's
    fourteen restore entries carried none at all — each running on whatever the release's default
    happened to be, which nobody had chosen.

    ``0`` is a real setting and means *no age gate*: every file becomes a candidate and the chain
    rule alone decides (never the newest full, nor anything at or after it). It does not mean
    "keep everything", and the wording here says so because the opposite was believed once and a
    staging directory reached 16.7 GB on the strength of it.
    """
    raw = entry.get(FIELD)
    source = FIELD
    if raw is None:
        for name, factor in LEGACY_FIELDS.items():
            if entry.get(name) is not None:
                raw, source = entry[name], name
                break
        else:
            if not required:
                return DEFAULT_SECONDS
            raise CleanupRetentionError(
                f"{context}.{FIELD} is required: how long this keeps its backup files, "
                f"**in seconds**. {DEFAULT_SECONDS} is 8 days and is the recommended value - the "
                "full backup is weekly, so an 8-day window always keeps the newest full and every "
                "incremental chained to it. 0 removes the age gate and leaves the chain rule "
                "alone (never the newest full, nor anything at or after it).")

    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CleanupRetentionError(
            f"{context}.{source} must be a whole number "
            f"({'seconds' if source != 'retention_days' else 'days'}); got {raw!r}.") from None
    if value < 0:
        raise CleanupRetentionError(
            f"{context}.{source} must not be negative (0 = no age gate).")
    return value * LEGACY_FIELDS.get(source, 1) if source != FIELD else value


def as_days(seconds: int) -> float:
    """Seconds as days, for the retention planner that still thinks in whole days."""
    return float(seconds) / 86400.0


def describe(seconds: int) -> str:
    """A short human reading, for a log line or an error: ``691200s (8.0 days)``."""
    if not seconds:
        return "0s (no age gate)"
    return f"{seconds}s ({as_days(seconds):.1f} days)"
