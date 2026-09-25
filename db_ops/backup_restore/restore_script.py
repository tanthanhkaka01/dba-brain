"""Script-driven restore for engines the SQL Server restore engine does not cover.

``restore_database.py`` is built around SQL Server: an SMB backup share, ``.bak``/``.trn`` files
copied to a VM, and ``sqlcmd RESTORE DATABASE``. Oracle and PostgreSQL restores share none of
that - their backups already sit on the container host that will restore them, so there is no
share to mount and no ``.bak`` to copy across the network.

So they are driven the same way their backups are: a shell script asset shipped over SSH and run
against the host, with the scheduling and run recording coming from :mod:`schedule` - the same
rules backup jobs use. Nothing here touches the SQL Server path.

The script runs the same logical phases as the SQL Server workflow, so an operator reads one
sequence whatever the engine is:

    restore-key   -> make the decryption material available on the target (wallet/keystore)
    copy-backup   -> put the backup pieces where the target instance can read them
    restore-data  -> restore/duplicate the database and open it

Each phase prints ``PHASE=<name> ...`` so a failure names the step it failed in.

A SQL Server entry that opts into ``server_metadata`` gets two more, printed the same way but
run from here rather than by the script - they are TDS connections, not shell steps:
``metadata-pre-database`` before ``restore-data`` and ``metadata-post-database`` after. Neither
can fail the restore; see :func:`replay_metadata_phase`.
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

from db_ops.backup_restore.events import announce
from db_ops.backup_restore import schedule
from db_ops.backup_restore.backup import (
    TOOL_ROOT,
    BackupTarget,
    _script_path,
    execute_over_ssh,
    resolve_ssh_target,
)
from db_ops.backup_restore.config import (
    DEFAULT_RESTORE_CONFIG_PATH,
    is_script_restore,
    parse_backup_restore_notify,
)
from db_ops.backup_restore.server_metadata import (
    ServerMetadataPlan,
    parse_server_metadata,
    replay_phase,
)
from db_ops.backup_restore.config import (
    DEFAULT_CLEANUP_RETENTION,
    parse_cleanup_retention,
)
from db_ops.lib import common_cli
from db_ops.lib import instance_bundle
from db_ops.lib.shell import docker_cli
from db_ops.lib.notify import NotifyConfig
from db_ops.lib.time_window import TimeWindow, parse_time_window_config


@dataclass(frozen=True)
class ScriptRestore:
    restore_id: str
    db_type: str
    server_id: str
    backup_dir: str
    script: str
    time_window: TimeWindow
    target_container: str = ""
    # A target on another machine: a db_instances server_id whose cmd_access says how to reach it.
    # Empty means the target lives on the source's own host (the in-place drill).
    target_server_id: str = ""
    # Where the backup is placed on the target host before restoring. Only used for a remote
    # target, where the pieces have to be transferred rather than shared through a mount.
    target_backup_dir: str = ""
    # The backup as seen on the SOURCE HOST's own filesystem. ``backup_dir`` is the path inside
    # the database container, which SFTP cannot reach - a transfer needs the host path, and the
    # two are only the same when the backup lives on a bind mount.
    source_backup_host_dir: str = ""
    # The staged directory AS THE DATABASE SEES IT. Not the same string as target_backup_dir when
    # the target is a container: the files are staged on the host and reached through a mount, and
    # the database resolves the path in its own namespace. Empty means the two are the same.
    target_visible_dir: str = ""
    # How long staged files are kept on the target host (seconds; 0 = never delete). See
    # db_ops.backup_restore.config.DEFAULT_CLEANUP_RETENTION.
    cleanup_retention: int = DEFAULT_CLEANUP_RETENTION
    active: bool = True
    # Opt-in: also replay the source instance's server-level metadata around this restore.
    # Absent means the restore behaves exactly as it did before this existed.
    server_metadata: ServerMetadataPlan = field(default_factory=ServerMetadataPlan)
    env: dict[str, str] = field(default_factory=dict)
    # Env vars whose value is a secret-store ref, resolved at run time: {ENV_NAME: SECRET_REF}.
    # A restore of an encrypted backup needs the passphrase, and it must not live in a
    # committed config file. Same mechanism as a backup job's env_secrets.
    env_secrets: dict[str, str] = field(default_factory=dict)
    # Per-entry notify object (db_ops.lib.notify): logging_on_run + alert_on_error.
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def is_remote(self) -> bool:
        return bool(self.target_server_id) and self.target_server_id != self.server_id

    @property
    def target_mode(self) -> str:
        """``docker`` when the target is a container, ``native`` when it is the host itself."""
        return "docker" if self.target_container else "native"

    @property
    def job_code(self) -> str:
        return schedule.restore_job_code(self.restore_id)

    @property
    def label(self) -> str:
        return f"{self.restore_id} ({self.db_type})"


def _read_entries(path: Path | None) -> list[Any] | None:
    if path is None or not path.exists():
        return None
    with path.open("r", encoding="utf-8-sig") as file:
        raw = json.load(file)
    section = raw.get("backup_restore") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return None
    entries = section.get("restores")
    return entries if isinstance(entries, list) else None


def load_script_restores(config_path: str | Path | None = None) -> list[ScriptRestore]:
    """The script-driven subset of ``backup_restore.restores[]``.

    Falls back to the canonical restore config for the same reason the backup loader does: the
    scheduled command runs with ``--config config.json``, which holds app settings and no restore
    entries, so without the fallback the daemon would silently restore nothing.
    """
    path = Path(config_path) if config_path else None
    entries = _read_entries(path)
    if entries is None and (path is None or path.resolve() != DEFAULT_RESTORE_CONFIG_PATH.resolve()):
        entries = _read_entries(DEFAULT_RESTORE_CONFIG_PATH)
    if entries is None:
        return []

    jobs: list[ScriptRestore] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not is_script_restore(entry):
            continue
        restore_id = str(entry.get("restore_id") or "").strip()
        if not restore_id:
            raise ValueError(f"backup_restore.restores[{index}] requires restore_id.")
        if restore_id in seen:
            raise ValueError(f"Duplicate restore_id: {restore_id}.")
        seen.add(restore_id)
        for required in ("server_id", "backup_dir", "script"):
            if not str(entry.get(required) or "").strip():
                raise ValueError(f"{restore_id} requires {required}.")
        target_container = str(entry.get("target_container") or "").strip()
        target_server_id = str(entry.get("target_server_id") or "").strip()
        if not target_container and not target_server_id:
            raise ValueError(
                f"{restore_id} requires target_container (restore into a container on the "
                f"source host) or target_server_id (restore onto another machine)."
            )
        target_backup_dir = str(entry.get("target_backup_dir") or "").strip()
        source_backup_host_dir = str(entry.get("source_backup_host_dir") or "").strip()
        if target_server_id and target_server_id != str(entry["server_id"]).strip():
            if not target_backup_dir:
                raise ValueError(
                    f"{restore_id} restores onto {target_server_id}, so it requires target_backup_dir: "
                    f"the backup has to be transferred there, not shared through a mount."
                )
            if not source_backup_host_dir:
                raise ValueError(
                    f"{restore_id} restores onto another machine, so it requires "
                    f"source_backup_host_dir: backup_dir is a path inside the database container "
                    f"and the transfer reads the source host's filesystem."
                )
        env = entry.get("env") or {}
        if not isinstance(env, dict):
            raise ValueError(f"{restore_id}.env must be an object.")
        env_secrets = entry.get("env_secrets") or {}
        if not isinstance(env_secrets, dict):
            raise ValueError(f"{restore_id}.env_secrets must be an object of {{ENV_NAME: secret_ref}}.")
        jobs.append(ScriptRestore(
            restore_id=restore_id,
            db_type=str(entry.get("db_type") or "").strip().lower(),
            server_id=str(entry["server_id"]).strip(),
            target_container=target_container,
            target_server_id=target_server_id,
            target_backup_dir=target_backup_dir,
            target_visible_dir=str(entry.get('target_visible_dir') or '').strip(),
            source_backup_host_dir=source_backup_host_dir,
            cleanup_retention=parse_cleanup_retention(
                entry, context=f"backup_restore.restores[{index}]"
            ),
            backup_dir=str(entry["backup_dir"]).strip(),
            script=str(entry["script"]).strip(),
            time_window=parse_time_window_config(
                entry, context=f"backup_restore.restores[{index}]"
            ).time_window,
            env_secrets={str(k): str(v) for k, v in env_secrets.items()},
            notify=parse_backup_restore_notify(
                entry, context=f"backup_restore.restores[{index}] ({restore_id})"
            ),
            active=bool(entry.get("active", True)),
            env={str(k): str(v) for k, v in env.items()},
            server_metadata=parse_server_metadata(
                entry.get("server_metadata"),
                label=f"backup_restore.restores[{index}] ({restore_id})",
                for_restore=True,
                db_type=str(entry.get("db_type") or ""),
            ),
        ))
    return jobs


def select_due_script_restores(
    *, jobs: list[ScriptRestore], latest_runs: dict[str, Any], now=None, local_now=None
) -> list[ScriptRestore]:
    return [
        item for item in jobs
        if item.active and schedule.is_due(
            job_code=item.job_code, time_window=item.time_window,
            latest_runs=latest_runs, now=now, local_now=local_now,
        )
    ]


def _restore_secrets(job: ScriptRestore, *, data_dir, key, key_base64) -> dict[str, str]:
    """The decrypted store, read only when the entry actually declares env_secrets - so a
    restore with nothing to decrypt still runs on a node without the passphrase."""
    if not job.env_secrets:
        return {}
    from db_ops.backup_restore.backup import _load_secrets
    return _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64)


def _script_env(job: ScriptRestore, source: BackupTarget,
                *, secrets: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        # The container the backup was taken from, and the one it is being restored into.
        # They are deliberately separate: a restore drill must never write over the source.
        # Empty when the script runs on another machine: the source container does not exist
        # there, so checking for it would fail on a perfectly good restore.
        "SOURCE_CONTAINER": "" if job.is_remote else source.container_name,
        "TARGET_CONTAINER": job.target_container,
        # On a remote target the pieces were transferred here; in place they are the same path.
        "BACKUP_DIR": job.target_backup_dir if job.is_remote else job.backup_dir,
        "RESTORE_ID": job.restore_id,
        # docker: exec into TARGET_CONTAINER. native: run on the host itself (a VM with the
        # engine installed directly), where there is no container to exec into.
        "TARGET_MODE": job.target_mode,
    }
    env.update(job.env)
    # Secrets last: the decryption passphrase reaches the script as an env var, exported ahead
    # of it on the remote side, so it never appears on a command line or in the config file.
    for name, ref in (job.env_secrets or {}).items():
        value = (secrets or {}).get(ref)
        if not value:
            raise ValueError(
                f"{job.restore_id}: env_secrets maps {name} to secret ref '{ref}', which is not "
                f"in the secret store. Add it, or pass --key/--key-base64 so the store can be read."
            )
        env[name] = str(value)
    return env


def _ssh_login(target: BackupTarget, *, data_dir, key, key_base64) -> dict[str, Any]:
    """The login a ``common.cli`` copy command opens its session with: resolved here, handed there.

    ``common`` reads no configuration, so the secret ref and the key name stop at this line.
    """
    from db_ops.common.data_sources import resolve_ssh_key, resolve_ssh_password

    login: dict[str, Any] = {"host": target.host, "port": target.port, "username": target.username}
    key_filename = resolve_ssh_key(target.key_file, data_dir) if target.key_file else None
    if key_filename:
        login["key_file"] = str(key_filename)
    else:
        login["password"] = resolve_ssh_password(
            password=None, password_ref=target.password_ref,
            key=key, key_base64=key_base64, data_dir=data_dir,
        )
    return login


def transfer_backup_to_target(
    job: ScriptRestore,
    *,
    source: BackupTarget,
    target: BackupTarget,
    data_dir=None,
    key=None,
    key_base64=None,
    log=None,
    prune: bool = True,
    point_in_time: str = "",
) -> dict[str, Any]:
    """Copy the backup from the source host to the target host before the restore runs.

    This is the copy-backup phase doing real work. In the in-place drill the same directory is
    visible to both ends through a mount and nothing moves; once the target is another machine
    the pieces genuinely have to travel, which is the whole difference between a drill and a
    recovery onto new hardware.

    Two ``common.cli`` commands since 0.23.0, each a step of its own: ``backup-chain`` says which
    parts of the source directory the restore needs (PostgreSQL's newest chain, RMAN's own answer
    for Oracle, everything for SQL Server and for a point in time), ``copy-backup-dir`` moves them
    as one tar stream and mirrors the source. The app resolves the two logins; ``common`` opens the
    sessions. Their progress streams on stderr as it happens (``log`` is kept for callers and no
    longer used).

    ``prune=False`` leaves the retention cleanup to the caller: ``restore_by_id`` runs it after the
    restored databases are verified (its DELETE step), as the engine path does.
    """
    del log
    source_login = _ssh_login(source, data_dir=data_dir, key=key, key_base64=key_base64)
    target_login = _ssh_login(target, data_dir=data_dir, key=key, key_base64=key_base64)
    chain = common_cli.run("backup-chain", {
        "db_type": job.db_type, "source": source_login,
        "source_dir": job.source_backup_host_dir, "backup_dir": job.backup_dir,
        "container": source.container_name, "point_in_time": point_in_time,
    }, stream_stderr=True)
    out = dict(common_cli.run("copy-backup-dir", {
        "source": source_login, "source_dir": job.source_backup_host_dir,
        "target": target_login, "target_dir": job.target_backup_dir,
        "include": chain.get("include") or [], "make_readable": True, "open_for_engine": True,
    }, stream_stderr=True))
    out["include"] = list(chain.get("include") or [])
    pruned = (prune_staged_backups(job, target=target, data_dir=data_dir, key=key,
                                   key_base64=key_base64)
              if prune else {"pruned": 0, "retention_seconds": int(job.cleanup_retention or 0),
                             "skipped": "left to the caller"})
    out["pruned"] = pruned.get("pruned", 0)
    out["retention_seconds"] = pruned.get("retention_seconds", 0)
    return out


def prune_staged_backups(
    job: ScriptRestore,
    *,
    target: BackupTarget,
    data_dir=None,
    key=None,
    key_base64=None,
    log=None,
) -> dict[str, Any]:
    """The DELETE step of a remote restore: ``common.cli prune-staged-backups`` on its staging.

    After the verify, not inside the copy: a drill that failed keeps what it staged for the next
    attempt - the rule the engine path has followed since 2026-09-11 ("the retention cleanup must
    not prune the backups when the drill that proves them restorable has just failed").
    """
    del log
    return common_cli.run("prune-staged-backups", {
        "target": _ssh_login(target, data_dir=data_dir, key=key, key_base64=key_base64),
        "target_dir": job.target_backup_dir,
        "cleanup_retention": int(job.cleanup_retention or 0),
    }, stream_stderr=True)


# `_announce` is `db_ops.backup_restore.events.announce` since 2026-08-16 — it was four
# near-copies in this app, one of which had already drifted its parameter names.


def replay_metadata_phase(
    job: ScriptRestore,
    *,
    phase: str,
    data_dir: str | Path | None = None,
    on_phase: Callable[..., None] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Adapter onto the shared :func:`server_metadata.replay_phase`.

    The decision is shared with the engine path and lives there; this only says how a
    ``ScriptRestore`` answers its questions - a container-to-container restore names a container,
    where the engine path names an ip.
    """
    return replay_phase(
        job.server_metadata,
        phase=phase,
        label=job.label,
        source_server_id=job.server_id,
        target_server_id=job.target_server_id,
        target_container=job.target_container,
        data_dir=data_dir,
        announce=on_phase,
    )


def run_script_restore(
    job: ScriptRestore,
    *,
    data_dir: str | Path | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    on_phase: Callable[..., None] | None = None,
) -> tuple[str, int | None, str, str]:
    """Run the restore. Returns (status, exit_code, out, err).

    In place, the script runs on the source's own host and execs into the target container.
    Onto another machine, the backup is transferred first and the script runs on the *target*
    host - so the engine commands, the data directory and the restored instance are all local
    to where the database will actually live.

    ``on_phase(phase, message, extra)`` is called at the copy boundaries when there is a copy to
    report, so a caller can announce them. It is optional and never affects the restore: a
    caller that does not pass it (or an emit that fails) must not turn a good restore into a
    failed one, which is why the copy runs outside the notification, not through it.
    """
    source = resolve_ssh_target(job.server_id, label=job.label, data_dir=data_dir)

    if job.is_remote:
        target = resolve_ssh_target(
            job.target_server_id, label=job.label, data_dir=data_dir, require_container=False,
        )
        if target.host == source.host and job.target_container == source.container_name:
            raise ValueError(
                f"{job.label}: the target is the source container ({source.container_name}). "
                f"A restore must not overwrite the database it is restoring from."
            )
        # Only a remote drill copies anything. An in-place one shares the directory through a
        # mount, so announcing a copy there would report work that never happened.
        announce(on_phase, "COPY_START",
                  f"Restore {job.label}: copy started {job.server_id} -> {job.target_server_id}.")
        transfer = transfer_backup_to_target(
            job, source=source, target=target,
            data_dir=data_dir, key=key, key_base64=key_base64,
        )
        announce(on_phase, "COPY_DONE",
                  f"Restore {job.label}: copy finished - {transfer['copied']} piece(s), "
                  f"{transfer['bytes_copied']} bytes, {transfer['skipped']} already present.",
                  {"copied": transfer["copied"], "skipped": transfer["skipped"],
                   "bytes_copied": transfer["bytes_copied"],
                   "pruned": transfer.get("pruned", 0)})
        prefix = (
            f"PHASE=copy-backup transferred {job.server_id} -> {job.target_server_id} "
            f"copied={transfer['copied']} skipped={transfer['skipped']} "
            f"bytes={transfer['bytes_copied']} pruned={transfer.get('pruned', 0)} "
            f"retention={transfer.get('retention_seconds', 0)}s\n"
        )
        run_on = target
    else:
        if not job.target_container:
            raise ValueError(
                f"{job.label}: an in-place restore needs target_container."
            )
        if job.target_container == source.container_name:
            raise ValueError(
                f"{job.label}: target_container is the source container ({source.container_name}). "
                f"A restore drill must not overwrite the database it is validating."
            )
        prefix = ""
        run_on = source

    # Before the databases are restored, or every restored database's users are orphaned: their
    # SIDs point at logins that do not exist on the target yet.
    pre_line, _pre = replay_metadata_phase(
        job, phase=instance_bundle.PRE_DATABASE, data_dir=data_dir, on_phase=on_phase,
    )

    script_text = _script_path(job.script, label=job.label).read_text(encoding="utf-8")
    out, err, exit_code = execute_over_ssh(
        script_text=script_text, env=_script_env(job, source, secrets=_restore_secrets(
            job, data_dir=data_dir, key=key, key_base64=key_base64)), target=run_on,
        timeout=job.time_window.timeout, data_dir=data_dir, key=key, key_base64=key_base64,
    )
    out = prefix + pre_line + out
    completed = "RESULT=ok" in out
    status = "done" if exit_code == 0 and completed else "error"

    # Only after a restore that worked. Agent job steps name databases that have to exist, so
    # replaying them onto a failed restore would create jobs pointing at databases that are not
    # there - and would do it while the operator is reading a failure message about something
    # else entirely.
    if status == "done":
        post_line, _post = replay_metadata_phase(
            job, phase=instance_bundle.POST_DATABASE, data_dir=data_dir, on_phase=on_phase,
        )
        out += post_line
    return status, exit_code, out, err
