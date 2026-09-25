"""A point-in-time restore reaches the moment that was asked for - on every engine, in any offset.

The point-in-time drill of 2026-09-25 (`/spbot_restore <id> "<moment>"`, marker rows written either
side of the moment on the lab source) found that none of the three engines got there:

* SQL Server refused the statement - `Invalid value specified for STOPAT parameter` (Msg 3217) -
  because the moment went into STOPAT as typed, `+00:00` and all. Behind that, the log chain was cut
  at "finished by the moment", which leaves out the one log that HOLDS the moment: fixed alone,
  STOPAT would have restored to the end of the previous log and reported success.
* PostgreSQL got the data right and then never finished: paused at the target by default, the
  server stayed in recovery and the restore waited its thirty minutes to call it a failure.
* Oracle's `TO_DATE(..., 'YYYY-MM-DD HH24:MI:SS')` refuses an offset.

Under all three, the listings' time window cut every stamp and bound to its first 19 characters -
the offset with it - so a moment typed in +08:00 chose backups eight hours off, and PostgreSQL's
finish times (`stat` prints the host's own +07:00) were seven hours off even with a UTC moment.
"""

from __future__ import annotations

import pytest

from db_ops.backup_restore.restore_by_id import _logs_through
from db_ops.common.backupfiles import _in_window, _later
from db_ops.common.restorestep import RestoreStepError, restore_step
from db_ops.lib.restore.moment import server_clock_text


def _mssql_log(stopat):
    return restore_step("log", {"db_type": "sqlserver", "dry_run": True, "database_name": "APPDB",
                                "backup_paths": ["/b/1.trn", "/b/2.trn"], "with_recovery": True,
                                "stopat": stopat, "target": {"host": "h", "username": "sa", "password": "x"}})


def _statements(result):
    return result.get("statements") or result.get("sql") or []


def test_stopat_carries_no_offset_and_is_in_the_servers_clock():
    last = _statements(_mssql_log("2026-09-25 07:13:40 +08:00"))[-1]

    assert "STOPAT = N'2026-09-24 23:13:40'" in last
    assert "+08" not in last and "+00" not in last


def test_the_same_moment_in_any_offset_gives_the_same_stopat():
    forms = ("2026-09-25 07:13:40 +08:00", "2026-09-24 23:13:40 +00:00", "2026-09-24T23:13:40Z".replace("Z", "+00:00"))
    assert {_statements(_mssql_log(form))[-1] for form in forms} == {_statements(_mssql_log(forms[0]))[-1]}


def test_a_moment_that_cannot_be_read_is_refused_by_name():
    with pytest.raises(RestoreStepError, match="YYYY-MM-DD HH:MM:SS"):
        _mssql_log("yesterday at two")


def test_oracle_until_time_carries_no_offset():
    result = restore_step("full", {"db_type": "oracle", "dry_run": True, "backup_path": "/b/a.bkp",
                                   "backup_location": "/b", "oracle_sid": "FREE", "mode": "duplicate",
                                   "stopat": "2026-09-25 07:13:40 +08:00"})
    # Scripts travel base64-encoded (never as shell text) - read them back.
    import base64
    import re

    text = str(result) + "".join(
        base64.b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8", "replace")
        for blob in re.findall(r"[A-Za-z0-9+/]{40,}={0,2}", str(result)))

    assert "TO_DATE('2026-09-24 23:13:40', 'YYYY-MM-DD HH24:MI:SS')" in text
    assert "+08:00" not in text
    # Inside a RUN block with the duplicate - outside one RMAN refuses it (RMAN-03031), which is
    # how the drill's point-in-time duplicate failed after the offset was fixed.
    script = text[text.index("SET UNTIL TIME") - 20:]
    assert "RUN {" in script[:40] and script.index("DUPLICATE") < script.index("}")


