"""A number nobody measured is shown as missing, never as zero.

The fleet page was built around SQL Server's metrics, and for every other engine it filled the gaps
with `or 0`. On 2026-10-08 that read, for two production Oracle 8i instances holding several hundred
sessions each: **PLE 0** and **0 sessions**.

- *PLE 0* was a number nobody collected. Page life expectancy is SQL Server's metric; Oracle and
  PostgreSQL have none. The page badges a PLE under 500 red - so a healthy Oracle server carried
  SQL Server's memory emergency.
- *0 sessions* was a number that had been collected and not read. Every engine's
  `INSTANCE_CONNECTIONS` counts sessions, but the Oracle 8i variant writes item `session_count`
  where the page asked for `server`. And where Oracle did write `server`, the page read the
  message's `active_sessions` - on Oracle the sessions whose status is ACTIVE, a handful out of
  hundreds - while reading the same key on SQL Server as the total.

A zero is a reading. So `None` is what a missing number is, the page prints a dash for it and badges
it neutral, and the summary prints `n/a` - and a session count is the row's value, which means the
same thing on every engine.
"""

from __future__ import annotations

import pytest

from db_ops.lib.inventory_render import _measured
from db_ops.reports.inventory_health import build_performance_health


def _row(value, message=""):
    return {"metric_value": value, "message": message, "status": "OK",
            "collected_at": "2026-10-09T04:00:00Z"}


SQL_SERVER = {
    ("INSTANCE_CONNECTIONS", "server"): _row(
        "212", "active_sessions=212, running_requests=3, blocked_sessions=0"),
    ("PAGE_LIFE_EXPECTANCY", "page_life_expectancy"): _row("5400"),
    ("LOCK_DEADLOCK_RECENT", "deadlock"): _row("0"),
}
ORACLE = {
    ("INSTANCE_CONNECTIONS", "server"): _row(
        "401", "active_sessions=7, user_sessions=401, blocked_sessions=0"),
}
ORACLE_8I = {
    ("INSTANCE_CONNECTIONS", "session_count"): _row("514", "total sessions=514"),
    ("INSTANCE_CONNECTIONS", "process_count"): _row("520", "total processes=520"),
}
POSTGRESQL = {
    ("INSTANCE_CONNECTIONS", "server"): _row(
        "18", "active_connections=18, max_connections=100, running_queries=1, "
              "idle_in_transaction=0"),
}


@pytest.mark.parametrize("code_map", [ORACLE, ORACLE_8I, POSTGRESQL, {}],
                         ids=["oracle", "oracle 8i", "postgresql", "nothing collected"])
def test_an_engine_without_page_life_expectancy_has_none_not_zero(code_map):
    """The page badges a PLE under 500 red; zero for an engine that has no PLE is a false alarm."""
    health = build_performance_health(code_map)

    assert health["page_life_expectancy"] is None
    assert health["deadlock_count_24h"] is None


def test_sql_server_keeps_its_readings_and_a_measured_zero_stays_a_zero():
    health = build_performance_health(SQL_SERVER)

    assert health["page_life_expectancy"] == 5400
    assert health["deadlock_count_24h"] == 0, "no deadlocks in 24 h is a reading, not a gap"


def test_an_8i_instance_shows_the_sessions_it_holds():
    """It wrote `session_count`; the page asked for `server` and printed 0."""
    assert build_performance_health(ORACLE_8I)["active_session_count"] == 514


def test_an_oracle_session_count_is_every_session_not_only_the_active_ones():
    """`active_sessions` is SQL Server's total and Oracle's ACTIVE subset: the value is the count."""
    assert build_performance_health(ORACLE)["active_session_count"] == 401


def test_every_engine_reports_the_same_thing_under_sessions():
    counts = {name: build_performance_health(code_map)["active_session_count"]
              for name, code_map in (("sqlserver", SQL_SERVER), ("postgresql", POSTGRESQL))}

    assert counts == {"sqlserver": 212, "postgresql": 18}


def test_no_session_row_is_none_not_zero():
    assert build_performance_health({})["active_session_count"] is None


def test_a_row_without_a_value_falls_back_to_the_message():
    """Where the count first lived, before the value carried it."""
    code_map = {("INSTANCE_CONNECTIONS", "server"): _row("", "active_sessions=33")}

    assert build_performance_health(code_map)["active_session_count"] == 33


def test_the_summary_prints_a_gap_as_a_gap():
    assert (_measured(None), _measured(0), _measured(5400)) == ("n/a", "0", "5400")
