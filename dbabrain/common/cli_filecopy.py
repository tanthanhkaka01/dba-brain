"""``copy-files`` - copy named files from one host to another, removing nothing.

Plumbing only: :mod:`dbabrain.common.filecopy` does the work, :mod:`dbabrain.lib.response` shapes
the answer.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from dbabrain.lib import response

USAGE = """\
Usage: python -m dbabrain.common.cli copy-files '<json>'|@file|-

Copy each named file from the host that holds it to another host, through this machine as a
stream, and remove nothing anywhere - the repository copy of a backup (0.34.0, 1.240). Unlike
copy-backup-dir it never mirrors: a file the source has deleted stays on the target. Each file is
written as <name>.dbabrain_partial and renamed over its name only once every byte has arrived.

  {"source": {"host": "192.0.2.20", "port": 22, "username": "u", "password": "...", "key_file": "/keys/id"},
   "target": {"host": "192.0.2.40", "port": 22, "username": "u", "key_file": "/keys/id"},
   "files": [{"source": "/opt/backup/APPDB/FULL/APPDB_FULL_20261029.bak",
              "target": "/repository/SRV-01/APPDB/FULL/APPDB_FULL_20261029.bak"}],
   "timeout_seconds": 21600,         // the whole command
   "stall_seconds": 300}             // one read or write that stops moving this long fails its file

Or a Windows source by its share instead of "source" (read with smbclient, or the UNC path on a
Windows node) - the file's "source" is then its path under the share:

  "source_share": {"host": "192.0.2.30", "share": "SQLBK", "username": "DOMAIN\\u", "password": "..."}

data: {"source_kind": "sftp|smbclient|unc", "files": [{"source", "target", "ok", "bytes_copied",
       "error", "warning"?}], "copied", "failed", "bytes_copied"}
A file that could not be copied is its own line (ok false, error), not a failure of the command;
a source or target that cannot be reached is. Nothing is hashed here: the caller holds each copy to
its checksum with file-checksum on both hosts.
"""


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(
            operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))

    request, code = read_request(argv[0], USAGE)
    if request is None:
        return code

    from dbabrain.common import filecopy

    started = time.monotonic()
    try:
        data = filecopy.copy_files(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))

    return response.emit(response.ok(
        operation,
        message=(f"Copied {data['copied']} of {len(data['files'])} file(s), {data['bytes_copied']} bytes"
                 + (f"; {data['failed']} failed." if data["failed"] else ".")),
        data=data,
        metrics={"duration_ms": int((time.monotonic() - started) * 1000),
                 "size_bytes": data["bytes_copied"]},
    ))
