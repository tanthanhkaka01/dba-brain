"""Stop one running backup on its host - ``common.cli backup-stop`` (0.34.0, 1.238).

Why a command: on a production SQL Server (2026-10-29) a manual FULL was "stopped" by killing its engine
session. The script driving it went through its database list, reconnected a second later and
backed up the next database - 548 GB from and onto the drive holding the data file - for another
hour, slowing the application's bulk inserts. It was finally stopped by ending the WinRM host of the
run and its sqlcmd, by pid and parent, then checking that no BACKUP session was left. This is that,
in the right order, as one call:

1. **the run's process tree on the host**, under the pid its script wrote to
   ``<temp>/dbabrain_runs/<run_tag>.pid`` - nothing is left to start the next database;
2. **SQL Server: every session the run opened** - its sqlcmd named each ``host_name =
   dbabrain:<run_tag>``, so a native job's backup on the same instance is never touched;
3. **what is left**, said: ``stopped`` is true only when the process is gone and no session remains.

A WinRM call that times out leaves its script running (pypsrp has no cancel), and a runner killed
locally leaves it too - the stop has to happen on the host. The request is the run's own host and
env (``backup-database``'s shape), plus its tag; nothing is read from configuration (R09).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from dbabrain.common.hostcmd import WINDOWS, parse_host, run_script
from dbabrain.lib import errors
from dbabrain.lib.backup_run import RUN_TAG_ENV, RUN_TAG_RE

#: The stop scripts, shipped beside the backup scripts; the host's platform picks one.
STOP_SCRIPTS = {"linux": "backup_stop.sh", WINDOWS: "backup_stop.ps1"}
_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "backup_scripts" / "stop"
DEFAULT_TIMEOUT_SECONDS = 120
#: The env the stop script reads; anything else in the request's env is not sent.
STOP_ENV = ("DOCKER_CONTAINER", "MSSQL_SERVER", "MSSQL_USER", "MSSQL_PASSWORD")


class BackupStopError(errors.InvalidConfig):
    """The request cannot describe a stop."""


def parse_stop_lines(text: str) -> dict[str, Any]:
    """The script's ``STOP`` lines as one answer."""
    answer: dict[str, Any] = {"pid": None, "pid_file": "found", "process_alive": False, "killed": [],
                              "sessions_killed": [], "sessions_left": []}
    for raw in str(text or "").splitlines():
        line = raw.rstrip("\r")
        if not line.startswith("STOP\t"):
            continue
        fields = dict(part.partition("=")[::2] for part in line.split("\t")[1:])
        if "pid" in fields:
            answer["pid"] = int(fields["pid"]) if fields["pid"].isdigit() else None
        if fields.get("pid_file") == "missing":
            answer["pid_file"] = "missing"
        if "alive" in fields:
            answer["process_alive"] = fields["alive"] == "1"
        if fields.get("killed", "").isdigit():
            answer["killed"].append(int(fields["killed"]))
        for key, target in (("session_killed", "sessions_killed"), ("session_left", "sessions_left")):
            if fields.get(key, "").isdigit():
                answer[target].append({"session_id": int(fields[key]), "database": fields.get("db", "")})
    return answer


def stop_backup(request: dict[str, Any]) -> dict[str, Any]:
    """Run the stop on the run's host and answer what it ended and what is left."""
    if not isinstance(request, dict):
        raise BackupStopError("request must be a JSON object.")
    tag = str(request.get("run_tag") or "").strip()
    if not RUN_TAG_RE.match(tag):
        raise BackupStopError("run_tag is required: 1-100 characters of A-Z a-z 0-9 _ . - "
                              "(the tag the run was started with, DBABRAIN_RUN_TAG).")
    db_type = str(request.get("db_type") or "").strip().lower()
    if not db_type:
        raise BackupStopError("db_type is required: sqlserver ends the run's sessions too.")
    host = parse_host(request.get("host"))
    raw_env = request.get("env") or {}
    if not isinstance(raw_env, dict):
        raise BackupStopError("env must be an object of resolved values.")
    timeout = request.get("timeout")
    try:
        timeout_seconds = int(timeout) if timeout not in (None, "") else DEFAULT_TIMEOUT_SECONDS
    except (TypeError, ValueError) as exc:
        raise BackupStopError(f"timeout must be a whole number of seconds; got {timeout!r}.") from exc
    platform = WINDOWS if host.runtime == WINDOWS else "linux"
    script = (_SCRIPT_DIR / STOP_SCRIPTS[platform]).read_text(encoding="utf-8")
    env = {name: str(raw_env[name]) for name in STOP_ENV if raw_env.get(name) not in (None, "")}
    env.update({RUN_TAG_ENV: tag, "DB_TYPE": db_type})

    started = time.monotonic()
    result = run_script(host, script, env=env, timeout=timeout_seconds)
    stdout = str(result.get("stdout") or "")
    answer = parse_stop_lines(stdout)
    finished = "RESULT=ok" in stdout
    answer.update({
        "run_tag": tag, "db_type": db_type, "host": host.host or "this machine", "platform": platform,
        "exit_code": result.get("exit_code"), "duration_ms": int((time.monotonic() - started) * 1000),
        # Stopped means nothing of the run is left: its process gone, and no session of it.
        "stopped": finished and not answer["process_alive"] and not answer["sessions_left"],
        "error": "" if finished else (str(result.get("stderr") or "").strip()[-2000:]
                                      or "the stop script did not finish"),
    })
    return answer
