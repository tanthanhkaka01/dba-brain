"""The backup catalog: every file a backup wrote, and every file a restore or a copy used, checked.

0.34.0, 1.211 - the store half of the plan
``audits/20261029_plan_ransomware_resilience_and_backup_integrity.md`` §B.1. The operator,
2026-10-29: *every backup and restore run is saved into the db - file name, server, checksum - so
every run is verified; a copy stores the checksum before and after; the DB must be complete -
source and target server_id, file name, file type, the checksum before and after the copy; the same
for every backup run without the target half; nullable columns for the virus scan.*

Six tables:

* ``backup_runs``         one row per backup run.
* ``backup_files``        one row per file a backup wrote: server, database, name, type, path, size,
  ``checksum_after_backup`` (taken on the server that holds it), the engine's own verification, its
  place in the chain, and the virus scan. Never deleted: a pruned file's row stays as evidence.
* ``backup_file_checks``  every later hash of a stored file - the scrub, a restore's source check,
  the vault, a virus scan - so the history is kept and not only the latest.
* ``restore_runs``        one row per restore or copy-only run.
* ``restore_run_files``   one row per file a run copied: both ``server_id``s, the file name and type,
  and the checksums before and after the copy, plus the virus scan of the copy.
* ``copy_attempts``       one row per copy attempt of one file, with the hash each attempt produced.

**A row reads on its own.** The server ids, file name and file type are repeated on the per-file
and per-attempt rows, though a join could find them: a row read alone, or exported, still says where
a file came from and where it went.

**Keys, in full** (the operator's rule for every store table): a primary key each, a foreign key to
every parent *inside* the catalog, unique keys on the natural key. Two choices are deliberate:

* **No foreign key to ``job_runs``.** ``job_run_id`` is a plain column. ``job_runs`` ages out into
  its archive, and a request table that pointed at it with a real key blocked that sweep for days
  (``run_requests.py``, 2026-09-04). The catalog outlives the run log by design.
* **The unique key on a backup file is partial** - ``(server_id, file_path)`` among rows that are not
  ``pruned`` - so a pruned file's row is kept and a later backup that reuses the name is not refused.

**The store enforces what "proven" means.** A per-file verdict of ``match``, ``copied`` or
``recopied`` is only accepted with a target checksum equal to the source checksum - a ``CHECK``, not
a convention a writer could forget. Every list a column may hold is ``lib.integrity``'s.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dbabrain.lib import errors
from dbabrain.lib import integrity as I
from dbabrain.lib.config import StoreConfig
from dbabrain.db import backend as backend_mod
from dbabrain.db.backend import StoreTarget
from dbabrain.db.store import utc_now_text

#: Bumped when these tables or their additive migrations change. 2 (0.34.0): the repository copy
#: (1.240) - backup_files' offload columns, the ``offload`` run, the ``repository`` check location -
#: and a proven copy's size held to its source's (1.245). 3 (0.34.0): the repository's retention -
#: ``expired``, ``offload_expired_at``, and offload_state's list CHECK by a name of ours. See
#: :func:`_migrate`.
CATALOG_SCHEMA_VERSION = 7

TABLES = ("backup_runs", "backup_files", "backup_file_checks", "restore_runs", "restore_run_files",
          "copy_attempts")

_NOW = "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))"


def _av_columns() -> str:
    return f"""
    av_scan_status TEXT NULL CHECK (av_scan_status IS NULL OR {I.check_list('av_scan_status', I.AV_SCAN_STATUSES)}),
    av_scanned_at TEXT NULL,
    av_scan_tool TEXT NULL,
    av_scan_tool_version TEXT NULL,
    av_scan_signature_version TEXT NULL,
    av_scan_server_id TEXT NULL,
    av_scan_result_text TEXT NULL,"""


def _named_check(table: str, column: str, allowed: tuple[str, ...]) -> str:
    """A list CHECK by a name of ours, not the backend's. A column CHECK is named by PostgreSQL
    (``<table>_<column>_check``) and cannot be widened in place; a named one can be dropped and
    added again by :func:`_migrate` when its list grows - the way ``offload`` joined the run actions."""
    return f"CONSTRAINT ck_{table}_{column} CHECK ({I.check_list(column, allowed)}),"


#: backup_files' repository copy (1.240, 1.247) - one list for the CREATE and for the migration
#: that adds them to a catalog made before them (CATALOG_SCHEMA_VERSION 2). NULL offload_state: the
#: entry does not offload. No FK on offload_run_id, as on last_restore_run_id: restore_runs is
#: created after this table.
OFFLOAD_COLUMNS = (
    # Its list CHECK is a named one (WIDENED_CHECKS): the list grew once already (expired, schema 3).
    ("offload_state", "TEXT NULL"),
    ("offload_server_id", "TEXT NULL"),
    ("offload_path", "TEXT NULL"),
    ("offload_run_id", "INTEGER NULL"),
    ("offload_attempted_at", "TEXT NULL"),
    ("offload_verified_at", "TEXT NULL"),
    ("offload_error", "TEXT NOT NULL DEFAULT ''"),
    ("source_deleted_at", "TEXT NULL"),
    # When the repository's retention removed the copy (offload_state expired, schema 3).
    ("offload_expired_at", "TEXT NULL"),
)

#: The rules the repository copy is held to BY THE DATABASE, as table constraints: one list for the
#: CREATE and for the migration. ``(table, name, check, validate)`` - ``validate`` False adds it NOT
#: VALID on PostgreSQL (held for every new row, not re-proven over rows written before the rule).
ADDED_CONSTRAINTS = (
    # "verified" names the copy: where it is, which run proved it, when.
    ("backup_files", "ck_backup_files_offload_verified",
     "offload_state IS NULL OR offload_state <> 'verified' OR (offload_server_id IS NOT NULL "
     "AND offload_path IS NOT NULL AND offload_run_id IS NOT NULL AND offload_verified_at IS NOT NULL)", True),
    # The scenario's first principle (2026-10-30): never delete an original before its copy is verified.
    # A released file's copy may later reach its own retention on the repository: expired, once verified.
    ("backup_files", "ck_backup_files_source_deleted_after_copy",
     "source_deleted_at IS NULL OR (offload_state IN ('verified', 'expired') AND offload_verified_at IS NOT NULL)",
     True),
    ("backup_files", "ck_backup_files_offload_expired",
     "offload_state IS NULL OR offload_state <> 'expired' OR (offload_expired_at IS NOT NULL "
     "AND offload_verified_at IS NOT NULL)", True),
    # 1.245: a proven copy has its source's size, when both were read.
    ("restore_run_files", "ck_restore_run_files_size",
     "verdict NOT IN ('match', 'copied', 'recopied') OR size_target_bytes IS NULL "
     "OR size_source_bytes IS NULL OR size_target_bytes = size_source_bytes", False),
)

#: Column CHECKs that became named ones when their list grew (version 2): ``(table, column, allowed)``.
WIDENED_CHECKS = (
    ("restore_runs", "action", I.RUN_ACTIONS),
    ("backup_file_checks", "checked_by", I.CHECKED_BY),
    ("backup_file_checks", "location", I.CHECK_LOCATIONS),
    ("backup_files", "offload_state", I.OFFLOAD_STATES),
)


def _offload_columns() -> str:
    return "".join(f"\n    {name} {sql}," for name, sql in OFFLOAD_COLUMNS)


def _added_constraints(table: str) -> str:
    named = "".join(f"\n    {_named_check(owner, column, allowed)}"
                    for owner, column, allowed in WIDENED_CHECKS
                    if owner == table and owner == "backup_files")
    return named + "".join(f"\n    CONSTRAINT {name} CHECK ({check}),"
                           for owner, name, check, _validate in ADDED_CONSTRAINTS if owner == table)


def _checksum(column: str, *, nullable: bool) -> str:
    # The length is the algorithm's; one algorithm today, so the CHECK says 64.
    length = I.CHECKSUM_LENGTH[I.SHA256]
    null = "NULL" if nullable else "NOT NULL"
    guard = f"{column} IS NULL OR length({column}) = {length}" if nullable else f"length({column}) = {length}"
    return f"{column} TEXT {null} CHECK ({guard}),"


SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS backup_runs
(
    backup_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_id TEXT NOT NULL,
    job_run_id INTEGER NULL,
    server_id TEXT NOT NULL,
    engine TEXT NOT NULL CHECK ({I.check_list('engine', I.ENGINES)}),
    -- NULL when the run wrote no file: no database due, no new archivelog.
    level TEXT NULL CHECK (level IS NULL OR {I.check_list('level', I.BACKUP_LEVELS)}),
    node_host TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    ended_at TEXT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK ({I.check_list('status', I.BACKUP_RUN_STATUSES)}),
    files_count INTEGER NOT NULL DEFAULT 0 CHECK (files_count >= 0),
    bytes_total INTEGER NOT NULL DEFAULT 0 CHECK (bytes_total >= 0),
    engine_verify TEXT NULL CHECK (engine_verify IS NULL OR {I.check_list('engine_verify', I.ENGINE_VERIFY_RESULTS)}),
    engine_verify_text TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT '',
    -- The retention the run's job states (1.233), as configured, and its point-in-time reach in
    -- seconds (NULL with a retention stated: kept, no limit by age) - so a page reads it here.
    retention_json TEXT NULL,
    pitr_reach_seconds INTEGER NULL,
    created_at TEXT NOT NULL DEFAULT {_NOW},
    CONSTRAINT uq_backup_runs UNIQUE (backup_id, started_at)
);

CREATE INDEX IF NOT EXISTS ix_backup_runs_server ON backup_runs (server_id, started_at DESC);

CREATE TABLE IF NOT EXISTS backup_files
(
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    backup_run_id INTEGER NULL,
    backup_id TEXT NOT NULL DEFAULT '',
    job_run_id INTEGER NULL,
    server_id TEXT NOT NULL,
    database_name TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL CHECK ({I.check_list('engine', I.ENGINES)}),
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL CHECK ({I.check_list('file_type', I.FILE_TYPES)}),
    file_path TEXT NOT NULL,
    file_folder TEXT NOT NULL DEFAULT '',
    -- The path on the server itself. file_path is where the ENGINE wrote it - inside its container
    -- when it runs in one; host_path is the same file through the container's mount, the path a
    -- person or a copy on that server uses. Equal without a container; '' for a file in no mount.
    host_path TEXT NOT NULL DEFAULT '',
    container_name TEXT NOT NULL DEFAULT '',
    backup_started_at TEXT NULL,
    backup_ended_at TEXT NULL,
    size_bytes INTEGER NULL CHECK (size_bytes IS NULL OR size_bytes >= 0),
    mtime_utc TEXT NULL,
    checksum_algo TEXT NOT NULL DEFAULT 'sha256' CHECK ({I.check_list('checksum_algo', I.ALGORITHMS)}),
    {_checksum('checksum_after_backup', nullable=True)}
    engine_verify TEXT NULL CHECK (engine_verify IS NULL OR {I.check_list('engine_verify', I.ENGINE_VERIFY_RESULTS)}),
    engine_verify_text TEXT NOT NULL DEFAULT '',
    chain_parent_file_id INTEGER NULL,
    chain_position INTEGER NULL CHECK (chain_position IS NULL OR chain_position >= 1),
    engine_meta_json TEXT NOT NULL DEFAULT '{{}}',
    encrypted INTEGER NULL CHECK (encrypted IS NULL OR encrypted IN (0, 1)),
    cert_thumbprint TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL CHECK ({I.check_list('verdict', I.BACKUP_FILE_VERDICTS)}),
    state TEXT NOT NULL DEFAULT 'present' CHECK ({I.check_list('state', I.BACKUP_FILE_STATES)}),
    -- The two answers a person asks of a backup: is it still there, and is it still the same bytes
    -- the backup wrote. 1 / 0 as of last_checked_at; NULL = not known (never checked, or no hash).
    on_disk INTEGER NULL CHECK (on_disk IS NULL OR on_disk IN (0, 1)),
    checksum_matches INTEGER NULL CHECK (checksum_matches IS NULL OR checksum_matches IN (0, 1)),
    {_checksum('checksum_last_check', nullable=True)}
    last_checked_at TEXT NULL,
    last_check_result TEXT NULL CHECK (last_check_result IS NULL OR {I.check_list('last_check_result', I.CHECK_RESULTS)}),
    baseline TEXT NOT NULL DEFAULT 'backup' CHECK ({I.check_list('baseline', I.BASELINES)}),{_av_columns()}
    -- The last restore or copy that USED this file (the operator, 2026-10-29: *each restore marks the
    -- file as used - when, server_id, target_id, result*). The source is this row's server_id. Set
    -- by the store whenever that run or its file row changes (_mark_used), so the result follows the
    -- run to its end; NULL = never used. Every use, not only the last, is a restore_run_files row.
    -- No FK: restore_runs is created after this table; the store writes the id from that table.
    last_restore_run_id INTEGER NULL,
    -- sha256 of this row's own fields and the entry's previous row_hash (1.224): a hash chain.
    row_hash TEXT NULL,
    -- What removed a pruned file (1.233): the retention window of its type, or the offload's release.
    pruned_by TEXT NULL,
    last_restore_at TEXT NULL,
    last_restore_action TEXT NULL CHECK (last_restore_action IS NULL OR {I.check_list('last_restore_action', I.RESTORE_ACTIONS)}),
    last_restore_target_server_id TEXT NULL,
    last_restore_result TEXT NULL CHECK (last_restore_result IS NULL OR {I.check_list('last_restore_result', I.RESTORE_PHASES)}),
    last_restore_verdict TEXT NULL CHECK (last_restore_verdict IS NULL OR {I.check_list('last_restore_verdict', I.RUN_FILE_VERDICTS)}),{_offload_columns()}
    created_at TEXT NOT NULL DEFAULT {_NOW},
    updated_at TEXT NOT NULL DEFAULT {_NOW},
    -- A file the run could not hash is recorded as such, never with an invented checksum.
    CONSTRAINT ck_backup_files_hashed CHECK (checksum_after_backup IS NOT NULL OR verdict = 'hash_failed'),
    -- Every file states its size in bytes; one whose size could not be read says so in its verdict.
    CONSTRAINT ck_backup_files_sized CHECK (size_bytes IS NOT NULL OR verdict IN ('hash_failed', 'size_failed')),{_added_constraints('backup_files')}
    CONSTRAINT fk_backup_files_run FOREIGN KEY (backup_run_id) REFERENCES backup_runs (backup_run_id),
    CONSTRAINT fk_backup_files_chain_parent FOREIGN KEY (chain_parent_file_id) REFERENCES backup_files (file_id)
);

-- Partial: a pruned file's row stays, and a later file at the same path is not refused for it.
CREATE UNIQUE INDEX IF NOT EXISTS ux_backup_files_path
    ON backup_files (server_id, file_path) WHERE state <> 'pruned';
CREATE INDEX IF NOT EXISTS ix_backup_files_server_db
    ON backup_files (server_id, database_name, backup_ended_at DESC);
CREATE INDEX IF NOT EXISTS ix_backup_files_run ON backup_files (backup_run_id);
CREATE INDEX IF NOT EXISTS ix_backup_files_last_restore ON backup_files (last_restore_run_id);
CREATE INDEX IF NOT EXISTS ix_backup_files_last_checked ON backup_files (state, last_checked_at);

CREATE TABLE IF NOT EXISTS backup_file_checks
(
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    server_id TEXT NOT NULL,
    database_name TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL CHECK ({I.check_list('file_type', I.FILE_TYPES)}),
    file_path TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    checked_by TEXT NOT NULL,
    by_run_id TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT 'source',
    checksum_algo TEXT NOT NULL DEFAULT 'sha256' CHECK ({I.check_list('checksum_algo', I.ALGORITHMS)}),
    {_checksum('checksum_after_backup', nullable=True)}
    {_checksum('checksum_now', nullable=True)}
    size_now INTEGER NULL CHECK (size_now IS NULL OR size_now >= 0),
    result TEXT NULL CHECK (result IS NULL OR {I.check_list('result', I.CHECK_RESULTS)}),{_av_columns()}
    created_at TEXT NOT NULL DEFAULT {_NOW},
    -- A hash check has a result; only a virus scan's row may leave it empty.
    CONSTRAINT ck_backup_file_checks_result CHECK (result IS NOT NULL OR checked_by = 'av_scan'),
    {_named_check('backup_file_checks', 'checked_by', I.CHECKED_BY)}
    {_named_check('backup_file_checks', 'location', I.CHECK_LOCATIONS)}
    CONSTRAINT uq_backup_file_checks UNIQUE (file_id, checked_at, location, checked_by),
    CONSTRAINT fk_backup_file_checks_file FOREIGN KEY (file_id) REFERENCES backup_files (file_id)
);

CREATE INDEX IF NOT EXISTS ix_backup_file_checks_server ON backup_file_checks (server_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS restore_runs
(
    restore_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    restore_id TEXT NOT NULL DEFAULT '',
    job_run_id INTEGER NULL,
    source_server_id TEXT NOT NULL,
    target_server_id TEXT NOT NULL,
    target_folder TEXT NOT NULL DEFAULT '',
    point_in_time TEXT NOT NULL DEFAULT 'LATEST',
    tier TEXT NULL,
    node_host TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    ended_at TEXT NULL,
    phase TEXT NOT NULL DEFAULT 'planning' CHECK ({I.check_list('phase', I.RESTORE_PHASES)}),
    chain_files INTEGER NOT NULL DEFAULT 0 CHECK (chain_files >= 0),
    files_matched INTEGER NOT NULL DEFAULT 0 CHECK (files_matched >= 0),
    files_recopied INTEGER NOT NULL DEFAULT 0 CHECK (files_recopied >= 0),
    copy_max_retry INTEGER NOT NULL CHECK (copy_max_retry BETWEEN 0 AND {I.MAX_COPY_MAX_RETRY}),
    refused_reason TEXT NOT NULL DEFAULT '',
    databases_ok INTEGER NULL,
    databases_failed INTEGER NULL,
    error_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT {_NOW},
    {_named_check('restore_runs', 'action', I.RUN_ACTIONS)}
    CONSTRAINT uq_restore_runs UNIQUE (restore_id, source_server_id, target_server_id, started_at)
);

CREATE INDEX IF NOT EXISTS ix_restore_runs_source ON restore_runs (source_server_id, started_at DESC);
CREATE INDEX IF NOT EXISTS ix_restore_runs_target ON restore_runs (target_server_id, started_at DESC);

CREATE TABLE IF NOT EXISTS restore_run_files
(
    run_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    restore_run_id INTEGER NOT NULL,
    file_id INTEGER NULL,
    source_server_id TEXT NOT NULL,
    target_server_id TEXT NOT NULL,
    database_name TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL DEFAULT '' CHECK (engine = '' OR {I.check_list('engine', I.ENGINES)}),
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL CHECK ({I.check_list('file_type', I.FILE_TYPES)}),
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    -- Each side's path on that server itself (through a container's mount), as on backup_files.
    source_host_path TEXT NOT NULL DEFAULT '',
    target_host_path TEXT NOT NULL DEFAULT '',
    backup_taken_at TEXT NULL,
    chain_position INTEGER NULL CHECK (chain_position IS NULL OR chain_position >= 1),
    -- Is the file still on disk, and do its bytes still hash to the checksum dbabrain STORED -
    -- checksum_after_backup, or the source's hash at the copy when no backup recorded it - on the
    -- source server and on the target. 1 / 0 as of each side's checked_at; NULL = not known.
    source_on_disk INTEGER NULL CHECK (source_on_disk IS NULL OR source_on_disk IN (0, 1)),
    source_checksum_matches INTEGER NULL CHECK (source_checksum_matches IS NULL OR source_checksum_matches IN (0, 1)),
    source_checked_at TEXT NULL,
    target_on_disk INTEGER NULL CHECK (target_on_disk IS NULL OR target_on_disk IN (0, 1)),
    target_checksum_matches INTEGER NULL CHECK (target_checksum_matches IS NULL OR target_checksum_matches IN (0, 1)),
    target_checked_at TEXT NULL,
    size_source_bytes INTEGER NULL CHECK (size_source_bytes IS NULL OR size_source_bytes >= 0),
    size_target_bytes INTEGER NULL CHECK (size_target_bytes IS NULL OR size_target_bytes >= 0),
    checksum_algo TEXT NOT NULL DEFAULT 'sha256' CHECK ({I.check_list('checksum_algo', I.ALGORITHMS)}),
    {_checksum('checksum_after_backup', nullable=True)}
    {_checksum('checksum_source_before_copy', nullable=True)}
    {_checksum('checksum_target_before_copy', nullable=True)}
    {_checksum('checksum_target_after_copy', nullable=True)}
    copy_attempts INTEGER NOT NULL DEFAULT 0 CHECK (copy_attempts >= 0),
    copy_max_retry INTEGER NOT NULL CHECK (copy_max_retry BETWEEN 0 AND {I.MAX_COPY_MAX_RETRY}),
    copy_started_at TEXT NULL,
    copy_ended_at TEXT NULL,
    verdict TEXT NOT NULL DEFAULT 'copying' CHECK ({I.check_list('verdict', I.RUN_FILE_VERDICTS)}),{_av_columns()}
    created_at TEXT NOT NULL DEFAULT {_NOW},
    updated_at TEXT NOT NULL DEFAULT {_NOW},
    -- No copy without the source's own hash, unless the source was not there to hash - or the file
    -- was already on the target and is held to the checksum its backup recorded instead (a match
    -- proven against the catalog, the source not read again).
    CONSTRAINT ck_restore_run_files_source CHECK (
        checksum_source_before_copy IS NOT NULL OR verdict = 'source_missing'
        OR (verdict = 'match' AND checksum_after_backup IS NOT NULL)),
    -- "Proven" means the copy's hash IS the source's - or, read against the catalog, the backup's:
    -- held by the store, not by each writer.
    CONSTRAINT ck_restore_run_files_proven CHECK (
        verdict NOT IN ('match', 'copied', 'recopied')
        OR (checksum_target_after_copy IS NOT NULL
            AND checksum_target_after_copy = COALESCE(checksum_source_before_copy, checksum_after_backup))),
    CONSTRAINT ck_restore_run_files_attempts CHECK (copy_attempts <= copy_max_retry + 1),{_added_constraints('restore_run_files')}
    CONSTRAINT uq_restore_run_files UNIQUE (restore_run_id, source_path),
    CONSTRAINT fk_restore_run_files_run FOREIGN KEY (restore_run_id) REFERENCES restore_runs (restore_run_id),
    CONSTRAINT fk_restore_run_files_file FOREIGN KEY (file_id) REFERENCES backup_files (file_id)
);

CREATE INDEX IF NOT EXISTS ix_restore_run_files_source
    ON restore_run_files (source_server_id, database_name, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_restore_run_files_file ON restore_run_files (file_id);

CREATE TABLE IF NOT EXISTS copy_attempts
(
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_file_id INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1 AND attempt_no <= {I.MAX_COPY_MAX_RETRY + 1}),
    source_server_id TEXT NOT NULL,
    target_server_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL CHECK ({I.check_list('file_type', I.FILE_TYPES)}),
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    source_host_path TEXT NOT NULL DEFAULT '',
    target_host_path TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    ended_at TEXT NULL,
    bytes_copied INTEGER NULL CHECK (bytes_copied IS NULL OR bytes_copied >= 0),
    copy_engine TEXT NOT NULL DEFAULT '',
    {_checksum('checksum_source_before_copy', nullable=False)}
    {_checksum('checksum_target_after_copy', nullable=True)}
    size_target_after_copy INTEGER NULL CHECK (size_target_after_copy IS NULL OR size_target_after_copy >= 0),
    result TEXT NOT NULL CHECK ({I.check_list('result', I.ATTEMPT_RESULTS)}),
    error_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT {_NOW},
    CONSTRAINT ck_copy_attempts_match CHECK (
        result <> 'match' OR checksum_target_after_copy = checksum_source_before_copy),
    CONSTRAINT uq_copy_attempts UNIQUE (run_file_id, attempt_no),
    CONSTRAINT fk_copy_attempts_run_file FOREIGN KEY (run_file_id) REFERENCES restore_run_files (run_file_id)
);
"""


