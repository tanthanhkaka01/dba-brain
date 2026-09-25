"""``run-sqlcmd`` — one ``sqlcmd`` batch, run where the SQL Server is.

Plumbing only: :mod:`db_ops.common.sqlcmd_run` does the work, :mod:`db_ops.lib.response` shapes
the answer. The SMB restore's statements run through here since 0.23.0 (1.38); the app decides
what to run and what the answer means.

**stdin only**, like ``secret-set``: the request carries a SQL password and a host password.
"""

from __future__ import annotations

import sys
from typing import Any

from db_ops.lib import response

OPERATION = "run-sqlcmd"

USAGE = """\
Usage: <request> | python -m db_ops.common.cli run-sqlcmd -

Run ONE sqlcmd batch where the SQL Server is. Reads no config - every value is resolved.

  {"sql": "RESTORE DATABASE [APPDB] FROM DISK = N'/import/APPDB_FULL.bak' WITH NORECOVERY, STATS = 10",
   "instance": "localhost,1433",        // -S, as the host running sqlcmd sees it
   "sqlcmd_path": "sqlcmd",
   "username": "sa", "password": "...", // a SQL login; give neither for -E
   "login_timeout_seconds": 30, "query_timeout_seconds": 0,
   "timeout_seconds": 0,                // the whole run; 0 = none
   "via": "ssh",                        // local | ssh | winrm
   "host": {"host": "192.0.2.249", "port": 22, "username": "labuser", "password": "...",
            "open_timeout_seconds": 30}}   // ssh / winrm only

Progress (every stdout line) goes to stderr as it arrives; the answer is
data: {"via", "exit_code", "stdout", "stderr", "timed_out", "duration_ms"}.
A non-zero exit is an answer, not a failure of this command - the caller decides what it means.
"""


def run(argv: list[str], *, read_request: Any) -> int:
    if argv and argv[0] in {"-h", "--help"}:
        print(USAGE)
        return 0
    if not argv or argv[0] != "-":
        print(USAGE, file=sys.stderr)
        return response.emit(response.fail(
            OPERATION, "the request must arrive on stdin (-): it carries a SQL password and a host "
                       "password, which inline are visible on the command line"))
    request, code = read_request("-", USAGE)
    if request is None:
        return code

    from db_ops.common.hostcmd import HostCommandError
    from db_ops.common.sqlcmd_run import SqlcmdRunError, run_sqlcmd
    from db_ops.lib.ssh_errors import SshError

    try:
        data = run_sqlcmd(request)
    except (SqlcmdRunError, HostCommandError, SshError, OSError) as exc:
        return response.emit(response.fail(OPERATION, str(exc)))
    except Exception as exc:  # noqa: BLE001 - the caller parses an answer; a traceback is none
        return response.emit(response.fail(OPERATION, f"{type(exc).__name__}: {exc}"))
    state = ("timed out" if data["timed_out"] else f"exit {data['exit_code']}")
    return response.emit(response.ok(OPERATION, message=f"sqlcmd via {data['via']}: {state}",
                                     data=data, metrics={"duration_ms": data["duration_ms"]}))
