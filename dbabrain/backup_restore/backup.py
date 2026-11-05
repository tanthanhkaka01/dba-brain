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

from dbabrain.transport import common_cli
from dbabrain.backup_restore.config import DEFAULT_RESTORE_CONFIG_PATH, parse_backup_restore_notify
from dbabrain.backup_restore.events import emit_backup_restore_event, stdout_excerpt
from dbabrain.backup_restore import schedule
from dbabrain.backup_restore.server_metadata import (
    export_for_backup,
    instance_bundle_dir,
    parse_server_metadata,
    summarize as summarize_server_metadata,
)
from dbabrain.lib import data_sources, errors
from dbabrain.lib.data_sources import resolve_ssh_key, resolve_ssh_password
from dbabrain.lib.ssh_errors import SshError
from dbabrain.lib.notify import NotifyConfig
from dbabrain.lib import cleanup_retention
from dbabrain.lib import integrity
from dbabrain.db import lockdown_store
from dbabrain.lib.backup_run import RUN_TAG_ENV, run_tag
from dbabrain.lib import offload as offload_rules
from dbabrain.lib import backup_scope
from dbabrain.lib.version import __version__
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib.time_window import TimeWindow, is_time_window_open, job_due, parse_time_window_config
from dbabrain.lib.config import DbaBrainConfig
from dbabrain.db.job_runs import JobRun
from dbabrain.db.store import DbaBrainStore, RunAlreadyClaimed, utc_now_text
from dbabrain.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module
from dbabrain.lib.paths import resolve_tool_path


# One vocabulary for the operator, three for the engines. `--backup-type full` means the same
# thing everywhere; what reaches the script is whatever that engine's own script understands
# (RMAN counts levels, pg_basebackup names them, SQL Server has native terms). Without this the
# operator has to remember that Oracle wants 0/1 and PostgreSQL wants full/incr - which is how
# a "full" backup ends up being taken as an incremental.
BACKUP_TYPES = ("full", "diff", "log")


def backup_level_for(db_type: str, backup_type: str) -> str:
    """The engine's own name for ``backup_type``. Raises when that engine has no such level.

    Re-exported from :mod:`dbabrain.lib.backup_level`, where it now lives: it is engine knowledge,
    not app configuration, and the CLI, the spec and this app must not be able to disagree about
    what "full" means for Oracle. Kept importable from here because runbooks and tests name it.
    """
    from dbabrain.lib.backup_level import backup_level_for as _level_for

    return _level_for(db_type, backup_type)


@dataclass(frozen=True)
class BackupJob:
    backup_id: str
    job: str
    db_type: str
    server_id: str
    script: str
    backup_dir: str
    #: How long the directory this job writes to keeps its files, in seconds. The one retention
    #: field, shared with the restore side - see dbabrain.lib.cleanup_retention. Not defaulted:
    #: every job states it, and a default here would reintroduce the silent fallback just removed.
    cleanup_retention: int
    time_window: TimeWindow
    active: bool = True
    env: dict[str, str] = field(default_factory=dict)
    # Env vars whose value is a secret-store ref, resolved at run time: {ENV_NAME: SECRET_REF}.
    # A backup script that needs a database password or an encryption passphrase must get it
    # this way — `env` is plain config and lives in a committed file.
    env_secrets: dict[str, str] = field(default_factory=dict)
    # Opt-in: also export this instance's server-level metadata (logins, Agent jobs, linked
    # servers, ...) next to the backup. An entry without the block is unchanged in every
    # way - see dbabrain.backup_restore.server_metadata.
    server_metadata: Any = None
    # Per-job notify object (dbabrain.lib.notify), inherited from the backup entry and
    # overridable rule by rule on the sub-job.
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    # Where the run's files go after it (0.34.0): the repository copy, its scan, the delete at the
    # source - dbabrain.lib.offload. ``None`` only for an entry upgrade-config has not reached.
    offload: Any = None
    # Which databases the job takes, as a list (0.34.0, dbabrain.lib.backup_scope) - handed to the
    # script by the app. ``None`` only for a job upgrade-config has not reached.
    scope: Any = None
    # Older files of the entry re-hashed after each run (1.218, lib.integrity.scrub_files_per_run).
    scrub_files_per_run: int = integrity.SCRUB_FILES_PER_RUN
    # The windows per file type (1.227); ``cleanup_retention`` above is their longest, for the
    # readers that apply one number. ``None`` for a job built without the loader.
    retention: Any = None

    @property
    def retention_days(self) -> int | None:
        """``cleanup_retention`` in whole days, for the readers that only speak days.

        Derived, never configured: the backup scripts take ``RETENTION_DAYS`` in their environment,
        and it is not a second setting - it is this one, at the edge where it leaves Python.

        **A window under a day is one day here**, the smallest a day-speaking reader can state:
        never a 0 it would read as "delete everything", and no longer nothing at all, which left
        each script on its own default of fourteen - a lab job declaring ``cleanup_retention:
        7200`` kept every backup it took, 77 GB on one host in 31 hours of an hourly cycle (the
        0.26.0 soak, 2026-10-02). RMAN is such a reader: its recovery window is whole days, so an
        Oracle job with a two-hour window keeps one day. The scripts that can do better are also
        handed :attr:`retention_seconds`. ``None`` for 0, which is "no age gate" and not a window.
        """
        if self.cleanup_retention <= 0:
            return None
        return max(1, int(cleanup_retention.as_days(self.cleanup_retention)))

    @property
    def retention_seconds(self) -> int | None:
        """The window exactly, when it is under a day; ``None`` otherwise.

        Handed to the script as ``RETENTION_SECONDS`` beside ``RETENTION_DAYS``, and read by the
        ones whose engine can apply it: SQL Server (file age, below the newest full) and
        PostgreSQL (whole chains, on the names' own UTC stamps). Only under a day, so a window
        stated in days is judged exactly as it always was.
        """
        if 0 < self.cleanup_retention < 86400:
            return int(self.cleanup_retention)
        return None

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
        entry_offload = offload_rules.parse_backup_offload(
            entry, label=f"backup_restore.backups[{index}] ({backup_id})", db_type=db_type)
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
            # The same field the restore side reads, in the same unit, handled differently:
            # backup prunes the directory it wrote to, restore prunes what it staged. One idea,
            # one spelling - see dbabrain.lib.cleanup_retention.
            # The entry states it once for its folder (1.232, the plan's §1.2); a job that still
            # states its own keeps it - check-objects names a job and an entry that disagree.
            stated = cleanup_retention.retention_source(raw_job, entry)
            retention_seconds = cleanup_retention.parse(stated, context=f"{backup_id}.{job_name}")
            per_type = cleanup_retention.parse_per_type(stated, context=f"{backup_id}.{job_name}",
                                                        engine=db_type)
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
                cleanup_retention=retention_seconds,
                retention=per_type,
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
                offload=entry_offload,
                scope=backup_scope.parse_backup_databases(raw_job, label=f"{backup_id}.{job_name}", db_type=db_type),
                scrub_files_per_run=integrity.scrub_files_per_run(entry, label=backup_id),
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
    :mod:`dbabrain.common.remote_exec` the metrics collectors use against these hosts every cycle.
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
            "`local` is refused on purpose: it would back up the dbabrain container, under the "
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
#: :func:`dbabrain.common.hostcmd.run_script`; this set is what the config resolver will accept.
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
    #: The files the script wrote, from its FILE lines (``common.cli backup-database``'s ``files``).
    files: list[dict[str, Any]] = field(default_factory=list)


