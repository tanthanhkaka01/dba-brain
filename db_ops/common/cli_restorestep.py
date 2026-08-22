"""``restore-full`` / ``restore-diff`` / ``restore-log`` / ``restore-metadata`` / ``verify-restore``.

Plumbing only. The work is in :mod:`db_ops.common.restorestep`,
:mod:`db_ops.common.restoremetadata` and :mod:`db_ops.common.verifyrestore`; the answer shape is
:mod:`db_ops.lib.response`.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from db_ops.lib import response

_HOST = """\
   "host": {"runtime": "docker",     // windows | linux | docker | k8s
            "host": "...", "username": "...", "key_file": "...",
            "container": "...", "sudo": true}"""

_TARGET = """\
   "target": {"host": "192.0.2.249", "port": 1433,
              "username": "sa", "password": "..."}"""

STEP_USAGE = f"""\
Usage: python -m db_ops.common.cli restore-full|restore-diff|restore-log '<json>'|@file|-

Apply ONE named backup. The caller picked the file (with list-backup-files) and asks for exactly
that - so a recovery can watch each step land before deciding the next.

  {{"db_type": "sqlserver",
   "backup_path": "/b/SALESDB_STG_FULL_20260807.bak",   // one file
   "backup_paths": ["/b/l1.trn", "/b/l2.trn"],      // ...or several, in order
   "database": "SALESDB_STG",            // sqlserver
   "with_recovery": false,           // DEFAULT false. true only on the LAST step of a chain -
                                     // a database recovered early cannot take the rest.
   "stopat": "2026-08-07 01:40:00",  // restore-log only
   "move": {{"SALESDB_STG": "/var/opt/mssql/data/SALESDB_STG.mdf"}},   // restore-full only
   "dry_run": true,                  // show what would run, touch nothing
{_TARGET},                            // sqlserver
{_HOST}}}                             // oracle / postgresql

The three engines do NOT mean the same thing by "restore one file", and the response says which
happened rather than smoothing it over:
  sqlserver   one RESTORE per file. NORECOVERY between, RECOVERY on the last, STOPAT on a log.
  oracle      the piece is CATALOGed and RMAN then RESTOREs/RECOVERs - it chooses what to read.
  postgresql  full = a base backup directory becomes the data directory; diff = the WHOLE chain
              combined with pg_combinebackup; log = writes recovery_target_time, replayed by the
              server at startup. Needs "data_dir".
"""

METADATA_USAGE = f"""\
Usage: python -m db_ops.common.cli restore-metadata '<json>'|@file|-

Apply SQL Server instance metadata - logins, roles, permissions, credentials, linked servers,
Agent jobs - from named .sql exports. A backup covers user databases only, so a restored instance
has the data and none of the machinery.

  {{"files": ["/bundle/server/logins.sql", "/bundle/server/permissions.sql"],  // IN ORDER
   "dry_run": false,
{_TARGET},
{_HOST}}}                             // optional: read the files from that host instead of here

Order is the caller's: logins BEFORE the databases are restored so their users are not orphaned,
agent_jobs AFTER, because job steps name databases that must exist. Each file is reported
separately - a linked server that cannot exist on this network must not cost the logins that can.
"""

VERIFY_USAGE = f"""\
Usage: python -m db_ops.common.cli verify-restore '<json>'|@file|-

Is the restored database actually usable - not merely "did the restore command return". Each
engine has its own way of looking finished and being unusable: SQL Server left RESTORING, Oracle
MOUNTED but never opened, PostgreSQL still in recovery. All three report success at the command
that put them there.

  {{"db_type": "sqlserver",
   "databases": ["SALESDB_STG", "APPDB_Prod"],   // optional; default every user database
{_TARGET},                            // sqlserver
{_HOST}}}                             // oracle / postgresql

A state column is not enough - a real query is run, because a database can read ONLINE and still
refuse one while it finishes an upgrade step.

data: {{"ok", "checked", "failed", "databases": [{{"database", "state", "ok", "detail"}}]}}
"""

KEY_USAGE = """Usage: python -m db_ops.common.cli restore-key '<json>'|@file|-

Import the certificate an encrypted backup set can only be read with. A backup written WITH
ENCRYPTION is readable ONLY by an instance holding that certificate; the backup job exports the
pair beside the backups for this reason. Without it, restore-full fails with SQL Server naming a
hex thumbprint rather than the file sitting next to the backup.

  {"certificate_name": "db_ops_backup_cert",
   "cer_path": "/var/opt/mssql/restore_stage/_cert/db_ops_backup_cert.cer",
   "pvk_path": "/var/opt/mssql/restore_stage/_cert/db_ops_backup_cert.pvk",
   "password": "...",              // decrypts the private key: the backup's own passphrase
   "dry_run": false,
   "target": {"host": "...", "port": 1433, "username": "sa", "password": "..."}}

Paths are resolved BY THE INSTANCE, so on a container target they are container paths.
data: {"certificate_name", "thumbprint", "imported"}
"""

_LEVELS = {"restore-full": "full", "restore-diff": "diff", "restore-log": "log"}


def run(operation: str, argv: list[str], *, read_request: Any) -> int:
    usage = (STEP_USAGE if operation in _LEVELS
             else KEY_USAGE if operation == "restore-key"
             else METADATA_USAGE if operation == "restore-metadata" else VERIFY_USAGE)

    if not argv or argv[0] in {"-h", "--help"}:
        print(usage, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(
            operation, f"{operation} takes one JSON payload; got {len(argv)} arguments."))

    request, code = read_request(argv[0], usage)
    if request is None:
        return code

    started = time.monotonic()
    try:
        data, message = _dispatch(operation, request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(operation, str(exc)))

    builder = response.ok if data.get("ok", True) else response.fail
    metrics = {"duration_ms": int((time.monotonic() - started) * 1000)}
    if builder is response.fail:
        return response.emit(response.fail(operation, message, data=data, metrics=metrics))
    return response.emit(response.ok(operation, message=message, data=data, metrics=metrics))


def _dispatch(operation: str, request: dict) -> tuple[dict, str]:
    if operation in _LEVELS:
        from db_ops.common.restorestep import restore_step

        level = _LEVELS[operation]
        data = restore_step(level, request)
        applied = data.get("applied") or data.get("cataloged") or []
        what = "would apply" if data.get("dry_run") else "applied"
        return data, f"{data['engine']}: {what} {len(applied) or 1} {level} backup(s)."

    if operation == "restore-key":
        from db_ops.common.restorekey import import_key

        data = import_key(request)
        what = "would import" if data.get("dry_run") else "imported"
        return data, f"Certificate {data['certificate_name']} {what}."

    if operation == "restore-metadata":
        from db_ops.common.restoremetadata import replay

        data = replay(request)
        return data, f"{len(data.get('applied') or [])} metadata file(s) applied."

    from db_ops.common.verifyrestore import verify

    data = verify(request)
    return data, (f"{data['checked']} database(s) checked, {data['failed']} not usable."
                  if data["failed"] else f"{data['checked']} database(s) usable.")


