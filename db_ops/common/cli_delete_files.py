"""``delete-file``, ``delete-files`` — remove backup files by full path.

Plumbing only: :mod:`db_ops.common.deletefiles` does the work, :mod:`db_ops.lib.response`
shapes the answer.

Two commands rather than one because they are two different decisions to authorise. ``delete-file``
is the unit — one path, one answer — and ``delete-files`` is that same operation run over a list,
so a caller that wants to see each step can drive the singular form itself and one that wants the
set done in a single connection asks for the plural. Neither ever expands a pattern.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from db_ops.lib import response

_HOST_BLOCK = """\
   "host": {"runtime": "linux",      // windows | linux | docker | k8s
            "host": "203.0.113.188", "port": 22,
            "username": "ubuntu", "password": "...", "key_file": "/keys/id",
            "container": "ora_dg_lab-primary",   // runtime=docker
            "pod": "...", "namespace": "...",    // runtime=k8s
            "sudo": true},               // omit `host` entirely to act on THIS machine"""

_SHARED = f"""\
{_HOST_BLOCK}
   "must_be_under": "/opt/oracle/backup",  // optional fence: refuse anything outside this root
   "dry_run": true                     // optional; report what would go, delete nothing

Paths are full paths and are never patterns - `*`, `?` and `[` are refused. List first with
list-backup-files, decide, then pass back the paths it returned. Expanding a pattern here would
delete files nobody looked at, and `*.bak` under the wrong root is one typo from the full backup
you are restoring from.

A file that is already gone is `not_found`, and that is SUCCESS: delete states an end condition,
so re-running after a partial failure is safe. A directory is refused - this deletes files, never
trees.

ORACLE: this removes the file, not RMAN's record of it. A backup piece deleted from disk leaves
RMAN still listing it and the next RESTORE picking it. Use DELETE BACKUPPIECE / DELETE OBSOLETE
through RMAN for those, and this for files RMAN does not own."""

DELETE_FILE_USAGE = f"""\
Usage: python -m db_ops.common.cli delete-file '<json>'|@file|-

Delete ONE file, named in full. Reads no config: the request says where the file is and how to
reach the machine holding it.

  {{"path": "/opt/oracle/backup/dbops/SALESDB_STG_20260801_full.bkp",
{_SHARED}

data: {{"file": {{"path", "status", "size", "reason"}},
       "counts", "bytes_freed", "failed", "dry_run"}}
status is one of deleted | not_found | skipped | failed.
"""

DELETE_FILES_USAGE = f"""\
Usage: python -m db_ops.common.cli delete-files '<json>'|@file|-

Delete every named file, one at a time, over one connection. Same operation as delete-file per
path, so the per-file answers are identical - this only saves reconnecting for each of them.

  {{"paths": ["/opt/oracle/backup/dbops/a.bkp",
             "/opt/oracle/backup/dbops/b.bkp"],
   "stop_on_error": false,             // optional; default is to try every path and report
{_SHARED}

Every path is validated before the FIRST delete, so a bad path in the list stops the request while
nothing has happened yet.

data: {{"files": [{{"path", "status", "size", "reason"}}, ...],
       "counts": {{"deleted", "not_found", "skipped", "failed"}},
       "bytes_freed", "failed": [path, ...], "dry_run", "stopped_early"}}

The request SUCCEEDS when no file failed; one failure makes it a failed response with the rest of
the answers still in `data`, so a caller can retry exactly the paths in `failed`.
"""

_COMMANDS = {
    "delete-file": ("delete_file", DELETE_FILE_USAGE),
    "delete-files": ("delete_files", DELETE_FILES_USAGE),
}


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    action, usage = _COMMANDS[operation]

    if not argv or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(
            operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))

    request, code = read_request(argv[0], usage)
    if request is None:
        return code

    from db_ops.common import deletefiles

    started = time.monotonic()
    try:
        data = getattr(deletefiles, action)(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))

    counts = data["counts"]
    metrics = {"duration_ms": int((time.monotonic() - started) * 1000),
               "bytes_freed": data["bytes_freed"], **counts}
    message = _message(counts, data)

    # A file that could not be deleted is a failed request even though the others went: the caller
    # asked for a set to be gone, and it is not. `data` is carried on the failure so the retry can
    # use `failed` directly rather than re-deriving it.
    if counts["failed"]:
        return response.emit(response.fail(
            operation, f"{counts['failed']} file(s) could not be deleted: "
                       f"{'; '.join(data['failed'][:3])}",
            message=message, data=data, metrics=metrics))
    return response.emit(response.ok(operation, message=message, data=data, metrics=metrics))


def _message(counts: dict[str, int], data: dict[str, Any]) -> str:
    if data.get("dry_run"):
        return f"Dry run: {counts['skipped']} file(s) would be deleted, {counts['not_found']} already gone."
    parts = [f"{counts['deleted']} file(s) deleted ({data['bytes_freed']} bytes freed)"]
    if counts["not_found"]:
        parts.append(f"{counts['not_found']} already gone")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    return ", ".join(parts) + "."


