"""Scheduled backup execution for the backup_restore app.

The app could restore but never take a backup, so RMAN was the one part of the Oracle
recovery story still done by hand. ``backup`` closes that: it scans the configured backup
jobs, runs the ones that are due, and records every run in SQLite.

Scheduling follows the same contract as sql_tasks (and the daemon, and metrics): a job has a
``time_window``, and the *last run of any status* drives the next one, so a failing job backs
off on ``retry_interval`` instead of hammering the database every tick. That is why each run
is written to ``job_runs`` before and after execution - the row is not just a log, it is the
state the next due check reads.

What actually runs is a shell script asset (``assets/backup/oracle/*.sh``) shipped over SSH to
the container host and executed there, mirroring how the metrics docker collector reaches the
same hosts. Keeping the RMAN logic in a visible, editable ``.sh`` (rather than embedded in
Python) means a DBA can read and adjust the backup commands without touching the scheduler.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db_ops.lib import common_cli
from db_ops.backup_restore.config import DEFAULT_RESTORE_CONFIG_PATH, parse_backup_restore_notify
from db_ops.backup_restore.events import emit_backup_restore_event, stdout_excerpt
from db_ops.backup_restore import schedule
from db_ops.backup_restore.server_metadata import (
    export_for_backup,
    instance_bundle_dir,
    parse_server_metadata,
    summarize as summarize_server_metadata,
)
from db_ops.common import data_sources
from db_ops.common.data_sources import resolve_ssh_key, resolve_ssh_password
from db_ops.common.ssh import open_ssh_client
from db_ops.lib.ssh_errors import SshError
from db_ops.lib.notify import NotifyConfig
from db_ops.lib.time_window import TimeWindow, is_time_window_open, job_due, parse_time_window_config
from db_ops.config import DbOpsConfig
from db_ops.db.job_runs import JobRun
from db_ops.db.store import DbOpsStore, utc_now_text
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.paths import resolve_tool_path


# One vocabulary for the operator, three for the engines. `--backup-type full` means the same
# thing everywhere; what reaches the script is whatever that engine's own script understands
# (RMAN counts levels, pg_basebackup names them, SQL Server has native terms). Without this the
# operator has to remember that Oracle wants 0/1 and PostgreSQL wants full/incr - which is how
# a "full" backup ends up being taken as an incremental.
BACKUP_TYPES = ("full", "diff", "log")


def backup_level_for(db_type: str, backup_type: str) -> str:
    """The engine's own name for ``backup_type``. Raises when that engine has no such level.

    Re-exported from :mod:`db_ops.lib.backup_level`, where it now lives: it is engine knowledge,
    not app configuration, and the CLI, the spec and this app must not be able to disagree about
    what "full" means for Oracle. Kept importable from here because runbooks and tests name it.
    """
    from db_ops.lib.backup_level import backup_level_for as _level_for

    return _level_for(db_type, backup_type)


@dataclass(frozen=True)
class BackupJob:
    backup_id: str
    job: str
    db_type: str
    server_id: str
    script: str
    backup_dir: str
    retention_days: int | None
    time_window: TimeWindow
    active: bool = True
    env: dict[str, str] = field(default_factory=dict)
    # Env vars whose value is a secret-store ref, resolved at run time: {ENV_NAME: SECRET_REF}.
    # A backup script that needs a database password or an encryption passphrase must get it
    # this way — `env` is plain config and lives in a committed file.
    env_secrets: dict[str, str] = field(default_factory=dict)
    # Opt-in: also export this instance's server-level metadata (logins, Agent jobs, linked
    # servers, ...) next to the backup. An entry without the block is unchanged in every
    # way - see db_ops.backup_restore.server_metadata.
    server_metadata: Any = None
    # Per-job notify object (db_ops.lib.notify), inherited from the backup entry and
    # overridable rule by rule on the sub-job.
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @property
    def job_code(self) -> str:
        return schedule.backup_job_code(self.backup_id, self.job)

    @property
    def label(self) -> str:
        return f"{self.backup_id}/{self.job}"


def _read_backup_entries(path: Path | None) -> list[Any] | None:
    """``backup_restore.backups`` from one config file, or None when it declares none."""
    if path is None or not path.exists():
        return None
    with path.open("r", encoding="utf-8-sig") as file:
        raw = json.load(file)
    section = raw.get("backup_restore") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return None
    entries = section.get("backups")
    if entries is None:
        return None
    if not isinstance(entries, list):
        raise ValueError("backup_restore.backups must be a JSON array.")
    return entries


def load_backup_jobs(config_path: str | Path | None = None) -> list[BackupJob]:
    """Read ``backup_restore.backups[]`` into one BackupJob per (entry, job).

    Falls back to the canonical ``data/restore_config.json`` the same way
    ``load_restore_configs`` does. The scheduled command runs with ``--config config.json``
    (app settings live there, backup entries do not), so without this fallback the daemon
    would find zero jobs and back up nothing without ever reporting an error.
    """
    path = Path(config_path) if config_path else None
    entries = _read_backup_entries(path)
    if entries is None and (path is None or path.resolve() != DEFAULT_RESTORE_CONFIG_PATH.resolve()):
        entries = _read_backup_entries(DEFAULT_RESTORE_CONFIG_PATH)
    if entries is None:
        return []

    jobs: list[BackupJob] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"backup_restore.backups[{index}] must be an object.")
        backup_id = str(entry.get("backup_id") or "").strip()
        if not backup_id:
            raise ValueError(f"backup_restore.backups[{index}] requires backup_id.")
        entry_active = bool(entry.get("active", True))
        db_type = str(entry.get("db_type") or "").strip().lower()
        server_id = str(entry.get("server_id") or "").strip()
        if not server_id:
            raise ValueError(f"backup_restore.backups[{index}] ({backup_id}) requires server_id.")
        backup_dir = str(entry.get("backup_dir") or "").strip()
        entry_notify = parse_backup_restore_notify(
            entry, context=f"backup_restore.backups[{index}] ({backup_id})"
        )
        raw_jobs = entry.get("jobs") or []
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise ValueError(f"backup_restore.backups[{index}] ({backup_id}) requires a non-empty jobs array.")
        for job_index, raw_job in enumerate(raw_jobs):
            if not isinstance(raw_job, dict):
                raise ValueError(f"{backup_id}.jobs[{job_index}] must be an object.")
            job_name = str(raw_job.get("job") or "").strip()
            if not job_name:
                raise ValueError(f"{backup_id}.jobs[{job_index}] requires job.")
            script = str(raw_job.get("script") or "").strip()
            if not script:
                raise ValueError(f"{backup_id}.{job_name} requires script.")
            job_backup_dir = str(raw_job.get("backup_dir") or backup_dir).strip()
            if not job_backup_dir:
                raise ValueError(f"{backup_id}.{job_name} requires backup_dir (on the job or the entry).")
            context = f"backup_restore.backups[{index}].jobs[{job_index}]"
            parsed = parse_time_window_config(raw_job, context=context)
            retention = raw_job.get("retention_days")
            job_env = raw_job.get("env") or {}
            if not isinstance(job_env, dict):
                raise ValueError(f"{backup_id}.{job_name}.env must be an object.")
            job_env_secrets = raw_job.get("env_secrets") or entry.get("env_secrets") or {}
            if not isinstance(job_env_secrets, dict):
                raise ValueError(f"{backup_id}.{job_name}.env_secrets must be an object "
                                 f"of {{ENV_NAME: secret_ref}}.")
            item = BackupJob(
                backup_id=backup_id,
                job=job_name,
                db_type=db_type,
                server_id=server_id,
                script=script,
                backup_dir=job_backup_dir,
                retention_days=int(retention) if retention not in (None, "") else None,
                time_window=parsed.time_window,
                active=entry_active and bool(raw_job.get("active", True)),
                env={str(k): str(v) for k, v in job_env.items()},
                env_secrets={str(k): str(v) for k, v in job_env_secrets.items()},
                server_metadata=parse_server_metadata(
                    raw_job.get("server_metadata", entry.get("server_metadata")),
                    label=f"{backup_id}.{job_name}",
                    db_type=str(entry.get("db_type") or ""),
                ),
                # The entry's block is the default; the sub-job's overrides it rule by rule.
                # This is why a full backup can report every run while the log/WAL job beside
                # it — running every 15 minutes — stays quiet.
                notify=parse_backup_restore_notify(raw_job, context=context, inherit=entry_notify),
            )
            if item.job_code in seen:
                raise ValueError(f"Duplicate backup job: {item.label}. backup_id + job must be unique.")
            seen.add(item.job_code)
            jobs.append(item)
    return jobs


def select_due_backup_jobs(
    *,
    jobs: list[BackupJob],
    latest_runs: dict[str, Any],
    now: datetime | None = None,
    local_now: datetime | None = None,
) -> list[BackupJob]:
    """The due subset. The rule itself lives in schedule.is_due, shared with restore entries."""
    return [
        item for item in jobs
        if item.active and schedule.is_due(
            job_code=item.job_code, time_window=item.time_window,
            latest_runs=latest_runs, now=now, local_now=local_now,
        )
    ]


# --------------------------------------------------------------------------- #
# Target resolution (SSH + container come from db_instances.json, not restated here)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BackupTarget:
    server_id: str
    host: str
    port: int
    username: str
    container_name: str
    key_file: str | None
    password_ref: str | None
    #: What runs on that machine, from db_instances.json. Carried because the spec below has to
    #: say `runtime` and a Windows SQL Server is neither a container nor a Linux host - it was
    #: hardcoded to "linux", which is right for every entry that existed before Windows had one.
    platform: str = "linux"
    #: How to reach it: ``ssh`` or ``winrm``. Independent of ``platform`` - a Windows host may run
    #: an OpenSSH server, and most of the Windows estate here is reached over WinRM instead. This
    #: used to be absent because the resolver refused anything but SSH; see its docstring.
    access: str = "ssh"
    #: WinRM only: 5986 with TLS, 5985 without.
    ssl: bool = False


def resolve_backup_target(job: BackupJob, *, data_dir: str | Path | None = None) -> BackupTarget:
    return resolve_ssh_target(job.server_id, label=job.label, data_dir=data_dir)


def resolve_ssh_target(
    server_id: str, *, label: str, data_dir: str | Path | None = None, require_container: bool = True
) -> BackupTarget:
    """Find a server's command endpoint and container in db_instances.json.

    The endpoint and container name are already declared there for the metrics collectors;
    duplicating them in restore_config.json would mean two places to update when a lab host
    moves, so the backup config only names the ``server_id``.

    **SSH or WinRM.** This refused anything but SSH until 2026-08-10, which read as a statement
    about what the transport could do and was really a statement about what nobody had needed yet:
    ``hostcmd.run_script`` has spoken WinRM the whole time, delegating to the same
    :mod:`db_ops.common.remote_exec` the metrics collectors use against these hosts every cycle.
    The cost of the restriction was that a Windows SQL Server reachable only over WinRM could not
    be backed up at all - it failed with ``needs cmd_access.method=ssh, got winrm`` at config
    resolution, before anything was attempted. 192.0.2.248 is such a host, and the alternative
    was installing an OpenSSH server on it to satisfy a check rather than a limitation.
    """
    instance = None
    for item in data_sources.load_db_instances(data_dir):
        if str(item.get("server_id") or "").strip() == server_id:
            instance = item
            break
    if instance is None:
        raise ValueError(f"{label}: server_id not found in db_instances.json: {server_id}")

    cmd_access = instance.get("cmd_access") or {}
    if not bool(cmd_access.get("enabled", False)):
        raise ValueError(f"{label}: cmd_access is not enabled for {server_id}.")
    method = str(cmd_access.get("method") or "").strip().lower()
    if method not in SUPPORTED_BACKUP_ACCESS:
        raise ValueError(
            f"{label}: needs cmd_access.method to be one of "
            f"{', '.join(sorted(SUPPORTED_BACKUP_ACCESS))}; got {method or '<missing>'}. "
            "`local` is refused on purpose: it would back up the db_ops container, under the "
            "target's name."
        )
    container_name = str(instance.get("container_name") or "").strip()
    platform = str(instance.get("platform") or "").strip().lower() or "linux"
    # A Windows host runs the engine natively; there is no container to name and demanding one
    # would lock the backup out of every SQL Server on Windows.
    if platform == "windows":
        require_container = False
    # A restore *target* host needs no container_name of its own: the entry being restored names
    # the container (or restores natively onto the host). Only a source must say which container
    # its backup came from.
    if not container_name and require_container:
        raise ValueError(f"{label}: {server_id} has no container_name.")

    host = str(cmd_access.get("host") or instance.get("ip") or "").strip()
    if not host:
        raise ValueError(f"{label}: no host/ip for {server_id}.")
    credential_name = str(cmd_access.get("credential_name") or "").strip()
    username, password_ref = _lookup_remote_credential(credential_name, data_dir=data_dir)
    ssl = bool(cmd_access.get("ssl", False))
    return BackupTarget(
        server_id=server_id,
        host=host,
        port=int(cmd_access.get("port") or _default_access_port(method, ssl=ssl)),
        username=username,
        container_name=container_name,
        # A key is an SSH idea; carrying one on a WinRM target would silently suppress the
        # password the transport actually needs (see spec_builder._ssh_password).
        key_file=(str(cmd_access.get("key_file") or "").strip() or None) if method == "ssh" else None,
        password_ref=password_ref,
        platform=platform,
        access=method,
        ssl=ssl,
    )


#: Transports a backup can be run over. Both are already implemented in
#: :func:`db_ops.common.hostcmd.run_script`; this set is what the config resolver will accept.
SUPPORTED_BACKUP_ACCESS = frozenset({"ssh", "winrm"})


def _default_access_port(method: str, *, ssl: bool) -> int:
    """The port to use when ``cmd_access`` does not state one.

    Defaulting to 22 for every method was harmless while only SSH was allowed and is not now: a
    WinRM target with no explicit port would try to speak WinRM to an SSH port and report the host
    unreachable, which is the least informative way to be wrong.
    """
    if method == "winrm":
        return 5986 if ssl else 5985
    return 22


def _lookup_remote_credential(credential_name: str, *, data_dir: str | Path | None = None) -> tuple[str, str | None]:
    if not credential_name:
        raise ValueError("cmd_access.credential_name is required for backup targets.")
    for group in data_sources.load_remote_credentials(data_dir):
        for credential in group.get("credentials") or []:
            if str(credential.get("credential_name") or "").strip() == credential_name:
                username = str(credential.get("username") or "").strip()
                if not username:
                    raise ValueError(f"Remote credential {credential_name} has no username.")
                return username, str(credential.get("password_ref") or "").strip() or None
    raise ValueError(f"Remote credential not found in users.json: {credential_name}")


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
@dataclass
class BackupRunResult:
    job: BackupJob
    status: str
    exit_code: int | None
    duration_ms: int
    stdout: str
    stderr: str
    error_text: str | None = None


def _script_path(script: str, *, label: str) -> Path:
    path = Path(script)
    candidate = resolve_tool_path(path)
    if not candidate.is_file():
        raise ValueError(f"{label}: script not found: {candidate}")
    return candidate


def execute_over_ssh(
    *,
    script_text: str,
    env: dict[str, str],
    target: BackupTarget,
    timeout: int | None = None,
    data_dir: str | Path | None = None,
    key: str | None = None,
    key_base64: str | None = None,
) -> tuple[str, str, int]:
    """Run a shell script on the container host, with ``env`` exported ahead of it.

    Shared by the backup jobs and the script-driven restores so both reach a host the same way.
    The script is fed to ``bash -s`` on stdin, which is why any ``docker exec`` inside it must
    close its own stdin - otherwise the container reads the rest of the script as its input and
    the shell silently runs out of work (exit 0, no output, nothing done).
    """
    prelude = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in sorted(env.items()))
    payload = prelude + script_text

    key_filename = resolve_ssh_key(target.key_file, data_dir) if target.key_file else None
    password = None
    if not key_filename:
        password = resolve_ssh_password(
            password=None, password_ref=target.password_ref,
            key=key, key_base64=key_base64, data_dir=data_dir,
        )

    client = open_ssh_client(
        target.host, target.username, port=target.port,
        password=password, key_filename=key_filename,
    )
    try:
        stdin, stdout, stderr = client.exec_command("bash -s", timeout=timeout or None)
        stdin.write(payload)
        stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", errors="replace")
    finally:
        client.close()
    return out, err, exit_code


def _load_secrets(
    *, data_dir: str | Path | None, key: str | None, key_base64: str | None
) -> dict[str, str]:
    """The decrypted secret store, for jobs that declare ``env_secrets``."""
    from db_ops.lib.secret_text import resolve_cli_key

    secret_key: str | None = None
    try:
        secret_key = resolve_cli_key(key, key_base64)
    except Exception:  # noqa: BLE001 - no/invalid key on argv; the env may still carry one.
        secret_key = None
    secret_key = secret_key or os.environ.get("DB_OPS_SECRET_KEY") or None
    try:
        return data_sources.load_secret_text(data_dir, key=secret_key) if secret_key \
            else data_sources.load_secret_text(data_dir)
    except Exception as exc:  # noqa: BLE001 - report which job needed it, in the caller.
        raise ValueError(f"Could not read the secret store for a job that needs it: {exc}") from exc


def execute_backup_job(
    job: BackupJob,
    *,
    target: BackupTarget,
    data_dir: str | Path | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    env_overrides: dict[str, str] | None = None,
    backup_type: str | None = None,
) -> BackupRunResult:
    """Ship the job's script to the container host over SSH and run it there.

    The running itself moved to :mod:`db_ops.common.backup`; what is left here is the app's half —
    read the secret store, build the spec, and translate the answer back into a ``BackupRunResult``
    the scheduler and the store rows already speak. The receipt rule, the level translation and the
    transport all live below now, so a one-off run against an unregistered machine gets exactly the
    behaviour the scheduled run gets.
    """
    from db_ops.backup_restore.spec_builder import backup_request_from_job

    # The store is read when *the spec* will need a secret — which is more than when the job
    # declares env_secrets. The SSH password is a secret too, and since the transport moved to
    # db_ops.common.backup the target's password_ref is resolved here into spec.host.password
    # instead of lazily inside execute_over_ssh. A password-auth target on a job with no
    # env_secrets therefore built its spec against an empty store and failed every single run with
    # "password_ref ... is not in the secret store" — every ACME_* job at once, while the key-auth
    # CLOUD_* ones kept working because a key needs nothing decrypted.
    needs_ssh_password = not target.key_file and bool(target.password_ref)
    secrets: dict[str, str] = {}
    if job.env_secrets or needs_ssh_password:
        # Still conditional: a key-auth job that decrypts nothing keeps working on a node where
        # the passphrase is not available.
        secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64)
    request = backup_request_from_job(
        job, target=target, secrets=secrets, level=str(backup_type or ""),
        env_overrides=env_overrides, data_dir=data_dir,
    )
    # `run_allowing_failure`, not `run`: a backup that fails is an outcome this app records, not
    # an exception it stops on. The CLI answers `success: false` with the exit code, stderr and
    # error text still in `data` — which is exactly what the job_runs row below is made of.
    _ok, result, error = common_cli.run_allowing_failure("backup-database", request)
    return BackupRunResult(
        job=job, status=result.get("status") or "error", exit_code=result.get("exit_code"),
        duration_ms=result.get("duration_ms"), stdout=result.get("stdout"), stderr=result.get("stderr"),
        error_text=result.get("error") or error or None,
    )


def run_backup(
    *,
    app_config: DbOpsConfig,
    config_path: str | Path,
    logger: Any = None,
    backup_id: str | None = None,
    job_name: str | None = None,
    data_dir: str | Path | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    env_overrides: dict[str, str] | None = None,
    backup_type: str | None = None,
) -> dict[str, Any]:
    """Scan configured backup jobs, run the due ones, and record each run.

    ``backup_type`` (``full``/``diff``/``log``) forces the level for this run instead of
    letting the script derive it. It is translated per engine, so one word works for all of
    them; the scheduled config passes nothing and the script keeps deciding for itself.
    """
    jobs = load_backup_jobs(config_path)
    if backup_id:
        jobs = [item for item in jobs if item.backup_id == backup_id]
        if not jobs:
            raise ValueError(f"No backup entry found with backup_id={backup_id}.")
    if job_name:
        jobs = [item for item in jobs if item.job == job_name]
        if not jobs:
            raise ValueError(f"No backup job named {job_name}.")

    store = DbOpsStore.from_config(app_config)
    # Close and report runs abandoned at RUNNING past their timeout — a job whose process was
    # killed never got to report its own failure. See schedule.reap_stale_runs.
    stale = schedule.reap_stale_runs(
        store=store, app_config=app_config, logger=logger,
        timeouts={item.job_code: item.time_window.timeout for item in jobs},
        notify={item.job_code: item.notify for item in jobs},
    )
    latest_runs = store.fetch_latest_job_runs_by_job_code()
    # --force runs every selected job regardless of schedule; the due check is the normal path.
    # --force skips the schedule, never a run that is still in flight: two backups of the
    # same database at once is what the RUNNING row exists to prevent.
    due = ([j for j in jobs if j.active and not schedule.is_running(j.job_code, latest_runs)]
           if force else select_due_backup_jobs(jobs=jobs, latest_runs=latest_runs))

    summary: dict[str, Any] = {
        "configured": len(jobs),
        "due": len(due),
        "timed_out": stale,
        "ran": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": len(jobs) - len(due),
        "dry_run": bool(dry_run),
        "jobs": [],
    }

    for item in due:
        if dry_run:
            summary["jobs"].append({"job": item.label, "status": "dry-run"})
            continue
        summary["ran"] += 1
        started_at = utc_now_text()
        log_id = store.insert_job_run(
            JobRun(
                job_code=item.job_code,
                level="logging",
                status="RUNNING",
                message=f"Backup {item.label} started.",
                started_at=started_at,
                host_name=socket.gethostname(),
                metadata={"backup_id": item.backup_id, "job": item.job, "server_id": item.server_id},
            )
        )
        emit_backup_restore_event(
            app_config=app_config, command="backup", phase="START", level="logging",
            message=f"{item.label} started.", logger=logger, started_at=started_at,
            metadata={"backup_id": item.backup_id, "job": item.job, "server_id": item.server_id},
            notify=item.notify,
        )
        try:
            target = resolve_backup_target(item, data_dir=data_dir)
            # backup_type travels as a word, not as a translated BACKUP_LEVEL: the same word maps
            # to a different level per engine (and a --backup-id can select jobs of more than one),
            # so the translation belongs where the engine is known — once, in the spec.
            result = execute_backup_job(
                item, target=target, data_dir=data_dir, key=key, key_base64=key_base64,
                env_overrides=dict(env_overrides or {}), backup_type=backup_type,
            )
        except (ValueError, SshError, OSError) as exc:
            result = BackupRunResult(
                job=item, status="error", exit_code=None, duration_ms=0,
                stdout="", stderr=str(exc), error_text=str(exc),
            )

        # Only after the data backup succeeded, and never able to change its verdict: a metadata
        # export that could not read sys.credentials must not turn a good backup into a failed
        # one, which is why export_for_backup returns its failure instead of raising. Exporting
        # after a FAILED backup would be worse than useless - it would pair a fresh bundle with
        # no backup to restore it beside.
        server_metadata_result = None
        if result.status == "done":
            server_metadata_result = export_for_backup(
                item.server_metadata,
                server_id=item.server_id,
                bundle_dir=instance_bundle_dir(item.server_id),
                data_dir=data_dir,
            )

        finished_at = utc_now_text()
        level = "logging" if result.status == "done" else "error"
        message = (
            f"Backup {item.label} finished: {result.status}"
            + (f" (exit {result.exit_code})" if result.exit_code is not None else "")
        )
        metadata = {
            "backup_id": item.backup_id,
            "job": item.job,
            "server_id": item.server_id,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "stdout_tail": stdout_excerpt(result.stdout),
        }
        if server_metadata_result is not None:
            metadata["server_metadata"] = summarize_server_metadata(server_metadata_result)
            # Named in the message because the alternative is an operator having to open the run
            # row to discover the bundle beside tonight's backup is yesterday's.
            if not server_metadata_result.get("ok", False):
                message += " (server metadata export FAILED - see metadata.server_metadata)"
        store.update_job_run(
            log_id=log_id,
            level=level,
            status=result.status.upper(),
            message=message,
            finished_at=finished_at,
            duration_ms=result.duration_ms,
            error_text=result.error_text,
            metadata=metadata,
        )
        emit_backup_restore_event(
            app_config=app_config, command="backup",
            phase="END" if result.status == "done" else "ERROR",
            level=level, message=message, logger=logger,
            started_at=started_at, finished_at=finished_at,
            duration_ms=result.duration_ms, error_text=result.error_text, metadata=metadata,
            notify=item.notify,
        )
        if result.status == "done":
            summary["succeeded"] += 1
        else:
            summary["failed"] += 1
        summary["jobs"].append({
            "job": item.label, "status": result.status,
            "exit_code": result.exit_code, "duration_ms": result.duration_ms,
        })

    return summary
