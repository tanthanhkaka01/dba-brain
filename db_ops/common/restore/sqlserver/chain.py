"""Choosing the SQL Server restore chain, including for a point in time.

Pure: it takes backup headers as data and returns which ones to restore, in order. No file is
listed, no instance is queried, no config is read - the caller reads ``RESTORE HEADERONLY`` (or an
equivalent catalogue) and hands the rows over. That is what makes the rule below testable against
awkward chains without a SQL Server anywhere near it, and it is the rule that decides whether a
recovery lands on the right second.

**Why headers rather than file names.** The chain is a property of LSNs, not of the strings someone
put in the file name. A ``..._FULL_20260806_084916.bak`` that was actually taken from a different
database, or a differential whose base was superseded by a later full, both read perfectly in a
sorted listing and both produce a restore that either fails at the last step or - much worse -
succeeds at an earlier point in time than the operator believes. SQL Server states the answer in
``database_backup_lsn`` / ``checkpoint_lsn`` / ``first_lsn`` / ``last_lsn``; this module reads it.

**The rule.**

* **FULL** - the newest whose backup finished at or before the target moment. A full that finished
  after it cannot be the base: its data already contains changes past the moment being recovered
  to, and no amount of log restoring removes them.
* **DIFF** - the newest differential whose ``database_backup_lsn`` equals the chosen full's
  ``checkpoint_lsn``, and which also finished at or before the moment. A differential chained to a
  *different* full is the classic silent failure: it restores, and it restores the wrong base.
* **LOG** - every log after the base, in ``first_lsn`` order, up to and including the one whose
  interval contains the moment. That last one carries ``STOPAT``.

**A moment past the end of the logs is refused, not rounded down.** If no log covers it, the best
the chain can reach is the end of the last log - which may be hours earlier. Restoring to that
silently would answer a different question than the one asked, and the operator would have no way
to notice; the data simply looks older than expected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

FULL = "FULL"
DIFF = "DIFF"
LOG = "LOG"

#: What ``RESTORE HEADERONLY`` calls each type, mapped to the names used here.
_HEADER_TYPES = {"1": FULL, "5": DIFF, "2": LOG, FULL: FULL, DIFF: DIFF, LOG: LOG,
                 "D": FULL, "I": DIFF, "L": LOG}


class RestoreChainError(ValueError):
    """No chain can satisfy the request - stated rather than approximated."""


@dataclass(frozen=True)
class BackupHeader:
    """One backup, as SQL Server describes it. ``path`` is where the caller found it."""

    path: str
    database_name: str
    backup_type: str
    first_lsn: int
    last_lsn: int
    checkpoint_lsn: int = 0
    database_backup_lsn: int = 0
    backup_start_date: datetime | None = None
    backup_finish_date: datetime | None = None


@dataclass(frozen=True)
class RestoreChain:
    """The ordered answer: one full, an optional differential, then logs."""

    full: BackupHeader
    diff: BackupHeader | None
    logs: tuple[BackupHeader, ...]
    stopat: datetime | None = None

    @property
    def ordered(self) -> tuple[BackupHeader, ...]:
        return tuple([self.full] + ([self.diff] if self.diff else []) + list(self.logs))

    @property
    def stopat_path(self) -> str:
        """The one backup that carries STOPAT - always the last log, never a full or diff."""
        return self.logs[-1].path if (self.stopat and self.logs) else ""


def _normalise_type(value: Any) -> str:
    key = str(value or "").strip().upper()
    return _HEADER_TYPES.get(key, key)


def parse_headers(rows: Iterable[dict[str, Any]]) -> list[BackupHeader]:
    """Build headers from ``RESTORE HEADERONLY`` rows. Unknown types are dropped, not guessed.

    A row whose type this does not recognise is a row whose place in the chain is unknown, and
    guessing it wrong is exactly the failure this module exists to prevent - a file-copy-only or
    partial backup treated as a full would produce a chain that restores and is wrong.
    """
    headers: list[BackupHeader] = []
    for row in rows:
        backup_type = _normalise_type(row.get("BackupType") or row.get("backup_type"))
        if backup_type not in {FULL, DIFF, LOG}:
            continue
        headers.append(BackupHeader(
            path=str(row.get("path") or row.get("Path") or ""),
            database_name=str(row.get("DatabaseName") or row.get("database_name") or ""),
            backup_type=backup_type,
            first_lsn=int(float(row.get("FirstLSN") or row.get("first_lsn") or 0)),
            last_lsn=int(float(row.get("LastLSN") or row.get("last_lsn") or 0)),
            checkpoint_lsn=int(float(row.get("CheckpointLSN") or row.get("checkpoint_lsn") or 0)),
            database_backup_lsn=int(float(
                row.get("DatabaseBackupLSN") or row.get("database_backup_lsn") or 0)),
            backup_start_date=row.get("BackupStartDate") or row.get("backup_start_date"),
            backup_finish_date=row.get("BackupFinishDate") or row.get("backup_finish_date"),
        ))
    return headers


def _at_or_before(header: BackupHeader, moment: datetime | None) -> bool:
    if moment is None:
        return True
    finished = header.backup_finish_date
    return finished is None or finished <= moment


def select_chain(
    headers: Iterable[BackupHeader],
    *,
    database: str,
    point_in_time: datetime | None = None,
) -> RestoreChain:
    """The backups to restore for one database, in order. Raises when none can satisfy it."""
    candidates = [h for h in headers
                  if not database or h.database_name.lower() == database.lower()]
    if not candidates:
        raise RestoreChainError(
            f"No backups found for database {database!r} in the headers supplied."
        )

    fulls = [h for h in candidates if h.backup_type == FULL and _at_or_before(h, point_in_time)]
    if not fulls:
        moment = f" finishing at or before {point_in_time}" if point_in_time else ""
        raise RestoreChainError(
            f"{database}: no FULL backup{moment}. A full that finished after the target moment "
            "cannot be the base - it already contains changes past it, and restoring logs cannot "
            "remove them."
        )
    full = max(fulls, key=lambda h: (h.backup_finish_date or datetime.min, h.last_lsn))

    diffs = [h for h in candidates
             if h.backup_type == DIFF
             and h.database_backup_lsn == full.checkpoint_lsn
             and _at_or_before(h, point_in_time)]
    diff = max(diffs, key=lambda h: (h.backup_finish_date or datetime.min, h.last_lsn)) if diffs else None

    base = diff or full
    logs = sorted((h for h in candidates
                   if h.backup_type == LOG and h.last_lsn > base.last_lsn),
                  key=lambda h: h.first_lsn)

    if point_in_time is None:
        return RestoreChain(full=full, diff=diff, logs=tuple(logs))

    # Every log up to and including the one whose interval contains the moment. Stopping one
    # short would recover to an earlier second than asked for; going one further would fail,
    # because a log cannot be applied after RECOVERY.
    covering: list[BackupHeader] = []
    reached = False
    for header in logs:
        covering.append(header)
        if (header.backup_finish_date or datetime.max) >= point_in_time:
            reached = True
            break
    if not reached:
        last_end = logs[-1].backup_finish_date if logs else base.backup_finish_date
        raise RestoreChainError(
            f"{database}: no log backup covers {point_in_time}. The chain reaches {last_end} at "
            "best. Restoring to that instead would answer a different question than the one asked "
            "- take the missing log backups, or choose a moment inside the chain."
        )
    return RestoreChain(full=full, diff=diff, logs=tuple(covering), stopat=point_in_time)