def record_in_catalog(
    catalog: BackupCatalogStore,
    item: BackupJob,
    result: BackupRunResult,
    *,
    job_run_id: int | None,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """Write one finished run and its files into the backup catalog; answer what was verified.

    The operator, 2026-10-29: *every backup run is saved into the db - file name, server, checksum
    - so every run is verified*. A file the script could not hash, or whose engine check failed,
    is recorded as such and makes the run an error: a backup nobody can verify is not a success.
    The run's level is its files' (``lib.integrity.LEVEL_OF_FILE_TYPE``) - a PostgreSQL job asked
    for an incremental may have taken a full - and is NULL for a run that wrote nothing.
    """
    engine = str(item.db_type or "").strip().lower()
    files = [dict(entry) for entry in result.files or []]
    levels = {integrity.LEVEL_OF_FILE_TYPE.get(str(entry.get("file_type") or "")) for entry in files}
    levels.discard(None)
    # One level when the files agree; the strongest otherwise (a level 0 run also writes archivelogs).
    level = next((name for name in ("full", "diff", "log") if name in levels), None)
    rows = []
    for entry in files:
        verdict = integrity.file_verdict(entry)
        rows.append({
            "backup_id": item.backup_id, "job_run_id": job_run_id, "server_id": item.server_id,
            "database_name": entry.get("database_name") or "", "engine": engine,
            "file_name": entry.get("file_name") or "", "file_type": entry.get("file_type") or "",
            "file_path": entry.get("path") or "", "file_folder": entry.get("file_folder") or "",
            "host_path": entry.get("host_path") or "", "container_name": entry.get("container_name") or "",
            # When this file's backup began, from its FILE line (1.254). NULL where the script did not
            # say, not the run's start: a WAL segment the server archived was written before the run.
            "backup_started_at": entry.get("backup_started_at"),
            "backup_ended_at": finished_at, "size_bytes": entry.get("size_bytes"),
            "checksum_algo": integrity.SHA256, "checksum_after_backup": entry.get("sha256"),
            "engine_verify": entry.get("engine_verify"),
            "engine_verify_text": entry.get("engine_verify_text") or "", "verdict": verdict,
            # As the script wrote it (its FILE line); NULL when the script did not say.
            "encrypted": entry.get("encrypted"),
            # Written this minute, so on disk and the stored checksum is its own; verify-backup-files
            # keeps both answers true afterwards. Unknown for a file that could not be hashed.
            "on_disk": 1, "checksum_matches": 1 if entry.get("sha256") else None,
            "last_checked_at": finished_at if entry.get("sha256") else None,
            "last_check_result": "match" if entry.get("sha256") else None,
            "checksum_last_check": entry.get("sha256"),
            # Waiting for its repository copy, when the entry has one (1.240); NULL when it has none.
            "offload_state": "pending" if (item.offload is not None and item.offload.enabled) else None,
        })
    unverified = [row for row in rows if row["verdict"] != "ok"]
    status = "DONE" if result.status == "done" and not unverified else "ERROR"
    verify_results = {row["engine_verify"] for row in rows}
    run = {
        "backup_id": item.backup_id, "job_run_id": job_run_id, "server_id": item.server_id,
        "engine": engine, "level": level, "node_host": socket.gethostname(),
        "app_version": __version__, "started_at": started_at, "ended_at": finished_at,
        "status": status, "files_count": len(rows),
        "bytes_total": sum(int(row["size_bytes"] or 0) for row in rows),
        "engine_verify": ("failed" if "failed" in verify_results else "passed" if "passed" in verify_results
                          else "not_run" if rows else None),
        "error_text": str(result.error_text or "")[:2000],
        **_retention_of(item),
    }
    backup_run_id = catalog.record_backup(run=run, files=rows)
    return {"backup_run_id": backup_run_id, "files": len(rows), "level": level,
            "verified": len(rows) - len(unverified),
            "unverified": [f"{row['file_name']} ({row['verdict']})" for row in unverified]}


class _UnavailableCatalog:
    """Stands in for a catalog that could not be opened: every write raises with the reason, so each
    run records "the backup catalog could not be written" on its own row - one bad store does not
    stop the pass, and no run reads as recorded when it was not."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def record_backup(self, **_kwargs: Any) -> int:
        # The run's own error, of the kind it is: the catalog could not be opened.
        raise errors.OperationFailed(self.reason)


def _open_catalog(app_config: Any) -> Any:
    try:
        return BackupCatalogStore.from_config(app_config)
    except Exception as exc:  # noqa: BLE001 - becomes each run's own error, see _UnavailableCatalog
        return _UnavailableCatalog(f"{type(exc).__name__}: {exc}")


#: What a backup folder holds that a restore uses, per engine catalog-backfill reads (1.215). SQL
#: Server's layout `<DB>/FULL|DIFF|LOG/<file>` types every file; a PostgreSQL base backup is a directory
#: catalogued, as its receipt does, by its `backup_manifest`, and a WAL segment is a 24-hex-digit name
#: under `wal/` (0.35.0); an RMAN piece is typed by the names the Oracle scripts give it (0.35.0).
BACKFILL_PATTERNS = {"sqlserver": ["*.bak", "*.trn", "*.cer", "*.pvk"],
                     "postgresql": ["backup_manifest", "?" * 24],
                     "oracle": ["*.bkp", "autobackup_*", "c-*"]}
_BACKFILL_NOTE = "catalog-backfill: hashed as found on the server, not verified by the engine"
_BACKFILL_NOTES = {
    "postgresql": "catalog-backfill: the base backup's backup_manifest hashed as found, as its receipt does; "
                  "the directory's size is not taken",
    "oracle": "catalog-backfill: hashed as found, typed by its piece name (a level run's controlfile piece "
              "reads as that level)",
}


def _backfill_item(engine: str, relative: str, name: str) -> tuple[str, str, str] | None:
    """``(file_type, catalogued name, catalogued relative path)`` of one file ``file-checksum`` listed, or
    None for a file the engine's layout does not catalogue. A PostgreSQL base backup is catalogued as its
    directory - the file listed is its manifest, and the row's name and path are the directory's."""
    if engine == "postgresql":
        if name == "backup_manifest":
            directory = relative.rsplit("/", 1)[0] if "/" in relative else ""
            if not directory or "/" in directory.replace("base/", "", 1):
                return None             # a manifest that is not base/<stamp>_<level>/backup_manifest
            return integrity.script_file_type(engine, directory), directory.rsplit("/", 1)[-1], directory
        if integrity.WAL_SEGMENT_NAME.match(name.upper()) and (relative.startswith("wal/") or "/wal/" in relative):
            return "WAL", name, relative
        return None
    if engine == "oracle":
        return integrity.script_file_type(engine, relative), name, relative
    return integrity.sqlserver_file_type(relative), name, relative
#: A file written this recently may be a backup still being written: it is left for the next pass.
BACKFILL_SETTLE_SECONDS = 600


def catalog_backfill(
    *,
    app_config: DbaBrainConfig,
    config_path: str | Path,
    backup_id: str,
    apply: bool = False,
    max_files: int = 5000,
    data_dir: str | Path | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Catalog the backups an entry wrote before 0.34.0 - hashed as they are found (1.215, plan B.6).

    Every file a restore copies is held to the checksum its backup recorded; a file taken before
    the catalog existed has none, so the gate can only compare the copy with the source
    (``uncatalogued: compare_copy``) and an entry that says ``refuse`` cannot restore it at all. This
    hashes each such file **on its own server** (``file-checksum`` with a ``folder``) and records it
    with ``baseline='backfill'`` and ``engine_verify='not_run'``. That proves the file has not changed
    since the backfill - not that it was good when written - and the row says which it is.

    A dry run unless ``apply``: the answer lists what would be written. Files already catalogued are
    left alone, and so is a file written in the last ``BACKFILL_SETTLE_SECONDS`` (a backup running now).
    """
    from dbabrain.backup_restore.spec_builder import host_block

    jobs = [job for job in load_backup_jobs(config_path) if job.backup_id == backup_id]
    if not jobs:
        raise ValueError(f"No backup entry found with backup_id={backup_id}.")
    job = jobs[0]
    engine = str(job.db_type or "").strip().lower()
    if engine not in BACKFILL_PATTERNS:
        raise ValueError(
            f"{backup_id}: catalog-backfill reads the layouts the SQL Server, PostgreSQL and Oracle backup "
            f"scripts write; a {engine or 'blank'} entry has none it knows.")
    catalog = BackupCatalogStore.from_config(app_config)
    target = resolve_backup_target(job, data_dir=data_dir)
    needs_password = not target.key_file and bool(target.password_ref)
    secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64) if needs_password else {}
    host = host_block(target, secrets=secrets, data_dir=data_dir)
    settled_before = (now or datetime.now(timezone.utc)).timestamp() - BACKFILL_SETTLE_SECONDS
    summary: dict[str, Any] = {"backup_id": backup_id, "server_id": job.server_id, "apply": bool(apply),
                               "folders": [], "found": 0, "catalogued_already": 0, "too_new": 0,
                               "written": 0, "would_write": 0, "failed": [], "truncated": False, "files": []}

    for folder in sorted({j.backup_dir for j in jobs if j.backup_dir}):
        answer = common_cli.run("file-checksum", {
            "host": host, "folder": folder, "include": BACKFILL_PATTERNS[engine],
            "container": target.container_name or "", "max_files": int(max_files)})
        host_folder = str(answer.get("host_folder") or folder)
        files = list(answer.get("files") or [])
        summary["folders"].append({"folder": folder, "host_folder": host_folder,
                                   "found": bool(answer.get("folder_found")), "files": len(files)})
        summary["found"] += len(files)
        summary["truncated"] = summary["truncated"] or bool(answer.get("truncated"))
        windows = "\\" in host_folder or ":" in host_folder[:3]
        root = host_folder.replace("\\", "/").rstrip("/")
        listed = []
        for item in files:
            normalized = str(item["path"]).replace("\\", "/")
            name = normalized.rsplit("/", 1)[-1]
            relative = (normalized[len(root) + 1:] if normalized.lower().startswith(root.lower() + "/")
                        else name)
            typed = _backfill_item(engine, relative, name)
            if typed is None:
                summary["not_a_backup"] = summary.get("not_a_backup", 0) + 1
                continue
            listed.append((item, normalized, *typed))
        names = sorted({entry[3] for entry in listed})
        known = catalog.stored_files(server_id=job.server_id, file_names=names) if names else {}
        for item, normalized, file_type, name, relative in listed:
            if name in known:
                summary["catalogued_already"] += 1
                continue
            mtime = item.get("mtime_utc")
            if mtime and datetime.fromisoformat(str(mtime).replace("Z", "+00:00")).timestamp() > settled_before:
                summary["too_new"] += 1
                continue
            parts = [part for part in relative.split("/") if part]
            separator = "\\" if windows else "/"
            file_path = folder.rstrip("/\\") + separator + relative.replace("/", separator)
            # The server's own path of what is catalogued: a base backup's directory, not its manifest.
            host_path = str(item["path"]) if name == normalized.rsplit("/", 1)[-1] else \
                root + "/" + relative
            if engine == "sqlserver":
                database = parts[0] if len(parts) > 1 and file_type != "CERT" else ""
            else:
                # As the scripts' receipts say: a PostgreSQL backup is the cluster's, an RMAN piece the
                # database's - the entry's SID when it has one.
                database = "cluster" if engine == "postgresql" else str(job.env.get("ORACLE_SID") or "database")
            row = {
                "backup_id": backup_id, "server_id": job.server_id, "engine": engine,
                "database_name": database,
                "file_name": name, "file_type": file_type, "file_path": file_path,
                "file_folder": file_path[: -len(name)].rstrip("/\\"), "host_path": host_path,
                "container_name": target.container_name or "",
                # A base backup's size is its whole directory's, which a listing of its manifest does not
                # give: recorded as not taken (`size_failed`, the catalog's word), never as the manifest's.
                "size_bytes": None if file_type in integrity.HASHED_MEMBER else item.get("size_bytes"),
                "mtime_utc": mtime, "checksum_after_backup": item.get("sha256"),
                "verdict": "size_failed" if file_type in integrity.HASHED_MEMBER else "ok",
                "engine_verify": "not_run", "baseline": "backfill", "state": "present", "on_disk": 1,
                "checksum_matches": 1,
                "engine_verify_text": _BACKFILL_NOTES.get(engine, _BACKFILL_NOTE),
            }
            if len(summary["files"]) < 50:
                summary["files"].append({"file": relative, "type": file_type, "size_bytes": row["size_bytes"]})
            if not apply:
                summary["would_write"] += 1
                continue
            try:
                catalog.record_backup_file(**row)
                summary["written"] += 1
            except Exception as exc:  # noqa: BLE001 - one file's row must not stop the others
                summary["failed"].append({"file": relative, "why": f"{type(exc).__name__}: {exc}"[:300]})
    return summary


