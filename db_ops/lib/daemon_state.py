"""The daemon says when it started, so something other than the daemon can say how long it has.

`self-status` is asked "how long has DBA Brain been up on this node" and structurally cannot see
it: the process answering is the short-lived one the bot spawned to ask, not the scheduler. The
store could answer it, and `self-status` deliberately does not open the store — it is the one
status that still works when the store is the thing that is down.

So the daemon leaves a file. One small JSON object in ``runtime/``, written once at startup and
removed on a clean stop, holding the pid and the instant. Reading it costs nothing and needs no
process table, no store, and no privilege.

**The pid is what makes it trustworthy.** A hard kill leaves the file behind, so a reader that
believed the timestamp alone would report a daemon that has been "up" since a stop three days ago.
Every read checks the pid is a live process first and reports the file as stale otherwise — which
is itself worth saying out loud, because a stale file means the daemon died without unwinding.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db_ops.lib.process_liveness import is_pid_alive

#: In ``runtime/`` beside the store and the reports: generated state, never configuration, and
#: never carried in a config bundle - it describes one process on one machine.
STATE_FILENAME = "daemon_state.json"


def state_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / STATE_FILENAME


def record_start(runtime_dir: str | Path, *, version: str = "", node_role: str = "",
                 pid: int | None = None, now: datetime | None = None) -> Path:
    """Write the file. Failure is never fatal — a daemon that cannot say when it started still
    runs, and the reader reports the absence rather than inventing a time."""
    path = state_path(runtime_dir)
    moment = now or datetime.now(timezone.utc)
    payload = {
        "pid": int(pid if pid is not None else os.getpid()),
        "started_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": version,
        "node_role": node_role,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    except OSError:
        pass
    return path


def clear(runtime_dir: str | Path) -> None:
    """Remove the file on a clean stop, so 'no file' means 'stopped on purpose'."""
    try:
        state_path(runtime_dir).unlink()
    except OSError:
        pass


def read_state(runtime_dir: str | Path) -> dict[str, Any] | None:
    try:
        raw = state_path(runtime_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def uptime(runtime_dir: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    """How long the daemon of *this tool root* has been running.

    Four answers, and they are different facts rather than degrees of the same one:

    ``running``     the pid is alive and the file says when it started
    ``stopped``     no file - it was stopped cleanly, or has never run here
    ``stale``       a file whose pid is not a running process: it died without unwinding
    ``unreadable``  a file that is there and is not the shape this writes
    """
    state = read_state(runtime_dir)
    if state is None:
        return {"status": "stopped", "hours": None, "since": None, "pid": None}

    pid = state.get("pid")
    started = str(state.get("started_at") or "")
    try:
        began = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return {"status": "unreadable", "hours": None, "since": started or None, "pid": pid}
    if not isinstance(pid, int) or not is_pid_alive(pid):
        return {"status": "stale", "hours": None, "since": started, "pid": pid}

    seconds = ((now or datetime.now(timezone.utc)) - began).total_seconds()
    return {
        "status": "running",
        "hours": round(max(0.0, seconds) / 3600.0, 2),
        "since": started,
        "pid": pid,
        "version": state.get("version") or None,
        "node_role": state.get("node_role") or None,
    }


__all__ = ["STATE_FILENAME", "clear", "read_state", "record_start", "state_path", "uptime"]
