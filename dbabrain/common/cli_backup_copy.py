"""``prepare-staging``, ``finish-staging`` - a restore's staging folder, as commands; and the three
they replace, refused.

Plumbing only: :mod:`dbabrain.common.backup_copy` does the work, :mod:`dbabrain.lib.response` shapes
the answer. Until 0.23.0 the copy between hosts and the cleanup of the target's staging folder ran
inside the backup_restore app, over sessions the app opened itself: a restore was one long call
with two steps nobody could run, test or watch on their own (the operator, 2026-09-25: "not one
long command - split it: common cli copy, common cli verify, common cli metadata ..."). The app
now resolves the logins and decides; these do.

**0.38.0 (1.329): no command here copies a byte or deletes a file.** There were three -
``backup-chain`` (which parts of a backup directory a restore needs), ``copy-backup-dir`` (choose
the files, stream them as one tar, mirror the source by removing what it no longer had) and
``prune-staged-backups`` (``find -delete`` by age, ``rm -rf`` of an emptied set). A backup file is
now copied by ``copy-files`` only, with its bytes and sha256 stated, and deleted by
``delete-verified-file`` only. What a staging needs besides its bytes is these two:

* ``prepare-staging`` - which files the restore needs (it reads the chain itself when the request
  gives it the engine), the folder made, marked and proven writable, every folder recreated, which
  files still have to travel, room for them, and what the staging holds that the source no longer
  has. ``plan_only`` answers the selection and opens no target.
* ``finish-staging`` - each copied file given its source's time, the folder opened for the engine.

The three old names read their request and refuse it, naming what to use.

**stdin only**, like ``run-sqlcmd``: every request carries SSH passwords.
"""

from __future__ import annotations

import sys
from typing import Any

from dbabrain.lib import response

COMMANDS = ("prepare-staging", "finish-staging", "backup-chain", "copy-backup-dir", "prune-staged-backups")

_LOGIN = """{"host": "192.0.2.49", "port": 22, "username": "labuser",
            "password": "..."}          // or "key_file": "/abs/path/key" - never a ref"""

USAGE = {
    "prepare-staging": f"""\
Usage: <request> | python -m dbabrain.common.cli prepare-staging -

Make a restore's staging folder ready for a backup directory's files, and answer which of them
still have to travel. It copies nothing and removes nothing; reads no config.

  {{"source": {_LOGIN},
   "source_dir": "/opt/db_ops/backup/PG_LAB_A",
   "target": {_LOGIN},
   "target_dir": "/opt/db_ops/backup/pg_restore_from_a",
   "chain": {{"db_type": "postgresql",          // postgresql | oracle | sqlserver: read the chain here
             "backup_dir": "/opt/oracle/backup",  // oracle: the path RMAN's catalog names
             "container": "ORA_LAB_A",            // oracle: where RMAN runs
             "point_in_time": ""}},               // set: the whole directory
   "include": ["base/20260925T004710Z_FULL", "wal/"],   // instead of chain: the prefixes; [] = everything
   "window_hours": 0,            // > 0: also every file written in the last N hours (copy_selection
                                 // window) - the chain and every other restore point of the window
   "make_readable": true,       // sudo chmod -R a+rX on the source first (a live archivelog job)
   "staging_marker": ".dbabrain-staging",   // the file that marks target_dir as this tool's; the default
   "space_check": {{"enabled": true, "factor": 2.0, "on_unknown": "refuse"}},   // these are the defaults
   "replace": [],                // relative paths that travel even at the same size (a copy whose checksum differs)
   "plan_only": false}}           // true: answer the files selected, read from the source; no target is opened

The chain (when `chain` is given and `include` is not): PostgreSQL the newest _FULL, the _INCRs after
it and wal/; Oracle the pieces RMAN names; SQL Server each database's newest FULL, its newest DIFF and
the LOGs after them (a database with no FULL, or a layout with no FULL/DIFF/LOG folders, whole). A
certificate or key (_cert/, .cer, .pvk) is never selected: the restore imports it from dbabrain's vault.

A file already on the target with the same size and not older is skipped, unless replace names it. A
target folder that holds files, none of them at the source, and no marker is refused: state
staging_marker as the name the folder already carries. The files still to travel must fit the target's
free space, times the factor, or the request fails before anything moves.

data: {{"include", "narrowed", "files", "pending", "skipped", "absent_at_source",
       "folders_absent_at_source", "space_check"}} - files is every file selected, as {{"path", "size_bytes"}}
(path relative to source_dir); pending is the ones still to travel, as {{"path", "size_bytes", "mtime"}} -
hand them to copy-files, then to finish-staging; absent_at_source / folders_absent_at_source are what the
staging holds that the source no longer has (folders deepest first) - named, never removed: delete each
with delete-verified-files / delete-empty-folders. Progress goes to stderr.
""",
    "finish-staging": f"""\
Usage: <request> | python -m dbabrain.common.cli finish-staging -

After the files have travelled (copy-files): give each its source's time, and open the staging
folder for the engine that restores from it. Reads no config.

  {{"target": {_LOGIN},
   "target_dir": "/opt/db_ops/backup/pg_restore_from_a",
   "files": [{{"path": "base/20260925T004710Z_FULL/backup_manifest", "mtime": 1790000000}}],   // prepare-staging's pending
   "open_for_engine": true}}      // chmod -R a+rX on the staging (the engine's own uid reads it)

data: {{"timed", "not_timed": [{{"path", "reason"}}], "opened_for_engine"}}. A file that is not there
to be timed is in not_timed, not an error: whether the copy stands is the checksum gate's to say.
""",
    "backup-chain": """\
Usage: <request> | python -m dbabrain.common.cli backup-chain -

REFUSED since 0.38.0. Which parts of a backup directory a restore needs is answered by
prepare-staging: give it "chain": {"db_type", "backup_dir", "container", "point_in_time"} with
"plan_only": true, and it answers "include" beside the files selected.

The request it took is still read - {"db_type", "source", "source_dir", "backup_dir", "container",
"point_in_time"} - and then refused.
""",
    "copy-backup-dir": """\
Usage: <request> | python -m dbabrain.common.cli copy-backup-dir -

REFUSED since 0.38.0. It chose a restore's files, streamed them as one tar, and mirrored the source -
removing from the staging whatever the source no longer had, held to nothing.

  the staging made ready, and which files   prepare-staging
  the bytes, each file with its facts       copy-files   ("overwrite": true for one prepare-staging replaced)
  the files' times, the engine's access     finish-staging
  what the source no longer has             delete-verified-files / delete-empty-folders

The request it took is still read - {"source", "source_dir", "target", "target_dir", "include",
"window_hours", "make_readable", "open_for_engine", "copy_mode", "staging_marker", "space_check",
"replace", "plan_only"} - and then refused.
""",
    "prune-staged-backups": """\
Usage: <request> | python -m dbabrain.common.cli prune-staged-backups -

REFUSED since 0.38.0. It removed a staging folder's files by age with one `find -delete`, and an
emptied backup set with `rm -rf` - many files, and a folder, in one command, none held to anything.

A restore's staging is now cleaned by backup_restore itself (the DELETE step of a restore): the
folder is listed (list-files), each file past its retention that a proven copy recorded is deleted
by delete-verified-files with the bytes and sha256 that copy measured, an emptied set's folders by
delete-empty-folders, and a file no copy recorded is kept and named. One stray file:
delete-file with "force": true.

The request it took is still read, so a malformed one is said to be malformed - and then refused:
  {"target": {...}, "target_dir": "/opt/db_ops/backup/pg_restore_from_a",
   "cleanup_retention": 259200, "staging_marker": ".dbabrain-staging"}
""",
}

