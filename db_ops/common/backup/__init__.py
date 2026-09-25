"""Running one backup: ship the script to where the database lives, and judge the result honestly.

The judging is the part with history behind it. ``exit 0`` means the shell finished, not that a
backup happened — a script fed to ``bash -s`` can end early with a clean status and no output, and
one did: a ``docker exec -i`` inside it read the rest of the script as its own stdin, so the shell
ran out of work and reported success having backed up nothing. A backup that lies about succeeding
is worse than one that fails, because the failure is only discovered by the restore that needed it.

So every script ends by printing ``RESULT=ok``, and success here means **that receipt was seen**.
Exit 0 without it is reported as an error naming exactly that, which is a sentence an operator can
act on rather than a silent green run.
"""

from __future__ import annotations

import time
from typing import Any

from db_ops.common.backup.spec import (
    BACKUP_LEVEL_BY_ENGINE,
    LEVEL_ENV,
    RECEIPT,
    BackupSpec,
    BackupSpecError,
    backup_level_for,
    parse_backup_spec,
)
from db_ops.common.hostcmd import HostCommandError, run_script

__all__ = [
    "BACKUP_LEVEL_BY_ENGINE",
    "LEVEL_ENV",
    "RECEIPT",
    "BackupSpec",
    "BackupSpecError",
    "backup_level_for",
    "parse_backup_spec",
    "plan_backup",
    "run_backup",
]

#: How much of a script's output is worth carrying in an error. Enough to see what the engine said,
#: bounded because this ends up in a Telegram message and a store row.
EXCERPT = 2000


def plan_backup(spec: BackupSpec) -> dict[str, Any]:
    """What the run would do, without doing it. Also the dry-run answer.

    Env **names** but never values: the whole point of the plan is that it can be shown to somebody,
    and half of these are passphrases.
    """
    return {
        "db_type": spec.db_type,
        "label": spec.label,
        "level": spec.level or "(the script decides)",
        "script": spec.script_source,
        "script_lines": len(spec.script.splitlines()),
        "host": spec.host.host or "(this machine)",
        "runtime": spec.host.runtime,
        "container": spec.host.container,
        "env_names": sorted(spec.env),
        "timeout": spec.timeout,
    }


def run_backup(spec: BackupSpec) -> dict[str, Any]:
    """Run the backup and return what happened. Never raises for a backup that merely failed."""
    planned = plan_backup(spec)
    started = time.monotonic()
    try:
        result = run_script(spec.host, spec.script, env=spec.env, timeout=spec.timeout)
    except HostCommandError as exc:
        # Could not run at all - a host that refused the connection. Distinct from a backup that
        # ran and failed, because the fix is somewhere else entirely.
        return {**planned, "status": "error", "exit_code": None, "duration_ms": _ms(started),
                "stdout": "", "stderr": "", "error": str(exc), "receipt": False}

    out = result.get("stdout") or ""
    err = result.get("stderr") or ""
    exit_code = result.get("exit_code")
    receipt = RECEIPT in out
    status = "done" if exit_code == 0 and receipt else "error"

    if status == "done":
        error = None
    elif exit_code == 0 and not receipt:
        error = (
            f"Backup script exited 0 without printing {RECEIPT} - it did not run to completion. "
            f"stdout={out.strip()[-800:]!r} stderr={err.strip()[-800:]!r}"
        )
    else:
        error = (err.strip() or out.strip())[-EXCERPT:] or f"exit {exit_code}"

    result = {**planned, "status": status, "exit_code": exit_code, "duration_ms": _ms(started),
              "stdout": out, "stderr": err, "error": error, "receipt": receipt}
    return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
