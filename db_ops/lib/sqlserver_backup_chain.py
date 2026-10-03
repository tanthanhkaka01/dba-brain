"""Which SQL Server backups a restore to a moment needs - decided BEFORE anything is copied.

A restore of SQL Server applies one chain: the newest FULL at or before the moment, the newest
DIFF taken after that FULL and at or before the moment, and the LOGs taken after whichever of the
two is the baseline - for a point in time, up to and including the first LOG that reaches the
moment, because that one carries it. The restore step has always chosen exactly this
(``backup_restore/restore_database.py``) - but only among files already staged, and the copy before
it took **every** file written in the last ``copy_recent_hours``.

That window has to reach back to the FULL, and a weekly FULL makes it eight days: on 2026-10-01 the
100.250 drill would have copied **527.9 GiB** - every daily DIFF and every LOG of eight days - for a
chain of **56.8 GiB**, and its space check refused (0.26.0 §1.74). So the copy asks this module
first, and the space check counts what it answers.

Pure: callers hand in ``(item, database, kind, timestamp)`` and get their items back. A database
for which no chain can be established - no FULL at or before the moment, or a layout with no
``FULL`` / ``DIFF`` / ``LOG`` folders - is named in ``unresolved`` and the caller copies it by its
window as before: a narrowed copy that guessed wrong fails the restore, a wide one only costs time.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: The folder each kind of backup lives in, one level under the database's folder.
KINDS = ("FULL", "DIFF", "LOG")

#: ``_YYYYMMDD_HHMMSS`` in a backup's name, with ``Z`` when the stamp is UTC. Here since 2026-10-03:
#: the copy to another machine (``common``) reads a chain from names too, and ``common`` imports
#: only ``lib`` (rules R04) - it was ``backup_restore.shell_quoting``'s, which re-exports it.
BACKUP_TIMESTAMP_RE = re.compile(r"_(\d{8})_(\d{6})(Z?)")


def backup_time_from_name(name: str) -> float | None:
    """The time a backup file's name records (``..._YYYYMMDD_HHMMSS[Z].bak``), as epoch seconds.

    A trailing ``Z`` says the stamp is UTC - what db_ops' own backup scripts write since 0.20.0.
    A name without it is read in this process's local time, as it always was: backups written by
    other tools on the source server (a maintenance-plan or Ola Hallengren job) stamp local time and
    carry no marker, and reading those as UTC would move every one of them by the server's offset.
    """
    match = BACKUP_TIMESTAMP_RE.search(name)
    if not match:
        return None
    try:
        moment = dt.datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    if match.group(3):
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.timestamp()


def kind_from_folder(name: str) -> str:
    """``FULL`` / ``DIFF`` / ``LOG`` for a backup's parent folder, ``""`` for anything else."""
    upper = str(name or "").strip().upper()
    return upper if upper in KINDS else ""


@dataclass(frozen=True)
class Candidate:
    """One backup file as the chain rule sees it. ``item`` is the caller's own handle."""

    item: Any
    database: str
    kind: str
    timestamp: float


@dataclass(frozen=True)
class ChainChoice:
    selected: tuple[Any, ...] = ()
    unresolved: tuple[str, ...] = ()
    lines: tuple[str, ...] = field(default_factory=tuple)


def restore_chain(candidates: Iterable[Candidate], *, until: float | None = None) -> ChainChoice:
    """The items a restore to ``until`` (``None`` = latest) needs, per database, oldest first."""
    by_database: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_database.setdefault(str(candidate.database or "").lower(), []).append(candidate)

    selected: list[Candidate] = []
    unresolved: list[str] = []
    lines: list[str] = []
    for key in sorted(by_database):
        files = by_database[key]
        name = files[0].database or "(no database folder)"
        fulls = [c for c in files if c.kind == "FULL" and (until is None or c.timestamp <= until)]
        if not fulls:
            unresolved.append(name)
            lines.append(f"{name}: no FULL at or before the moment - copied by its window instead")
            continue
        full = max(fulls, key=lambda c: c.timestamp)
        diffs = [c for c in files if c.kind == "DIFF" and c.timestamp >= full.timestamp
                 and (until is None or c.timestamp <= until)]
        diff = max(diffs, key=lambda c: c.timestamp) if diffs else None
        baseline = (diff or full).timestamp
        logs = sorted((c for c in files if c.kind == "LOG" and c.timestamp >= baseline),
                      key=lambda c: c.timestamp)
        if until is not None:
            # Up to and including the first LOG that reaches the moment: it is the one that holds it.
            # A later one would not chain on from the moment and SQL Server refuses it (Msg 4305).
            kept: list[Candidate] = []
            for log in logs:
                kept.append(log)
                if log.timestamp >= until:
                    break
            logs = kept
        chain = [full] + ([diff] if diff else []) + logs
        selected.extend(chain)
        lines.append(f"{name}: FULL 1 + DIFF {1 if diff else 0} + LOG {len(logs)} "
                     f"of {len(files)} file(s) on the source")
    selected.sort(key=lambda c: (c.timestamp, str(c.database).lower()))
    return ChainChoice(selected=tuple(c.item for c in selected), unresolved=tuple(unresolved),
                       lines=tuple(lines))


__all__ = ["Candidate", "ChainChoice", "KINDS", "kind_from_folder", "restore_chain"]
