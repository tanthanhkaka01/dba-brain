from __future__ import annotations

from dbabrain.lib import errors
import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dbabrain.backup_restore.backup import (
    BACKUP_TYPES,
    _load_secrets,
    load_backup_jobs,
)
from dbabrain.transport import common_cli
from dbabrain.lib import sql_instance
from dbabrain.backup_restore.restore_script import load_script_restores, unusable_reason
from dbabrain.lib.time_window import weekdays_text
from dbabrain.lib.backup_runbook import LOG_STREAM_JOBS
from dbabrain.lib.listing import active_only, hidden_note
# The backup's and the offload's messages are the workflow's (0.40.0, 1.374): a hand run goes through it too.
from dbabrain.backup_restore.workflow import run_backup, run_offloads, run_workflow
from dbabrain.backup_restore.config import (
    BACKUP_RESTORE_NOTIFY_DEFAULTS,
    BackupRestoreConfig,
    DatabaseRestoreMapping,
    load_restore_configs,
    merge_notify_configs,
)
from dbabrain.backup_restore.copy_backup import newest_backup_hint, run_copy_backup
from dbabrain.backup_restore.delete_backup import run_delete_backup
from dbabrain.backup_restore.preflight import PreflightError, run_target_preflight
from dbabrain.backup_restore import restore_messages
from dbabrain.backup_restore.events import announce, emit_backup_restore_event, resolve_run_id
from dbabrain.backup_restore.restore_database import (
    ChainRecord,
    parse_point_in_time,
    run_restore_all_latest,
    run_restore_database,
    summarize_error_tail,
)
from dbabrain.backup_restore.sanitize import sanitize_text, sanitize_value
from dbabrain.lib.notify import NotifyConfig
from dbabrain.lib.secret_text import add_key_argument, set_key_env
from dbabrain.lib.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from dbabrain.db.store import utc_now_text
from dbabrain.logging_ops import LOG_SCOPE_ENV_VAR, log_event, log_function_call, setup_app_logger
from dbabrain.logging_ops.runtime_stdout import patch_stdout


class RestoreWorkflowError(errors.OperationFailed):
    def __init__(self, message: str, *, metadata: dict[str, object]) -> None:
        super().__init__(message)
        self.metadata = metadata


