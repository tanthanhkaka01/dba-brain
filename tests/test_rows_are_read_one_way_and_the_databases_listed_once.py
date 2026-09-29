"""Rows are read one way, and "which databases are there" is asked in one place (rules R11).

Until 0.25.0 six modules of ``common`` held a connection and read its rows with their own
``execute`` / ``description`` / ``fetchall``, and they had drifted: one skipped a result set with
no rows in front of the answer (an ``EXEC`` before the ``SELECT``) and five did not; one turned
every value into text; the certificate import walked the result sets with ``fetchone`` and a
bare ``except``. And "list the databases" was written six times - the instance export and its
orphan check, the patch gate twice, the restore check, the PostgreSQL metric fan-out, a SQL task's
diagnosis - each with its own ``sys.databases`` or ``pg_database`` and its own columns, while
``db_catalog`` answered the same question for ``list-databases``.

Now ``sql_run.query_rows`` reads rows - on ``execute_capture``, the reader ``run-sql`` runs on - and
``db_catalog.databases`` lists the databases. The guards at the end hold both to one place.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from db_ops.common import db_catalog, restorekey, sql_run

DB_OPS = Path(__file__).resolve().parents[1] / "db_ops"


class _Cursor:
    """A DB-API cursor over a script's result sets: ``None`` for a statement that returns no rows,
    else ``(columns, rows)``. Enough of pyodbc for ``execute_capture``."""

    rowcount = -1

    def __init__(self, *sets):
        self._script = list(sets)
        self._sets: list = []
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))
        self._sets = [None if item is None else (list(item[0]), list(item[1]))
                      for item in self._script]

    @property
    def description(self):
        current = self._sets[0] if self._sets else None
        return None if current is None else [(name,) for name in current[0]]

    def fetchmany(self, size):
        columns, rows = self._sets[0]
        taken, self._sets[0] = rows[:size], (columns, rows[size:])
        return taken

    def nextset(self):
        self._sets = self._sets[1:]
        return bool(self._sets)


# --------------------------------------------------------------------------- #
# query_rows
# --------------------------------------------------------------------------- #
def test_every_row_is_read_not_the_first_page():
    """``run-sql`` caps at 50,000 rows; a catalogue read is not an export and has no page."""
    rows = [(n,) for n in range(sql_run.DEFAULT_MAX_ROWS + 5)]

    read = sql_run.query_rows(_Cursor((["n"], rows)), "SELECT n FROM t")

    assert len(read) == len(rows) and read[-1] == {"n": len(rows) - 1}


def test_a_value_is_kept_as_the_driver_returned_it():
    """A login's SID is bytes, and the export writes it back as ``0x...``: text would destroy it."""
    sid = b"\x01\x05\x00\xff"

    read = sql_run.query_rows(_Cursor((["name", "sid"], [("app", sid)])), "SELECT name, sid FROM x")

    assert read == [{"name": "app", "sid": sid}]


def test_the_answer_after_statements_with_no_rows_is_the_one_read():
    """``sp_getapplock`` then ``SELECT @rc``: the first result has no rows, and reading its
    description answered "nothing" for a query that answered."""
    cursor = _Cursor(None, None, (["rc"], [(0,)]))

    assert sql_run.query_rows(cursor, "EXEC sp_getapplock ...; SELECT @rc AS rc") == [{"rc": 0}]


def test_a_read_past_the_ceiling_is_refused_not_cut(monkeypatch):
    monkeypatch.setattr(sql_run, "QUERY_ROWS_CEILING", 3)

    with pytest.raises(sql_run.SqlRunError, match="more than 3 rows"):
        sql_run.query_rows(_Cursor((["n"], [(n,) for n in range(5)])), "SELECT n FROM t")


def test_values_are_bound_as_a_sequence():
    """pg8000, pymssql and oracledb take a sequence only; the lookups bind the table they check."""
    cursor = _Cursor((["count"], [(1,)]))

    assert sql_run.query_value(cursor, "SELECT COUNT(*) FROM t WHERE a = ?", params=("x",)) == 1
    assert cursor.executed == [("SELECT COUNT(*) FROM t WHERE a = ?", ("x",))]


def test_a_single_value_is_read_by_position_whatever_the_driver_calls_it():
    assert sql_run.query_value(_Cursor(([""], [(7,)])), "SELECT COUNT(*) FROM t") == 7
    assert sql_run.query_value(_Cursor((["COUNT(*)"], [])), "SELECT COUNT(*) FROM t") is None


def test_the_certificate_import_reads_the_answer_its_batch_ends_with(monkeypatch):
    """The batch declares, checks and creates before its one ``SELECT``; over a driver the answer
    is that set - not a guess across ``fetchone`` calls that swallowed any error."""
    import db_ops.common.db_connect as db_connect

    cursor = _Cursor(None, None, (["certificate_name", "thumbprint", "imported"],
                                  [("db_ops_backup_cert_source", "A1B2", 1)]))
    monkeypatch.setattr(db_connect, "connect_engine", lambda **_: type(
        "Conn", (), {"cursor": lambda self: cursor, "close": lambda self: None})())

    answer = restorekey.import_key({"cer_path": "/c.cer", "pvk_path": "/c.pvk", "password": "pw",
                                    "target": {"host": "192.0.2.10"}})

    assert answer == {"certificate_name": "db_ops_backup_cert_source", "thumbprint": "a1b2",
                      "imported": True, "ok": True}


