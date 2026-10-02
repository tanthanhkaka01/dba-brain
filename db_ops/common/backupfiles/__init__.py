"""Listing the backup files an engine has, classified as full / diff / log.

One request shape for three engines that answer the question in completely different ways:

* **SQL Server** - the files carry their own headers, so the *instance* is asked
  (``RESTORE HEADERONLY``). Nothing on disk has to be parsed by name.
* **Oracle** - RMAN owns the catalogue, so ``rman`` is asked (``LIST BACKUP SUMMARY``). An RMAN
  directory is flat and its file names say nothing about the chain.
* **PostgreSQL** - the layout *is* the answer: ``base/<stamp>_FULL``, ``base/<stamp>_INCR``,
  ``wal/``. There is nothing to interrogate.

Each returns the same rows, so a caller can list a set from any of the three and get back
``kind`` / ``path`` / ``database`` / ``size`` / ``finished_at`` without knowing which engine
produced it. Where a field genuinely does not exist for an engine it is ``null`` rather than
invented - PostgreSQL has no per-database backups, so ``database`` is null there and saying
otherwise would let a caller filter on something that was never true.
"""

from __future__ import annotations

from typing import Any
# Re-exported: the vocabulary itself lives in db_ops/lib/backup_kinds.py, once.
from db_ops.lib.backup_kinds import DIFF, FULL, LOG  # noqa: F401

#: The three levels, named the same way for every engine.
#: A fourth thing, and the reason it is named: Oracle writes a controlfile/spfile autobackup every
#: 15 minutes here, and calling those "full" made the CLOUD lab report 610 full backups where it
#: has one level 0. They are not restorable as a full - a caller filtering for `full` and getting
#: them would pick one and fail. Excluded from the default answer; ask for it to see them.
CONTROLFILE = "controlfile"
KINDS = (FULL, DIFF, LOG, CONTROLFILE)
DEFAULT_KINDS = (FULL, DIFF, LOG)


class BackupListError(ValueError):
    """The listing could not be produced."""


#: Marks the line a host listing ends with: that machine's own clock, printed by the same shell
#: that listed the files. One spelling for the engines that list through a shell.
HOST_NOW = "__HOST_NOW__"


