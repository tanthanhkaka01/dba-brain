from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from db_ops.backup_restore.backup import BACKUP_TYPES, load_backup_jobs, run_backup
from db_ops.backup_restore.restore_script import load_script_restores
from db_ops.lib.listing import active_only, hidden_note
from db_ops.backup_restore.workflow import run_workflow
from db_ops.backup_restore.config import (
    BACKUP_RESTORE_NOTIFY_DEFAULTS,
    BackupRestoreConfig,
    DatabaseRestoreMapping,
    load_restore_configs,
    merge_notify_configs,
)
from db_ops.backup_restore.copy_backup import run_copy_backup
from db_ops.backup_restore.delete_backup import run_delete_backup
from db_ops.backup_restore.preflight import PreflightError, run_target_preflight
from db_ops.backup_restore.events import announce, emit_backup_restore_event, resolve_run_id
from db_ops.backup_restore.restore_database import (
    build_restore_candidate,
    get_latest_full_backup_for_database,
    parse_point_in_time,
    run_restore_all_latest,
    run_restore_database,
    run_restore_diff,
    run_restore_full,
    run_restore_log,
    summarize_error_tail,
)
from db_ops.backup_restore.sanitize import sanitize_text, sanitize_value
from db_ops.backup_restore.verify_restore import run_verify_restore
from db_ops.lib.notify import NotifyConfig
from db_ops.lib.secret_text import add_key_argument, set_key_env
from db_ops.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from db_ops.db.store import utc_now_text
from db_ops.logging_ops import LOG_SCOPE_ENV_VAR, log_event, log_function_call, setup_app_logger
from db_ops.logging_ops.runtime_stdout import patch_stdout


class RestoreWorkflowError(RuntimeError):
    def __init__(self, message: str, *, metadata: dict[str, object]) -> None:
        super().__init__(message)
        self.metadata = metadata


def parse_args(argv: list[str]) -> argparse.Namespace:
    config_parent = argparse.ArgumentParser(add_help=False)
    # SUPPRESS for the same reason as the key flags below: the top-level parser registers
    # --config too, and a plain default here overwrote a --config given before the subcommand,
    # silently falling back to config.json instead of the file that was asked for.
    config_parent.add_argument("--config", default=argparse.SUPPRESS, help="Path to db_ops config JSON. Defaults to config.backup_restore.json or config.json.")
    # inherited=True: these same flags are registered on the top-level parser below, and every
    # subcommand inherits this one. Without it, a --key-base64 given *before* the subcommand was
    # overwritten by this copy's default at subparse time and the run died with "No decryption
    # key provided" on a command line that was perfectly correct.
    add_key_argument(config_parent, inherited=True)

    parser = argparse.ArgumentParser(description="DB Ops backup restore automation.")
    parser.add_argument("--config", default=None, help="Path to db_ops config JSON. Defaults to config.backup_restore.json or config.json.")
    add_key_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    copy = subparsers.add_parser("copy-backup", parents=[config_parent], help="Copy backup files into the local import folder.")
    copy.add_argument("--hours", type=int, help="Override copy_recent_hours for this run. Use 0 or negative to copy all matching files.")
    copy.add_argument("--source-id", help="Copy only one configured source/server.")
    copy.add_argument("--restore-id", help="Copy only the restore entry with this restore_id.")
    copy.add_argument("--force", action="store_true", help="Copy files even if they already exist in the target.")

    delete = subparsers.add_parser("delete-backup", parents=[config_parent], help="Delete old backup files from the target import folder.")
    delete.add_argument("--hours", type=int, required=True, help="Delete target files older than this many hours. Use 0 or negative to delete all target files.")
    delete.add_argument("--source-id", help="Delete target files for only one configured source/server.")
    delete.add_argument("--restore-id", help="Delete target files only for the restore entry with this restore_id.")

    backup = subparsers.add_parser(
        "backup",
        parents=[config_parent],
        help="Run every configured backup job that is due (scans backup_restore.backups).",
    )
    backup.add_argument("--backup-id", help="Run jobs for only one configured backup entry.")
    backup.add_argument("--job", help="Run only this job name within the selected entries (e.g. database, archivelog).")
    backup.add_argument("--force", action="store_true", help="Run the selected jobs even if they are not due.")
    backup.add_argument("--dry-run", action="store_true", help="List the jobs that would run without executing them.")
    backup.add_argument(
        "--backup-type", choices=BACKUP_TYPES, default=None,
        help="Force the level for this run instead of letting the script derive it from the "
             "weekday: full | diff | log. One word for every engine - it is translated to that "
             "engine's own name (Oracle 0/1, PostgreSQL full/incr, SQL Server full/diff/log). "
             "The scheduled config passes nothing, so the script keeps deciding for itself.",
    )
    backup.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE",
        help="Override one script env var for this run only, e.g. --env BACKUP_LEVEL=0 to take a "
             "level 0 baseline on a day the script would otherwise pick level 1. Repeatable.",
    )

    prune = subparsers.add_parser(
        "prune-backups",
        parents=[config_parent],
        help="Delete the obsolete backup files of the configured entries (retention cleanup).",
    )
    prune.add_argument("--backup-id", help="Prune only one configured backup entry.")
    prune.add_argument("--job", help="Prune only this job name within the selected entries.")
    prune.add_argument(
        "--retention-days", type=int, default=None,
        help="Override the window for this run. Default: whatever each job already declares, "
             "which is the same number its own script prunes with.",
    )
    prune.add_argument(
        "--mode", choices=("age", "recovery_window"), default="age",
        help="age (default): a file older than the window is obsolete, full/diff/log alike. "
             "recovery_window: keep whatever is needed to restore to any point in the window.",
    )
    prune.add_argument(
        "--apply", action="store_true",
        help="Actually delete. Without it the command reports what is obsolete and removes "
             "nothing - the opposite default from `backup`, because reporting a backup that did "
             "not happen is a wasted run and reporting a deletion that did not happen is not.",
    )

    workflow = subparsers.add_parser(
        "workflow",
        parents=[config_parent],
        help="Run the due backup jobs, then the due restore entries (the scheduled entry point).",
    )
    workflow.add_argument("--backup-id", help="Limit the backup half to one configured backup entry.")
    workflow.add_argument("--job", help="Limit the backup half to this job name (e.g. database, archivelog).")
    workflow.add_argument("--restore-id", help="Limit the restore half to one configured restore entry.")
    workflow.add_argument("--force", action="store_true", help="Run the selected work even if it is not due.")
    workflow.add_argument("--dry-run", action="store_true", help="List what would run without executing it.")
    workflow.add_argument("--skip-backup", action="store_true", help="Run only the restore half.")
    workflow.add_argument("--skip-restore", action="store_true", help="Run only the backup half.")
    workflow.add_argument("--copy-hours", type=int, default=24, help="Hours for the restore copy step. Default: 24.")
    workflow.add_argument("--delete-hours", type=int, default=None,
                          help="Override target retention for the restore half, in hours. "
                               "Default: each entry's target_retention_seconds.")
    workflow.add_argument(
        "--backup-type", choices=BACKUP_TYPES, default=None,
        help="Force the backup level for the backup half of this run: full | diff | log.",
    )
    workflow.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE",
        help="Override one backup script env var for this run only. Repeatable.",
    )

    subparsers.add_parser(
        "list-backups", parents=[config_parent],
        help="List configured backup IDs with their engine, level and schedule.",
    )

    import_cert = subparsers.add_parser(
        "import-certificate",
        parents=[config_parent],
        help="Fetch the configured backup encryption certificate and import it on the restore VM.",
    )
    import_cert.add_argument("--source-id", help="Import the certificate for only one configured source/server.")
    import_cert.add_argument("--restore-id", help="Import the certificate only for the restore entry with this restore_id.")
    import_cert.add_argument("--dry-run", action="store_true", help="Validate config without calling the certificate API or sqlcmd.")

    restore = subparsers.add_parser(
        "restore-latest",
        parents=[config_parent],
        help="Restore the latest FULL .bak file with recovery.",
    )
    restore.add_argument("--backup-file", help="Restore a specific VM import UNC .bak file instead of auto-selecting latest FULL.")
    restore.add_argument("--source-id", help="Restore only one configured source/server.")
    restore.add_argument("--restore-id", help="Restore only the entry with this restore_id.")
    restore.add_argument("--database", help="Restore only one source database from the selected source.")
    restore.add_argument("--target-database", help="Override target database name when --database is used.")
    restore.add_argument("--dry-run", action="store_true", help="Print generated SQL and command without executing sqlcmd.")

    restore_workflow = subparsers.add_parser(
        "restore-workflow",
        parents=[config_parent],
        help="Run copy-backup, restore-latest, then delete-backup.",
    )
    restore_workflow.add_argument(
        "restore_id_pos", nargs="?", default=None, metavar="RESTORE_ID",
        help="Optional positional restore_id — equivalent to --restore-id RESTORE_ID.",
    )
    restore_workflow.add_argument("--source-id", help="Run workflow for only one configured source/server.")
    restore_workflow.add_argument("--restore-id", help="Run workflow only for the restore entry with this restore_id. Example: python -m db_ops.backup_restore.cli restore-workflow --config data/restore_config.json --restore-id ACME_TO_SQLSERVER_198_51_100_31 --force")
    restore_workflow.add_argument("--copy-hours", type=int, default=24, help="Hours for copy-backup. Default: 24.")
    restore_workflow.add_argument("--delete-hours", type=int, default=None,
                                  help="Override how long staged files are kept on the target, in hours. "
                                       "Default: each entry's target_retention_seconds.")
    restore_workflow.add_argument("--dry-run", action="store_true", help="Dry-run restore-latest during the workflow.")
    restore_workflow.add_argument("--force", action="store_true", help="Force copy even if files already exist in the target.")
    restore_workflow.add_argument(
        "--point-in-time",
        default=None,
        help="Restore to a specific point in time (PITR). Format: 'YYYY-MM-DD HH:MM:SS +HH:MM'. "
             "Requires transaction log backups. If omitted, restores to the latest available state.",
    )
    restore_workflow.add_argument(
        "--execution-mode",
        default=None,
        choices=["sync", "unsync"],
        help="Override execution_mode for this run only (sync or unsync). Defaults to the value in config.",
    )

    restore_step = argparse.ArgumentParser(add_help=False)
    restore_step.add_argument("--backup-file", help="Restore step against a specific VM import UNC .bak file.")
    restore_step.add_argument("--source-id", help="Run restore step for only one configured source/server.")
    restore_step.add_argument("--restore-id", help="Run restore step only for the entry with this restore_id.")
    restore_step.add_argument("--database", help="Run restore step for one source database.")
    restore_step.add_argument("--target-database", help="Override target database name when --database is used.")
    for step_name in ("restore-full", "restore-diff", "restore-log"):
        subparsers.add_parser(step_name, parents=[config_parent, restore_step], help=argparse.SUPPRESS)

    by_id = subparsers.add_parser(
        "restore-by-id", parents=[config_parent],
        help="Restore one configured entry through the db_ops.common primitives (JSON request).")
    by_id.add_argument("request", help="JSON object: {\"restore_id\": ..., \"point_in_time\": ..., \"dry_run\": ...}")

    subparsers.add_parser("verify-restore", parents=[config_parent], help="Run DBCC CHECKDB against the restored database.")
    subparsers.add_parser(
        "list-restores",
        parents=[config_parent],
        help="List configured restore IDs with their source and target IPs (reads restore_config.json; no secrets needed).",
    )
    return parser.parse_args(argv)


