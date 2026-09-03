from __future__ import annotations

import json
import sqlite3
import getpass
import socket
from typing import Any
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db_ops.config import StoreConfig
from db_ops.db.job_runs import JobRun
from db_ops.db import backend as backend_mod
from db_ops.db.backend import StoreTarget


#: 2 — added telegram_send_messages.message_type and the job_runs_history archive table.
SCHEMA_VERSION = 2

#: Columns copied verbatim when a job_runs row ages into job_runs_history. Listed rather than
#: `SELECT *` so a future column added to job_runs fails loudly here instead of silently
#: dropping out of the archive.
_JOB_RUN_ARCHIVE_COLUMNS = (
    "log_id, created_at, started_at, finished_at, job_code, level, status, message, "
    "duration_ms, error_text, host_name, metadata_json"
)


class DbOpsStore:
    """The db_ops runtime store.

    Runs on SQLite or PostgreSQL, decided by ``data/store_config.json``. A bare path still means
    SQLite (every existing caller and test passes one); use :meth:`from_config` to follow the
    declared backend. See :mod:`db_ops.db.backend` and ``docs/01_runtime_store.md``.
    """

    def __init__(
        self,
        source: "str | Path | StoreTarget | StoreConfig",
        *,
        key: str | None = None,
        password: str | None = None,
    ) -> None:
        self.target = StoreTarget.coerce(source, key=key, password=password)
        # Kept as an attribute because callers, tests and schema_export read it directly.
        self.sqlite_path = self.target.sqlite_path

    @classmethod
    def from_config(cls, config, *, key: str | None = None, password: str | None = None) -> "DbOpsStore":
        """Open the store the config declares - SQLite or PostgreSQL."""
        return cls(StoreTarget.from_config(config, key=key, password=password))

    @property
    def backend(self) -> str:
        return self.target.store.backend

    def initialize(self, *, force: bool = False) -> None:
        """Create/upgrade this store's schema.

        ``force`` skips both the in-process memo and the recorded schema version, so the DDL and the
        additive migrations run even when everything looks current. That is what
        ``db_ops.db.cli init`` uses: an explicit "build the schema" request should do the work, and
        it is the only way to re-run a repair (an identity-sequence resync, say) on a store whose
        recorded version has not changed.
        """
        # Called at the top of ~40 methods here. Once per process is enough - see
        # db_ops.db.backend.schema_is_ready for why that matters on PostgreSQL.
        if not force and backend_mod.schema_is_ready("DbOpsStore", self.target):
            return
        # Cross-process check: the daemon's app commands are new processes every run, so the
        # in-process memo above never helps them. schema_meta answers "already built?" with one
        # indexed SELECT instead of ~55 DDL statements per process.
        if not force and backend_mod.remote_schema_is_current(self.target, "DbOpsStore", SCHEMA_VERSION):
            backend_mod.mark_schema_ready("DbOpsStore", self.target)
            return
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            migrate_telegram_command_messages_table(conn)
            migrate_telegram_send_messages_table(conn)
            migrate_reports_table(conn)
            migrate_report_send_state_table(conn)
            ensure_metric_results_table(conn)
            ensure_sqlite_column(
                conn,
                table_name="telegram_command_messages",
                column_name="command_status",
                column_sql="command_status INTEGER NOT NULL DEFAULT 0",
            )
            ensure_sqlite_column(
                conn,
                table_name="telegram_command_messages",
                column_name="processed_at",
                column_sql="processed_at TEXT NULL",
            )
            ensure_sqlite_column(
                conn,
                table_name="telegram_command_messages",
                column_name="process_note",
                column_sql="process_note TEXT NULL",
            )
            ensure_sqlite_column(
                conn,
                table_name="telegram_command_messages",
                column_name="command_id",
                column_sql="command_id INTEGER NULL",
            )
            ensure_sqlite_column(
                conn,
                table_name="telegram_command_messages",
                column_name="reply_message_id",
                column_sql="reply_message_id INTEGER NULL",
            )
            # A command message stays command_status=0 (pending) until its action finishes. The
            # Telegram workflow runs every second, so a command that takes longer than one cycle
            # — or a workflow that is killed mid-action — would be picked up and dispatched
            # again. claimed_at is the ownership marker that makes the pick-up exclusive.
            ensure_sqlite_column(
                conn,
                table_name="telegram_command_messages",
                column_name="claimed_at",
                column_sql="claimed_at TEXT NULL",
            )
            ensure_sqlite_column(
                conn,
                table_name="telegram_conversation_states",
                column_name="claimed_at",
                column_sql="claimed_at TEXT NULL",
            )
            # What kind of message this is, so the send layer stops guessing it from the text.
            # Nullable and never backfilled: an existing row says nothing and keeps falling back
            # to the header heuristic, so this migration cannot change what any queued message
            # already looks like.
            ensure_sqlite_column(
                conn,
                table_name="telegram_send_messages",
                column_name="message_type",
                column_sql="message_type TEXT NULL",
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_command_id
                    ON telegram_command_messages (command_id);
                """
            )
            conn.execute(
                """
                INSERT INTO schema_meta (schema_name, schema_version)
                VALUES ('db_ops', ?)
                ON CONFLICT(schema_name) DO UPDATE SET schema_version = excluded.schema_version;
                """,
                (SCHEMA_VERSION,),
            )
            # The rebuild migrations above copy rows with their ids, which does not advance a
            # PostgreSQL identity sequence. Re-base them before anything inserts.
            backend_mod.resync_identity_sequences(conn)
            backend_mod.record_schema_version(conn, "DbOpsStore", SCHEMA_VERSION)
        backend_mod.mark_schema_ready("DbOpsStore", self.target)
        backend_mod.mark_schema_ready("DbOpsStore", self.target)

    def connect(self):
        return self.target.connect()

    def insert_job_run(self, item: JobRun) -> int:
        self.initialize()
        created_at = utc_now_text()
        metadata_json = json.dumps(item.metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO job_runs
                (
                    created_at,
                    started_at,
                    finished_at,
                    job_code,
                    level,
                    status,
                    message,
                    duration_ms,
                    error_text,
                    host_name,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    created_at,
                    item.started_at,
                    item.finished_at,
                    item.job_code,
                    item.level,
                    item.status,
                    item.message,
                    item.duration_ms,
                    item.error_text,
                    item.host_name,
                    metadata_json,
                ),
            )
            return int(cursor.lastrowid)

    def fetch_recent_job_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        log_id,
                        created_at,
                        job_code,
                        level,
                        status,
                        message,
                        duration_ms
                    FROM job_runs
                    ORDER BY log_id DESC
                    LIMIT ?;
                    """,
                    (limit,),
                )
            )

    def fetch_terminal_job_runs(
        self, *, job_codes: list[str], since_created_at: str, limit: int = 50
    ) -> list[sqlite3.Row]:
        """Job-run rows whose ``job_code`` is one of ``job_codes`` and were created at or
        after ``since_created_at`` (ISO string), newest first. Used to detect a background
        command's authoritative completion from SQLite (e.g. the
        ``backup_restore.restore-workflow.end`` / ``.error`` record) instead of relying on
        the detached process staying alive and its stdout marker."""
        self.initialize()
        codes = [c for c in job_codes if c]
        if not codes:
            return []
        placeholders = ",".join("?" for _ in codes)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT log_id, created_at, job_code, status, message, error_text, metadata_json
                    FROM job_runs
                    WHERE job_code IN ({placeholders})
                      AND created_at >= ?
                    ORDER BY log_id DESC
                    LIMIT ?;
                    """,
                    (*codes, since_created_at, limit),
                )
            )

    def fetch_latest_job_runs_by_job_code(self) -> dict[str, sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    log_id,
                    created_at,
                    started_at,
                    finished_at,
                    job_code,
                    level,
                    status,
                    message,
                    duration_ms,
                    error_text,
                    host_name,
                    metadata_json
                FROM job_runs
                WHERE log_id IN (
                    SELECT max(log_id)
                    FROM job_runs
                    GROUP BY job_code
                );
                """
            ).fetchall()
        return {str(row["job_code"]): row for row in rows}

    def fetch_running_job_runs(self, job_code_prefix: str = "") -> list[sqlite3.Row]:
        """Every row still marked RUNNING, optionally limited to one job_code prefix.

        Unlike :meth:`fetch_latest_job_runs_by_job_code` this returns *all* of them: once a
        stale run has been overtaken by a newer one it is no longer the latest row for its
        job_code, but it is still open and still needs closing.
        """
        self.initialize()
        sql = """
            SELECT
                log_id,
                created_at,
                started_at,
                finished_at,
                job_code,
                level,
                status,
                message,
                duration_ms,
                error_text,
                host_name,
                metadata_json
            FROM job_runs
            WHERE lower(status) = 'running'
        """
        params: tuple = ()
        if job_code_prefix:
            sql += " AND job_code LIKE ?"
            params = (f"{job_code_prefix}%",)
        sql += " ORDER BY log_id;"
        with self.connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    def update_job_run(
        self,
        *,
        log_id: int,
        level: str,
        status: str,
        message: str,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        error_text: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.initialize()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE job_runs
                SET
                    finished_at = ?,
                    level = ?,
                    status = ?,
                    message = ?,
                    duration_ms = ?,
                    error_text = ?,
                    metadata_json = ?
                WHERE log_id = ?;
                """,
                (
                    finished_at,
                    level,
                    status,
                    message,
                    duration_ms,
                    error_text,
                    metadata_json,
                    log_id,
                ),
            )

    def upsert_telegram_messages(self, messages: list[dict]) -> int:
        self.initialize()
        if not messages:
            return 0

        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO telegram_messages
                (
                    update_id,
                    message_id,
                    message_date,
                    chat_id,
                    chat_type,
                    user_id,
                    text,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO NOTHING;
                """,
                [
                    (
                        item.get("update_id"),
                        item.get("message_id"),
                        item.get("message_date"),
                        item.get("chat_id", ""),
                        item.get("chat_type", ""),
                        item.get("user_id", ""),
                        item.get("text", ""),
                        json.dumps(item.get("raw") or {}, ensure_ascii=False, sort_keys=True),
                    )
                    for item in messages
                ],
            )
        return len(messages)

    def sync_telegram_command_messages(self, *, command_prefix: str = "/spbot") -> int:
        self.initialize()
        prefix = command_prefix.strip()
        if not prefix:
            raise ValueError("command_prefix is required.")

        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO telegram_command_messages
                (
                    telegram_message_id,
                    update_id,
                    message_id,
                    message_date,
                    chat_id,
                    chat_type,
                    user_id,
                    text,
                    command_prefix,
                    command_payload,
                    raw_json
                )
                SELECT
                    telegram_message_id,
                    update_id,
                    message_id,
                    message_date,
                    chat_id,
                    chat_type,
                    user_id,
                    text,
                    ?,
                    trim(substr(text, length(?) + 1)),
                    raw_json
                FROM telegram_messages
                WHERE text LIKE ? || '%'
                ON CONFLICT(chat_id, message_id) DO NOTHING;
                """,
                (prefix, prefix, prefix),
            )
        return int(cursor.rowcount)

    def fetch_pending_telegram_command_messages(self, limit: int = 50) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        telegram_command_message_id,
                        telegram_message_id,
                        update_id,
                        message_id,
                        message_date,
                        chat_id,
                        chat_type,
                        user_id,
                        text,
                        command_prefix,
                        command_payload,
                        command_id,
                        command_status,
                        reply_message_id,
                        processed_at,
                        raw_json
                    FROM telegram_command_messages
                    WHERE command_status = 0
                    ORDER BY message_date ASC, telegram_command_message_id ASC
                    LIMIT ?;
                    """,
                    (limit,),
                )
            )

    def claim_telegram_conversation_state(
        self,
        *,
        state_id: int,
        claimed_at: str,
        stale_before: str,
    ) -> bool:
        """Exclusive ownership of a waiting conversation state, for the same reason as
        :meth:`claim_telegram_command_message`: the state stays 'waiting' until its action
        finishes, so overlapping workflow cycles would each act on the same user reply."""
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_conversation_states
                SET claimed_at = ?
                WHERE state_id = ?
                  AND status = 'waiting'
                  AND (claimed_at IS NULL OR claimed_at < ?);
                """,
                (claimed_at, state_id, stale_before),
            )
            return cursor.rowcount == 1

    def claim_telegram_command_message(
        self,
        *,
        telegram_command_message_id: int,
        claimed_at: str,
        stale_before: str,
    ) -> bool:
        """Take exclusive ownership of a pending command message. True = this caller owns it.

        The claim is a single conditional UPDATE, so of two Telegram workflow cycles racing for
        the same message exactly one wins and the other skips it. ``stale_before`` re-opens a
        message whose owner died before finishing (e.g. the container was restarted mid-run),
        so a crash costs one retry rather than the command disappearing."""
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_command_messages
                SET claimed_at = ?
                WHERE telegram_command_message_id = ?
                  AND command_status = 0
                  AND (claimed_at IS NULL OR claimed_at < ?);
                """,
                (claimed_at, telegram_command_message_id, stale_before),
            )
            return cursor.rowcount == 1

    def fetch_telegram_command_message(
        self,
        *,
        telegram_command_message_id: int,
    ) -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    telegram_command_message_id,
                    telegram_message_id,
                    update_id,
                    message_id,
                    message_date,
                    chat_id,
                    chat_type,
                    user_id,
                    text,
                    command_prefix,
                    command_payload,
                    command_id,
                    command_status,
                    reply_message_id,
                    processed_at,
                    raw_json
                FROM telegram_command_messages
                WHERE telegram_command_message_id = ?;
                """,
                (telegram_command_message_id,),
            ).fetchone()

    def update_telegram_command_message_status(
        self,
        *,
        telegram_command_message_id: int,
        command_status: int,
        process_note: str = "",
        command_id: int | None = None,
        reply_message_id: int | None = None,
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_command_messages
                SET
                    command_status = ?,
                    command_id = COALESCE(?, command_id),
                    reply_message_id = COALESCE(?, reply_message_id),
                    processed_at = ?,
                    process_note = ?
                WHERE telegram_command_message_id = ?;
                """,
                (
                    command_status,
                    command_id,
                    reply_message_id,
                    utc_now_text(),
                    process_note,
                    telegram_command_message_id,
                ),
            )

    def upsert_telegram_conversation_state(
        self,
        *,
        chat_id: str,
        user_id: str,
        command_id: int,
        command_text: str,
        state_key: str,
        wait_after_message_id: int,
        source_telegram_command_message_id: int | None = None,
        state_data: dict | None = None,
    ) -> int:
        self.initialize()
        state_json = json.dumps(state_data or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_conversation_states
                SET status = 'replaced',
                    updated_at = ?
                WHERE chat_id = ?
                  AND user_id = ?
                  AND status = 'waiting';
                """,
                (utc_now_text(), chat_id, user_id),
            )
            cursor = conn.execute(
                """
                INSERT INTO telegram_conversation_states
                (
                    chat_id,
                    user_id,
                    command_id,
                    command_text,
                    state_key,
                    status,
                    wait_after_message_id,
                    source_telegram_command_message_id,
                    state_json
                )
                VALUES (?, ?, ?, ?, ?, 'waiting', ?, ?, ?);
                """,
                (
                    chat_id,
                    user_id,
                    command_id,
                    command_text,
                    state_key,
                    wait_after_message_id,
                    source_telegram_command_message_id,
                    state_json,
                ),
            )
            return int(cursor.lastrowid)

    def fetch_waiting_telegram_conversation_states(self, limit: int = 50) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        state_id,
                        chat_id,
                        user_id,
                        command_id,
                        command_text,
                        state_key,
                        status,
                        wait_after_message_id,
                        source_telegram_command_message_id,
                        state_json,
                        created_at,
                        updated_at
                    FROM telegram_conversation_states
                    WHERE status = 'waiting'
                    ORDER BY created_at ASC, state_id ASC
                    LIMIT ?;
                    """,
                    (limit,),
                )
            )

    def fetch_next_telegram_message_for_state(self, *, chat_id: str, user_id: str, after_message_id: int) -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    telegram_message_id,
                    update_id,
                    message_id,
                    message_date,
                    chat_id,
                    chat_type,
                    user_id,
                    text,
                    raw_json
                FROM telegram_messages
                WHERE chat_id = ?
                  AND user_id = ?
                  AND message_id > ?
                  AND (
                        trim(COALESCE(text, '')) <> ''
                        OR json_extract(raw_json, '$.document.file_id') IS NOT NULL
                      )
                  AND COALESCE(text, '') NOT LIKE '/spbot%'
                ORDER BY message_id ASC
                LIMIT 1;
                """,
                (chat_id, user_id, after_message_id),
            ).fetchone()

    def update_telegram_conversation_state(
        self,
        *,
        state_id: int,
        status: str,
        state_data: dict | None = None,
        consumed_telegram_message_id: int | None = None,
        note: str = "",
    ) -> None:
        self.initialize()
        state_json = json.dumps(state_data or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_conversation_states
                SET
                    status = ?,
                    consumed_telegram_message_id = COALESCE(?, consumed_telegram_message_id),
                    state_json = ?,
                    note = ?,
                    updated_at = ?
                WHERE state_id = ?;
                """,
                (status, consumed_telegram_message_id, state_json, note, utc_now_text(), state_id),
            )

    def insert_telegram_background_task(
        self,
        *,
        chat_id: str,
        message_id: int | None,
        user_id: str,
        command_id: int,
        command_text: str,
        source_id: str,
        pid: int,
        stdout_path: str,
        stderr_path: str,
        task_data: dict | None = None,
    ) -> int:
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO telegram_background_tasks
                (chat_id, message_id, user_id, command_id, command_text, source_id,
                 pid, stdout_path, stderr_path, task_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    chat_id, message_id, user_id, command_id, command_text, source_id,
                    pid, stdout_path, stderr_path,
                    json.dumps(task_data or {}, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def fetch_running_telegram_background_tasks(self) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT task_id, created_at, chat_id, message_id, user_id,
                       command_id, command_text, source_id, pid,
                       stdout_path, stderr_path, status, task_data
                FROM telegram_background_tasks
                WHERE status = 'running'
                ORDER BY task_id ASC;
                """
            ).fetchall()

    def complete_telegram_background_task(
        self,
        *,
        task_id: int,
        status: str,
        result_json: str | None = None,
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_background_tasks
                SET status = ?, result_json = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE task_id = ?;
                """,
                (status, result_json, task_id),
            )

    def insert_telegram_send_message(
        self,
        *,
        tlgchat_id: str,
        message_text: str,
        reply_message_id: int | None = None,
        entities: str | None = None,
        note: str = "",
        source_type: str | None = None,
        source_id: str | None = None,
        metadata: dict | None = None,
        message_type: str | None = None,
    ) -> int:
        """Queue one outgoing Telegram message.

        Prefer :func:`db_ops.db.telegram_queue.queue_telegram_message` over calling this
        directly — it is the one entry point every app shares, and it is what keeps
        ``message_type`` consistent instead of each app inventing its own vocabulary.
        """
        self.initialize()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO telegram_send_messages
                (
                    tlgchat_id,
                    message_text,
                    entities,
                    note,
                    host,
                    os_user,
                    ip_address,
                    reply_message_id,
                    source_type,
                    source_id,
                    message_type,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    tlgchat_id,
                    message_text,
                    entities,
                    note,
                    socket.gethostname(),
                    getpass.getuser(),
                    "",
                    reply_message_id,
                    source_type,
                    source_id,
                    (str(message_type).strip().lower() or None) if message_type else None,
                    metadata_json,
                ),
            )
            return int(cursor.lastrowid)

    def archive_old_job_runs(
        self,
        *,
        retention_days: int = 15,
        batch_size: int = 20000,
        max_batches: int | None = None,
    ) -> int:
        """Move ``job_runs`` rows older than ``retention_days`` into ``job_runs_history``.

        Rows are kept, not deleted — the same trade ``metric_results`` makes. Returns how many
        moved.

        Batched, because the first sweep on an unpruned store has months to clear (~1M rows /
        965 MB here) and one transaction that size holds locks on the table the daemon appends
        to on every single app-command start and finish. Each batch is its own transaction, so
        an interrupted sweep leaves a consistent store and the next pass carries on.

        ``max_batches`` caps one call to bounded work. The daemon needs that: it sweeps from the
        same loop that starts due app commands, so an uncapped first sweep would stall scheduling
        for as long as it took to move eight hundred thousand rows. Capped, the backlog drains
        over successive passes, and steady state (~13k rows/day age out here) clears inside the
        first batch every time.
        """
        self.initialize()
        days = max(int(retention_days), 0)
        if days <= 0:
            return 0
        # The cutoff is computed here and bound as a parameter. Inlining SQLite's three-argument
        # strftime('%Y-...','now','-N days') is what took down every metrics and SLA run seconds
        # after the store was switched to PostgreSQL, where that function does not exist; the
        # translator only rewrites the two-argument UTC-now form. A guard test enforces this.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        moved = 0
        batches = 0
        while True:
            if max_batches is not None and batches >= int(max_batches):
                return moved
            batches += 1
            archived_at = utc_now_text()
            with self.connect() as conn:
                ids = [
                    int(row["log_id"])
                    for row in conn.execute(
                        "SELECT log_id FROM job_runs WHERE created_at < ? ORDER BY log_id LIMIT ?",
                        (cutoff, int(batch_size)),
                    ).fetchall()
                ]
                if not ids:
                    return moved
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"INSERT INTO job_runs_history ({_JOB_RUN_ARCHIVE_COLUMNS}, archived_at) "
                    f"SELECT {_JOB_RUN_ARCHIVE_COLUMNS}, ? FROM job_runs "
                    f"WHERE log_id IN ({placeholders})",
                    (archived_at, *ids),
                )
                conn.execute(f"DELETE FROM job_runs WHERE log_id IN ({placeholders})", tuple(ids))
            moved += len(ids)
            if len(ids) < int(batch_size):
                return moved

    def insert_report(
        self,
        *,
        report_code: str,
        report_name: str,
        report_type: str,
        report_level: str,
        report_text: str,
        status: str = "created",
        source_type: str | None = None,
        source_id: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        self.initialize()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO reports
                (
                    report_code,
                    report_name,
                    report_type,
                    report_level,
                    status,
                    report_text,
                    source_type,
                    source_id,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    report_code,
                    report_name,
                    report_type,
                    report_level,
                    status,
                    report_text,
                    source_type,
                    source_id,
                    metadata_json,
                ),
            )
            return int(cursor.lastrowid)

    def fetch_reports_for_push(
        self,
        *,
        limit: int = 50,
        report_type: str | None = None,
        report_level: str | None = None,
        report_ids: list[int] | None = None,
        target_id: str | None = None,
    ) -> list[sqlite3.Row]:
        self.initialize()
        clauses = ["status = 'created'"]
        params: list[object] = []
        if report_ids is not None:
            if not report_ids:
                return []
            placeholders = ", ".join("?" for _ in report_ids)
            clauses.append(f"report_id IN ({placeholders})")
            params.extend(report_ids)
        if report_type:
            clauses.append("report_type = ?")
            params.append(report_type)
        if report_level:
            clauses.append("report_level = ?")
            params.append(report_level)
        if target_id:
            clauses.append("json_extract(metadata_json, '$.target_id') = ?")
            params.append(target_id)
        params.append(limit)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT
                        report_id,
                        report_code,
                        report_name,
                        report_type,
                        report_level,
                        status,
                        report_text,
                        source_type,
                        source_id,
                        created_at,
                        pushed_at,
                        telegram_send_message_id,
                        metadata_json
                    FROM reports
                    WHERE {" AND ".join(clauses)}
                    ORDER BY created_at ASC, report_id ASC
                    LIMIT ?;
                    """,
                    params,
                )
            )

    def mark_report_pushed(self, *, report_id: int, telegram_send_message_id: int) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reports
                SET
                    status = 'pushed',
                    pushed_at = ?,
                    telegram_send_message_id = ?
                WHERE report_id = ?;
                """,
                (utc_now_text(), telegram_send_message_id, report_id),
            )

    def mark_report_skipped(self, *, report_id: int, reason: str) -> None:
        self.initialize()
        metadata_json = json.dumps({"skip_reason": reason}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reports
                SET
                    status = 'skipped',
                    metadata_json = ?
                WHERE report_id = ?;
                """,
                (metadata_json, report_id),
            )

    def report_exists_on_local_date(self, *, report_code: str, local_date: str,
                                    utc_offset_hours: int = 7) -> bool:
        """Has this report already been produced on the given **local** calendar day?

        The daily guard behind a once-a-day report. Local, not UTC, because "today" for the
        operator reading it ends at local midnight — a report generated at 07:30 local is
        yesterday in UTC, and a UTC-day guard lets the same report out twice.

        Only ``created``/``pushed`` count: a row that failed to generate is not a report that
        happened, and treating it as one silences the retry.
        """
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM reports
                WHERE report_code = ?
                  AND substr(datetime(created_at, ?), 1, 10) = ?
                  AND status IN ('created', 'pushed')
                LIMIT 1;
                """,
                (report_code, f"+{int(utc_offset_hours)} hours", local_date),
            ).fetchone()
        return row is not None

    def recent_alert_exists(self, *, source_id: str, within_seconds: int,
                            source_types: tuple[str, ...] = ("metrics", "reports")) -> bool:
        """Was an alert for this source queued recently — the dedupe check before queueing another.

        Counts a message in any non-terminal or delivered state (``send_status`` 0/1/2): one still
        waiting in the queue is exactly as much a duplicate as one already sent, and skipping
        queued rows is how a stuck queue turns into a burst of identical messages when it drains.
        """
        self.initialize()
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=int(within_seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")
        placeholders = ",".join("?" for _ in source_types)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT 1
                FROM telegram_send_messages
                WHERE source_type IN ({placeholders})
                  AND source_id = ?
                  AND row_ins_date >= ?
                  AND send_status IN (0, 1, 2)
                LIMIT 1;
                """,
                (*source_types, source_id, cutoff),
            ).fetchone()
        return row is not None

    def fetch_report_send_state(self, *, report_code: str, channel: str) -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    report_code,
                    channel,
                    last_sent_at,
                    last_run_at,
                    last_status,
                    last_skipped_reason,
                    updated_at
                FROM report_send_state
                WHERE report_code = ?
                  AND channel = ?;
                """,
                (report_code, channel),
            ).fetchone()

    def upsert_report_send_state(
        self,
        *,
        report_code: str,
        channel: str,
        last_run_at: str,
        last_status: str,
        last_skipped_reason: str = "",
        last_sent_at: str | None = None,
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO report_send_state
                (
                    report_code,
                    channel,
                    last_sent_at,
                    last_run_at,
                    last_status,
                    last_skipped_reason,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_code, channel) DO UPDATE SET
                    last_sent_at = COALESCE(excluded.last_sent_at, report_send_state.last_sent_at),
                    last_run_at = excluded.last_run_at,
                    last_status = excluded.last_status,
                    last_skipped_reason = excluded.last_skipped_reason,
                    updated_at = excluded.updated_at;
                """,
                (
                    report_code,
                    channel,
                    last_sent_at,
                    last_run_at,
                    last_status,
                    last_skipped_reason,
                    utc_now_text(),
                ),
            )

    def fetch_latest_metric_report_results(
        self,
        *,
        db_type: str | None = None,
        target_id: str | None = None,
        target_ip: str | None = None,
        metric_code: str | None = None,
        metric_codes: set[str] | None = None,
        collected_at_gte: str | None = None,
        unreported_only: bool = False,
    ) -> list[sqlite3.Row]:
        self.initialize()
        clauses: list[str] = []
        params: list[object] = []
        if db_type:
            clauses.append("r.db_type = ?")
            params.append(db_type.lower())
        if target_id:
            clauses.append("r.target_id = ?")
            params.append(target_id)
        if target_ip:
            clauses.append("r.ip = ?")
            params.append(target_ip)
        if metric_code:
            clauses.append("r.metric_code = ?")
            params.append(metric_code)
        elif metric_codes is not None:
            if not metric_codes:
                return []
            placeholders = ", ".join("?" for _ in metric_codes)
            clauses.append(f"r.metric_code IN ({placeholders})")
            params.extend(sorted(metric_codes))
        if collected_at_gte:
            clauses.append("r.collected_at >= ?")
            params.append(collected_at_gte)
        if unreported_only:
            clauses.append("r.daily_report_created = 0")
        extra_where = " AND " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT r.result_id, r.run_id, r.collected_at, r.target_id, r.server_id, r.ip,
                           r.db_type, r.db_name, r.metric_code, r.metric_item, r.metric_value,
                           r.metric_unit, r.status, r.importance, r.message, r.daily_report_created,
                           r.collector_type, r.category, r.error_type, r.normalized_error_signature
                    FROM metric_results AS r
                    WHERE r.collected_at = (
                        SELECT MAX(inner_r.collected_at)
                        FROM metric_results AS inner_r
                        WHERE inner_r.target_id = r.target_id
                          AND inner_r.metric_code = r.metric_code
                    )
                    {extra_where}
                    ORDER BY r.metric_code, r.target_id, r.result_id;
                    """,
                    params,
                )
            )

    def fetch_metric_report_results(
        self,
        *,
        db_type: str | None = None,
        server_id: str | None = None,
        target_id: str | None = None,
        target_ip: str | None = None,
        metric_code: str | None = None,
        metric_codes: set[str] | None = None,
        collected_at_gte: str | None = None,
        collected_at_lte: str | None = None,
        unreported_only: bool = False,
    ) -> list[sqlite3.Row]:
        self.initialize()
        clauses: list[str] = []
        params: list[object] = []
        if db_type:
            clauses.append("r.db_type = ?")
            params.append(db_type.lower())
        if server_id:
            clauses.append("r.server_id = ?")
            params.append(server_id)
        if target_id:
            clauses.append("r.target_id = ?")
            params.append(target_id)
        if target_ip:
            clauses.append("r.ip = ?")
            params.append(target_ip)
        if metric_code:
            clauses.append("r.metric_code = ?")
            params.append(metric_code)
        elif metric_codes is not None:
            if not metric_codes:
                return []
            placeholders = ", ".join("?" for _ in metric_codes)
            clauses.append(f"r.metric_code IN ({placeholders})")
            params.extend(sorted(metric_codes))
        if collected_at_gte:
            clauses.append("r.collected_at >= ?")
            params.append(collected_at_gte)
        if collected_at_lte:
            clauses.append("r.collected_at <= ?")
            params.append(collected_at_lte)
        if unreported_only:
            clauses.append("r.daily_report_created = 0")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT r.result_id, r.run_id, r.collected_at, r.target_id, r.server_id, r.ip,
                           r.db_type, r.db_name, r.metric_code, r.metric_item, r.metric_value,
                           r.metric_unit, r.status, r.importance, r.message, r.daily_report_created,
                           r.collector_type, r.category, r.error_type, r.normalized_error_signature
                    FROM metric_results AS r
                    {where}
                    ORDER BY r.collected_at, r.metric_code, r.target_id, r.result_id;
                    """,
                    params,
                )
            )

    def mark_metric_daily_report_created_for_scope(
        self,
        *,
        metric_codes: set[str],
        target_ids: set[str],
        collected_at_lte: str,
    ) -> int:
        self.initialize()
        clean_metric_codes = sorted({str(metric_code).strip() for metric_code in metric_codes if str(metric_code).strip()})
        clean_target_ids = sorted({str(target_id).strip() for target_id in target_ids if str(target_id).strip()})
        cutoff = str(collected_at_lte or "").strip()
        if not clean_metric_codes or not clean_target_ids or not cutoff:
            return 0

        metric_placeholders = ", ".join("?" for _ in clean_metric_codes)
        target_placeholders = ", ".join("?" for _ in clean_target_ids)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE metric_results
                SET daily_report_created = 1
                WHERE daily_report_created = 0
                  AND metric_code IN ({metric_placeholders})
                  AND target_id IN ({target_placeholders})
                  AND collected_at <= ?;
                """,
                [*clean_metric_codes, *clean_target_ids, cutoff],
            )
            return int(cursor.rowcount)

    # `mark_metric_daily_report_created` was deleted on 2026-08-16: it was byte-identical to
    # `MetricStore.mark_daily_report_created` and **nothing called it**. Two methods writing
    # `metric_results.daily_report_created` is a rule with two versions, and the second one is
    # found by whoever is debugging why the first did not apply. The scoped variant next to it
    # is the live one; the row-id form lives on `MetricStore`, which owns that table.

    def fetch_latest_metric_run_meta(self) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT run_id, started_at, finished_at, status, target_count, metric_count,
                       result_count, error_count, warning_count, critical_count, message
                FROM metric_runs
                ORDER BY run_id DESC
                LIMIT 1;
                """
            ).fetchone()
        return dict(row) if row else None

    def fetch_pending_telegram_send_messages(self, limit: int = 50) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        send_tlgmsg_id,
                        row_ins_date,
                        tlgchat_id,
                        list_tlguser_id,
                        message_text,
                        entities,
                        note,
                        host,
                        os_user,
                        ip_address,
                        send_status,
                        send_date,
                        message_id,
                        reply_message_id,
                        source_type,
                        source_id,
                        metadata_json,
                        message_type
                    FROM telegram_send_messages
                    WHERE send_status = 0
                    ORDER BY row_ins_date ASC, send_tlgmsg_id ASC
                    LIMIT ?;
                    """,
                    (limit,),
                )
            )

    def fetch_telegram_send_message(self, *, send_tlgmsg_id: int) -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    send_tlgmsg_id,
                    row_ins_date,
                    tlgchat_id,
                    list_tlguser_id,
                    message_text,
                    entities,
                    note,
                    host,
                    os_user,
                    ip_address,
                    send_status,
                    send_date,
                    message_id,
                    reply_message_id,
                    source_type,
                    source_id,
                    metadata_json,
                    -- The send layer decides the severity emoji from this. Leaving it out of the
                    -- SELECT made every row look like it declared nothing, so the emoji fell back
                    -- to guessing from the header — and a conversation prompt that merely
                    -- contains the word "timeout" went out tagged as a failure.
                    message_type
                FROM telegram_send_messages
                WHERE send_tlgmsg_id = ?;
                """,
                (send_tlgmsg_id,),
            ).fetchone()

    def mark_telegram_send_message_processing(self, *, send_tlgmsg_id: int) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_send_messages
                SET send_status = 2
                WHERE send_tlgmsg_id = ?
                  AND send_status = 0;
                """,
                (send_tlgmsg_id,),
            )

    def mark_telegram_send_message_sent(self, *, send_tlgmsg_id: int, message_id: int | None) -> None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT source_type, source_id
                FROM telegram_send_messages
                WHERE send_tlgmsg_id = ?;
                """,
                (send_tlgmsg_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE telegram_send_messages
                SET
                    send_status = 1,
                    send_date = ?,
                    message_id = ?
                WHERE send_tlgmsg_id = ?;
                """,
                (utc_now_text(), message_id, send_tlgmsg_id),
            )
            if row and row["source_type"] == "telegram_command_messages" and row["source_id"] and message_id:
                conn.execute(
                    """
                    UPDATE telegram_command_messages
                    SET reply_message_id = ?
                    WHERE telegram_command_message_id = ?;
                    """,
                    (message_id, int(row["source_id"])),
                )

    def mark_telegram_send_message_failed(self, *, send_tlgmsg_id: int, fail_text: str) -> None:
        self.initialize()
        metadata_json = json.dumps({"fail_text": fail_text}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_send_messages
                SET
                    send_status = -1,
                    send_date = ?,
                    metadata_json = ?
                WHERE send_tlgmsg_id = ?;
                """,
                (utc_now_text(), metadata_json, send_tlgmsg_id),
            )

    def reset_telegram_send_message_pending(self, *, send_tlgmsg_id: int, fail_text: str) -> None:
        self.initialize()
        metadata_json = json.dumps({"last_fail_text": fail_text}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_send_messages
                SET
                    send_status = 0,
                    metadata_json = ?
                WHERE send_tlgmsg_id = ?;
                """,
                (metadata_json, send_tlgmsg_id),
            )

    def insert_sql_run(
        self,
        *,
        run_key: str,
        sql_id: int,
        sql_code: str,
        target_no: int,
        server_id: str,
        db_type: str,
        service_name: str,
        instance_name: str,
        database_name: str | None,
        credential_name: str,
        status: str,
        level: str,
        message: str,
        started_at: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        self.initialize()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO sql_runs
                (
                    run_key,
                    sql_id,
                    sql_code,
                    target_no,
                    server_id,
                    db_type,
                    service_name,
                    instance_name,
                    database_name,
                    credential_name,
                    status,
                    level,
                    message,
                    started_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    run_key,
                    sql_id,
                    sql_code,
                    target_no,
                    server_id,
                    db_type,
                    service_name,
                    instance_name,
                    database_name,
                    credential_name,
                    status,
                    level,
                    message,
                    started_at,
                    metadata_json,
                ),
            )
            return int(cursor.lastrowid)

    def update_sql_run(
        self,
        *,
        sql_run_id: int,
        status: str,
        level: str,
        message: str,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        row_count: int | None = None,
        result: dict | list | None = None,
        error_text: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.initialize()
        result_json = json.dumps(result if result is not None else {}, ensure_ascii=False, sort_keys=True)
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sql_runs
                SET
                    status = ?,
                    level = ?,
                    message = ?,
                    finished_at = ?,
                    duration_ms = ?,
                    row_count = ?,
                    result_json = ?,
                    error_text = ?,
                    metadata_json = ?
                WHERE sql_run_id = ?;
                """,
                (
                    status,
                    level,
                    message,
                    finished_at,
                    duration_ms,
                    row_count,
                    result_json,
                    error_text,
                    metadata_json,
                    sql_run_id,
                ),
            )

    def fetch_latest_done_or_running_sql_runs_by_run_key(self) -> dict[str, sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    sql_run_id,
                    run_key,
                    sql_id,
                    sql_code,
                    target_no,
                    server_id,
                    db_type,
                    service_name,
                    instance_name,
                    database_name,
                    credential_name,
                    status,
                    level,
                    message,
                    started_at,
                    finished_at,
                    duration_ms,
                    row_count,
                    error_text,
                    metadata_json
                FROM sql_runs
                WHERE status IN ('done', 'running')
                  AND sql_run_id IN (
                    SELECT max(sql_run_id)
                    FROM sql_runs
                    WHERE status IN ('done', 'running')
                    GROUP BY run_key
                );
                """
            ).fetchall()
        return {str(row["run_key"]): row for row in rows}

    def fetch_latest_sql_runs_by_run_key(self) -> dict[str, sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    sql_run_id,
                    run_key,
                    sql_id,
                    sql_code,
                    target_no,
                    server_id,
                    db_type,
                    service_name,
                    instance_name,
                    database_name,
                    credential_name,
                    status,
                    level,
                    message,
                    started_at,
                    finished_at,
                    duration_ms,
                    row_count,
                    error_text,
                    metadata_json
                FROM sql_runs
                WHERE sql_run_id IN (
                    SELECT max(sql_run_id)
                    FROM sql_runs
                    GROUP BY run_key
                );
                """
            ).fetchall()
        return {str(row["run_key"]): row for row in rows}

    def fetch_latest_sql_run_for_sql_id(self, *, sql_id: int, status: str = "done") -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    sql_run_id,
                    run_key,
                    sql_id,
                    sql_code,
                    target_no,
                    server_id,
                    db_type,
                    service_name,
                    instance_name,
                    database_name,
                    credential_name,
                    status,
                    level,
                    message,
                    started_at,
                    finished_at,
                    duration_ms,
                    row_count,
                    result_json,
                    error_text,
                    metadata_json
                FROM sql_runs
                WHERE sql_id = ?
                  AND status = ?
                ORDER BY sql_run_id DESC
                LIMIT 1;
                """,
                (sql_id, status),
            ).fetchone()


    def fetch_recent_sql_runs(self, *, limit: int = 10, sql_id: int | None = None) -> list:
        """The most recent SQL task runs, newest first — the history, not the schedule.

        `fetch_latest_*_by_run_key` answer "where does each task stand"; this answers "what has
        been happening", which is the question someone asks after an alert. Ordered by
        ``sql_run_id`` rather than ``started_at`` because two runs can start in the same second
        and the id is the only total order the table has.
        """
        self.initialize()
        sql = """
            SELECT sql_run_id, sql_id, sql_code, target_no, server_id, service_name,
                   status, level, message, started_at, finished_at, duration_ms, row_count,
                   error_text
            FROM sql_runs
        """
        params: list = []
        if sql_id is not None:
            sql += " WHERE sql_id = ?"
            params.append(int(sql_id))
        sql += " ORDER BY sql_run_id DESC LIMIT ?;"
        params.append(int(limit))
        with self.connect() as conn:
            return list(conn.execute(sql, tuple(params)).fetchall())


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta
(
    schema_name TEXT NOT NULL PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS job_runs
(
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    started_at TEXT NULL,
    finished_at TEXT NULL,
    job_code TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('logging', 'warning', 'error', 'critical')),
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    duration_ms INTEGER NULL CHECK (duration_ms IS NULL OR duration_ms >= 0),
    error_text TEXT NULL,
    host_name TEXT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK (json_valid(metadata_json))
);

