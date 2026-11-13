"""``delete-file`` - ONE stray file, forced, measured on the host; ``delete-files`` - refused (0.38.0).

Plumbing only: :mod:`dbabrain.common.deleteverified` does the work, :mod:`dbabrain.lib.response`
shapes the answer.

Until 0.38.0 these two deleted backup files by full path: a path, or a list of paths, one ``rm``
each, with nothing on the host held to anything. A backup file is now deleted only by
``delete-verified-file`` - its name, path, bytes and sha256 stated, and all true on the host
(0.37.0) - and every cleanup that called these is on it (1.329).

What a verified delete cannot remove is a file nobody recorded: a partial of a killed copy, a file
the catalog never knew. The operator, 2026-11-11 (translated): *the old delete commands refuse to
run; but with force a file can still be deleted, to clear junk too - one file at a time: two files
or more fail outright, a folder that still holds a file fails, and to delete many, make an array
and call the delete once per file.* So:

* ``delete-file`` **without** ``"force": true`` is refused, and names ``delete-verified-file``;
* ``delete-file`` **with** it deletes exactly one regular file, in the one script that measures it,
  and answers with the bytes and sha256 of what it removed;
* ``delete-files`` is refused whatever it carries - one command never deletes two files. A caller
  with a list calls ``delete-file`` once for each.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from dbabrain.lib import response

_HOST_BLOCK = """\
   "host": {"runtime": "linux",      // windows | linux | docker | k8s
            "access": "ssh",         // ssh | winrm
            "host": "203.0.113.188", "port": 22,
            "username": "ubuntu", "password": "...", "key_file": "/keys/id",
            "container": "...",      // runtime=docker
            "sudo": true},           // omit `host` to act on THIS machine (runtime still stated)"""

DELETE_FILE_USAGE = f"""\
Usage: python -m dbabrain.common.cli delete-file '<json>'|@file|-

Delete ONE stray file that nobody recorded - forced. Reads no config.

A backup file dbabrain wrote or copied is deleted by delete-verified-file, which holds it to its
name, path, bytes and sha256. This is for what has no stored facts: a partial of a killed copy, a
file the catalog never knew. Without "force": true the request is refused.

  {{"path": "/mnt/nas/SRV-01/APPDB/FULL/APPDB_FULL_20261101.bak.age.4411.partial",
   "must_be_under": "/mnt/nas/SRV-01",           // required; the fence itself is never deleted
   "force": true,                                // required: this file has no stored facts
{_HOST_BLOCK}
   "dry_run": false,                             // optional; measure, delete nothing
   "timeout_seconds": 3600}}                     // optional; the hash runs at disk speed

One file per command, always: `paths`, `files` or any unknown field is refused, and a pattern
(`*`, `?`, `[`) never expands. To delete several, call it once for each.

On the host, in one script: not a link, not a folder (empty or not), not a device; the bytes hashed,
the size and modification time read again around the hash (nothing wrote to it meanwhile); then one
non-recursive delete of that one file, and a look to prove it is gone.

data: {{"file": {{"path", "file_name", "must_be_under", "size_bytes_found", "sha256_found",
                "status", "reason"}}, "deleted", "dry_run", "forced": true}}
status: deleted | not_found | skipped (dry run) - success; refused | failed - a failed response.
The bytes and sha256 in the answer are the record of what was removed.

ORACLE: this removes the file, not RMAN's record of it - use DELETE BACKUPPIECE / DELETE OBSOLETE
through RMAN for a piece RMAN owns.
"""

DELETE_FILES_USAGE = """\
Usage: python -m dbabrain.common.cli delete-files '<json>'|@file|-

REFUSED since 0.38.0 - one command never deletes two files.

  a file dbabrain recorded    delete-verified-file   (name, path, bytes, sha256 - all held)
  a list of those             delete-verified-files  (every item its own facts, its own delete)
  one stray file              delete-file with "force": true - called once for each file
  an emptied folder           delete-empty-folder    (a folder that holds anything is refused)
"""

REFUSED_FILES = (
    "delete-files is refused (0.38.0): one command never deletes two files. A file dbabrain recorded "
    "is deleted by delete-verified-file (a list of them: delete-verified-files, every item with its own "
    "name, path, bytes and sha256); one stray file by delete-file with \"force\": true, called once for "
    "each file; an emptied folder by delete-empty-folder. Nothing was deleted.")


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    usage = DELETE_FILE_USAGE if operation == "delete-file" else DELETE_FILES_USAGE
    if not argv or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(
            operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))

    request, code = read_request(argv[0], usage)
    if request is None:
        return code
    if operation == "delete-files":
        # Read like any request (a malformed one is still said to be malformed) - and then refused,
        # whatever it carries: nothing in it can make one command delete two files.
        return response.emit(response.fail(operation, REFUSED_FILES))

    from dbabrain.common import deleteverified

    started = time.monotonic()
    try:
        data = deleteverified.delete_forced_file(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))

    row = data["file"]
    metrics = {"duration_ms": int((time.monotonic() - started) * 1000),
               "freed_bytes": int(row["size_bytes_found"] or 0) if row["status"] == deleteverified.DELETED else 0}
    described = (f"{row['size_bytes_found']} bytes, sha256 {row['sha256_found']}"
                 if row["size_bytes_found"] is not None and row["sha256_found"] else "not measured")
    if row["status"] not in deleteverified.SUCCESS_STATUSES:
        return response.emit(response.fail(
            operation, f"{row['path']}: {row['status']} - {row['reason']}",
            message=f"{row['path']}: nothing deleted.", data=data, metrics=metrics))
    message = {
        deleteverified.DELETED: f"{row['path']} deleted (forced): {described}.",
        deleteverified.NOT_FOUND: f"{row['path']}: no file there - nothing deleted.",
        deleteverified.SKIPPED: f"Dry run: {row['path']} would be deleted ({described}).",
    }[row["status"]]
    return response.emit(response.ok(operation, message=message, data=data, metrics=metrics))
