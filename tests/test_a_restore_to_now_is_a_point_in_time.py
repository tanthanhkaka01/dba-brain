"""The newest restore is a point in time of now (0.39.0, 1.363).

The operator, 2026-11-13: *restore must be able to restore to a point in time; restoring the newest
is a point in time of now* - and the test of it a random moment within the last 14 days. Read the
same day: a moment after the newest log backup broke the restore it asked for. SQL Server was given
STOPAT past the end of its last log and left the database RESTORING (Msg 4326); PostgreSQL 13+ stops
a recovery whose target lies after the WAL with "recovery ended before configured recovery target
was reached". A moment at or after the newest log is that log's end - nothing later is in a backup -
so the chain is replayed to its end, and the answer says where that is.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import landing_of
from dbabrain.backup_restore import restore_by_id

FULL = {"path": "/s/APP/FULL/APP_FULL_1.bak", "first_lsn": 900, "last_lsn": 1000, "checkpoint_lsn": 950,
        "finished_at": "2026-11-12T04:35:50Z"}
LOGS = [{"path": f"/s/APP/LOG/APP_LOG_{n}.trn", "first_lsn": 1000 + 100 * (n - 1), "last_lsn": 1000 + 100 * n,
         "finished_at": f"2026-11-12T0{4 + n}:00:00Z"} for n in (1, 2, 3)]


def _plan(monkeypatch, moment: str) -> list[dict]:
    def listed(request):
        kind = request["kinds"][0]
        files = [FULL] if kind == "full" else LOGS if kind == "log" else []
        return {"files": files, "newest_finished_at": "2026-11-12 04:35:50" if files else None}

    monkeypatch.setattr(restore_by_id, "_list_backup_files", listed)
    monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/s")
    monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, data_dir=None: 1433)
    job = SimpleNamespace(restore_id="APP_TO_LAB", env={"MSSQL_USER": "dba"}, landing=landing_of("APP"),
                          env_secrets={}, overwrite_existing=False)
    return restore_by_id._plan_sqlserver(job, {}, point_in_time=moment, host={"host": "192.0.2.40"}, data_dir=None)


def _log_step(steps):
    """The logs of the plan as one view: a log step holds one file since 1.370 (one message per log),
    so the paths are every step's in order, and the recovery and the STOPAT are the last step's -
    no other log step carries either."""
    logs = [step for step in steps if step["op"] == "restore-log"]
    assert all(step["request"].get("backup_path") and "backup_paths" not in step["request"] for step in logs)
    assert not any(step["request"].get("with_recovery") or step["request"].get("stopat") for step in logs[:-1]),         "a log before the last recovers nothing and stops nowhere"
    return {**logs[-1], "request": {**logs[-1]["request"],
                                    "backup_paths": [step["request"]["backup_path"] for step in logs]}}


@pytest.mark.parametrize("moment", ["2026-11-13 04:05:00 +00:00", "2026-11-12 07:00:00 +00:00"])
def test_a_moment_at_or_after_the_newest_log_replays_the_chain_to_its_end(monkeypatch, moment):
    steps = _plan(monkeypatch, moment)
    log = _log_step(steps)["request"]
    assert "stopat" not in log, "a STOPAT past the last log leaves SQL Server RESTORING (Msg 4326)"
    assert log["with_recovery"] is True and len(log["backup_paths"]) == 3
    said = [step["warning"] for step in steps if step["op"] == "warning"]
    assert said and "newest log backup (finished 2026-11-12T07:00:00Z)" in said[0]
    assert "restored to that log's end" in said[0]


def test_a_moment_inside_the_logs_stops_there(monkeypatch):
    steps = _plan(monkeypatch, "2026-11-12 05:30:00 +00:00")
    log = _log_step(steps)["request"]
    assert log["stopat"] == "2026-11-12 05:30:00 +00:00"
    assert len(log["backup_paths"]) == 2, "every log up to the first that holds the moment"
    assert not [step for step in steps if step["op"] == "warning"]


@pytest.mark.parametrize("files, moment, reached", [
    (LOGS, "2026-11-12 07:00:00 +00:00", "2026-11-12T07:00:00Z"),
    (LOGS, "2026-11-12 06:59:59 +00:00", ""),
    (LOGS, "", ""),
    ([], "2026-11-13 00:00:00 +00:00", ""),
    ([{"path": "x"}], "2026-11-13 00:00:00 +00:00", ""),
])
def test_past_the_newest_is_the_newest_end_only_when_the_moment_reaches_it(files, moment, reached):
    assert restore_by_id.past_the_newest(files, moment) == reached


# --------------------------------------------------------------------------- #
# The moment in the source's clock (found by the random test, 2026-11-13)
# --------------------------------------------------------------------------- #
#: A Windows source on +07: its files are named in UTC, its headers end in its own clock.
SOURCE_LOGS = [{"path": f"/s/APP/LOG/APP_LOG_20261112_0{h}0000Z.trn", "first_lsn": 1000 + 100 * i,
                "last_lsn": 1100 + 100 * i, "finished_at": f"2026-11-12T{h + 7:02d}:00:03"} for i, h in enumerate((5, 6, 7))]


def test_the_sources_offset_is_read_off_its_own_files():
    assert restore_by_id.source_offset_minutes(SOURCE_LOGS) == 420
    assert restore_by_id.source_offset_minutes([{"path": "/s/APP_LOG_1.trn", "finished_at": "2026-11-12T05:00:00"}]) is None
    # A FULL takes long; the shortest file decides, so it cannot skew the offset.
    with_full = [{"path": "/s/F/APP_FULL_20261112_043548Z.bak", "finished_at": "2026-11-12T12:50:00"}, *SOURCE_LOGS]
    assert restore_by_id.source_offset_minutes(with_full) == 420


def test_a_moment_is_handed_to_sql_server_in_the_sources_clock(monkeypatch):
    """STOPAT 06:30 in UTC on a +07 source stopped seven hours early - the random test's restore to
    now stopped at the log of seven hours before and said done."""
    full = {"path": "/s/APP/FULL/APP_FULL_20261112_043548Z.bak", "first_lsn": 900, "last_lsn": 1000,
            "checkpoint_lsn": 950, "finished_at": "2026-11-12T11:35:50"}

    def listed(request):
        kind = request["kinds"][0]
        files = [full] if kind == "full" else SOURCE_LOGS if kind == "log" else []
        return {"files": files, "newest_finished_at": "2026-11-12 11:35:50" if files else None}

    monkeypatch.setattr(restore_by_id, "_list_backup_files", listed)
    monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/s")
    monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, data_dir=None: 1433)
    job = SimpleNamespace(restore_id="APP_TO_LAB", env={"MSSQL_USER": "dba"}, landing=landing_of("APP"),
                          env_secrets={}, overwrite_existing=False)
    steps = restore_by_id._plan_sqlserver(job, {}, point_in_time="2026-11-12 13:30:00 +07:00",
                                          host={"host": "192.0.2.40"}, data_dir=None)
    log = _log_step(steps)["request"]
    assert log["stopat"] == "2026-11-12 13:30:00", "the source's own clock, not 06:30 in UTC"
    assert [p.rsplit("_", 1)[-1] for p in log["backup_paths"]] == ["050000Z.trn", "060000Z.trn", "070000Z.trn"]
