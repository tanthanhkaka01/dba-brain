"""Tests for the store backend adapter (db_ops/db/backend.py).

The adapter's job is to let the store classes run on SQLite or PostgreSQL without branching at
their 73 ``with self.connect()`` blocks and 335 ``row["column"]`` accesses. Two things are worth
guarding hard:

* the **SQLite path must stay exactly what it was** - it returns a real ``sqlite3.Connection``, so a
  regression here would hit every existing deployment;
* the **statement translation** must not quietly change meaning.

The live-PostgreSQL behaviour (lastrowid, CHECK constraints, upserts) is exercised against a real
server during development; these tests cover everything that can be asserted without one.
"""
from __future__ import annotations

import sqlite3

import pytest

from db_ops.db import backend
from db_ops.db.backend import POSTGRES_UTC_NOW, Row, split_statements, translate_statement


# --------------------------------------------------------------------------- #
# The SQLite path is untouched
# --------------------------------------------------------------------------- #
def test_sqlite_backend_returns_a_real_sqlite3_connection(tmp_path):
    """No wrapper on the default path: the store keeps its exact previous behaviour."""
    conn = backend.open_sqlite_connection(tmp_path / "db_ops.sqlite")
    try:
        assert isinstance(conn, sqlite3.Connection)
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_sqlite_backend_applies_the_store_pragmas(tmp_path):
    conn = backend.open_sqlite_connection(tmp_path / "db_ops.sqlite")
    try:
        assert conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys;").fetchone()[0] == 1
    finally:
        conn.close()


def test_sqlite_backend_creates_the_parent_directory(tmp_path):
    target = tmp_path / "nested" / "deeper" / "db_ops.sqlite"
    conn = backend.open_sqlite_connection(target)
    try:
        assert target.parent.is_dir()
    finally:
        conn.close()


def test_open_connection_routes_on_the_declared_backend(tmp_path):
    from db_ops.config import SqliteStoreConfig, StoreConfig

    store = StoreConfig(backend="sqlite", sqlite=SqliteStoreConfig(path=tmp_path / "s.sqlite"))
    conn = backend.open_connection(store)
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()


def test_unknown_backend_is_refused(tmp_path):
    from db_ops.config import StoreConfig

    with pytest.raises(backend.StoreBackendError, match="Unknown store backend"):
        backend.open_connection(StoreConfig(backend="mysql"))


# --------------------------------------------------------------------------- #
# Row: the sqlite3.Row surface the store relies on
# --------------------------------------------------------------------------- #
def test_row_supports_key_and_positional_access():
    row = Row(("log_id", "job_code"), (7, "backup"))
    assert row["log_id"] == 7
    assert row["job_code"] == "backup"
    assert row[0] == 7
    assert row[1] == "backup"


def test_row_membership_tests_column_names_not_values():
    """The store does `"error_text" in row` to probe for a column. A tuple would have tested
    values instead and silently answered the wrong question."""
    row = Row(("error_text",), ("boom",))
    assert "error_text" in row
    assert "boom" not in row
    assert "missing" not in row


def test_row_keys_and_dict_conversion():
    row = Row(("a", "b"), (1, None))
    assert row.keys() == ["a", "b"]
    assert dict(row) == {"a": 1, "b": None}
    assert len(row) == 2


def test_row_get_returns_default_for_missing_column():
    row = Row(("a",), (1,))
    assert row.get("a") == 1
    assert row.get("zzz") is None
    assert row.get("zzz", "fallback") == "fallback"


def test_row_raises_index_error_for_unknown_column_like_sqlite3_row():
    row = Row(("a",), (1,))
    with pytest.raises(IndexError):
        row["nope"]