# --------------------------------------------------------------------------- #
# The listings' window
# --------------------------------------------------------------------------- #
def test_a_bound_with_an_offset_is_compared_in_the_servers_clock():
    files = [{"path": "a", "finished_at": "2026-09-24T22:37:00"},
             {"path": "b", "finished_at": "2026-09-24T23:14:05"}]

    kept = _in_window(files, after=None, before="2026-09-25 07:13:40 +08:00")   # = 23:13:40 UTC

    assert [f["path"] for f in kept] == ["a"], "cut to 19 characters it read 07:13 UTC and kept neither"


def test_a_hosts_local_stat_time_is_read_with_its_offset():
    """`stat -c %y` on a +07:00 lab host: a full taken at 18:35 UTC is BEFORE a 23:02 UTC moment."""
    stamp = "2026-09-25 01:35:12.123456789 +0700"

    assert not _later(stamp, "2026-09-24 23:02:15 +00:00")
    assert server_clock_text("2026-09-25 01:35:12 +0700") == "2026-09-24 18:35:12"


# --------------------------------------------------------------------------- #
# The SQL Server log chain
# --------------------------------------------------------------------------- #
def test_the_log_holding_the_moment_is_in_the_chain():
    logs = [{"path": "l1", "finished_at": "2026-09-24T22:37:00"},
            {"path": "l2", "finished_at": "2026-09-24T23:14:05"},   # holds 23:13:40
            {"path": "l3", "finished_at": "2026-09-24T23:29:00"}]

    assert [f["path"] for f in _logs_through(logs, "2026-09-24 23:13:40 +00:00")] == ["l1", "l2"]
    assert [f["path"] for f in _logs_through(logs, "2026-09-25 07:13:40 +08:00")] == ["l1", "l2"]


def test_a_moment_after_every_log_takes_them_all():
    logs = [{"path": "l1", "finished_at": "2026-09-24T22:37:00"}]

    assert _logs_through(logs, "2026-09-24 23:59:00 +00:00") == logs


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #
def test_a_postgresql_target_time_promotes_rather_than_pauses():
    from db_ops.common.restorestep.postgresql import _recovery_config

    config = _recovery_config("/data", "/wal", "2026-09-24 23:02:15 +00:00")

    assert "recovery_target_action = '\"'\"'promote'\"'\"'" in config or "recovery_target_action = 'promote'" in config
    assert "recovery_target_action" not in _recovery_config("/data", "/wal", "")


def test_a_paused_recovery_ends_the_wait_at_once(monkeypatch):
    import db_ops.common.restorestep.postgresql as pg
    from db_ops.common.hostcmd import parse_host

    def fake_run(host, command, **kwargs):
        if "State.Status" in command:
            return {"exit_code": 0, "stdout": "running 0", "stderr": ""}
        if "pg_is_wal_replay_paused" in command:
            return {"exit_code": 0, "stdout": "t\n", "stderr": ""}
        return {"exit_code": 0, "stdout": "t\n", "stderr": ""}

    monkeypatch.setattr(pg, "run", fake_run)
    host = parse_host({"runtime": "docker", "host": "h", "container": "c"})

    with pytest.raises(RestoreStepError, match="PAUSED"):
        pg._wait_for_recovery(pg._host_of(host), host, run_as="postgres", timeout=600)


def _oracle_scripts(**over):
    import base64
    import re

    request = {"db_type": "oracle", "dry_run": True, "backup_path": "/b/a.bkp",
               "backup_location": "/opt/db_ops/backup/ora_restore_from_249", "oracle_sid": "FREE",
               "mode": "duplicate"}
    request.update(over)
    result = restore_step("full", request)
    return str(result) + "".join(
        base64.b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8", "replace")
        for blob in re.findall(r"[A-Za-z0-9+/]{40,}={0,2}", str(result)))


def test_oracle_reads_only_its_own_staging_folder():
    """Without the trailing slash RMAN took the location as a prefix and read `..._249ha` too -
    the Data Guard lab's pieces, same DBID - and the point-in-time duplicate died on its logs."""
    for location in ("/opt/db_ops/backup/ora_restore_from_249", "/opt/db_ops/backup/ora_restore_from_249/"):
        text = _oracle_scripts(backup_location=location)

        assert "BACKUP LOCATION '/opt/db_ops/backup/ora_restore_from_249/'" in text
        assert "ora_restore_from_249//" not in text