def parse_args(argv: list[str]) -> argparse.Namespace:
    config_parent = argparse.ArgumentParser(add_help=False)
    # SUPPRESS for the same reason as the key flags below: the top-level parser registers
    # --config too, and a plain default here overwrote a --config given before the subcommand,
    # silently falling back to config.json instead of the file that was asked for.
    config_parent.add_argument("--config", default=argparse.SUPPRESS, help="Path to dbabrain config JSON. Defaults to config.backup_restore.json or config.json.")
    # inherited=True: these same flags are registered on the top-level parser below, and every
    # subcommand inherits this one. Without it, a --key-base64 given *before* the subcommand was
    # overwritten by this copy's default at subparse time and the run died with "No decryption
    # key provided" on a command line that was perfectly correct.
    add_key_argument(config_parent, inherited=True)

    parser = argparse.ArgumentParser(description="dbabrain backup restore automation.")
    parser.add_argument("--config", default=None, help="Path to dbabrain config JSON. Defaults to config.backup_restore.json or config.json.")
    add_key_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    copy = subparsers.add_parser("copy-backup", parents=[config_parent], help="Copy backup files into the local import folder.")
    copy.add_argument("--hours", type=int, help="Override copy_recent_hours for this run. Use 0 or negative to copy all matching files.")
    copy.add_argument("--source-id", help="Copy only one configured source/server.")
    copy.add_argument("--restore-id", help="Copy only the restore entry with this restore_id.")
    copy.add_argument("--force", action="store_true", help="Copy files even if they already exist in the target.")

    delete = subparsers.add_parser("delete-backup", parents=[config_parent], help="Delete old backup files from the target import folder.")
    # Seconds, and no longer required. It was `--hours`, mandatory, and the workflow converted the
    # configured seconds into it with `max(1, seconds // 3600)` - so a retention under an hour
    # silently became an hour. Now the entry's own `cleanup_retention` is the default and the
    # override is in the same unit as the config. `--hours` still parses, because it was the only
    # spelling for a year, and it says what it does to the number.
    delete.add_argument("--retention-seconds", type=int, default=None,
                        help="Delete target files older than this many seconds. "
                             "Use 0 to remove the age gate. Default: each entry's cleanup_retention.")
    delete.add_argument("--hours", type=int, default=None,
                        help="Deprecated spelling of --retention-seconds, in hours (multiplied by 3600).")
    delete.add_argument("--source-id", help="Delete target files for only one configured source/server.")
    delete.add_argument("--restore-id", help="Delete target files only for the restore entry with this restore_id.")
    delete.add_argument(
        "--force", action="store_true",
        help="Also delete the chosen files no proven copy recorded (stray files), each by its own "
             "forced command - measured on the host, deleted, its bytes and sha256 recorded. Without "
             "it such a file is kept and named. By hand only.",
    )
    delete.add_argument(
        "--dry-run", action="store_true",
        help="List what would be deleted and delete nothing. The engine has always accepted "
             "this; the flag was missing here, so 'what would this remove' had no answer "
             "short of running it against a real staging directory.")

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

    take_cert = subparsers.add_parser(
        "take-certificate",
        parents=[config_parent],
        help="Take a SQL Server entry's backup certificate pair into dbabrain's vault (encrypted, in the "
             "runtime store), once; with --delete-at-source, then delete it on the server. Run by hand.",
    )
    take_cert.add_argument("--backup-id", required=True, help="The SQL Server backup entry whose pair to take.")
    take_cert.add_argument("--apply", action="store_true",
                           help="Keep it in the vault and prove it. Without: read and report only.")
    take_cert.add_argument("--delete-at-source", action="store_true",
                           help="After the vault row is proven: delete the .cer, then the .pvk, one file per "
                                "command with the facts the vault holds, then the empty _cert folder.")
    subparsers.add_parser("certificates", parents=[config_parent],
                          help="List the backup certificates dbabrain's vault holds (no secret shown).")
    cert_files = subparsers.add_parser(
        "certificate-files", parents=[config_parent],
        help="Every verified vault row as a sealed file on THIS node (age, for dbabrain and the offline "
             "recovery key): a file there is proven, never rewritten; --apply writes a missing one.")
    cert_files.add_argument("--apply", action="store_true", help="Write the missing files. Without: report only.")
    vault_key = subparsers.add_parser(
        "vault-key", parents=[config_parent],
        help="Make a key for the vault's files: --recovery --out FILE (the private half to FILE, to keep "
             "off every dbabrain machine) or --dbabrain (dbabrain's own, into the secret store, once).")
    which_key = vault_key.add_mutually_exclusive_group(required=True)
    which_key.add_argument("--recovery", action="store_true", help="A recovery key.")
    which_key.add_argument("--dbabrain", action="store_true", help="dbabrain's own identity.")
    vault_key.add_argument("--out", help="With --recovery: the file the private half is written to (never overwritten).")

    prune = subparsers.add_parser(
        "prune-backups",
        parents=[config_parent],
        help="Delete the obsolete backup files of the configured entries (retention cleanup).",
    )
    prune.add_argument("--backup-id", help="Prune only one configured backup entry.")
    prune.add_argument("--job", help="Prune only this job name within the selected entries.")
    prune.add_argument(
        "--retention-seconds", type=int, default=None,
        help="Override the window for this run, in seconds. Default: whatever each job already "
             "declares in cleanup_retention, which is the same number its own script prunes with.",
    )
    prune.add_argument(
        "--retention-days", type=int, default=None,
        help="Deprecated spelling of --retention-seconds, in days.",
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
    prune.add_argument(
        "--force", action="store_true",
        help="With --apply: also delete the obsolete files the catalog does NOT know (stray files), "
             "each by its own forced command - measured on the host, deleted, its bytes and sha256 "
             "recorded in the answer. Without it such a file is kept and named. By hand only.",
    )
    prune.add_argument(
        "--allow-mass-delete", action="store_true",
        help="Delete even when more than half of an entry's files would go in one run (1.221). "
             "Refused without it: a window cut by mistake or a folder rewritten by ransomware looks "
             "exactly like that.",
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
    workflow.add_argument("--skip-offload", action="store_true",
                          help="Leave the repository copies (offload_enabled entries) for the next pass.")
    workflow.add_argument("--copy-hours", type=int, default=None,
                          help="Hours for the restore copy step, overriding every entry's copy_recent_hours "
                               "for this run. Default: each entry's own (24 when it sets none).")
    workflow.add_argument("--delete-retention-seconds", type=int, default=None,
                          help="Override target retention for the restore half, in seconds. "
                               "Default: each entry's cleanup_retention.")
    workflow.add_argument("--delete-hours", type=int, default=None,
                          help="Deprecated spelling of --delete-retention-seconds, in hours.")
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

    # 0.34.0: is each catalogued backup file still on disk, and still the bytes the backup wrote?
    verify_files = subparsers.add_parser(
        "verify-backup-files", parents=[config_parent],
        help="Re-hash catalogued backup files on their own server and hold each to the checksum the "
             "catalog stored: sets on_disk and checksum_matches. Read-only on the servers.",
    )
    verify_files.add_argument("--backup-id", help="Only this backup entry's files.")
    verify_files.add_argument("--server-id", help="Only this server's files.")
    verify_files.add_argument("--limit", type=int, default=500,
                              help="At most this many files, the ones checked longest ago first (500).")

    # 0.34.0 (1.240): the repository copy on its own - what the workflow runs after its restores.
    offload = subparsers.add_parser(
        "offload", parents=[config_parent],
        help="Copy every catalogued file of the entries with offload_enabled to their repository, prove "
             "each copy there by checksum and size, and - with offload_delete_source - release the "
             "source of a verified copy (never the newest chain).",
    )
    offload.add_argument("--backup-id", help="Only this backup entry.")
    offload.add_argument("--dry-run", action="store_true", help="Say what each entry would copy and release.")
    # No default of its own (0.38.0, 1.340): a stated 100 replaced every entry's
    # `offload_files_per_pass` on each run started by hand, so the same entry took 5 files from the
    # scheduler and 100 from a terminal.
    offload.add_argument("--limit", type=_files_per_run, default=None,
                         help="At most this many files per entry, for this run only "
                              "(default: each entry's offload_files_per_pass).")

    # 0.34.0 (1.215): the backups taken before the catalog, hashed where they lie and recorded as
    # `baseline=backfill` - so a restore's gate has a checksum to hold each one to.
    chain = subparsers.add_parser(
        "catalog-verify-chain", parents=[config_parent],
        help="Recompute the backup catalog's hash chain (1.224): a row edited after it was written breaks "
             "it, and the first broken row of each entry is named. Exit 1 when one is.")
    chain.add_argument("--backup-id", default="", help="Only this entry.")
    candidates = subparsers.add_parser(
        "restore-candidates", parents=[config_parent],
        help="Each backup chain of an entry, newest first: verified or not, and whether it ended before "
             "the first sign of an attack - the clean points to restore from (1.244).")
    candidates.add_argument("--backup-id", required=True, help="The backup entry.")
    backfill = subparsers.add_parser(
        "catalog-backfill", parents=[config_parent],
        help="Hash the backup files of one entry that the catalog does not hold yet (taken before "
             "0.34.0), on their own server, and record them as baseline=backfill. A dry run unless --apply.",
    )
    backfill.add_argument("--backup-id", required=True, help="The backup entry whose folder is read.")
    backfill.add_argument("--apply", action="store_true", help="Write the rows; without it, only say what would be written.")
    backfill.add_argument("--max-files", type=int, default=5000,
                          help="At most this many files per folder (5000); more is answered as truncated.")

    # 0.34.0 (1.238): stop a running backup the right way round - its process tree on the host, then
    # its sessions. Killing the session alone let the script back up the next database (.245).
    backup_stop = subparsers.add_parser(
        "backup-stop", parents=[config_parent],
        help="Stop the running run(s) of one backup entry: its script's process tree on the host first, "
             "then (SQL Server) every session the run opened; the run is recorded as stopped by the operator.",
    )
    backup_stop.add_argument("--backup-id", required=True, help="The backup entry whose running run is stopped.")

    # The JSON-request contract, the same one `instance-add` takes: one object, inline, @file or
    # `-`. Never flags for the payload - a backup entry is a nested object with an array of jobs
    # inside it, and flattening that into flags is how `--job-1-cleanup-retention` gets invented.
    backup_add = subparsers.add_parser(
        "backup-add", parents=[config_parent],
        help="Register one backup entry from a JSON request object (see --help).",
    )
    backup_add.add_argument(
        "request", nargs="?",
        help="A JSON object, @path/to/file.json, or - to read stdin. Omit for the full help.")

    restore_add = subparsers.add_parser(
        "restore-add", parents=[config_parent],
        help="Register one restore entry from a JSON request object (see --help).",
    )
    restore_add.add_argument(
        "request", nargs="?",
        help="A JSON object, @path/to/file.json, or - to read stdin. Omit for the full help.")

    entry_active = subparsers.add_parser(
        "entry-active", parents=[config_parent],
        help="Switch backup and restore entries on or off - their active flag only (see --help).",
    )
    entry_active.add_argument(
        "request", nargs="?",
        help="A JSON object, @path/to/file.json, or - to read stdin. Omit for the full help.")

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
    restore_workflow.add_argument("--restore-id", help="Run workflow only for the restore entry with this restore_id. Example: python -m dbabrain.backup_restore.cli restore-workflow --config data/restore_config.json --restore-id ACME_TO_SQLSERVER_198_51_100_31 --force")
    restore_workflow.add_argument("--copy-hours", type=int, default=None,
                                  help="Hours for copy-backup, overriding the entry's copy_recent_hours for "
                                       "this run. Default: the entry's own (24 when it sets none).")
    restore_workflow.add_argument("--delete-retention-seconds", type=int, default=None,
                                  help="Override how long staged files are kept on the target, in seconds. "
                                       "Default: each entry's cleanup_retention.")
    restore_workflow.add_argument("--delete-hours", type=int, default=None,
                                  help="Deprecated spelling of --delete-retention-seconds, in hours.")
    restore_workflow.add_argument(
        "--dry-run", action="store_true",
        help="Rehearse without writing: the space check runs (it only reads), the copy is not run, "
             "the restore and the cleanup are dry runs over what is staged now.")
    restore_workflow.add_argument("--force", action="store_true", help="Force copy even if files already exist in the target.")
    restore_workflow.add_argument(
        "--point-in-time",
        default=None,
        help="Restore to a specific point in time (PITR). Format: 'YYYY-MM-DD HH:MM:SS +HH:MM'. "
             "Requires transaction log backups. If omitted, restores to the latest available state.",
    )
    restore_workflow.add_argument(
        "--copy-only", action="store_true",
        help="Copy from the entry's server_id to its target and prove every file by checksum, then "
             "stop: nothing is restored. An entry whose action is copy_only always runs this way.")
    restore_workflow.add_argument(
        "--execution-mode",
        default=None,
        choices=["sync", "unsync"],
        help="Override execution_mode for this run only (sync or unsync). Defaults to the value in config.",
    )

    # `restore-full`, `restore-diff` and `restore-log` were hidden commands here until 0.24.0 - one
    # step of a configured restore, the job `common.cli restore-full/-diff/-log` does (rules R43).
    # The diff and log ones were handed no file and only ever skipped.

    by_id = subparsers.add_parser(
        "restore-by-id", parents=[config_parent],
        help="Restore one configured entry through the dbabrain.common primitives (JSON request).")
    by_id.add_argument("request", help="JSON object: {\"restore_id\": ..., \"point_in_time\": ..., \"dry_run\": ...}")

    bundle = subparsers.add_parser(
        "export-instance-bundle", parents=[config_parent],
        help="Export one backup entry's instance metadata bundle (logins, jobs ...) on THIS node - "
             "read-only on the source - so a restore here can replay it. Runs whether or not the "
             "entry is active on this node; its server_metadata must be on.")
    bundle.add_argument("--backup-id", required=True)

    # `verify-restore` was here until 0.24.0 - its own CHECKDB through run-sqlcmd, the job
    # `common.cli verify-restore` does on every engine (rules R43, the operator's choice). The
    # restore workflow's last phase asks common's; a person does the same, with the login stated.
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
    # Inactive entries a loader could not read (`RestoreConfigs.unusable`, `ScriptRestores.unusable`):
    # they stop nothing, and they are said here, each with what it lacks, because this is where an
    # operator looks first.
    unusable = {**(getattr(restore_configs, "unusable", {}) or {}),
                **(getattr(script_jobs, "unusable", {}) or {})}
    incomplete = ([f"Inactive and incomplete ({len(unusable)}) - not usable until fixed:"]
                  + [f"- {reason}" for _restore_id, reason in sorted(unusable.items())]) if unusable else []
    total = len(configs) + len(scripts)
    if not total:
        note = hidden_note(hidden_configs + hidden_scripts, noun="entry")
        return "\n".join(["No active restore entries in restore_config.json."]
                         + ([note] if note else []) + ([""] + incomplete if incomplete else []))

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
    if incomplete:
        lines.extend(["", *incomplete])
    return "\n".join(lines)


def _registration_command(args) -> int:
    """``backup-add`` / ``restore-add`` — the CLI face of :mod:`dbabrain.backup_restore.registration`.

    Both take one JSON object, the way every command in the ``instance-add`` family does, and both
    answer with the shared response envelope so a shell caller and the bot read the same shape.
    The passphrase is already in the environment: ``main`` calls ``set_key_env`` before this runs,
    so ``--key-base64`` given before *or* after the subcommand has the same effect.
    """
    from dbabrain.backup_restore import registration
    from dbabrain.lib import response
    from dbabrain.lib.json_io import read_json_request

    add, usage = ((registration.add_backup, registration.BACKUP_ADD_USAGE)
                  if args.command == "backup-add"
                  else (registration.add_restore, registration.RESTORE_ADD_USAGE))
    source = getattr(args, "request", None)
    if not source:
        sys.stdout.write(usage)
        return 2
    try:
        request = read_json_request(source)
    except ValueError as exc:
        return _emit(response.fail(args.command, str(exc)))
    try:
        outcome = add(request, key=os.environ.get("DBABRAIN_SECRET_KEY") or None)
    except registration.RegistrationError as exc:
        return _emit(response.fail(args.command, str(exc)))
    name = outcome.get("backup_id") or outcome.get("restore_id")
    verb = "replaced" if outcome["replaced"] else "registered"
    return _emit(response.ok(
        args.command,
        message=f"{verb} {name} - wrote {', '.join(outcome['files_written'])}",
        data=outcome))


def _entry_active_command(args) -> int:
    """``entry-active`` — the CLI face of :func:`registration.set_entries_active`, in the same
    JSON-request contract and response envelope as ``backup-add`` / ``restore-add``."""
    from dbabrain.backup_restore import registration
    from dbabrain.lib import response
    from dbabrain.lib.json_io import read_json_request

    source = getattr(args, "request", None)
    if not source:
        sys.stdout.write(registration.ENTRY_ACTIVE_USAGE)
        return 2
    try:
        request = read_json_request(source)
    except ValueError as exc:
        return _emit(response.fail(args.command, str(exc)))
    try:
        outcome = registration.set_entries_active(request)
    except registration.RegistrationError as exc:
        return _emit(response.fail(args.command, str(exc)))
    changed = [item.get("backup_id") or item.get("restore_id") for item in outcome["items"] if item["changed"]]
    state = "on" if outcome["active"] else "off"
    return _emit(response.ok(
        args.command,
        message=(f"switched {state}: {', '.join(changed)} - wrote {', '.join(outcome['files_written'])}"
                 if changed else f"already {state}: nothing changed"),
        data=outcome))


def _vault_files_command(args, app_config) -> int:
    """``certificate-files`` and ``vault-key`` (0.37.0) - one JSON answer each."""
    from dbabrain.backup_restore import certificate_files
    from dbabrain.lib.secret_text import resolve_cli_key, resolve_key

    key, key_base64 = getattr(args, "key", None), getattr(args, "key_base64", None)
    try:
        if args.command == "vault-key" and args.recovery:
            if not args.out:
                raise ValueError("--recovery needs --out FILE: where the private half is written.")
            from dbabrain.db.store import utc_now_text

            answer = certificate_files.new_recovery_key(args.out, created_at=utc_now_text())
        else:
            secrets = _load_secrets(data_dir=None, key=key, key_base64=key_base64)
            if args.command == "vault-key":
                answer = certificate_files.new_dbabrain_identity(app_config, secrets=secrets)
            else:
                passphrase = resolve_cli_key(key, key_base64) or resolve_key()
                answer = certificate_files.seal_all(app_config, passphrase=passphrase, secrets=secrets,
                                                    apply=bool(args.apply))
    except Exception as exc:  # noqa: BLE001 - one answer, said
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                                    ensure_ascii=False, indent=2) + "\n")
        return 1
    sys.stdout.write(json.dumps({"ok": True, **answer}, ensure_ascii=False, indent=2, default=str) + "\n")
    return 0 if answer.get("ok", True) else 1


