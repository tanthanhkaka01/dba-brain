"""The runtime store's schema and its migrations - the SQL that makes a store, and what upgrades one.

Split out of ``db/store.py`` on 2026-10-03 (see ``db/store_base.py``). ``DbOpsStore.initialize``
runs it; ``db/store.py`` re-exports every name.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from db_ops.db.store_base import utc_now_text




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
    -- The unit of work this row claims exclusively, or NULL for a row that claims nothing.
    -- A backup job, a restore and a `sync` app command set it to their own code; an `async` app
    -- command leaves it NULL, because the daemon is meant to call it while another copy is still
    -- running and the duplicates it must avoid are the *tasks inside it*, not itself.
    claim_key TEXT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK (json_valid(metadata_json))
);

CREATE INDEX IF NOT EXISTS ix_job_runs_created_at
    ON job_runs (created_at DESC);

CREATE INDEX IF NOT EXISTS ix_job_runs_job_code_created_at
    ON job_runs (job_code, created_at DESC);

-- The same claim for app commands, backup jobs and restores. A second restore started 47 minutes
-- into the first on 2026-09-14 because the check that should have stopped it was a read taken
-- before the decision rather than the decision itself.
CREATE UNIQUE INDEX IF NOT EXISTS ux_job_runs_claim
    ON job_runs (claim_key, COALESCE(host_name, ''))
    WHERE claim_key IS NOT NULL AND lower(status) = 'running';

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
    log_id INTEGER PRIMARY KEY,
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
    -- Which machine is running this. `job_runs` has carried it from the start; `sql_runs` did not,
    -- and a claim cannot be keyed on a value that lives inside metadata_json.
    host_name TEXT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    CHECK (json_valid(result_json)),
    CHECK (json_valid(metadata_json))
);

CREATE INDEX IF NOT EXISTS ix_sql_runs_run_key_created_at
    ON sql_runs (run_key, created_at DESC);

-- **The claim.** At most one running row per (task+target, host). A scheduler takes its turn by
-- INSERTing this row: whoever the index accepts owns the run, and whoever it refuses skips the
-- task. Before this, "is it already running?" was a SELECT followed by an INSERT, which is safe
-- only while exactly one scan process exists - and the moment a scan is allowed to overlap another
-- (a long task, a scan past its app-command timeout), both read "not running" and both start.
-- Eight duplicate production SQL runs on 2026-09-08 are what that costs.
--
-- Keyed WITH the host: two nodes sharing one schema are governed by node_role and R9, not by this
-- index, and a crashed node must not be able to block a healthy one forever The predicate is
-- lower(status) because this store holds both spellings - the daemon writes 'running' and
-- backup_restore writes 'RUNNING' - and an index that saw only one of them would guard only half
-- the rows while looking complete.
CREATE UNIQUE INDEX IF NOT EXISTS ux_sql_runs_claim
    ON sql_runs (run_key, COALESCE(host_name, '')) WHERE lower(status) = 'running';

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

-- What the operator was asked, what they answered, and which question is live right now.
--
-- `telegram_conversation_states` answers only "what is this run waiting for": the answers live in
-- a positional `args` list inside `state_json`, the prompt that produced each one is not kept, and
-- a row is marked `replaced` as the run moves on. So "which step is the operator on, what were
-- they shown, and what did they say" could not be answered from the store at all -- which is
-- exactly what a workflow with branching and a Back button has to be able to answer.
--
-- One row per *asked* step. A re-ask after Back is a NEW row rather than an update, because it is
-- a new question at a new moment; the old row keeps status='back' and the answer that was undone.
-- Exactly one row per run is 'active', and that is the definition of "the live step".
CREATE TABLE IF NOT EXISTS telegram_workflow_steps
(
    workflow_step_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    command_id INTEGER NOT NULL,
    command_text TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    parameter_name TEXT NOT NULL,
    parameter_position INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'answered', 'rejected', 'skipped', 'back', 'cancelled',
                          'abandoned')),
    prompt_text TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL DEFAULT '[]',
    controls_json TEXT NOT NULL DEFAULT '[]',
    answer_text TEXT NULL,
    -- 'option' means the answer matched one of the values offered, 'text' that it did not. With a
    -- reply keyboard a tap and the same word typed by hand arrive as the same Telegram message, so
    -- this column does NOT claim to know which one happened -- see the plan's 8a.
    answer_kind TEXT NULL
        CHECK (answer_kind IN ('option', 'text', 'inline', 'skip', 'back', 'cancel',
                              'auto', 'file')),
    is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
    state_id INTEGER NULL,
    prompt_send_tlgmsg_id INTEGER NULL,
    answer_telegram_message_id INTEGER NULL,
    asked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    answered_at TEXT NULL,
    CHECK (json_valid(options_json)),
    CHECK (json_valid(controls_json)),
    FOREIGN KEY (state_id) REFERENCES telegram_conversation_states (state_id),
    UNIQUE (run_key, step_no)
);

CREATE INDEX IF NOT EXISTS ix_telegram_workflow_steps_run
    ON telegram_workflow_steps (run_key, step_no);

CREATE INDEX IF NOT EXISTS ix_telegram_workflow_steps_active
    ON telegram_workflow_steps (status, asked_at DESC);

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

CREATE TABLE IF NOT EXISTS runtime_nodes
(
    node_id TEXT NOT NULL PRIMARY KEY,
    node_role TEXT NOT NULL DEFAULT 'master',
    hostname TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    utc_offset_minutes INTEGER NOT NULL DEFAULT 0,
    tz_abbreviation TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS ix_runtime_nodes_updated_at
    ON runtime_nodes (updated_at DESC);

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


def prepare_run_claims(conn) -> dict[str, int]:
    """Make an existing store fit to carry the claim indexes. Runs **before** the schema script.

    Two things have to be true before ``ux_sql_runs_claim`` and ``ux_job_runs_claim`` can be
    created, and neither is true of a store that has been running:

    * ``sql_runs`` needs its ``host_name`` column. ``CREATE TABLE IF NOT EXISTS`` will not add a
      column to a table that already exists, and the index names it.
    * There must be no key already holding **two** ``running`` rows. Every node that was killed
      mid-run left one behind, and this estate's shared schema has them from three soak cycles;
      a unique index over that data does not fail loudly at the right person, it fails at whoever
      next runs ``init``.

    The duplicates are closed, not deleted — the newest row of each key keeps running and the older
    ones become ``timeout``, which is what they always were in fact. Returns what it closed, so the
    upgrade can say so rather than doing it silently.
    """
    closed = {"sql_runs": 0, "job_runs": 0}
    if _table_columns(conn, "sql_runs"):
        ensure_sqlite_column(conn, table_name="sql_runs", column_name="host_name",
                             column_sql="host_name TEXT NULL")
        closed["sql_runs"] = _close_duplicate_running(
            conn, table="sql_runs", id_column="sql_run_id", key_column="run_key")
    if _table_columns(conn, "job_runs"):
        ensure_sqlite_column(conn, table_name="job_runs", column_name="claim_key",
                             column_sql="claim_key TEXT NULL")
        closed["job_runs"] = _close_duplicate_running(
            conn, table="job_runs", id_column="log_id", key_column="claim_key")
    return closed


def _table_columns(conn, table_name: str) -> set[str]:
    """The table's columns, or an empty set when it does not exist yet (a fresh store)."""
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name});")}
    except Exception:  # noqa: BLE001 - "no such table" differs per backend and means the same here.
        return set()


