"""Turning a configured backup entry into the complete request ``common.cli backup-database`` takes.

This is the half that reads data, and it lives here because reading data is an app's job:
``restore_config.json`` for the entry, ``db_instances.json`` for the host behind a ``server_id``,
and the encrypted store for the passwords. ``common`` does none of that - it is handed the answers.

It also built a ``RestoreSpec`` for ``common.cli restore-database`` until 0.24.0; that command was a
third route for a SQL Server restore and went (rules R43), and the spec with it.
"""

from __future__ import annotations

from db_ops.lib import errors
from pathlib import Path
from typing import Any

from db_ops.lib.paths import resolve_tool_path
from db_ops.lib.timezone import display_now


class SpecBuildError(errors.ConfigError):
    """The entry cannot be turned into a complete request: a secret or a script it names is missing."""


def _secret(ref: str, secrets: dict[str, str], *, where: str) -> str:
    """Resolve one secret ref, or say which field wanted it. An empty password reaches the target
    as a failed login, which is a much worse place to discover a missing ref."""
    if not ref:
        return ""
    value = secrets.get(ref, "")
    if not value:
        raise SpecBuildError(
            f"{where} maps to secret ref {ref!r}, which is not in the secret store. Add it, or "
            "pass --key/--key-base64 so the store can be read."
        )
    return str(value)


def backup_request_from_job(
    job: Any, *, target: Any, secrets: dict[str, str], level: str = "",
    env_overrides: dict[str, str] | None = None, dry_run: bool = False,
    data_dir: str | Path | None = None,
) -> dict:
    """The ``backup-database`` request for one configured job — every lookup already resolved.

    This is the seam the layering rests on, and it is the app's half of it: ``restore_config.json``
    produced the job, ``db_instances.json`` produced the target, and the secret store turned each
    ``env_secrets`` ref into the value the script will read. What crosses to ``common`` is a plain
    object with no reference to any of that, so the same call works for a scheduled run and for a
    one-off against a machine in no inventory at all.
    """
    env: dict[str, str] = {"BACKUP_DIR": job.backup_dir}
    # The weekly-full rule in the PostgreSQL and Oracle scripts ("Sunday") is a day in THIS node's
    # configured timezone. Left to the script it read the host's clock, which may be UTC - a
    # Sunday that began seven hours late on a +07 node. Set before job.env so a job can pin it.
    env["DB_OPS_WEEKDAY"] = str(display_now().isoweekday())
    if target.container_name:
        # Omitted rather than sent empty on a host with no container: the spec refuses an empty
        # env value, because an empty value is how a missing secret arrives. A Windows SQL Server
        # runs the engine natively and has nothing to `docker exec` into.
        env["DOCKER_CONTAINER"] = target.container_name
    if job.retention_days is not None:
        env["RETENTION_DAYS"] = str(job.retention_days)
    # A window under a day, exactly. Beside the days, not instead of them: each script prunes the
    # way its engine does, and RMAN's recovery window is whole days - Oracle reads the days (one,
    # at least), SQL Server and PostgreSQL read these seconds. See BackupJob.retention_seconds.
    if getattr(job, "retention_seconds", None) is not None:
        env["RETENTION_SECONDS"] = str(job.retention_seconds)
    env.update(job.env)
    for name, ref in (job.env_secrets or {}).items():
        env[name] = _secret(str(ref), secrets, where=f"{job.label}.env_secrets.{name}")
    # CLI --env wins over config: it exists for the one-off run (a level 0 baseline outside the
    # Sunday schedule) and must not be silently overruled by the job's own settings.
    env.update(env_overrides or {})

    return {
        "db_type": job.db_type,
        "label": job.label,
        "level": level,
        "script_path": str(_script_path(job.script, label=job.label, data_dir=data_dir)),
        "env": env,
        "host": {
            # windows: the engine runs natively on the host. docker: the script `docker exec`s
            # into the container itself, so it still runs ON the host - the runtime tells hostcmd
            # which interpreter to use, not where to step into.
            "runtime": ("windows" if str(getattr(target, "platform", "")).lower() == "windows"
                        else "linux"),
            "host": target.host,
            "port": target.port,
            "username": target.username,
            "key_file": _resolved_key_file(target, data_dir),
            "password": _ssh_password(target, secrets),
            # How to reach it, which is a different question from what runs there: a Windows
            # SQL Server may answer on SSH or on WinRM, and hostcmd dispatches on this.
            "access": str(getattr(target, "access", "") or "ssh").strip().lower(),
            "ssl": bool(getattr(target, "ssl", False)),
        },
        "timeout": job.time_window.timeout or 3600,
        "dry_run": dry_run,
    }


def _script_path(script: str, *, label: str, data_dir: str | Path | None = None) -> Path:
    from db_ops.backup_restore.backup import TOOL_ROOT

    path = Path(script)
    candidate = resolve_tool_path(path)
    if not candidate.is_file():
        raise SpecBuildError(f"{label}: script not found: {candidate}")
    return candidate


def _resolved_key_file(target: Any, data_dir: str | Path | None = None) -> str:
    """The private key as a path on this machine, or empty when the target uses a password."""
    if not getattr(target, "key_file", None):
        return ""
    from db_ops.lib.data_sources import resolve_ssh_key

    return str(resolve_ssh_key(target.key_file, data_dir) or "")


def _ssh_password(target: Any, secrets: dict[str, str]) -> str:
    """Only when there is no key. A spec carrying both invites a caller to wonder which wins."""
    if getattr(target, "key_file", None):
        return ""
    ref = str(getattr(target, "password_ref", "") or "")
    return _secret(ref, secrets, where="backup.host.password_ref") if ref else ""
