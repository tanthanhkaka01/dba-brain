"""``backup-chain``, ``copy-backup-dir``, ``prune-staged-backups`` — a restore's copy, as commands.

Plumbing only: :mod:`db_ops.common.backup_copy` does the work, :mod:`db_ops.lib.response` shapes
the answer. Until 0.23.0 the copy between hosts and the cleanup of the target's staging folder ran
inside the backup_restore app, over sessions the app opened itself: a restore was one long call
with two steps nobody could run, test or watch on their own (the operator, 2026-09-25: "not one
long command - split it: common cli copy, common cli verify, common cli metadata ..."). The app
now resolves the logins and decides; these do.

**stdin only**, like ``run-sqlcmd``: every request carries SSH passwords.
"""

from __future__ import annotations

import sys
from typing import Any

from db_ops.lib import response

COMMANDS = ("backup-chain", "copy-backup-dir", "prune-staged-backups")

_LOGIN = """{"host": "192.0.2.49", "port": 22, "username": "labuser",
            "password": "..."}          // or "key_file": "/abs/path/key" - never a ref"""

USAGE = {
    "backup-chain": f"""\
Usage: <request> | python -m db_ops.common.cli backup-chain -

Which parts of a backup directory a restore needs, as path prefixes. Reads no config.

  {{"db_type": "postgresql",                  // postgresql | oracle | sqlserver - each narrows
   "source": {_LOGIN},
   "source_dir": "/opt/db_ops/backup/PG_LAB_A",   // on the source host
   "backup_dir": "/opt/db_ops/backup/ORA_LAB_A",  // oracle: the path RMAN's catalog names
   "container": "ORA_LAB_A",                      // oracle: where RMAN runs
   "point_in_time": ""}}                          // set: the whole directory

data: {{"include": [prefix, ...], "narrowed": bool}} - an empty include means "everything".
The chain: PostgreSQL the newest _FULL, the _INCRs after it and wal/; Oracle the pieces RMAN names;
SQL Server each database's newest FULL, its newest DIFF and the LOGs after them, and _cert/ (a
database with no FULL, or a layout with no FULL/DIFF/LOG folders, is copied whole).
""",
    "copy-backup-dir": f"""\
Usage: <request> | python -m db_ops.common.cli copy-backup-dir -

Copy a backup directory from one host to another as one tar stream, mirroring the source.

  {{"source": {_LOGIN},
   "source_dir": "/opt/db_ops/backup/PG_LAB_A",
   "target": {_LOGIN},
   "target_dir": "/opt/db_ops/backup/pg_restore_from_a",
   "include": ["base/20260925T004710Z_FULL", "wal/"],   // from backup-chain; [] = everything
   "window_hours": 0,            // > 0: also every file written in the last N hours (copy_selection
                                 // window) - the chain and every other restore point of the window
   "make_readable": true,       // sudo chmod -R a+rX on the source first (a live archivelog job)
   "open_for_engine": true,      // chmod -R a+rX on the target after (the engine's own uid)
   "copy_mode": "auto",          // auto: tar, else file by file | tar | sftp - pinned, no step down
   "space_check": {{"enabled": true, "factor": 2.0, "on_unknown": "refuse"}}}}   // these are the defaults

A file already on the target with the same size and not older is skipped; a staged file the source
no longer has is removed. The files still to copy must fit the target's free space, times the
factor, or nothing is copied. data: {{"copied", "skipped", "bytes_copied",
"removed_absent_at_source", "opened_for_engine", "copy_mode", "copy_fell_back", "space_check"}} -
copy_mode is how the files went (tar | sftp | none), copy_fell_back whether that was the step down
from tar, space_check what was measured (absent when there was nothing to copy).
Progress goes to stderr.
""",
    "prune-staged-backups": f"""\
Usage: <request> | python -m db_ops.common.cli prune-staged-backups -

Delete what a restore's staging folder holds past its retention - the DELETE step of a restore.

  {{"target": {_LOGIN},
   "target_dir": "/opt/db_ops/backup/pg_restore_from_a",
   "cleanup_retention": 259200}}   // seconds; 0 = keep everything

data: {{"pruned", "retention_seconds", "skipped"?, "error"?}}.
""",
}


def _log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _open(login: Any, *, role: str):
    """A connected :mod:`remote_exec` SSH session - the one executor (0.25.0); this opened a
    paramiko client of its own until then."""
    from db_ops.common import remote_exec

    if not isinstance(login, dict) or not str(login.get("host") or "").strip():
        raise ValueError(f"{role} must be an object with host, username and a password or key_file.")
    key_file = str(login.get("key_file") or "").strip()
    access: dict[str, Any] = {
        "method": "ssh", "host": str(login["host"]).strip(), "port": int(login.get("port") or 22),
        "username": str(login.get("username") or "").strip(), "password": str(login.get("password") or ""),
        "auth_type": "key" if key_file else "password", "platform": "linux",
        "timeout_seconds": int(login.get("open_timeout_seconds") or 30)}
    if key_file:
        access["key_file"] = key_file
    session = remote_exec.open_session(access)
    session.client  # connect now, so a refused login is this call's error, not a later one's
    return session


def _required(request: dict[str, Any], *names: str) -> None:
    missing = [name for name in names if not str(request.get(name) or "").strip()]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}.")


