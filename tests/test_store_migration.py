"""Tests for the SQLite -> PostgreSQL store migration.

The translation and encoding layers are pure functions over an introspected SQLite schema, so
they are tested against a real temporary SQLite database built by the store's own initializers.
That is the point: the tool must track whatever ``DbOpsStore``/``MetricStore``/``SlaStore``
create, so the tests read the same source of truth rather than a restated copy of the schema.

Nothing here talks to a live PostgreSQL server.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from db_ops.db import sqlite_to_postgres as M
from db_ops.db.postgres_store import PostgresStoreError, validate_identifier


@pytest.fixture
def store_sqlite(tmp_path):
    """A real db_ops store, created by the app's own initializers."""
    from db_ops.backup_restore.history import BackupRestoreHistory
    from db_ops.db import DbOpsStore
    from db_ops.metrics.storage import MetricStore
    from db_ops.sla.storage import SlaStore

    path = tmp_path / "db_ops.sqlite"
    DbOpsStore(path).initialize()
    MetricStore(path).initialize()
    SlaStore(path).initialize()
    BackupRestoreHistory(path).store.initialize()
    return path


# --------------------------------------------------------------------------- #
# Schema introspection
# --------------------------------------------------------------------------- #
def test_reads_every_store_table(store_sqlite):
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, indexes, foreign_keys = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    names = {table.name for table in tables}
    # One from each initializer, so a store that stops creating one of them fails here.
    for expected in ("job_runs", "metric_results", "sla_runs", "telegram_messages", "reports"):
        assert expected in names
    assert indexes, "secondary indexes must be picked up"
    assert foreign_keys, "foreign keys must be picked up"


def test_autoincrement_pk_is_detected(store_sqlite):
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, _, _ = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    by_name = {table.name: table for table in tables}
    assert by_name["job_runs"].autoincrement_column == "log_id"
    assert by_name["metric_results"].autoincrement_column == "result_id"
    # A text primary key is not an identity column.
    assert by_name["report_types"].autoincrement_column == ""
    assert by_name["report_types"].pk_columns == ("report_type",)


def test_composite_primary_key_keeps_its_order(store_sqlite):
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, _, _ = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    state = next(table for table in tables if table.name == "report_send_state")
    assert state.pk_columns == ("report_code", "channel")


def test_unique_constraints_stay_constraints_not_indexes(store_sqlite):
    """telegram_messages declares UNIQUE(chat_id, message_id). Turning it into a plain index
    would silently break the ON CONFLICT upserts the Telegram app relies on."""
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, indexes, _ = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    messages = next(table for table in tables if table.name == "telegram_messages")
    assert ("chat_id", "message_id") in messages.unique_constraints
    # The implicit SQLite index behind it must not be re-created separately.
    assert not any(index.name.startswith("sqlite_autoindex") for index in indexes)


def test_primary_key_autoindex_is_not_migrated(store_sqlite):
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        _, indexes, _ = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    assert all("autoindex" not in index.name for index in indexes)


# --------------------------------------------------------------------------- #
# Type mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "declared,expected",
    [
        ("INTEGER", "BIGINT"),
        ("int", "BIGINT"),
        ("BIGINT", "BIGINT"),
        ("TEXT", "TEXT"),
        ("VARCHAR(50)", "TEXT"),
        ("CLOB", "TEXT"),
        ("REAL", "DOUBLE PRECISION"),
        ("FLOAT", "DOUBLE PRECISION"),
        ("DOUBLE", "DOUBLE PRECISION"),
        ("BLOB", "BYTEA"),
        ("DECIMAL(10,2)", "NUMERIC"),
        ("", "TEXT"),
    ],
)
def test_postgres_type_follows_sqlite_affinity(declared, expected):
    assert M.postgres_type(declared) == expected


def test_store_uses_only_types_the_mapper_handles(store_sqlite):
    """A new column with an unmapped type must not slip in unnoticed."""
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, _, _ = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    declared = {column.declared_type.upper() for table in tables for column in table.columns}
    assert declared <= {"INTEGER", "REAL", "TEXT"}, f"unexpected declared types: {declared}"