def _files_per_run(text: str) -> int:
    """``offload --limit``, read by argparse: 1 or more. Zero or less is refused at the command
    line, not read as "no limit" - a pass of zero files would end ``done`` having copied nothing."""
    try:
        limit = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"a number of files, got {text!r}") from None
    if limit < 1:
        raise argparse.ArgumentTypeError(
            f"1 or more, got {limit}; leave --limit out for each entry's offload_files_per_pass")
    return limit


def _request_is_dry_run(request_text: str) -> bool:
    """Whether a ``restore-by-id`` request asks for a dry run - read before the run is announced.
    A request that does not parse is not a dry run: the run fails on it and says so."""
    try:
        request = json.loads(request_text)
    except (TypeError, ValueError):
        return False
    return isinstance(request, dict) and bool(request.get("dry_run"))


def _emit(result: dict) -> int:
    """The response envelope on stdout, the way ``restore-by-id`` already writes it here."""
    from dbabrain.lib import response

    sys.stdout.write(json.dumps(result, ensure_ascii=False, default=str) + chr(10))
    return response.exit_code(result)


def _retention_text(job: Any) -> str:
    """The job's windows, and how far back point-in-time reaches (1.233, T3) - the number an
    operator means by "retention", printed rather than inferred."""
    from dbabrain.lib import cleanup_retention

    retention = getattr(job, "retention", None)
    if retention is None or retention.uniform:
        return cleanup_retention.describe(int(job.cleanup_retention))
    windows = ", ".join(f"{kind} {cleanup_retention.describe_window(window)}"
                        for kind, window in retention.windows.items())
    reach = cleanup_retention.point_in_time_reach(retention, engine=str(job.db_type or ""))
    return (f"{windows}, default {cleanup_retention.describe_window(retention.default)}"
            f" | point-in-time reach {cleanup_retention.describe_window(reach)}")


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
        # The days, when the entry names them. Since 0.21.0 "the weekly full" is
        # `time_window.weekdays` rather than a literal inside the engine script, and an entry that
        # runs one day in seven listed as "every 1200m" reads as a job that runs every 20 hours.
        # `[]` is a real configuration meaning never, so it is printed, not skipped.
        if window.weekdays is not None:
            when = f"{when} on {weekdays_text(window.weekdays)}"
        # A log/archive job has no full-vs-diff choice, so naming a derived level there would
        # invite --backup-type on a job that cannot take one. The names are lib's: the assistant's
        # runbook (lib.backup_runbook) withholds the same flag from the same jobs.
        if job.job in LOG_STREAM_JOBS:
            level = f"{job.job} (no full/diff level)"
        else:
            # No BACKUP_LEVEL is a level the SCRIPT picks, and it no longer picks it by the day:
            # it takes the incremental/diff, and promotes to a baseline when there is nothing it
            # can chain onto. Saying "auto (Sun=full, else diff)" here outlived the rule it
            # described by a whole version, and it was the only place an operator could read it.
            level = job.env.get("BACKUP_LEVEL") or "unset (script default: diff/incr)"
        encrypted = "  [encrypted]" if job.env_secrets.get("BACKUP_ENCRYPTION_PASSWORD") else ""
        lines.append(f"- {job.backup_id}{encrypted}")
        lines.append(f"    {job.db_type} {job.server_id}")
        lines.append(f"    level: {level} | schedule: {when}")
        if getattr(job, "cleanup_retention", None) is not None:
            lines.append(f"    retention: {_retention_text(job)}")
    # A PostgreSQL incremental chains onto the newest backup in BACKUP_DIR/base/, so the baseline
    # has to be a job of the SAME entry - that is what makes them share one backup_dir. Split across
    # two entries, a mismatched directory is not an error: `latest` comes back empty, the script
    # takes a FULL baseline instead, reports success, and does so for ever. Nothing else would say
    # so, which is why it is said here, in the listing an operator reads before running one.
    # Grouped by backup_id, because `active` is already flattened one element per JOB - the first
    # draft of this read each element as an entry and so reported every incremental in the estate.
    by_entry: dict[str, set[str]] = {}
    engines: dict[str, str] = {}
    for one in active:
        by_entry.setdefault(one.backup_id, set()).add(str(getattr(one, "job", "")).lower())
        engines[one.backup_id] = str(getattr(one, "db_type", "")).lower()
    for backup_id in sorted(by_entry):
        if engines.get(backup_id) != "postgresql":
            continue
        levels = by_entry[backup_id]
        if "database" in levels and "database_full" not in levels:
            lines.append(f"    ! {backup_id}: an incremental with no database_full job beside "
                         "it. A PostgreSQL incremental chains onto the newest backup in this "
                         "entry's own backup_dir, so a baseline in another entry is not one it can "
                         "reach - every run will take a FULL instead, and report success.")
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