# --------------------------------------------------------------------------- #
# db_catalog.databases
# --------------------------------------------------------------------------- #
def test_the_databases_are_asked_with_the_catalogues_own_query():
    cursor = _Cursor((["NAME", "STATE", "IS_SYSTEM", "HAS_ACCESS"],
                      [("master", "ONLINE", 1, 1), ("APPDB", "ONLINE", 0, 0)]))

    found = db_catalog.databases(cursor, "sqlserver")

    assert cursor.executed[0][0] == db_catalog._DATABASES_SQL["sqlserver"].strip()
    assert found[1] == {"name": "APPDB", "state": "ONLINE", "is_system": 0, "has_access": 0}, \
        "keys are lower-case on every engine"


def test_sql_server_says_which_databases_this_login_can_open():
    """The instance export walks only those; ``HAS_DBACCESS`` was its own query's until 0.25.0."""
    assert "HAS_DBACCESS(d.name)" in db_catalog._DATABASES_SQL["sqlserver"]


def test_an_engine_without_a_listing_query_is_refused_by_name():
    with pytest.raises(db_catalog.DbCatalogError, match="oracle"):
        db_catalog.databases(_Cursor(), "oracle")


# --------------------------------------------------------------------------- #
# The guards
# --------------------------------------------------------------------------- #
_CATALOGUE_TABLE = re.compile(r"\bFROM\s+(?:master\.)?(?:sys\.databases|pg_database)\b", re.I)

#: Modules that name the table without listing it - each reads one database's row, or joins it -
#: and how many times. A count, so a listing added beside one still fails.
_ONE_ROW_OR_A_JOIN = {
    "common/db_catalog.py": 3,            # the listing, one query per engine; its docstring
    "common/sqlserver_emergency.py": 1,   # the database the connection is in (DB_NAME())
    "common/sqlserver_instance.py": 1,    # `model`'s row, in the replay script
    "common/sqlserver_patch.py": 1,       # joined to msdb.dbo.backupset: each last full backup
    "common/verifyrestore.py": 1,         # psql on the host counts them: the server answers
    "db/postgres_store.py": 1,            # whether the store's own database exists
}


def test_no_module_lists_the_databases_itself():
    found = {}
    for path in DB_OPS.rglob("*.py"):
        count = len(_CATALOGUE_TABLE.findall(path.read_text(encoding="utf-8")))
        if count:
            found[path.relative_to(DB_OPS).as_posix()] = count
    assert found == _ONE_ROW_OR_A_JOIN, (
        "a module reads sys.databases / pg_database: list the databases with "
        "db_catalog.databases(cursor, engine) - or `common.cli list-databases` from an app - and "
        "filter its rows (is_system, state, has_access, allow_connections)")


_CURSOR_READ = re.compile(r"\.(?:fetchall|fetchone|fetchmany)\(|\bcursor\.description\b")

#: Where ``common`` touches a cursor's rows itself, and why that is not a second reader.
_READS_A_CURSOR = {
    "common/sql_run.py",        # the reader: execute_capture, and query_rows on it
    "common/schema_copy.py",    # a table streamed from one cursor into another, not a read
}

#: A class that *is* a cursor - its reads are the driver's, handed on - is not a reader of one.
_CURSOR_ADAPTERS = {"common/sql_execution.py": "PymssqlCursorAdapter"}


def _without_adapter(relative: str, text: str) -> str:
    adapter = _CURSOR_ADAPTERS.get(relative)
    if not adapter:
        return text
    for node in ast.parse(text).body:
        if isinstance(node, ast.ClassDef) and node.name == adapter:
            return text.replace(ast.get_source_segment(text, node) or "", "")
    raise AssertionError(f"{relative} no longer defines {adapter}: update _CURSOR_ADAPTERS")


def test_no_module_of_common_reads_a_cursor_itself():
    """``metric-batch`` read every metric through ``sql_execution.execute_cursor_batches``, a second
    batch reader beside ``execute_capture``, until 0.25.0; ``sql_execution`` holds only the pymssql
    adapter now, and a reader added back beside it fails here."""
    readers = set()
    for path in (DB_OPS / "common").rglob("*.py"):
        relative = path.relative_to(DB_OPS).as_posix()
        if _CURSOR_READ.search(_without_adapter(relative, path.read_text(encoding="utf-8"))):
            readers.add(relative)
    assert readers == _READS_A_CURSOR, (
        "read rows with sql_run.query_rows / query_value (or execute_capture for a script's "
        f"result sets), not a cursor of your own: {sorted(readers - _READS_A_CURSOR)}")