def verify_backup_files(
    *,
    app_config: DbaBrainConfig,
    config_path: str | Path,
    backup_id: str | None = None,
    server_id: str | None = None,
    limit: int = 500,
    data_dir: str | Path | None = None,
    key: str | None = None,
    key_base64: str | None = None,
) -> dict[str, Any]:
    """Is each catalogued backup file still on disk, and still the bytes the backup wrote?

    The files checked longest ago (or never) first, ``limit`` of them. Each is hashed on its own
    server at its ``host_path`` - the server's own path, so a stopped container does not stop the
    check - through ``common.cli file-checksum``, and the catalog holds the answer to the checksum
    IT stored (``check_backup_file``): ``on_disk``, ``checksum_matches``, ``state``. A mismatch is a
    backup that changed after it was written - the ransomware signal - and is named in the answer.
    """
    from dbabrain.backup_restore.spec_builder import host_block

    catalog = BackupCatalogStore.from_config(app_config)
    files = catalog.files_to_check(server_id=server_id or "", backup_id=backup_id or "", limit=limit)
    jobs: dict[str, BackupJob] = {}
    # Every job's retention, per FOLDER: any job writing a folder prunes it with its own window - a
    # LOG entry's 7 days cut the FULL entry's files beside it (separate entries, one directory) -
    # so a gone file is held to every window that can reach it, not only its own entry's.
    retentions: dict[tuple[str, str], list[Any]] = {}
    for job in load_backup_jobs(config_path):
        jobs.setdefault(job.backup_id, job)
        retentions.setdefault((job.server_id, job.backup_dir.rstrip("/\\")), []).append(
            job.retention or cleanup_retention.Retention(default=job.cleanup_retention, uniform=True))
    summary: dict[str, Any] = {"checked": 0, "match": 0, "mismatch": [], "missing": [], "skipped": [],
                               "pruned": [], "unreadable": []}
    now_utc = utc_now_text()
    bases: dict[str, str | None] = {}
    by_entry: dict[str, list[dict[str, Any]]] = {}
    for row in files:
        by_entry.setdefault(str(row["backup_id"]), []).append(row)

    for entry_id, rows in by_entry.items():
        job = jobs.get(entry_id)
        if job is None:
            summary["skipped"] += [{"file": r["file_name"], "why": f"backup entry {entry_id} is no longer configured"}
                                   for r in rows]
            continue
        checkable = [r for r in rows if r.get("host_path")]
        summary["skipped"] += [{"file": r["file_name"], "why": "in no mount of its container - no path on the server"}
                               for r in rows if not r.get("host_path")]
        if not checkable:
            continue
        try:
            target = resolve_backup_target(job, data_dir=data_dir)
            needs_password = not target.key_file and bool(target.password_ref)
            secrets = (_load_secrets(data_dir=data_dir, key=key, key_base64=key_base64)
                       if needs_password else {})
            host = host_block(target, secrets=secrets, data_dir=data_dir)
            paths, answer = _hash_stored_files(checkable, host=host, container=getattr(target, "container_name", "") or "")
        except Exception as exc:  # noqa: BLE001 - one unreachable server must not stop the others
            reasons = [{"file": r["file_name"], "why": f"{job.server_id}: {exc}"} for r in checkable]
            summary["skipped"] += reasons
            # Said, not swallowed: a scrub that read nothing looked exactly like a clean one.
            summary["unreadable"] += reasons
            continue
        found = {item["path"]: item for item in answer.get("files") or []}
        for row in checkable:
            item = found.get(paths[row["file_id"]])
            # Gone past its type's window: the backup script's own retention removed it (1.233), and
            # the catalog says so - pruned, with the window - instead of an attack's "gone".
            why = "" if item else next((reason for reason in (
                cleanup_retention.removed_by_retention(
                    retention, str(row.get("file_type") or ""),
                    str(row.get("backup_ended_at") or row.get("created_at") or ""), now_utc=now_utc)
                for retention in retentions.get((job.server_id, job.backup_dir.rstrip("/\\")), [])) if reason), "")
            if not item and not why:
                why = _cut_by_archivecleanup(catalog, row, bases)
            if why:
                catalog.set_backup_file(row["file_id"], state="pruned", on_disk=0, pruned_by=why)
                summary["pruned"].append({"server_id": row["server_id"], "file": row["file_name"], "why": why})
                continue
            result = catalog.check_backup_file(
                row["file_id"], checksum_now=item["sha256"] if item else None,
                size_now=item.get("size_bytes") if item else None,
                checked_by="scrub", by_run_id="verify-backup-files")
            summary["checked"] += 1
            if result == "match":
                summary["match"] += 1
            else:
                summary[result].append({"server_id": row["server_id"], "file": row["file_name"],
                                        "host_path": row["host_path"]})
    return summary