def _format_restore_list(
    restore_configs: list[BackupRestoreConfig],
    script_jobs: list | None = None,
) -> str:
    """Human/Telegram-friendly listing of the restore_ids that can actually be run.

    Both kinds of restore belong here. An entry drives either the SMB + sqlcmd path or a
    script (Oracle, PostgreSQL, the SQL Server container drill), and listing only the first
    kind hid every restore this deployment actually runs - the reply named three ids, two of
    them disabled, while the five live ones went unmentioned.
    """
    configs, hidden_configs = active_only(restore_configs)
    scripts, hidden_scripts = active_only(script_jobs or [])
    total = len(configs) + len(scripts)
    if not total:
        note = hidden_note(hidden_configs + hidden_scripts, noun="entry")
        return "No active restore entries in restore_config.json." + (f"\n{note}" if note else "")

    lines = [f"Restore IDs ({total}):"]
    for cfg in configs:
        lines.append(f"- {cfg.restore_id}")
        lines.append("    smb restore")
        lines.append(f"    source: {cfg.prod_smb_credential_target or '?'}")
        lines.append(f"    target: {cfg.vm_credential_target or '?'}")
    for job in scripts:
        lines.append(f"- {job.restore_id}")
        lines.append(f"    {job.db_type} script restore on {job.server_id}")
        lines.append(f"    target: {job.target_container or '?'}")
    note = hidden_note(hidden_configs + hidden_scripts, noun="entry")
    if note:
        lines.extend(["", note])
    return "\n".join(lines)


def _format_backup_list(jobs: list) -> str:
    """Human/Telegram-friendly listing of backup_id + engine + level + schedule.

    What an operator needs before typing `spbot_backup <id>`: which ids exist, what each one
    backs up, and whether its level is fixed or derived - because `--backup-type` only makes
    sense for the derived ones.
    """
    active, hidden = active_only(jobs)
    if not active:
        note = hidden_note(hidden, noun="backup")
        return "No active backup entries in restore_config.json." + (f"\n{note}" if note else "")
    lines = [f"Backup IDs ({len(active)}):"]
    for job in sorted(active, key=lambda j: j.backup_id):
        window = job.time_window
        if window.from_hour is not None:
            when = f"{window.from_hour:02d}-{window.to_hour:02d}h"
        elif window.repeat_interval:
            when = f"every {window.repeat_interval // 60}m"
        else:
            when = "run-once"
        # A log/archive job has no full-vs-diff choice, so saying "auto (Sun=full...)" there
        # would invite --backup-type on a job that cannot take one.
        if job.job in ("archivelog", "wal", "log"):
            level = f"{job.job} (no full/diff level)"
        else:
            level = job.env.get("BACKUP_LEVEL") or "auto (Sun=full, else diff)"
        encrypted = "  [encrypted]" if job.env_secrets.get("BACKUP_ENCRYPTION_PASSWORD") else ""
        lines.append(f"- {job.backup_id}{encrypted}")
        lines.append(f"    {job.db_type} {job.server_id}")
        lines.append(f"    level: {level} | schedule: {when}")
    lines.append("")
    lines.append("Run one:  /spbot_backup <backup_id> <full|diff|log|->")
    lines.append("Use - to let the schedule's own rule pick the level.")
    note = hidden_note(hidden, noun="backup")
    if note:
        lines.append(note)
    return "\n".join(lines)


