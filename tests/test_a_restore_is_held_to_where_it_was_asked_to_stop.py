"""A restore that opens and checks clean is still held to where it was asked to stop (0.39.0, 1.362).

The operator, 2026-11-13 (translated): *what matters now is that restore runs reliably - it must be
verified carefully.* A restore had just shown why "the databases open and CHECKDB is clean" is not
that: handed its moment in the wrong clock it stopped seven hours early, came up ONLINE, passed every
check and said done (1.363). Nothing asked the one witness that knew - the target itself.

So ``verify-restore`` now reads, for each SQL Server database, what the target's own ``msdb``
recorded of its last restore (``restored_to``: the STOPAT it was given, and when the last backup
applied ended), and the restore compares that with what its plan asked:

* a moment carried by a STOPAT - the target's ``stop_at`` is that moment;
* anything else (the newest backup, a moment past the newest log, a chain with no log) - the last
  backup the target applied ended when the last file of the plan's chain ended.

A difference fails the restore, by name; a record that could not be read is said and fails nothing.
What was read is kept on the run (``restore_runs.landed_json``) and said in the END message.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from conftest import landing_of

from dbabrain.backup_restore import restore_by_id, restore_script
from dbabrain.common import verifyrestore
from dbabrain.db.backup_catalog import BackupCatalogStore

# Its restore targets are lab machines the tests do not list in an inventory; a target whose
# environment nothing states is refused since 0.39.0 (D-39.6) - see tests/conftest.py.
pytestmark = pytest.mark.usefixtures("restore_targets_are_lab")


def _expect(**over):
    expect = {"database_name": "APP", "source_database": "APP", "moment": "", "stopat": False,
              "chain_ends": "2026-11-13 11:09:15", "files": 5}
    expect.update(over)
    return expect


def _row(**restored_to):
    return {"database_name": "APP", "state": "ONLINE", "ok": True, "restored_to": restored_to}


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #
def test_a_stopat_is_held_to_the_targets_stop_at():
    landed, off = restore_by_id.where_it_stopped(
        [_expect(moment="2026-11-13 08:45:22", stopat=True)],
        [_row(stop_at="2026-11-13 08:45:22", last_backup_finished="2026-11-13 08:54:10", last_backup_type="log")])

    assert off == []
    assert landed[0]["verdict"] == "stopped at the moment asked" and landed[0]["by_stopat"] is True
    assert landed[0]["stop_at"] == "2026-11-13 08:45:22" and landed[0]["state"] == "ONLINE"


def test_a_restore_that_stopped_somewhere_else_is_named():
    """1.363's shape: asked 11:17 in the source's clock, stopped at the 04:17 it was handed."""
    landed, off = restore_by_id.where_it_stopped(
        [_expect(moment="2026-11-13 11:17:29", stopat=True)],
        [_row(stop_at="2026-11-13 04:17:29", last_backup_finished="2026-11-13 04:24:01")])

    assert landed[0]["verdict"] == "NOT at the moment asked"
    assert off == ["APP: asked to stop at 2026-11-13 11:17:29, the target recorded 2026-11-13 04:17:29"]


def test_a_stopat_the_target_never_recorded_is_named_too():
    _, off = restore_by_id.where_it_stopped(
        [_expect(moment="2026-11-13 08:45:22", stopat=True)],
        [_row(stop_at=None, last_backup_finished="2026-11-13 08:54:10")])

    assert off == ["APP: asked to stop at 2026-11-13 08:45:22, the target recorded no stop point"]


def test_the_newest_backup_is_held_to_the_end_of_the_chain():
    landed, off = restore_by_id.where_it_stopped(
        [_expect()], [_row(stop_at=None, last_backup_finished="2026-11-13 11:09:15", last_backup_type="log")])

    assert off == [] and landed[0]["verdict"] == "restored to the end of its chain"
    assert landed[0]["asked"] == "the newest backup"


def test_a_last_log_that_never_went_in_is_named():
    landed, off = restore_by_id.where_it_stopped(
        [_expect()], [_row(stop_at=None, last_backup_finished="2026-11-13 10:54:15")])

    assert landed[0]["verdict"] == "NOT at the end of its chain"
    assert off == ["APP: its chain ends 2026-11-13 11:09:15, the last backup the target applied ended "
                   "2026-11-13 10:54:15"]


@pytest.mark.parametrize("listed, recorded", [
    ("2026-11-13T11:09:15", "2026-11-13 11:09:15"),
    ("2026-11-13T11:09:15Z", "2026-11-13 11:09:15"),
    ("2026-11-13 11:09:15.0000000", "2026-11-13 11:09:15"),
])
def test_the_two_sides_are_compared_as_moments_not_as_text(listed, recorded):
    _, off = restore_by_id.where_it_stopped([_expect(chain_ends=listed)],
                                            [_row(last_backup_finished=recorded)])

    assert off == []


@pytest.mark.parametrize("rows", [[], [{"database_name": "APP", "state": "ONLINE"}], [_row()],
                                  [_row(stop_at=None, last_backup_finished=None)]])
def test_a_record_that_could_not_be_read_is_said_and_fails_nothing(rows):
    landed, off = restore_by_id.where_it_stopped([_expect(moment="2026-11-13 08:45:22", stopat=True)], rows)

    assert off == [] and landed[0]["verdict"] == "not read"


def test_a_database_restored_under_another_name_is_read_under_that_name():
    landed, off = restore_by_id.where_it_stopped(
        [_expect(database_name="APP_COPY", source_database="APP")],
        [{"database_name": "APP_COPY", "state": "ONLINE",
          "restored_to": {"last_backup_finished": "2026-11-13 11:09:15"}}])

    assert off == [] and landed[0]["source_database"] == "APP"
    assert restore_by_id.landed_said(landed) == (
        "APP as APP_COPY: last backup applied ended 2026-11-13 11:09:15 (restored to the end of its chain)")


def test_the_end_message_says_where_each_database_stopped():
    landed, _ = restore_by_id.where_it_stopped(
        [_expect(moment="2026-11-13 08:45:22", stopat=True)],
        [_row(stop_at="2026-11-13 08:45:22", last_backup_finished="2026-11-13 08:54:10")])

    assert restore_by_id.landed_said(landed) == "APP: stopped at 2026-11-13 08:45:22 (stopped at the moment asked)"


# --------------------------------------------------------------------------- #
# The plan says what it asked
# --------------------------------------------------------------------------- #
FULL = {"path": "/s/APP/FULL/APP_FULL_20261112_043500Z.bak", "database_name": "APP", "kind": "full",
        "finished_at": "2026-11-12 04:35:50"}
LOGS = [{"path": f"/s/APP/LOG/APP_LOG_20261112_0{hour}0000Z.trn", "database_name": "APP", "kind": "log",
         "finished_at": f"2026-11-12 0{hour}:00:05"} for hour in (5, 6, 7)]


def _plan(monkeypatch, moment, logs=LOGS):
    def listed(request):
        kind = request["kinds"][0]
        files = [FULL] if kind == "full" else logs if kind == "log" else []
        return {"files": files, "newest_finished_at": "2026-11-12 04:35:50" if files else None}

    monkeypatch.setattr(restore_by_id, "_list_backup_files", listed)
    monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/s")
    monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, data_dir=None: 1433)
    job = SimpleNamespace(restore_id="APP_TO_LAB", env={"MSSQL_USER": "dba"}, env_secrets={},
                          landing=landing_of("APP"), overwrite_existing=False)
    steps = restore_by_id._plan_sqlserver(job, {}, point_in_time=moment, host={"host": "192.0.2.40"}, data_dir=None)
    return next(step["expect"] for step in steps if step["op"] == "expect")


def test_the_plan_states_a_stopat_and_the_moment_as_the_engine_was_handed_it(monkeypatch):
    expect = _plan(monkeypatch, "2026-11-12 05:30:00 +00:00")

    # In the SOURCE's clock (1.363) - these files are stamped in UTC and end in it, so an offset of none.
    assert expect["stopat"] is True and expect["moment"] == "2026-11-12 05:30:00"
    assert expect["chain_ends"] == "2026-11-12 06:00:05", "the first log that reaches the moment"
    assert expect["files"] == 3


def test_a_moment_past_the_newest_log_is_held_to_the_chains_end_not_to_a_stopat(monkeypatch):
    expect = _plan(monkeypatch, "2026-11-13 04:05:00 +00:00")

    assert expect["stopat"] is False and expect["chain_ends"] == "2026-11-12 07:00:05"


def test_the_newest_backup_and_a_chain_with_no_log_end_at_their_last_file(monkeypatch):
    assert _plan(monkeypatch, "")["chain_ends"] == "2026-11-12 07:00:05"
    only_full = _plan(monkeypatch, "", logs=[])
    assert only_full["chain_ends"] == "2026-11-12 04:35:50" and only_full["stopat"] is False


# --------------------------------------------------------------------------- #
# verify-restore reads the target's own record
# --------------------------------------------------------------------------- #
def test_the_targets_record_is_its_newest_restore_of_that_database(monkeypatch):
    from dbabrain.common import sql_run

    asked = []

    def rows(cursor, sql, **kw):
        asked.append(sql)
        return [{"stop_at": "2026-11-13 08:45:22", "last_backup_finished": "2026-11-13 08:54:10",
                 "last_backup_type": "L", "restored_at": "2026-11-13 04:21:07"}]

    monkeypatch.setattr(sql_run, "query_rows", rows)

    found = verifyrestore._sqlserver_restored_to(object(), "O'Brien")

    assert found == {"stop_at": "2026-11-13 08:45:22", "last_backup_finished": "2026-11-13 08:54:10",
                     "last_backup_type": "log", "restored_at": "2026-11-13 04:21:07"}
    assert "msdb.dbo.restorehistory" in asked[0] and "ORDER BY rh.restore_history_id DESC" in asked[0]
    assert "destination_database_name = N'O''Brien'" in asked[0], "a quote in the name is doubled"


def test_a_database_never_restored_there_or_an_msdb_that_will_not_answer_has_no_record(monkeypatch):
    from dbabrain.common import sql_run

    monkeypatch.setattr(sql_run, "query_rows", lambda cursor, sql, **kw: [])
    assert verifyrestore._sqlserver_restored_to(object(), "APP") is None

    def refuses(cursor, sql, **kw):
        raise RuntimeError("The SELECT permission was denied on the object 'restorehistory'")

    monkeypatch.setattr(sql_run, "query_rows", refuses)
    assert verifyrestore._sqlserver_restored_to(object(), "APP") is None


# --------------------------------------------------------------------------- #
# The restore: a difference fails it, and what was read is kept
# --------------------------------------------------------------------------- #
@pytest.fixture
def restored(monkeypatch, tmp_path):
    """One in-place SQL Server restore through ``restore_by_id``; the target records ``recorded``."""
    catalog = BackupCatalogStore(tmp_path / "catalog.sqlite")
    catalog.initialize()

    def run(recorded: dict):
        job = SimpleNamespace(restore_id="R", db_type="sqlserver", server_id="SRC", target_server_id="TGT",
                              env_secrets={}, is_remote=False, label="R (sqlserver)", landing=landing_of("APP"),
                              overwrite_existing=False, integrity=None, tier=None, target_backup_dir="/s",
                              server_metadata=SimpleNamespace(enabled=False))
        monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])
        monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "h"})
        monkeypatch.setattr(restore_by_id, "assert_target_is_not_source", lambda *a, **k: None)
        monkeypatch.setitem(restore_by_id._PLANNERS, "sqlserver", lambda *a, **k: [
            {"op": "restore-full", "source_database": "APP", "chain": [FULL],
             "request": {"database_name": "APP", "backup_path": FULL["path"], "with_recovery": True}},
            {"op": "expect", "expect": _expect(chain_ends="2026-11-12 04:35:50", files=1)},
            {"op": "verify-restore", "request": {"database_names": ["APP"]}}])
        monkeypatch.setattr(restore_by_id, "_execute", lambda op, request: (
            {"ok": True, "checked": 1, "failed": 0, "databases": [_row(**recorded)]}
            if op == "verify-restore" else {"ok": True}))
        return restore_by_id.restore_by_id({"restore_id": "R"}, catalog=catalog)

    run.catalog = catalog
    return run


def _runs(catalog):
    with catalog.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM restore_runs ORDER BY restore_run_id")]


def test_a_restore_where_it_was_asked_to_be_is_done_and_says_so(restored):
    outcome = restored({"last_backup_finished": "2026-11-12 04:35:50", "last_backup_type": "full"})

    assert outcome["landed"][0]["verdict"] == "restored to the end of its chain"
    run = _runs(restored.catalog)[0]
    assert run["phase"] == "done"
    assert json.loads(run["landed_json"])[0]["last_backup_finished"] == "2026-11-12 04:35:50"


def test_a_restore_that_is_not_where_it_was_asked_to_be_fails_by_name(restored):
    with pytest.raises(restore_by_id.RestoreByIdError,
                       match="restored and checked, but not to what was asked - APP: its chain ends 2026-11-12 04:35:50"):
        restored({"last_backup_finished": "2026-11-11 23:00:00", "last_backup_type": "full"})

    run = _runs(restored.catalog)[0]
    assert run["phase"] == "error", "open and consistent, and still not a restore that worked"
    assert json.loads(run["landed_json"])[0]["verdict"] == "NOT at the end of its chain", "what was read is kept"


def test_a_record_that_could_not_be_read_does_not_fail_the_restore(restored):
    outcome = restored({})

    assert outcome["landed"][0]["verdict"] == "not read"
    assert _runs(restored.catalog)[0]["phase"] == "done"