def test_an_oracle_duplicate_never_resumes_onto_an_earlier_ones_files():
    text = _oracle_scripts(stopat="2026-09-25 07:33:47 +08:00")

    assert "NORESUME" in text
    assert "rm -f $ORACLE_HOME/dbs/arch*.dbf" in text


# --------------------------------------------------------------------------- #
# PostgreSQL's listing: the host's clock, and a staging copy's dates
# --------------------------------------------------------------------------- #
def _pg_listing(monkeypatch, stdout):
    from db_ops.common.backupfiles import list_backup_files

    monkeypatch.setattr("db_ops.common.backupfiles.postgresql.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": stdout, "stderr": ""})
    return lambda **request: list_backup_files({"db_type": "postgresql", "path": "/b", **request})


def test_a_postgresql_row_knows_its_finish_in_utc_as_well_as_in_the_hosts_clock(monkeypatch):
    listing = _pg_listing(monkeypatch, "/b/base/20260925T035306Z_FULL|4096|2026-09-25 10:53:07.123 +0700\n")

    row_ = listing()["files"][0]

    assert row_["finished_at"] == "2026-09-25 10:53:07", "the host's clock - retention reads it"
    assert row_["finished_at_utc"] == "2026-09-25 03:53:07"


def test_a_point_in_time_on_a_host_ahead_of_utc_takes_the_newest_full_before_it(monkeypatch):
    """Compared in the host's +07 clock, the full that finished at 18:35 UTC read as 01:35 - after
    a 00:23 UTC moment - and an older full was chosen (the lab drill, 2026-09-25)."""
    listing = _pg_listing(monkeypatch,
                          "/b/base/20260924T135052Z_FULL|1|2026-09-24 20:51:00 +0700\n"
                          "/b/base/20260924T183532Z_FULL|1|2026-09-25 01:35:33 +0700\n"
                          "/b/base/20260925T004713Z_FULL|1|2026-09-25 07:47:14 +0700\n")

    chosen = listing(kinds=["full"], latest=True, before="2026-09-25 08:23:46 +08:00")["files"]

    assert [f["path"].rsplit("/", 1)[-1] for f in chosen] == ["20260924T183532Z_FULL"]


def test_the_postgresql_listing_dates_a_backup_by_its_manifest_not_its_folder():
    """A staged folder is dated by the copy that made it; the manifest keeps the backup's time."""
    import inspect

    from db_ops.common.backupfiles import postgresql

    assert "backup_manifest" in inspect.getsource(postgresql.list_files)


def test_an_incremental_staged_in_the_same_second_as_its_full_stays_in_the_chain(monkeypatch):
    from types import SimpleNamespace

    from db_ops.backup_restore import restore_by_id as rbi

    rows = {"full": [{"path": "/s/base/20260925T035306Z_FULL", "kind": "full",
                      "finished_at": "2026-09-25 10:53:22"}],
            "diff": [{"path": "/s/base/20260925T035310Z_INCR", "kind": "diff",
                      "finished_at": "2026-09-25 10:53:22"},
                     {"path": "/s/base/20260925T004716Z_INCR", "kind": "diff",
                      "finished_at": "2026-09-25 10:53:22"}]}
    monkeypatch.setattr(rbi, "_list_backup_files", lambda request: {
        "files": rows[request["kinds"][0]],
        "newest_finished_at": rows[request["kinds"][0]][-1]["finished_at"]})
    monkeypatch.setattr(rbi, "_visible_dir", lambda job: "/s")
    job = SimpleNamespace(restore_id="R", env={})

    step = rbi._plan_postgresql(job, {}, point_in_time="", host={"runtime": "docker", "container": "c"},
                                data_dir=None)[0]

    assert step["request"]["backup_paths"] == ["/s/base/20260925T035306Z_FULL",
                                               "/s/base/20260925T035310Z_INCR"], \
        "the older chain's incremental is not this full's; the one staged with it is"