def _cut_by_archivecleanup(catalog: Any, row: dict[str, Any], bases: dict[str, str | None]) -> str:
    """Why a gone WAL segment was the archive cleanup's, or ``""``. The PostgreSQL WAL job cuts the
    archive at the START WAL of the oldest base backup kept - by position, not by age - so segments
    well inside their age window go every time a base chain is dropped. Read as gone, twenty of them
    engaged the lockdown (2026-11-03). One that ended before the oldest kept full base began is the
    cleanup's; one after it, gone, is still the signal."""
    if str(row.get("file_type") or "").upper() != "WAL":
        return ""
    server = str(row.get("server_id") or "")
    if server not in bases:
        bases[server] = catalog.oldest_kept_base(server_id=server)
    base, ended = bases[server], str(row.get("backup_ended_at") or row.get("created_at") or "")
    if base and ended and ended < base:
        return f"pg_archivecleanup: before the oldest base backup kept ({base})"
    return ""


def _hash_stored_files(rows: list[dict[str, Any]], *, host: dict[str, Any],
                       container: str) -> tuple[dict[int, str], dict[str, Any]]:
    """Hash the stored files on their server: by their path on the host, and - when that cannot be
    read and the engine runs in a container - inside the container by its own path. A backup in a
    docker VOLUME (the runtime store's own, 2026-11-03) lives under /var/lib/docker, which the
    SSH user may not read: every file was skipped and the scrub said "checked 0". Answers the path
    each file was hashed under, and the ``file-checksum`` answer."""
    on_host = {r["file_id"]: integrity.hashed_path(r["file_type"], r["host_path"]) for r in rows}
    try:
        return on_host, common_cli.run("file-checksum", {"host": host, "paths": sorted(set(on_host.values()))})
    except Exception:
        if not container or not all(r.get("file_path") for r in rows):
            raise
    inside = {r["file_id"]: integrity.hashed_path(r["file_type"], r["file_path"]) for r in rows}
    return inside, common_cli.run("file-checksum", {
        "host": {**host, "runtime": "docker", "container": container}, "paths": sorted(set(inside.values()))})


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
    The script is fed to the remote shell on stdin, which is why any ``docker exec`` inside it must
    close its own stdin - otherwise the container reads the rest of the script as its input and
    the shell silently runs out of work (exit 0, no output, nothing done).

    Through ``common.cli run-cmd`` since 0.24.0 (rules R03): this app held a paramiko client from
    ``common.ssh`` until then.
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

    from dbabrain.lib.remote_host import RemoteHost
    from dbabrain.transport import common_cli

    remote = RemoteHost(
        host=target.host, username=target.username, port=target.port,
        password=password or "", key_file=str(key_filename or ""),
        auth_type="key" if key_filename else "password",
        call=common_cli.run_allowing_failure)
    result = remote.run_script(payload, timeout_seconds=timeout or None)
    return result.stdout, result.stderr, result.exit_code


