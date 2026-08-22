"""Which backups the retention window no longer covers — a pure function of a listing.

Moved out of ``common/backupfiles/`` on 2026-08-15. Listing what is on a share and deleting from
it are operations and go through the ``common`` CLI; deciding *which* files are obsolete is
arithmetic over the list that came back, and a subprocess to do arithmetic is the wrong shape at
any speed.

Which backup files are obsolete. Two rules, and the default is age.

**age** (default) — a file older than ``retention_days`` is obsolete. Simple, predictable, and what
the in-script retention in ``assets/backup/**`` already does; an operator reading "retention 14
days" against a directory listing can work out the answer themselves, which is worth a lot on the
night somebody is deciding whether to free space.

**recovery_window** — keep everything needed to restore to *any* point in the last N days. The
cutoff is ``now - retention_days``, the **anchor** is the newest FULL at or before it, and
everything from the anchor onwards is required. This is what RMAN spells ``DELETE OBSOLETE ...
RECOVERY WINDOW OF n DAYS``.

The difference is not academic and is worth stating once, here, where both are implemented.
Restoring to a point ten days ago needs the FULL taken *before* that point plus every DIFF and LOG
after it. Under **age**, that FULL is deleted the moment it turns N days old while the newer
differentials that restore onto it are kept — they then have no base, and the set reaches back only
as far as its newest full. That is fine when a full is taken often relative to the window (this
estate takes one daily against a 14-day window, so the newest full is never more than a day old)
and wrong when it is not.

Both rules keep a file whose ``finished_at`` the engine could not state: "unknown age" and "old"
are not the same fact, and only one of them is a reason to delete something.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
# Re-exported: the vocabulary itself lives in db_ops/lib/backup_kinds.py, once.
from db_ops.lib.backup_kinds import FULL  # noqa: F401


#: The estate's default for database backups, and what ``restore_config.json`` already says for
#: every ``database``/``full`` job. Log/archive/WAL jobs run shorter windows of their own; this is
#: the number an operator means when they say "retention" without qualifying it.
DEFAULT_RETENTION_DAYS = 14

#: Delete by file age alone. The default.
AGE = "age"
#: Keep whatever is needed to restore to any point inside the window.
RECOVERY_WINDOW = "recovery_window"
MODES = (AGE, RECOVERY_WINDOW)

KEEP = "keep"
OBSOLETE = "obsolete"


class RetentionError(ValueError):
    """The retention plan could not be produced."""


def plan_retention(files: list[dict[str, Any]], *, retention_days: int = DEFAULT_RETENTION_DAYS,
                   mode: str = AGE, now: datetime | None = None) -> dict[str, Any]:
    """Split ``files`` into what is kept and what is obsolete, with a reason for each.

    ``files`` are rows as :func:`db_ops.common.backupfiles.list_backup_files` returns them.
    """
    days = _days(retention_days)
    rule = str(mode or AGE).strip().lower()
    if rule not in MODES:
        raise RetentionError(f"mode must be one of {', '.join(MODES)}; got {mode!r}.")
    moment = now or datetime.now(timezone.utc)
    cutoff = _stamp(moment - timedelta(days=days))

    rows = _by_age(files, cutoff=cutoff, days=days) if rule == AGE \
        else _by_recovery_window(files, cutoff=cutoff, days=days)

    obsolete = [row for row in rows if row["verdict"] == OBSOLETE]
    keep = [row for row in rows if row["verdict"] == KEEP]
    return {
        "mode": rule,
        "retention_days": days,
        "cutoff": cutoff,
        "obsolete": obsolete,
        "keep": keep,
        # The paths on their own, because that is exactly what delete-files takes as `paths`.
        "obsolete_paths": [row["path"] for row in obsolete],
        "counts": {"obsolete": len(obsolete), "keep": len(keep), "total": len(rows)},
        # Only as good as what the listing reported. Oracle's is RMAN's, which carries no size, so
        # this is 0 for an RMAN directory whose files are gigabytes each — `delete-files` stats
        # each file as it goes and reports the bytes actually freed. Stated rather than guessed at:
        # a plan that invented a size would be a plan an operator sized a disk against.
        "reclaimable_bytes": sum(int(row.get("size") or 0) for row in obsolete),
        "sizes_known": all(row.get("size") is not None for row in obsolete),
    }


def _by_age(files: list[dict[str, Any]], *, cutoff: str, days: int) -> list[dict[str, Any]]:
    """Older than the cutoff, whatever it is and whatever depends on it."""
    rows = []
    for row in files:
        finished = _normalise(row.get("finished_at"))
        if not finished:
            rows.append(_verdict(row, KEEP, "no finished_at: age unknown, so not judged"))
        elif finished < cutoff:
            rows.append(_verdict(row, OBSOLETE, f"finished {finished}, older than the {days}-day "
                                                f"cutoff ({cutoff})"))
        else:
            rows.append(_verdict(row, KEEP, f"finished {finished}, inside the {days}-day window"))
    return rows


def _by_recovery_window(files: list[dict[str, Any]], *, cutoff: str,
                        days: int) -> list[dict[str, Any]]:
    """Anchored on the newest FULL at or before the cutoff; everything from there on is required.

    Judged **per database**, because a chain belongs to one: a single SQL Server directory holds
    every database on the instance, and one anchor across all of them would judge a database backed
    up nightly by one backed up monthly.
    """
    rows = []
    for group in _by_database(files).values():
        anchor, why = _anchor(group, cutoff=cutoff)
        for row in group:
            finished = _normalise(row.get("finished_at"))
            if not finished:
                rows.append(_verdict(row, KEEP, "no finished_at: age unknown, so not judged"))
            elif anchor is None:
                rows.append(_verdict(row, KEEP, why))
            elif finished >= anchor:
                rows.append(_verdict(row, KEEP,
                                     f"at or after the anchor full ({anchor}); needed to restore "
                                     f"into the {days}-day window"))
            else:
                rows.append(_verdict(row, OBSOLETE,
                                     f"older than the anchor full ({anchor}); nothing in the "
                                     f"{days}-day window restores from it"))
    return rows


def _by_database(files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Oracle and PostgreSQL report no database and collapse into one group, which is right:
    their backups are whole-instance."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in files:
        groups.setdefault(str(row.get("database") or ""), []).append(row)
    return groups


def _anchor(group: list[dict[str, Any]], *, cutoff: str) -> tuple[str | None, str]:
    """The ``finished_at`` every retained file must reach back to, or ``None`` with the reason
    nothing in this group may be deleted."""
    fulls = sorted(_normalise(row.get("finished_at")) for row in group
                   if row.get("kind") == FULL and _normalise(row.get("finished_at")))
    if not fulls:
        return None, ("no full backup in this set: the differentials and logs here have nothing to "
                      "restore onto, so none of them can be spared")
    older = [stamp for stamp in fulls if stamp <= cutoff]
    if not older:
        return None, (f"the oldest full ({fulls[0]}) is newer than the cutoff ({cutoff}): the chain "
                      "does not reach back to the far edge of the window yet")
    return older[-1], ""


def _verdict(row: dict[str, Any], verdict: str, reason: str) -> dict[str, Any]:
    return {**row, "verdict": verdict, "reason": reason}


def _days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise RetentionError(
            f"retention_days must be a whole number of days; got {value!r}.") from exc
    if days < 1:
        # Zero would mark the entire set obsolete, including the backup taken a minute ago. If that
        # is genuinely wanted it is a delete-files call with explicit paths, not a retention policy.
        raise RetentionError("retention_days must be at least 1.")
    return days


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _normalise(value: Any) -> str:
    """The same normalisation the listing applies, so the two can be compared as text."""
    text = str(value or "").strip()
    return text.replace("T", " ")[:19] if text else ""
