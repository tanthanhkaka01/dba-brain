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

**Under both rules each database's newest FULL, and everything after it, is kept whatever its age**
(:func:`_keep_newest_chain`). Without that floor, a database whose backups had stopped for longer
than the window lost every backup it had on the next prune - the newest full included, at exactly
the moment the backups were most needed (review 0.25.0, B4.3).

Both rules keep a file whose ``finished_at`` the engine could not state: "unknown age" and "old"
are not the same fact, and only one of them is a reason to delete something.
"""

from __future__ import annotations

from db_ops.lib import errors
from datetime import datetime, timedelta, timezone
from typing import Any
# Re-exported: the vocabulary itself lives in db_ops/lib/backup_kinds.py, once.
from db_ops.lib import timezone as timezone_lib
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


class RetentionError(errors.ConfigError):
    """The retention plan could not be produced."""


def plan_retention(files: list[dict[str, Any]], *, retention_days: int = DEFAULT_RETENTION_DAYS,
                   mode: str = AGE, now: datetime | None = None,
                   retention_seconds: int | None = None) -> dict[str, Any]:
    """Split ``files`` into what is kept and what is obsolete, with a reason for each.

    ``files`` are rows as :func:`db_ops.common.backupfiles.list_backup_files` returns them.

    ``retention_seconds``, when positive, is the window exactly as the config states it, and wins
    over ``retention_days``. The planner reasoned in whole days only, so a `cleanup_retention` under a
    day became 0 days and the caller replaced it with 14 - a two-hour lab retention kept two weeks
    of backups, without a word (review 0.25.0, B4.4).
    """
    seconds = int(retention_seconds or 0)
    days = _days(retention_days) if seconds <= 0 else seconds / 86400.0
    window = f"{int(days)}-day" if seconds <= 0 else (
        f"{int(days)}-day" if seconds % 86400 == 0 else f"{seconds}-second")
    rule = str(mode or AGE).strip().lower()
    if rule not in MODES:
        raise RetentionError(f"mode must be one of {', '.join(MODES)}; got {mode!r}.")
    span = timedelta(days=days) if seconds <= 0 else timedelta(seconds=seconds)
    # The file stamps this is compared against are what the database server printed, and they
    # carry no zone, so both sides have to be on one clock or the cutoff is wrong by the offset.
    # Where the listing says how old each file is on the clock that stamped it (`age_seconds`),
    # that clock is the one used - see `_source_cutoff`. Where it does not, the operator's wall
    # clock stands in for it, which is right when the server keeps the operator's time.
    moment = now or timezone_lib.display_now()
    cutoff = _source_cutoff(files, span) or _stamp(moment - span)

    rows = _by_age(files, cutoff=cutoff, days=window) if rule == AGE \
        else _by_recovery_window(files, cutoff=cutoff, days=window)
    rows = _keep_newest_chain(rows)

    obsolete = [row for row in rows if row["verdict"] == OBSOLETE]
    keep = [row for row in rows if row["verdict"] == KEEP]
    return {
        "mode": rule,
        # Whole days, as the field always was; 0 under a day - `retention_seconds` is exact.
        "retention_days": days if seconds <= 0 else seconds // 86400,
        "retention_seconds": seconds if seconds > 0 else int(days * 86400),
        # The window as words, in the unit it was given: "14-day", "7200-second".
        "window": window,
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
        "reclaimable_bytes": sum(int(row.get("size_bytes") or 0) for row in obsolete),
        "sizes_known": all(row.get("size_bytes") is not None for row in obsolete),
    }


def _source_cutoff(files: list[dict[str, Any]], span: timedelta) -> str:
    """The cutoff on the clock that stamped the files, or ``""`` when the listing does not say.

    A row that carries ``age_seconds`` states two instants on its own machine's clock: when the
    backup finished, and - that plus its age - what time it was there when the listing was taken.
    "Now, there" minus the window is the cutoff every ``finished_at`` of that listing can be
    compared with as text, whatever zone this node shows.

    Without it the cutoff was this node's wall clock. A node at +08 judging a lab container that
    keeps UTC put the cutoff six hours after the present: a backup finished a minute earlier read
    as older than a two-hour window, and only the newest-chain floor kept anything at all (the
    0.26.0 soak's labs, 2026-10-02). At fourteen days the same eight hours went unnoticed.
    """
    there_now: datetime | None = None
    for row in files:
        age = row.get("age_seconds")
        finished = _normalise(row.get("finished_at"))
        if age is None or not finished:
            continue
        try:
            moment = datetime.strptime(finished, "%Y-%m-%d %H:%M:%S") + timedelta(seconds=int(age))
        except (TypeError, ValueError):
            continue
        # One listing, one clock: the rows agree to the second or two the listing took. The
        # latest is the closest to when it ended.
        there_now = moment if there_now is None else max(there_now, moment)
    return _stamp(there_now - span) if there_now is not None else ""


def _by_age(files: list[dict[str, Any]], *, cutoff: str, days: str) -> list[dict[str, Any]]:
    """Older than the cutoff. :func:`_keep_newest_chain` then spares each database's newest chain."""
    rows = []
    for row in files:
        finished = _normalise(row.get("finished_at"))
        if not finished:
            rows.append(_verdict(row, KEEP, "no finished_at: age unknown, so not judged"))
        elif finished < cutoff:
            rows.append(_verdict(row, OBSOLETE, f"finished {finished}, older than the {days} "
                                                f"cutoff ({cutoff})"))
        else:
            rows.append(_verdict(row, KEEP, f"finished {finished}, inside the {days} window"))
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
                                     f"into the {days} window"))
            else:
                rows.append(_verdict(row, OBSOLETE,
                                     f"older than the anchor full ({anchor}); nothing in the "
                                     f"{days} window restores from it"))
    return rows


def _keep_newest_chain(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The floor under both rules: each database's newest FULL, and everything after it, is kept.

    Under **age** a database whose backups had been failing (or whose job was paused) for longer
    than the window lost **every** backup on the next ``prune --apply``, newest FULL included -
    exactly the situation the estate's monitoring exists for (review 0.25.0, B4.3). A database with
    no FULL in the listing is left to the rule that judged it.
    """
    newest: dict[str, str] = {}
    for row in rows:
        finished = _normalise(row.get("finished_at"))
        if row.get("kind") == FULL and finished:
            key = str(row.get("database_name") or "")
            newest[key] = max(newest.get(key, ""), finished)
    kept = []
    for row in rows:
        anchor = newest.get(str(row.get("database_name") or ""))
        finished = _normalise(row.get("finished_at"))
        if row["verdict"] == OBSOLETE and anchor and finished >= anchor:
            reason = ("the newest full of this database - kept regardless of age"
                      if row.get("kind") == FULL and finished == anchor
                      else f"after the newest full ({anchor}) - its chain is kept regardless of age")
            row = _verdict(row, KEEP, reason)
        kept.append(row)
    return kept


def _by_database(files: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Oracle and PostgreSQL report no database and collapse into one group, which is right:
    their backups are whole-instance."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in files:
        groups.setdefault(str(row.get("database_name") or ""), []).append(row)
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
