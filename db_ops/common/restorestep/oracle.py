"""Oracle: RMAN is asked to do the work, and the instance lifecycle around it is done here.

RMAN restores from its catalogue, never from a file name a caller hands it, so a piece the target
has not seen is **cataloged** first and RMAN then chooses what to read.

**Two modes, because they answer different questions.** Defaulting to the wrong one is not a
tuning mistake - it either fails outright or restores something nobody asked for:

* ``duplicate`` (**default**) - a *different* instance rebuilds itself from another database's
  backup. ``DUPLICATE ... BACKUP LOCATION`` builds a NEW database with its own DBID and its own
  file paths, which is exactly what a drill wants: the source keeps running and the copy cannot be
  mistaken for it. ``RESTORE DATABASE`` cannot do this - it restores a database into *itself*, so
  it needs a matching DBID and refuses across instances.
* ``restore`` - in-place recovery of a database from its own backups. Same DBID, same instance.

Everything below was learnt by ``assets/restore/oracle/oracle_rman_restore.sh`` the expensive way,
and is repeated here rather than rediscovered:

* **NOMOUNT for duplicate, MOUNT for restore.** DUPLICATE builds its own controlfile and needs the
  instance not to have one; RESTORE needs one mounted.
* **``shutdown abort``, never ``immediate``.** On 2026-08-05 an ``immediate`` stopped at
  ``alter pluggable database all close immediate`` and never returned, and the 02:00 drill hung
  until an operator noticed in the morning - nothing else bounds it, because the entry's timeout
  marks the run row, it does not kill the shell. A restore target is the one instance where a
  graceful shutdown buys nothing.
* **``SET DECRYPTION`` goes OUTSIDE the RUN block.** Inside one, RMAN answers RMAN-03032; verified
  on 23.26.2. Wrapping both in ``RUN{}`` does not help.
* **Success is the banner, not the exit code.** RMAN prints error stacks and still exits 0, so a
  duplicate is believed only when it says ``Finished Duplicate Db``.
* **Scripts travel as base64.** ``sh -c`` -> ``docker exec`` -> ``sh -lc`` is three chances for a
  shell to eat a ``$``, and ``v$database`` became ``v`` more than once while this was written -
  which reports as ORA-00942 and reads like a broken database rather than a quoting bug.
"""

from __future__ import annotations
from db_ops.common.backupfiles.oracle import _NLS  # noqa: F401 - one definition, see that module

import base64
import shlex
from typing import Any

from db_ops.common.hostcmd import parse_host, run
from db_ops.common.restorestep import DIFF, FULL, LOG, RestoreStepError

DUPLICATE = "duplicate"
RESTORE = "restore"
MODES = (DUPLICATE, RESTORE)

_WORK = "/tmp/dbops_restore"


def _deliver(script: str, path: str) -> str:
    """Write ``script`` to ``path`` without letting any shell in the chain read its contents."""
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"


def _rman(script: str, *, auxiliary: bool) -> str:
    # DUPLICATE ... BACKUP LOCATION restores INTO this instance, so RMAN connects to it as the
    # AUXILIARY; a plain `target /` would be the database being duplicated FROM.
    connect = "auxiliary /" if auxiliary else "target /"
    path = _WORK + ".rman"
    return (f"{_deliver(script, path)} && export {_NLS} && "
            f"rman {connect} cmdfile={shlex.quote(path)} log=/dev/stdout 2>&1")


def _sqlplus(script: str) -> str:
    path = _WORK + ".sql"
    return (f"{_deliver(script, path)} && export {_NLS} && "
            f"sqlplus -s -L / as sysdba @{shlex.quote(path)} 2>&1")


def _decryption(request: dict[str, Any]) -> str:
    """``SET DECRYPTION`` for an encrypted backup set, or nothing, said out loud either way.

    Without it an encrypted set fails deep inside with ORA-19913 ("unable to decrypt backup")
    rather than saying a key is missing.
    """
    password = str(request.get("encryption_password") or "")
    if not password:
        return ""
    if "'" in password:
        raise RestoreStepError(
            "encryption_password must not contain a single quote: it is passed to RMAN quoted."
        )
    return f"SET DECRYPTION IDENTIFIED BY '{password}';"


