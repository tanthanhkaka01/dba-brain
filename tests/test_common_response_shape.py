"""Every `common` CLI command answers in the same shape, or callers grow branches nobody tests.

The callers here are programs - the db_ops daemon, another app, a shell script, a Telegram action.
A command that omits `data` when it has none, or reports a failure only through the exit code,
makes each of them handle that command specially, and the special case is discovered in production
by the caller that did not.

So the five keys are always present, `success` describes the *work* rather than the process, and
the exit code is a summary of the response rather than a second opinion on it.
"""

from __future__ import annotations

from db_ops.lib import response

KEYS = {"success", "operation", "message", "error", "data", "metrics"}


def test_a_success_carries_every_key():
    assert set(response.ok("list-backup-files")) == KEYS


def test_a_failure_carries_every_key():
    assert set(response.fail("restore-full", "no such path")) == KEYS


def test_data_is_an_empty_object_never_a_missing_key():
    """A caller writing result["data"] must not have to guard for a key some commands omit."""
    assert response.ok("x")["data"] == {}
    assert response.fail("x", "boom")["data"] == {}


def test_metrics_is_an_empty_object_never_a_missing_key():
    assert response.ok("x")["metrics"] == {}


def test_a_failure_says_why_in_both_error_and_message():
    """`message` is what a Telegram line quotes; defaulting it to the error saves every caller
    an `or` when it renders one."""
    result = response.fail("restore-log", "log 12 is missing from the chain")

    assert result["error"] == "log 12 is missing from the chain"
    assert result["message"] == "log 12 is missing from the chain"
    assert result["success"] is False


def test_an_explicit_message_survives_on_a_failure():
    result = response.fail("x", "ORA-01017", message="Oracle refused the login.")
    assert result["message"] == "Oracle refused the login."
    assert result["error"] == "ORA-01017"


def test_success_carries_a_null_error_not_an_empty_string():
    """`null` is checkable; "" reads as a message that failed to render."""
    assert response.ok("x")["error"] is None


def test_the_exit_code_only_ever_summarises_the_response():
    """A shell caller checking $? and a program parsing the JSON must never disagree."""
    assert response.exit_code(response.ok("x")) == 0
    assert response.exit_code(response.fail("x", "boom")) == 1


def test_the_operation_travels_inside_the_body():
    """A stored or forwarded response is still self-describing once it has been separated from
    the command line that produced it."""
    assert response.ok("restore-metadata")["operation"] == "restore-metadata"


# --------------------------------------------------------------------------- #
# The pymssql fallback must offer a whole DB-API cursor, not part of one.
# --------------------------------------------------------------------------- #

def test_the_pymssql_adapter_forwards_every_fetch():
    """Callers get the adapter, not the pymssql cursor. One that read rows any way other than
    fetchmany died with "'PymssqlCursorAdapter' object has no attribute 'fetchall'" - and only on
    the target whose ODBC stack could not negotiate TLS, so it surfaced on the fallback path alone
    and only for whichever code happened to run there."""
    from db_ops.common.sql_execution import PymssqlCursorAdapter

    class _Cursor:
        description = [("a",)]
        rowcount = 2

        def execute(self, sql):
            self.sql = sql

        def fetchall(self):
            return [(1,), (2,)]

        def fetchone(self):
            return (1,)

        def fetchmany(self, size):
            return [(1,)]

    adapter = PymssqlCursorAdapter(_Cursor())
    adapter.execute("SELECT 1")

    assert adapter.fetchall() == [(1,), (2,)]
    assert adapter.fetchone() == (1,)
    assert adapter.fetchmany(1) == [(1,)]


class _RecordingCursor:
    """A pymssql-shaped cursor that records how it was called."""

    description = [("a",)]
    rowcount = 1

    def __init__(self):
        self.calls: list[tuple] = []

    def execute(self, sql, params=None):
        self.calls.append(("execute", sql, params))

    def executemany(self, sql, seq):
        self.calls.append(("executemany", sql, list(seq)))