def restore_candidates(*, app_config: Any, backup_id: str) -> dict[str, Any]:
    """Each chain of an entry - a full and the files that need it - newest first, with what a person
    choosing a restore point after an attack needs (0.35.0, 1.244 / 1.225 - the plan of 2026-10-29, A3.6):

    * ``verified``: every member on disk, hashed ok when written and unchanged when last checked;
    * ``ended_at``: the newest member's end;
    * ``before_first_signal``: the chain ended before the catalog's first file found changed or gone,
      or before the lockdown - the newest chain may already be the attacker's (dwell time);
    * ``candidate``: both - a chain to restore from.

    Read-only: it lists, it chooses nothing.
    """
    from dbabrain.db.lockdown_store import LockdownStore

    catalog = BackupCatalogStore.from_config(app_config)
    files = catalog.chain_files(backup_id=backup_id)
    signal = catalog.first_signal(backup_id=backup_id)
    try:
        lockdown = LockdownStore.from_config(app_config).active()
    except Exception:  # noqa: BLE001 - a lockdown that cannot be read is no signal to date chains by
        lockdown = None
    marks = [m for m in (signal, (lockdown or {}).get("started_at")) if m]
    first = min(marks) if marks else None
    by_full: dict[int, list[dict[str, Any]]] = {}
    for row in files:
        root = row["chain_parent_file_id"] or (row["file_id"] if integrity.LEVEL_OF_FILE_TYPE.get(
            str(row["file_type"] or "").upper()) == "full" else None)
        if root is not None:
            by_full.setdefault(int(root), []).append(row)
    chains = []
    for root, members in by_full.items():
        full = next((m for m in members if m["file_id"] == root), None)
        if full is None:
            continue
        def sound(m: dict[str, Any]) -> bool:
            return (m["state"] == "present" and m["verdict"] == "ok" and m["on_disk"] != 0
                    and m["checksum_matches"] != 0)

        verified = all(sound(m) for m in members)
        ended = max(str(m["ended_at"] or "") for m in members)
        before = first is None or ended < first
        chains.append({
            "database_name": full["database_name"], "full": full["file_name"], "full_ended_at": full["ended_at"],
            "files": len(members), "ended_at": ended, "verified": verified,
            "unverified": [m["file_name"] for m in members if not sound(m)][:10],
            "offloaded": sum(1 for m in members if m["offload_state"] == "verified"),
            "last_restored": max((str(m["last_restore_at"]) for m in members if m["last_restore_at"]), default=None),
            "before_first_signal": before, "candidate": verified and before,
        })
    chains.sort(key=lambda c: (c["database_name"] or "", c["ended_at"]), reverse=True)
    return {"backup_id": backup_id, "first_signal": first, "signal_from": (
                "lockdown" if first and lockdown and first == lockdown.get("started_at") else
                "a stored file changed or gone" if first else None),
            "chains": chains, "candidates": sum(1 for c in chains if c["candidate"])}


def _retention_of(item: "BackupJob") -> dict[str, Any]:
    """What a backup run records of its job's retention (1.233): the configured value and how far
    back point-in-time reaches - the shorter of the base and the log window (T3)."""
    retention = getattr(item, "retention", None)
    if retention is None:
        seconds = getattr(item, "cleanup_retention", None)
        if seconds is None:
            return {}
        return {"retention_json": json.dumps(int(seconds)), "pitr_reach_seconds": int(seconds)}
    reach = cleanup_retention.point_in_time_reach(retention, engine=str(item.db_type or ""))
    return {"retention_json": json.dumps(retention.as_config(), sort_keys=True),
            "pitr_reach_seconds": None if reach is None else int(reach)}