def _retention_override(seconds: int | None, hours: int | None) -> int | None:
    """One run's retention override, in seconds, from either spelling.

    ``None`` means "use what the entry states", which is now always something: `cleanup_retention`
    is mandatory on every restore entry and every backup job. The hours form is the old flag kept
    working; it is multiplied here, in the open, rather than the configured seconds being divided
    down somewhere inside the workflow - the division was `max(1, seconds // 3600)` and it turned
    every retention under an hour into one hour without a word.
    """
    if seconds is not None:
        return int(seconds)
    if hours is not None:
        return int(hours) * 3600
    return None


def _run_script_restore_on_demand(args, app_config, config_path: str, restore_id: str, log_scope: str) -> int:
    """``restore-workflow --restore-id <script-driven entry>``: one run, forced, active or not."""
    from dbabrain.backup_restore.workflow import run_scheduled_restores

    logger = setup_app_logger(app_config, app_name=log_scope, log_scope=log_scope,
                              enable_telegram_alerts=False, enable_console=False)
    log_function_call(logger, function_name="backup_restore.restore-workflow")
    summary = run_scheduled_restores(
        app_config=app_config, config_path=config_path, logger=logger, restore_id=restore_id,
        force=True, on_demand=True, key=getattr(args, "key", None),
        key_base64=getattr(args, "key_base64", None),
        point_in_time=str(getattr(args, "point_in_time", "") or ""),
        copy_only=bool(getattr(args, "copy_only", False)),
        # It was not passed (found 2026-11-10): `--dry-run` on a script-driven entry ran the restore
        # for real - stopped that day only because the target's cmd_access was not enabled yet.
        dry_run=bool(getattr(args, "dry_run", False)))
    if getattr(args, "dry_run", False):
        # One sentence for the chat that asked (/spbot_restore_plan, 0.39.0): its reply reads `said`.
        summary["said"] = "; ".join(str(item.get("said") or "") for item in summary.get("restores") or []
                                    if item.get("said")) or f"{restore_id}: no script restore entry ran a dry run"
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n")
    if getattr(args, "dry_run", False):
        return 0
    if summary.get("failed") or not summary.get("succeeded"):
        return 1
    # The line /spbot_restore's success_output_contains looks for, as the SQL Server path prints.
    sys.stdout.write(f"restore-workflow completed status=SUCCESS restore_id={restore_id}\n")
    return 0


def _default_log_scope(command: str) -> str:
    """Long-running, separately scheduled commands get their own runtime log file."""
    if command == "restore-workflow":
        return "restore_workflow"
    if command in ("backup", "workflow", "offload"):
        return "backup"
    return "backup_restore"


def _export_instance_bundle(config_path: str, backup_id: str) -> int:
    """The bundle a restore on this node replays, exported here (1.88).

    It is written by the node that runs the source's backup, beside that backup - so a node that
    restores a source whose backup another node runs (the soak node; the worker runs the 100.250
    backups) never has one, and the restore replays no login. Two scripts did this by hand (1.78,
    1.88); this is that call, for any backup entry, active here or not. Read-only on the source.
    """
    import json as _json

    from dbabrain.backup_restore.server_metadata import export_for_backup, instance_bundle_dir
    from dbabrain.lib import response as _response

    jobs = [job for job in load_backup_jobs(config_path) if job.backup_id == backup_id]
    if not jobs:
        result = _response.fail("export-instance-bundle", f"no backup entry {backup_id!r}.",
                                kind="request")
    elif not jobs[0].server_metadata.enabled:
        result = _response.fail(
            "export-instance-bundle",
            f"{backup_id} has server_metadata off - there is no bundle to export for it.",
            kind="not_configured")
    else:
        job = jobs[0]
        bundle_dir = instance_bundle_dir(job.server_id)
        exported = export_for_backup(job.server_metadata, server_id=job.server_id,
                                     bundle_dir=bundle_dir)
        ok = bool((exported or {}).get("ok", False))
        result = (_response.ok("export-instance-bundle",
                               message=f"{job.server_id}: bundle exported to {bundle_dir}.",
                               data={"bundle_dir": str(bundle_dir), "export": exported})
                  if ok else
                  _response.fail("export-instance-bundle",
                                 f"{job.server_id}: export failed - "
                                 f"{(exported or {}).get('error') or 'see data.export'}",
                                 data={"bundle_dir": str(bundle_dir), "export": exported}))
    sys.stdout.write(_json.dumps(result, ensure_ascii=False, default=str) + chr(10))
    return _response.exit_code(result)


