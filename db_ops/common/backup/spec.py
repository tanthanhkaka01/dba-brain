"""The backup spec: everything a backup run needs, stated in the request rather than looked up.

Same split as :mod:`db_ops.lib.restore.spec`, and for the same reason. ``backup_restore`` reads
``restore_config.json`` for the entry, asks ``db_instances.json`` for the SSH host and container
behind a ``server_id``, decrypts the passphrase, decides whether the job is due — and hands the
finished object down. Nothing here reads any of that.

What is left after that subtraction is small and worth having on its own: take *this* script to
*that* host with *these* environment variables, run it, and decide honestly whether it worked. A
one-off level 0 baseline on a machine that is in no inventory is then the same call as the
scheduled run, which is exactly the situation a real recovery tends to be.

    {"db_type": "oracle",
     "label": "CLOUD_ORA_DB/database",
     "level": "full",                       // optional; full | diff | log
     "script_path": "assets/backup/oracle/oracle_rman_database.sh",
     "env": {"DOCKER_CONTAINER": "ora_dg_lab-primary",
             "BACKUP_DIR": "/opt/oracle/backup/dbops",
             "RETENTION_DAYS": "14"},
     "host": {"runtime": "linux", "host": "203.0.113.188", "username": "ubuntu",
              "key_file": "data/ssh_keys/oracle-cloud.key"},
     "timeout": 7200}

``env`` carries **resolved values, never refs.** The caller decrypts, because the caller is the one
that knows which store and which passphrase; a spec holding ``TOKEN_203_0_113_188_BACKUP_ENC``
would make this layer read the secret store, which is the lookup the split exists to remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from db_ops.common.hostcmd import Host, parse_host
# Re-exported: the engine level vocabulary moved to db_ops/lib/backup_level.py so the
# app can decide what is due without importing `common`.
from db_ops.lib.backup_level import (  # noqa: F401 - re-exported for compatibility
    BACKUP_LEVEL_BY_ENGINE, BackupSpecError, backup_level_for)
from db_ops.lib.paths import resolve_tool_path





#: The env var a script reads its level from, when one is forced.
LEVEL_ENV = "BACKUP_LEVEL"

#: The receipt every backup script prints as its last line.
#:
#: Exit 0 alone is not proof a backup ran. A script fed to ``bash -s`` can end early with a clean
#: status and no output — a ``docker exec -i`` swallowing the rest of the script did exactly that —
#: and "silently reported success" is the worst failure a backup can have. So success is a positive
#: statement by the script, not the absence of an error from the shell.
RECEIPT = "RESULT=ok"




@dataclass(frozen=True)
class BackupSpec:
    """One backup run, fully described."""

    db_type: str
    script: str
    host: Host
    label: str = ""
    level: str = ""
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = 3600
    dry_run: bool = False
    #: Where the script came from, for the answer. Not used to run anything.
    script_source: str = ""
    #: SQL Server only: also export the instance's server-level metadata next to the backup.
    #: ``{"target": ..., "output_dir": ..., "include": [...]}`` — see :func:`_server_metadata`.
    server_metadata: dict[str, Any] = field(default_factory=dict)


def parse_backup_spec(request: dict[str, Any]) -> BackupSpec:
    """Validate a request into a :class:`BackupSpec`, refusing anything ambiguous."""
    if not isinstance(request, dict):
        raise BackupSpecError("request must be a JSON object.")

    db_type = str(request.get("db_type") or "").strip().lower()
    if db_type not in BACKUP_LEVEL_BY_ENGINE:
        raise BackupSpecError(
            f"db_type must be one of {', '.join(sorted(BACKUP_LEVEL_BY_ENGINE))}; got {db_type!r}."
        )

    script, source = _script(request)
    env = _env(request.get("env"))

    level = str(request.get("level") or "").strip().lower()
    if level:
        # Translated here rather than in the script so one word works for every engine, and so an
        # engine that has no such level is refused before anything is shipped anywhere.
        env[LEVEL_ENV] = backup_level_for(db_type, level)

    timeout = request.get("timeout")
    try:
        timeout_seconds = int(timeout) if timeout not in (None, "") else 3600
    except (TypeError, ValueError) as exc:
        raise BackupSpecError(f"timeout must be a whole number of seconds; got {timeout!r}.") from exc
    if timeout_seconds <= 0:
        raise BackupSpecError("timeout must be greater than zero.")

    return BackupSpec(
        db_type=db_type,
        script=script,
        script_source=source,
        host=parse_host(request.get("host")),
        label=str(request.get("label") or "").strip(),
        level=level,
        env=env,
        timeout=timeout_seconds,
        dry_run=bool(request.get("dry_run")),
        server_metadata=_server_metadata(request.get("server_metadata"), db_type=db_type),
    )


def _server_metadata(raw: Any, *, db_type: str) -> dict[str, Any]:
    """The opt-in that carries an instance's logins, roles, Agent jobs and linked servers.

    A SQL Server backup covers user databases only — the script selects ``database_id > 4``, so
    ``master``/``msdb``/``model`` are excluded and every login, server role, permission, credential,
    linked server, Agent job and ``sp_configure`` value is absent after a restore. The database
    comes back and none of the machinery around it does.

    Oracle and PostgreSQL need no equivalent: RMAN ``DUPLICATE`` and ``pg_basebackup`` are physical
    and whole-instance, so that state is inside the data. Asking for it there is refused rather
    than quietly ignored — a caller that set it believes it is getting something.
    """
    if raw in (None, {}, False):
        return {}
    if not isinstance(raw, dict):
        raise BackupSpecError("server_metadata must be an object.")
    if not bool(raw.get("enabled", True)):
        return {}
    if db_type != "sqlserver":
        raise BackupSpecError(
            f"server_metadata is SQL Server only; {db_type} backups are physical and carry "
            "instance state inside the data already."
        )
    target = str(raw.get("target") or "").strip()
    if not target:
        raise BackupSpecError("server_metadata.target is required: the instance to read.")
    output_dir = str(raw.get("output_dir") or "").strip()
    if not output_dir:
        raise BackupSpecError(
            "server_metadata.output_dir is required. The bundle belongs beside the backup it "
            "describes; a default here would put it somewhere the restore does not look."
        )
    metadata: dict[str, Any] = {"target": target, "output_dir": output_dir}
    include = raw.get("include")
    if include:
        if isinstance(include, str):
            raise BackupSpecError('server_metadata.include must be an array, e.g. ["logins"].')
        metadata["include"] = [str(item).strip() for item in include if str(item).strip()]
    return metadata


def _script(request: dict[str, Any]) -> tuple[str, str]:
    """The script text, from ``script`` or read from ``script_path``.

    ``script_path`` is not a config lookup: the caller named one exact file, the way ``pull-file``
    names a path. It exists because a backup script is a hundred lines of shell and a caller
    driving this from a shell should not have to JSON-escape it.
    """
    inline = request.get("script")
    path = str(request.get("script_path") or "").strip()
    if inline and path:
        raise BackupSpecError("give either script or script_path, not both.")
    if inline:
        text = str(inline)
        if not text.strip():
            raise BackupSpecError("script is empty.")
        return text, "<inline>"
    if not path:
        raise BackupSpecError("one of script or script_path is required.")
    # Relative to the tool root, not to whatever directory the process happens to be in — and
    # with the built-in assets as a fallback, so `"assets/backup/..."` in a config keeps working
    # wherever the shipped scripts were installed.
    candidate = resolve_tool_path(path)
    if not candidate.is_file():
        # Named rather than guessed at: a relative path that missed is a different mistake from a
        # path that is right and unreadable, and both used to arrive as the same empty script.
        raise BackupSpecError(f"script_path not found: {candidate}")
    try:
        return candidate.read_text(encoding="utf-8"), str(candidate)
    except OSError as exc:
        raise BackupSpecError(f"script_path could not be read: {candidate}: {exc}") from exc


def _env(raw: Any) -> dict[str, str]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise BackupSpecError("env must be an object of {NAME: value}.")
    env: dict[str, str] = {}
    for name, value in raw.items():
        key = str(name).strip()
        if not key:
            raise BackupSpecError("env has an entry with an empty name.")
        if value in (None, ""):
            # An empty value is how a missing secret arrives, and a backup script reading it as
            # "no passphrase" writes an unencrypted set that nobody notices until a restore.
            raise BackupSpecError(f"env[{key}] is empty. Resolve it before building the spec.")
        env[key] = str(value)
    return env
