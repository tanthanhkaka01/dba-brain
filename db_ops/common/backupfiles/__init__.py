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

    files = [row for row in engine.list_files(request) if row["kind"] in kinds]
    # One SQL Server directory holds every database on the instance, so walking a chain there
    # without naming one mixes them: the "diff after the full" would be some other database's.
    # Oracle and PostgreSQL report no database, so asking for one there finds nothing - which is
    # the honest answer rather than a filter that silently does nothing.
    database = str(request.get("database") or "").strip()
    if database:
        files = [row for row in files
                 if str(row.get("database") or "").lower() == database.lower()]
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
        if before_text and _later(stamp, before_text):
            continue
        kept.append(row_)
    return kept


def _later(stamp: str, reference: str) -> bool:
    """Is ``stamp`` after ``reference``?

    Compared as text after normalising the ``T`` separator, which works because every engine here
    reports ``YYYY-MM-DD HH:MM:SS`` — sortable as a string by construction. Parsing to datetimes
    would add a timezone question none of these strings answer.
    """
    return _normalise(stamp) > _normalise(reference)


def _normalise(stamp: str) -> str:
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
        newest[(str(row_.get("database") or ""), row_["kind"])] = row_
    return sorted(newest.values(), key=lambda r: (r.get("finished_at") or "", r["path"]))


def row(*, path: str, kind: str, database: str | None = None,
        size: int | None = None, finished_at: str | None = None,
        **extra: Any) -> dict[str, Any]:
    """One listed backup, in the shape every engine returns.

    Built through here so the three engines cannot drift into naming the same field differently -
    the whole point of the command is that a caller does not have to branch per engine.
    """
    return {"path": path, "kind": kind, "database": database,
            "size": size, "finished_at": _stamp(finished_at), **extra}


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
