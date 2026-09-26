"""The two archive tables get their primary keys - on a new store at once, on an old one by command.

`job_runs_history` and `metric_results_archive` had none (found by rules R22's guard, 0.24.0). A new
store declares them with the id each row kept (`log_id`, `result_id`). An existing store keeps its
tables - `CREATE TABLE IF NOT EXISTS` changes nothing that exists - and on this estate the archive is
14 million rows, so adding the key is not something an app starting may do on its own: it is
`db.cli archive-keys`, which reports first and adds a key only to a table it found clean. These run
it against a store built the old way.
"""

from __future__ import annotations

import sqlite3

import pytest

from db_ops.db import DbOpsStore, archive_keys
from db_ops.db.metric_store import SCHEMA_SQL as METRIC_SCHEMA_SQL, MetricStore
from db_ops.db.store import SCHEMA_SQL as STORE_SCHEMA_SQL

SCHEMAS = {"job_runs_history": STORE_SCHEMA_SQL, "metric_results_archive": METRIC_SCHEMA_SQL}


@pytest.fixture()
def old_store(tmp_path):
    """A store as 0.23.0 built it: both archives without a key, holding rows."""
    path = tmp_path / "db_ops.sqlite"
    DbOpsStore(path).initialize()
    MetricStore(path).initialize()
    with sqlite3.connect(path) as conn:
        for table, key in archive_keys.ARCHIVE_KEYS.items():
            keyed = archive_keys._create_statement(SCHEMAS[table], table)
            conn.execute(f"DROP TABLE {table}")
            conn.execute(keyed.replace(f"{key} INTEGER PRIMARY KEY", f"{key} INTEGER"))
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            for value in (1, 2, 3):
                conn.execute(f"INSERT INTO {table} ({key}, archived_at) VALUES (?, ?)",
                             (value, "2026-09-01T00:00:00Z"))
            assert key in columns
    return path


def _plan(path, **kwargs):
    store = DbOpsStore(path)
    with store.connect() as conn:
        return {entry["table"]: entry for entry in archive_keys.plan(conn, backend="sqlite", **kwargs)}


def _apply(path):
    store = DbOpsStore(path)
    with store.connect() as conn:
        report = archive_keys.apply(conn, backend="sqlite", schema_sql=SCHEMAS)
    DbOpsStore(path).initialize(force=True)
    MetricStore(path).initialize(force=True)
    return {entry["table"]: entry for entry in report}


def _keyed(path, table):
    with sqlite3.connect(path) as conn:
        return any(column[5] for column in conn.execute(f'PRAGMA table_info("{table}")'))


def test_a_new_store_declares_both_keys(tmp_path):
    path = tmp_path / "db_ops.sqlite"
    DbOpsStore(path).initialize()
    MetricStore(path).initialize()
    assert _keyed(path, "job_runs_history") and _keyed(path, "metric_results_archive")
    assert {e["state"] for e in _plan(path).values()} == {"keyed"}


def test_the_plan_only_reads_and_says_what_it_found(old_store):
    before = old_store.read_bytes()
    plan = _plan(old_store)
    assert plan["job_runs_history"] == {"table": "job_runs_history", "key": "log_id", "rows": 3,
                                        "missing_ids": 0, "duplicate_ids": 0, "state": "ready"}
    assert not _keyed(old_store, "job_runs_history")
    assert old_store.read_bytes() == before, "the plan wrote to the store"


def test_apply_keys_a_clean_table_and_keeps_its_rows_and_its_index(old_store):
    report = _apply(old_store)
    assert {e["state"] for e in report.values()} == {"keyed-now"}
    with sqlite3.connect(old_store) as conn:
        for table, key in archive_keys.ARCHIVE_KEYS.items():
            assert _keyed(old_store, table)
            assert [row[0] for row in conn.execute(f"SELECT {key} FROM {table} ORDER BY 1")] == [1, 2, 3]
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"ix_job_runs_history_created_at", "ix_metric_results_archive_collected_at"} <= indexes


def test_a_duplicated_id_leaves_that_table_alone_and_names_it(old_store):
    with sqlite3.connect(old_store) as conn:
        conn.execute("INSERT INTO job_runs_history (log_id, archived_at) VALUES (2, 'again')")
    report = _apply(old_store)
    assert report["job_runs_history"]["state"] == "blocked"
    assert report["job_runs_history"]["duplicate_ids"] == 1
    assert not _keyed(old_store, "job_runs_history")
    assert report["metric_results_archive"]["state"] == "keyed-now", "one blocked table stops no other"


def test_running_it_again_changes_nothing(old_store):
    _apply(old_store)
    assert {e["state"] for e in _apply(old_store).values()} == {"keyed"}


def test_archiving_still_works_into_a_keyed_table(old_store):
    _apply(old_store)
    store = DbOpsStore(old_store)
    with store.connect() as conn:
        conn.execute("INSERT INTO job_runs_history (log_id, archived_at) VALUES (4, 'later')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO job_runs_history (log_id, archived_at) VALUES (4, 'twice')")