def _close_duplicate_running(conn, *, table: str, id_column: str, key_column: str) -> int:
    """Leave the newest ``running`` row per (key, host) and close the rest."""
    rows = conn.execute(
        f"""
        SELECT {id_column} AS row_id, {key_column} AS claim_key,
               COALESCE(host_name, '') AS claim_host
          FROM {table}
         WHERE lower(status) = 'running' AND {key_column} IS NOT NULL
         ORDER BY {id_column} DESC;
        """
    ).fetchall()
    seen: set[tuple[str, str]] = set()
    stale: list[int] = []
    for row in rows:
        key = (str(row["claim_key"]), str(row["claim_host"]))
        if key in seen:
            stale.append(int(row["row_id"]))
            continue
        seen.add(key)
    for row_id in stale:
        conn.execute(
            f"""
            UPDATE {table}
               SET status = 'timeout',
                   finished_at = ?,
                   error_text = COALESCE(error_text, '')
                       || 'closed by the schema 5 upgrade: a second running row for the same key'
             WHERE {id_column} = ?;
            """,
            (utc_now_text(), row_id),
        )
    return len(stale)


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


def _redacted_raw_json(raw: Any, replacement: str) -> str:
    """``raw_json`` with every ``text``/``caption`` value replaced - the rest (ids, dates) kept."""
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return json.dumps({"redacted": True})

    def scrub(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: (replacement if key in ("text", "caption") and isinstance(value, str)
                          else scrub(value)) for key, value in node.items()}
        if isinstance(node, list):
            return [scrub(item) for item in node]
        return node

    return json.dumps(scrub(payload), ensure_ascii=False)
