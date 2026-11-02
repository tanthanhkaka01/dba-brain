"""``file-checksum`` - hash files on the host that holds them.

Plumbing only: :mod:`dbabrain.common.filechecksum` does the work, :mod:`dbabrain.lib.response`
shapes the answer.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from dbabrain.lib import response

USAGE = """\
Usage: python -m dbabrain.common.cli file-checksum '<json>'|@file|-

Hash files where they are - on the host that holds them, or on this machine with no host - and
answer one sha256 per file. The one way this tool takes a checksum (R43): a backup records it, a
restore compares it before and after its copy, the scrub compares it again later.

  {"paths": ["/opt/backup/APPDB_FULL_20261029.bak", "/opt/backup/APPDB_LOG_20261029_0315.trn"],
   "algorithm": "sha256",            // the only one
   "timeout_seconds": 3600,          // per batch; a large FULL hashes at disk speed
   "host": {"runtime": "linux",      // windows | linux | docker | k8s; omit host to hash here
            "host": "192.0.2.20", "port": 22, "access": "ssh",
            "username": "u", "password": "...", "key_file": "/keys/id"}}

Or every file under a folder instead of `paths` (0.34.0, 1.215 - a catalog backfill):

  {"folder": "/var/opt/mssql/backup",  // listed on the host: find, or Get-ChildItem -Recurse
   "include": ["*.bak", "*.trn"],      // file name patterns, case-insensitive; default every file
   "container": "mssql",               // the folder is inside it: translated through its mounts
   "max_files": 5000,                  // more is answered as truncated, never cut silently
   "host": {...}}

data: {"algorithm", "host", "files": [{"path", "sha256", "size_bytes", "mtime_utc"}], "missing",
       "file_count", "bytes_total"} - with a folder also "folder", "host_folder" (the paths in
       files are under it), "folder_found", "listed", "truncated"
A file that is not there is listed under `missing`, not an error: the caller decides what a missing
backup means. Windows hashes through .NET, so PowerShell 2.0 (Server 2008 R2) works too.
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

    from dbabrain.common import filechecksum

    started = time.monotonic()
    try:
        data = filechecksum.checksum(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))

    missing = len(data["missing"])
    return response.emit(response.ok(
        operation,
        message=(f"Hashed {data['file_count']} file(s), {data['bytes_total']} bytes, on {data['host']}"
                 + (f"; {missing} missing." if missing else ".")),
        data=data,
        metrics={"duration_ms": int((time.monotonic() - started) * 1000),
                 "size_bytes": data["bytes_total"]},
    ))
