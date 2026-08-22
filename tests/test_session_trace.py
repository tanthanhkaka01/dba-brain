"""Tracing an open transaction back to the person, on an estate where every session looks alike.

Through a middle tier — Dynamics AX here — SQL Server sees one shape for everybody:
`login=salesdbadmin, host=ACMEAOS04, program=salesOnline`. The service account, the app server, the tier.
An alert built from those names an incident and nobody to talk to about it.

AX writes the real caller into `sys.dm_exec_sessions.context_info`, and that is the whole trick.
What these tests pin is the handful of places the trick can quietly stop working: the sentinel that
lets a caller ask for "everything", the parse refusing to invent a user id out of a field some other
application wrote, and the filters actually reaching the SQL.
"""

from __future__ import annotations

import pytest

from db_ops.common import session_trace


class _Recorder:
    """Stands in for `sql_run.run_sql` and keeps the request it was handed."""

    def __init__(self, rows=None, columns=None):
        self.requests = []
        self.rows = rows or []
        self.columns = columns or []

    def __call__(self, request):
        self.requests.append(dict(request))
        return {"server_id": "S1", "database": "SALESDB",
                "columns": list(self.columns), "rows": list(self.rows)}


def _patch(monkeypatch, recorder):
    monkeypatch.setattr(session_trace.sql_run, "run_sql", recorder)


def test_session_id_zero_means_every_session_not_spid_zero(monkeypatch):
    """A caller that must pass something needs a way to say "no filter".

    The Telegram command takes one argument; before this, sending 0 filtered on SPID 0, matched
    nothing, and reported "no long transactions" — the most reassuring possible way to be wrong.
    SPID 0 is never a user session, so it is free to mean "all".
    """
    recorder = _Recorder()
    _patch(monkeypatch, recorder)

    session_trace.trace_sessions({"target": "S1", "session_id": 0, "min_tran_seconds": 300})

    sql = recorder.requests[0]["sql"]
    # The filter form specifically — `s.session_id =` also appears in the joins.
    assert "AND s.session_id =" not in sql
    assert "DATEDIFF(second, at.transaction_begin_time, GETDATE()) >= 300" in sql


@pytest.mark.parametrize("value", [None, "", "0", 0])
def test_the_same_holds_for_every_spelling_of_absent(monkeypatch, value):
    recorder = _Recorder()
    _patch(monkeypatch, recorder)

    session_trace.trace_sessions({"target": "S1", "session_id": value})

    assert "AND s.session_id =" not in recorder.requests[0]["sql"]


def test_a_real_session_id_filters_to_it(monkeypatch):
    recorder = _Recorder()
    _patch(monkeypatch, recorder)

    session_trace.trace_sessions({"target": "S1", "session_id": 505})

    sql = recorder.requests[0]["sql"]
    assert "AND s.session_id = 505" in sql
    assert "DATEDIFF" not in sql.split("WHERE")[1], "an explicit SPID is not also age-filtered"


def test_a_non_numeric_session_id_is_refused_rather_than_interpolated(monkeypatch):
    """It is interpolated into SQL, so it is validated before it gets there."""
    _patch(monkeypatch, _Recorder())

    with pytest.raises(session_trace.SessionTraceError, match="session_id"):
        session_trace.trace_sessions({"target": "S1", "session_id": "1; DROP TABLE x"})


def test_blocking_only_asks_for_the_sessions_that_are_costing_something(monkeypatch):
    recorder = _Recorder()
    _patch(monkeypatch, recorder)

    session_trace.trace_sessions({"target": "S1", "blocking_only": True})

    assert "blocking_session_id = s.session_id" in recorder.requests[0]["sql"]


def test_the_ax_context_is_split_into_user_session_and_client_type():
    parsed = session_trace._parse_app_context("ACMECEN01.PU 4654 CLIENT - read-only 0")

    assert parsed["app_user"] == "ACMECEN01.PU"
    assert parsed["app_session"] == "4654"
    assert parsed["app_client_type"] == "CLIENT"
    assert parsed["app_context_raw"] == "ACMECEN01.PU 4654 CLIENT - read-only 0"