def test_row_matches_sqlite3_row_behaviour(tmp_path):
    """Pin the contract against the real thing rather than against my memory of it."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        native = conn.execute("SELECT 1 AS a, NULL AS b").fetchone()
        ours = Row(("a", "b"), (1, None))
        assert list(native.keys()) == ours.keys()
        assert native["a"] == ours["a"] and native["b"] == ours["b"]
        assert native[0] == ours[0]
        assert dict(native) == dict(ours)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Statement translation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "pragma",
    ["PRAGMA journal_mode=WAL;", "PRAGMA synchronous=NORMAL;", "  pragma foreign_keys=ON;"],
)
def test_pragmas_are_skipped_not_errored(pragma):
    """The store issues these on every connection; erroring would break every call."""
    assert translate_statement(pragma) is None


def test_blank_statement_is_skipped():
    assert translate_statement("") is None
    assert translate_statement("   \n  ") is None


def test_utc_now_default_is_rewritten_to_the_same_string_format():
    sql = "CREATE TABLE t (created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))"
    out = translate_statement(sql)
    assert POSTGRES_UTC_NOW in out
    assert "strftime" not in out


def test_utc_now_is_rewritten_in_queries_too_not_only_ddl():
    """sqlite_store has one UPDATE that sets a timestamp with strftime; it must translate as well."""
    sql = "UPDATE t SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?"
    out = translate_statement(sql)
    assert "strftime" not in out
    assert POSTGRES_UTC_NOW in out
    assert out.endswith("WHERE id = ?")  # placeholders survive untouched


def test_autoincrement_pk_becomes_by_default_identity():
    """BY DEFAULT, not ALWAYS: the migration inserts original ids explicitly."""
    out = translate_statement("CREATE TABLE t (log_id INTEGER PRIMARY KEY AUTOINCREMENT, x TEXT)")
    assert "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" in out
    assert "GENERATED ALWAYS" not in out
    assert "AUTOINCREMENT" not in out


def test_ddl_types_are_mapped():
    out = translate_statement("CREATE TABLE t (a INTEGER, b REAL, c TEXT, d BLOB)")
    assert "BIGINT" in out and "DOUBLE PRECISION" in out and "BYTEA" in out
    assert "TEXT" in out


def test_type_names_are_not_rewritten_outside_ddl():
    """A query mentioning these words in a literal must be left alone."""
    sql = "SELECT * FROM t WHERE note = 'the INTEGER and REAL values'"
    assert translate_statement(sql) == sql


def test_placeholders_are_left_alone_for_qmark_paramstyle():
    """pg8000 is put in qmark mode, so '?' must survive - rewriting to %s would double-translate."""
    sql = "SELECT * FROM t WHERE a = ? AND b = ?"
    assert translate_statement(sql) == sql


def test_literal_percent_is_not_escaped():
    """Under the default 'format' paramstyle a literal % would need doubling. qmark avoids that,
    so the statement must pass through unchanged."""
    sql = "SELECT * FROM t WHERE code LIKE '%abc%'"
    assert translate_statement(sql) == sql
    assert "%%" not in translate_statement(sql)


def test_on_conflict_is_left_alone_because_both_engines_support_it():
    sql = ("INSERT INTO schema_meta (schema_name, schema_version) VALUES (?, ?) "
           "ON CONFLICT(schema_name) DO UPDATE SET schema_version = excluded.schema_version")
    assert translate_statement(sql) == sql


def test_check_constraints_survive_translation():
    sql = "CREATE TABLE t (level TEXT NOT NULL CHECK (level IN ('logging', 'error')))"
    out = translate_statement(sql)
    assert "CHECK (level IN ('logging', 'error'))" in out


# --------------------------------------------------------------------------- #
# Script splitting
# --------------------------------------------------------------------------- #
def test_splits_on_statement_boundaries():
    assert split_statements("CREATE TABLE a (x INTEGER); CREATE TABLE b (y TEXT);") == [
        "CREATE TABLE a (x INTEGER)",
        "CREATE TABLE b (y TEXT)",
    ]


def test_semicolon_inside_a_string_literal_does_not_split():
    statements = split_statements("INSERT INTO t VALUES ('a;b'); SELECT 1;")
    assert statements == ["INSERT INTO t VALUES ('a;b')", "SELECT 1"]


def test_escaped_quote_inside_a_literal_is_handled():
    statements = split_statements("INSERT INTO t VALUES ('it''s; fine'); SELECT 2;")
    assert statements == ["INSERT INTO t VALUES ('it''s; fine')", "SELECT 2"]


def test_line_comments_are_stripped():
    statements = split_statements("-- a comment with ; in it\nSELECT 1;")
    assert statements == ["SELECT 1"]


def test_trailing_statement_without_semicolon_is_kept():
    assert split_statements("SELECT 1") == ["SELECT 1"]


def test_empty_script_yields_nothing():
    assert split_statements("") == []
    assert split_statements("   ;  ;  ") == []


def test_real_store_schemas_split_into_plausible_statement_counts():
    """Guards the splitter against the actual schema scripts it has to handle."""
    from db_ops.db.store import SCHEMA_SQL
    from db_ops.metrics.storage import SCHEMA_SQL as METRIC_SQL
    from db_ops.sla.storage import SCHEMA_SQL as SLA_SQL

    for script in (SCHEMA_SQL, METRIC_SQL, SLA_SQL):
        statements = split_statements(script)
        assert statements, "schema script produced no statements"
        assert all(item.strip() for item in statements)
        # Every statement in these scripts is DDL.
        assert all(
            item.upper().startswith(("CREATE", "INSERT", "ALTER", "DROP")) for item in statements
        ), [item[:40] for item in statements if not item.upper().startswith(("CREATE", "INSERT", "ALTER", "DROP"))]


def test_every_real_schema_statement_translates():
    """No statement in the shipped schemas may fail translation."""
    from db_ops.db.store import SCHEMA_SQL
    from db_ops.metrics.storage import SCHEMA_SQL as METRIC_SQL
    from db_ops.sla.storage import SCHEMA_SQL as SLA_SQL

    for script in (SCHEMA_SQL, METRIC_SQL, SLA_SQL):
        for statement in split_statements(script):
            translated = translate_statement(statement)
            assert translated is not None
            assert "AUTOINCREMENT" not in translated.upper()
            assert "strftime" not in translated


# --------------------------------------------------------------------------- #
# StoreTarget: how the store classes choose a backend
# --------------------------------------------------------------------------- #
def test_a_bare_path_always_means_sqlite(tmp_path):
    """35 call sites and the whole test suite pass a path. That must never reach PostgreSQL,
    whatever data/store_config.json happens to say."""
    from db_ops.db.backend import StoreTarget

    target = StoreTarget.coerce(tmp_path / "db_ops.sqlite")
    assert target.is_sqlite
    assert target.sqlite_path == tmp_path / "db_ops.sqlite"


def test_from_config_follows_the_declared_backend(tmp_path):
    from db_ops.config import PostgresStoreConfig, StoreConfig
    from db_ops.db.backend import StoreTarget

    store = StoreConfig(
        backend="postgresql",
        postgresql=PostgresStoreConfig(host="h", database="db_ops", username="u"),
    )
    target = StoreTarget.from_config(store)
    assert not target.is_sqlite
    assert target.store.backend == "postgresql"


def test_from_config_rejects_something_that_is_not_a_config():
    from db_ops.db.backend import StoreTarget

    with pytest.raises(backend.StoreBackendError, match="expects a DbOpsConfig or StoreConfig"):
        StoreTarget.from_config("not-a-config")


def test_prepare_creates_the_directory_only_for_sqlite(tmp_path):
    """PostgreSQL needs no directory; creating one would leave a stray runtime/ folder behind."""
    from db_ops.config import PostgresStoreConfig, StoreConfig
    from db_ops.db.backend import StoreTarget

    sqlite_target = StoreTarget.coerce(tmp_path / "nested" / "db_ops.sqlite")
    sqlite_target.prepare()
    assert (tmp_path / "nested").is_dir()

    postgres_target = StoreTarget.from_config(
        StoreConfig(backend="postgresql",
                    postgresql=PostgresStoreConfig(host="h", database="d", username="u"))
    )
    postgres_target.prepare()  # must be a no-op, not an error


@pytest.mark.parametrize(
    "store_class_path",
    [
        ("db_ops.db.store", "DbOpsStore"),
        ("db_ops.metrics.storage", "MetricStore"),
        ("db_ops.sla.storage", "SlaStore"),
        ("db_ops.backup_restore.history", "BackupRestoreHistory"),
        ("db_ops.db.config_store", "ConfigStore"),
        ("db_ops.db.web_auth_store", "WebAuthStore"),
        ("db_ops.db.run_requests", "RunRequestStore"),
    ],
)
def test_every_store_class_takes_a_path_and_reports_sqlite(tmp_path, store_class_path):
    """Every store keeps the path constructor, and every one exposes the backend it is on."""
    import importlib

    module_name, class_name = store_class_path
    store_class = getattr(importlib.import_module(module_name), class_name)
    store = store_class(tmp_path / "db_ops.sqlite")
    assert store.backend == "sqlite"
    assert store.sqlite_path == tmp_path / "db_ops.sqlite"


@pytest.mark.parametrize(
    "store_class_path",
    [
        ("db_ops.db.store", "DbOpsStore"),
        ("db_ops.metrics.storage", "MetricStore"),
        ("db_ops.sla.storage", "SlaStore"),
        ("db_ops.backup_restore.history", "BackupRestoreHistory"),
        ("db_ops.db.config_store", "ConfigStore"),
        ("db_ops.db.web_auth_store", "WebAuthStore"),
        ("db_ops.db.run_requests", "RunRequestStore"),
    ],
)
def test_every_store_class_has_from_config(store_class_path):
    import importlib

    module_name, class_name = store_class_path
    store_class = getattr(importlib.import_module(module_name), class_name)
    assert hasattr(store_class, "from_config")


def test_pragma_table_info_becomes_a_catalog_query():
    """Six additive-migration sites ask "does this column exist?" with PRAGMA table_info. Skipping
    it like the tuning pragmas would report every column missing and re-ALTER existing columns."""
    out = translate_statement("PRAGMA table_info(job_runs);")
    assert out is not None
    assert "information_schema.columns" in out
    assert "AS name" in out
    assert "'job_runs'" in out


def test_tuning_pragmas_still_skip_while_table_info_does_not():
    assert translate_statement("PRAGMA journal_mode=WAL;") is None
    assert translate_statement("PRAGMA table_info(reports);") is not None


def test_cutoff_text_is_in_the_store_timestamp_format():
    """The window used by the metric reads has to compare against the same text format the store
    writes, or it silently matches nothing."""
    import re as _re

    from db_ops.metrics.storage import cutoff_text

    assert _re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", cutoff_text(7))
    assert cutoff_text(0) > cutoff_text(7)


# --------------------------------------------------------------------------- #
# sqlite_path and store must never disagree
# --------------------------------------------------------------------------- #
def test_explicit_sqlite_path_keeps_the_store_declaration_in_sync(tmp_path):
    """Two settable fields for one destination drift. parse_config derives them together, but a
    directly-constructed DbOpsConfig defaulted them independently - so DbOpsStore.from_config()
    silently opened the tool's default database instead of the path the caller asked for. That is
    exactly how the first PostgreSQL cutover ended up still writing to SQLite."""
    from db_ops.config import DbOpsConfig
    from db_ops.db import DbOpsStore

    target = tmp_path / "custom" / "db_ops.sqlite"
    config = DbOpsConfig(sqlite_path=target)
    assert config.store.sqlite.path == target
    assert DbOpsStore.from_config(config).sqlite_path == target
    assert DbOpsStore(config.sqlite_path).sqlite_path == target


def test_a_postgres_declaration_is_not_overwritten_by_the_path(tmp_path):
    """On a PostgreSQL store the path is informational, so it must not clobber the declaration."""
    from db_ops.config import DbOpsConfig, PostgresStoreConfig, StoreConfig

    store = StoreConfig(backend="postgresql",
                        postgresql=PostgresStoreConfig(host="h", database="db_ops", username="u"))
    config = DbOpsConfig(sqlite_path=tmp_path / "ignored.sqlite", store=store)
    assert config.store.backend == "postgresql"
    assert config.store.postgresql.host == "h"


@pytest.mark.parametrize(
    "store_class_path",
    [
        ("db_ops.db.store", "DbOpsStore"),
        ("db_ops.metrics.storage", "MetricStore"),
        ("db_ops.sla.storage", "SlaStore"),
        ("db_ops.backup_restore.history", "BackupRestoreHistory"),
        ("db_ops.db.config_store", "ConfigStore"),
        ("db_ops.db.web_auth_store", "WebAuthStore"),
        ("db_ops.db.run_requests", "RunRequestStore"),
    ],
)
def test_from_config_follows_a_postgres_declaration(store_class_path):
    """The flip is only real if the store classes act on it."""
    import importlib

    from db_ops.config import DbOpsConfig, PostgresStoreConfig, StoreConfig

    module_name, class_name = store_class_path
    store_class = getattr(importlib.import_module(module_name), class_name)
    config = DbOpsConfig(store=StoreConfig(
        backend="postgresql",
        postgresql=PostgresStoreConfig(host="h", database="db_ops", username="u")))
    assert store_class.from_config(config).backend == "postgresql"


def test_a_helper_handed_the_store_declaration_uses_it():
    """The reports/telegram CLIs inject `sqlite_path` into any helper that names it. Those helpers
    pass it straight to a store class, so handing them config.store must select PostgreSQL."""
    from db_ops.config import PostgresStoreConfig, StoreConfig
    from db_ops.db import DbOpsStore

    store = StoreConfig(backend="postgresql",
                        postgresql=PostgresStoreConfig(host="h", database="db_ops", username="u"))
    assert DbOpsStore(store).backend == "postgresql"


def test_no_app_module_constructs_a_store_from_config_sqlite_path():
    """Regression guard for the cutover that did nothing: an app passing config.sqlite_path pins
    SQLite by design, so the backend switch would not reach it.

    Uses the AST rather than a text search, so prose in a docstring or comment cannot trip it - only
    a real call does.
    """
    import ast
    import pathlib

    store_names = {"DbOpsStore", "MetricStore", "SlaStore", "BackupRestoreHistory",
                   "ConfigStore", "WebAuthStore", "RunRequestStore"}
    root = pathlib.Path(__file__).resolve().parents[1] / "db_ops"
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in store_names:
                continue
            first = node.args[0]
            if (isinstance(first, ast.Attribute) and first.attr == "sqlite_path"
                    and isinstance(first.value, ast.Name) and "config" in first.value.id):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, (
        "these construct a store from a path, so `backend: postgresql` would not reach them; "
        f"use X.from_config(config): {offenders}"
    )


def test_no_store_sql_uses_an_untranslatable_strftime_form():
    """SQLite's three-argument strftime('%Y-...','now','-N days') has no PostgreSQL equivalent and
    the translator only rewrites the two-argument UTC-now form. One inlined use in
    archive_old_results took down every metrics and SLA run seconds after the store was switched to
    PostgreSQL, with 'function strftime(unknown, unknown, unknown) does not exist'.

    Windows belong in Python (metrics.storage.cutoff_text) and get bound as parameters.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "db_ops"
    store_modules = [
        root / "db" / "store.py",
        root / "metrics" / "storage.py",
        root / "sla" / "storage.py",
        root / "backup_restore" / "history.py",
    ]
    # strftime( with two commas at the top level = the three-argument, modifier form.
    three_arg = re.compile(r"strftime\('[^']*'\s*,\s*'now'\s*,")
    offenders = []
    for path in store_modules:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if three_arg.search(line):
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert not offenders, (
        "three-argument strftime has no PostgreSQL equivalent; compute the cutoff in Python and "
        f"bind it: {offenders}"
    )


