"""``list-files`` - every entry under a folder, its kind, bytes and time, no hash (0.37.0).

Plumbing only: :mod:`dbabrain.common.listfiles` does the work and says why.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from dbabrain.lib import response

LIST_FILES_USAGE = """\
Usage: python -m dbabrain.common.cli list-files '<json>'|@file|-

List every entry under a folder that is not a folder itself - its kind (file | link | other), bytes
and modification time - WITHOUT hashing: a repository folder of terabytes is listed in seconds. The
repository check reads it on every offload pass.

  {"host": {"runtime": "linux", "access": "ssh", "host": "192.0.2.30", "username": "u",
            "key_file": "/keys/id"},       // windows | linux | docker | k8s; omit host to list here
   "folder": "/mnt/nas/SRV-01",
   "max_files": 200000,                    // optional; more is answered as truncated
   "with_folders": false,                  // optional; true also answers every folder under it
   "timeout_seconds": 600}                 // optional

A folder that is not there is folder_found false, not an error. A link is listed as a link, never
followed.

data: {"folder", "folder_found", "files": [{"path", "type", "size_bytes", "mtime_utc"}], "listed",
       "truncated", "folders": [path, ...]}     // folders only when with_folders was asked
"""


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(LIST_FILES_USAGE, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))
    request, code = read_request(argv[0], LIST_FILES_USAGE)
    if request is None:
        return code

    from dbabrain.common import listfiles

    started = time.monotonic()
    try:
        data = listfiles.list_files(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))
    message = (f"{data['folder']}: not there" if not data["folder_found"] else
               f"{data['folder']}: {data['listed']} entr{'y' if data['listed'] == 1 else 'ies'}"
               + (" (truncated)" if data["truncated"] else ""))
    return response.emit(response.ok(operation, message=message, data=data,
                                     metrics={"duration_ms": int((time.monotonic() - started) * 1000),
                                              "listed": data["listed"]}))