#: What a refused command says (0.38.0, 1.329): its request is read, and no host is reached.
REFUSED = {
    "backup-chain": (
        "backup-chain is refused (0.38.0): which parts of a backup directory a restore needs is "
        "prepare-staging's answer - give it \"chain\": {\"db_type\", \"backup_dir\", \"container\", "
        "\"point_in_time\"} and \"plan_only\": true, and read \"include\"."),
    "copy-backup-dir": (
        "copy-backup-dir is refused (0.38.0): it streamed a restore's files as one tar and removed from the "
        "staging what the source no longer had, with nothing held to anything. Use prepare-staging (the "
        "staging made ready, which files still travel), copy-files (the bytes, each file with its size and "
        "sha256), finish-staging (the files' times, the engine's access), and delete-verified-files / "
        "delete-empty-folders for what the source no longer has. Nothing was copied or removed."),
    "prune-staged-backups": (
        "prune-staged-backups is refused (0.38.0): it deleted many files and whole folders in one "
        "command, none held to anything. A restore's staging is cleaned by backup_restore's own DELETE "
        "step - each file a proven copy recorded by delete-verified-files, an emptied set's folders by "
        "delete-empty-folders, a file no copy recorded kept and named; one stray file: delete-file "
        "with \"force\": true. Nothing was deleted."),
}


def _log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _open(login: Any, *, role: str):
    """A connected :mod:`remote_exec` SSH session - the one executor (0.25.0); this opened a
    paramiko client of its own until then."""
    from dbabrain.common import remote_exec

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


def _chain(request: dict[str, Any], source, source_dir: str) -> tuple[tuple[str, ...], bool] | None:
    """``(include, narrowed)`` read from the source when the request asks for the chain; ``None``
    when it states ``include`` itself (or neither: everything)."""
    from dbabrain.common import backup_copy

    chain = request.get("chain")
    if chain is None or request.get("include") is not None:
        return None
    if not isinstance(chain, dict) or not str(chain.get("db_type") or "").strip():
        raise ValueError('chain must be an object with "db_type" (postgresql | oracle | sqlserver).')
    include = backup_copy.chain_include(
        str(chain["db_type"]), source, source_dir=source_dir,
        backup_dir=str(chain.get("backup_dir") or ""), container=str(chain.get("container") or ""),
        point_in_time=str(chain.get("point_in_time") or ""), log=_log)
    return tuple(include), bool(include)


