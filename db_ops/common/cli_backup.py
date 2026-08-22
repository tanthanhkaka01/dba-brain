"""``backup-database`` — run one backup from a self-contained spec.

Plumbing only: :mod:`db_ops.common.backup` does the work, :mod:`db_ops.lib.response` shapes the
answer. The scheduling, the config and the store rows stay in ``backup_restore`` — this is the one
run, and nothing about when it should happen.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from db_ops.lib import response

OPERATION = "backup-database"

USAGE = """\
Usage: python -m db_ops.common.cli backup-database '<json>'|@file|-

Run ONE backup: ship a script to the host the database runs on, with the environment it needs, and
report whether it actually completed. Reads no config - the request states the script, the host and
the values.

  {"db_type": "oracle",               // oracle | postgresql | sqlserver | mysql
   "label": "CLOUD_ORA_DB/database", // optional; what the answer calls this run
   "level": "full",                   // optional; full | diff | log - translated per engine into
                                      // BACKUP_LEVEL. Omit and the script decides for itself.
   "script_path": "assets/backup/oracle/oracle_rman_database.sh",
   "script": "#!/bin/bash\\n...",      // or the text inline; give exactly one of the two
   "env": {"DOCKER_CONTAINER": "ora_dg_lab-primary",
           "BACKUP_DIR": "/opt/oracle/backup/dbops",
           "RETENTION_DAYS": "14",
           "BACKUP_ENCRYPTION_PASSWORD": "..."},   // RESOLVED values, never secret refs
   "host": {"runtime": "linux",       // windows | linux | docker | k8s
            "access": "ssh",          // ssh (default) | winrm - HOW it is reached, which is a
                                      // different question from what runs there. Every Windows
                                      // SQL Server here is reached by winrm.
            "host": "203.0.113.188", "port": 22,
            "username": "ubuntu", "key_file": "/keys/oracle-cloud.key"},
   "server_metadata": {               // optional, SQL Server only
       "target": "ACME-192-0-2-115",     // instance to read
       "output_dir": "/backup/_instance",  // beside the backup it describes
       "include": ["logins", "agent_jobs"]},   // default: every artifact the policy declares
   "timeout": 7200,                   // seconds; default 3600
   "dry_run": true}                   // optional; report the plan, ship nothing

A SQL Server backup covers user databases only (`database_id > 4`), so master/msdb/model are
excluded and every login, server role, permission, credential, linked server, Agent job and
sp_configure value is absent after a restore - the database comes back and none of the machinery
around it does. `server_metadata` exports that beside the backup. It is refused for oracle and
postgresql, whose physical backups carry the state inside the data already.

A metadata failure NEVER fails the backup: the data is the thing that must not be lost. It is
skipped entirely when the backup itself failed, because a bundle beside a backup that did not
complete is a pair of files that look matched and are not.

The script runs ON the host, not inside the container: these scripts `docker exec` themselves,
because they need to be on the host for the directory the backup is written to. Such a `docker
exec` must close its own stdin (no -i) - the script IS the shell's stdin, and a container that
reads it eats the rest of the script.

`level` is one word for every engine. Oracle and PostgreSQL have no `log` here on purpose: their
archive/WAL backups are a separate script with its own schedule, and asking for one is refused
rather than passed into a script that would read it as something else.

SUCCESS MEANS THE SCRIPT PRINTED `RESULT=ok`, not that it exited 0. A script that ends early with
a clean status and no output has happened here, and a backup that silently reports success is only
discovered by the restore that needed it.

data: {"status": "done"|"error", "exit_code", "duration_ms", "receipt", "error",
       "stdout", "stderr", "label", "level", "script", "host", "env_names", ...}
env_names, never env values - half of them are passphrases.
"""


def run(argv: list[str], *, read_request: Any) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(
            OPERATION, f"backup-database takes one JSON payload; got {len(argv)} arguments."))

    request, code = read_request(argv[0], USAGE)
    if request is None:
        return code

    from db_ops.common.backup import parse_backup_spec, plan_backup, run_backup

    try:
        spec = parse_backup_spec(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(OPERATION, str(exc)))

    if spec.dry_run:
        planned = plan_backup(spec)
        return response.emit(response.ok(
            OPERATION,
            message=f"Would run {planned['script']} on {planned['host']} "
                    f"({planned['level']}).",
            data={**planned, "dry_run": True}))

    try:
        result = run_backup(spec)
    except Exception as exc:  # noqa: BLE001
        return response.emit(response.fail(OPERATION, str(exc)))

    metrics = {"duration_ms": result["duration_ms"], "exit_code": result["exit_code"]}
    label = result["label"] or result["script"]
    if result["status"] != "done":
        return response.emit(response.fail(
            OPERATION, result["error"] or "the backup did not complete",
            message=f"Backup {label} failed.", data=result, metrics=metrics))
    return response.emit(response.ok(
        OPERATION,
        message=f"Backup {label} completed in {result['duration_ms'] // 1000}s ({result['level']}).",
        data=result, metrics=metrics))