def _check_moment() -> str:
    """A check's time to the microsecond: a restore gate and a scrub can check one file in the same
    second, and the history keys a check by its moment - whole seconds refused the second check."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


#: The checksums - and the sizes in bytes beside them - a run records the first time it reads a file,
#: and never again (the operator, 2026-10-29: *the checksum created the first time - backup, restore -
#: is never updated*). They are the evidence every later check is held to; one that could be
#: rewritten would let a changed backup be "fixed" by recording its new hash or size. A later reading
#: is a row of its own (backup_file_checks, copy_attempts) or a `*_now` / `*_checked_at` column.
FIRST_CHECKSUMS = {
    "backup_files": ("checksum_after_backup", "size_bytes"),
    "restore_run_files": ("checksum_after_backup", "checksum_source_before_copy",
                          "checksum_target_before_copy", "checksum_target_after_copy",
                          "size_source_bytes"),
}
_GUARD_MESSAGE = "a first checksum is never updated, nor a first size - they are what every later check is held to"


def _changed(column: str) -> str:
    # A stored first checksum that the update would replace - by another value, or by NULL.
    return (f"(OLD.{column} IS NOT NULL AND (NEW.{column} IS NULL "
            f"OR NEW.{column} <> OLD.{column}))")


#: The same rule as FIRST_CHECKSUMS, held by the DATABASE (the operator, 2026-10-29: *the first
#: checksum validates every later one, so it may never be changed*). The store class refuses it on
#: its own update path; these refuse it for anything else that can reach the store - a hand-written
#: UPDATE, another tool. One trigger per table, per backend: SQLite's WHEN / RAISE, PostgreSQL's
#: plpgsql. Created after the tables; each statement is sent whole, never split on its semicolons.
FIRST_CHECKSUM_TRIGGERS_SQLITE = tuple(
    f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_first_checksum
BEFORE UPDATE ON {table}
WHEN {' OR '.join(_changed(column) for column in columns)}
BEGIN
    SELECT RAISE(ABORT, '{table}: {_GUARD_MESSAGE}');
END"""
    for table, columns in FIRST_CHECKSUMS.items())

