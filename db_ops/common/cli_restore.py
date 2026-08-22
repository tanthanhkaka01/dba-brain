"""The ``restore-database`` command: parse a spec, plan it, run it. Nothing else.

Its own module because ``common/cli.py`` is a dispatcher, not a place for commands to accumulate -
a command that lives in the dispatcher can only be tested through argv, and grows until nobody
reads it. Everything here is plumbing between :mod:`db_ops.common.restore` and stdout.

The payload carries the whole restore - engine, both ends, credentials - because this layer reads
no config. See :mod:`db_ops.lib.restore.spec` for why, and
:mod:`db_ops.backup_restore.spec_builder` for the thing that turns a configured entry into one.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from db_ops.lib import response

USAGE = """\
Usage: python -m db_ops.common.cli restore-database '<json>'
       python -m db_ops.common.cli restore-database @request.json
       echo '<json>' | python -m db_ops.common.cli restore-database -

Restore a database from a self-contained spec. This command reads no config: the request states
the engine, both ends and the credentials, so it works against a machine nothing has registered.

  {"db_type": "sqlserver",
   "source": {"access": "smb",          // smb | ssh | local
              "host": "192.0.2.250",
              "path": "\\\\\\\\192.0.2.250\\\\SQLBK\\\\APPDB-DB$APPDB",
              "username": "...", "password": "..."},
   "target": {"platform": "linux",      // windows | linux
              "host": "192.0.2.249", "port": 1433,
              "username": "sa", "password": "...",
              "data_dir": "/var/opt/mssql/data",
              "import_dir": "/opt/mssql2025/backup/SQLBK_IMPORT",
              "container": "mssql2025", // optional, for clearer logs only
              "ssh_username": "tuser", "ssh_password": "..."},
   "databases": ["APPDB"],               // optional; empty = every database in the backup set
   "point_in_time": "2026-08-06 14:00:00 +07:00",  // optional; refused, never downgraded
   "dry_run": true}                     // optional; plan without touching anything

A container target is a Linux host with the instance on a port - point_in_time works there, on an
Ubuntu VM and on a Windows VM alike, because STOPAT has no platform branch.

Exit 0 on success, 1 on failure (the JSON output carries {"ok": false, "error"}).
"""


OPERATION = "restore-database"


def _fail(error: str) -> int:
    return response.emit(response.fail(OPERATION, error))


def run(argv: list[str], *, read_request: Any) -> int:
    """``restore-database``. ``read_request`` is the dispatcher's shared JSON-payload reader."""
    from db_ops.common.restore import parse_restore_spec, plan_restore

    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE, file=sys.stderr)
        return 0 if argv else 2

    if len(argv) > 1:
        return _fail(f"restore-database takes one JSON payload; got {len(argv)} arguments.")

    request, code = read_request(argv[0], USAGE)
    if request is None:
        return code

    try:
        spec = parse_restore_spec(request)
        # The plan is computed for every run, not only a rehearsal: it is what refuses a
        # point-in-time request the chosen method cannot honour, and it has to refuse before
        # anything is copied or dropped.
        planned = plan_restore(spec)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return _fail(str(exc))

    if spec.dry_run:
        return response.emit(response.ok(
            OPERATION,
            message=f"Would restore {planned['target']} ({planned['restore_mode']}).",
            data={**planned, "dry_run": True},
        ))

    started = time.monotonic()
    try:
        from db_ops.common.restore import run_restore

        result = run_restore(spec, planned)
    except Exception as exc:  # noqa: BLE001
        return _fail(str(exc))

    restored = result.get("databases") or []
    return response.emit(response.ok(
        OPERATION,
        message=f"Restored {len(restored)} database(s) to {planned['target']} "
                f"({planned['restore_mode']}).",
        data=result,
        metrics={"duration_ms": int((time.monotonic() - started) * 1000),
                 "databases": len(restored)},
    ))
