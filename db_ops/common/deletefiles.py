"""Deleting backup files by full path — one file at a time, on whatever the file lives on.

The old cleanup (``backup_restore/delete_backup.py``) decided *and* deleted in one step: it read
``restore_config.json``, globbed ``*.bak``/``*.trn`` under the configured import share, worked out
which were older than ``copy_recent_hours``, and removed them. Three things were wrong with that
shape, and all three are why this module exists:

* **It chose what to delete from a rule nobody could inspect first.** The set was computed inside
  the deleting process, so "what would this remove" had no answer short of reading the code and
  trusting the clock. Here the caller lists (``list-backup-files``), decides, and passes explicit
  paths — the decision is visible, reviewable, and can come from a human.
* **It only knew one place.** A UNC share reached from Windows. Backups on this estate also live on
  an Ubuntu VM, inside a container and on an Oracle Cloud host, and none of those were reachable.
  This runs wherever :mod:`db_ops.common.hostcmd` can reach.
* **A pattern deletes what you did not look at.** ``*.bak`` under a root is one typo away from the
  full backup you are restoring from. Nothing here expands a pattern: a path is a path, and a
  wildcard character in one is refused.

**Not finding the file is success.** Delete is a statement about the end state, not about the act,
so a caller that re-runs after a partial failure gets ``not_found`` and moves on rather than an
error it has to special-case. The status is still reported separately from ``deleted``, because
"there was nothing there" and "there was, and now there is not" are different facts about what the
last run actually did.

**Oracle: this removes the file, not the catalogue entry.** An RMAN backup piece deleted from disk
leaves RMAN still listing it, and the next ``RESTORE`` picks it and fails. Delete those with
``DELETE BACKUPPIECE`` / ``DELETE OBSOLETE`` through RMAN, and use this only for files RMAN does
not own.
"""

from __future__ import annotations

import shlex
from typing import Any

from db_ops.common.hostcmd import WINDOWS, Host, HostCommandError, open_client, parse_host, run

#: Per-file outcomes. ``deleted`` and ``not_found`` are both success — see the module docstring.
DELETED = "deleted"
NOT_FOUND = "not_found"
SKIPPED = "skipped"
FAILED = "failed"
SUCCESS_STATUSES = (DELETED, NOT_FOUND)

#: Refused inside a path. Expanding one here would delete files the caller never listed, and the
#: whole point of taking full paths is that the caller has seen every one of them.
WILDCARDS = ("*", "?", "[")


class DeleteFileError(ValueError):
    """The request could not be honoured. Distinct from a file that could not be deleted."""


def delete_file(request: dict[str, Any]) -> dict[str, Any]:
    """Delete one file named by its full path. Returns one result row.

    The unit of work, and the only thing that ever touches a file: :func:`delete_files` is a loop
    over this. One file per command means one answer per file — a batch that half-succeeds reports
    which half, instead of an exit code covering a set.
    """
    host = parse_host(request.get("host"))
    path = _validated_path(request.get("path"), host=host,
                           must_be_under=request.get("must_be_under"))
    row = _delete_one(host, path, dry_run=bool(request.get("dry_run")))
    return {"file": row, **_totals([row]), "dry_run": bool(request.get("dry_run"))}


def delete_files(request: dict[str, Any]) -> dict[str, Any]:
    """Delete every named file, one at a time, over a single connection to the host.

    Every path is validated **before** the first delete. A batch that stops halfway because path
    nine was relative would have already removed eight files the caller may not have wanted gone on
    their own — refusing the whole request costs nothing, since nothing has happened yet.
    """
    host = parse_host(request.get("host"))
    raw = request.get("paths")
    if isinstance(raw, str):
        raise DeleteFileError('paths must be an array of full paths, e.g. ["/b/a.bkp"] — not a string.')
    if not raw:
        raise DeleteFileError("paths is required and must name at least one file.")
    must_be_under = request.get("must_be_under")
    paths = [_validated_path(item, host=host, must_be_under=must_be_under) for item in raw]

    dry_run = bool(request.get("dry_run"))
    stop_on_error = bool(request.get("stop_on_error"))

    # One connection for the batch; `run` borrows it and leaves it open. Reconnecting per file is
    # the cost this layer already learned about the hard way with per-file SFTP.
    client = None if host.is_local else open_client(host)
    rows: list[dict[str, Any]] = []
    try:
        for path in paths:
            row = _delete_one(host, path, dry_run=dry_run, client=client)
            rows.append(row)
            if stop_on_error and row["status"] == FAILED:
                break
    finally:
        if client is not None:
            client.close()

    return {"files": rows, **_totals(rows), "dry_run": dry_run,
            # Stated rather than inferred from len(files) != len(paths): a caller reading a stored
            # response should not have to reconstruct whether a short list means "stopped" or
            # "that is all there was".
            "stopped_early": len(rows) < len(paths)}


