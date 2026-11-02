"""``backup-stop`` - stop one running backup on its host, the right way round (0.34.0, 1.238)."""

from __future__ import annotations

import sys
from typing import Any

from dbabrain.lib import response

OPERATION = "backup-stop"

USAGE = """\
Usage: python -m dbabrain.common.cli backup-stop '<json>'   (or @file / -)

Stop ONE running backup on the host it runs on: first the run's own process tree (the backup script,
under the pid it wrote to <temp>/dbabrain_runs/<run_tag>.pid), then - SQL Server - every session the
run opened (its sqlcmd named each host_name = dbabrain:<run_tag>), then what is left, said. Reads no
config: the request is the run's host and env, as backup-database took them, and its tag.

  {"run_tag": "ACME_MSSQL_SALES_FULL_8812",   // the run's DBABRAIN_RUN_TAG: <backup_id>_<job_runs id>
   "db_type": "sqlserver",                  // sqlserver also ends the run's sessions
   "host": {"runtime": "windows", "access": "winrm", "host": "192.0.2.245",
            "username": "...", "password": "..."},
   "env": {"MSSQL_SERVER": ".", "MSSQL_USER": "...", "MSSQL_PASSWORD": "...",
           "DOCKER_CONTAINER": "..."},      // RESOLVED values; only these four are sent
   "timeout": 120}                          // seconds; default 120

Killing the engine session first is the mistake this exists for: on a production SQL Server (2026-10-29) the
script reconnected a second later and backed up the next database, 548 GB, for another hour.

A pid is ended only when that process started before its pid file was written - a file left by a
run that died never ends whatever process has that number now. A run with no pid file (started
before 0.34.0, or already finished) still has its sessions ended.

data: {"run_tag", "db_type", "host", "platform", "pid", "pid_file": found|missing, "process_alive",
       "killed": [pid, ...], "sessions_killed": [{"session_id", "database"}], "sessions_left": [...],
       "stopped", "exit_code", "duration_ms", "error"}
stopped is true only when the process is gone and no session of the run is left; exit 1 otherwise.
"""


def run(argv: list[str], *, read_request: Any) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(USAGE, file=sys.stderr)
        return 0 if argv else 2
    if len(argv) > 1:
        return response.emit(response.fail(
            OPERATION, f"backup-stop takes one JSON payload; got {len(argv)} arguments."))

    request, code = read_request(argv[0], USAGE)
    if request is None:
        return code

    from dbabrain.common.backup.stop import stop_backup

    try:
        data = stop_backup(request)
    except Exception as exc:  # noqa: BLE001 - reported as JSON, like every command here.
        return response.emit(response.fail(OPERATION, str(exc)))

    killed = f"{len(data['killed'])} process(es), {len(data['sessions_killed'])} session(s) ended"
    if not data["stopped"]:
        left = data["error"] or (
            ("the process is still running" if data["process_alive"] else "")
            + ("; " if data["process_alive"] and data["sessions_left"] else "")
            + (f"{len(data['sessions_left'])} session(s) still open" if data["sessions_left"] else ""))
        return response.emit(response.fail(
            OPERATION, f"run {data['run_tag']} is not fully stopped: {left}",
            message=f"Backup run {data['run_tag']}: {killed} - not fully stopped.", data=data))
    return response.emit(response.ok(
        OPERATION, message=f"Backup run {data['run_tag']} stopped: {killed}.", data=data))
