"""The runtime store: ``DbOpsStore``, composed of its table families.

Until 2026-10-03 this was one 3,400-line module. The 0.26.0 refactor (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5) split it by table family -
``store_job_runs``, ``store_telegram``, ``store_reports``, ``store_sql_runs`` (one mixin each) - with
what they share in ``store_base`` and the schema and migrations in ``store_schema``. ``DbOpsStore``
keeps its name, its constructor, ``initialize`` and ``connect`` here, and every name the module
used to define is re-exported, so no caller changes an import.
"""

from __future__ import annotations

from pathlib import Path

from db_ops.lib.config import StoreConfig
from db_ops.db import backend as backend_mod
from db_ops.db.backend import StoreTarget
from db_ops.db.store_base import (  # noqa: F401 - re-exported: every name kept its address
    RunAlreadyClaimed,
    SCHEMA_VERSION,
    _APP_COMMAND_REQUEST_ARCHIVE_COLUMNS,
    _JOB_RUN_ARCHIVE_COLUMNS,
    _archive_requests_for_runs,
    _claim_pid,
    _claim_started,
    utc_now_text,
)
from db_ops.db.store_schema import (  # noqa: F401 - re-exported
    SCHEMA_SQL,
    _close_duplicate_running,
    _redacted_raw_json,
    _table_columns,
    ensure_metric_results_table,
    ensure_sqlite_column,
    migrate_report_send_state_table,
    migrate_reports_table,
    migrate_telegram_command_messages_table,
    migrate_telegram_send_messages_table,
    prepare_run_claims,
    rebuild_reports_table_if_needed,
    seed_report_types,
    source_expr,
)
from db_ops.db.store_job_runs import JobRunsMixin
from db_ops.db.store_telegram import TelegramMixin
from db_ops.db.store_reports import ReportsMixin
from db_ops.db.store_sql_runs import SqlRunsMixin


class DbOpsStore(JobRunsMixin, TelegramMixin, ReportsMixin, SqlRunsMixin):
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
            # Before the schema script, because the script creates the claim indexes and they
            # cannot be built over a table that is missing a column or already holds duplicates.
            prepare_run_claims(conn)
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
