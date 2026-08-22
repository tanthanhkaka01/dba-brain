"""INSTANCE_STATUS end to end, against a simulated database, in the three outcomes that matter.

This is the metric the whole failure-severity split exists for, so it is exercised through the
real collector path — the catalog entry the estate actually ships, the real variant resolution,
the real SQL file on disk — with only the driver replaced. What changes between the three tests is
exactly what changes in production: the connection is refused, or it is opened and the query
fails, or everything works.

  connect OK  + query OK     -> the row the metric returned (OK), no error_type
  connect OK  + query fails  -> execution_error_severity  = CRITICAL, error_type QUERY_FAILED
  connect fails              -> connection_error_severity = CRITICAL, error_type CONNECT_FAILED

The last two used to be one flat WARNING each, which is how an unreachable production instance
could be reported at the same level as a stale statistics scan. See
tests/test_metric_error_severity.py for the per-phase grading rules themselves.
"""

import pytest

from db_ops.metrics.collector import _collect_one_metric
from conftest import shipped_config
from db_ops.metrics.definitions import load_metric_definitions
from db_ops.metrics.models import MetricTarget


COLLECTED_AT = "2026-08-14T09:00:00Z"


class _FakeCursor:
    """Just enough of a DB-API cursor for ``execute_cursor_batches``."""

    def __init__(self, *, rows, columns, execute_error=None):
        self._rows = list(rows)
        self._columns = list(columns)
        self._execute_error = execute_error
        self.description = None
        self.rowcount = -1

    def execute(self, *_args, **_kwargs):
        if self._execute_error is not None:
            raise self._execute_error
        self.description = [(name,) for name in self._columns]

    def fetchmany(self, size):
        taken, self._rows = self._rows[:size], self._rows[size:]
        return taken

    def nextset(self):
        return False

    def close(self):
        return None


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _instance_status_definition():
    definitions = {item.metric_code: item for item in load_metric_definitions(shipped_config("metric_definitions.json"))}
    return definitions["INSTANCE_STATUS"]


def _target():
    return MetricTarget(
        target_id="ACME-192-0-2-250/sqlserver/SALESDB-PROD",
        server_id="ACME-192-0-2-250",
        ip="192.0.2.250",
        db_type="sqlserver",
        db_name="SALESDB-PROD",
        credential_name="sqlserver_db_ops",
        port=1433,
        credential={"username": "db_ops", "password": "not-a-real-password"},
    )


def _collect(monkeypatch, *, connect_error=None, execute_error=None, rows=()):
    cursor = _FakeCursor(
        rows=[tuple(row.values()) for row in rows],
        columns=list(rows[0].keys()) if rows else [],
        execute_error=execute_error,
    )
    connection = _FakeConnection(cursor)

    def fake_connect_engine(**_kwargs):
        if connect_error is not None:
            raise connect_error
        return connection

    monkeypatch.setattr("db_ops.metrics.executor.db_connect.connect_engine", fake_connect_engine)
    results = _collect_one_metric(
        metric=_instance_status_definition(),
        target=_target(),
        importance=5,
        secrets={},
        collected_at=COLLECTED_AT,
    )
    return results, connection


def test_instance_status_that_connects_and_runs_reports_what_the_instance_answered(monkeypatch):
    results, connection = _collect(
        monkeypatch,
        rows=[
            {
                "metric_item": "instance",
                "metric_value": "ONLINE",
                "metric_unit": "state",
                "status": "OK",
                "message": "SQL Server is up; uptime 41 days.",
            }
        ],
    )

    assert [item.status for item in results] == ["OK"]
    assert results[0].metric_value == "ONLINE"
    # Nothing failed, so nothing may be filed as an error — an OK row with an error_type is how
    # the metric coverage panel starts reporting healthy metrics as broken.
    assert results[0].error_type is None
    assert connection.closed


def test_instance_status_whose_query_fails_on_an_open_connection_is_critical(monkeypatch):
    """The instance answered the login and then could not answer the check — still CRITICAL for
    this metric, because either way nobody can say the instance is healthy."""
    results, connection = _collect(
        monkeypatch,
        execute_error=RuntimeError("Msg 297: The user does not have permission to perform this action."),
    )

    assert [item.status for item in results] == ["CRITICAL"]
    assert "SQL execution failed" in results[0].message
    assert results[0].error_type == "QUERY_FAILED"
    # The connection is closed even on the failing path; a metric that leaks sessions on every
    # error would exhaust the instance it is watching, one collect pass at a time.
    assert connection.closed


def test_instance_status_that_cannot_connect_at_all_is_critical(monkeypatch):
    """The case the split exists for: an unreachable production instance is an outage, and used
    to be filed as an ordinary WARNING alongside a stale statistics scan."""
    results, _ = _collect(
        monkeypatch,
        connect_error=RuntimeError("[Microsoft][ODBC Driver 17 for SQL Server]Login timeout expired"),
    )

    assert [item.status for item in results] == ["CRITICAL"]
    assert "Connection failed" in results[0].message
    assert results[0].error_type == "CONNECT_FAILED"


@pytest.mark.parametrize(
    "failure",
    [
        {"connect_error": RuntimeError("Login timeout expired")},
        {"execute_error": RuntimeError("Invalid object name 'msdb.dbo.backupset'.")},
    ],
)
def test_the_same_two_failures_on_a_non_availability_metric_stay_warnings(monkeypatch, failure):
    """Proof the severity is the metric's own and not a new global rule: BACKUP_AGE breaking the
    same two ways is a warning, because a backup check that cannot run is not an outage."""
    definitions = {item.metric_code: item for item in load_metric_definitions(shipped_config("metric_definitions.json"))}
    backup_age = definitions["BACKUP_AGE"]

    cursor = _FakeCursor(rows=[], columns=[], execute_error=failure.get("execute_error"))

    def fake_connect_engine(**_kwargs):
        if failure.get("connect_error") is not None:
            raise failure["connect_error"]
        return _FakeConnection(cursor)

    monkeypatch.setattr("db_ops.metrics.executor.db_connect.connect_engine", fake_connect_engine)
    results = _collect_one_metric(
        metric=backup_age,
        target=_target(),
        importance=4,
        secrets={},
        collected_at=COLLECTED_AT,
    )

    assert [item.status for item in results] == ["WARNING"]