def test_no_store_sql_calls_a_sqlite_date_function_on_a_column():
    """The same lesson as the test above, one function family further on.

    `report_exists_on_local_date` compared with `datetime(created_at, '+7 hours')`. SQLite has
    `datetime`/`date`/`time`/`julianday`; PostgreSQL has none of them with those signatures, the
    translator rewrites only the two-argument `strftime` UTC-now form, and the compatibility layer
    supplies only `json_valid` and `json_extract`. Measured on 2026-09-04 on the two live stores:
    the same call answered False on SQLite and raised
    `42883 function datetime(text, unknown) does not exist` on PostgreSQL.

    It survived for months because the only scheduled caller passes `force=True` and never reaches
    it, and because the suite is offline - no test executes store SQL against a real PostgreSQL.
    That is exactly the gap a text guard can close: a window belongs in Python, bound as a
    parameter, and then it reads the same on both engines.
    """
    import ast
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "db_ops"
    store_modules = [
        root / "db" / "store.py",
        root / "db" / "metric_store.py",
        root / "db" / "sla_store.py",
        root / "db" / "backup_restore_history.py",
        root / "metrics" / "storage.py",
        root / "sla" / "storage.py",
        root / "backup_restore" / "history.py",
    ]
    # The AST, and every string literal EXCEPT the docstrings. A line scan cannot tell the SQL
    # from the prose about it: the sentence in `report_exists_on_local_date` explaining this very
    # defect names `datetime(created_at, ...)`, and a guard that trips on its own explanation gets
    # deleted rather than obeyed. Docstrings are the only prose held in strings here; a comment is
    # not a string node at all.
    on_a_column = re.compile(r"\b(datetime|date|julianday|time)\s*\(\s*[A-Za-z_][A-Za-z0-9_.\"]*\s*,")
    offenders = []
    for path in store_modules:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        prose = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    prose.add(doc)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in prose:
                continue
            found = on_a_column.search(node.value)
            if found:
                offenders.append(f"{path.relative_to(root)}:{node.lineno}: {found.group(0)}")
    assert not offenders, (
        "SQLite's date functions do not exist on PostgreSQL and are not translated; compute the "
        f"window in Python and bind it: {offenders}"
    )