def _engage_lockdown(app_config: Any, metadata: dict[str, Any], *, reason: str, by: str) -> str:
    """Turn the lockdown on (1.220) and answer the words the alert carries."""
    try:
        engaged = lockdown_store.LockdownStore.from_config(app_config).engage(reason=reason, by=by)
        metadata["lockdown"] = {"lockdown_id": engaged.get("lockdown_id"), "already_on": engaged.get("already_on")}
        return " - LOCKDOWN ON: no prune, delete or overwrite until it is turned off"
    except Exception as exc:  # noqa: BLE001 - the alert must still go out
        return f" - the lockdown could not be engaged: {type(exc).__name__}: {exc}"


def _ransom_markers(item: "BackupJob", *, data_dir: Any, key: str | None, key_base64: str | None) -> list[str]:
    """The files in the entry's backup folder named like ransomware's notes or encrypted files (1.219).
    A listing that cannot be taken answers nothing: the backup that just ran reached the same host."""
    from dbabrain.backup_restore.spec_builder import host_block

    if not item.backup_dir:
        return []
    try:
        target = resolve_backup_target(item, data_dir=data_dir)
        needs_password = not target.key_file and bool(target.password_ref)
        secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64) if needs_password else {}
        host = host_block(target, secrets=secrets, data_dir=data_dir)
        request = {"folder": item.backup_dir, "include": list(integrity.RANSOM_MARKERS), "max_files": 50}
        try:
            answer = common_cli.run("file-checksum", {**request, "host": host,
                                                      "container": target.container_name or ""})
        except Exception:
            if not target.container_name:
                raise
            # A folder in a docker volume is not readable from the host (see _hash_stored_files):
            # listed inside the container, by its own path.
            answer = common_cli.run("file-checksum", {
                **request, "host": {**host, "runtime": "docker", "container": target.container_name}})
    except Exception:  # noqa: BLE001 - see docstring
        return []
    return sorted(str(f.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1] for f in answer.get("files") or [])


def _size_anomalies(catalog: Any, item: "BackupJob", catalog_summary: dict[str, Any] | None) -> list[str]:
    """Each file this run wrote whose size is far from the same database's and type's history (1.219)."""
    if not catalog_summary or not catalog_summary.get("backup_run_id"):
        return []
    found = []
    try:
        for row in catalog.backup_run_files(int(catalog_summary["backup_run_id"])):
            history = catalog.earlier_sizes(backup_id=item.backup_id, database_name=row.get("database_name") or "",
                                            file_type=row.get("file_type") or "",
                                            before_run=int(catalog_summary["backup_run_id"]))
            why = integrity.size_anomaly(row.get("size_bytes"), history)
            if why:
                found.append(f"{row.get('file_name')}: {why}")
    except Exception:  # noqa: BLE001 - a judgement that cannot be made is not a finding
        return found
    return found


def _load_secrets(
    *, data_dir: str | Path | None, key: str | None, key_base64: str | None
) -> dict[str, str]:
    """The decrypted secret store, for jobs that declare ``env_secrets``."""
    from dbabrain.lib.secret_text import resolve_cli_key

    secret_key: str | None = None
    try:
        secret_key = resolve_cli_key(key, key_base64)
    except Exception:  # noqa: BLE001 - no/invalid key on argv; the env may still carry one.
        secret_key = None
    secret_key = secret_key or os.environ.get("DBABRAIN_SECRET_KEY") or None
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

    The running itself moved to :mod:`dbabrain.common.backup`; what is left here is the app's half —
    read the secret store, build the spec, and translate the answer back into a ``BackupRunResult``
    the scheduler and the store rows already speak. The receipt rule, the level translation and the
    transport all live below now, so a one-off run against an unregistered machine gets exactly the
    behaviour the scheduled run gets.
    """
    request = _backup_request(job, target=target, data_dir=data_dir, key=key, key_base64=key_base64,
                              env_overrides=env_overrides, backup_type=backup_type)
    # `run_allowing_failure`, not `run`: a backup that fails is an outcome this app records, not
    # an exception it stops on. The CLI answers `success: false` with the exit code, stderr and
    # error text still in `data` — which is exactly what the job_runs row below is made of.
    _ok, result, error = common_cli.run_allowing_failure("backup-database", request)
    return BackupRunResult(
        job=job, status=result.get("status") or "error", exit_code=result.get("exit_code"),
        duration_ms=result.get("duration_ms"), stdout=result.get("stdout"), stderr=result.get("stderr"),
        error_text=result.get("error") or error or None, files=list(result.get("files") or []),
    )


def _backup_request(job: BackupJob, *, target: BackupTarget, data_dir: str | Path | None, key: str | None,
                    key_base64: str | None, env_overrides: dict[str, str] | None = None,
                    backup_type: str | None = None) -> dict[str, Any]:
    """The ``backup-database`` request for ``job`` - built once, for the run and for its stop, so a
    stop reaches the run's host with the run's own login (1.238)."""
    from dbabrain.backup_restore.spec_builder import backup_request_from_job

    # The store is read when *the spec* will need a secret — which is more than when the job
    # declares env_secrets. The SSH password is a secret too, and since the transport moved to
    # dbabrain.common.backup the target's password_ref is resolved here into spec.host.password
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
    return backup_request_from_job(
        job, target=target, secrets=secrets, level=str(backup_type or ""),
        env_overrides=env_overrides, data_dir=data_dir,
    )


def _stopped_by_operator(store: Any, log_id: int) -> bool:
    """Whether ``backup-stop`` closed this run's row (its metadata says so)."""
    try:
        row = store.fetch_job_run(log_id)
    except Exception:  # noqa: BLE001 - a store that cannot answer leaves the runner to write its row
        return False
    if row is None:
        return False
    try:
        return bool(json.loads(row["metadata_json"] or "{}").get("stopped_by_operator"))
    except (TypeError, ValueError):
        return False