# --------------------------------------------------------------------------- #
# DEFAULT translation
# --------------------------------------------------------------------------- #
def test_sqlite_utc_now_default_becomes_the_same_string_format():
    translated = M.translate_default("strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
    assert "to_char(now() AT TIME ZONE 'UTC'" in translated
    assert 'YYYY-MM-DD"T"HH24:MI:SS"Z"' in translated


@pytest.mark.parametrize("literal", ["'{}'", "''", "'created'", "'waiting'", "'*'", "0", "1", "24"])
def test_literal_defaults_pass_through(literal):
    assert M.translate_default(literal) == literal


def test_no_default_stays_none():
    assert M.translate_default(None) is None
    assert M.translate_default("   ") is None


def test_untranslatable_expression_default_raises_instead_of_being_dropped():
    """Dropping a DEFAULT on a NOT NULL column turns the first insert into a constraint error,
    so an unknown expression must stop the migration instead of being silently discarded."""
    with pytest.raises(M.MigrationError, match="Cannot translate SQLite DEFAULT"):
        M.translate_default("(julianday('now') * 86400)")


def test_every_store_default_is_translatable(store_sqlite):
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, _, _ = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    for table in tables:
        for column in table.columns:
            M.translate_default(column.default)  # must not raise


# --------------------------------------------------------------------------- #
# DDL generation
# --------------------------------------------------------------------------- #
def test_identity_is_by_default_so_migrated_ids_can_be_inserted(store_sqlite):
    """GENERATED ALWAYS would reject the original id values the migration copies in."""
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, _, _ = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    job_runs = next(table for table in tables if table.name == "job_runs")
    sql = M.create_table_sql(job_runs, schema="db_ops")
    assert '"log_id" BIGINT GENERATED BY DEFAULT AS IDENTITY' in sql
    assert "GENERATED ALWAYS" not in sql
    assert 'PRIMARY KEY ("log_id")' in sql


def test_create_table_is_schema_qualified_and_idempotent(store_sqlite):
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, _, _ = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    sql = M.create_table_sql(tables[0], schema="db_ops")
    assert sql.startswith('CREATE TABLE IF NOT EXISTS "db_ops".')


def test_descending_index_keeps_its_direction():
    index = M.SqliteIndex(
        name="ix_job_runs_created_at", table="job_runs", unique=False,
        columns=(("created_at", True),),
    )
    sql = M.create_index_sql(index, schema="db_ops")
    assert '"created_at" DESC' in sql
    assert sql.startswith('CREATE INDEX IF NOT EXISTS')


def test_unique_index_stays_unique():
    index = M.SqliteIndex(
        name="ux_target_health_run_target", table="target_health", unique=True,
        columns=(("run_id", False), ("target_id", False)),
    )
    assert "CREATE UNIQUE INDEX" in M.create_index_sql(index, schema="db_ops")


def test_foreign_key_statement_is_schema_qualified_on_both_sides():
    fk = M.SqliteForeignKey(
        table="sla_results", column="sla_run_id",
        references_table="sla_runs", references_column="sla_run_id",
    )
    sql = M.add_foreign_key_sql(fk, schema="db_ops")
    assert 'ALTER TABLE "db_ops"."sla_results"' in sql
    assert 'REFERENCES "db_ops"."sla_runs" ("sla_run_id")' in sql


def test_build_ddl_groups_phases_so_indexes_come_after_the_load(store_sqlite):
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, indexes, foreign_keys = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    ddl = M.build_ddl(tables, indexes, foreign_keys, schema="db_ops")
    assert set(ddl) == {"tables", "indexes", "foreign_keys"}
    assert len(ddl["tables"]) == len(tables)
    assert len(ddl["indexes"]) == len(indexes)


# --------------------------------------------------------------------------- #
# COPY encoding — where a data-corrupting bug would actually live
# --------------------------------------------------------------------------- #
def _encode(value, target_type="TEXT"):
    return M.encode_copy_value(value, target_type=target_type, table="t", column="c")


def test_null_and_empty_string_are_distinguishable():
    """The store holds both. CSV cannot tell them apart without writer-specific quoting, which
    is why COPY *text* format is used: NULL is \\N and the empty string is nothing at all."""
    assert _encode(None) == r"\N"
    assert _encode("") == ""


def test_a_literal_backslash_n_is_not_mistaken_for_null():
    """A value that happens to be the text \\N must survive as text, not become NULL."""
    assert _encode("\\N") == "\\\\N"


@pytest.mark.parametrize(
    "raw,encoded",
    [
        ("a\tb", "a\\tb"),
        ("a\nb", "a\\nb"),
        ("a\r\nb", "a\\r\\nb"),
        ("a\\b", "a\\\\b"),
        (r"C:\path\to", "C:\\\\path\\\\to"),
    ],
)
def test_field_and_row_terminators_are_escaped(raw, encoded):
    assert _encode(raw) == encoded


def test_unicode_passes_through_unescaped():
    value = "Xin chào — ĐB ops ✓ 日本語"
    assert _encode(value) == value


def test_json_payloads_survive():
    payload = json.dumps({"a": [1, 2, None], "b": "x\ty"})
    assert _encode(payload) == payload.replace("\\", "\\\\").replace("\t", "\\t")


def test_integers_are_coerced_and_int64_max_survives():
    assert _encode(9223372036854775807, "BIGINT") == "9223372036854775807"
    assert _encode("42", "BIGINT") == "42"
    assert _encode(True, "BIGINT") == "1"


def test_text_in_an_integer_column_names_the_table_and_column():
    """SQLite allows any type in any column, so this really happens. The error must say where."""
    with pytest.raises(M.MigrationError, match=r"t\.c: value 'abc'"):
        _encode("abc", "BIGINT")


def test_float_round_trips_at_full_precision():
    assert float(_encode(0.1 + 0.2, "DOUBLE PRECISION")) == 0.1 + 0.2


def test_bad_float_is_reported():
    with pytest.raises(M.MigrationError, match="DOUBLE PRECISION"):
        _encode("not-a-number", "DOUBLE PRECISION")


# --------------------------------------------------------------------------- #
# Safety rails
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name", ["db ops", "db_ops; DROP DATABASE x", 'db"ops', "DBOPS", "1db", "", "a" * 64]
)
def test_unsafe_identifiers_are_refused(name):
    with pytest.raises(PostgresStoreError):
        validate_identifier(name, kind="database")


