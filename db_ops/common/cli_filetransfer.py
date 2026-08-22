"""``pack-backup``, ``pull-file``, ``push-file`` — one archive, moved and proven.

Plumbing only: :mod:`db_ops.common.filetransfer` does the work, :mod:`db_ops.lib.response`
shapes the answer.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from db_ops.lib import response

_HOST_BLOCK = """\
   "host": {"runtime": "docker",     // windows | linux | docker | k8s
            "host": "203.0.113.188", "port": 22,
            "username": "ubuntu", "password": "...", "key_file": "/keys/id",
            "container": "ora_dg_lab-primary",   // runtime=docker
            "pod": "...", "namespace": "...",    // runtime=k8s
            "sudo": true}"""

PACK_USAGE = f"""\
Usage: python -m db_ops.common.cli pack-backup '<json>'|@file|-

Pack a folder, or a named list of files, into ONE archive on the host that already holds them -
then move that archive. Per-file SFTP across two internet hops measured 10 KB/s here, and a
backup set is thousands of small files.

  {{"folder": "/opt/oracle/backup/dbops",   // exactly one of folder or files
   "files": ["/b/a.bkp", "/b/b.bkp"],
   "archive_path": "/tmp/pieces.tar",
   "format": "tar",                  // tar | zip. Windows gets tar.exe / Compress-Archive
{_HOST_BLOCK}}}

data: {{"archive_path", "format", "sha256", "size", "packed", "folder"}}
The sha256 is the point: size alone catches a truncated copy, not a corrupted one.
"""

PULL_USAGE = f"""\
Usage: python -m db_ops.common.cli pull-file '<json>'|@file|-

Copy one file from a host to THIS machine (the db_ops worker), and prove it arrived intact - the
archive is hashed on the host and again here, and a mismatch is refused rather than retried.

  {{"remote_path": "/tmp/pieces.tar",
   "local_path": "/opt/db_ops/stage/pieces.tar",
{_HOST_BLOCK}}}

data: {{"remote_path", "local_path", "sha256", "size", "verified"}}
"""

PUSH_USAGE = f"""\
Usage: python -m db_ops.common.cli push-file '<json>'|@file|-

Copy one file from THIS machine to a host - a Windows VM or an Ubuntu one - and prove it arrived
intact. The destination directory is created if missing: SFTP will not make it, and the error it
gives names the file rather than the directory that is not there.

  {{"local_path": "/opt/db_ops/stage/pieces.tar",
   "remote_path": "D:\\\\restore\\\\pieces.tar",
{_HOST_BLOCK}}}

data: {{"local_path", "remote_path", "sha256", "size", "verified"}}
"""

_COMMANDS = {
    "pack-backup": ("pack", PACK_USAGE, "Packed {sha256} ({size} bytes) to {archive_path}."),
    "pull-file": ("pull", PULL_USAGE, "Pulled {size} bytes to {local_path}, sha256 {sha256}."),
    "push-file": ("push", PUSH_USAGE, "Pushed {size} bytes to {remote_path}, sha256 {sha256}."),
}


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    action, usage, template = _COMMANDS[operation]

    if not argv or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(
            operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))

    request, code = read_request(argv[0], usage)
    if request is None:
        return code

    from db_ops.common import filetransfer

    started = time.monotonic()
    try:
        data = getattr(filetransfer, action)(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))

    return response.emit(response.ok(
        operation,
        message=template.format(**{k: data.get(k) for k in ("sha256", "size", "archive_path",
                                                            "local_path", "remote_path")}),
        data=data,
        metrics={"duration_ms": int((time.monotonic() - started) * 1000),
                 "bytes": data.get("size") or 0},
    ))


