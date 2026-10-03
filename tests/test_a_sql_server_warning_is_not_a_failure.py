"""A SQL Server *warning* is not a failure, and a run that finished past one says so.

`SQL033-NIGHTLY-ENGINE` was recorded `error` on 2026-09-24 with

    ('01003', '[01003] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Warning: Null value is
    eliminated by an aggregate or other SET operation. (8153) (SQLMoreResults)')

SQLSTATE class 01 is a warning: the statement that raised it completed, and the task - a 59-day loop
with `autocommit` - had run to its end and committed. pyodbc raises it out of `nextset()`, and every
result loop called `nextset()` bare, so the warning failed the run exactly as an error would.

What pyodbc does at that moment was measured on a lab SQL Server rather than assumed (see
`db_ops/lib/driver_warnings.py`): the statement is closed, the rest of the batch still runs on the
server, and nothing after that point - result sets or errors - reaches the client. So these hold both
halves: a warning no longer fails the run, and the run does not claim to have seen what it could
not. Under a wrapping transaction that means a rollback with the reason; under autocommit, `done`
with the warning in the level, the message and the Telegram header.
"""

from __future__ import annotations

import pytest

from db_ops.common import sql_run
from db_ops.lib import driver_warnings
from db_ops.lib.telegram_severity import classify_message
from db_ops.sql_tasks import runner
from tests.test_common_sql_run import _STATED, FakeConn, FakeCursor, _patch

from conftest import patch_sql_runner

WARNING_8153 = ("01003", "[01003] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Warning: Null "
                "value is eliminated by an aggregate or other SET operation. (8153) (SQLMoreResults)")
REAL_ERROR = ("42000", "[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Invalid object "
              "name 'dbo.nope'. (208) (SQLMoreResults)")


class DriverError(Exception):
    """Shaped like `pyodbc.Error`: `(sqlstate, message)`."""


class RaisingCursor(FakeCursor):
    """Raises ``error`` from the ``at``-th `nextset()` and is closed afterwards, as pyodbc is."""

    def __init__(self, result_sets, *, error, at=1):
        super().__init__(result_sets)
        self._error, self._at, self._calls, self._closed = error, at, 0, False

    def nextset(self):
        if self._closed:
            return False
        self._calls += 1
        if self._calls == self._at:
            self._closed = True
            raise DriverError(*self._error)
        return super().nextset()


FIRST_SET = ([("n",)], [[1]], -1)
SECOND_SET = ([("m",)], [[2]], -1)


# --------------------------------------------------------------------------- #
# The rule: every SQLSTATE the error carries is class 01
# --------------------------------------------------------------------------- #
def test_a_class_01_error_is_a_warning():
    assert "(8153)" in driver_warnings.warning_text(DriverError(*WARNING_8153))


def test_a_real_error_is_not_a_warning():
    assert driver_warnings.warning_text(DriverError(*REAL_ERROR)) is None


def test_a_warning_followed_by_an_error_is_not_a_warning():
    """pyodbc joins every diagnostic record into the message; the first SQLSTATE alone would let
    an error through as a warning because a warning happened to come first."""
    mixed = DriverError("01003", WARNING_8153[1] + "; " + REAL_ERROR[1])
    assert driver_warnings.sqlstates(mixed) == ["01003", "42000"]
    assert driver_warnings.warning_text(mixed) is None


def test_an_exception_with_no_sqlstate_is_not_a_warning():
    assert driver_warnings.warning_text(RuntimeError("connection reset")) is None


# --------------------------------------------------------------------------- #
# run-sql
# --------------------------------------------------------------------------- #
def test_the_reader_keeps_what_came_before_a_warning_and_records_it():
    cursor = RaisingCursor([FIRST_SET, SECOND_SET], error=WARNING_8153)
    cursor.execute("SELECT 1; SELECT 2;")
    warnings: list[str] = []

    sets, _, _ = sql_run.execute_capture(cursor, "SELECT 1; SELECT 2;", capture_all=True,
                                         warnings=warnings)

    assert [s["rows"] for s in sets] == [[[1]]]
    assert len(warnings) == 1 and "(8153)" in warnings[0]
    assert "could not be read" in warnings[0], "the warning also marks where the run stopped seeing"


def test_an_autocommit_run_past_a_warning_succeeds_and_says_so(monkeypatch):
    conn = FakeConn(RaisingCursor([FIRST_SET, SECOND_SET], error=WARNING_8153))
    _patch(monkeypatch, conn)

    result = sql_run.run_sql({**_STATED, "target": "ACME-x", "sql_text": "EXEC dbo.engine;", "autocommit": True})

    assert result["ok"] is True
    assert len(result["warnings"]) == 1
    assert conn.rolled_back is False, "every statement already committed as it ran"