@pytest.mark.parametrize("name", ["db_ops", "_x", "metric_results", "a" * 63])
def test_safe_identifiers_are_accepted(name):
    assert validate_identifier(name, kind="table") == name


def test_source_is_opened_read_only(store_sqlite):
    """The worker's daemon keeps writing to this file; the migration must not be able to alter it."""
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE should_not_work (x INTEGER)")
    finally:
        conn.close()


def test_missing_source_is_reported_clearly(tmp_path):
    with pytest.raises(M.MigrationError, match="SQLite store not found"):
        M.open_sqlite_readonly(tmp_path / "nope.sqlite")


def test_unknown_table_selection_is_refused(store_sqlite):
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, _, _ = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    with pytest.raises(M.MigrationError, match="Unknown table"):
        M._select_tables(tables, only_tables=["no_such_table"], exclude_tables=[])


def test_table_selection_filters_both_ways(store_sqlite):
    conn = M.open_sqlite_readonly(store_sqlite)
    try:
        tables, _, _ = M.read_sqlite_schema(conn)
    finally:
        conn.close()
    only = M._select_tables(tables, only_tables=["job_runs", "reports"], exclude_tables=[])
    assert {table.name for table in only} == {"job_runs", "reports"}
    excluded = M._select_tables(tables, only_tables=[], exclude_tables=["job_runs"])
    assert "job_runs" not in {table.name for table in excluded}


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
def test_snapshot_creates_a_standalone_consistent_copy(store_sqlite, tmp_path):
    conn = sqlite3.connect(store_sqlite)
    conn.execute(
        "INSERT INTO job_runs (job_code, level, status, message) VALUES ('t','logging','done','x')"
    )
    conn.commit()
    conn.close()

    snapshot = M.snapshot_sqlite(store_sqlite, tmp_path / "snap.sqlite")
    assert snapshot.exists()
    copied = sqlite3.connect(snapshot)
    try:
        assert copied.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 1
    finally:
        copied.close()