def _delete_one(host: Host, path: str, *, dry_run: bool, client: Any = None) -> dict[str, Any]:
    """Check and delete in one command, because two round trips can disagree.

    Statting first and deleting second is a race with whatever else writes to a backup directory,
    and on a slow link it is also twice the latency per file. The shell decides and reports in one
    breath; this only has to read the word it printed.
    """
    command = _windows_command(path, dry_run=dry_run) if host.is_windows \
        else _posix_command(path, dry_run=dry_run)
    try:
        result = run(host, command, timeout=120, client=client)
    except HostCommandError as exc:
        return _row(path, FAILED, reason=str(exc))

    token, _, size_text = (result.get("stdout") or "").strip().partition(" ")
    size = int(size_text.strip() or 0) if size_text.strip().isdigit() else 0

    if token == "DELETED":
        return _row(path, DELETED, size=size)
    if token == "WOULD":
        return _row(path, SKIPPED, size=size, reason="dry_run: not deleted")
    if token == "MISSING":
        return _row(path, NOT_FOUND, reason="already gone")
    if token == "DIR":
        # Never recursive, and never a directory: a caller that meant one file and passed its
        # parent would otherwise lose the whole backup set to a single missing basename.
        return _row(path, FAILED, reason="path is a directory; this command deletes files only")
    stderr = (result.get("stderr") or "").strip()
    return _row(path, FAILED,
                reason=stderr or f"unexpected output {(result.get('stdout') or '').strip()!r}")


def _posix_command(path: str, *, dry_run: bool) -> str:
    quoted = shlex.quote(path)
    act = 'echo "WOULD $S"' if dry_run else \
        f'if rm -f {quoted} 2>/dev/null; then echo "DELETED $S"; else echo "FAILED $S"; fi'
    return (
        f'if [ -d {quoted} ]; then echo "DIR 0"; '
        f'elif [ -e {quoted} ]; then S=$(stat -c %s {quoted} 2>/dev/null || echo 0); {act}; '
        f'else echo "MISSING 0"; fi'
    )


def _windows_command(path: str, *, dry_run: bool) -> str:
    quoted = _ps_quote(path)
    act = ('"WOULD $s"' if dry_run else
           # Re-testing after Remove-Item rather than trusting it: -Force on a file held open by
           # another process reports nothing and leaves the file there.
           'Remove-Item -LiteralPath ' + quoted + ' -Force -ErrorAction SilentlyContinue; '
           'if (Test-Path -LiteralPath ' + quoted + ') { "FAILED $s" } else { "DELETED $s" }')
    return (
        f'if (Test-Path -LiteralPath {quoted} -PathType Container) {{ "DIR 0" }} '
        f'elseif (Test-Path -LiteralPath {quoted}) {{ '
        f'$s = (Get-Item -LiteralPath {quoted}).Length; {act} }} '
        f'else {{ "MISSING 0" }}'
    )


def _ps_quote(value: str) -> str:
    """A PowerShell single-quoted literal: no expansion, and a path is never a command."""
    return "'" + str(value).replace("'", "''") + "'"


def _validated_path(value: Any, *, host: Host, must_be_under: Any = None) -> str:
    """The refusals, all before anything is deleted.

    Absolute only, no wildcard, and optionally inside a stated root. A relative path resolves
    against whatever directory the shell happened to start in, which for a delete is a different
    file on every runtime.
    """
    path = str(value or "").strip()
    if not path:
        raise DeleteFileError("path is required and must be a full path.")
    if any(char in path for char in WILDCARDS):
        raise DeleteFileError(
            f"path must name one file, not a pattern: {path!r}. List with list-backup-files and "
            "pass the paths it returned."
        )
    if not _is_absolute(path, host=host):
        raise DeleteFileError(f"path must be absolute: {path!r}.")
    root = str(must_be_under or "").strip()
    if root and not _under(path, root, host=host):
        # An optional fence for a caller that already knows the one directory it is allowed to
        # clean. It cannot be the default: the paths list-backup-files returns for Oracle and
        # PostgreSQL live under directories this layer is not told about.
        raise DeleteFileError(f"path {path!r} is not under must_be_under {root!r}.")
    return path


def _is_absolute(path: str, *, host: Host) -> bool:
    if host.runtime == WINDOWS:
        # A UNC share is as absolute as a drive letter, and is where SQL Server backups live here.
        return path.startswith("\\\\") or (len(path) > 2 and path[1] == ":" and path[2] in "\\/")
    return path.startswith("/")


def _under(path: str, root: str, *, host: Host) -> bool:
    """Is ``path`` inside ``root``? Compared as text, case-insensitively on Windows.

    Text rather than resolved paths because neither end is on this machine — there is nothing here
    to resolve a symlink or a mapped drive against, and pretending otherwise would answer
    confidently about a filesystem this process cannot see.
    """
    if host.runtime == WINDOWS:
        normalised, base = path.replace("/", "\\").lower(), root.replace("/", "\\").lower()
        separator = "\\"
    else:
        normalised, base, separator = path, root, "/"
    base = base.rstrip(separator)
    return normalised.startswith(base + separator)


def _row(path: str, status: str, *, size: int = 0, reason: str = "") -> dict[str, Any]:
    return {"path": path, "status": status, "size": size, "reason": reason}


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts a caller charts, and the one number an operator asks for: how much came back."""
    return {
        "counts": {status: sum(1 for row in rows if row["status"] == status)
                   for status in (DELETED, NOT_FOUND, SKIPPED, FAILED)},
        # Only what was actually removed. Counting a dry run's bytes here would report space freed
        # by a command that freed none.
        "bytes_freed": sum(row["size"] for row in rows if row["status"] == DELETED),
        "failed": [row["path"] for row in rows if row["status"] == FAILED],
    }