def test_context_info_written_by_something_else_yields_no_user():
    """`context_info` is a general-purpose 128 bytes any application may use. Reporting a wrong
    user id is worse than reporting none — someone would go and ask that person about it."""
    parsed = session_trace._parse_app_context("12345")

    assert parsed["app_user"] == ""
    assert parsed["app_context_raw"] == "12345"


def test_an_empty_context_info_is_not_an_error():
    parsed = session_trace._parse_app_context("")

    assert parsed == {"app_user": "", "app_session": "", "app_client_type": "",
                      "app_context_raw": ""}


def test_a_missing_target_is_refused_before_anything_connects():
    with pytest.raises(session_trace.SessionTraceError, match="target"):
        session_trace.trace_sessions({"session_id": 505})


def test_rows_are_shaped_with_the_context_decoded_and_the_name_resolved(monkeypatch):
    columns = ["session_id", "app_context", "tran_age_seconds"]
    recorder = _Recorder(columns=columns, rows=[[505, "ACMECEN01.PU 4654 CLIENT - read-only 0", 23331]])
    _patch(monkeypatch, recorder)
    monkeypatch.setattr(session_trace, "_user_names",
                        lambda **_: {"ACMECEN01.PU": "ORDER PROCESSING PU&NON"})

    result = session_trace.trace_sessions({"target": "S1"})

    assert result["transaction_count"] == 1
    assert result["session_count"] == 1
    session = result["sessions"][0]
    assert session["app_user"] == "ACMECEN01.PU"
    assert session["app_user_name"] == "ORDER PROCESSING PU&NON"
    assert "app_context" not in session, "the raw column is replaced by the parsed fields"


def test_a_database_without_userinfo_costs_the_display_name_and_nothing_else(monkeypatch):
    """`USERINFO` is a Dynamics AX table. On any other database the lookup fails, and the trace
    must still return every session — the user id from context_info is the useful part."""
    columns = ["session_id", "app_context"]
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if "USERINFO" in str(request.get("sql", "")):
            raise RuntimeError("Invalid object name 'USERINFO'.")
        return {"server_id": "S1", "database": "db", "columns": columns,
                "rows": [[505, "SOMEAPP 12 CLIENT"]]}

    _patch(monkeypatch, flaky)

    result = session_trace.trace_sessions({"target": "S1"})

    assert result["transaction_count"] == 1
    assert result["sessions"][0]["app_user"] == "SOMEAPP"
    assert result["sessions"][0]["app_user_name"] == ""


def test_a_session_with_nested_transactions_is_counted_once_as_a_session(monkeypatch):
    """SPID 135 did this live: two rows from dm_tran_session_transactions, one session. Reporting
    "2 sessions" would overstate how many people are involved in an incident."""
    columns = ["session_id", "app_context"]
    recorder = _Recorder(columns=columns,
                         rows=[[135, "ACMECEN01.AD 4072 CLIENT"], [135, "ACMECEN01.AD 4072 CLIENT"]])
    _patch(monkeypatch, recorder)
    monkeypatch.setattr(session_trace, "_user_names", lambda **_: {})

    result = session_trace.trace_sessions({"target": "S1"})

    assert result["transaction_count"] == 2
    assert result["session_count"] == 1


def test_describe_names_the_person_before_the_numbers():
    line = session_trace.describe({
        "session_id": 505, "app_user": "ACMECEN01.PU", "app_user_name": "ORDER PROCESSING PU&NON",
        "app_session": "4654", "tran_age_seconds": 23331,
        "transaction_begin_time": "2026-08-11 08:35:47", "log_bytes_used": 1875268,
        "locks_held": 3931, "session_status": "sleeping", "request_status": None,
        "client_host": "ACMEAOS04", "client_net_address": "192.0.2.119",
        "blocked_session_ids": "52,1266",
    })

    assert "app_user=ACMECEN01.PU (ORDER PROCESSING PU&NON)" in line
    assert "tran_age=388m" in line
    assert "blocking=52,1266" in line
