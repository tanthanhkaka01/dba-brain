"""The two archive tables' primary keys - on a store made before they had one (rules R22).

``job_runs_history`` and ``metric_results_archive`` are where rows go when they age out of
``job_runs`` and ``metric_results``. Each row keeps the id it had there (``log_id``, ``result_id``),
which is the key it always should have had: without one the migration tool cannot copy the table
row for row, a re-archived row would sit beside itself unnoticed, and nothing can address one row.
A store made from 0.24.0 creates both with the key.

A store made before keeps its tables, because ``CREATE TABLE IF NOT EXISTS`` changes nothing that
exists - and this module is **not** run by any app starting. On the estate's store the archive is
14 million rows and 7 GB: adding the key reads all of it and holds the table while it builds the
index, and one duplicate id would make it fail. So it is a command the operator runs, when the
store can take it (``db.cli archive-keys``): the plan reads and reports - rows, missing ids,
duplicate ids - and ``--apply`` adds the key only to a table the plan found clean.
"""

from __future__ import annotations

import re
from typing import Any

#: table -> the id each row kept from the table it was archived from.
ARCHIVE_KEYS = {"job_runs_history": "log_id", "metric_results_archive": "result_id"}


def plan(conn: Any, *, backend: str) -> list[dict[str, Any]]:
    """What each archive table holds and whether it can take its key. Reads only."""
    report = []
    for table, key in ARCHIVE_KEYS.items():
        entry: dict[str, Any] = {"table": table, "key": key}
        if not _exists(conn, table, backend=backend):
            report.append({**entry, "state": "absent"})
            continue
        if _has_primary_key(conn, table, backend=backend):
            report.append({**entry, "state": "keyed"})
            continue
        # Rows are mappings on both backends - never unpacked by position.
        counts = conn.execute(
            f"SELECT COUNT(*) AS n_rows, SUM(CASE WHEN {key} IS NULL THEN 1 ELSE 0 END) AS n_missing, "
            f"COUNT({key}) - COUNT(DISTINCT {key}) AS n_duplicated FROM {table}").fetchone()
        entry.update(rows=int(counts["n_rows"] or 0), missing_ids=int(counts["n_missing"] or 0),
                     duplicate_ids=int(counts["n_duplicated"] or 0))
        entry["state"] = "ready" if not entry["missing_ids"] and not entry["duplicate_ids"] else "blocked"
        report.append(entry)
    return report


def apply(conn: Any, *, backend: str, schema_sql: dict[str, str]) -> list[dict[str, Any]]:
    """Add the key to every table the plan finds ``ready``; leave the rest, and say why.

    ``schema_sql`` is the ``CREATE TABLE`` script that now declares each table with its key - a
    SQLite table cannot gain a primary key in place, so it is rebuilt from that declaration and
    its rows copied across; PostgreSQL adds the constraint where the table stands.
    """
    report = plan(conn, backend=backend)
    for entry in report:
        if entry["state"] != "ready":
            continue
        table, key = entry["table"], entry["key"]
        if backend == "postgresql":
            conn.execute(f"ALTER TABLE {table} ADD PRIMARY KEY ({key})")
        else:
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_before_key")
            conn.execute(_create_statement(schema_sql[table], table))
            conn.execute(f"INSERT INTO {table} SELECT * FROM {table}_before_key")
            conn.execute(f"DROP TABLE {table}_before_key")
        entry["state"] = "keyed-now"
    return report


def _create_statement(script: str, table: str) -> str:
    match = re.search(rf"CREATE TABLE IF NOT EXISTS {table}\s*\(.*?\);", script, re.DOTALL)
    if match is None:
        raise ValueError(f"the schema declares no {table}")
    return match.group(0)


def _exists(conn: Any, table: str, *, backend: str) -> bool:
    if backend == "postgresql":
        row = conn.execute("SELECT COUNT(*) AS n FROM information_schema.tables "
                           "WHERE table_schema = current_schema() AND table_name = ?", (table,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name = ?",
                           (table,)).fetchone()
    return bool(row and row["n"])


def _has_primary_key(conn: Any, table: str, *, backend: str) -> bool:
    if backend == "postgresql":
        row = conn.execute("SELECT COUNT(*) AS n FROM information_schema.table_constraints "
                           "WHERE table_schema = current_schema() AND table_name = ? "
                           "AND constraint_type = 'PRIMARY KEY'", (table,)).fetchone()
        return bool(row and row["n"])
    return any(column["pk"] for column in conn.execute(f'PRAGMA table_info("{table}")').fetchall())
