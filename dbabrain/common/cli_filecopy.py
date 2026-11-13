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
the copy-backup-dir of before 0.38.0 it never mirrored: a file the source has deleted stays on the target. Each file is
written as <name>.dbabrain_partial and renamed over its name only once every byte has arrived.

  {"source": {"host": "192.0.2.20", "port": 22, "username": "u", "password": "...", "key_file": "/keys/id"},
   "target": {"host": "192.0.2.40", "port": 22, "username": "u", "key_file": "/keys/id"},
   "files": [{"source": "/opt/backup/APPDB/FULL/APPDB_FULL_20261029.bak",
              "target": "/repository/SRV-01/APPDB/FULL/APPDB_FULL_20261029.bak",
              "file_name": "APPDB_FULL_20261029.bak",   // the last part of both
              "size_bytes": 52811390976,                // what the file must stream as,
              "sha256": "<64 hex>"}],                   // or nothing lands at its name
   "overwrite": false,               // required: false leaves an existing target as it is
   "transfer": "cat",                // cat | sftp (sftp for a Windows source over SSH)
   "encrypt": {"recipients": ["age1..."], "key_id": 2,          // optional (0.38.0): each file lands
               "key_expires_at": "2027-02-09T04:00:00Z"},       // as <target>.age, encrypted on the way
   "timeout_seconds": 21600,         // the whole command
   "stall_seconds": 300}             // one read or write that stops moving this long fails its file

Or a Windows source by its share instead of "source" (read with smbclient, or the UNC path on a
Windows node) - the file's "source" is then its path under the share:

  "source_share": {"host": "192.0.2.30", "share": "SQLBK", "username": "DOMAIN\\u", "password": "..."}

data: {"source_kind": "sftp|smbclient|unc", "files": [{"source", "target", "ok", "bytes_copied",
       "sha256_streamed", "error", "warning"?, and encrypted: "encrypted_path", "encrypted_size_bytes",
       "encrypted_sha256", "key_id", "age_version"}], "copied", "failed", "bytes_copied", "encrypted"}
A file that could not be copied is its own line (ok false, error), not a failure of the command;
a source or target that cannot be reached is. The bytes are hashed as they stream and held to the
line's facts; the copy's proof is still the caller's - file-checksum on the target, or for an
encrypted copy verify-encrypted-file, which decrypts it back.
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