def test_the_pymssql_adapter_binds_parameters_like_a_db_api_cursor():
    """The same lesson as the fetch test above, found again on 2026-08-13 and again only in
    production: a table load called `execute(sql, params)` and got
    `execute() takes 2 positional arguments but 3 were given`. The adapter promises a DB-API
    cursor; DB-API `execute` takes parameters."""
    from db_ops.common.sql_execution import PymssqlCursorAdapter

    cursor = _RecordingCursor()
    adapter = PymssqlCursorAdapter(cursor)

    adapter.execute("SELECT 1 WHERE x = %s", ("value",))

    assert cursor.calls == [("execute", "SELECT 1 WHERE x = %s", ("value",))]


def test_the_pymssql_adapter_still_runs_a_statement_with_no_parameters():
    """Passing `None` through as a parameter tuple breaks pymssql for the many callers that
    bind nothing, so the no-parameter call must stay a one-argument call."""
    from db_ops.common.sql_execution import PymssqlCursorAdapter

    cursor = _RecordingCursor()
    PymssqlCursorAdapter(cursor).execute("SELECT 1")

    assert cursor.calls == [("execute", "SELECT 1", None)]


def test_the_pymssql_adapter_can_insert_a_batch():
    """`executemany` was missing entirely, so a batch insert reached the adapter and died on
    `execute` — with nothing telling the caller this cursor could not do it."""
    from db_ops.common.sql_execution import PymssqlCursorAdapter

    cursor = _RecordingCursor()
    adapter = PymssqlCursorAdapter(cursor)

    adapter.executemany("INSERT INTO t VALUES (%s)", [("a",), ("b",)])

    assert cursor.calls == [("executemany", "INSERT INTO t VALUES (%s)", [("a",), ("b",)])]


def test_an_empty_batch_does_not_reach_the_driver():
    """pymssql raises on an empty sequence; a sheet with a header and no rows is not an error."""
    from db_ops.common.sql_execution import PymssqlCursorAdapter

    cursor = _RecordingCursor()
    PymssqlCursorAdapter(cursor).executemany("INSERT INTO t VALUES (%s)", [])

    assert cursor.calls == []


def test_the_adapter_offers_every_cursor_method_its_callers_use():
    """A structural guard instead of a third round of the same bug. Both previous failures were
    a method that existed on pyodbc and not here, found only on the fallback path — which is the
    Linux worker for every SQL Server target, so 'it worked locally' proves nothing."""
    from db_ops.common.sql_execution import PymssqlCursorAdapter

    required = {"execute", "executemany", "fetchone", "fetchmany", "fetchall", "nextset",
                "description", "rowcount"}
    missing = sorted(name for name in required
                     if not hasattr(PymssqlCursorAdapter(_RecordingCursor()), name))

    assert not missing, f"PymssqlCursorAdapter is missing {missing} that callers rely on"


# --------------------------------------------------------------------------- #
# Which placeholder to write is a property of the driver, not of the engine
# --------------------------------------------------------------------------- #

def test_sql_server_placeholders_follow_the_driver_that_actually_opened():
    """pyodbc binds `?`, the pymssql fallback binds `%s`, and which one opened depends on
    whether an ODBC stack is installed — pyodbc on the Windows master, pymssql in the Linux
    worker. Answering from db_type alone was correct in development and wrong in production."""
    from db_ops.common import db_connect

    class _Pymssql:
        is_pymssql = True

    class _Pyodbc:
        is_pymssql = False

    assert db_connect.parameter_style("sqlserver", _Pyodbc()) == "qmark"
    assert db_connect.parameter_style("sqlserver", _Pymssql()) == "format"


def test_the_other_engines_do_not_change_with_the_connection():
    from db_ops.common import db_connect

    assert db_connect.parameter_style("oracle", object()) == "numeric"
    assert db_connect.parameter_style("mysql", object()) == "format"
