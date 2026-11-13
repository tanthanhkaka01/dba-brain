"""Reading the runtime store for a model: its tables, and one SELECT that is always rolled back.

0.34.0 (1.260) for the Telegram assistant; moved down in 0.38.0 (1.312) when the MCP server needed
the same two reads. A SELECT passes :func:`dbabrain.lib.read_only_sql.read_only_sql` first, then runs
in a read-only transaction (``SET TRANSACTION READ ONLY`` on PostgreSQL, ``PRAGMA query_only`` on
SQLite) with a statement timeout, and is rolled back whatever happens. A blocked table is neither
listed nor readable.
"""

from __future__ import annotations

from typing import Any

from dbabrain.lib import errors
from dbabrain.lib.read_only_sql import ReadOnlyRefusal, read_only_sql


class StoreReadRefused(errors.RequestError):
    """A table that is blocked or not there."""


def store_schema(store_target: Any, *, table: str = "", blocked_tables: list[str] | tuple[str, ...] = ()
                 ) -> dict[str, Any]:
    """Every table and its columns - or one table's columns with their types."""
    table = str(table or "").strip()
    blocked = {name.lower() for name in blocked_tables}
    if table and table.lower() in blocked:
        raise StoreReadRefused(f"table {table} is not readable.")
    with store_target.connect() as conn:
        if store_target.is_sqlite:
            names = [row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name").fetchall()]
            columns = {name: [(row["name"], row["type"]) for row in conn.execute(f'PRAGMA table_info("{name}")')]
                       for name in names if not table or name == table}
        else:
            binds: tuple[Any, ...] = ()
            sql = ("SELECT table_name, column_name, data_type FROM information_schema.columns "
                   "WHERE table_schema = current_schema()")
            if table:
                sql += " AND table_name = ?"
                binds = (table,)
            columns = {}
            for row in conn.execute(sql + " ORDER BY table_name, ordinal_position", binds).fetchall():
                columns.setdefault(row["table_name"], []).append((row["column_name"], row["data_type"]))
        conn.rollback()
    columns = {name: cols for name, cols in columns.items() if name.lower() not in blocked}
    if table:
        if table not in columns:
            raise StoreReadRefused(f"no table {table} in the store.")
        return {"table": table, "columns": [{"name": name, "type": kind} for name, kind in columns[table]]}
    return {"tables": {name: ", ".join(col for col, _kind in cols) for name, cols in sorted(columns.items())}}


def store_query(store_target: Any, sql: str, *, max_rows: int, timeout_seconds: int,
                blocked_tables: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """``{columns, rows, row_count, truncated}`` of one read-only SELECT; :class:`ReadOnlyRefusal` else."""
    text = read_only_sql(str(sql or ""), blocked_tables=blocked_tables)
    limit = max(1, int(max_rows))
    with store_target.connect() as conn:
        try:
            if store_target.is_sqlite:
                conn.execute("PRAGMA query_only = ON")
            else:
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute(f"SET LOCAL statement_timeout = {int(timeout_seconds) * 1000}")
            cursor = conn.execute(text)
            names = [item[0] for item in (cursor.description or [])]
            rows = cursor.fetchmany(limit + 1) if hasattr(cursor, "fetchmany") else cursor.fetchall()[: limit + 1]
        finally:
            conn.rollback()
            if store_target.is_sqlite:
                conn.execute("PRAGMA query_only = OFF")
    truncated = len(rows) > limit
    rows = rows[:limit]
    values = [[row[index] for index in range(len(names))] for row in rows]
    return {"columns": names, "rows": values, "row_count": len(values), "truncated": truncated}


__all__ = ["ReadOnlyRefusal", "StoreReadRefused", "store_query", "store_schema"]