def test_a_transaction_past_a_warning_is_rolled_back_with_the_reason(monkeypatch):
    """All or nothing cannot be checked past a warning - the driver hides even a later error - so a
    wrapping transaction is not committed, and the answer says why instead of the bare warning."""
    conn = FakeConn(RaisingCursor([FIRST_SET, SECOND_SET], error=WARNING_8153))
    _patch(monkeypatch, conn)

    with pytest.raises(sql_run.SqlRunError, match="rolled back") as raised:
        sql_run.run_sql({**_STATED, "target": "ACME-x", "sql_text": "EXEC dbo.engine;", "commit": True})

    assert "autocommit" in str(raised.value)
    assert conn.committed is False and conn.rolled_back is True


def test_a_real_error_still_fails_the_run(monkeypatch):
    conn = FakeConn(RaisingCursor([FIRST_SET, SECOND_SET], error=REAL_ERROR))
    _patch(monkeypatch, conn)

    with pytest.raises(sql_run.SqlRunError, match="Invalid object name"):
        sql_run.run_sql({**_STATED, "target": "ACME-x", "sql_text": "SELECT 1; SELECT * FROM dbo.nope;",
                         "autocommit": True})


def test_a_run_with_no_warning_answers_an_empty_list(monkeypatch):
    conn = FakeConn(FakeCursor([FIRST_SET]))
    _patch(monkeypatch, conn)

    assert sql_run.run_sql({**_STATED, "target": "ACME-x", "sql_text": "SELECT 1"})["warnings"] == []


# --------------------------------------------------------------------------- #
# The metrics reader
# --------------------------------------------------------------------------- #
def test_a_metric_past_a_warning_keeps_what_came_before_it(monkeypatch):
    """A metric reads through `run-sql`'s reader since 0.25.0 and never commits: the rows before the
    warning are its answer, and the warning does not fail it."""
    from db_ops.common import metric_batch

    conn = FakeConn(RaisingCursor([FIRST_SET, SECOND_SET], error=WARNING_8153))
    monkeypatch.setattr(metric_batch, "_connect", lambda *_a, **_k: conn)

    rows, truncated = metric_batch._execute({"db_type": "sqlserver"}, "", "EXEC dbo.metric;",
                                            timeout=5, max_rows=0)

    assert rows == [{"n": 1}] and truncated is False


# --------------------------------------------------------------------------- #
# The SQL task: done, at warning level, and the header says so
# --------------------------------------------------------------------------- #
def test_the_task_reads_the_warnings_run_sql_answers(monkeypatch):
    from test_sql_task_claims import sql_command, sql_target

    answer = {"result_sets": [], "affected_rows": 3, "warnings": [WARNING_8153[1]]}
    monkeypatch.setattr(runner.common_cli, "run_allowing_failure",
                        lambda command, request, **_kw: (True, answer, ""))

    result = runner.execute_sql(command=sql_command(), target=sql_target(), database={},
                                credential={}, password="", sql_text="EXEC dbo.engine;")

    assert result["warnings"] == [WARNING_8153[1]]


def test_a_task_that_finished_past_a_warning_is_done_at_warning_level(tmp_path, monkeypatch):
    from test_sql_task_claims import (RecordingSqlRunStore, inventory_and_credentials,
                                      sql_command, sql_target)

    data_dir = tmp_path / "data"
    (data_dir / "sql").mkdir(parents=True)
    (data_dir / "sql" / "engine.sql").write_text("EXEC dbo.engine;", encoding="utf-8")
    patch_sql_runner(monkeypatch, "execute_sql", lambda **_: {
        "row_count": 0, "result_sets": [], "warnings": [WARNING_8153[1]]})
    patch_sql_runner(monkeypatch, "log_event", lambda *args, **kwargs: None)
    sent = []
    patch_sql_runner(monkeypatch, "enqueue_sql_task_message", lambda **kwargs: sent.append(kwargs))
    store = RecordingSqlRunStore()
    inventory, credentials = inventory_and_credentials()

    assert runner.run_one_sql_task(
        store=store, data_dir=data_dir, telegram_groups={},
        command=sql_command(script_files=("sql/engine.sql",)), target=sql_target(),
        inventory=inventory, credentials=credentials, secrets={}, logger=None) is True

    final = store.updated[-1]
    assert final["status"] == "done" and final["level"] == "warning"
    assert "(8153)" in final["message"]
    done = [m for m in sent if m["status"] == "done"][-1]
    assert done["headline"] == "done with a warning"


def test_done_with_a_warning_reads_as_a_warning_not_a_success_or_a_failure():
    """The header is what the severity emoji is read from, centrally."""
    assert classify_message("[SQL] SQL task done with a warning\nsql_code: X") == "warning"
    assert classify_message("[SQL] SQL task done\nsql_code: X") == "success"