def stop_backup_run(
    *,
    app_config: DbaBrainConfig,
    config_path: str | Path,
    backup_id: str,
    data_dir: str | Path | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    logger: Any = None,
) -> dict[str, Any]:
    """Stop every running run of ``backup_id`` on its host, and close its row as stopped (1.238).

    The order is ``common.cli backup-stop``'s: the run's process tree first, then its sessions - on
    192.0.2.245 (2026-10-29) the session alone was killed and the script backed up the next
    database. The row is closed only while it still says RUNNING, and says *stopped by the
    operator* with what was ended; the runner, when its call returns, finds that and leaves it.
    """
    jobs = [job for job in load_backup_jobs(config_path) if job.backup_id == backup_id]
    if not jobs:
        raise ValueError(f"No backup entry found with backup_id={backup_id}.")
    store = DbaBrainStore.from_config(app_config)
    stops: list[dict[str, Any]] = []
    for job in jobs:
        for row in store.fetch_running_job_runs(job.job_code):
            if row["job_code"] != job.job_code:
                continue
            log_id = int(row["log_id"])
            tag = run_tag(job.backup_id, log_id)
            target = resolve_backup_target(job, data_dir=data_dir)
            request = _backup_request(job, target=target, data_dir=data_dir, key=key, key_base64=key_base64)
            _ok, data, error = common_cli.run_allowing_failure("backup-stop", {
                "run_tag": tag, "db_type": request["db_type"], "host": request["host"],
                "env": request.get("env") or {}})
            data = dict(data or {})
            ended = (f"{len(data.get('killed') or [])} process(es), "
                     f"{len(data.get('sessions_killed') or [])} session(s) ended")
            stopped = bool(data.get("stopped"))
            message = (f"Backup {job.label} stopped by the operator - {ended}."
                       if stopped else
                       f"Backup {job.label} stop asked by the operator - {ended}, NOT fully stopped: "
                       f"{error or data.get('error') or 'something of the run is still there'}")
            try:
                previous = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                previous = {}
            metadata = {**previous, "run_tag": tag, "stopped_by_operator": {
                "at": utc_now_text(), "stopped": stopped, "pid": data.get("pid"),
                "killed": data.get("killed") or [], "sessions_killed": data.get("sessions_killed") or [],
                "sessions_left": data.get("sessions_left") or [], "process_alive": data.get("process_alive")}}
            closed = store.update_job_run(
                log_id=log_id, level="error", status="ERROR", message=message, finished_at=utc_now_text(),
                error_text=None if stopped else (error or data.get("error") or message)[:2000],
                metadata=metadata, only_if_running=True)
            if closed:
                emit_backup_restore_event(
                    app_config=app_config, command="backup", phase="ERROR", level="warning",
                    message=message, logger=logger, started_at=row["started_at"], finished_at=utc_now_text(),
                    metadata=metadata, notify=job.notify)
            stops.append({"backup_id": job.backup_id, "job": job.job, "log_id": log_id, "run_tag": tag,
                          "stopped": stopped, "row_closed": bool(closed), "message": message, "stop": data})
    return {"backup_id": backup_id, "running": len(stops), "stops": stops,
            "stopped": bool(stops) and all(item["stopped"] for item in stops)}