def _prepare_staging(request: dict[str, Any]) -> dict[str, Any]:
    import shlex

    from dbabrain.common import backup_copy
    from dbabrain.lib import restore_space

    _required(request, "source_dir")
    source_dir = str(request["source_dir"])
    plan_only = bool(request.get("plan_only"))
    if not plan_only:
        _required(request, "target_dir")
    marker = backup_copy.marker_name(request.get("staging_marker"))
    # Absent means on, as on a restore entry: a check that has to be asked for protects only the
    # copies somebody remembered.
    space_check = restore_space.parse_space_check({"space_check": request.get("space_check")})
    if space_check.measure_restore:
        raise ValueError("space_check.measure_restore belongs to a restore entry: this command "
                         "prepares for files and measures only them.")
    replace = request.get("replace") or []
    if not isinstance(replace, list) or not all(isinstance(item, str) for item in replace):
        raise ValueError("replace must be a list of relative paths.")
    stated = request.get("include")
    if stated is not None and (not isinstance(stated, list) or not all(isinstance(item, str) for item in stated)):
        raise ValueError("include must be a list of path prefixes.")
    source = _open(request.get("source"), role="source")
    try:
        if request.get("make_readable", True):
            # Readable at the moment of reading: a copy of a large set takes minutes, and the
            # archivelog job writes new 0640 pieces every 15 minutes - so a set fully readable when
            # the copy started can grow an unreadable file while it runs.
            source.run(f"sudo chmod -R a+rX {shlex.quote(source_dir)} 2>/dev/null || true")
        read = _chain(request, source, source_dir)
        include = read[0] if read is not None else tuple(str(item) for item in stated or ())
        said = {"include": list(include), "narrowed": bool(include)}
        window_since = _window_since(request.get("window_hours"))
        if plan_only:
            # The files this request selects, and nothing moved: the caller's checksum gate hashes
            # them on both hosts before the first copy (0.34.0, 1.213b). The target is not opened.
            return {**backup_copy.plan_backup_dir(source_session=source, source_dir=source_dir, include=include,
                                                  window_since=window_since).as_dict(), **said}
        target = _open(request.get("target"), role="target")
        try:
            result = backup_copy.prepare_staging(
                source_session=source, source_dir=source_dir,
                target_session=target, target_dir=str(request["target_dir"]),
                include=include, log=_log, space_check=space_check,
                window_since=window_since, marker=marker, replace=tuple(replace))
        finally:
            target.close()
    finally:
        source.close()
    return {**result.as_dict(), **said}


def _finish_staging(request: dict[str, Any]) -> dict[str, Any]:
    from dbabrain.common import backup_copy

    _required(request, "target_dir")
    files = request.get("files") or []
    if not isinstance(files, list) or not all(isinstance(item, dict) and str(item.get("path") or "").strip()
                                              for item in files):
        raise ValueError('files must be a list of {"path", "mtime"} - prepare-staging\'s pending.')
    times: list[tuple[str, int | None]] = []
    for item in files:
        mtime = item.get("mtime")
        if mtime is not None and (isinstance(mtime, bool) or not isinstance(mtime, (int, float))):
            raise ValueError(f"files: mtime of {item['path']!r} must be a number of seconds, or null.")
        relative = str(item["path"])
        if relative.startswith("/") or ".." in relative.replace("\\", "/").split("/"):
            raise ValueError(f"files: {relative!r} must be a path relative to target_dir, without '..'.")
        times.append((relative, None if mtime is None else int(mtime)))
    target = _open(request.get("target"), role="target")
    try:
        answer = backup_copy.finish_staging(target, str(request["target_dir"]), times, log=_log)
        answer["opened_for_engine"] = (
            backup_copy.open_for_the_engine(target, str(request["target_dir"]), log=_log)
            if request.get("open_for_engine", True) else False)
    finally:
        target.close()
    return answer


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


_WORK = {"prepare-staging": _prepare_staging, "finish-staging": _finish_staging}


def _message(command: str, data: dict[str, Any]) -> str:
    if command == "finish-staging":
        return (f"{data.get('timed', 0)} file(s) given their source's time"
                + (f", {len(data['not_timed'])} not" if data.get("not_timed") else "")
                + (", the folder opened for the engine" if data.get("opened_for_engine") else ""))
    return (f"{len(data.get('files') or [])} file(s) selected, {len(data.get('pending') or [])} still to travel, "
            f"{data.get('skipped', 0)} already there, {len(data.get('absent_at_source') or [])} staged file(s) "
            "the source no longer has (named, not removed)")


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
    if command in REFUSED:
        # Read like any request - and then refused, whatever it carries: no host is reached.
        return response.emit(response.fail(command, REFUSED[command]))

    import contextlib

    from dbabrain.common.backup_copy import CopySpaceError
    from dbabrain.lib.ssh_errors import SshError

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