FIRST_CHECKSUM_FUNCTION_POSTGRES = """CREATE OR REPLACE FUNCTION dbabrain_first_checksum_guard()
RETURNS trigger LANGUAGE plpgsql AS $guard$
BEGIN
    IF TG_TABLE_NAME = 'backup_files' THEN
        IF """ + " OR ".join(_changed(column) for column in FIRST_CHECKSUMS["backup_files"]) + """ THEN
            RAISE EXCEPTION 'backup_files: """ + _GUARD_MESSAGE + """';
        END IF;
    ELSIF """ + " OR ".join(_changed(column) for column in FIRST_CHECKSUMS["restore_run_files"]) + """ THEN
        RAISE EXCEPTION 'restore_run_files: """ + _GUARD_MESSAGE + """';
    END IF;
    RETURN NEW;
END
$guard$"""


def _install_first_checksum_guard(conn: Any, backend: str) -> None:
    if backend == backend_mod.POSTGRESQL_BACKEND:
        conn.execute(FIRST_CHECKSUM_FUNCTION_POSTGRES)
        for table in FIRST_CHECKSUMS:
            # Created when missing rather than replaced: CREATE OR REPLACE TRIGGER needs PostgreSQL
            # 14, and nothing here drops one.
            exists = conn.execute(
                "SELECT 1 FROM pg_trigger WHERE tgname = ? AND tgrelid = to_regclass(?)",
                (f"trg_{table}_first_checksum", table)).fetchone()
            if exists is None:
                conn.execute(f"CREATE TRIGGER trg_{table}_first_checksum BEFORE UPDATE ON {table} "
                             "FOR EACH ROW EXECUTE FUNCTION dbabrain_first_checksum_guard()")
        return
    for statement in FIRST_CHECKSUM_TRIGGERS_SQLITE:
        conn.execute(statement)


#: Columns a later version adds to a run table (version 4, 1.248): the drill's tier, so the reports
#: judge a drill by its own deadline without reading the backup app's configuration.
RUN_COLUMNS = (("restore_runs", "tier", "TEXT NULL"),
               # Version 5 (1.224): the hash chain of backup_files, per entry.
               ("backup_files", "row_hash", "TEXT NULL"),
               # Version 6 (1.233): what removed a pruned file.
               ("backup_files", "pruned_by", "TEXT NULL"),
               # Version 7 (1.233): each run's retention and point-in-time reach.
               ("backup_runs", "retention_json", "TEXT NULL"),
               ("backup_runs", "pitr_reach_seconds", "INTEGER NULL"))


