"""An Oracle or PostgreSQL point in time is read in the source database's own clock (0.40.0, 1.385).

Oracle's ``UNTIL TIME`` is read against the times the source's archived logs carry, and PostgreSQL's
``recovery_target_time`` without an offset in the cluster's own ``timezone``. Every lab and drill ran on
UTC, and the restore took the database's clock to be UTC: from a source on +07 an Oracle restore would
have stopped seven hours off and said it was done - the database opens, and the missing afternoon is
found by someone looking for a row. The operator, 2026-11-16: *build it in 0.40.0* - ask the source
database its offset and read the moment by it; what cannot be read is refused, never assumed.

The moment is carried in UTC with its offset stated from then on: the chain chosen by it, PostgreSQL
honouring the offset, Oracle's ``UNTIL TIME`` turned back into the source's clock.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import restore_by_id as R
from dbabrain.backup_restore import source_clock
from dbabrain.lib.restore.moment import MomentError, utc_moment_text


# --------------------------------------------------------------------------- #
# The moment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text, offset, utc", [
    ("2026-11-16 15:00:00", 420, "2026-11-16 08:00:00 +00:00"),       # bare: the source's clock, +07
    ("2026-11-16 15:00:00 +07:00", None, "2026-11-16 08:00:00 +00:00"),  # stated: no clock needed
    ("2026-11-16 15:00:00 +07:00", -300, "2026-11-16 08:00:00 +00:00"),  # stated wins over the clock
    ("2026-11-16 08:00:00", 0, "2026-11-16 08:00:00 +00:00"),
    ("2026-11-16 01:30:00", 330, "2026-11-15 20:00:00 +00:00"),       # a half-hour zone, across a day
])
def test_a_moment_is_carried_in_utc_with_its_offset_stated(text, offset, utc):
    assert utc_moment_text(text, source_offset_minutes=offset) == utc


def test_a_bare_moment_whose_clock_is_unknown_is_refused_not_taken_as_utc():
    with pytest.raises(MomentError, match="state it with its offset|State it with its offset"):
        utc_moment_text("2026-11-16 15:00:00", source_offset_minutes=None)


@pytest.mark.parametrize("text, minutes", [
    ("+07:00", 420), ("+07", 420), ("-03:30", -210), ("+0530", 330), ("+00:00", 0), (" -05 ", -300),
    ("UTC", None), ("+25:00", None), ("", None), (None, None),
])
def test_an_offset_is_read_as_the_database_says_it(text, minutes):
    assert source_clock.parse_offset(text) == minutes


# --------------------------------------------------------------------------- #
# Asking the source, and what a backup recorded
# --------------------------------------------------------------------------- #
@pytest.fixture
def database(monkeypatch):
    asked = []
    answer = {"rows": [["+07:00"]]}

    def run(command, request, **_k):
        asked.append((command, request))
        if isinstance(answer, Exception):
            raise answer
        return answer

    from dbabrain.lib.data_sources import request_fill
    from dbabrain.transport import common_cli

    monkeypatch.setattr(request_fill, "sql_connection", lambda target, **_k: {"db_type": "oracle", "host": target})
    monkeypatch.setattr(common_cli, "run", run)
    return SimpleNamespace(asked=asked, set=lambda value: answer.clear() or answer.update(value))


@pytest.mark.real_source_clock
def test_the_source_is_asked_read_only_by_its_own_login(database):
    assert source_clock.ask("ORA-SRC", db_type="oracle") == (420, "")
    command, request = database.asked[0]
    assert command == "run-sql" and request["connection"] == {"db_type": "oracle", "host": "ORA-SRC"}
    assert "SYSTIMESTAMP" in request["sql_text"] and request["max_rows"] == 1
    source_clock.ask("PG-SRC", db_type="postgres")
    assert "now()" in database.asked[1][1]["sql_text"]


@pytest.mark.real_source_clock
def test_a_source_that_does_not_answer_says_why(monkeypatch):
    from dbabrain.lib.data_sources import request_fill

    def gone(target, **_k):
        raise request_fill.RequestFillError("no credential")

    monkeypatch.setattr(request_fill, "sql_connection", gone)
    clock, why = source_clock.ask("ORA-SRC", db_type="oracle")
    assert clock is None and "ORA-SRC's clock not read" in why and "no credential" in why
    assert source_clock.ask("SQL-1", db_type="sqlserver")[0] is None


@pytest.mark.real_source_clock
def test_an_answer_that_is_not_an_offset_is_said(database):
    database.set({"rows": [["DBTIMEZONE"]]})
    clock, why = source_clock.ask("ORA-SRC", db_type="oracle")
    assert clock is None and "'DBTIMEZONE'" in why


def test_the_newest_recorded_clock_stands_in(monkeypatch):
    rows = [{"rman_chain_json": json.dumps({"pieces": ["a"], "clock_offset_minutes": 0})},
            {"rman_chain_json": json.dumps({"pieces": ["b"], "clock_offset_minutes": 420})},
            {"rman_chain_json": json.dumps({"error": "ssh timed out"})}]
    catalog = SimpleNamespace(oracle_chain_rows=lambda server_id: (rows, []))
    assert source_clock.recorded(catalog, "ORA-SRC") == 420
    assert source_clock.recorded(None, "ORA-SRC") is None


# --------------------------------------------------------------------------- #
# The restore reads the moment once, before a file is copied
# --------------------------------------------------------------------------- #
def _job():
    return SimpleNamespace(restore_id="ORA_TO_LAB", server_id="ORA-SRC")


@pytest.fixture
def clock(monkeypatch):
    state = {"asked": (None, "ORA-SRC's clock not read - TimeoutError"), "recorded": None}
    monkeypatch.setattr(source_clock, "ask", lambda server_id, **_k: state["asked"])
    monkeypatch.setattr(source_clock, "recorded", lambda catalog, server_id: state["recorded"])
    from dbabrain.backup_restore import backup

    monkeypatch.setattr(backup, "_load_secrets", lambda **_k: {})
    return state


def _utc(engine, moment):
    return R._moment_in_utc(_job(), moment, engine=engine, catalog=None, data_dir=None, key=None, key_base64=None)


def test_a_source_that_answers_gives_the_clock(clock):
    clock["asked"] = (420, "")
    assert _utc("oracle", "2026-11-16 15:00:00") == ("2026-11-16 08:00:00 +00:00", 420)


def test_an_oracle_source_gone_is_read_by_the_clock_its_backup_recorded(clock):
    clock["recorded"] = 420
    assert _utc("oracle", "2026-11-16 15:00:00") == ("2026-11-16 08:00:00 +00:00", 420)


def test_an_oracle_restore_with_no_clock_at_all_is_refused_even_with_a_stated_offset(clock):
    with pytest.raises(R.RestoreByIdError, match="UNTIL TIME is read in the source's clock"):
        _utc("oracle", "2026-11-16 15:00:00 +07:00")


def test_postgresql_needs_no_clock_for_a_moment_that_states_its_offset(clock):
    assert _utc("postgresql", "2026-11-16 15:00:00 +07:00") == ("2026-11-16 08:00:00 +00:00", None)


def test_a_bare_moment_with_no_clock_is_refused_with_why(clock):
    with pytest.raises(R.RestoreByIdError, match="ORA_TO_LAB: .*State it with its offset.*TimeoutError"):
        _utc("postgresql", "2026-11-16 15:00:00")


# --------------------------------------------------------------------------- #
# Oracle's UNTIL TIME, back in the source's clock
# --------------------------------------------------------------------------- #
def _oracle_job():
    return SimpleNamespace(restore_id="ORA_TO_LAB", target_visible_dir="/stage", env={"ORACLE_SID": "FREE"},
                           env_secrets={}, overwrite_existing=True, checkdb=True)


@pytest.mark.parametrize("offset, stopat", [(420, "2026-11-16 15:00:00"), (0, "2026-11-16 08:00:00"),
                                            (-300, "2026-11-16 03:00:00")])
def test_until_time_is_the_moment_in_the_sources_clock(monkeypatch, offset, stopat):
    monkeypatch.setattr(R, "_locked", lambda: False)
    steps = R._plan_oracle(_oracle_job(), {}, point_in_time="2026-11-16 08:00:00 +00:00", host={"host": "t"},
                           data_dir=None, clock_offset_minutes=offset)
    assert steps[0]["request"]["stopat"] == stopat


def test_the_restore_hands_the_clock_to_oracles_plan_only(monkeypatch):
    seen = {}

    def planner(job, secrets, **kwargs):
        seen.update(kwargs)
        return []

    prep = SimpleNamespace(engine="oracle", planner=planner, job=None, secrets={}, point_in_time="p", host={},
                           data_dir=None, source_clock_offset_minutes=420)
    R.plan_chain(prep)
    assert seen["clock_offset_minutes"] == 420
    seen.clear()
    R.plan_chain(SimpleNamespace(**{**vars(prep), "engine": "postgresql"}))
    assert "clock_offset_minutes" not in seen