def _backup_chain(request: dict[str, Any]) -> dict[str, Any]:
    from db_ops.common import backup_copy

    _required(request, "db_type", "source_dir")
    session = _open(request.get("source"), role="source")
    try:
        include = backup_copy.chain_include(
            str(request["db_type"]), session, source_dir=str(request["source_dir"]),
            backup_dir=str(request.get("backup_dir") or ""),
            container=str(request.get("container") or ""),
            point_in_time=str(request.get("point_in_time") or ""), log=_log)
    finally:
        session.close()
    return {"include": list(include), "narrowed": bool(include)}


def _copy_backup_dir(request: dict[str, Any]) -> dict[str, Any]:
    import shlex

    from db_ops.common import backup_copy
    from db_ops.lib import restore_space
    from db_ops.lib.restore.copy_mode import parse_copy_mode

    _required(request, "source_dir", "target_dir")
    source_dir, target_dir = str(request["source_dir"]), str(request["target_dir"])
    copy_mode = parse_copy_mode(request.get("copy_mode"))
    # Absent means on, as on a restore entry: a check that has to be asked for protects only the
    # copies somebody remembered.
    space_check = restore_space.parse_space_check({"space_check": request.get("space_check")})
    if space_check.measure_restore:
        raise ValueError("space_check.measure_restore belongs to a restore entry: this command "
                         "copies files and measures only them.")
    source = _open(request.get("source"), role="source")
    try:
        if request.get("make_readable", True):
            # Readable at the moment of reading: a copy of a large set takes minutes, and the
            # archivelog job writes new 0640 pieces every 15 minutes - so a set fully readable when
            # the copy started can grow an unreadable file while it runs.
            source.run(f"sudo chmod -R a+rX {shlex.quote(source_dir)} 2>/dev/null || true")
        target = _open(request.get("target"), role="target")
        try:
            result = backup_copy.sync_backup_dir(
                source_session=source, source_dir=source_dir,
                target_session=target, target_dir=target_dir,
                include=tuple(str(item) for item in request.get("include") or ()), log=_log,
                copy_mode=copy_mode, space_check=space_check,
                window_since=_window_since(request.get("window_hours")))
            opened = (backup_copy.open_for_the_engine(target, target_dir, log=_log)
                      if request.get("open_for_engine", True) else False)
        finally:
            target.close()
    finally:
        source.close()
    return {**result.as_dict(), "opened_for_engine": opened}


def _window_since(hours: Any) -> float | None:
    """The epoch a window copy reaches back to; ``None`` for no window (0, absent, negative).

    The source's file times are absolute epochs, so this host's clock is the one to subtract from.
    """
    import time

    try:
        span = float(hours or 0)
    except (TypeError, ValueError):
        raise ValueError(f"window_hours must be a number of hours; got {hours!r}.") from None
    return time.time() - span * 3600 if span > 0 else None


def _prune_staged_backups(request: dict[str, Any]) -> dict[str, Any]:
    from db_ops.common import backup_copy

    _required(request, "target_dir")
    session = _open(request.get("target"), role="target")
    try:
        return backup_copy.prune_target_dir(
            session, str(request["target_dir"]), int(request.get("cleanup_retention") or 0), log=_log)
    finally:
        session.close()


_WORK = {"backup-chain": _backup_chain, "copy-backup-dir": _copy_backup_dir,
         "prune-staged-backups": _prune_staged_backups}


def _message(command: str, data: dict[str, Any]) -> str:
    if command == "backup-chain":
        return (f"{len(data['include'])} prefix(es) to copy" if data["narrowed"]
                else "the whole directory")
    if command == "copy-backup-dir":
        return (f"{data['copied']} copied, {data['skipped']} already there, "
                f"{data.get('removed_absent_at_source', 0)} removed (gone at the source)"
                # Said in the one line a person reads: the step down is the slow way (G4).
                + (" - file by file over SFTP, the tar stream could not be used"
                   if data.get("copy_fell_back") else ""))
    return f"{data.get('pruned', 0)} staged file(s) removed"


def run(command: str, argv: list[str], *, read_request: Any) -> int:
    usage = USAGE[command]
    if argv and argv[0] in {"-h", "--help"}:
        print(usage)
        return 0
    if not argv or argv[0] != "-":
        print(usage, file=sys.stderr)
        return response.emit(response.fail(
            command, "the request must arrive on stdin (-): it carries SSH passwords, which inline "
                     "are visible on the command line"))
    request, code = read_request("-", usage)
    if request is None:
        return code

    import contextlib

    from db_ops.common.backup_copy import CopySpaceError
    from db_ops.lib.ssh_errors import SshError

    try:
        # stdout is the answer and nothing else: opening a session prints "Connecting to ..." there,
        # and the first lab run's answer arrived behind it, unreadable ("exited 0 without a JSON
        # response", 2026-09-25). Everything said while working goes to stderr, as progress.
        with contextlib.redirect_stdout(sys.stderr):
            data = _WORK[command](request)
    except (ValueError, SshError, OSError, CopySpaceError) as exc:
        # A refusal is said in its own words: the reader acts on the sentence, not on a class name.
        return response.emit(response.fail(command, str(exc)))
    except Exception as exc:  # noqa: BLE001 - the caller parses an answer; a traceback is none
        return response.emit(response.fail(command, f"{type(exc).__name__}: {exc}"))
    if data.get("error"):
        return response.emit(response.fail(command, str(data["error"])))
    return response.emit(response.ok(command, message=_message(command, data), data=data))
