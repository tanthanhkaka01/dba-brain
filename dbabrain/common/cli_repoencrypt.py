"""``encrypt-file`` / ``verify-encrypted-file`` - one backup file encrypted with age where it lies on
the repository, then proven by decrypting it on the way back (0.37.0).

Plumbing only: :mod:`dbabrain.common.repoencrypt` does the work and says why.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from dbabrain.lib import response

ENCRYPT_USAGE = """\
Usage: python -m dbabrain.common.cli encrypt-file '<json>'|@file|-

Encrypt ONE file with age where it lies - on the repository (a Linux host with the `age` tool) -
for the key's public half only, so the repository can never decrypt what it wrote.

  {"host": {"runtime": "linux", "host": "192.0.2.30", "username": "u", "key_file": "/keys/id"},
   "path": "/srv/backup-repo/SRV-01/APPDB/FULL/APPDB_FULL_20261110.bak",
   "file_name": "APPDB_FULL_20261110.bak",    // the last part of path
   "size_bytes": 123456, "sha256": "<64 hex>", // the plain file, as the catalog proved it
   "must_be_under": "/srv/backup-repo",
   "recipients": ["age1..."],                  // the key's public half
   "key_id": 3, "key_expires_at": "2027-02-08T06:00:00Z",
   "timeout_seconds": 3600}

The plain file is held to its name, bytes and sha256 first; an expired key is refused (it only
decrypts); <path>.age is written through a partial and never replaces one already there; the plain
file is left as it is - it goes only after verify-encrypted-file proves the result.

data: {"path", "encrypted_path", "size_bytes", "sha256", "encrypted_size_bytes", "encrypted_sha256",
       "key_id", "age_version"}
"""

VERIFY_USAGE = """\
Usage: <request> | python -m dbabrain.common.cli verify-encrypted-file -

Prove ONE .age file: streamed from its host over SFTP, its bytes and sha256 measured as they pass,
and decrypted on the way with the key's identity (never written anywhere) - the plaintext must be
the plain file's bytes and sha256. The request carries a private key: stdin only.

  {"host": {"host": "192.0.2.30", "port": 22, "username": "u", "key_file": "/keys/id"},
   "path": "/srv/backup-repo/SRV-01/APPDB/FULL/APPDB_FULL_20261110.bak.age",
   "size_bytes": 123999, "sha256": "<64 hex>",              // the .age, as encrypt-file measured it
   "plain_size_bytes": 123456, "plain_sha256": "<64 hex>",  // the plain file
   "transfer": "cat",                                       // required: cat | sftp
   "identity": "AGE-SECRET-KEY-1..."}

A wrong key, a damaged file, or bytes that differ from either set of facts are refused.

data: {"path", "size_bytes", "sha256", "plain_size_bytes", "plain_sha256", "proven"}
"""


FETCH_USAGE = """\
Usage: <request> | python -m dbabrain.common.cli fetch-decrypted-file -

ONE encrypted backup file back from the repository to a restore target, decrypted on the way. The
.age is streamed from the repository, measured, decrypted with the key's identity (never written
anywhere); the plaintext is written to target_path through a partial and takes its name only when
the .age and the plaintext are each the bytes and sha256 stated. The request carries a private key:
stdin only.

  {"source": {"host": "192.0.2.30", "port": 22, "username": "u", "key_file": "/keys/id"},
   "path": "/srv/backup-repo/SRV-01/APPDB/FULL/APPDB_FULL_20261110.bak.age",
   "size_bytes": 123999, "sha256": "<64 hex>",              // the .age, as encrypt-file measured it
   "target": {"host": "192.0.2.40", "port": 22, "username": "u", "password": "..."},
   "target_path": "/restore/APPDB_FULL_20261110.bak",
   "file_name": "APPDB_FULL_20261110.bak",                 // its last part; path is it + .age
   "plain_size_bytes": 123456, "plain_sha256": "<64 hex>",  // the plain file the backup wrote
   "identity": "AGE-SECRET-KEY-1...",
   "transfer": "cat",                                       // required: cat | sftp
   "overwrite": false}                                      // required

data: {"path", "target_path", "file_name", "size_bytes", "sha256", "plain_size_bytes", "plain_sha256",
       "transfer", "restored"}
"""


ON_HOST_USAGE = """\
Usage: <request> | python -m dbabrain.common.cli decrypt-on-host -

ONE .age file decrypted where it lies, into target_path on the SAME host - when a restore's target
is the repository host itself, so the bytes never leave it. The key's private half reaches age on
stdin and a process substitution: never a command line, never a file. The .age is held to its
bytes and sha256 before anything is written; the plaintext to the plain file's before it takes its
name. The request carries a private key: stdin only.

  {"host": {"runtime": "linux", "host": "192.0.2.30", "username": "u", "key_file": "/keys/id"},
   "path": "/srv/backup-repo/SRV-01/cluster/base/X_FULL/base.tar.zst.age",
   "size_bytes": 123999, "sha256": "<64 hex>",
   "target_path": "/srv/restore-drill/stage/base/X_FULL/base.tar.zst",
   "file_name": "base.tar.zst", "must_be_under": "/srv/restore-drill",
   "plain_size_bytes": 123456, "plain_sha256": "<64 hex>",
   "identity": "AGE-SECRET-KEY-1...", "overwrite": false}

data: {"path", "target_path", "file_name", "size_bytes", "sha256", "plain_size_bytes", "plain_sha256",
       "restored"}
"""


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    usage = {"encrypt-file": ENCRYPT_USAGE, "verify-encrypted-file": VERIFY_USAGE,
             "decrypt-on-host": ON_HOST_USAGE}.get(operation, FETCH_USAGE)
    if not argv or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))
    request, code = read_request(argv[0], usage)
    if request is None:
        return code

    from dbabrain.common import repoencrypt

    started = time.monotonic()
    try:
        if operation == "encrypt-file":
            data = repoencrypt.encrypt_file(request)
            message = (f"{data['path']}: encrypted to {data['encrypted_path']} "
                       f"({data['encrypted_size_bytes']} bytes) with key {data['key_id']}")
        elif operation == "verify-encrypted-file":
            data = repoencrypt.verify_encrypted_file(request)
            message = f"{data['path']}: decrypts to the plain file's {data['plain_size_bytes']} bytes and sha256"
        elif operation == "decrypt-on-host":
            data = repoencrypt.decrypt_on_host(request)
            message = f"{data['path']} -> {data['target_path']}: decrypted on the host, proven"
        else:
            data = repoencrypt.fetch_decrypted_file(request)
            message = f"{data['path']} -> {data['target_path']}: decrypted, {data['plain_size_bytes']} bytes, proven"
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))
    return response.emit(response.ok(
        operation, message=message, data=data,
        metrics={"duration_ms": int((time.monotonic() - started) * 1000), "size_bytes": data["size_bytes"]}))
