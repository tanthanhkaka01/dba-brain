"""``read-file`` - ONE small file's bytes from the host that holds it, proven on arrival (0.37.0).

Plumbing only: :mod:`dbabrain.common.readfile` does the work and says why.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from dbabrain.lib import response

READ_FILE_USAGE = """\
Usage: python -m dbabrain.common.cli read-file '<json>'|@file|-

Read ONE small file on the host that holds it - a backup certificate pair for the vault. The host
states the file's size and sha256 beside its bytes; the bytes are held to both on arrival.

  {"host": {"runtime": "windows", "access": "winrm", "host": "192.0.2.20", "username": "u",
            "password": "..."},       // or linux / docker (read inside the container) / k8s
   "path": "D:\\\\SQLBK\\\\_cert\\\\dbabrain_backup_cert.cer",
   "max_bytes": 1048576,              // optional, at most 16 MiB
   "timeout_seconds": 120}            // optional

A link, a missing file, a file over max_bytes, or bytes that arrive other than the host stated are
refused.

data: {"path", "file_name", "size_bytes", "sha256", "base64"}
"""


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(READ_FILE_USAGE, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))
    request, code = read_request(argv[0], READ_FILE_USAGE)
    if request is None:
        return code

    from dbabrain.common import readfile

    started = time.monotonic()
    try:
        data = readfile.read_file(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))
    return response.emit(response.ok(
        operation, message=f"{data['path']}: {data['size_bytes']} bytes, sha256 {data['sha256']}",
        data=data, metrics={"duration_ms": int((time.monotonic() - started) * 1000),
                            "size_bytes": data["size_bytes"]}))