def _migrate(conn: Any, backend: str) -> None:
    """Bring a catalog made by an earlier CATALOG_SCHEMA_VERSION up to this one, additively.

    ``CREATE TABLE IF NOT EXISTS`` leaves an existing table as it is, so what a later version adds
    to one is added here, and only what is missing - run twice, it changes nothing. Version 2: the
    offload columns, the widened list CHECKs, the added constraints, the offload index.

    **PostgreSQL gets all of it** - the live store (the worker's, 2.87.51 onward) has version 1.
    **SQLite gets the columns only**: it cannot add or drop a table constraint without rebuilding the
    table, and no SQLite catalog of version 1 exists outside a test (0.34.0 shipped no wheel before
    version 2). A version 1 SQLite catalog would refuse an ``offload`` run by its old action CHECK -
    an error naming the constraint, never a wrong row.
    """
    if backend == backend_mod.POSTGRESQL_BACKEND:
        for name, sql in OFFLOAD_COLUMNS:
            conn.execute(f"ALTER TABLE backup_files ADD COLUMN IF NOT EXISTS {name} {sql}")
        for table, name, sql in RUN_COLUMNS:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {sql}")
        for table, column, allowed in WIDENED_CHECKS:
            # The backend's own name for the column CHECK of version 1, then ours with today's list.
            conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_{column}_check")
            conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_{column}")
            conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_{column} "
                         f"CHECK ({I.check_list(column, allowed)})")
        for table, name, check, validate in ADDED_CONSTRAINTS:
            # Dropped and added again, so a rule whose text changed between versions is today's.
            conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
            conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({check})"
                         + ("" if validate else " NOT VALID"))
    else:
        present = {str(row["name"]) for row in conn.execute("PRAGMA table_info(backup_files)").fetchall()}
        for name, sql in OFFLOAD_COLUMNS:
            if name not in present:
                conn.execute(f"ALTER TABLE backup_files ADD COLUMN {name} {sql}")
        for table, name, sql in RUN_COLUMNS:
            columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if columns and name not in columns:      # no columns: the table is not there to widen
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql}")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_backup_files_offload ON backup_files (offload_state, backup_id)")


#: The fields of a backup file row that never change after it is written - what its row_hash covers
#: (1.224, the plan of 2026-10-29, A3.7). The state, the checks and the offload move afterwards and
#: are not in it: they are the record of what happened later, not of what the backup wrote.
CHAIN_FIELDS = ("backup_id", "server_id", "database_name", "engine", "file_name", "file_type", "file_path",
                "size_bytes", "checksum_after_backup", "backup_started_at", "backup_ended_at", "baseline")


def row_hash(values: dict[str, Any], previous: str | None) -> str:
    """sha256 over the entry's previous row_hash and this row's :data:`CHAIN_FIELDS`, each as text - so
    a value written as 1024 and read back as "1024" hashes the same."""
    fields = {name: (None if values.get(name) is None else str(values.get(name))) for name in CHAIN_FIELDS}
    return hashlib.sha256(json.dumps([previous or "", fields], sort_keys=True).encode("utf-8")).hexdigest()


def _insert_chained(conn: Any, values: dict[str, Any]) -> int:
    """Insert a backup file row and chain it: its row_hash is taken over the row AS STORED - read back,
    the store's own defaults included - and the newest hashed row of the same entry before it."""
    file_id = _insert(conn, "backup_files", values)
    stored = conn.execute(f"SELECT backup_id, {', '.join(CHAIN_FIELDS)} FROM backup_files WHERE file_id = ?",
                          (int(file_id),)).fetchone()
    previous = conn.execute(
        "SELECT row_hash FROM backup_files WHERE backup_id = ? AND row_hash IS NOT NULL AND file_id < ? "
        "ORDER BY file_id DESC LIMIT 1", (stored["backup_id"], int(file_id))).fetchone()
    conn.execute("UPDATE backup_files SET row_hash = ? WHERE file_id = ?",
                 (row_hash(dict(stored), previous["row_hash"] if previous is not None else None), int(file_id)))
    return file_id


class BackupCatalogError(errors.OperationFailed):
    """A catalog row could not be written as asked."""


def _require(values: dict[str, Any], *names: str) -> None:
    missing = [name for name in names if values.get(name) in (None, "")]
    if missing:
        raise BackupCatalogError(f"missing required field(s): {', '.join(missing)}.")


def _one_of(values: dict[str, Any], name: str, allowed: tuple[str, ...]) -> None:
    value = values.get(name)
    if value is not None and value not in allowed:
        raise BackupCatalogError(f"{name} must be one of {', '.join(allowed)}; got {value!r}.")


def _insert(conn: Any, table: str, values: dict[str, Any]) -> int:
    columns = [name for name, value in values.items() if value is not None]
    placeholders = ", ".join("?" for _ in columns)
    cursor = conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                          tuple(values[name] for name in columns))
    return int(cursor.lastrowid)


def _refuse_rewrite(conn: Any, table: str, key: str, key_value: int, values: dict[str, Any]) -> None:
    """Empty to a value is the first write; the same value again is harmless; anything else is refused."""
    guarded = [name for name in FIRST_CHECKSUMS.get(table, ()) if name in values]
    if not guarded:
        return
    row = conn.execute(f"SELECT {', '.join(guarded)} FROM {table} WHERE {key} = ?", (key_value,)).fetchone()
    if row is None:
        return
    for name in guarded:
        stored = row[name]
        if stored is not None and values[name] != stored:
            raise BackupCatalogError(
                f"{table}.{name} of {key} {key_value} was recorded as {str(stored)[:12]}... and is never "
                "rewritten - it is what every later check is held to. A later hash is a check of its own.")


def _update(conn: Any, table: str, key: str, key_value: int, values: dict[str, Any]) -> None:
    if not values:
        return
    _refuse_rewrite(conn, table, key, key_value, values)
    assignments = ", ".join(f"{name} = ?" for name in values)
    conn.execute(f"UPDATE {table} SET {assignments} WHERE {key} = ?", (*values.values(), key_value))


def _hold_offload(conn: Any, file_id: int, values: dict[str, Any]) -> None:
    """The repository rules, held by the store on every backend (PostgreSQL holds them again as
    constraints; a SQLite catalog made before them cannot): ``verified`` names its copy, and a source
    is deleted only once its copy is verified - never on the copy command's word."""
    row = conn.execute("SELECT * FROM backup_files WHERE file_id = ?", (file_id,)).fetchone()
    if row is None:
        raise BackupCatalogError(f"no backup file {file_id} in the catalog.")
    merged = {**dict(row), **values}
    if merged.get("offload_state") == "verified":
        missing = [name for name in ("offload_server_id", "offload_path", "offload_run_id", "offload_verified_at")
                   if merged.get(name) in (None, "")]
        if missing:
            raise BackupCatalogError(f"backup file {file_id}: a verified repository copy names "
                                     f"{', '.join(missing)}.")
    if merged.get("offload_state") == "expired" and not (merged.get("offload_expired_at")
                                                         and merged.get("offload_verified_at")):
        raise BackupCatalogError(f"backup file {file_id}: only a copy that was verified can expire, and it "
                                 "says when (offload_expired_at).")
    if merged.get("source_deleted_at") and not (merged.get("offload_state") in ("verified", "expired")
                                                and merged.get("offload_verified_at")):
        raise BackupCatalogError(f"backup file {file_id}: its source is deleted only after its repository copy "
                                 f"is verified - offload_state is {merged.get('offload_state')!r}.")


def _mark_used(conn: Any, *, restore_run_id: int | None = None, run_file_id: int | None = None) -> None:
    """Mark each catalogued backup file a run used: when, onto which target, the run's result and
    this file's copy verdict. Run on every write to the run or its files, so the mark follows the run
    to its end. A file's mark only moves forward - an older run finishing late never covers a newer
    one's."""
    where, key = (("rf.run_file_id = ?", run_file_id) if run_file_id is not None
                  else ("rf.restore_run_id = ?", restore_run_id))
    used = conn.execute(
        "SELECT rf.file_id, rf.verdict, rr.restore_run_id, rr.started_at, rr.action, rr.target_server_id, "
        "rr.phase FROM restore_run_files rf JOIN restore_runs rr ON rr.restore_run_id = rf.restore_run_id "
        # A repository copy is not a use: it is the backup's own second home (1.240), and its run
        # would hide the last real restore of the file behind it.
        f"WHERE {where} AND rf.file_id IS NOT NULL AND rr.action <> '{I.ACTION_OFFLOAD}'", (int(key),)).fetchall()
    for row in used:
        conn.execute(
            "UPDATE backup_files SET last_restore_run_id = ?, last_restore_at = ?, last_restore_action = ?, "
            "last_restore_target_server_id = ?, last_restore_result = ?, last_restore_verdict = ?, "
            "updated_at = ? WHERE file_id = ? AND (last_restore_run_id IS NULL OR last_restore_run_id <= ?)",
            (row["restore_run_id"], row["started_at"], row["action"], row["target_server_id"], row["phase"],
             row["verdict"], utc_now_text(), row["file_id"], row["restore_run_id"]))