def age_seconds(finished: Any, now: Any) -> int | None:
    """How old a backup is, with both instants read off ONE clock: the machine's that stamped it.

    ``finished_at`` carries no zone - it is what the engine or the host printed - so its age cannot
    be worked out against this node's clock without knowing the offset between the two. A node at
    +08 judging a container that keeps UTC read a backup finished a minute ago as eight hours old,
    and a two-hour retention marked it obsolete at once (the lab, 2026-10-02). So each engine also
    asks the same machine what time it is *now*, and the age is the difference: no zone enters it.

    ``None`` when either instant is missing or unreadable - an unknown age is not an old one.
    """
    from datetime import datetime

    def instant(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        text = str(value or "").strip().replace("T", " ")[:19]
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    start, end = instant(finished), instant(now)
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def list_backup_files(request: dict[str, Any]) -> dict[str, Any]:
    """List the backups one engine holds. Returns ``{"files": [...], "counts": {...}}``.

    A plain dispatch on ``db_type``: three engines, three modules, and the branch is here so a
    caller never has to know which one it is talking to.
    """
    db_type = str(request.get("db_type") or "").strip().lower()
    # One field, always an array — `["full"]` is how you ask for one level. A second singular
    # field would be two ways to say the same thing, and eventually two ways that disagree.
    wanted = request.get("kinds") or DEFAULT_KINDS
    if isinstance(wanted, str):
        raise BackupListError('kinds must be an array, e.g. ["full"] — not a string.')
    kinds = [str(k).strip().lower() for k in wanted]
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown:
        raise BackupListError(f"Unknown kind(s) {', '.join(unknown)}. Known: {', '.join(KINDS)}.")

    if db_type == "sqlserver":
        from db_ops.common.backupfiles import sqlserver as engine
    elif db_type == "oracle":
        from db_ops.common.backupfiles import oracle as engine
    elif db_type in {"postgresql", "postgres"}:
        from db_ops.common.backupfiles import postgresql as engine
    else:
        raise BackupListError(
            f"db_type must be sqlserver, oracle or postgresql; got {db_type!r}."
        )

    unreadable: list[str] = []
    listed = (engine.list_files(request, skipped=unreadable) if db_type == "sqlserver"
              else engine.list_files(request))
    files = [row for row in listed if row["kind"] in kinds]
    # One SQL Server directory holds every database on the instance, so walking a chain there
    # without naming one mixes them: the "diff after the full" would be some other database's.
    # Oracle and PostgreSQL report no database, so asking for one there finds nothing - which is
    # the honest answer rather than a filter that silently does nothing.
    database = str(request.get("database_name") or request.get("database") or "").strip()
    if database:
        files = [row for row in files
                 if str(row.get("database_name") or "").lower() == database.lower()]
    files = _in_window(files, after=request.get("after"), before=request.get("before"))
    files.sort(key=lambda row: (row.get("finished_at") or "", row["path"]))
    if request.get("latest"):
        files = _latest_only(files)
    return {
        "files": files,
        "counts": {kind: sum(1 for row in files if row["kind"] == kind) for kind in KINDS},
        # The moment the caller should pass as `after` on the next call. Returned rather than left
        # to be dug out of the last row, because that is the whole point of the three-step walk:
        # newest full -> diffs after it -> logs after those.
        "newest_finished_at": files[-1].get("finished_at") if files else None,
        # Files named like backups that the engine could not read as one (SQL Server only).
        "unreadable": unreadable,
    }


def _in_window(files: list[dict[str, Any]], *, after: Any, before: Any) -> list[dict[str, Any]]:
    """Keep the backups finishing strictly after ``after`` and at or before ``before``.

    ``after`` is exclusive and ``before`` inclusive on purpose. Chaining a restore means "what came
    *after* the full I already have" - including that full again would restore it twice - while a
    point-in-time bound means "everything up to and including this moment".

    A row with no ``finished_at`` is kept: it is an engine that could not state one, and dropping
    it would silently shorten a chain. Better an extra piece the caller can see than a missing one
    it cannot.
    """
    after_text = str(after or "").strip()
    before_text = str(before or "").strip()
    if not after_text and not before_text:
        return list(files)
    kept: list[dict[str, Any]] = []
    for row_ in files:
        stamp = str(row_.get("finished_at") or "").strip()
        if not stamp:
            kept.append(row_)
            continue
        if after_text and not _later(stamp, after_text):
            continue
        # `before` is a point in time, converted to UTC; a row that knows its UTC finish is judged
        # by it (PostgreSQL, whose `stat` times are the host's clock).
        if before_text and _later(str(row_.get("finished_at_utc") or stamp), before_text):
            continue
        kept.append(row_)
    return kept


def _later(stamp: str, reference: str) -> bool:
    """Is ``stamp`` after ``reference``?

    Compared as ``YYYY-MM-DD HH:MM:SS`` text in the server's clock, which sorts as a string by
    construction. The engines report their finish times that way; what a caller passes as a bound
    may carry an offset - the bot's point in time does, `2026-09-25 07:13:40 +08:00` - and that
    offset used to be cut off with the rest of the string, so the window was eight hours off with
    no error anywhere (the point-in-time drill, 2026-09-25). It is converted instead.
    """
    return _normalise(stamp) > _normalise(reference)


def _normalise(stamp: str) -> str:
    from db_ops.lib.restore.moment import MomentError, server_clock_text

    text = str(stamp).strip()
    # Fractional seconds are dropped: a listing compares to the second, and the moment parser
    # reads none.
    head, dot, rest = text.partition(".")
    if dot and len(head) >= 19:
        text = head + rest.lstrip("0123456789")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return server_clock_text(text)
    except MomentError:
        # Not a moment the parser reads: the old text rule, on the text as it came.
        return str(stamp).strip().replace("T", " ")[:19]


def _latest_only(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The newest backup of each kind, per database.

    Per database, not per kind alone: a SQL Server backup directory holds every database on the
    instance, so "the latest full" is meaningless across the set — it would answer with whichever
    database happened to be backed up last. Oracle and PostgreSQL report no database, so they
    collapse to one row per kind, which is the right answer there.
    """
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for row_ in files:  # already sorted oldest-first, so the last write wins
        newest[(str(row_.get("database_name") or ""), row_["kind"])] = row_
    return sorted(newest.values(), key=lambda r: (r.get("finished_at") or "", r["path"]))


def row(*, path: str, kind: str, database: str | None = None,
        size: int | None = None, finished_at: str | None = None,
        age: int | None = None, **extra: Any) -> dict[str, Any]:
    """One listed backup, in the shape every engine returns.

    Built through here so the three engines cannot drift into naming the same field differently -
    the whole point of the command is that a caller does not have to branch per engine.

    ``age`` is :func:`age_seconds` - how old the backup was on the clock that stamped
    ``finished_at`` - and ``None`` where the listing could not say.
    """
    return {"path": path, "kind": kind, "database_name": database,
            "size_bytes": size, "finished_at": _stamp(finished_at), "age_seconds": age, **extra}


def _stamp(value: Any) -> str | None:
    """``YYYY-MM-DD HH:MM:SS``, whatever the engine happened to print.

    The three disagree: SQL Server returns ISO with a ``T``, Oracle the NLS format it was told to
    use, and PostgreSQL a ``stat`` mtime with nanoseconds and an offset. Normalising here rather
    than at each caller is what makes ``finished_at`` mean one thing — the value is handed straight
    back as ``after`` on the next call, and a caller comparing two engines' strings should not have
    to know which produced which.
    """
    text = str(value or "").strip()
    if not text:
        return None
    return text.replace("T", " ")[:19]