CREATE INDEX IF NOT EXISTS ix_job_runs_created_at
    ON job_runs (created_at DESC);

CREATE INDEX IF NOT EXISTS ix_job_runs_job_code_created_at
    ON job_runs (job_code, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_job_runs_level_created_at
    ON job_runs (level, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_job_runs_status_created_at
    ON job_runs (status, created_at DESC);

-- Rows aged out of job_runs. Same shape plus archived_at, and deliberately *without* job_runs'
-- indexes: this table is written by the archive sweep and read by hand, so the indexes would
-- cost write time on every sweep and earn nothing. (metric_results_archive is the same trade:
-- 1.1 GB, 27 index scans in its lifetime.)
--
-- job_runs is the busiest table in the store - the daemon appends to it on every app-command
-- start and finish - and nothing pruned it, so it reached ~1M rows / 965 MB with the oldest row
-- 2.5 months back. Rows are moved, never deleted: an incident review needs the run history that
-- is by then well past any live retention window.
CREATE TABLE IF NOT EXISTS job_runs_history
(
    log_id INTEGER,
    created_at TEXT,
    started_at TEXT NULL,
    finished_at TEXT NULL,
    job_code TEXT,
    level TEXT,
    status TEXT,
    message TEXT,
    duration_ms INTEGER NULL,
    error_text TEXT NULL,
    host_name TEXT NULL,
    metadata_json TEXT,
    archived_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_job_runs_history_created_at
    ON job_runs_history (created_at DESC);

CREATE TABLE IF NOT EXISTS sql_runs
(
    sql_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    run_key TEXT NOT NULL,
    sql_id INTEGER NOT NULL,
    sql_code TEXT NOT NULL,
    target_no INTEGER NOT NULL,
    server_id TEXT NOT NULL,
    db_type TEXT NOT NULL,
    service_name TEXT NOT NULL,
    instance_name TEXT NOT NULL DEFAULT '',
    database_name TEXT NULL,
    credential_name TEXT NOT NULL,
    status TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('logging', 'warning', 'error', 'critical')),
    message TEXT NOT NULL,
    started_at TEXT NULL,
    finished_at TEXT NULL,
    duration_ms INTEGER NULL CHECK (duration_ms IS NULL OR duration_ms >= 0),
    row_count INTEGER NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK (json_valid(result_json)),
    CHECK (json_valid(metadata_json))
);

CREATE INDEX IF NOT EXISTS ix_sql_runs_run_key_created_at
    ON sql_runs (run_key, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_sql_runs_sql_code_created_at
    ON sql_runs (sql_code, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_sql_runs_status_created_at
    ON sql_runs (status, created_at DESC);

CREATE TABLE IF NOT EXISTS telegram_messages
(
    telegram_message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER NULL,
    message_id INTEGER NOT NULL,
    message_date INTEGER NULL,
    chat_id TEXT NOT NULL,
    chat_type TEXT NULL,
    user_id TEXT NULL,
    text TEXT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (json_valid(raw_json)),
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS ix_telegram_messages_update_id
    ON telegram_messages (update_id);

CREATE INDEX IF NOT EXISTS ix_telegram_messages_chat_date
    ON telegram_messages (chat_id, message_date DESC);

CREATE INDEX IF NOT EXISTS ix_telegram_messages_user_date
    ON telegram_messages (user_id, message_date DESC);

CREATE TABLE IF NOT EXISTS telegram_command_messages
(
    telegram_command_message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id INTEGER NULL,
    update_id INTEGER NULL,
    message_id INTEGER NOT NULL,
    message_date INTEGER NULL,
    chat_id TEXT NOT NULL,
    chat_type TEXT NULL,
    user_id TEXT NULL,
    text TEXT NULL,
    command_prefix TEXT NOT NULL,
    command_payload TEXT NULL,
    command_id INTEGER NULL,
    command_status INTEGER NOT NULL DEFAULT 0 CHECK (command_status IN (-2, -1, 0, 1)),
    reply_message_id INTEGER NULL,
    processed_at TEXT NULL,
    process_note TEXT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (json_valid(raw_json)),
    FOREIGN KEY (telegram_message_id) REFERENCES telegram_messages (telegram_message_id),
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_update_id
    ON telegram_command_messages (update_id);

CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_chat_date
    ON telegram_command_messages (chat_id, message_date DESC);

CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_user_date
    ON telegram_command_messages (user_id, message_date DESC);

CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_prefix_date
    ON telegram_command_messages (command_prefix, message_date DESC);

CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_status_date
    ON telegram_command_messages (command_status, message_date ASC);

CREATE TABLE IF NOT EXISTS telegram_conversation_states
(
    state_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    command_id INTEGER NOT NULL,
    command_text TEXT NOT NULL,
    state_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    wait_after_message_id INTEGER NOT NULL,
    source_telegram_command_message_id INTEGER NULL,
    consumed_telegram_message_id INTEGER NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NULL,
    CHECK (json_valid(state_json))
);

CREATE INDEX IF NOT EXISTS ix_telegram_conversation_states_status_created
    ON telegram_conversation_states (status, created_at ASC);

CREATE INDEX IF NOT EXISTS ix_telegram_conversation_states_chat_user_status
    ON telegram_conversation_states (chat_id, user_id, status);

CREATE TABLE IF NOT EXISTS telegram_send_messages
(
    send_tlgmsg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_ins_date TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    tlgchat_id TEXT NULL,
    list_tlguser_id TEXT NULL,
    message_text TEXT NOT NULL,
    entities TEXT NULL,
    note TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    os_user TEXT NOT NULL DEFAULT '',
    ip_address TEXT NOT NULL DEFAULT '',
    send_status INTEGER NOT NULL DEFAULT 0 CHECK (send_status IN (-1, 0, 1, 2)),
    send_date TEXT NULL,
    message_id INTEGER NULL,
    reply_message_id INTEGER NULL,
    source_type TEXT NULL,
    source_id TEXT NULL,
    -- What the producer says this message IS: started/success/failed/warning/running/critical,
    -- or 'plain' for a message that carries no status (a command reply, a listing). NULL means
    -- the producer did not say, and the send layer falls back to reading the header. See
    -- db_ops.telegram.severity.
    message_type TEXT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK (json_valid(metadata_json))
);

CREATE INDEX IF NOT EXISTS ix_telegram_send_messages_status_created
    ON telegram_send_messages (send_status, row_ins_date ASC);

CREATE INDEX IF NOT EXISTS ix_telegram_send_messages_chat_created
    ON telegram_send_messages (tlgchat_id, row_ins_date DESC);

CREATE TABLE IF NOT EXISTS telegram_background_tasks
(
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    chat_id TEXT NOT NULL,
    message_id INTEGER NULL,
    user_id TEXT NOT NULL,
    command_id INTEGER NOT NULL,
    command_text TEXT NOT NULL,
    source_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    stdout_path TEXT NOT NULL,
    stderr_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    result_json TEXT NULL,
    task_data TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_telegram_background_tasks_status
    ON telegram_background_tasks (status, created_at ASC);

CREATE TABLE IF NOT EXISTS report_types
(
    report_type TEXT NOT NULL PRIMARY KEY,
    report_type_name TEXT NOT NULL,
    report_level TEXT NOT NULL CHECK (report_level IN ('logging', 'warning', 'critical')),
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NULL
);

CREATE INDEX IF NOT EXISTS ix_report_types_level_active
    ON report_types (report_level, active);

CREATE TABLE IF NOT EXISTS reports
(
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_code TEXT NOT NULL,
    report_name TEXT NOT NULL,
    report_type TEXT NOT NULL,
    report_level TEXT NOT NULL CHECK (report_level IN ('logging', 'warning', 'critical')),
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'pushed', 'skipped', 'failed')),
    report_text TEXT NOT NULL,
    source_type TEXT NULL,
    source_id TEXT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    pushed_at TEXT NULL,
    telegram_send_message_id INTEGER NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK (json_valid(metadata_json)),
    FOREIGN KEY (report_type) REFERENCES report_types (report_type)
);

CREATE INDEX IF NOT EXISTS ix_reports_status_created
    ON reports (status, created_at ASC);

CREATE INDEX IF NOT EXISTS ix_reports_type_level_created
    ON reports (report_type, report_level, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_reports_code_created
    ON reports (report_code, created_at DESC);
"""


def ensure_sqlite_column(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name});")}
    if column_name not in columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql};")


def ensure_metric_results_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metric_runs
        (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            target_count INTEGER,
            metric_count INTEGER,
            result_count INTEGER,
            error_count INTEGER,
            warning_count INTEGER,
            critical_count INTEGER,
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS metric_results
        (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            target_id TEXT,
            server_id TEXT,
            ip TEXT,
            db_type TEXT,
            db_name TEXT,
            metric_code TEXT NOT NULL,
            metric_item TEXT,
            metric_value TEXT,
            metric_unit TEXT,
            status TEXT,
            importance INTEGER,
            message TEXT,
            collected_at TEXT NOT NULL,
            daily_report_created INTEGER NOT NULL DEFAULT 0 CHECK (daily_report_created IN (0, 1)),
            collector_type TEXT,
            category TEXT,
            error_type TEXT,
            normalized_error_signature TEXT
        );

        CREATE INDEX IF NOT EXISTS ix_metric_results_collected_at ON metric_results(collected_at);
        CREATE INDEX IF NOT EXISTS ix_metric_results_target_id ON metric_results(target_id);
        CREATE INDEX IF NOT EXISTS ix_metric_results_metric_code ON metric_results(metric_code);
        CREATE INDEX IF NOT EXISTS ix_metric_results_server_metric_time
            ON metric_results(server_id, metric_code, collected_at);
        CREATE INDEX IF NOT EXISTS ix_metric_results_status ON metric_results(status);
        CREATE INDEX IF NOT EXISTS ix_metric_results_importance ON metric_results(importance);
        CREATE INDEX IF NOT EXISTS ix_metric_results_daily_report_created ON metric_results(daily_report_created);
        """
    )
    ensure_sqlite_column(
        conn,
        table_name="metric_results",
        column_name="daily_report_created",
        column_sql="daily_report_created INTEGER NOT NULL DEFAULT 0 CHECK (daily_report_created IN (0, 1))",
    )
    ensure_sqlite_column(conn, table_name="metric_results", column_name="collector_type", column_sql="collector_type TEXT")
    ensure_sqlite_column(conn, table_name="metric_results", column_name="category", column_sql="category TEXT")
    ensure_sqlite_column(conn, table_name="metric_results", column_name="error_type", column_sql="error_type TEXT")
    ensure_sqlite_column(
        conn,
        table_name="metric_results",
        column_name="normalized_error_signature",
        column_sql="normalized_error_signature TEXT",
    )


def migrate_reports_table(conn: sqlite3.Connection) -> None:
    seed_report_types(conn)


def migrate_report_send_state_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS report_send_state
        (
            report_code TEXT NOT NULL,
            channel TEXT NOT NULL,
            last_sent_at TEXT NULL,
            last_run_at TEXT NULL,
            last_status TEXT NOT NULL DEFAULT '',
            last_skipped_reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            PRIMARY KEY (report_code, channel)
        );

        CREATE INDEX IF NOT EXISTS ix_report_send_state_updated
            ON report_send_state (updated_at DESC);
        """
    )
    rebuild_reports_table_if_needed(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS report_types
        (
            report_type TEXT NOT NULL PRIMARY KEY,
            report_type_name TEXT NOT NULL,
            report_level TEXT NOT NULL CHECK (report_level IN ('logging', 'warning', 'critical')),
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NULL
        );

        CREATE TABLE IF NOT EXISTS reports
        (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_code TEXT NOT NULL,
            report_name TEXT NOT NULL,
            report_type TEXT NOT NULL,
            report_level TEXT NOT NULL CHECK (report_level IN ('logging', 'warning', 'critical')),
            status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'pushed', 'skipped', 'failed')),
            report_text TEXT NOT NULL,
            source_type TEXT NULL,
            source_id TEXT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            pushed_at TEXT NULL,
            telegram_send_message_id INTEGER NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            CHECK (json_valid(metadata_json)),
            FOREIGN KEY (report_type) REFERENCES report_types (report_type)
        );

        CREATE INDEX IF NOT EXISTS ix_report_types_level_active
            ON report_types (report_level, active);

        CREATE INDEX IF NOT EXISTS ix_reports_status_created
            ON reports (status, created_at ASC);

        CREATE INDEX IF NOT EXISTS ix_reports_type_level_created
            ON reports (report_type, report_level, created_at DESC);

        CREATE INDEX IF NOT EXISTS ix_reports_code_created
            ON reports (report_code, created_at DESC);
        """
    )
    seed_report_types(conn)


def seed_report_types(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS report_types
        (
            report_type TEXT NOT NULL PRIMARY KEY,
            report_type_name TEXT NOT NULL,
            report_level TEXT NOT NULL CHECK (report_level IN ('logging', 'warning', 'critical')),
            description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NULL
        );
        """
    )
    conn.executemany(
        """
        INSERT INTO report_types
        (
            report_type,
            report_type_name,
            report_level,
            description,
            active
        )
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(report_type) DO UPDATE SET
            report_type_name = excluded.report_type_name,
            report_level = excluded.report_level,
            description = excluded.description,
            active = excluded.active,
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now');
        """,
        [
            ("instancely_logging", "Instance logging metrics report", "logging", "Instance-level normal metrics summary."),
            ("instancely_warning", "Instance warning metrics report", "warning", "Instance-level warning metrics report."),
            ("instancely_critical", "Instance critical metrics report", "critical", "Instance-level critical/error metrics report."),
            ("BACKUP_HEALTH", "Backup health report", "logging", "Daily SQL Server backup health report from SQLite metric history."),
            ("metric_history", "Metric history report", "logging", "On-demand history for one stored metric and server."),
            # Its own type because a per-index listing cannot share a report with alerts: one
            # server can carry tens of thousands of indexes, and the rows are maintenance work
            # rather than incidents. Level 'logging' by default; the report itself is raised to
            # 'critical' for a server with a disabled CLUSTERED index, which makes a table
            # unreadable.
            ("index_usage", "Index usage report", "logging", "Per-server index inventory: disabled indexes, drop candidates and fragmentation, each with a recommended action."),
        ],
    )


def rebuild_reports_table_if_needed(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(reports);")}
    if not columns:
        return
    fk_rows = conn.execute("PRAGMA foreign_key_list(reports);").fetchall()
    has_report_type_fk = any(row["table"] == "report_types" and row["from"] == "report_type" for row in fk_rows)
    if has_report_type_fk:
        return

    conn.executescript(
        """
        DROP INDEX IF EXISTS ix_reports_status_created;
        DROP INDEX IF EXISTS ix_reports_type_level_created;
        DROP INDEX IF EXISTS ix_reports_code_created;

        -- An interrupted rebuild leaves this table behind while the original is still in
        -- place, and the next startup then fails forever with
        -- 'relation "reports_new" already exists' -- which is exactly how the worker's daemon
        -- ended up in a crash loop on the first PostgreSQL cutover. The copy and the rename are two
        -- separate transactions, so that gap is reachable. Dropping any leftover first makes the
        -- rebuild resumable instead of fatal; nothing of value is in it, because the rename that
        -- would have made it the real table never happened.
        DROP TABLE IF EXISTS reports_new;

        CREATE TABLE reports_new
        (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_code TEXT NOT NULL,
            report_name TEXT NOT NULL,
            report_type TEXT NOT NULL,
            report_level TEXT NOT NULL CHECK (report_level IN ('logging', 'warning', 'critical')),
            status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'pushed', 'skipped', 'failed')),
            report_text TEXT NOT NULL,
            source_type TEXT NULL,
            source_id TEXT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            pushed_at TEXT NULL,
            telegram_send_message_id INTEGER NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            CHECK (json_valid(metadata_json)),
            FOREIGN KEY (report_type) REFERENCES report_types (report_type)
        );
        """
    )

    insert_columns = [
        "report_id",
        "report_code",
        "report_name",
        "report_type",
        "report_level",
        "status",
        "report_text",
        "source_type",
        "source_id",
        "created_at",
        "pushed_at",
        "telegram_send_message_id",
        "metadata_json",
    ]
    select_expressions = [
        source_expr(columns, "report_id"),
        source_expr(columns, "report_code", default="''"),
        source_expr(columns, "report_name", default="''"),
        source_expr(columns, "report_type", default="'instancely_logging'"),
        source_expr(columns, "report_level", default="'logging'"),
        source_expr(columns, "status", default="'created'"),
        source_expr(columns, "report_text", default="''"),
        source_expr(columns, "source_type", default="NULL"),
        source_expr(columns, "source_id", default="NULL"),
        source_expr(columns, "created_at", default="strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"),
        source_expr(columns, "pushed_at", default="NULL"),
        source_expr(columns, "telegram_send_message_id", default="NULL"),
        source_expr(columns, "metadata_json", default="'{}'"),
    ]
    conn.execute(
        f"""
        INSERT INTO reports_new ({", ".join(insert_columns)})
        SELECT {", ".join(select_expressions)}
        FROM reports;
        """
    )
    conn.executescript(
        """
        DROP TABLE reports;
        ALTER TABLE reports_new RENAME TO reports;

        CREATE INDEX IF NOT EXISTS ix_reports_status_created
            ON reports (status, created_at ASC);

        CREATE INDEX IF NOT EXISTS ix_reports_type_level_created
            ON reports (report_type, report_level, created_at DESC);

        CREATE INDEX IF NOT EXISTS ix_reports_code_created
            ON reports (report_code, created_at DESC);
        """
    )


def migrate_telegram_send_messages_table(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(telegram_send_messages);")}
    if {"send_tlgmsg_id", "row_ins_date", "tlgchat_id", "message_text"}.issubset(columns):
        return

    conn.executescript(
        """
        DROP INDEX IF EXISTS ix_telegram_send_messages_status_created;
        DROP INDEX IF EXISTS ix_telegram_send_messages_chat_created;
        DROP INDEX IF EXISTS ix_telegram_send_messages_message_id;

        -- An interrupted rebuild leaves this table behind while the original is still in
        -- place, and the next startup then fails forever with
        -- 'relation "telegram_send_messages_new" already exists' -- which is exactly how the worker's daemon
        -- ended up in a crash loop on the first PostgreSQL cutover. The copy and the rename are two
        -- separate transactions, so that gap is reachable. Dropping any leftover first makes the
        -- rebuild resumable instead of fatal; nothing of value is in it, because the rename that
        -- would have made it the real table never happened.
        DROP TABLE IF EXISTS telegram_send_messages_new;

        CREATE TABLE telegram_send_messages_new
        (
            send_tlgmsg_id INTEGER PRIMARY KEY AUTOINCREMENT,
            row_ins_date TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            tlgchat_id TEXT NULL,
            list_tlguser_id TEXT NULL,
            message_text TEXT NOT NULL,
            entities TEXT NULL,
            note TEXT NOT NULL DEFAULT '',
            host TEXT NOT NULL DEFAULT '',
            os_user TEXT NOT NULL DEFAULT '',
            ip_address TEXT NOT NULL DEFAULT '',
            send_status INTEGER NOT NULL DEFAULT 0 CHECK (send_status IN (-1, 0, 1, 2)),
            send_date TEXT NULL,
            message_id INTEGER NULL,
            reply_message_id INTEGER NULL,
            source_type TEXT NULL,
            source_id TEXT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            CHECK (json_valid(metadata_json))
        );
        """
    )

    insert_columns = [
        "send_tlgmsg_id",
        "row_ins_date",
        "tlgchat_id",
        "list_tlguser_id",
        "message_text",
        "entities",
        "note",
        "host",
        "os_user",
        "ip_address",
        "send_status",
        "send_date",
        "message_id",
        "reply_message_id",
        "source_type",
        "source_id",
        "metadata_json",
    ]
    select_expressions = [
        source_expr(columns, "send_tlgmsg_id", "telegram_send_message_id"),
        source_expr(columns, "row_ins_date", "created_at", "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"),
        source_expr(columns, "tlgchat_id", "chat_id", "NULL"),
        source_expr(columns, "list_tlguser_id", default="NULL"),
        source_expr(columns, "message_text", "text", "''"),
        source_expr(columns, "entities", default="NULL"),
        source_expr(columns, "note", default="''"),
        source_expr(columns, "host", default="''"),
        source_expr(columns, "os_user", default="''"),
        source_expr(columns, "ip_address", default="''"),
        source_expr(columns, "send_status", default="0"),
        source_expr(columns, "send_date", "sent_at", "fail_at", "NULL"),
        source_expr(columns, "message_id", default="NULL"),
        source_expr(columns, "reply_message_id", "reply_to_message_id", "NULL"),
        source_expr(columns, "source_type", default="NULL"),
        source_expr(columns, "source_id", default="NULL"),
        source_expr(columns, "metadata_json", default="'{}'"),
    ]
    conn.execute(
        f"""
        INSERT INTO telegram_send_messages_new ({", ".join(insert_columns)})
        SELECT {", ".join(select_expressions)}
        FROM telegram_send_messages;
        """
    )
    conn.executescript(
        """
        DROP TABLE telegram_send_messages;
        ALTER TABLE telegram_send_messages_new RENAME TO telegram_send_messages;

        CREATE INDEX IF NOT EXISTS ix_telegram_send_messages_status_created
            ON telegram_send_messages (send_status, row_ins_date ASC);

        CREATE INDEX IF NOT EXISTS ix_telegram_send_messages_chat_created
            ON telegram_send_messages (tlgchat_id, row_ins_date DESC);
        """
    )


def migrate_telegram_command_messages_table(conn: sqlite3.Connection) -> None:
    fk_rows = conn.execute("PRAGMA foreign_key_list(telegram_command_messages);").fetchall()
    has_message_fk = any(row["table"] == "telegram_messages" for row in fk_rows)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(telegram_command_messages);")}
    required_columns = {
        "telegram_command_message_id",
        "telegram_message_id",
        "update_id",
        "message_id",
        "message_date",
        "chat_id",
        "chat_type",
        "user_id",
        "text",
        "command_prefix",
        "command_payload",
        "command_id",
        "command_status",
        "reply_message_id",
        "processed_at",
        "process_note",
        "raw_json",
        "created_at",
    }
    if has_message_fk and required_columns.issubset(columns):
        return

    conn.executescript(
        """
        DROP INDEX IF EXISTS ix_telegram_command_messages_update_id;
        DROP INDEX IF EXISTS ix_telegram_command_messages_chat_date;
        DROP INDEX IF EXISTS ix_telegram_command_messages_user_date;
        DROP INDEX IF EXISTS ix_telegram_command_messages_prefix_date;
        DROP INDEX IF EXISTS ix_telegram_command_messages_status_date;
        DROP INDEX IF EXISTS ix_telegram_command_messages_command_id;

        -- An interrupted rebuild leaves this table behind while the original is still in
        -- place, and the next startup then fails forever with
        -- 'relation "telegram_command_messages_new" already exists' -- which is exactly how the worker's daemon
        -- ended up in a crash loop on the first PostgreSQL cutover. The copy and the rename are two
        -- separate transactions, so that gap is reachable. Dropping any leftover first makes the
        -- rebuild resumable instead of fatal; nothing of value is in it, because the rename that
        -- would have made it the real table never happened.
        DROP TABLE IF EXISTS telegram_command_messages_new;

        CREATE TABLE telegram_command_messages_new
        (
            telegram_command_message_id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_message_id INTEGER NULL,
            update_id INTEGER NULL,
            message_id INTEGER NOT NULL,
            message_date INTEGER NULL,
            chat_id TEXT NOT NULL,
            chat_type TEXT NULL,
            user_id TEXT NULL,
            text TEXT NULL,
            command_prefix TEXT NOT NULL,
            command_payload TEXT NULL,
            command_id INTEGER NULL,
            command_status INTEGER NOT NULL DEFAULT 0 CHECK (command_status IN (-2, -1, 0, 1)),
            reply_message_id INTEGER NULL,
            processed_at TEXT NULL,
            process_note TEXT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            CHECK (json_valid(raw_json)),
            FOREIGN KEY (telegram_message_id) REFERENCES telegram_messages (telegram_message_id),
            UNIQUE (chat_id, message_id)
        );
        """
    )
    insert_columns = [
        "telegram_command_message_id",
        "telegram_message_id",
        "update_id",
        "message_id",
        "message_date",
        "chat_id",
        "chat_type",
        "user_id",
        "text",
        "command_prefix",
        "command_payload",
        "command_id",
        "command_status",
        "reply_message_id",
        "processed_at",
        "process_note",
        "raw_json",
        "created_at",
    ]
    select_expressions = [
        source_expr(columns, "telegram_command_message_id"),
        source_expr(columns, "telegram_message_id"),
        source_expr(columns, "update_id"),
        source_expr(columns, "message_id", default="0"),
        source_expr(columns, "message_date"),
        source_expr(columns, "chat_id", default="''"),
        source_expr(columns, "chat_type"),
        source_expr(columns, "user_id"),
        source_expr(columns, "text"),
        source_expr(columns, "command_prefix", default="''"),
        source_expr(columns, "command_payload"),
        source_expr(columns, "command_id"),
        source_expr(columns, "command_status", default="0"),
        source_expr(columns, "reply_message_id"),
        source_expr(columns, "processed_at"),
        source_expr(columns, "process_note"),
        source_expr(columns, "raw_json", default="'{}'"),
        source_expr(columns, "created_at", default="strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"),
    ]
    conn.execute(
        f"""
        INSERT INTO telegram_command_messages_new ({", ".join(insert_columns)})
        SELECT {", ".join(select_expressions)}
        FROM telegram_command_messages;
        """
    )
    conn.executescript(
        """
        DROP TABLE telegram_command_messages;
        ALTER TABLE telegram_command_messages_new RENAME TO telegram_command_messages;

        CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_update_id
            ON telegram_command_messages (update_id);

        CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_chat_date
            ON telegram_command_messages (chat_id, message_date DESC);

        CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_user_date
            ON telegram_command_messages (user_id, message_date DESC);

        CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_prefix_date
            ON telegram_command_messages (command_prefix, message_date DESC);

        CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_status_date
            ON telegram_command_messages (command_status, message_date ASC);

        CREATE INDEX IF NOT EXISTS ix_telegram_command_messages_command_id
            ON telegram_command_messages (command_id);
        """
    )


def source_expr(columns: set[str], *candidates: str, default: str = "NULL") -> str:
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return default
