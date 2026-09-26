"""A cross-machine SQL Server restore works on a fresh target, and says why when it does not.

On 2026-09-24 the lab drill - encrypted FULL/DIFF/LOG backups of a SQL Server lab on 192.0.2.249,
restored onto one on 192.0.2.250 - met six faults in a row before the first row arrived:

* the first run died listing a target folder it had not created yet ("[Errno 2] No such file");
* SQL Server could not open the pieces the copy had written as the SSH user, mode 0660 (Msg 3201);
* the backups could not be read before their certificate was imported, and the import was queued
  behind the listing that needed it (Msg 33111);
* both of those were swallowed as "stray files" and reported as "no databases found";
* every restore step then ran and failed on its own summary - ``data['engine']``, a key the 0.22.0
  rename had taken away - leaving the database RESTORING;
* and the alert said "Restore workflow FAILED." with the reason only in ``job_runs.error_text``.

The plan also connected to port 1433 whatever the target's port, which on a host with a 1433 and
an 11433 lab is the other instance. Each of these is pinned below; the drill itself then restored
1000 + 100 + 10 + 1 marked rows onto a fresh target, exactly what the source held.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from db_ops.backup_restore import events, restore_by_id, restore_script
from db_ops.common import backup_copy, cli_restorestep
from db_ops.common import backup_copy as transfer
from db_ops.common.backupfiles import BackupListError
from db_ops.common.backupfiles import sqlserver as listing
from tests.test_backup_transfer_stream import _Client, _Sftp


# --------------------------------------------------------------------------- #
# 1.11 - a restore step reports with the name it answers with
# --------------------------------------------------------------------------- #
def test_a_restore_step_summary_reads_db_type(monkeypatch):
    import db_ops.common.restorestep as restorestep

    monkeypatch.setattr(restorestep, "restore_step",
                        lambda level, request: {"db_type": "sqlserver", "level": level, "applied": ["a.bak"]})

    data, message = cli_restorestep._dispatch("restore-full", {})

    assert message == "sqlserver: applied 1 full backup(s)."


# --------------------------------------------------------------------------- #
# 1.12 - the first run onto a new target
# --------------------------------------------------------------------------- #
class _FreshTarget(_Sftp):
    """A target on which the staging folder does not exist until something makes it."""

    def listdir_attr(self, path):
        if path not in self.made:
            raise IOError(2, "No such file")
        return super().listdir_attr(path)


def test_the_first_run_creates_the_target_folder_before_looking_in_it(monkeypatch):
    source = _Client(sftp=_Sftp({"/src": [("a.bak", 100, False)]}))
    target_sftp = _FreshTarget({})
    monkeypatch.setattr(transfer, "_stream_files", lambda **kw: True)

    result = transfer.sync_backup_dir(source_client=source, source_dir="/src",
                                      target_client=_Client(sftp=target_sftp), target_dir="/dst")

    assert "/dst" in target_sftp.made
    assert result.copied == 1


def test_an_unreadable_source_still_stops_the_copy():
    """The walk stays strict where it matters: a source it cannot read looks like an empty one."""
    source = _Client(sftp=_FreshTarget({}))

    with pytest.raises(PermissionError, match="could not be read by the SSH user"):
        transfer.sync_backup_dir(source_client=source, source_dir="/src",
                                 target_client=_Client(sftp=_Sftp({})), target_dir="/dst")


# --------------------------------------------------------------------------- #
# 1.13 - the engine can read what the copy wrote
# --------------------------------------------------------------------------- #
def test_the_staged_pieces_are_opened_to_the_engine():
    client = _Client()

    assert backup_copy.open_for_the_engine(client, "/opt/db_ops/backup/in") is True
    assert client.commands == ["chmod -R a+rX /opt/db_ops/backup/in"]


def test_a_chmod_that_fails_is_said_and_does_not_stop_the_restore():
    client, said = _Client(rc=1), []

    assert backup_copy.open_for_the_engine(client, "/in", log=said.append) is False
    assert "could not open every staged piece" in said[0]


# --------------------------------------------------------------------------- #
# 1.14 / 1.17 - the plan: the certificate first, the target's own port
# --------------------------------------------------------------------------- #
def _job(**env):
    return SimpleNamespace(
        restore_id="LAB_TO_LAB", server_id="SRC", target_server_id="TGT-11433", target_container="lab",
        target_backup_dir="/in", target_visible_dir="", backup_dir="/out", env=dict(env),
        env_secrets={"MSSQL_PASSWORD": "SA_REF", "BACKUP_ENCRYPTION_PASSWORD": "ENC_REF"})


@pytest.fixture
def plan(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(restore_by_id, "_execute", lambda op, request: calls.append((op, request)) or {"ok": True})

    def listing_(request):
        calls.append(("list", request))
        full = [{"path": "/in/DB/FULL/f.bak", "database_name": "DB", "kind": "full"}]
        return {"files": full if "full" in request.get("kinds", []) else [],
                "newest_finished_at": "2026-09-24T10:00:00"}

    monkeypatch.setattr(restore_by_id, "_list_backup_files", listing_)
    import db_ops.lib.data_sources as data_sources
    monkeypatch.setattr(data_sources, "load_db_instances", lambda _d=None: [
        {"server_id": "TGT-11433", "db_type": "sqlserver", "port": 11433}])
    secrets = {"SA_REF": "pw", "ENC_REF": "enc"}

    def run(job, *, dry_run=False):
        steps = restore_by_id._plan_sqlserver(job, secrets, point_in_time="", host={"host": "192.0.2.250"},
                                              data_dir=None, dry_run=dry_run)
        return steps, calls
    return run


def test_the_certificate_is_imported_before_the_backups_are_listed(plan):
    steps, calls = plan(_job())

    assert [op for op, _ in calls][:2] == ["restore-key", "list"]
    assert steps[0]["op"] == "restore-key" and steps[0]["done"] is True


def test_a_dry_run_imports_nothing(plan):
    steps, calls = plan(_job(), dry_run=True)

    assert "restore-key" not in [op for op, _ in calls]
    assert steps[0]["op"] == "restore-key" and not steps[0].get("done")


def test_the_plan_connects_to_the_port_the_target_listens_on(plan):
    steps, _ = plan(_job())

    assert {step["request"]["target"]["port"] for step in steps} == {11433}


def test_env_mssql_port_overrides_the_inventory(plan):
    steps, _ = plan(_job(MSSQL_PORT="1533"))

    assert steps[1]["request"]["target"]["port"] == 1533


def test_a_step_run_while_planning_is_recorded_not_repeated(monkeypatch):
    ran = []
    job = SimpleNamespace(restore_id="R", db_type="sqlserver", env_secrets={}, is_remote=False, label="L")
    monkeypatch.setattr(restore_script, "load_script_restores", lambda _p=None: [job])
    monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "h"})
    monkeypatch.setitem(restore_by_id._PLANNERS, "sqlserver", lambda *a, **k: [
        {"op": "restore-key", "request": {}, "done": True},
        {"op": "restore-full", "request": {}}])
    monkeypatch.setattr(restore_by_id, "_execute", lambda op, request: ran.append(op) or {"ok": True})

    outcome = restore_by_id.restore_by_id({"restore_id": "R"})

    assert ran == ["restore-full"]
    assert [s["step"] for s in outcome["steps"]] == ["restore-key", "restore-full"]


# --------------------------------------------------------------------------- #
# 1.15 - a backup that cannot be read is not a stray file
# --------------------------------------------------------------------------- #
class _Cursor:
    def __init__(self, error):
        self.error, self.description, self._rows = error, [("path",), ("size",)], []

    def execute(self, sql):
        if sql.startswith("RESTORE HEADERONLY"):
            raise RuntimeError(self.error)
        self._rows = [("/in/DB/FULL/f.bak", 10)]

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, error):
        self._cursor = _Cursor(error)

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _list_with(monkeypatch, error):
    import db_ops.common.db_connect as db_connect
    monkeypatch.setattr(db_connect, "connect_engine", lambda **_: _Conn(error))
    return listing.list_files({"path": "/in", "target": {"host": "192.0.2.250"}})


def test_a_file_sql_server_calls_not_a_backup_is_skipped(monkeypatch):
    assert _list_with(monkeypatch, "The media family on device is incorrectly formed. (3241)") == []


@pytest.mark.parametrize("error", [
    "Cannot open backup device. Operating system error 5(Access is denied.). (3201)",
    "Cannot find server certificate with thumbprint '0xB384'. (33111)",
])
def test_a_backup_that_cannot_be_read_is_named_not_skipped(monkeypatch, error):
    with pytest.raises(BackupListError, match="cannot read /in/DB/FULL/f.bak"):
        _list_with(monkeypatch, error)


# --------------------------------------------------------------------------- #
# 1.16 - the reason reaches the log and the alert
# --------------------------------------------------------------------------- #
@pytest.fixture
def emitted(monkeypatch):
    seen = {}
    monkeypatch.setattr(events, "_safe_log_file", lambda **kw: seen.update(log=kw["message"]))
    monkeypatch.setattr(events, "_safe_insert_sqlite", lambda **kw: None)
    monkeypatch.setattr(events, "_safe_push_telegram", lambda **kw: seen.update(
        telegram=events._format_telegram_message(level=kw["level"], message=kw["message"],
                                                 metadata=kw["metadata"])))
    return seen


def test_a_failed_restore_says_why(emitted):
    events.emit_backup_restore_event(
        app_config=SimpleNamespace(store=None), command="restore-workflow", phase="ERROR", level="error",
        message="Restore R finished: error.", logger=object(),
        error_text="restore-full failed: 'engine'", metadata={"restore_id": "R"})

    assert emitted["log"].endswith("finished: error. - restore-full failed: 'engine'")
    assert "error_text=restore-full failed: 'engine'" in emitted["telegram"]


def test_a_reason_already_in_the_message_is_not_repeated(emitted):
    events.emit_backup_restore_event(
        app_config=SimpleNamespace(store=None), command="backup", phase="ERROR", level="error",
        message="Backup B/log finished: error (exit 1) - cannot log in", logger=object(),
        error_text="cannot log in", metadata={"backup_id": "B"})

    assert emitted["log"].count("cannot log in") == 1