def _parse_env_overrides(values: list[str] | None) -> dict[str, str]:
    """Parse repeated ``--env KEY=VALUE`` into a dict."""
    overrides: dict[str, str] = {}
    for item in values or []:
        name, sep, value = str(item).partition("=")
        if not sep or not name.strip():
            raise ValueError(f"--env expects KEY=VALUE, got: {item!r}")
        overrides[name.strip()] = value
    return overrides


def _default_log_scope(command: str) -> str:
    """Long-running, separately scheduled commands get their own runtime log file."""
    if command == "restore-workflow":
        return "restore_workflow"
    if command in ("backup", "workflow"):
        return "backup"
    return "backup_restore"


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    set_key_env(getattr(args, "key", None), getattr(args, "key_base64", None))
    logger = None
    app_config = None
    start_monotonic = time.monotonic()
    started_at = utc_now_text()
    pit_utc = None
    _rw_meta: dict[str, object] = {}
    # The restore ids this invocation is working on. Carried into every event (START, END and
    # the failure path) so no message about a run can be published without naming it — and so
    # a failure that happens before restore_configs resolve still says which ids it meant.
    _id_meta: dict[str, object] = {}
    # Bound here, not where the entries are resolved: the failure path below reads it, and a
    # command that fails *before* resolving its entries (an unknown restore_id, a bad config)
    # would otherwise die with UnboundLocalError and hide the real error.
    _notify: NotifyConfig = BACKUP_RESTORE_NOTIFY_DEFAULTS
    try:
        # getattr, not args.config: the inherited parser leaves the attribute unset when the flag
        # was not given (see parse_args), which is what stops it clobbering the top-level value.
        resolved_config_path = str(resolve_config_path("backup_restore", getattr(args, "config", None)))
        # list-restores returns early — before patch_stdout — so the listing reaches
        # real stdout (captured by the Telegram cli_execute caller), needs no key,
        # and does not require a single resolved restore entry.
        if args.command == "restore-by-id":
            # The app reads the config and decrypts; db_ops.common performs. This is the seam.
            import json as _json

            from db_ops.backup_restore.restore_by_id import restore_by_id
            from db_ops.lib import response as _response

            # This branch returns before the block that brackets every other subcommand, so it
            # used to emit nothing at all: a restore driven from here - by an operator or by a
            # Telegram command - was completely silent, while the same restore through the
            # scheduler reported four events. Whoever starts a run brackets it, so this one
            # brackets its own.
            _rbid_app_config = load_config(resolved_config_path)
            _rbid_id = str((_json.loads(args.request) or {}).get("restore_id") or "")
            _rbid_meta = {"restore_id": _rbid_id}
            _rbid_started = utc_now_text()
            _rbid_notify = _restore_entry_notify(resolved_config_path, _rbid_id)

            def _rbid_event(phase: str, message: str, extra: dict[str, object] | None = None,
                            level: str = "logging", error_text: str | None = None) -> None:
                emit_backup_restore_event(
                    app_config=_rbid_app_config, command="restore-by-id", phase=phase,
                    level=level, message=message, started_at=_rbid_started,
                    error_text=error_text, metadata={**_rbid_meta, **(extra or {})},
                    notify=_rbid_notify,
                )

            _rbid_event("START", f"Restore {_rbid_id} started.")
            try:
                payload = _json.loads(args.request)
                data = restore_by_id(payload, key=getattr(args, "key", None),
                                     key_base64=getattr(args, "key_base64", None),
                                     on_phase=lambda p, m, e=None: _rbid_event(p, m, e))
                result = _response.ok("restore-by-id",
                                      message=f"{data['restore_id']} restored ({data['db_type']}).",
                                      data=data)
                _rbid_event("END", f"Restore {_rbid_id} finished: done.")
            except Exception as exc:  # noqa: BLE001 - reported as JSON, like the common commands.
                result = _response.fail("restore-by-id", str(exc))
                _rbid_event("ERROR", f"Restore {_rbid_id} FAILED.", level="error",
                            error_text=str(exc)[-2000:])
            sys.stdout.write(_json.dumps(result, ensure_ascii=False, default=str) + chr(10))
            return _response.exit_code(result)

        if args.command == "list-backups":
            sys.stdout.write(_format_backup_list(load_backup_jobs(resolved_config_path)) + "\n")
            return 0
        if args.command == "list-restores":
            sys.stdout.write(_format_restore_list(
                load_restore_configs(resolved_config_path),
                load_script_restores(resolved_config_path),
            ) + "\n")
            return 0
        app_config = load_config(resolved_config_path)
        log_scope = os.getenv(LOG_SCOPE_ENV_VAR) or _default_log_scope(args.command)
        patch_stdout(app_config.log_dir / f"{log_scope}_runtime.log", app_name=log_scope, sanitizer=sanitize_text)
        # backup returns early: it is driven by backup_restore.backups, not by the restore
        # entries the rest of main() resolves, and it must run even with no restores configured.
        if args.command == "workflow":
            logger = setup_app_logger(app_config, app_name=log_scope, log_scope=log_scope,
                                      enable_telegram_alerts=False, enable_console=False)
            log_function_call(logger, function_name="backup_restore.workflow")
            summary = run_workflow(
                app_config=app_config,
                config_path=resolved_config_path,
                logger=logger,
                backup_id=getattr(args, "backup_id", None),
                job_name=getattr(args, "job", None),
                restore_id=getattr(args, "restore_id", None),
                key=getattr(args, "key", None),
                key_base64=getattr(args, "key_base64", None),
                dry_run=bool(getattr(args, "dry_run", False)),
                force=bool(getattr(args, "force", False)),
                env_overrides=_parse_env_overrides(getattr(args, "env", None)),
                backup_type=getattr(args, "backup_type", None),
                copy_hours=int(getattr(args, "copy_hours", 24)),
                delete_hours=getattr(args, "delete_hours", None),
                skip_backup=bool(getattr(args, "skip_backup", False)),
                skip_restore=bool(getattr(args, "skip_restore", False)),
            )
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            return 1 if summary.get("failed") else 0
        if args.command == "backup":
            logger = setup_app_logger(app_config, app_name=log_scope, log_scope=log_scope,
                                      enable_telegram_alerts=False, enable_console=False)
            log_function_call(logger, function_name="backup_restore.backup")
            summary = run_backup(
                app_config=app_config,
                config_path=resolved_config_path,
                logger=logger,
                backup_id=getattr(args, "backup_id", None),
                job_name=getattr(args, "job", None),
                key=getattr(args, "key", None),
                key_base64=getattr(args, "key_base64", None),
                dry_run=bool(getattr(args, "dry_run", False)),
                force=bool(getattr(args, "force", False)),
                env_overrides=_parse_env_overrides(getattr(args, "env", None)),
                backup_type=getattr(args, "backup_type", None),
            )
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            return 1 if summary.get("failed") else 0
        if args.command == "prune-backups":
            from db_ops.backup_restore.prune import run_prune

            logger = setup_app_logger(app_config, app_name=log_scope, log_scope=log_scope,
                                      enable_telegram_alerts=False, enable_console=False)
            log_function_call(logger, function_name="backup_restore.prune")
            summary = run_prune(
                app_config=app_config,
                config_path=resolved_config_path,
                logger=logger,
                backup_id=getattr(args, "backup_id", None),
                job_name=getattr(args, "job", None),
                retention_days=getattr(args, "retention_days", None),
                mode=getattr(args, "mode", "age"),
                key=getattr(args, "key", None),
                key_base64=getattr(args, "key_base64", None),
                apply=bool(getattr(args, "apply", False)),
            )
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            return 1 if summary.get("failed") else 0
        restore_configs = load_restore_configs(resolved_config_path)
        # restore_id: --restore-id flag takes precedence; positional restore_id_pos is the shorthand.
        restore_id_filter = getattr(args, "restore_id", None) or getattr(args, "restore_id_pos", None)
        if restore_id_filter:
            restore_configs = [config for config in restore_configs if config.restore_id == restore_id_filter]
            if not restore_configs:
                # A script-driven entry (one that declares `script`: the Oracle, PostgreSQL and
                # container-to-container SQL Server drills) is not in this list at all — it is
                # run by `workflow`, through run_scheduled_restores. Reporting it as "no entry
                # found" sent an operator looking for a config problem that did not exist, on an
                # entry sitting in the file they were reading. Name the command that runs it.
                script_ids = {job.restore_id for job in load_script_restores(resolved_config_path)}
                if restore_id_filter in script_ids:
                    raise ValueError(
                        f"restore_id={restore_id_filter} is a script-driven restore (it declares "
                        f"`script` in restore_config.json), which `{args.command}` does not run. "
                        f"Use: python -m db_ops.backup_restore.cli workflow "
                        f"--restore-id {restore_id_filter} [--force]"
                    )
                raise ValueError(f"No backup_restore entry found with restore_id={restore_id_filter}.")
            if len(restore_configs) > 1:
                raise ValueError(
                    f"Ambiguous config: {len(restore_configs)} entries share restore_id={restore_id_filter!r}. "
                    f"Each restore_id must be unique in restore_config.json."
                )
        elif getattr(args, "source_id", None):
            restore_configs = [config for config in restore_configs if config.source_id == args.source_id]
            if not restore_configs:
                raise ValueError(f"No backup_restore source found with source_id={args.source_id}.")
        if args.command == "restore-workflow" and not restore_id_filter:
            restore_configs = [config for config in restore_configs if config.active]
            if not restore_configs:
                raise ValueError("No active backup_restore workflow entries found.")
        # Apply --execution-mode CLI override (per-run only, does not persist to config file).
        execution_mode_override = getattr(args, "execution_mode", None)
        if execution_mode_override is not None:
            restore_configs = [dataclasses.replace(config, execution_mode=execution_mode_override) for config in restore_configs]
        restore_config = restore_configs[0]
        _id_meta = {"restore_ids": [config.restore_id for config in restore_configs]}
        if len(restore_configs) == 1:
            _id_meta["restore_id"] = restore_config.restore_id
        # These events cover every selected entry, so a level is only overridden when they all
        # agree on it — see merge_notify_configs.
        _notify = merge_notify_configs([config.notify for config in restore_configs])
        logger = setup_app_logger(app_config, app_name=log_scope, log_scope=log_scope, enable_telegram_alerts=False, enable_console=False)
        log_function_call(logger, function_name=f"backup_restore.{args.command}")
        if args.command == "restore-workflow" and getattr(args, "point_in_time", None):
            pit_utc = parse_point_in_time(args.point_in_time)
        restore_mode = "POINT_IN_TIME" if pit_utc is not None else "LATEST"
        if args.command == "restore-workflow":
            _mappings = [_build_restore_mapping(c) for c in restore_configs]
            _rw_meta = {
                "restore_mode": restore_mode,
                "restore_count": len(restore_configs),
                "mappings": _mappings,
            }
            if len(restore_configs) == 1:
                _cfg0 = restore_configs[0]
                _rw_meta["restore_id"] = _cfg0.restore_id
                _rw_meta["target_id"] = _cfg0.target_id
                _rw_meta["target_host"] = _cfg0.vm_credential_target
            if pit_utc is not None:
                _rw_meta["point_in_time_utc"] = pit_utc.isoformat()
                _rw_meta["point_in_time_original"] = getattr(args, "point_in_time", None)
        if args.command == "restore-workflow":
            if len(restore_configs) == 1:
                _cfg = restore_configs[0]
                _start_message = f"started restore_id={_cfg.restore_id}"
                _start_meta: dict[str, object] = {
                    "restore_id": _cfg.restore_id,
                    "source_id": _cfg.source_id,
                    "target_id": _cfg.target_id,
                    "target_host": _cfg.vm_credential_target,
                    "target_os_type": _cfg.vm_platform,
                    "execution_mode": _cfg.execution_mode,
                    **_rw_meta,
                }
            else:
                _start_message = f"started restore_count={len(restore_configs)}"
                _start_meta = dict(_rw_meta)
                _start_meta["restore_ids"] = [c.restore_id for c in restore_configs]
        elif len(restore_configs) == 1:
            _cfg = restore_configs[0]
            _start_message = f"started restore_id={_cfg.restore_id}"
            _start_meta = {
                "restore_id": _cfg.restore_id,
                "source_id": _cfg.source_id,
                "target_id": _cfg.target_id,
                "target_host": _cfg.vm_credential_target,
                "target_os_type": _cfg.vm_platform,
                "execution_mode": _cfg.execution_mode,
            }
        else:
            _start_message = f"started sources={len(restore_configs)}"
            _start_meta = {
                "restore_ids": [config.restore_id for config in restore_configs],
                "source_ids": [config.source_id for config in restore_configs],
                "target_ids": [config.target_id for config in restore_configs],
            }
        emit_backup_restore_event(
            app_config=app_config,
            command=args.command,
            phase="START",
            level="logging",
            message=_start_message,
            logger=logger,
            started_at=started_at,
            metadata={**_id_meta, **_start_meta},
            notify=_notify,
        )

        if args.command == "copy-backup":
            source_outputs = []
            for config in restore_configs:
                if args.hours is not None:
                    config = dataclasses.replace(config, copy_recent_hours=int(args.hours))
                result = run_copy_backup(config, logger=logger, force=bool(getattr(args, "force", False)))
                source_outputs.append(
                    {
                        "source_id": config.source_id,
                        "returncode": result.returncode,
                        "prod_backup_share": str(config.prod_backup_share),
                        "vm_import_unc": str(config.vm_import_unc),
                        "copy_recent_hours": config.copy_recent_hours,
                        "copy_file_patterns": list(config.copy_file_patterns),
                        "files_considered": result.files_considered,
                        "copied": result.copied,
                        "skipped": result.skipped,
                    }
                )
            output = {
                "status": "SUCCESS",
                "sources_considered": len(source_outputs),
                "sources": source_outputs,
            }
        elif args.command == "delete-backup":
            source_outputs = []
            for config in restore_configs:
                config = dataclasses.replace(config, copy_recent_hours=int(args.hours))
                result = run_delete_backup(config, logger=logger)
                source_outputs.append(
                    {
                        "source_id": config.source_id,
                        "target_id": config.target_id,
                        "returncode": result.returncode,
                        "target_backup_dir": str(config.vm_import_unc),
                        "delete_older_than_hours": result.delete_older_than_hours,
                        "files_considered": result.files_considered,
                        "deleted": result.deleted,
                    }
                )
            output = {
                "status": "SUCCESS",
                "sources_considered": len(source_outputs),
                "sources": source_outputs,
            }
        elif args.command == "import-certificate":
            from db_ops.backup_restore.certificate import ensure_source_certificate

            source_outputs = [
                ensure_source_certificate(config=config, dry_run=bool(args.dry_run), logger=logger)
                for config in restore_configs
            ]
            output = {
                "status": "DRY_RUN" if args.dry_run else "SUCCESS",
                "sources_considered": len(source_outputs),
                "sources": source_outputs,
            }
        elif args.command == "restore-latest":
            if args.backup_file:
                selected_config = _find_config_for_backup(Path(args.backup_file), restore_configs)
                output = run_restore_database(
                    config=selected_config,
                    db_ops_config=app_config,
                    backup_file=Path(args.backup_file),
                    dry_run=bool(args.dry_run),
                    logger=logger,
                )
            elif args.database:
                database = DatabaseRestoreMapping(
                    source_database=args.database,
                    target_database=args.target_database or "",
                )
                output = run_restore_database(
                    config=restore_config,
                    db_ops_config=app_config,
                    database=database,
                    dry_run=bool(args.dry_run),
                    logger=logger,
                )
            else:
                source_outputs = [
                    run_restore_all_latest(config=config, db_ops_config=app_config, dry_run=bool(args.dry_run), logger=logger)
                    for config in restore_configs
                ]
                # An aggregate must not be greener than the sources it aggregates - the same rule
                # run_restore_instance now applies to its databases, for the same reason.
                aggregate_status = "DRY_RUN" if args.dry_run else (
                    "SUCCESS"
                    if all(str(source.get("status")) in {"SUCCESS", "DRY_RUN"}
                           for source in source_outputs)
                    else "FAILED"
                )
                output = {
                    "status": aggregate_status,
                    "overall_status": aggregate_status,
                    "sources_considered": len(source_outputs),
                    **_summarize_restore_sources(source_outputs),
                    "sources": source_outputs,
                }
        elif args.command in {"restore-full", "restore-diff", "restore-log"}:
            selected_config = _find_config_for_backup(Path(args.backup_file), restore_configs) if args.backup_file else restore_config
            database = DatabaseRestoreMapping(
                source_database=args.database,
                target_database=args.target_database or "",
            ) if args.database else None
            selected_backup = Path(args.backup_file) if args.backup_file else get_latest_full_backup_for_database(selected_config, database)
            candidate = build_restore_candidate(selected_backup, selected_config, database=database)
            if args.command == "restore-full":
                output = run_restore_full(config=selected_config, candidate=candidate, logger=logger)
            elif args.command == "restore-diff":
                output = run_restore_diff(config=selected_config, candidate=candidate, logger=logger)
            else:
                output = run_restore_log(config=selected_config, candidate=candidate, logger=logger)
        elif args.command == "restore-workflow":
            def _rw_announce(phase: str, message: str, extra: dict[str, object] | None = None) -> None:
                # Same events the scheduler emits. A run reports the same way whether an operator
                # started it or the daemon did - that is the whole contract.
                emit_backup_restore_event(
                    app_config=app_config, command=args.command, phase=phase, level="logging",
                    message=message, logger=logger, started_at=utc_now_text(),
                    metadata={**_id_meta, **_rw_meta, **(extra or {})}, notify=_notify,
                )

            # Instance metadata replays here too, not only on the scheduled path. It used to live
            # solely in run_scheduled_restores, so `restore-workflow` silently skipped the logins,
            # roles and Agent jobs and said nothing about having skipped them - the same
            # "behaviour depends on how you invoked it" asymmetry that left restore-by-id mute.
            from db_ops.backup_restore.workflow import replay_engine_phase
            from db_ops.lib import instance_bundle as _instance

            _meta_lines, _meta_reports = "", {}
            for _cfg in restore_configs:
                _line, _report = replay_engine_phase(
                    _cfg, phase=_instance.PRE_DATABASE, announce=_rw_announce)
                _meta_lines += _line
                _meta_reports.update(_report)

            output = run_restore_workflow(
                restore_configs=restore_configs,
                app_config=app_config,
                copy_hours=int(args.copy_hours),
                delete_hours=args.delete_hours,
                dry_run=bool(args.dry_run),
                force=bool(args.force),
                logger=logger,
                point_in_time_utc=pit_utc,
                on_phase=_rw_announce,
            )

            # Only after a restore that worked: Agent job steps name databases that have to exist.
            for _cfg in restore_configs:
                _line, _report = replay_engine_phase(
                    _cfg, phase=_instance.POST_DATABASE, announce=_rw_announce)
                _meta_lines += _line
                _meta_reports.update(_report)
            if _meta_reports:
                output["server_metadata"] = _meta_reports
        elif args.command == "verify-restore":
            source_outputs = []
            for config in restore_configs:
                result = run_verify_restore(config)
                source_outputs.append({"source_id": config.source_id, "returncode": result.returncode})
            output = {
                "status": "SUCCESS",
                "sources_considered": len(source_outputs),
                "sources": source_outputs,
            }
        else:
            raise ValueError(f"Unknown command: {args.command}")

        duration_ms = int((time.monotonic() - start_monotonic) * 1000)
        safe_output = sanitize_value(output)
        _finished_rid = f"restore_id={_rw_meta['restore_id']} " if _rw_meta.get("restore_id") else ""
        log_event(logger, level="logging", message=sanitize_text(f"{_finished_rid}backup_restore.{args.command} finished status={output.get('status')}"))
        summary_output = _summarize_command_output(args.command, safe_output)
        end_level, end_message, end_metadata = _build_end_event(command=args.command, output=summary_output)
        if args.command == "restore-workflow" and _rw_meta:
            end_metadata.update(_rw_meta)
            end_metadata["restore_mode"] = summary_output.get("restore_mode", restore_mode)
            if summary_output.get("point_in_time_utc"):
                end_metadata["point_in_time_utc"] = summary_output["point_in_time_utc"]
            for _field in ("databases_considered", "success", "failed", "skipped", "duration_seconds"):
                if summary_output.get(_field) is not None:
                    end_metadata[_field] = summary_output[_field]
            if summary_output.get("per_restore_results"):
                end_metadata["per_restore_results"] = summary_output["per_restore_results"]
        emit_backup_restore_event(
            app_config=app_config,
            command=args.command,
            phase="END",
            level=end_level,
            message=sanitize_text(end_message),
            logger=logger,
            started_at=started_at,
            finished_at=utc_now_text(),
            duration_ms=duration_ms,
            metadata={**_id_meta, **end_metadata},
            notify=_notify,
        )
        if args.command == "restore-workflow":
            print(_format_restore_workflow_stdout(summary_output))
        else:
            print(json.dumps(safe_output, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - command-line failure path.
        if logger:
            _failed_command = getattr(args, "command", "cli")
            _, _failed_id = resolve_run_id(_failed_command, _id_meta)
            log_event(logger, level="critical", message=sanitize_text(
                f"backup_restore.{_failed_command} failed restore_id={_failed_id}: {type(exc).__name__}: {exc}"))
        if app_config:
            error_metadata: dict[str, object] = {**_id_meta, **_rw_meta}
            if isinstance(exc, RestoreWorkflowError):
                error_metadata.update(exc.metadata)
            error_metadata.update(_exception_metadata(exc, app_config=app_config, log_scope=os.getenv(LOG_SCOPE_ENV_VAR) or "backup_restore"))
            emit_backup_restore_event(
                app_config=app_config,
                command=getattr(args, "command", "cli"),
                phase="ERROR",
                level="critical",
                message=sanitize_text(f"failed: {exc}"),
                logger=logger,
                started_at=started_at,
                finished_at=utc_now_text(),
                duration_ms=int((time.monotonic() - start_monotonic) * 1000),
                error_text=sanitize_text(f"{type(exc).__name__}: {exc}"),
                metadata=error_metadata or None,
                notify=_notify,
            )
        print(sanitize_text(f"ERROR: {exc}"), file=sys.stderr)
        return 1


def _restore_entry_notify(config_path: str, restore_id: str) -> NotifyConfig:
    """The entry's own notify block, so a manual run reports where the scheduled one does.

    Looked up rather than defaulted: `restore` and `backup` go to different chats, and falling
    back to the app defaults would put a hand-run restore in whichever chat the defaults name -
    quietly, and only for the runs somebody is watching most closely.
    """
    for loader in (load_script_restores, load_restore_configs):
        try:
            for entry in loader(config_path):
                if getattr(entry, "restore_id", "") == restore_id:
                    return entry.notify
        except Exception:  # noqa: BLE001 - a bad entry must not stop the run reporting itself.
            continue
    return BACKUP_RESTORE_NOTIFY_DEFAULTS


def _find_config_for_backup(backup_file: Path, configs: list[BackupRestoreConfig]) -> BackupRestoreConfig:
    for config in configs:
        try:
            backup_file.relative_to(config.vm_import_unc)
        except ValueError:
            continue
        return config
    return configs[0]


def _exception_metadata(exc: Exception, *, app_config: object, log_scope: str) -> dict[str, object]:
    text = sanitize_text(str(exc))
    metadata: dict[str, object] = {
        "exception_type": type(exc).__name__,
        "exception_message": text,
        "error_tail": summarize_error_tail(text),
    }
    log_dir = getattr(app_config, "log_dir", None)
    if log_dir is not None:
        metadata["log_file_path"] = str(Path(log_dir) / f"{log_scope}_runtime.log")
    stdout_tail = _section_tail(text, "stdout:")
    stderr_tail = _section_tail(text, "stderr:")
    if stdout_tail:
        metadata["stdout_tail"] = stdout_tail
    if stderr_tail:
        metadata["stderr_tail"] = stderr_tail
    sql_error = _sql_error_text(text)
    if sql_error:
        metadata["sql_error_text"] = sql_error
    return metadata


def _section_tail(text: str, marker: str, *, max_lines: int = 12) -> str:
    index = text.lower().find(marker.lower())
    if index < 0:
        return ""
    section = text[index + len(marker):]
    for next_marker in ("stdout:", "stderr:"):
        next_index = section.lower().find(next_marker)
        if next_index > 0:
            section = section[:next_index]
    lines = [line for line in section.strip().splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:])


def _sql_error_text(text: str, *, max_lines: int = 12) -> str:
    lines = []
    for line in text.splitlines():
        lowered = line.lower()
        if (
            lowered.startswith("msg ")
            or "restore " in lowered
            or "alter database" in lowered
            or "sql server" in lowered
        ):
            lines.append(line)
    return "\n".join(lines[-max_lines:])


def run_restore_workflow(
    *,
    restore_configs: list[BackupRestoreConfig],
    app_config: object,
    copy_hours: int = 24,
    delete_hours: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    logger: object | None = None,
    point_in_time_utc: object = None,
    on_phase: object = None,
) -> dict[str, object]:
    """``on_phase(phase, message, extra)`` reports the copy boundaries, when a caller supplies it.

    The engine path copies inside this function, so without it a restore went ``START`` -> silence
    -> ``END`` while the script path (which copies in ``restore_by_id``) announced both. Same
    operation, different reporting, decided by which branch happened to run it.
    """
    # Optional and never fatal: reporting is not the restore. `events.announce` is the one copy
    # of that rule — this was a fourth (2026-08-16).
    def _say(phase: str, message: str, extra: dict[str, object] | None = None) -> None:
        announce(on_phase, phase, message, extra)

    restore_mode = "POINT_IN_TIME" if point_in_time_utc is not None else "LATEST"
    mappings = [_build_restore_mapping(c) for c in restore_configs]
    summary: dict[str, object] = {
        "status": "RUNNING",
        "overall_workflow_status": "RUNNING",
        "restore_mode": restore_mode,
        "point_in_time_utc": point_in_time_utc.isoformat() if point_in_time_utc is not None and hasattr(point_in_time_utc, "isoformat") else None,
        "restore_count": len(restore_configs),
        "mappings": mappings,
        "per_restore_results": None,
        "copy-backup": None,
        "restore-latest": None,
        "delete-backup": None,
    }
    _rw_restore_id = restore_configs[0].restore_id if len(restore_configs) == 1 else ""
    _rid = f"restore_id={_rw_restore_id} " if _rw_restore_id else ""
    workflow_start = time.monotonic()
    if logger:
        log_event(logger, level="logging", message=f"{_rid}restore-workflow start restore_mode={restore_mode}")
    try:
        # Preflight: validate (and if possible prepare) each target before touching any files.
        # Windows: checks UNC share, tries WinRM create, then admin-share fallback.
        # Linux: no-op — SSH/SFTP handles dir creation lazily.
        # run_target_preflight returns a patched config when admin-share fallback is used
        # (vm_import_unc overridden to admin share path). Scoped per restore_id.
        _preflighted: list[BackupRestoreConfig] = []
        with _workflow_phase(logger, f"{_rid}restore-workflow preflight", summary=summary, current_phase="preflight", restore_count=len(restore_configs)):
            for config in restore_configs:
                override = run_target_preflight(config, logger=logger)
                _preflighted.append(override if override is not None else config)
        restore_configs = _preflighted

        copy_window_start = None
        copy_window_end = None
        if point_in_time_utc is not None:
            copy_window_end = point_in_time_utc
            copy_window_start = point_in_time_utc - dt.timedelta(hours=copy_hours)
        with _workflow_phase(
            logger,
            f"{_rid}restore-workflow calculating-copy-window",
            summary=summary,
            current_phase="calculating-copy-window",
            restore_mode=restore_mode,
            copy_hours=copy_hours,
            window_start_utc=copy_window_start.isoformat() if copy_window_start is not None else None,
            window_end_utc=copy_window_end.isoformat() if copy_window_end is not None else None,
        ):
            pass
        copy_outputs = []
        _say("COPY_START", f"Restore {restore_configs[0].restore_id or restore_configs[0].source_id}: "
                           f"copy started ({len(restore_configs)} source(s)).")
        with _workflow_phase(logger, f"{_rid}restore-workflow copy-backup", summary=summary, current_phase="copy-backup", source_count=len(restore_configs)):
            for config in restore_configs:
                step_config = dataclasses.replace(
                    config,
                    copy_recent_hours=copy_hours,
                    copy_window_start_utc=copy_window_start,
                    copy_window_end_utc=copy_window_end,
                )
                result = run_copy_backup(step_config, logger=logger, force=force)
                copy_outputs.append(
                    {
                        "source_id": step_config.source_id,
                        "returncode": result.returncode,
                        "copy_recent_hours": step_config.copy_recent_hours,
                        "copy_window_start_utc": copy_window_start.isoformat() if copy_window_start is not None else None,
                        "copy_window_end_utc": copy_window_end.isoformat() if copy_window_end is not None else None,
                        "files_considered": result.files_considered,
                        "copied": result.copied,
                        "skipped": result.skipped,
                    }
                )
                if result.returncode != 0:
                    raise RuntimeError(f"copy-backup failed for source_id={step_config.source_id} returncode={result.returncode}")
                if result.files_considered == 0:
                    raise RuntimeError(
                        "copy-backup selected no files "
                        f"for source_id={step_config.source_id} restore_id={step_config.restore_id or 'unknown'} "
                        f"window_start_utc={copy_window_start.isoformat() if copy_window_start is not None else 'now-minus-hours'} "
                        f"window_end_utc={copy_window_end.isoformat() if copy_window_end is not None else 'unbounded'}"
                    )
        summary["copy-backup"] = {"status": "SUCCESS", "sources": copy_outputs}
        _copied = sum(int(o.get("copied") or 0) for o in copy_outputs)
        _skipped = sum(int(o.get("skipped") or 0) for o in copy_outputs)
        _say("COPY_DONE",
             f"Restore {restore_configs[0].restore_id or restore_configs[0].source_id}: "
             f"copy finished - {_copied} file(s), {_skipped} already present.",
             {"copied": _copied, "skipped": _skipped})

        with _workflow_phase(logger, f"{_rid}restore-workflow restore-preparation", summary=summary, current_phase="restore-preparation", source_count=len(restore_configs)):
            restore_inputs = list(restore_configs)
        with _workflow_phase(logger, f"{_rid}restore-workflow restore-execution", summary=summary, current_phase="restore-execution", source_count=len(restore_inputs)):
            restore_outputs = [
                run_restore_all_latest(config=config, db_ops_config=app_config, dry_run=dry_run, logger=logger, point_in_time_utc=point_in_time_utc)
                for config in restore_inputs
            ]
        summary["restore-latest"] = {
            "status": "DRY_RUN" if dry_run else "SUCCESS",
            "sources": restore_outputs,
        }
        restore_counts = _count_restore_statuses(restore_outputs)
        summary.update(restore_counts)
        per_restore_results = _build_per_restore_results(restore_configs, restore_outputs)
        summary["per_restore_results"] = per_restore_results

        delete_outputs = []
        with _workflow_phase(logger, f"{_rid}restore-workflow delete-backup", summary=summary, current_phase="delete-backup", source_count=len(restore_configs), retention_hours=delete_hours):
            for config in restore_configs:
                # Hours, because run_delete_backup speaks hours; the config is in seconds so a
                # single field can express both this and the script path without a unit clash.
                seconds = int(config.target_retention_seconds or 0)
                entry_hours = max(1, seconds // 3600) if seconds else 0
                step_config = dataclasses.replace(
                    config, copy_recent_hours=delete_hours if delete_hours is not None else entry_hours
                )
                result = run_delete_backup(step_config, logger=logger)
                delete_outputs.append(
                    {
                        "source_id": step_config.source_id,
                        "returncode": result.returncode,
                        "delete_older_than_hours": result.delete_older_than_hours,
                        "files_considered": result.files_considered,
                        "deleted": result.deleted,
                        "skipped": getattr(result, "skipped", 0),
                    }
                )
                if result.returncode != 0:
                    raise RuntimeError(f"delete-backup failed for source_id={step_config.source_id} returncode={result.returncode}")
        summary["delete-backup"] = {"status": "SUCCESS", "sources": delete_outputs}
        summary["status"] = "SUCCESS"
        summary["overall_workflow_status"] = "SUCCESS"
        summary["duration_seconds"] = round(time.monotonic() - workflow_start, 3)
        if logger:
            log_event(logger, level="logging", message=f"{_rid}restore-workflow completion status=SUCCESS elapsed_seconds={summary['duration_seconds']}")
            log_event(logger, level="logging", message=f"{_rid}{_format_restore_workflow_stdout(summary)}")
        return summary
    except Exception as exc:
        summary["status"] = "FAILED"
        summary["overall_workflow_status"] = "FAILED"
        summary["duration_seconds"] = round(time.monotonic() - workflow_start, 3)
        summary["exception_type"] = type(exc).__name__
        summary["exception_message"] = sanitize_text(str(exc))
        summary["error_tail"] = summarize_error_tail(str(exc))
        if logger:
            log_event(logger, level="critical", message=sanitize_text(f"{_rid}restore-workflow failed error={type(exc).__name__}: {exc}"))
        raise RestoreWorkflowError(str(exc), metadata=summary) from exc


class _workflow_phase:
    def __init__(
        self,
        logger: object | None,
        phase: str,
        *,
        summary: dict[str, object] | None = None,
        current_phase: str | None = None,
        **metadata: object,
    ) -> None:
        self.logger = logger
        self.phase = phase
        self.summary = summary
        self.current_phase = current_phase
        self.metadata = metadata
        self.started = 0.0

    def __enter__(self) -> "_workflow_phase":
        self.started = time.monotonic()
        if self.summary is not None and self.current_phase:
            self.summary["current_phase"] = self.current_phase
        if self.logger:
            log_event(self.logger, level="logging", message=f"{self.phase} start {_metadata_text(self.metadata)}".strip())
        return self

    def __exit__(self, exc_type: object, exc: object, _tb: object) -> None:
        elapsed = time.monotonic() - self.started
        status = "FAILED" if exc_type else "SUCCESS"
        if self.logger:
            log_event(
                self.logger,
                level="critical" if exc_type else "logging",
                message=f"{self.phase} end status={status} elapsed_seconds={elapsed:.3f} {_metadata_text(self.metadata)}".strip(),
            )


def _metadata_text(metadata: dict[str, object]) -> str:
    return " ".join(f"{key}={value}" for key, value in metadata.items() if value is not None)


def _summarize_restore_sources(source_outputs: list[dict[str, object]]) -> dict[str, object]:
    return {
        "selected_full_backup": [
            backup
            for source in source_outputs
            for backup in _as_list(source.get("selected_full_backup"))
        ],
        "selected_diff_backup": [
            backup
            for source in source_outputs
            for backup in _as_list(source.get("selected_diff_backup"))
        ],
        "selected_log_backups": [
            backup
            for source in source_outputs
            for backup in _as_list(source.get("selected_log_backups"))
        ],
        "skipped_backups": [
            skipped
            for source in source_outputs
            for skipped in _as_list(source.get("skipped_backups"))
        ],
        "per_database_restore_status": {
            database: status
            for source in source_outputs
            for database, status in (
                source.get("per_database_restore_status", {}).items()
                if isinstance(source.get("per_database_restore_status"), dict)
                else []
            )
        },
        "final_recovery_status": {
            database: status
            for source in source_outputs
            for database, status in (
                source.get("final_recovery_status", {}).items()
                if isinstance(source.get("final_recovery_status"), dict)
                else []
            )
        },
        "final_recovery_model_status": {
            database: status
            for source in source_outputs
            for database, status in (
                source.get("final_recovery_model_status", {}).items()
                if isinstance(source.get("final_recovery_model_status"), dict)
                else []
            )
        },
    }


def _count_restore_statuses(source_outputs: list[dict[str, object]]) -> dict[str, int]:
    statuses: list[str] = []
    for source in source_outputs:
        if not isinstance(source, dict):
            continue
        per_database = source.get("per_database_restore_status")
        if isinstance(per_database, dict):
            statuses.extend(str(status) for status in per_database.values())
            continue
        if source.get("status"):
            statuses.append(str(source.get("status")))
    success = sum(1 for status in statuses if status in {"SUCCESS", "DRY_RUN"})
    skipped = sum(1 for status in statuses if status == "SKIPPED")
    failed = len(statuses) - success - skipped
    return {
        "databases_considered": len(statuses),
        "success": success,
        "failed": failed,
        "skipped": skipped,
    }


def _build_restore_mapping(config: BackupRestoreConfig) -> dict[str, object]:
    mapping: dict[str, object] = {
        "restore_id": config.restore_id,
        "source_id": config.source_id,
        "target_id": config.target_id,
    }
    if config.vm_credential_target:
        mapping["target_host"] = config.vm_credential_target
    if config.vm_platform:
        mapping["target_os_type"] = config.vm_platform
    return mapping


def _build_per_restore_results(
    configs: list[BackupRestoreConfig],
    restore_outputs: list[dict[str, object]],
) -> list[dict[str, object]]:
    results = []
    for config, output in zip(configs, restore_outputs):
        counts = _count_single_restore_statuses(output)
        per: dict[str, object] = {
            "restore_id": config.restore_id,
            "source_id": config.source_id,
            "target_id": config.target_id,
        }
        if config.vm_credential_target:
            per["target_host"] = config.vm_credential_target
        per["status"] = str(output.get("status") or "UNKNOWN")
        per.update(counts)
        results.append(per)
    return results


def _count_single_restore_statuses(output: dict[str, object]) -> dict[str, int]:
    statuses: list[str] = []
    per_database = output.get("per_database_restore_status")
    if isinstance(per_database, dict):
        statuses.extend(str(s) for s in per_database.values())
    elif output.get("status"):
        statuses.append(str(output.get("status")))
    success = sum(1 for s in statuses if s in {"SUCCESS", "DRY_RUN"})
    skipped = sum(1 for s in statuses if s == "SKIPPED")
    failed = len(statuses) - success - skipped
    return {
        "databases_considered": len(statuses),
        "success": success,
        "failed": failed,
        "skipped": skipped,
    }


def _format_restore_workflow_stdout(output: dict[str, object]) -> str:
    parts = [
        "restore-workflow completed",
        f"status={output.get('status')}",
        f"restore_mode={output.get('restore_mode', 'LATEST')}",
    ]
    if output.get("restore_count") is not None:
        parts.append(f"restore_count={output['restore_count']}")
    parts.extend([
        f"databases_considered={output.get('databases_considered', 0)}",
        f"success={output.get('success', 0)}",
        f"failed={output.get('failed', 0)}",
        f"skipped={output.get('skipped', 0)}",
        f"duration_seconds={output.get('duration_seconds', 0)}",
    ])
    if output.get("point_in_time_utc"):
        parts.append(f"point_in_time_utc={output.get('point_in_time_utc')}")
    return sanitize_text(" ".join(parts))


def _summarize_command_output(command: str, output: dict[str, object]) -> dict[str, object]:
    if command == "restore-workflow":
        summary: dict[str, object] = {
            "status": output.get("status"),
            "overall_workflow_status": output.get("overall_workflow_status"),
            "restore_mode": output.get("restore_mode", "LATEST"),
            "restore_count": output.get("restore_count"),
            "mappings": output.get("mappings"),
            "databases_considered": output.get("databases_considered", 0),
            "success": output.get("success", 0),
            "failed": output.get("failed", 0),
            "skipped": output.get("skipped", 0),
            "duration_seconds": output.get("duration_seconds", 0),
            "per_restore_results": output.get("per_restore_results"),
        }
        if output.get("point_in_time_utc"):
            summary["point_in_time_utc"] = output.get("point_in_time_utc")
        return summary
    return output


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _build_end_event(*, command: str, output: dict[str, object]) -> tuple[str, str, dict[str, object]]:
    if command != "restore-latest":
        return (
            "logging",
            f"finished status={output.get('status')} sources={output.get('sources_considered')}",
            {"output": output},
        )

    restore_results = _collect_restore_results(output)
    total = len(restore_results)
    failed = [item for item in restore_results if _restore_result_has_error(item)]
    success_count = total - len(failed)
    level = "logging" if not failed else "critical"
    message = (
        f"finished status={output.get('status')} restore_success={success_count}/{total} "
        f"failed={len(failed)} sources={output.get('sources_considered')}"
    )
    return (
        level,
        message,
        {
            "output": output,
            "restore_success": success_count,
            "restore_total": total,
            "restore_failed": len(failed),
            "failed_databases": [
                {
                    "source_id": item.get("source_id"),
                    "database_name": item.get("database_name"),
                    "backup_file_on_vm": item.get("backup_file_on_vm"),
                    "status": item.get("status"),
                }
                for item in failed
            ],
        },
    )


def _collect_restore_results(output: dict[str, object]) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    if "database_name" in output:
        results.append(output)
    for source in output.get("sources", []) if isinstance(output.get("sources"), list) else []:
        if not isinstance(source, dict):
            continue
        for item in source.get("results", []) if isinstance(source.get("results"), list) else []:
            if isinstance(item, dict):
                results.append(item)
    return results


def _restore_result_has_error(result: dict[str, object]) -> bool:
    if str(result.get("status", "")).upper() not in {"SUCCESS", "DRY_RUN"}:
        return True
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    error_text = f"{stdout}\n{stderr}".lower()
    return any(
        marker in error_text
        for marker in (
            "restore database is terminating abnormally",
            "terminating abnormally",
            "incorrectly formed",
            "can not be read",
            "cannot be read",
            "the media family",
            "level 16",
            "level 17",
            "level 18",
            "level 19",
            "level 20",
            "msg 3013",
            "msg 3287",
        )
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