#: Commands that start NEW work - a backup, a schedule, a sweep. Past a key's grace they stop; every
#: other command reads or restores what exists, which a key's end never breaks (1.0.0, D1).
NEW_WORK_COMMANDS = frozenset({"workflow", "backup", "offload", "prune-backups", "backup-add", "restore-add",
                               "copy-backup", "delete-backup", "catalog-backfill"})


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    set_key_env(getattr(args, "key", None), getattr(args, "key_base64", None))
    from dbabrain.lib import dbabrain_key
    refused = dbabrain_key.said_at_the_door(
        dbabrain_key.gate_here("backup_restore", new_work=args.command in NEW_WORK_COMMANDS), app="backup_restore")
    if refused is not None:
        return refused
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
    # The run rows a hand-started share-driven restore claimed (0.39.0, 1.342), closed where it ends.
    _hand_store: object = None
    _hand_claims: list[tuple[object, int]] = []
    # A hand-run restore-workflow's steps, said by the workflow that calls them (0.40.0, 1.375).
    _rw_say: object = None
    # The id the operator typed, before anything resolves it: the refusal of an unknown id is the
    # message that most needs to name it, and it said `backup_id=<unknown>` for NO_SUCH_BACKUP
    # (the bot's refusal cases, 2026-09-25).
    for _arg, _key in (("restore_id", "restore_id"), ("restore_id_pos", "restore_id"),
                       ("backup_id", "backup_id")):
        if getattr(args, _arg, None) and _key not in _id_meta:
            _id_meta[_key] = str(getattr(args, _arg))
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
            # The app reads the config and decrypts; dbabrain.common performs. This is the seam.
            import json as _json

            from dbabrain.backup_restore.restore_by_id import restore_by_id
            from dbabrain.lib import response as _response

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

            # A dry run is bracketed like a run - whoever starts one reports it - but in its own
            # words (0.38.0, 1.341): it used to say "started" and "finished: done" to the entry's
            # group, which reads as a database restored when nothing was touched.
            _rbid_dry = _request_is_dry_run(args.request)
            _rbid_noun = f"Restore {_rbid_id} dry run" if _rbid_dry else f"Restore {_rbid_id}"
            if _rbid_dry:
                _rbid_meta["dry_run"] = True
            # A restore by hand says the same eleven steps as a scheduled one, said by the workflow that
            # calls them (0.40.0, 1.375); a dry run keeps its own two words - it touches nothing.
            from dbabrain.backup_restore.restore_script import load_script_restores as _load_scripts
            from dbabrain.backup_restore.workflow import _RestoreSay, run_restore_by_id

            _rbid_job = next((j for j in _load_scripts(resolved_config_path) if j.restore_id == _rbid_id), None)
            _rbid_say = None
            if _rbid_job is not None and not _rbid_dry:
                _rbid_payload = _json.loads(args.request) or {}
                _rbid_say = _RestoreSay(app_config=_rbid_app_config, logger=None, notify=_rbid_notify,
                                        metadata=_rbid_meta, restore_id=_rbid_id, command="restore-by-id",
                                        engine=str(_rbid_job.db_type or "").lower(),
                                        target=str(_rbid_job.target_server_id or _rbid_job.server_id),
                                        source=str(_rbid_job.server_id))
                _rbid_say.started(source=str(_rbid_job.server_id),
                                  moment=str(_rbid_payload.get("point_in_time") or ""),
                                  copy_only=bool(_rbid_payload.get("copy_only")),
                                  from_repository=getattr(_rbid_job, "restore_from", None) == "repository",
                                  remote=bool(getattr(_rbid_job, "is_remote", False)))
            else:
                _rbid_event("START", f"{_rbid_noun} started." + (" Nothing will be restored." if _rbid_dry else ""))
            _rbid_clock = time.monotonic()
            try:
                payload = _json.loads(args.request)
                # A dry run is handed the catalog too, as the workflow's is: an entry restoring from the
                # repository plans from it (where each copy is, what it was proven as), and without it the
                # dry run refused what the real run then restored (a lab drill, 2026-11-16). It only
                # reads there.
                data = run_restore_by_id(payload, key=getattr(args, "key", None),
                                         key_base64=getattr(args, "key_base64", None), say=_rbid_say,
                                         catalog=_open_catalog(_rbid_app_config, None))
                result = _response.ok("restore-by-id",
                                      message=(f"{data['restore_id']} dry run: the plan was read, nothing restored "
                                               f"({data['db_type']})." if _rbid_dry
                                               else f"{data['restore_id']} restored ({data['db_type']})."),
                                      data=data)
                _rbid_warnings = [str(w) for w in (data.get("warnings") or [])]
                if _rbid_say is not None:
                    from dbabrain.backup_restore.workflow import restored_said

                    _rbid_say.finished(status="done", copy_only=bool(data.get("copy_only")),
                                       restored=restored_said(data), warnings=_rbid_warnings,
                                       seconds=int(time.monotonic() - _rbid_clock))
                else:
                    _rbid_event("END", (f"{_rbid_noun} finished: the plan was read, nothing restored." if _rbid_dry
                                        else f"{_rbid_noun} finished: done.")
                                + (f" With a warning - {'; '.join(_rbid_warnings)}" if _rbid_warnings else ""),
                                {"warnings": _rbid_warnings} if _rbid_warnings else None,
                                level="warning" if _rbid_warnings else "logging")
            except Exception as exc:  # noqa: BLE001 - reported as JSON, like the common commands.
                result = _response.fail("restore-by-id", str(exc))
                if _rbid_say is not None:
                    _rbid_say.finished(status="error", error=str(exc)[-2000:],
                                       seconds=int(time.monotonic() - _rbid_clock))
                else:
                    _rbid_event("ERROR", f"{_rbid_noun} FAILED.", level="error",
                                error_text=str(exc)[-2000:])
            sys.stdout.write(_json.dumps(result, ensure_ascii=False, default=str) + chr(10))
            return _response.exit_code(result)

        if args.command in {"backup-add", "restore-add"}:
            return _registration_command(args)
        if args.command == "entry-active":
            return _entry_active_command(args)
        if args.command == "export-instance-bundle":
            return _export_instance_bundle(resolved_config_path, str(args.backup_id))
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
                copy_hours=getattr(args, "copy_hours", None),
                delete_retention=_retention_override(
                    getattr(args, "delete_retention_seconds", None),
                    getattr(args, "delete_hours", None)),
                skip_backup=bool(getattr(args, "skip_backup", False)),
                skip_restore=bool(getattr(args, "skip_restore", False)),
                skip_offload=bool(getattr(args, "skip_offload", False)),
            )
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            return 1 if summary.get("failed") else 0
        if args.command == "offload":
            logger = setup_app_logger(app_config, app_name=log_scope, log_scope=log_scope,
                                      enable_telegram_alerts=False, enable_console=False)
            summary = run_offloads(
                app_config=app_config, config_path=resolved_config_path, logger=logger,
                backup_id=getattr(args, "backup_id", None), dry_run=bool(args.dry_run),
                limit=args.limit, key=getattr(args, "key", None),
                key_base64=getattr(args, "key_base64", None))
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n")
            return 1 if summary["failed"] else 0
        if args.command == "catalog-verify-chain":
            from dbabrain.db.backup_catalog import BackupCatalogStore

            summary = BackupCatalogStore.from_config(app_config).verify_row_chain(backup_id=args.backup_id)
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + chr(10))
            return 1 if summary["broken"] else 0
        if args.command == "restore-candidates":
            from dbabrain.backup_restore.backup import restore_candidates

            summary = restore_candidates(app_config=app_config, backup_id=args.backup_id)
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + chr(10))
            return 0
        if args.command == "catalog-backfill":
            from dbabrain.backup_restore.backup import catalog_backfill

            summary = catalog_backfill(
                app_config=app_config, config_path=resolved_config_path, backup_id=args.backup_id,
                apply=bool(args.apply), max_files=int(args.max_files or 5000),
                key=getattr(args, "key", None), key_base64=getattr(args, "key_base64", None))
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n")
            return 1 if summary["failed"] else 0
        if args.command == "backup-stop":
            from dbabrain.backup_restore.backup import stop_backup_run

            logger = setup_app_logger(app_config, app_name=log_scope, log_scope=log_scope,
                                      enable_telegram_alerts=False, enable_console=False)
            summary = stop_backup_run(
                app_config=app_config, config_path=resolved_config_path, backup_id=args.backup_id,
                key=getattr(args, "key", None), key_base64=getattr(args, "key_base64", None), logger=logger)
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n")
            if not summary["running"]:
                sys.stdout.write(f"backup-stop: {args.backup_id} has no running run - nothing to stop.\n")
                return 0
            return 0 if summary["stopped"] else 1
        if args.command == "verify-backup-files":
            from dbabrain.backup_restore.backup import verify_backup_files

            summary = verify_backup_files(
                app_config=app_config, config_path=resolved_config_path,
                backup_id=getattr(args, "backup_id", None), server_id=getattr(args, "server_id", None),
                limit=int(getattr(args, "limit", 500) or 500),
                key=getattr(args, "key", None), key_base64=getattr(args, "key_base64", None))
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            # A changed or vanished backup is the answer this exists to give - a non-zero exit.
            return 1 if summary["mismatch"] or summary["missing"] else 0
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
        if args.command in ("certificate-files", "vault-key"):
            return _vault_files_command(args, app_config)
        if args.command in ("take-certificate", "certificates"):
            from dbabrain.lib.secret_text import resolve_cli_key

            if args.command == "certificates":
                from dbabrain.db.certificate_vault import CertificateVault

                rows = CertificateVault.from_config(app_config).listing()
                sys.stdout.write(json.dumps({"certificates": rows}, ensure_ascii=False, indent=2, default=str) + "\n")
                return 0
            from dbabrain.backup_restore.certificate_take import take_certificate

            passphrase = resolve_cli_key(getattr(args, "key", None), getattr(args, "key_base64", None)) or ""
            if not passphrase:
                from dbabrain.lib.secret_text import resolve_key

                passphrase = resolve_key()
            try:
                answer = take_certificate(
                    app_config=app_config, config_path=resolved_config_path, backup_id=args.backup_id,
                    passphrase=passphrase, key=getattr(args, "key", None), key_base64=getattr(args, "key_base64", None),
                    apply=bool(args.apply), delete_at_source=bool(args.delete_at_source), by="take-certificate")
            except Exception as exc:  # noqa: BLE001 - one answer, said
                sys.stdout.write(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                                            ensure_ascii=False, indent=2) + "\n")
                return 1
            sys.stdout.write(json.dumps({"ok": True, **answer}, ensure_ascii=False, indent=2, default=str) + "\n")
            return 0
        if args.command == "prune-backups":
            from dbabrain.backup_restore.prune import run_prune

            logger = setup_app_logger(app_config, app_name=log_scope, log_scope=log_scope,
                                      enable_telegram_alerts=False, enable_console=False)
            log_function_call(logger, function_name="backup_restore.prune")
            summary = run_prune(
                app_config=app_config,
                config_path=resolved_config_path,
                logger=logger,
                backup_id=getattr(args, "backup_id", None),
                job_name=getattr(args, "job", None),
                retention_seconds=_retention_override(
                    getattr(args, "retention_seconds", None),
                    (getattr(args, "retention_days", None) or 0) * 24 or None),
                mode=getattr(args, "mode", "age"),
                key=getattr(args, "key", None),
                key_base64=getattr(args, "key_base64", None),
                apply=bool(getattr(args, "apply", False)),
                allow_mass_delete=bool(getattr(args, "allow_mass_delete", False)),
                force=bool(getattr(args, "force", False)),
            )
            sys.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            return 1 if summary.get("failed") else 0
        restore_configs = load_restore_configs(resolved_config_path)
        # restore_id: --restore-id flag takes precedence; positional restore_id_pos is the shorthand.
        restore_id_filter = getattr(args, "restore_id", None) or getattr(args, "restore_id_pos", None)
        if restore_id_filter:
            smb_why_not = unusable_reason(restore_configs, restore_id_filter)
            restore_configs = [config for config in restore_configs if config.restore_id == restore_id_filter]
            if not restore_configs:
                # A script-driven entry (one that declares `script`: the Oracle, PostgreSQL and
                # container-to-container SQL Server drills) is not in this list at all — it is
                # run by `workflow`, through run_scheduled_restores. Reporting it as "no entry
                # found" sent an operator looking for a config problem that did not exist, on an
                # entry sitting in the file they were reading. Name the command that runs it.
                script_jobs = load_script_restores(resolved_config_path)
                why_not = smb_why_not or unusable_reason(script_jobs, restore_id_filter)
                if why_not:
                    # An inactive entry the loader could not read: its own reason, not "no entry".
                    raise ValueError(why_not)
                script_ids = {job.restore_id for job in script_jobs}
                if restore_id_filter in script_ids and args.command == "restore-workflow":
                    # Run it, by the scheduler's own runner. This used to refuse, naming a CLI
                    # command - and `/spbot_restore` runs exactly this, so not one PostgreSQL,
                    # Oracle or container SQL Server drill could be started from the bot
                    # (2026-09-24). The runner writes the restore-workflow END/ERROR rows the
                    # bot's completion probe reads, under this restore_id.
                    return _run_script_restore_on_demand(args, app_config, resolved_config_path,
                                                         restore_id_filter, log_scope)
                if restore_id_filter in script_ids:
                    raise ValueError(
                        f"restore_id={restore_id_filter} is a script-driven restore (it declares "
                        f"`script` in restore_config.json), which `{args.command}` does not run. "
                        f"Use: python -m dbabrain.backup_restore.cli workflow "
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
        if not restore_configs:
            # An install with nothing configured loads as no entries, which is right for the
            # scheduled `workflow` above - it has nothing due and says so. A command asked to act
            # on one restore has nothing to act on, and without this it reached
            # `restore_configs[0]` and failed as `IndexError: list index out of range`: the same
            # unreadable error the empty-config fix of 2026-09-11 removed, one line further on.
            raise ValueError(
                f"`{args.command}` needs a restore entry and none is configured: "
                "data/restore_config.json declares no `backup_restore.restores`. Add one through "
                "the bot or by hand; `list-restores` shows what the file declares."
            )
        # Apply --execution-mode CLI override (per-run only, does not persist to config file).
        execution_mode_override = getattr(args, "execution_mode", None)
        if execution_mode_override is not None:
            restore_configs = [dataclasses.replace(config, execution_mode=execution_mode_override) for config in restore_configs]
        restore_config = restore_configs[0]
        _copy_only = (_decide_copy_only(restore_configs, asked=bool(getattr(args, "copy_only", False)))
                      if args.command == "restore-workflow" else False)
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
                "action": "copy_only" if _copy_only else "restore",
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
        if args.command == "restore-workflow" and not getattr(args, "dry_run", False):
            # Claimed before the START is said and before anything on the target is touched (1.342):
            # an entry whose restore is already running is told so and nothing else happens - it is
            # not a failure and is not reported as one (RunAlreadyClaimed). A dry run changes nothing
            # and claims nothing.
            try:
                _hand_store, _hand_claims = _claim_hand_started(
                    app_config, restore_configs, action=str(_rw_meta.get("action") or "restore"), logger=logger)
            except AlreadyRunning as busy:
                log_event(logger, level="warning",
                          message=sanitize_text(f"backup_restore.restore-workflow not started: {busy}"))
                print(sanitize_text(f"NOT STARTED: {busy}"), file=sys.stderr)
                return 1
        # A restore-workflow run by hand says its eleven steps as the scheduled one does, from the workflow
        # that calls them (0.40.0, 1.375); the other commands keep their START and END.
        if args.command == "restore-workflow" and not getattr(args, "dry_run", False):
            from dbabrain.backup_restore.workflow import _RestoreSay

            _rw_say = _RestoreSay(
                app_config=app_config, logger=logger, notify=_notify, metadata={**_id_meta, **_rw_meta},
                restore_id=str(_rw_meta.get("restore_id") or ",".join(str(c.restore_id) for c in restore_configs)),
                engine="sqlserver", command=args.command,
                target=",".join(sorted({str(c.target_id) for c in restore_configs})),
                source=",".join(sorted({str(c.source_id) for c in restore_configs})))
            _rw_say.started(source=_rw_say.source, moment=str(getattr(args, "point_in_time", "") or ""),
                            copy_only=_copy_only, from_repository=False, remote=True)
        else:
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
                result = run_copy_backup(config, logger=logger, force=bool(getattr(args, "force", False)),
                                         catalog=_open_catalog(app_config, logger))
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
                        "restore_run_id": result.restore_run_id,
                        "integrity": result.integrity,
                    }
                )
            output = {
                "status": "SUCCESS",
                "sources_considered": len(source_outputs),
                "sources": source_outputs,
            }
        elif args.command == "delete-backup":
            source_outputs = []
            override = _retention_override(args.retention_seconds, args.hours)
            for config in restore_configs:
                if override is not None:
                    config = dataclasses.replace(config, cleanup_retention=override)
                result = run_delete_backup(config, logger=logger,
                                           dry_run=bool(getattr(args, "dry_run", False)),
                                           force=bool(getattr(args, "force", False)))
                source_outputs.append(
                    {
                        "source_id": config.source_id,
                        "target_id": config.target_id,
                        "returncode": result.returncode,
                        "target_backup_dir": str(config.vm_import_unc),
                        "cleanup_retention": result.cleanup_retention,
                        "files_considered": result.files_considered,
                        "deleted": result.deleted,
                        "skipped": getattr(result, "skipped", 0),
                        "dry_run": bool(getattr(args, "dry_run", False)),
                    }
                )
            output = {
                "status": "SUCCESS",
                "dry_run": bool(getattr(args, "dry_run", False)),
                "sources_considered": len(source_outputs),
                "sources": source_outputs,
            }
        elif args.command == "import-certificate":
            from dbabrain.backup_restore.certificate import ensure_source_certificate

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
                    dbabrain_config=app_config,
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
                    dbabrain_config=app_config,
                    database=database,
                    dry_run=bool(args.dry_run),
                    logger=logger,
                )
            else:
                source_outputs = [
                    run_restore_all_latest(config=config, dbabrain_config=app_config, dry_run=bool(args.dry_run), logger=logger)
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
        elif args.command == "restore-workflow":
            # Instance metadata replays here too, not only on the scheduled path: the workflow's
            # run_share_restore replays it between the copy and the certificate, and after the check.
            output = run_restore_workflow(
                restore_configs=restore_configs,
                app_config=app_config,
                copy_hours=args.copy_hours,
                delete_retention=_retention_override(
                    args.delete_retention_seconds, args.delete_hours),
                dry_run=bool(args.dry_run),
                force=bool(args.force),
                logger=logger,
                point_in_time_utc=pit_utc,
                copy_only=_copy_only,
                # The catalog's run row points at the claimed row, as the scheduled run's does (1.214).
                job_run_id=_hand_claims[0][1] if len(_hand_claims) == 1 else None,
                say=_rw_say,
                # A copy-only run touches nothing on the target instance.
                replay_metadata=not _copy_only,
            )
            _close_hand_started(_hand_store, _hand_claims, output=output, started=start_monotonic)
            _hand_claims = []
        else:
            raise ValueError(f"Unknown command: {args.command}")

        duration_ms = int((time.monotonic() - start_monotonic) * 1000)
        safe_output = sanitize_value(output)
        _finished_rid = f"restore_id={_rw_meta['restore_id']} " if _rw_meta.get("restore_id") else ""
        log_event(logger, level="logging", message=sanitize_text(f"{_finished_rid}backup_restore.{args.command} finished status={output.get('status')}"))
        summary_output = _summarize_command_output(args.command, safe_output)
        end_level, end_message, end_metadata = _build_end_event(command=args.command, output=summary_output)
        if args.command == "restore-workflow" and _rw_meta.get("action") == "copy_only":
            end_message = f"Copy only - nothing restored. {end_message}"
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
        if _rw_say is not None:
            from dbabrain.backup_restore.workflow import _share_restored

            _rw_ok = str(output.get("status")) in {"SUCCESS", "DRY_RUN"}
            _rw_say.finished(status="done" if _rw_ok else "error", copy_only=_copy_only,
                             restored=_share_restored(output), seconds=duration_ms // 1000,
                             error="" if _rw_ok else f"restore reported {output.get('status')}", run_row=end_metadata)
        else:
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
        # A hand-started restore's claimed rows are closed with its error, never left RUNNING (1.342).
        _close_hand_started(_hand_store, _hand_claims, error=exc, started=start_monotonic)
        if logger:
            _failed_command = getattr(args, "command", "cli")
            _, _failed_id = resolve_run_id(_failed_command, _id_meta)
            log_event(logger, level="critical", message=sanitize_text(
                f"backup_restore.{_failed_command} failed restore_id={_failed_id}: {type(exc).__name__}: {exc}"))
        if app_config and _rw_say is not None:
            # The restore-workflow run said its steps; its end is step 11, failed at the step it was in.
            _rw_say.finished(status="error", error=sanitize_text(f"{type(exc).__name__}: {exc}")[:2000],
                             seconds=int(time.monotonic() - start_monotonic))
        elif app_config:
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


def point_in_time_window_start(point_in_time_utc: dt.datetime | None, hours: int) -> dt.datetime | None:
    """Where a point-in-time restore's copy window begins, or ``None`` for no lower bound.

    ``hours <= 0`` means "every matching file" - the copy step's own rule for ``copy_recent_hours``
    - so a point-in-time restore then reaches back without limit. ``point - 0 h`` made the window
    zero wide (an inverted one for a negative value) and the copy found nothing to take.
    """
    if point_in_time_utc is None or int(hours) <= 0:
        return None
    return point_in_time_utc - dt.timedelta(hours=int(hours))


def run_restore_workflow(
    *,
    restore_configs: list[BackupRestoreConfig],
    app_config: object,
    copy_hours: int | None = None,
    delete_retention: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    logger: object | None = None,
    point_in_time_utc: object = None,
    on_phase: object = None,
    copy_only: bool = False,
    job_run_id: int | None = None,
    say: object = None,
    replay_metadata: bool = False,
) -> dict[str, object]:
    """A share-driven restore - the workflow's run of its steps (``workflow.run_share_restore``), said by
    it through ``say`` (``None``: no one told). ``on_phase`` is accepted and not called (0.40.0, 1.375)."""
    from dbabrain.backup_restore.workflow import run_share_restore

    return run_share_restore(restore_configs=restore_configs, app_config=app_config, copy_hours=copy_hours,
                             delete_retention=delete_retention, dry_run=dry_run, force=force, logger=logger,
                             point_in_time_utc=point_in_time_utc, copy_only=copy_only, job_run_id=job_run_id,
                             say=say, replay_metadata=replay_metadata)


class AlreadyRunning(errors.Refused):
    """A hand-started restore whose entry has a run in flight: nothing is started."""


def _claim_hand_started(app_config: object, restore_configs: list[BackupRestoreConfig], *, action: str,
                        logger: object = None) -> tuple[object, list[tuple[object, int]]]:
    """The RUNNING row of each selected entry, taken before a hand-started restore touches anything
    (0.39.0, 1.342). ``(store, [(entry, job run id)])``.

    The scheduler's ``workflow`` and a script-driven restore claim their restore_id's row first. This
    door - ``restore-workflow`` on a share-driven entry, which is what ``/spbot_restore`` runs for one
    - took none: a restore started by hand ran beside the scheduled one of the same entry (two
    restores into one database, the failure of 2026-09-14), the next due check did not see it, and a
    run that died left no row for the reaper to close.

    An entry already running raises :class:`AlreadyRunning` before anything is claimed; a claim lost
    in a race releases the ones taken. **A store that cannot be reached is not a refusal**: a restore
    by hand is what is run when things are broken, so it goes on unclaimed and says so in the log.
    """
    from dbabrain.backup_restore import schedule
    from dbabrain.db.store import DbaBrainStore, RunAlreadyClaimed

    keyed = [config for config in restore_configs if config.restore_id]
    claims: list[tuple[object, int]] = []
    store = None
    try:
        store = DbaBrainStore.from_config(app_config)
        latest = store.fetch_latest_job_runs_by_job_code()
        running = [config.restore_id for config in keyed
                   if schedule.is_running(schedule.restore_job_code(config.restore_id), latest)]
        if running:
            raise AlreadyRunning(
                f"restore_id={', '.join(running)} is already running - two restores into one database is "
                "what the run claim stops. Nothing was started. Wait for it; a run abandoned at RUNNING is "
                "closed by itself once it is past its time_window.timeout.")
        noun = "Copy only" if action == "copy_only" else "Restore"
        for config in keyed:
            try:
                log_id, _started = schedule.start_run(
                    store=store, job_code=schedule.restore_job_code(config.restore_id),
                    message=f"{noun} {config.restore_id} started by hand.",
                    metadata={"restore_id": config.restore_id, "source_id": config.source_id,
                              "target_id": config.target_id, "action": action, "started_by": "restore-workflow"})
            except RunAlreadyClaimed as exc:
                _close_hand_started(store, claims, started=time.monotonic(),
                                    error=AlreadyRunning(f"not started: {config.restore_id} was claimed by another run"))
                raise AlreadyRunning(f"restore_id={config.restore_id} is already running ({exc}). "
                                     "Nothing was started.") from exc
            claims.append((config, log_id))
    except AlreadyRunning:
        raise
    except Exception as exc:  # noqa: BLE001 - the claim is a guard, not a condition of a restore by hand
        if logger:
            log_event(logger, level="warning", message=sanitize_text(
                f"backup_restore.restore-workflow: no run claim taken - the store could not be used "
                f"({type(exc).__name__}: {exc}); the restore goes on unclaimed"))
        return store, claims
    return store, claims


def _hand_run_outcome(output: dict[str, object], restore_id: str) -> tuple[str, str | None]:
    """``(status, error)`` of one entry of a hand-started run, by the rule the scheduled run uses:
    done only when the engine said so AND no database of it was left unrestored or unclean."""
    per = next((row for row in output.get("per_restore_results") or []
                if isinstance(row, dict) and row.get("restore_id") == restore_id), None)
    verdict = str((per if per is not None else output).get("status") or "")
    if per is not None:
        failed = int(per.get("failed") or 0)
    else:
        failed = len([state for state in (output.get("per_database_restore_status") or {}).values()
                      if str(state) not in {"SUCCESS", "SUCCESS_RESUMED", "DRY_RUN"}])
    if verdict in {"SUCCESS", "DRY_RUN"} and not failed:
        return "done", None
    return "error", (f"restore reported {verdict or 'no status'}"
                     + (f"; {failed} database(s) not restored or not clean" if failed else ""))


def _close_hand_started(store: object, claims: list[tuple[object, int]], *, started: float,
                        output: dict[str, object] | None = None, error: BaseException | None = None) -> None:
    """Close each claimed row with what its restore came to. Never raises: the record is not the restore."""
    if store is None or not claims:
        return
    from dbabrain.backup_restore import schedule

    duration_ms = int((time.monotonic() - started) * 1000)
    for config, log_id in claims:
        try:
            if error is not None:
                status, error_text = "error", f"{type(error).__name__}: {error}"[-2000:]
            else:
                status, error_text = _hand_run_outcome(output or {}, config.restore_id)
            schedule.finish_run(
                store=store, log_id=log_id, status=status,
                message=f"Restore {config.restore_id} finished: {status}.",
                duration_ms=duration_ms, error_text=error_text,
                metadata={"restore_id": config.restore_id, "status": status, "started_by": "restore-workflow",
                          "output_status": str((output or {}).get("status", ""))})
        except Exception:  # noqa: BLE001
            continue


def _decide_copy_only(restore_configs: list[BackupRestoreConfig], *, asked: bool) -> bool:
    """Whether this run copies only (0.34.0, 1.216): ``--copy-only``, or every selected entry stating
    ``action: copy_only``. An entry that states no action is refused (R50), and a run over entries that
    disagree is refused - one run either restores or it does not."""
    from dbabrain.lib.integrity import ACTION_COPY_ONLY, require_restore_action
    from dbabrain.lib.offload import OffloadConfigError, require_restore_from

    for c in restore_configs:
        # Stated on every entry (R50). This path reads a Windows share; the repository is an SSH host, so
        # restore_from repository is the script-driven restores' (restore_script.REPOSITORY_ENGINES).
        source = require_restore_from(getattr(c, "restore_from", None), label=f"restore_id={c.restore_id}")
        if source != "server":
            raise OffloadConfigError(f"restore_id={c.restore_id}: restore_from {source!r} is built for the "
                                     "script-driven restores; this share-driven SQL Server entry reads its "
                                     "server's share (restore_from: server).")
        # And the rest of what the scheduled run refuses an entry for (workflow._entry_action; 0.39.0,
        # 1.342): whether it may replace a database that exists, and the fields that decide a run
        # (1.366) - this door asked for the source and the action only.
        from dbabrain.backup_restore.workflow import _entry_action

        _action, refused = _entry_action(c, copy_only=asked)
        if refused is not None:
            raise refused
    actions = {require_restore_action(c.action, label=f"restore_id={c.restore_id}") for c in restore_configs}
    if asked:
        return True
    if len(actions) > 1:
        raise ValueError("these entries disagree on action (restore and copy_only): run them one at a time "
                         "with --restore-id.")
    return actions == {ACTION_COPY_ONLY}


def _open_catalog(app_config: object, logger: object | None):
    """The backup catalog on this node's store, or None when it cannot be opened. The copy's gate
    still decides without it - every file is still proven - it only has nowhere to record."""
    try:
        from dbabrain.db.backup_catalog import BackupCatalogStore

        catalog = BackupCatalogStore.from_config(app_config)
        catalog.initialize()
        return catalog
    except Exception as exc:  # noqa: BLE001 - reported, and the restore still verifies every file
        if logger:
            log_event(logger, level="warning",
                      message=sanitize_text(f"backup catalog unavailable, copies are verified but not recorded: {exc}"))
        return None


def _close_catalog_runs(catalog, gated_runs: list[tuple[int, bool]], *, phase: str, error_text: str) -> None:
    """End each run row with the restore's outcome; a copy the gate refused stays `refused`."""
    from dbabrain.backup_restore.copy_gate import close_restore_runs

    close_restore_runs(catalog, gated_runs, phase=phase, error_text=error_text)


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


def _verify_targets(config: Any, restored: Iterable[str] = ()) -> list[str]:
    """The names to look for on the TARGET, which are not always the names on the source.

    A drill commonly restores `SALES` as `SALES_STG`. Verifying the source name asks the target
    about a database it was never asked to create, and reports the drill broken when it worked.

    **Only what the entry states** (the operator, 2026-10-06, 0.27.0 1.101): its `database_mappings`,
    or - when it states `restore_all_databases: true`, every database on the share under its
    backup's name - ``restored``, the databases this run restored. An entry that states neither has
    nothing here: the loader refuses it, and this answers empty rather than guess.
    """
    names: list[str] = []
    # `target_database` / `source_database` are DatabaseRestoreMapping's fields. This asked for
    # `restore_database_name` / `source_database_name` - the ENTRY's single-database fields - through
    # getattr with a default until 2026-09-23, so every mapping answered "", the list came back
    # empty, and every engine restore's verification was skipped as "not configured". The test
    # passed because its stand-in mapping carried the same wrong names.
    for mapping in (getattr(config, "databases", None) or ()):
        name = mapping.target_database or mapping.source_database
        if name and name not in names:
            names.append(str(name))
    if not names and getattr(config, "restore_all_databases", False) is True:
        for name in restored:
            if name and str(name) not in names:
                names.append(str(name))
    return names


def _held_to_the_plan(restore_output: object, outcome: dict) -> tuple[list[dict], list[str]]:
    """``(landed, off_target)`` for one entry of a share-driven restore: each database its run
    restored, held to the stop point its own restore planned (``expect``) by the target's record
    (``verify-restore``'s ``restored_to``) - ``restore_by_id.where_it_stopped``, the one rule."""
    from dbabrain.backup_restore.restore_by_id import where_it_stopped

    results = restore_output.get("results") if isinstance(restore_output, dict) else None
    expects = [result["expect"] for result in results or []
               if isinstance(result, dict) and result.get("status") == "SUCCESS" and isinstance(result.get("expect"), dict)]
    if not expects:
        return [], []
    return where_it_stopped(expects, list(outcome.get("databases") or []))


def _restored_databases(output: object) -> list[str]:
    """The databases one entry's restore reported restored this run, by the target's name."""
    per_database = output.get("per_database_restore_status") if isinstance(output, dict) else None
    if not isinstance(per_database, dict):
        return []
    return [str(name) for name, status in per_database.items() if str(status) == "SUCCESS"]


def _verify_plan(config: Any, secrets: dict[str, str],
                 restored: Iterable[str] = ()) -> tuple[dict[str, Any] | None, str]:
    """The `verify-restore` request for one entry - or ``None`` and the reason it cannot be checked.

    ``None`` is a *state*, not a failure: an entry that names no target login is not a broken
    restore, it is one this node was never given the credentials to look at. It is reported as
    skipped, with the reason, rather than counted against the run - the rule the whole app follows
    for "not configured". The reason is the one that applies: every skip read "no target login
    configured" until 0.27.0, which sent the reader to a login that was configured.
    """
    host = str(getattr(config, "vm_credential_target", "") or "").strip()
    username = str(getattr(config, "restore_sql_username", "") or "").strip()
    password_ref = str(getattr(config, "restore_sql_password_env", "") or "").strip()
    databases = _verify_targets(config, restored)
    if not host:
        return None, "no target host configured for this entry (target.credential_target)"
    if not (username and password_ref):
        return None, ("no target login configured for this entry "
                      "(target.sql_username and target.sql_password_ref)")
    if not databases:
        return None, ("no database to check: the entry states no database_mappings, or states "
                      "restore_all_databases and the run restored none")
    password = secrets.get(password_ref)
    if not password:
        return None, f"the target login's secret {password_ref} is not in this node's secret store"
    # **The port comes from the entry, and did not until 2026-09-19.** It was 1433 unconditionally,
    # while the restore writes wherever `sql_instance` says. On this estate that entry restores into
    # `localhost,1453` - the container MSSQL_192_0_2_115_1453 - and the check asked
    # 192.0.2.115:1433, which is a different container with thirteen unrelated databases. Port
    # 1453 held the three the drill restores, one of them stuck in RESTORING: the exact state this
    # verification exists to catch, invisible to it, while all three were reported ABSENT.
    #
    # The *host* stays this node's own route to the machine. `sql_instance` commonly reads
    # `localhost`, which is localhost on the target and not where this process is.
    address = sql_instance.parse(getattr(config, "restore_sql_instance_on_vm", ""))
    if not address.has_port:
        # A named instance is resolved by the SQL Server Browser at connect time and has no port to
        # hand a driver. Substituting the default would ask a different server and report its answer
        # as this drill's verdict, which is the failure above wearing different numbers. Skipped
        # with a reason, the way every other "cannot check this" is.
        return None, ("the target instance names no port (target.sql_instance): a named "
                      "instance's port is found only at connect time")
    return ({"db_type": "sqlserver", "database_names": databases,
             "target": {"host": host, "port": address.port,
                        "username": username, "password": password}}, "")


def _verify_request(config: Any, secrets: dict[str, str],
                    restored: Iterable[str] = ()) -> dict[str, Any] | None:
    """The `verify-restore` request for one entry, or ``None`` (see :func:`_verify_plan`)."""
    return _verify_plan(config, secrets, restored)[0]


#: A per-database outcome that is not a failure. `SKIPPED` is here because a database the run had
#: no work for has not failed - it was not restored at all - while anything else (`FAILED`, an
#: engine's own word, or a status nobody recognises) is not evidence of a usable database and is
#: counted against the run.
_RESTORE_SUCCESS_STATUSES = frozenset({"SUCCESS", "DRY_RUN"})
_RESTORE_OK_STATUSES = _RESTORE_SUCCESS_STATUSES | {"SKIPPED"}


def _restore_failure_text(source_outputs: list[dict[str, object]], failed: list[str]) -> str:
    """Which databases failed, and at which step: a database whose `DBCC CHECKDB` failed was
    restored and recovered, and is not reported as a failed restore.

    "restore failed for 2 database(s): APPINST, SALESDB" on 2026-09-24 sent the operator to the
    backups and the copy; both were fine - the target could not create the check's snapshot.
    """
    checked: list[str] = []
    errors: dict[str, str] = {}
    for source in source_outputs:
        if not isinstance(source, dict):
            continue
        errors.update({str(k): str(v) for k, v in (source.get("per_database_error") or {}).items()})
        checked.extend(str(name) for name, status in (source.get("per_database_restore_status") or {}).items()
                       if str(status) == "CHECK_FAILED")
    restore_failed = sorted(name for name in failed if name not in checked)
    parts = []
    if restore_failed:
        parts.append(f"restore failed for {len(restore_failed)} database(s): {', '.join(restore_failed)}")
    if checked:
        numbers = sorted({number for name in checked
                          for number in re.findall(r"Msg (\d+)", errors.get(name, ""))}, key=int)
        parts.append(
            f"{len(checked)} database(s) restored and recovered, but the integrity check "
            f"(DBCC CHECKDB) failed: {', '.join(sorted(checked))}"
            + (f" - SQL Server Msg {', '.join(numbers)}" if numbers else "")
            + " (checkdb: false on the restore entry skips the check)")
    return "; ".join(parts) + "."


def _failed_restore_databases(source_outputs: list[dict[str, object]]) -> list[str]:
    """Which databases the restore reported a non-success for, named so the failure can be read.

    The count has always been computed; what was missing was anything that *said* which. A workflow
    that fails with "1 database failed" sends its reader back to the store, which is where this
    failure hid for two releases.
    """
    failed: list[str] = []
    for source in source_outputs:
        if not isinstance(source, dict):
            continue
        per_database = source.get("per_database_restore_status")
        if not isinstance(per_database, dict):
            if source.get("status") and str(source.get("status")) not in _RESTORE_OK_STATUSES:
                failed.append(str(source.get("source_id") or source.get("target_id") or "?"))
            continue
        failed.extend(str(name) for name, status in per_database.items()
                      if str(status) not in _RESTORE_OK_STATUSES)
    return failed


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
    success = sum(1 for status in statuses if status in _RESTORE_SUCCESS_STATUSES)
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