#: The columns a writer may set on each table - anything else is a misspelling, refused.
_COLUMNS = {
    "backup_runs": ("backup_id", "job_run_id", "server_id", "engine", "level", "node_host",
                    "app_version", "started_at", "ended_at", "status", "files_count", "bytes_total",
                    "engine_verify", "engine_verify_text", "error_text", "retention_json",
                    "pitr_reach_seconds"),
    "backup_files": ("backup_run_id", "backup_id", "job_run_id", "server_id", "database_name", "engine",
                     "file_name", "file_type", "file_path", "file_folder", "host_path", "container_name",
                     "backup_started_at",
                     "backup_ended_at", "size_bytes", "mtime_utc", "checksum_algo",
                     "checksum_after_backup", "engine_verify", "engine_verify_text",
                     "chain_parent_file_id", "chain_position", "engine_meta_json", "encrypted",
                     "cert_thumbprint", "verdict", "state", "on_disk", "checksum_matches",
                     "checksum_last_check", "last_checked_at",
                     "last_check_result", "baseline", "pruned_by", *I.AV_SCAN_COLUMNS,
                     *(name for name, _sql in OFFLOAD_COLUMNS)),
    "backup_file_checks": ("file_id", "server_id", "database_name", "file_name", "file_type", "file_path",
                           "checked_at", "checked_by", "by_run_id", "location", "checksum_algo",
                           "checksum_after_backup", "checksum_now", "size_now", "result",
                           *I.AV_SCAN_COLUMNS),
    "restore_runs": ("action", "restore_id", "job_run_id", "source_server_id", "target_server_id",
                     "target_folder", "point_in_time", "tier", "node_host", "app_version", "started_at",
                     "ended_at", "phase", "chain_files", "files_matched", "files_recopied",
                     "copy_max_retry", "refused_reason", "databases_ok", "databases_failed", "error_text"),
    "restore_run_files": ("restore_run_id", "file_id", "source_server_id", "target_server_id",
                          "database_name", "engine", "file_name", "file_type", "source_path",
                          "target_path", "source_host_path", "target_host_path", "backup_taken_at",
                          "chain_position", "source_on_disk", "source_checksum_matches",
                          "source_checked_at", "target_on_disk", "target_checksum_matches",
                          "target_checked_at", "size_source_bytes",
                          "size_target_bytes", "checksum_algo", "checksum_after_backup",
                          "checksum_source_before_copy", "checksum_target_before_copy",
                          "checksum_target_after_copy", "copy_attempts", "copy_max_retry",
                          "copy_started_at", "copy_ended_at", "verdict", *I.AV_SCAN_COLUMNS),
    "copy_attempts": ("run_file_id", "attempt_no", "source_server_id", "target_server_id", "file_name",
                      "file_type", "source_path", "target_path", "source_host_path", "target_host_path",
                      "started_at", "ended_at",
                      "bytes_copied", "copy_engine", "checksum_source_before_copy",
                      "checksum_target_after_copy", "size_target_after_copy", "result", "error_text"),
}


def _known(table: str, values: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(values) - set(_COLUMNS[table]))
    if unknown:
        raise BackupCatalogError(f"{table} has no column(s) {', '.join(unknown)}.")
    return values


