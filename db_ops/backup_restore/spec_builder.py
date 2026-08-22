"""Turning a configured restore entry into a self-contained :class:`RestoreSpec`.

This is the half that reads data, and it lives here because reading data is an app's job:
``restore_config.json`` for the entry, ``db_instances.json`` for the host behind a ``server_id``,
and the encrypted store for the passwords. :mod:`db_ops.common.restore` does none of that - it is
handed the answers.

The split is what makes the API portable. ``common`` can be packaged and dropped anywhere with no
config file beside it, because everything it needs arrives in the request; and the same API is
callable for a one-off recovery against a machine that is in no inventory at all, which is exactly
the situation a real recovery tends to be.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from db_ops.lib.restore.plan import ENGINE, SCRIPT
from db_ops.lib.restore.spec import RestoreSpecError, parse_restore_spec
from db_ops.lib.paths import resolve_tool_path


def _secret(ref: str, secrets: dict[str, str], *, where: str) -> str:
    """Resolve one secret ref, or say which field wanted it. An empty password reaches the target
    as a failed login, which is a much worse place to discover a missing ref."""
    if not ref:
        return ""
    value = secrets.get(ref, "")
    if not value:
        raise RestoreSpecError(
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
    if target.container_name:
        # Omitted rather than sent empty on a host with no container: the spec refuses an empty
        # env value, because an empty value is how a missing secret arrives. A Windows SQL Server
        # runs the engine natively and has nothing to `docker exec` into.
        env["DOCKER_CONTAINER"] = target.container_name
    if job.retention_days is not None:
        env["RETENTION_DAYS"] = str(job.retention_days)
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
        raise RestoreSpecError(f"{label}: script not found: {candidate}")
    return candidate


def _resolved_key_file(target: Any, data_dir: str | Path | None = None) -> str:
    """The private key as a path on this machine, or empty when the target uses a password."""
    if not getattr(target, "key_file", None):
        return ""
    from db_ops.common.data_sources import resolve_ssh_key

    return str(resolve_ssh_key(target.key_file, data_dir) or "")


def _ssh_password(target: Any, secrets: dict[str, str]) -> str:
    """Only when there is no key. A spec carrying both invites a caller to wonder which wins."""
    if getattr(target, "key_file", None):
        return ""
    ref = str(getattr(target, "password_ref", "") or "")
    return _secret(ref, secrets, where="backup.host.password_ref") if ref else ""


def spec_from_engine_entry(
    config: Any, *, secrets: dict[str, str], point_in_time: str = "", dry_run: bool = False
) -> Any:
    """Build a spec from a ``BackupRestoreConfig`` - the SMB + sqlcmd entries."""
    return parse_restore_spec({
        "db_type": "sqlserver",
        "label": config.restore_id or config.source_id,
        "source": {
            "access": "smb",
            "host": config.prod_smb_credential_target,
            "path": str(config.prod_backup_share),
            "username": config.prod_smb_username,
            "password": _secret(config.prod_smb_password_env, secrets,
                                where=f"{config.restore_id}.source.password_env"),
        },
        "target": {
            "platform": config.vm_platform,
            "host": config.vm_credential_target,
            "port": 1433,
            "instance": config.restore_sql_instance_on_vm,
            "username": config.restore_sql_username,
            "password": _secret(config.restore_sql_password_env, secrets,
                                where=f"{config.restore_id}.target.sql_password_env"),
            "data_dir": str(config.restore_data_dir_on_vm),
            "import_dir": str(config.vm_import_unc),
            "ssh_username": config.vm_username,
            "ssh_password": _secret(config.vm_password_env, secrets,
                                    where=f"{config.restore_id}.target.password_env"),
        },
        "databases": [m.source_database_name for m in (config.databases or ())
                      if getattr(m, "source_database_name", "")],
        "point_in_time": point_in_time,
        "copy_hours": config.copy_recent_hours,
        "dry_run": dry_run,
        "extras": {"method": ENGINE, "restore_id": config.restore_id},
    })


def spec_from_script_entry(
    job: Any, *, secrets: dict[str, str], data_dir: str | Path | None = None,
    dry_run: bool = False,
) -> Any:
    """Build a spec from a ``ScriptRestore`` - the container-to-container drills.

    No ``point_in_time`` parameter: this method cannot honour one, and offering the argument would
    invite a caller to pass it and be quietly given the newest chain. The refusal belongs to
    :mod:`db_ops.lib.restore.pitr`, which sees it through ``extras.method``.
    """
    from db_ops.backup_restore.backup import resolve_ssh_target

    target = resolve_ssh_target(
        job.target_server_id or job.server_id, label=job.label,
        data_dir=data_dir, require_container=False,
    )
    return parse_restore_spec({
        "db_type": job.db_type,
        "label": job.label,
        "source": {
            "access": "ssh",
            "host": job.server_id,
            "path": job.source_backup_host_dir or job.backup_dir,
        },
        "target": {
            "platform": "linux",
            "host": target.host,
            "port": 1433,
            "username": job.env.get("MSSQL_USER", "sa"),
            "password": _secret(job.env_secrets.get("MSSQL_PASSWORD", ""), secrets,
                                where=f"{job.restore_id}.env_secrets.MSSQL_PASSWORD"),
            "container": job.target_container,
            "import_dir": job.target_backup_dir or job.backup_dir,
            "ssh_username": target.username,
        },
        "dry_run": dry_run,
        "extras": {"method": SCRIPT, "restore_id": job.restore_id},
    })
