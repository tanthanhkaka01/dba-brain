"""The last open items of 0.23.0, fixed before its clock was allowed to count (the operator, 2026-09-24).

* 1.10 - a SQL task's credential was hidden by a `service_name` that did not match its group: three
  worker targets said SALES-PROD against the server's SALES-DEV group and failed "Credential not found"
  with the credential right there in users.json. On SQL Server `service_name` is a label (§1.1).
* 1.23 - a file named like a backup that SQL Server could not read as one (a truncated newest FULL)
  was passed over without a word; the restore silently went back to an older chain.
* 1.32 - the first process of a day archived the live log as *yesterday* whatever day its lines were
  from, so a new root filed its first lines under the wrong date.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db_ops.common import data_sources

GROUPS = [{"server_id": "S1", "db_type": "sqlserver", "service_name": "SALES-DEV", "instance_name": "MSSQLSERVER",
           "credentials": [{"credential_name": "sqlserver_s1_dba", "username": "dba"}]},
          {"server_id": "S1", "db_type": "oracle", "service_name": "ORCLPDB", "sid": "ORCL",
           "credentials": [{"credential_name": "oracle_s1_system", "username": "system"}]}]


# --------------------------------------------------------------------------- #
# 1.10
# --------------------------------------------------------------------------- #
def test_a_sql_server_credential_is_found_whatever_label_the_target_carries():
    found = data_sources.find_database_credential(
        GROUPS, server_id="S1", credential_name="sqlserver_s1_dba", db_type="sqlserver",
        service_name="SALES-PROD", instance_name="MSSQLSERVER")

    assert found["username"] == "dba"


def test_an_oracle_service_still_chooses_the_group():
    """Oracle connects by service, so there the name is not a label."""
    with pytest.raises(data_sources.CredentialNotFound):
        data_sources.find_database_credential(
            GROUPS, server_id="S1", credential_name="oracle_s1_system", db_type="oracle",
            service_name="OTHERPDB")


def test_a_credential_in_a_group_the_target_did_not_match_is_located():
    with pytest.raises(data_sources.CredentialNotFound, match="in the group for service_name 'ORCLPDB'"):
        data_sources.find_database_credential(
            GROUPS, server_id="S1", credential_name="oracle_s1_system", db_type="oracle",
            service_name="OTHERPDB")


def test_the_sql_task_says_the_lookups_reason_not_just_the_name():
    from db_ops.sql_tasks import runner

    target = SimpleNamespace(server_id="S1", credential_name="oracle_s1_system", db_type="oracle",
                             service_name="OTHERPDB", instance_name="")

    assert "in the group for service_name 'ORCLPDB'" in runner.credential_problem(target, GROUPS)


# --------------------------------------------------------------------------- #
# 1.23
# --------------------------------------------------------------------------- #
class _Cursor:
    def __init__(self):
        self.description, self._rows = [("path",), ("size",)], []

    def execute(self, sql):
        if sql.startswith("RESTORE HEADERONLY"):
            raise RuntimeError("The media family on device is incorrectly formed. (3241)")
        self._rows = [("/in/DB/FULL/newest.bak", 10), ("/in/_cert/key.cer", 1)]

    def fetchall(self):
        return self._rows


def test_a_piece_named_like_a_backup_that_is_not_one_is_reported(monkeypatch):
    import db_ops.common.db_connect as db_connect
    from db_ops.common.backupfiles import list_backup_files

    monkeypatch.setattr(db_connect, "connect_engine", lambda **_: SimpleNamespace(
        cursor=lambda: _Cursor(), close=lambda: None))

    result = list_backup_files({"db_type": "sqlserver", "path": "/in", "target": {"host": "h"}})

    assert result["unreadable"] == ["/in/DB/FULL/newest.bak"], "the certificate is not ours to report"


def test_the_restore_finishes_with_a_warning_naming_it(monkeypatch):
    from db_ops.backup_restore import restore_by_id, restore_script

    job = SimpleNamespace(restore_id="R", db_type="sqlserver", env_secrets={}, is_remote=False, label="L")
    monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])
    monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "h"})

    def planner(*a, **k):
        restore_by_id._UNREADABLE.add("/in/DB/FULL/newest.bak")
        return [{"op": "restore-full", "request": {}},
                {"op": "warning", "warning": "1 file(s) named like backups ... /in/DB/FULL/newest.bak"}]

    monkeypatch.setitem(restore_by_id._PLANNERS, "sqlserver", planner)
    monkeypatch.setattr(restore_by_id, "_execute", lambda op, request: {"ok": True})

    outcome = restore_by_id.restore_by_id({"restore_id": "R"})

    assert [s["step"] for s in outcome["steps"]] == ["restore-full"]
    assert "newest.bak" in outcome["warnings"][0]


# --------------------------------------------------------------------------- #
# 1.32
# --------------------------------------------------------------------------- #
def _log(tmp_path, written: datetime):
    path = tmp_path / "app.log"
    path.write_text("a line", encoding="utf-8")
    stamp = written.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_a_log_written_today_is_not_archived_as_yesterday(tmp_path):
    from db_ops.logging_ops.handlers import archive_yesterday_if_missing
    from db_ops.lib.timezone import display_today

    path = _log(tmp_path, datetime.now(timezone.utc))

    assert archive_yesterday_if_missing(path, today=display_today()) is None
    assert path.exists()


def test_a_log_last_written_days_ago_is_archived_under_that_day(tmp_path):
    """A node back from three days down: its last lines belong to their own day, not yesterday."""
    from db_ops.logging_ops.handlers import archive_yesterday_if_missing
    from db_ops.lib.timezone import display_today, to_display

    written = datetime.now(timezone.utc) - timedelta(days=3)
    path = _log(tmp_path, written)

    archived = archive_yesterday_if_missing(path, today=display_today())

    assert archived is not None
    assert archived.name == f"app_{to_display(written).date():%Y%m%d}.log"
    assert archived.name != f"app_{(display_today() - timedelta(days=1)):%Y%m%d}.log"