# --------------------------------------------------------------------------- #
# pg8000's paramstyle is a process-wide global
# --------------------------------------------------------------------------- #
def test_no_pg8000_call_site_uses_format_placeholders():
    """pg8000's paramstyle is module-global with no per-connection override.

    The runtime store pins it to ``qmark`` for its ~128 '?' placeholders. Any other pg8000 caller
    in the same process that used '%s' therefore broke: the metrics collector's set_config call
    did, and every PostgreSQL *target* metric started failing with
    `syntax error at or near "%"` the moment the store and the collector shared a process.

    Call sites must either use '?' or bind nothing at all.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "db_ops"
    # Modules that talk to pg8000 directly.
    suspects = [
        root / "metrics" / "executor.py",
        root / "db" / "postgres_store.py",
        root / "db" / "sqlite_to_postgres.py",
        root / "db" / "backend.py",
    ]
    placeholder = re.compile(r"""execute\(\s*f?["'][^"']*%s""")
    offenders = []
    for path in suspects:
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if placeholder.search(line):
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert not offenders, (
        "pg8000 paramstyle is qmark process-wide; these use '%s' and will fail at runtime: "
        f"{offenders}"
    )


def test_paramstyle_is_pinned_where_connections_are_made():
    """One place decides it, so the two subsystems cannot disagree."""
    from db_ops.db import postgres_store

    source = __import__("inspect").getsource(postgres_store._dbapi)
    assert 'paramstyle = "qmark"' in source