class BackupCatalogStore:
    """The six catalog tables (SQLite or PostgreSQL, per the store declaration).

    Writers call one method per event - a run started, a file written, a file checked, an attempt
    made - so a run that dies half way leaves every row it reached, which is what the evidence is
    for. Each method opens its own transaction.
    """

    def __init__(self, source: "str | Path | StoreTarget | StoreConfig", *,
                 key: str | None = None, password: str | None = None) -> None:
        self.target = StoreTarget.coerce(source, key=key, password=password)
        self.sqlite_path = self.target.sqlite_path

    @classmethod
    def from_config(cls, config, *, key: str | None = None,
                    password: str | None = None) -> "BackupCatalogStore":
        return cls(StoreTarget.from_config(config, key=key, password=password))

    @property
    def backend(self) -> str:
        return self.target.store.backend

    def connect(self):
        return self.target.connect()

    def initialize(self, *, force: bool = False) -> None:
        if not force and backend_mod.schema_is_ready("BackupCatalogStore", self.target):
            return
        if not force and backend_mod.remote_schema_is_current(
                self.target, "BackupCatalogStore", CATALOG_SCHEMA_VERSION):
            backend_mod.mark_schema_ready("BackupCatalogStore", self.target)
            return
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            _migrate(conn, self.backend)
            _install_first_checksum_guard(conn, self.backend)
            backend_mod.record_schema_version(conn, "BackupCatalogStore", CATALOG_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("BackupCatalogStore", self.target)

    # ------------------------------------------------------------------ #
    # Backup side
    # ------------------------------------------------------------------ #
    def start_backup_run(self, **values: Any) -> int:
        values = _known("backup_runs", {"started_at": utc_now_text(), **values})
        _require(values, "backup_id", "server_id", "engine")
        _one_of(values, "engine", I.ENGINES)
        _one_of(values, "level", I.BACKUP_LEVELS)
        self.initialize()
        with self.connect() as conn:
            return _insert(conn, "backup_runs", values)

    def finish_backup_run(self, backup_run_id: int, *, status: str, **values: Any) -> None:
        _one_of({"status": status}, "status", I.BACKUP_RUN_STATUSES)
        values = _known("backup_runs", {"ended_at": utc_now_text(), "status": status, **values})
        _one_of(values, "engine_verify", I.ENGINE_VERIFY_RESULTS)
        self.initialize()
        with self.connect() as conn:
            _update(conn, "backup_runs", "backup_run_id", int(backup_run_id), values)

    def record_backup(self, *, run: dict[str, Any], files: list[dict[str, Any]]) -> int:
        """A finished backup run and every file it wrote, in one transaction.

        Written at the end because the files are known only then - the script prints them as its
        receipt. One transaction, so the store never holds a run with half its files. A DIFF, an
        incremental or a log is linked to the newest FULL of the same database on the same server
        (``chain_parent_file_id``), which is what lets a restore read its chain from here.
        """
        run = _known("backup_runs", dict(run))
        _require(run, "backup_id", "server_id", "engine", "started_at", "status")
        _one_of(run, "engine", I.ENGINES)
        _one_of(run, "level", I.BACKUP_LEVELS)
        _one_of(run, "status", I.BACKUP_RUN_STATUSES)
        rows = [_known("backup_files", dict(item)) for item in files]
        for item in rows:
            _require(item, "server_id", "engine", "file_name", "file_type", "file_path", "verdict")
            _one_of(item, "file_type", I.FILE_TYPES)
            _one_of(item, "verdict", I.BACKUP_FILE_VERDICTS)
        full_types = tuple(t for t, level in I.LEVEL_OF_FILE_TYPE.items() if level == "full")
        self.initialize()
        with self.connect() as conn:
            backup_run_id = _insert(conn, "backup_runs", run)
            for item in rows:
                item["backup_run_id"] = backup_run_id
                if (I.LEVEL_OF_FILE_TYPE.get(item["file_type"]) in ("diff", "log")
                        and item.get("chain_parent_file_id") is None):
                    marks = ", ".join("?" for _ in full_types)
                    parent = conn.execute(
                        f"SELECT file_id FROM backup_files WHERE server_id = ? AND database_name = ? "
                        f"AND file_type IN ({marks}) AND state = 'present' "
                        f"ORDER BY file_id DESC LIMIT 1",
                        (item["server_id"], item.get("database_name") or "", *full_types)).fetchone()
                    if parent is not None:
                        item["chain_parent_file_id"] = int(parent["file_id"])
                _insert_chained(conn, item)
            return backup_run_id

    def verify_row_chain(self, *, backup_id: str = "") -> dict[str, Any]:
        """Walk each entry's rows in order and recompute their row_hash (1.224). A row edited after it
        was written breaks its own hash and every one after it; the first broken row is named. Rows
        from before the chain (no row_hash, older than the first hashed row) are counted, not judged;
        an unhashed row after a hashed one is a row written past the chain, and is a break."""
        self.initialize()
        clause, params = ("WHERE backup_id = ?", (backup_id,)) if backup_id else ("", ())
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT file_id, row_hash, {', '.join(CHAIN_FIELDS)} FROM backup_files {clause} "
                "ORDER BY backup_id, file_id", params).fetchall()]
        entries: dict[str, dict[str, Any]] = {}
        previous: dict[str, str | None] = {}
        for row in rows:
            entry = str(row["backup_id"] or "")
            state = entries.setdefault(entry, {"backup_id": entry, "rows": 0, "chained": 0, "before_chain": 0,
                                               "broken_at": None})
            state["rows"] += 1
            if state["broken_at"] is not None:
                continue
            if not row["row_hash"]:
                if entry in previous:
                    state["broken_at"] = {"file_id": row["file_id"], "file_name": row["file_name"],
                                          "why": "no row_hash after the chain began"}
                else:
                    state["before_chain"] += 1
                continue
            expected = row_hash(row, previous.get(entry))
            if expected != row["row_hash"]:
                state["broken_at"] = {"file_id": row["file_id"], "file_name": row["file_name"],
                                      "why": "the row is not what was written, or a row before it was removed"}
                continue
            previous[entry] = row["row_hash"]
            state["chained"] += 1
        broken = [e for e in entries.values() if e["broken_at"] is not None]
        return {"entries": sorted(entries.values(), key=lambda e: e["backup_id"]), "broken": len(broken)}

    def chain_files(self, *, backup_id: str) -> list[dict[str, Any]]:
        """Every file of an entry, whatever its state, with what makes a chain of it - the full it belongs
        to (``chain_parent_file_id``, NULL on the full itself) - and its verification (1.244). Not only
        the files still ``present``: a file the scrub found changed would drop out of its chain, and the
        chain would read as verified."""
        self.initialize()
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT file_id, database_name, file_type, file_name, chain_parent_file_id, "
                "COALESCE(backup_ended_at, created_at) AS ended_at, verdict, state, on_disk, checksum_matches, "
                "last_checked_at, last_check_result, offload_state, last_restore_at, last_restore_result "
                "FROM backup_files WHERE backup_id = ? ORDER BY file_id", (backup_id,)).fetchall()]

    def first_signal(self, *, backup_id: str) -> str | None:
        """The earliest moment a stored file of the entry was found changed or gone (1.244) - the first
        sign of an attack the catalog holds. A chain that ended before it is a clean candidate."""
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MIN(c.checked_at) AS at FROM backup_file_checks c JOIN backup_files f ON f.file_id = c.file_id "
                "WHERE f.backup_id = ? AND c.result IN ('mismatch', 'missing')", (backup_id,)).fetchone()
        return str(row["at"]) if row is not None and row["at"] else None

    def oldest_kept_base(self, *, server_id: str) -> str | None:
        """When the oldest full base backup the catalog still holds as present on a server began - the
        line ``pg_archivecleanup`` cuts the WAL archive at (its START WAL)."""
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MIN(COALESCE(backup_started_at, backup_ended_at, created_at)) AS at FROM backup_files "
                "WHERE server_id = ? AND file_type = 'FULL_BASE' AND state = 'present'", (server_id,)).fetchone()
        return str(row["at"]) if row is not None and row["at"] else None

    def retention_by_entry(self, *, server_id: str = "") -> list[dict[str, Any]]:
        """Each backup entry's retention as its newest run recorded it (1.233) - what the pages show,
        read from the store: the reports never read the backup app's configuration."""
        self.initialize()
        clause, params = ("AND r.server_id = ? ", (server_id,)) if server_id else ("", ())
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT r.backup_id, r.server_id, r.engine, r.retention_json, r.pitr_reach_seconds, r.started_at "
                "FROM backup_runs r WHERE r.retention_json IS NOT NULL " + clause +
                "AND r.backup_run_id = (SELECT MAX(r2.backup_run_id) FROM backup_runs r2 "
                "WHERE r2.backup_id = r.backup_id AND r2.retention_json IS NOT NULL) "
                "ORDER BY r.server_id, r.backup_id", params).fetchall()]

    def backup_run_files(self, backup_run_id: int) -> list[dict[str, Any]]:
        """The files one backup run wrote."""
        self.initialize()
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT file_id, file_name, database_name, file_type, size_bytes FROM backup_files "
                "WHERE backup_run_id = ? ORDER BY file_id", (int(backup_run_id),)).fetchall()]

    def earlier_sizes(self, *, backup_id: str, database_name: str, file_type: str, before_run: int | None,
                      limit: int = 20) -> list[int]:
        """The sizes of the same entry's, database's and type's files before this run, newest first -
        the history a new file's size is held to (1.219)."""
        self.initialize()
        with self.connect() as conn:
            return [int(row["size_bytes"]) for row in conn.execute(
                "SELECT size_bytes FROM backup_files WHERE backup_id = ? AND database_name = ? AND file_type = ? "
                "AND size_bytes IS NOT NULL AND (? IS NULL OR backup_run_id IS NULL OR backup_run_id < ?) "
                "ORDER BY file_id DESC LIMIT ?",
                (backup_id, database_name, file_type, before_run, before_run, int(limit))).fetchall()]

    def stored_files(self, *, server_id: str, file_names: list[str]) -> dict[str, dict[str, Any]]:
        """The catalog's row for each named file of a server, newest first - what a restore's copy
        holds a file to. Backup file names carry their database and a UTC stamp, so a name is the
        file on that server. Pruned rows are left out: their file is gone."""
        if not file_names:
            return {}
        self.initialize()
        found: dict[str, dict[str, Any]] = {}
        with self.connect() as conn:
            for start in range(0, len(file_names), 400):
                chunk = file_names[start:start + 400]
                marks = ", ".join("?" for _ in chunk)
                for row in conn.execute(
                        f"SELECT * FROM backup_files WHERE server_id = ? AND file_name IN ({marks}) "
                        f"AND state <> 'pruned' AND checksum_after_backup IS NOT NULL ORDER BY file_id DESC",
                        (server_id, *chunk)):
                    found.setdefault(str(row["file_name"]), dict(row))
        return found

    def last_file_name(self, *, backup_id: str, file_type: str) -> str:
        """The newest file name catalogued for an entry and type - where a WAL job continues from."""
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT file_name FROM backup_files WHERE backup_id = ? AND file_type = ? "
                "ORDER BY file_name DESC LIMIT 1", (backup_id, file_type)).fetchone()
        return str(row["file_name"]) if row is not None else ""

    def record_backup_file(self, **values: Any) -> int:
        """One file a backup wrote. ``checksum_after_backup`` absent only with ``verdict='hash_failed'``."""
        values = _known("backup_files", values)
        _require(values, "server_id", "engine", "file_name", "file_type", "file_path", "verdict")
        _one_of(values, "file_type", I.FILE_TYPES)
        _one_of(values, "verdict", I.BACKUP_FILE_VERDICTS)
        _one_of(values, "av_scan_status", I.AV_SCAN_STATUSES)
        self.initialize()
        with self.connect() as conn:
            return _insert_chained(conn, values)

    def set_backup_file(self, file_id: int, **values: Any) -> None:
        """Change a file's state, its latest check or its virus scan - never its recorded checksum."""
        values = _known("backup_files", values)
        if "checksum_after_backup" in values:
            # Refused outright here, not only when it differs: a file's checksum is set by the
            # backup that wrote it (record_backup / record_backup_file), and by nothing after.
            raise BackupCatalogError(
                "checksum_after_backup is what the backup recorded and is never rewritten; a later "
                "hash is a check (record_file_check).")
        _one_of(values, "state", I.BACKUP_FILE_STATES)
        _one_of(values, "av_scan_status", I.AV_SCAN_STATUSES)
        _one_of(values, "offload_state", I.OFFLOAD_STATES)
        self.initialize()
        with self.connect() as conn:
            if "source_deleted_at" in values or "offload_state" in values:
                _hold_offload(conn, int(file_id), values)
            _update(conn, "backup_files", "file_id", int(file_id), {**values, "updated_at": utc_now_text()})

    def files_to_offload(self, *, backup_id: str, limit: int = 200, retry_before: str = "") -> list[dict[str, Any]]:
        """An entry's backup files still to be copied to its repository, oldest first (1.240).

        Every file the entry's backups wrote and that is still on its server, intact (``verdict``
        ``ok``) - not only the ones written since the entry turned the offload on: a repository that
        holds tonight's LOGs but not the FULL under them restores nothing. A ``failed`` copy is
        tried again; ``verified`` and ``quarantined`` are done with. A FULL before the files that
        depend on it, so a pass cut short leaves a usable prefix of the chain on the repository.

        ``retry_before``: a ``failed`` copy is taken only when its last attempt is older - filtered
        here, before the LIMIT, so a hundred failures waiting out their pause never fill the batch
        and starve the files written since.
        """
        self.initialize()
        full_types = tuple(t for t, level in I.LEVEL_OF_FILE_TYPE.items() if level == "full")
        marks = ", ".join("?" for _ in full_types)
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM backup_files WHERE backup_id = ? AND state = 'present' AND verdict = 'ok' "
                "AND (offload_state IS NULL OR offload_state = 'pending' OR (offload_state = 'failed' "
                "AND (? = '' OR COALESCE(offload_attempted_at, '') < ?))) "
                f"ORDER BY COALESCE(backup_ended_at, created_at), CASE WHEN file_type IN ({marks}) THEN 0 ELSE 1 END, "
                "file_id LIMIT ?", (backup_id, retry_before, retry_before, *full_types, int(limit)))]

    @staticmethod
    def _chain_floor_sql(full_types: tuple[str, ...], *, outer: str) -> str:
        """The moment the newest chain of ``outer``'s database begins: the START of its newest full's
        run (``backup_runs.started_at``). Not its end - a LOG or WAL written while that full ran
        belongs to it: PostgreSQL needs the WAL of a base backup's own window to make it consistent."""
        marks = ", ".join("?" for _ in full_types)
        return ("(SELECT MAX(COALESCE(fr.started_at, ff.backup_started_at, ff.backup_ended_at, ff.created_at)) "
                "FROM backup_files ff LEFT JOIN backup_runs fr ON fr.backup_run_id = ff.backup_run_id "
                f"WHERE ff.backup_id = {outer}.backup_id AND ff.database_name = {outer}.database_name "
                f"AND ff.state = 'present' AND ff.verdict = 'ok' AND ff.file_type IN ({marks}))")

    def newest_full_per_database(self, *, backup_id: str) -> list[dict[str, Any]]:
        """Each database's newest chain start of an entry, ``{database_name, at}`` - the floor below
        which a source may be released (1.246): the newest chain stays (see _chain_floor_sql)."""
        self.initialize()
        full_types = tuple(t for t, level in I.LEVEL_OF_FILE_TYPE.items() if level == "full")
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                f"SELECT d.database_name, {self._chain_floor_sql(full_types, outer='d')} AS at "
                "FROM (SELECT DISTINCT backup_id, database_name FROM backup_files WHERE backup_id = ?) d",
                (*full_types, backup_id))]

    def repository_copies(self, *, backup_id: str) -> list[dict[str, Any]]:
        """An entry's verified repository copies, oldest first, each with ``chain_start`` - for a full,
        the start of the run that wrote it - what the repository's retention reads (expiry_plan)."""
        self.initialize()
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT bf.*, COALESCE(br.started_at, bf.backup_started_at, bf.backup_ended_at, bf.created_at) "
                "AS chain_start FROM backup_files bf LEFT JOIN backup_runs br ON br.backup_run_id = bf.backup_run_id "
                "WHERE bf.backup_id = ? AND bf.offload_state = 'verified' "
                "ORDER BY COALESCE(bf.backup_ended_at, bf.created_at), bf.file_id", (backup_id,))]

    def has_repository_copy_before(self, *, backup_id: str, before: str) -> bool:
        """Whether any verified repository copy of the entry ended before ``before`` - the cheap
        question the retention asks every pass before it reads the whole set."""
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM backup_files WHERE backup_id = ? AND offload_state = 'verified' "
                "AND COALESCE(backup_ended_at, created_at) < ? LIMIT 1", (backup_id, before)).fetchone() is not None

    def repository_locations(self, *, server_id: str, under: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """A server's verified repository copies of files under ``under`` (a folder as the server names
        it), newest first: ``{offload_server_id, host_path, file_path, offload_path}`` - what a restore
        from the repository maps its folder with. Filtered by the folder here, so the newest LOGs of
        some other folder never crowd out the copies the restore asks about."""
        self.initialize()
        prefix = under.replace("\\", "/").rstrip("/")
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT offload_server_id, host_path, file_path, offload_path FROM backup_files "
                "WHERE server_id = ? AND offload_state = 'verified' AND offload_path IS NOT NULL "
                "AND (? = '' OR REPLACE(COALESCE(NULLIF(host_path, ''), file_path), '\\', '/') LIKE ?) "
                "ORDER BY COALESCE(offload_verified_at, created_at) DESC, file_id DESC LIMIT ?",
                (server_id, prefix, prefix + "/%", int(limit)))]

    def repository_proofs(self, *, server_id: str, repository_server_id: str) -> dict[str, dict[str, Any]]:
        """Every file of ``server_id``'s backups on its repository, by its path there, with the checksum
        and size its offload PROVED it at (0.34.0, restore_from repository).

        Taken from the offload runs' own file rows - each the repository path, held to the source's
        checksum when it was copied - so a member of a base backup directory has its proof too, not
        only the files a backup recorded one by one. Only copies whose backup file is still
        ``verified``: an expired or failed copy proves nothing. The newest proof of a path wins.
        """
        self.initialize()
        proven = "(" + ", ".join(f"'{v}'" for v in I.PROVEN_VERDICTS) + ")"
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT rf.target_path, rf.checksum_target_after_copy, rf.size_target_bytes, rf.size_source_bytes, "
                "rf.file_id, rf.file_type, rf.file_name, rf.database_name, bf.backup_ended_at, rf.run_file_id "
                "FROM restore_run_files rf JOIN restore_runs rr ON rr.restore_run_id = rf.restore_run_id "
                "JOIN backup_files bf ON bf.file_id = rf.file_id "
                f"WHERE rr.action = '{I.ACTION_OFFLOAD}' AND rr.source_server_id = ? AND rr.target_server_id = ? "
                f"AND rf.verdict IN {proven} AND bf.offload_state = 'verified' "
                "ORDER BY rf.run_file_id", (server_id, repository_server_id)).fetchall()
        found: dict[str, dict[str, Any]] = {}
        for row in rows:
            found[str(row["target_path"])] = {
                "checksum_after_backup": row["checksum_target_after_copy"],
                "size_bytes": row["size_target_bytes"] if row["size_target_bytes"] is not None else row["size_source_bytes"],
                "file_id": row["file_id"], "file_type": row["file_type"], "file_name": row["file_name"],
                "database_name": row["database_name"], "backup_ended_at": row["backup_ended_at"]}
        return found

    def files_to_release(self, *, backup_id: str, limit: int = 500) -> list[dict[str, Any]]:
        """An entry's files ``offload_delete_source`` may delete (1.246), oldest first: the repository
        copy verified, the source still on the server, older than the newest chain of its database,
        and not a base backup directory.

        The rules are in the query, not only in the caller: a file that may never be released - the
        newest chain - left in this answer would make every pass a run with nothing to do, and with a
        LIMIT it would crowd out the files that may.
        """
        self.initialize()
        full_types = tuple(t for t, level in I.LEVEL_OF_FILE_TYPE.items() if level == "full")
        directories = tuple(I.HASHED_MEMBER)
        marks = ", ".join("?" for _ in directories)
        floor = self._chain_floor_sql(full_types, outer="bf")
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT bf.* FROM backup_files bf WHERE bf.backup_id = ? AND bf.state = 'present' "
                "AND bf.offload_state = 'verified' AND bf.source_deleted_at IS NULL "
                f"AND bf.file_type NOT IN ({marks}) "
                f"AND COALESCE(bf.backup_ended_at, bf.created_at) < {floor} "
                "ORDER BY COALESCE(bf.backup_ended_at, bf.created_at), bf.file_id LIMIT ?",
                (backup_id, *directories, *full_types, int(limit)))]

    def record_file_check(self, **values: Any) -> int:
        """One later hash (or virus scan) of a stored file; the file's latest-check columns follow it."""
        values = _known("backup_file_checks", {"checked_at": _check_moment(), **values})
        _require(values, "file_id", "server_id", "file_name", "file_type", "file_path", "checked_by")
        _one_of(values, "checked_by", I.CHECKED_BY)
        _one_of(values, "result", I.CHECK_RESULTS)
        self.initialize()
        with self.connect() as conn:
            check_id = _insert(conn, "backup_file_checks", values)
            latest: dict[str, Any] = {}
            # The latest-check columns speak for the file ON ITS SERVER. A check of its repository
            # copy (1.240) is a row of the history, and the copy's own state is offload_state - a
            # copy found changed must never make the source read "mismatch", nor the reverse.
            if values.get("result") and values.get("location", "source") == "source":
                result = values["result"]
                latest = {"checksum_last_check": values.get("checksum_now"),
                          "last_checked_at": values["checked_at"], "last_check_result": result,
                          "on_disk": 0 if result == "missing" else 1,
                          "checksum_matches": {"match": 1, "mismatch": 0}.get(result),
                          # A file found again with its bytes intact is present again.
                          "state": "present" if result == "match" else result}
                row = conn.execute("SELECT state FROM backup_files WHERE file_id = ?",
                                   (int(values["file_id"]),)).fetchone()
                if row is not None and row["state"] == "pruned":
                    # Deleted on purpose (a retention, a released source): "missing" is what it is
                    # meant to be, never an alert, and the path stays free for a later backup.
                    latest.pop("state")
            if values.get("checked_by") == "av_scan":
                latest.update({name: values.get(name) for name in I.AV_SCAN_COLUMNS})
            if latest:
                _update(conn, "backup_files", "file_id", int(values["file_id"]),
                        {**latest, "updated_at": utc_now_text()})
            return check_id

    def check_backup_file(self, file_id: int, *, checksum_now: str | None, size_now: int | None = None,
                          checked_by: str = "scrub", by_run_id: str = "", location: str = "source",
                          checked_at: str | None = None) -> str:
        """Hold a file found on disk now - or not found: ``checksum_now=None`` - to the checksum this
        catalog STORED when the backup wrote it, and record the answer. Returns match | mismatch |
        missing.

        The comparison is made here, against the stored value, never by a caller with a hash of its
        own: the operator's question is *is the file still the one the backup wrote*, and only the
        catalog holds that answer.
        """
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM backup_files WHERE file_id = ?", (int(file_id),)).fetchone()
        if row is None:
            raise BackupCatalogError(f"no backup file {file_id} in the catalog.")
        stored = row["checksum_after_backup"]
        now = (checksum_now or "").lower() or None
        result = "missing" if now is None else ("match" if stored and now == stored else "mismatch")
        self.record_file_check(
            file_id=int(file_id), server_id=row["server_id"], database_name=row["database_name"],
            file_name=row["file_name"], file_type=row["file_type"], file_path=row["file_path"],
            checked_at=checked_at or _check_moment(), checked_by=checked_by, by_run_id=by_run_id,
            location=location, checksum_after_backup=stored, checksum_now=now, size_now=size_now,
            result=result)
        return result

    def files_to_check(self, *, server_id: str = "", backup_id: str = "", limit: int = 500) -> list[dict[str, Any]]:
        """Catalogued backup files not pruned, the ones checked longest ago (or never) first."""
        self.initialize()
        where = ["state <> 'pruned'"]
        params: list[Any] = []
        if server_id:
            where.append("server_id = ?")
            params.append(server_id)
        if backup_id:
            where.append("backup_id = ?")
            params.append(backup_id)
        sql = (f"SELECT * FROM backup_files WHERE {' AND '.join(where)} "
               "ORDER BY CASE WHEN last_checked_at IS NULL THEN 0 ELSE 1 END, last_checked_at, file_id LIMIT ?")
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, (*params, int(limit)))]

    # ------------------------------------------------------------------ #
    # Restore and copy-only side
    # ------------------------------------------------------------------ #
    def start_restore_run(self, **values: Any) -> int:
        values = _known("restore_runs", {"started_at": utc_now_text(), **values})
        _require(values, "action", "source_server_id", "target_server_id")
        if values.get("copy_max_retry") is None:
            # Never filled in here: the entry states it (lib.integrity), and a run that does not
            # know it has not read its entry.
            raise BackupCatalogError("copy_max_retry is required - the entry's integrity.copy_max_retry.")
        _one_of(values, "action", I.RUN_ACTIONS)
        _one_of(values, "phase", I.RESTORE_PHASES)
        self.initialize()
        with self.connect() as conn:
            return _insert(conn, "restore_runs", values)

    def set_restore_run(self, restore_run_id: int, **values: Any) -> None:
        values = _known("restore_runs", values)
        _one_of(values, "phase", I.RESTORE_PHASES)
        self.initialize()
        with self.connect() as conn:
            _update(conn, "restore_runs", "restore_run_id", int(restore_run_id), values)
            _mark_used(conn, restore_run_id=int(restore_run_id))

    def add_run_file(self, **values: Any) -> int:
        """One file of a run, written once its source has been hashed (or found missing)."""
        values = _known("restore_run_files", values)
        _require(values, "restore_run_id", "source_server_id", "target_server_id", "file_name",
                 "file_type", "source_path", "target_path")
        if values.get("copy_max_retry") is None:
            raise BackupCatalogError("copy_max_retry is required - the entry's integrity.copy_max_retry.")
        _one_of(values, "file_type", I.FILE_TYPES)
        _one_of(values, "verdict", I.RUN_FILE_VERDICTS)
        self.initialize()
        with self.connect() as conn:
            run_file_id = _insert(conn, "restore_run_files", values)
            _mark_used(conn, run_file_id=run_file_id)
            return run_file_id

    def set_run_file(self, run_file_id: int, **values: Any) -> None:
        values = _known("restore_run_files", values)
        _one_of(values, "verdict", I.RUN_FILE_VERDICTS)
        _one_of(values, "av_scan_status", I.AV_SCAN_STATUSES)
        self.initialize()
        with self.connect() as conn:
            _update(conn, "restore_run_files", "run_file_id", int(run_file_id),
                    {**values, "updated_at": utc_now_text()})
            _mark_used(conn, run_file_id=int(run_file_id))

    def check_run_file(self, run_file_id: int, *, side: str, checksum_now: str | None,
                       checked_at: str | None = None) -> str:
        """One side of a copied file, found on disk now (or not: ``checksum_now=None``), held to the
        checksum dbabrain STORED for it - the backup's ``checksum_after_backup``, or, for a file no
        backup recorded, the source's hash taken at the copy. ``side`` is ``source`` or ``target``.
        Sets that side's ``*_on_disk``, ``*_checksum_matches`` and ``*_checked_at``; returns
        match | mismatch | missing.
        """
        if side not in ("source", "target"):
            raise BackupCatalogError(f"side must be source or target; got {side!r}.")
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT checksum_after_backup, checksum_source_before_copy "
                               "FROM restore_run_files WHERE run_file_id = ?", (int(run_file_id),)).fetchone()
        if row is None:
            raise BackupCatalogError(f"no copied file {run_file_id} in the catalog.")
        stored = row["checksum_after_backup"] or row["checksum_source_before_copy"]
        now = (checksum_now or "").lower() or None
        result = "missing" if now is None else ("match" if stored and now == stored else "mismatch")
        self.set_run_file(run_file_id, **{
            f"{side}_on_disk": 0 if result == "missing" else 1,
            f"{side}_checksum_matches": {"match": 1, "mismatch": 0}.get(result),
            f"{side}_checked_at": checked_at or utc_now_text()})
        return result

    def record_copy_attempt(self, **values: Any) -> int:
        values = _known("copy_attempts", {"started_at": utc_now_text(), **values})
        _require(values, "run_file_id", "attempt_no", "source_server_id", "target_server_id",
                 "file_name", "file_type", "source_path", "target_path",
                 "checksum_source_before_copy", "result")
        _one_of(values, "file_type", I.FILE_TYPES)
        _one_of(values, "result", I.ATTEMPT_RESULTS)
        self.initialize()
        with self.connect() as conn:
            return _insert(conn, "copy_attempts", values)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def backup_history(self, *, server_id: str, since: str = "", database_name: str = "",
                       limit: int = 500) -> list[dict[str, Any]]:
        """A server's backup files, oldest first - what ``db.cli backup-history`` prints."""
        self.initialize()
        sql = ("SELECT * FROM backup_files WHERE server_id = ?"
               + (" AND backup_ended_at >= ?" if since else "")
               + (" AND database_name = ?" if database_name else "")
               + " ORDER BY database_name, backup_ended_at, file_id LIMIT ?")
        params = [server_id, *([since] if since else []), *([database_name] if database_name else []),
                  int(limit)]
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params))]

    def catalogued_servers(self) -> list[str]:
        """Every server the catalog holds a backup file of, or a copy from or onto - the fleet page's
        rows (backup-restore.html)."""
        self.initialize()
        with self.connect() as conn:
            found = {str(r["server_id"]) for r in conn.execute("SELECT DISTINCT server_id FROM backup_files")}
            for r in conn.execute("SELECT DISTINCT source_server_id, target_server_id FROM restore_run_files"):
                found.update({str(r["source_server_id"]), str(r["target_server_id"])})
        return sorted(server for server in found if server)

    def drill_runs(self, *, since: str) -> list[dict[str, Any]]:
        """Every restore drill since ``since`` (a copy-only or an offload is no drill), newest first:
        what the Recoverability section counts per entry and per tier (1.253)."""
        self.initialize()
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT restore_run_id, restore_id, source_server_id, target_server_id, tier, phase, "
                "databases_ok, databases_failed, started_at, ended_at, error_text, refused_reason "
                "FROM restore_runs WHERE action = ? AND started_at >= ? ORDER BY started_at DESC",
                (I.ACTION_RESTORE, since)).fetchall()]

    def coverage(self) -> list[dict[str, Any]]:
        """Per server and database, what its present backups cover (1.252): the newest file of each
        level, how many are copied off the host, and when one of them was last used by a restore."""
        self.initialize()
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT server_id, database_name, engine, file_type, COUNT(*) AS files, "
                "MAX(COALESCE(backup_ended_at, created_at)) AS newest, "
                "SUM(CASE WHEN offload_state = 'verified' THEN 1 ELSE 0 END) AS offloaded, "
                "MAX(last_restore_at) AS last_restored "
                "FROM backup_files WHERE state = 'present' "
                "GROUP BY server_id, database_name, engine, file_type ORDER BY server_id, database_name").fetchall()]

    def server_backup_files(self, server_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """A server's backup files, newest first - the server page's *Backup & restore files* (1.217).

        Each row also carries its own restore mark (``last_restore_*``) and two counts: every restore
        or copy that USED the file (``used_count``), and the ones that **restored it successfully** -
        a proven copy (``match`` / ``copied`` / ``recopied``) in a ``restore`` run that ended ``done``
        (``restored_ok_count``, with the last such run's time and target). Matched by ``file_id``, or
        by the source server and the file name for a copy made before the file was catalogued.
        """
        self.initialize()
        proven = "(" + ", ".join(f"'{v}'" for v in I.PROVEN_VERDICTS) + ")"
        of_file = ("(rf.file_id = bf.file_id OR (rf.file_id IS NULL AND rf.source_server_id = bf.server_id "
                   "AND rf.file_name = bf.file_name))")
        restored = (f"FROM restore_run_files rf JOIN restore_runs rr ON rr.restore_run_id = rf.restore_run_id "
                    f"WHERE {of_file} AND rf.verdict IN {proven} "
                    f"AND rr.action = 'restore' AND rr.phase = 'done'")
        sql = (f"SELECT bf.*, "
               f"(SELECT COUNT(*) FROM restore_run_files rf JOIN restore_runs ru ON ru.restore_run_id = rf.restore_run_id "
               f"WHERE {of_file} AND ru.action <> '{I.ACTION_OFFLOAD}') AS used_count, "
               f"(SELECT COUNT(*) {restored}) AS restored_ok_count, "
               f"(SELECT MAX(rr.ended_at) {restored}) AS last_restored_ok_at, "
               f"(SELECT rr.target_server_id {restored} ORDER BY rr.ended_at DESC LIMIT 1) AS last_restored_to "
               f"FROM backup_files bf WHERE bf.server_id = ? "
               f"ORDER BY COALESCE(bf.backup_ended_at, bf.created_at) DESC, bf.file_id DESC LIMIT ?")
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, (server_id, int(limit)))]

    def server_restore_files(self, server_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """Every file copied or restored from or onto a server, newest first, with its run's
        outcome - the server page's *Restore files* section (1.217). A run that ended ``done`` is a
        restore (or copy) that finished and was verified; its proven files are the tested ones."""
        self.initialize()
        sql = ("SELECT rf.*, rr.action, rr.restore_id, rr.phase AS run_phase, rr.started_at AS run_started_at, "
               "rr.ended_at AS run_ended_at, rr.point_in_time, rr.refused_reason "
               "FROM restore_run_files rf JOIN restore_runs rr ON rr.restore_run_id = rf.restore_run_id "
               "WHERE rf.source_server_id = ? OR rf.target_server_id = ? "
               "ORDER BY rr.started_at DESC, rf.chain_position, rf.run_file_id DESC LIMIT ?")
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, (server_id, server_id, int(limit)))]

    def run_files(self, restore_run_id: int) -> list[dict[str, Any]]:
        """One run's files in chain order - what ``db.cli copy-history`` prints."""
        self.initialize()
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM restore_run_files WHERE restore_run_id = ? "
                "ORDER BY chain_position, run_file_id", (int(restore_run_id),))]

    def attempts(self, run_file_id: int) -> list[dict[str, Any]]:
        self.initialize()
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM copy_attempts WHERE run_file_id = ? ORDER BY attempt_no",
                (int(run_file_id),))]