def run_backup(
    *,
    app_config: DbaBrainConfig,
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

    store = DbaBrainStore.from_config(app_config)
    catalog = _open_catalog(app_config)
    # Close and report runs abandoned at RUNNING past their timeout — a job whose process was
    # killed never got to report its own failure. See schedule.reap_stale_runs.
    stale = schedule.reap_stale_runs(
        store=store, app_config=app_config, logger=logger,
        timeouts={item.job_code: item.time_window.timeout for item in jobs},
        notify={item.job_code: item.notify for item in jobs},
    )
    listed_at = utc_now_text()
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
        # Due when this run listed it is not the same as still due: an overlapping async run may have
        # taken it since, and finished it (schedule.taken_since). An explicit --force still runs.
        if not force and schedule.taken_since(store, item.job_code, listed_at):
            summary["skipped"] += 1
            summary.setdefault("taken_by_another_run", []).append(
                {"backup_id": item.backup_id, "job": item.job})
            continue
        started_at = utc_now_text()
        try:
            log_id = store.insert_job_run(
                JobRun(
                    job_code=item.job_code,
                    # One backup job runs at a time on one host, whatever calls it. The store's
                    # unique index is what says so, so two overlapping scans cannot both start it
                    # and write to the same target directory.
                    claim_key=item.job_code,
                    level="logging",
                    status="RUNNING",
                    message=f"Backup {item.label} started.",
                    started_at=started_at,
                    host_name=socket.gethostname(),
                    metadata={"backup_id": item.backup_id, "job": item.job,
                              "server_id": item.server_id},
                )
            )
        except RunAlreadyClaimed as exc:
            # Already running. Skipped rather than failed: the scheduler is working, and an alert
            # here would be an alert about nothing going wrong.
            summary["skipped"] += 1
            summary.setdefault("already_running", []).append(
                {"backup_id": item.backup_id, "job": item.job, "detail": str(exc)})
            continue
        summary["ran"] += 1
        emit_backup_restore_event(
            app_config=app_config, command="backup", phase="START", level="logging",
            message=f"{item.label} started.", logger=logger, started_at=started_at,
            metadata={"backup_id": item.backup_id, "job": item.job, "server_id": item.server_id},
            notify=item.notify,
        )
        try:
            target = resolve_backup_target(item, data_dir=data_dir)
            job_env = dict(env_overrides or {})
            # What a stop finds this run by on its host: the script's pid file and its sessions'
            # host_name (0.34.0, 1.238).
            job_env[RUN_TAG_ENV] = run_tag(item.backup_id, log_id)
            if str(item.db_type).lower() == "postgresql" and not isinstance(catalog, _UnavailableCatalog):
                # The WAL job lists the segments after the newest one the catalog holds: the
                # archive is written by PostgreSQL, not by the job, so only the store knows where
                # the last run left off (0.34.0). Ignored by the base backup script.
                # Absent, not empty, before the catalog holds a segment: the spec refuses an empty
                # env value (it is how a missing secret arrives), and the script reads absent as
                # "from the first segment". Sent empty, the store's own WAL job failed its first run
                # on the worker (2.87.51, 2026-10-29).
                last_wal = catalog.last_file_name(backup_id=item.backup_id, file_type="WAL")
                if last_wal:
                    job_env.setdefault("CATALOG_AFTER", last_wal)
            # backup_type travels as a word, not as a translated BACKUP_LEVEL: the same word maps
            # to a different level per engine (and a --backup-id can select jobs of more than one),
            # so the translation belongs where the engine is known — once, in the spec.
            result = execute_backup_job(
                item, target=target, data_dir=data_dir, key=key, key_base64=key_base64,
                env_overrides=job_env, backup_type=backup_type,
            )
        except (ValueError, SshError, OSError) as exc:
            result = BackupRunResult(
                job=item, status="error", exit_code=None, duration_ms=0,
                stdout="", stderr=str(exc), error_text=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - one job's failure must not stop the pass.
            # A `common.cli` child that printed no JSON (crash, OOM, import error) raises
            # CommonCliError - a RuntimeError - which used to leave this function: every job after
            # it in the pass was skipped and its own row stayed RUNNING (review 0.25.0, F10.1).
            text = f"{type(exc).__name__}: {exc}"
            result = BackupRunResult(
                job=item, status="error", exit_code=None, duration_ms=0,
                stdout="", stderr=text, error_text=text,
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
        # Every run into the backup catalog, a failed one too (0.34.0). A file left unverified, or
        # a catalog that cannot be written, turns the run into an error: the operator's rule is
        # that every run is verified and recorded, and a green run that is neither is the failure
        # this exists to end.
        catalog_summary: dict[str, Any] | None = None
        try:
            catalog_summary = record_in_catalog(catalog, item, result, job_run_id=log_id,
                                                started_at=started_at, finished_at=finished_at)
            if catalog_summary["unverified"] and result.status == "done":
                result.status = "error"
                result.error_text = ("written but not verified: "
                                     + ", ".join(catalog_summary["unverified"][:10]))
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never lost in a traceback
            if result.status == "done":
                result.status = "error"
            result.error_text = " ".join(filter(None, [
                result.error_text, f"the backup catalog could not be written: {type(exc).__name__}: {exc}"]))
        # The rolling scrub (1.218): the entry's older files, the ones checked longest ago first,
        # re-hashed where they lie. Not on a failed run - its host may be the reason it failed.
        scrub: dict[str, Any] | None = None
        if result.status == "done" and item.scrub_files_per_run and not dry_run:
            try:
                scrub = verify_backup_files(app_config=app_config, config_path=config_path,
                                            backup_id=item.backup_id, limit=item.scrub_files_per_run,
                                            data_dir=data_dir, key=key, key_base64=key_base64)
            except Exception as exc:  # noqa: BLE001 - a scrub that could not run is said, the backup stands
                scrub = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        # What ransomware leaves in a folder (1.219): looked for by name only, so it costs a listing.
        markers: list[str] = []
        if result.status == "done" and not dry_run:
            markers = _ransom_markers(item, data_dir=data_dir, key=key, key_base64=key_base64)
        level = "logging" if result.status == "done" else "error"
        message = (
            f"Backup {item.label} finished: {result.status}"
            + (f" (exit {result.exit_code})" if result.exit_code is not None else "")
        )
        # The REASON goes in the message, not only in error_text. Both are written to the run row,
        # but only the message reaches backup.log, the Telegram alert and the workflow summary - so
        # a failure read anywhere but the store said "error (exit 1)" and nothing else. Measured
        # 2026-09-15: a backup failing every cycle, whose cause - `pg_basebackup failed
        # (level=INCR); partial target removed` - was sitting in job_runs.error_text and nowhere a
        # person was looking. A script's own words are the diagnosis; the exit code is not.
        _reason = " ".join(str(result.error_text or "").split())
        if result.status != "done" and _reason:
            message += f" - {_reason[:300]}"
        metadata = {
            "backup_id": item.backup_id,
            "job": item.job,
            "server_id": item.server_id,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "stdout_tail": stdout_excerpt(result.stdout),
        }
        if catalog_summary is not None:
            metadata["catalog"] = catalog_summary
            if catalog_summary["files"]:
                message += (f" - {catalog_summary['verified']}/{catalog_summary['files']} file(s) "
                            "hashed and verified")
        if markers:
            # Not a backup: an attack. CRITICAL, and the lockdown engaged like a canary hit (D6).
            level = "critical"
            metadata["ransom_markers"] = markers[:20]
            message += f" - RANSOM MARKERS in {item.backup_dir}: {', '.join(markers[:5])}"
            message += _engage_lockdown(app_config, metadata, reason=f"ransomware markers in the backup folder of "
                                        f"{item.label}: {', '.join(markers[:3])}", by=f"markers:{item.label}")
        anomalies = _size_anomalies(catalog, item, catalog_summary)
        if anomalies:
            metadata["size_anomalies"] = anomalies[:10]
            if level == "logging":
                level = "warning"
            message += " - SIZE far from its history: " + "; ".join(anomalies[:3])
        if scrub is not None:
            changed, gone = scrub.get("mismatch") or [], scrub.get("missing") or []
            unreadable = scrub.get("unreadable") or []
            metadata["scrub"] = {"checked": scrub.get("checked", 0), "match": scrub.get("match", 0),
                                  "mismatch": len(changed), "missing": len(gone),
                                  "pruned": len(scrub.get("pruned") or []), "unreadable": len(unreadable),
                                  "error": scrub.get("error")}
            if unreadable and not (changed or gone):
                # Not an attack, and not a clean scrub either: the files could not be read at all.
                if level == "logging":
                    level = "warning"
                message += (f" - SCRUB could not read {len(unreadable)} stored file(s): "
                            f"{str(unreadable[0].get('why') or '')[:200]}")
            if changed or gone:
                # A stored backup changed after it was written, or vanished: the ransomware signal.
                # CRITICAL whatever tonight's backup did - the backup stands, this is another finding.
                level = "critical"
                if len(changed) + len(gone) >= lockdown_store.AUTO_ENGAGE_FILES:
                    # An attack is not waited out (D6): no prune, no delete, no overwrite from now on.
                    message += _engage_lockdown(
                        app_config, metadata, by=f"scrub:{item.label}",
                        reason=f"the scrub of {item.label} found {len(changed)} changed and {len(gone)} "
                               "gone stored backup file(s)")
                names = ", ".join(str(f.get("file")) for f in [*changed, *gone][:5])
                message += (f" - SCRUB: {len(changed)} stored backup file(s) changed after they were written, "
                            f"{len(gone)} gone: {names}")
            elif scrub.get("checked"):
                message += f" - {scrub['checked']} older file(s) re-hashed, all unchanged"
        if server_metadata_result is not None:
            metadata["server_metadata"] = summarize_server_metadata(server_metadata_result)
            # Named in the message because the alternative is an operator having to open the run
            # row to discover the bundle beside tonight's backup is yesterday's.
            if not server_metadata_result.get("ok", False):
                message += " (server metadata export FAILED - see metadata.server_metadata)"
        if _stopped_by_operator(store, log_id):
            # backup-stop closed this row and said so; what the killed script left behind (a WinRM
            # error, a broken pipe) is not news, and must not replace "stopped by the operator".
            result.status = "stopped"
        else:
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