def test_snapshot_refuses_to_overwrite(store_sqlite, tmp_path):
    target = tmp_path / "snap.sqlite"
    target.write_text("existing", encoding="utf-8")
    with pytest.raises(M.MigrationError, match="already exists"):
        M.snapshot_sqlite(store_sqlite, target)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def test_result_is_not_ok_when_counts_disagree():
    result = M.MigrationResult(schema="db_ops", database="db_ops")
    result.tables.append(M.TableResult(table="job_runs", source_rows=10, target_rows=10))
    assert result.ok
    result.tables.append(M.TableResult(table="reports", source_rows=5, target_rows=4))
    assert not result.ok
    assert "MISMATCH" in result.report()


def test_skipped_table_does_not_fail_the_run():
    result = M.MigrationResult(schema="db_ops", database="db_ops")
    result.tables.append(
        M.TableResult(table="reports", skipped=True, reason="not selected")
    )
    assert result.ok


def test_dry_run_does_not_report_mismatch():
    """A dry run never queries the target, so comparing source against a target of 0 labelled every
    table MISMATCH and made a healthy plan read as a failure."""
    result = M.MigrationResult(schema="db_ops", database="db_ops", dry_run=True)
    result.tables.append(M.TableResult(table="job_runs", source_rows=1019430, planned=True))
    assert result.ok
    report = result.report()
    assert "MISMATCH" not in report
    assert "would copy" in report


def test_a_real_run_still_reports_mismatch():
    result = M.MigrationResult(schema="db_ops", database="db_ops")
    result.tables.append(M.TableResult(table="job_runs", source_rows=10, target_rows=9))
    assert not result.ok
    assert "MISMATCH" in result.report()


def test_nul_byte_is_stripped_because_postgres_text_cannot_hold_it():
    """PostgreSQL aborts a whole COPY with 'invalid byte sequence for encoding "UTF8": 0x00'.
    SQLite has no such restriction, and a command collector that captured binary output really does
    leave NULs in raw_stdout. Found on the live 5.3 GB store at metric_results line 21546."""
    assert _encode("before\x00after") == "beforeafter"
    assert "\x00" not in _encode("\x00\x00")


def test_nul_values_are_counted_so_the_change_is_not_silent():
    rows = [("clean", "has\x00nul"), ("also\x00bad", None), (123, "fine")]
    assert M.count_nul_values(rows) == 2


def test_sanitized_count_is_reported_on_the_table_line():
    result = M.TableResult(table="metric_results", source_rows=10, target_rows=10,
                           copied_rows=10, seconds=1.0, sanitized_values=3)
    assert result.verified
    assert "sanitized 3 NUL value(s)" in result.line()


def test_no_note_when_nothing_was_sanitized():
    result = M.TableResult(table="job_runs", source_rows=5, target_rows=5, copied_rows=5, seconds=1.0)
    assert "sanitized" not in result.line()


def test_skip_data_is_exposed_by_the_cli():
    """The resume path for "copied everything, then the index phase failed"."""
    import inspect

    from db_ops.db import cli

    assert "skip_data" in inspect.signature(M.migrate).parameters
    parser = cli.build_parser()
    action = next(
        a for a in parser._subparsers._group_actions[0].choices["migrate-sqlite-to-postgres"]._actions
        if a.dest == "skip_data"
    )
    assert action.help and "resume" in action.help.lower()


def test_timestamp_column_is_detected_per_table_shape():
    """Delta mode re-syncs the recent window using each table's own date column."""
    def table(*names):
        return M.SqliteTable(
            name="t",
            columns=tuple(M.SqliteColumn(name=n, declared_type="TEXT", not_null=False,
                                         default=None, pk_position=0) for n in names),
            autoincrement_column="id")
    assert M._timestamp_column(table("collected_at", "created_at")) == "collected_at"
    assert M._timestamp_column(table("created_at")) == "created_at"
    assert M._timestamp_column(table("row_ins_date")) == "row_ins_date"
    assert M._timestamp_column(table("nothing_dated")) == ""


def test_delta_defaults_are_exposed():
    assert M.DEFAULT_RELOAD_UNDER_ROWS > 0
    assert M.DEFAULT_RESYNC_DAYS >= 0
    import inspect
    params = inspect.signature(M.migrate).parameters
    for name in ("delta", "reload_under", "resync_days"):
        assert name in params