def build_steps(level: str, request: dict[str, Any], paths: list[str]) -> list[dict[str, str]]:
    """Every command this step will run, in order. Pure - nothing is executed."""
    mode = str(request.get("mode") or DUPLICATE).strip().lower()
    if mode not in MODES:
        raise RestoreStepError(f"mode must be one of {', '.join(MODES)}; got {mode!r}.")
    until = str(request.get("stopat") or "").strip()
    if until and level == FULL and mode == RESTORE:
        raise RestoreStepError(
            "stopat belongs to recovery, not to the restore of the datafiles; ask for it on the "
            "log step, which is where RMAN applies it."
        )
    steps: list[dict[str, str]] = []

    if mode == DUPLICATE:
        if level != FULL:
            raise RestoreStepError(
                "duplicate rebuilds the whole database in one operation - RMAN picks the level 0, "
                "the incrementals and the archived logs itself. There is no separate diff or log "
                "step. Use mode=restore for in-place recovery of an existing database."
            )
        backup_location = str(request.get("backup_location") or "").strip()
        if not backup_location:
            raise RestoreStepError(
                "backup_location is required for a duplicate: RMAN reads the whole set from one "
                "directory, and needs a controlfile autobackup in it to start."
            )
        sid = str(request.get("oracle_sid") or "").strip()
        if not sid:
            raise RestoreStepError("oracle_sid is required: DUPLICATE DATABASE TO <sid>.")

        # ABORT, not IMMEDIATE - see the module docstring. The instance is about to be rebuilt.
        steps.append({"name": "nomount", "command": _sqlplus(
            "WHENEVER SQLERROR CONTINUE\nSHUTDOWN ABORT;\nSTARTUP NOMOUNT;\nEXIT;\n")})
        until_line = (f"SET UNTIL TIME \"TO_DATE('{until}', 'YYYY-MM-DD HH24:MI:SS')\";\n"
                      if until else "")
        # SET DECRYPTION outside any RUN block: inside one RMAN answers RMAN-03032.
        script = (f"{_decryption(request)}\n{until_line}"
                  f"DUPLICATE DATABASE TO {sid}\n"
                  f"  BACKUP LOCATION '{backup_location}'\n"
                  f"  NOFILENAMECHECK;\nEXIT;\n")
        steps.append({"name": "duplicate", "command": _rman(script, auxiliary=True)})
        return steps

    # --- mode=restore: in-place recovery of this database from its own backups ---------------
    if level == FULL:
        steps.append({"name": "mount", "command": _sqlplus(
            "WHENEVER SQLERROR CONTINUE\nSHUTDOWN ABORT;\nSTARTUP MOUNT;\nEXIT;\n")})

    lines = [_decryption(request), "RUN {"]
    for path in paths:
        # Idempotent when RMAN already knows the piece, so it is always done: the alternative is
        # asking the caller to know whether this host has seen the file.
        lines.append(f"  CATALOG BACKUPPIECE '{path}';")
    if until:
        lines.append(f"  SET UNTIL TIME \"TO_DATE('{until}', 'YYYY-MM-DD HH24:MI:SS')\";")
    lines.append("  RESTORE DATABASE;" if level == FULL else "  RECOVER DATABASE;")
    lines += ["}", "EXIT;", ""]
    steps.append({"name": "restore" if level == FULL else "recover",
                  "command": _rman("\n".join(lines), auxiliary=False)})

    if bool(request.get("with_recovery", False)):
        # RESETLOGS because recovery ended before the end of the redo stream - true of every
        # point-in-time restore and of every restore from a backup set.
        steps.append({"name": "open", "command": _sqlplus(
            "WHENEVER SQLERROR EXIT SQL.SQLCODE\nALTER DATABASE OPEN RESETLOGS;\nEXIT;\n")})
    return steps


def _failed(name: str, output: str, exit_code: int) -> bool:
    """Whether a step went wrong. RMAN exits 0 while printing error stacks, so the text decides."""
    if name == "duplicate":
        # It prints a final banner; an error stack without it is a real failure.
        return "Finished Duplicate Db" not in output
    if name in {"nomount", "mount"}:
        # ORA-01109 ("database not open") is normal when shutting an instance that is already down.
        return exit_code != 0 or ("ORA-" in output and "ORA-01109" not in output)
    return exit_code != 0 or "ORA-" in output or "RMAN-" in output


def apply(level: str, request: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    steps = build_steps(level, request, paths)
    mode = str(request.get("mode") or DUPLICATE).strip().lower()
    if request.get("dry_run"):
        return {"engine": "oracle", "level": level, "mode": mode, "dry_run": True,
                "steps": [s["name"] for s in steps],
                "scripts": {s["name"]: s["command"] for s in steps}}

    host = parse_host(request.get("host"))
    timeout = int(request.get("timeout_seconds") or 7200)
    ran: list[dict[str, Any]] = []
    for step in steps:
        result = run(host, step["command"], timeout=timeout)
        output = result["stdout"]
        ran.append({"step": step["name"], "exit_code": result["exit_code"],
                    "output_tail": output.strip()[-1500:]})
        if _failed(step["name"], output, result["exit_code"]):
            raise RestoreStepError(f"oracle {step['name']} failed: {output.strip()[-800:]}")

    return {"engine": "oracle", "level": level, "mode": mode, "steps": ran,
            "cataloged": paths if mode == RESTORE else [],
            "backup_location": str(request.get("backup_location") or "") or None,
            "stopat": str(request.get("stopat") or "") or None,
            "note": ("RMAN chose which pieces to read from the backup location"
                     if mode == DUPLICATE else "pieces were cataloged; RMAN selected what to read")}
