"""Whether the daemon waits for an app command to finish before starting it again.

**The failure.** One SQL task that took 22 minutes held every other SQL task for 22 minutes, because
``APP-SQL_TASKS`` is a single app command, the daemon runs one process per app command, and that one
process works through its due list in order. The same is true of ``APP-BACKUP-RESTORE``: a long
backup is a stopped queue, not a busy one.

**The answer is one field, not one special case per app.** An app command declares how it wants to
be called:

| ``run_mode`` | The daemon | The app |
| --- | --- | --- |
| ``sync`` (default) | starts it only when no run of it is in flight — today's behaviour, unchanged | may assume it is alone |
| ``async`` | **starts it whenever it is due, in flight or not**, up to ``max_parallel`` | **must refuse its own duplicates**, because nothing else will |

The second half of that table is the whole risk, and it is why this file is not just a boolean. An
``async`` app that does not claim its work runs the same task twice — eight duplicate production SQL
runs on 2026-09-08, a second restore 47 minutes into the first on 2026-09-14. What makes ``async``
safe is the **claim**: a unique index accepts one ``running`` row per unit of work
(``ux_sql_runs_claim`` per task-and-target, ``ux_job_runs_claim`` per backup job or restore), so a
second process is told the work is taken and moves on to the next item instead.

**``max_parallel`` is not a tuning knob, it is a brake.** ``APP-SQL_TASKS`` declares
``repeat_interval: 1``: due every second. Without a cap, ``async`` means a new process every second
for as long as the first one runs — which is the loop, arriving by a different door than the one the
claim closes.
"""

from __future__ import annotations

from db_ops.lib import errors
from dataclasses import dataclass

SYNC = "sync"
ASYNC = "async"
MODES: tuple[str, ...] = (SYNC, ASYNC)

#: The default, and the behaviour every app command had before this field existed. Chosen so that a
#: config written before this release keeps running exactly as it did.
DEFAULT_MODE = SYNC

#: How many processes of one ``async`` command may be alive at once when it does not say.
DEFAULT_MAX_PARALLEL = 4

#: The most any command may ask for. A number past this is nearly always a misunderstanding of what
#: the field does — it schedules processes, not threads inside one — and the cost lands on the
#: database being worked, not on this host.
MAX_PARALLEL_LIMIT = 32


class RunModeError(errors.ConfigError):
    """An app command whose ``run_mode`` block cannot be obeyed as written."""


@dataclass(frozen=True)
class RunMode:
    mode: str = DEFAULT_MODE
    max_parallel: int = 1

    @property
    def is_async(self) -> bool:
        return self.mode == ASYNC


def parse(entry: dict | None) -> RunMode:
    """Read ``run_mode`` and ``max_parallel`` from one ``app_commands.json`` record.

    A record that says nothing is ``sync``, one at a time — which is what every record said before
    this field existed, so an older config does not change behaviour by being read by a newer build.
    """
    data = entry or {}
    raw_mode = data.get("run_mode")
    mode = str(raw_mode or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        raise RunModeError(
            f"run_mode must be one of {', '.join(MODES)}, got {raw_mode!r}")

    raw_parallel = data.get("max_parallel")
    if mode == SYNC:
        if raw_parallel not in (None, "", 1, "1"):
            raise RunModeError(
                "max_parallel applies only to run_mode 'async'; a 'sync' command runs one at a "
                "time by definition")
        return RunMode(mode=SYNC, max_parallel=1)

    if raw_parallel in (None, ""):
        return RunMode(mode=ASYNC, max_parallel=DEFAULT_MAX_PARALLEL)
    try:
        parallel = int(raw_parallel)
    except (TypeError, ValueError) as exc:
        raise RunModeError(f"max_parallel must be a whole number, got {raw_parallel!r}") from exc
    if parallel < 1:
        raise RunModeError(
            f"max_parallel must be at least 1, got {parallel}. To stop a command running, set "
            '"active": false - a cap of 0 would leave it due forever and never run it')
    if parallel > MAX_PARALLEL_LIMIT:
        raise RunModeError(
            f"max_parallel must be at most {MAX_PARALLEL_LIMIT}, got {parallel}")
    return RunMode(mode=ASYNC, max_parallel=parallel)


def may_start(run_mode: RunMode, *, in_flight: int) -> bool:
    """Given how many of this command are already running, may the daemon start another?"""
    if not run_mode.is_async:
        return in_flight == 0
    return in_flight < run_mode.max_parallel


__all__ = [
    "ASYNC",
    "DEFAULT_MAX_PARALLEL",
    "DEFAULT_MODE",
    "MAX_PARALLEL_LIMIT",
    "MODES",
    "SYNC",
    "RunMode",
    "RunModeError",
    "may_start",
    "parse",
]
