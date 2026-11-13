"""``delete-verified-file``, ``delete-empty-folder`` - remove ONE file whose every fact is stated, or
ONE empty folder (0.37.0).

Plumbing only: :mod:`dbabrain.common.deleteverified` does the work and says why it is shaped as it is,
:mod:`dbabrain.lib.response` shapes the answer.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from dbabrain.lib import response

_HOST_BLOCK = """\
   "host": {"runtime": "linux",      // windows | linux | docker | k8s - stated, never guessed
            "access": "ssh",         // ssh | winrm
            "host": "203.0.113.188", "port": 22,
            "username": "ubuntu", "password": "...", "key_file": "/keys/id",
            "container": "...",      // runtime=docker
            "sudo": true},           // omit `host` to act on THIS machine (runtime still stated)"""

DELETE_VERIFIED_FILE_USAGE = f"""\
Usage: python -m dbabrain.common.cli delete-verified-file '<json>'|@file|-

Delete ONE file - and only when every fact the request states is true on the host. One file per
command, always: `paths`, `files` or any unknown field is refused.

  {{"path": "/var/opt/mssql/backup/APPDB/FULL/APPDB_FULL_20261101.bak",
   "file_name": "APPDB_FULL_20261101.bak",       // the last part of `path`
   "size_bytes": 104857600,                      // what it must measure now
   "sha256": "<64 hex>",                         // what it must hash to now
   "must_be_under": "/var/opt/mssql/backup",     // required; the fence itself is never deleted
{_HOST_BLOCK}
   "dry_run": false,                             // optional; check everything, delete nothing
   "timeout_seconds": 3600}}                     // optional; the hash runs at disk speed

On the host, in one script: not a link, not a folder, not a device; the size held to size_bytes;
the bytes hashed and held to sha256; size and modification time read again (nothing wrote to it
while it was hashed); then one non-recursive delete of that one file, and a look to prove it is gone.
Any difference refuses, and the answer says what the host found.

data: {{"file": {{"path", "file_name", "must_be_under", "size_bytes", "sha256",
                "size_bytes_found", "sha256_found", "status", "reason"}},
       "deleted", "dry_run"}}
status: deleted | not_found | skipped (dry run) - success; refused | failed - a failed response.
"""

DELETE_EMPTY_FOLDER_USAGE = f"""\
Usage: python -m dbabrain.common.cli delete-empty-folder '<json>'|@file|-

Delete ONE folder that holds nothing at all - one entry in it, hidden or not, refuses.

  {{"path": "/var/opt/mssql/backup/APPDB/FULL",
   "must_be_under": "/var/opt/mssql/backup",     // required; the fence itself is never deleted
{_HOST_BLOCK}
   "dry_run": false}}

The removal is the platform's non-recursive one (rmdir / Directory.Delete(path, false)), which
refuses a folder that is not empty by itself.

data: {{"folder": {{"path", "must_be_under", "entries", "status", "reason"}}, "deleted", "dry_run"}}
status: deleted | not_found | skipped (dry run) - success; refused | failed - a failed response.
"""

DELETE_VERIFIED_FILES_USAGE = f"""\
Usage: python -m dbabrain.common.cli delete-verified-files '<json>'|@file|-

A list of files, each deleted exactly as delete-verified-file deletes one: every item states its own
name, path, bytes, sha256 and fence; each is checked and removed by its own script on the host (one
non-recursive delete of one file), over one connection. The whole list is validated before the host
is reached - one malformed or repeated item refuses all of them - and the first item that is not
deleted (refused, failed) stops the list: the items after it are `not_attempted`, never sent.

  {{"files": [{{"path": "/mnt/nas/_test/pgdata/base/1/1247",
              "file_name": "1247",                // the last part of `path`
              "size_bytes": 106496,
              "sha256": "<64 hex>",
              "must_be_under": "/mnt/nas/_test"}},   // required on every item
             ...],                                // at most 20000
{_HOST_BLOCK}
   "dry_run": false,                             // optional; check every item, delete nothing
   "timeout_seconds": 3600}}                     // optional; per item

data: {{"files": [<the delete-verified-file row>, ...], "deleted", "not_found", "skipped",
       "not_attempted", "complete", "stopped_at", "dry_run"}}
A list that stopped (`complete` false) is a failed response; `stopped_at` names the item and why.
"""

DELETE_EMPTY_FOLDERS_USAGE = f"""\
Usage: python -m dbabrain.common.cli delete-empty-folders '<json>'|@file|-

A list of folders, each deleted exactly as delete-empty-folder deletes one, in the order given -
deepest first is the caller's to state: a parent named before its child is refused as not empty and
stops the list. Validated whole before the host is reached; one connection.

  {{"folders": [{{"path": "/mnt/nas/_test/pgdata/base/1", "must_be_under": "/mnt/nas/_test"}},
               {{"path": "/mnt/nas/_test/pgdata/base", "must_be_under": "/mnt/nas/_test"}}],
{_HOST_BLOCK}
   "dry_run": false}}

data: {{"folders": [<the delete-empty-folder row>, ...], "deleted", "not_found", "skipped",
       "not_attempted", "complete", "stopped_at", "dry_run"}}
"""

_COMMANDS = {
    "delete-verified-file": ("delete_verified_file", "file", DELETE_VERIFIED_FILE_USAGE),
    "delete-empty-folder": ("delete_empty_folder", "folder", DELETE_EMPTY_FOLDER_USAGE),
    "delete-verified-files": ("delete_verified_files", "files", DELETE_VERIFIED_FILES_USAGE),
    "delete-empty-folders": ("delete_empty_folders", "folders", DELETE_EMPTY_FOLDERS_USAGE),
}


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    action, key, usage = _COMMANDS[operation]

    if not argv or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(
            operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))

    request, code = read_request(argv[0], usage)
    if request is None:
        return code

    from dbabrain.common import deleteverified

    started = time.monotonic()
    try:
        data = getattr(deleteverified, action)(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))

    if key in {"files", "folders"}:
        return _emit_list(operation, data, key, started)
    row = data[key]
    metrics = {"duration_ms": int((time.monotonic() - started) * 1000),
               "freed_bytes": (row.get("size_bytes") or 0) if row["status"] == deleteverified.DELETED else 0}
    message = f"{row['path']}: {row['status']}" + (f" - {row['reason']}" if row["reason"] else "")
    if row["status"] not in deleteverified.SUCCESS_STATUSES:
        # The caller asked for this one thing to be gone, and it is not: a failed request, with what the
        # host found carried in `data`.
        return response.emit(response.fail(operation, message, message=message, data=data, metrics=metrics))
    return response.emit(response.ok(operation, message=message, data=data, metrics=metrics))


def _emit_list(operation: str, data: dict[str, Any], key: str, started: float) -> int:
    from dbabrain.common import deleteverified

    rows = data[key]
    freed = sum(int(row.get("size_bytes") or 0) for row in rows if row["status"] == deleteverified.DELETED)
    metrics = {"duration_ms": int((time.monotonic() - started) * 1000), "items": len(rows),
               "deleted": data["deleted"], "freed_bytes": freed}
    message = (f"{len(rows)} {key}: {data['deleted']} deleted, {data['not_found']} not found, "
               f"{data['skipped']} skipped (dry run), {data['not_attempted']} not attempted")
    if not data["complete"]:
        stop = data["stopped_at"]
        message += f" - stopped at {stop['path']}: {stop['status']} - {stop['reason']}"
        return response.emit(response.fail(operation, message, message=message, data=data, metrics=metrics))
    return response.emit(response.ok(operation, message=message, data=data, metrics=metrics))
