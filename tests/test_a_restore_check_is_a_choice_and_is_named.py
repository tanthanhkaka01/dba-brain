"""`DBCC CHECKDB` after a restore is a choice on the entry, and a failed check is called what it is.

On 2026-09-24 `ACME_RESTORE_DRILL_01` restored and recovered all 13 databases, then
reported *restore failed for 2 database(s): APPINST, SALESDB*. What failed was the check afterwards:
the target, a SQL Server container, could not create the check's internal snapshot (Msg 1823 /
7928) and killed the session. "Restore failed" sent the operator to the backups and the copy, which
were fine. And the check ran on every database of every restore, with no way to say "not here".

So: `checkdb` on the restore entry (default true - an upgrade must not quietly weaken every drill);
false skips it and says so; a failed check is `CHECK_FAILED`, not `FAILED`, and the message says
*restored and recovered, integrity check failed*, with SQL Server's message numbers. It still fails
the run and still holds back retention cleanup - only the words changed.
"""

from __future__ import annotations

import dataclasses
import subprocess

import pytest

from db_ops.backup_restore import cli as backup_cli
from db_ops.backup_restore import restore_database as restore_module
from db_ops.backup_restore.config import parse_restore_config
from db_ops.lib.config import DbOpsConfig
from db_ops.lib import shared_objects
from test_backup_restore import make_config

MINIMAL = {
    "cleanup_retention": 691200, "prod_backup_share": r"\\192.0.2.250\SQLBK",
    "vm_import_unc": "x", "vm_import_local": r"E:\SQLBK_IMPORT", "vm_log_unc": "y",
    "vm_log_local": r"E:\LOGS", "restore_sql_instance_on_vm": "localhost",
}


def test_the_check_is_on_unless_the_entry_says_otherwise():
    assert parse_restore_config(dict(MINIMAL)).checkdb is True
    assert parse_restore_config({**MINIMAL, "checkdb": False}).checkdb is False


def _restore(tmp_path, monkeypatch, *, checkdb=True, checkdb_fails=False):
    config = dataclasses.replace(make_config(tmp_path), source_id="SRC1", target_id="TGT1",
                                 restore_sql_instance_on_vm="localhost", checkdb=checkdb)
    backup = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    app_config = DbOpsConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                             sqlite_path=tmp_path / "runtime" / "db_ops.sqlite")
    commands, messages = [], []

    def fake_run_sqlcmd(cmd, **_kwargs):
        commands.append(" ".join(map(str, cmd)))
        if checkdb_fails and "CHECKDB" in commands[-1]:
            # As the real one does: a non-zero sqlcmd exit is raised, with its output.
            raise RuntimeError(
                "sqlcmd command failed with exit code 1.\nstdout:\nMsg 1823, Level 16, State 6\nA "
                "database snapshot cannot be created because it failed to start.\nMsg 7928, Level 16")
        return subprocess.CompletedProcess(cmd, 0, "complete", "")

    monkeypatch.setattr(restore_module, "run_sqlcmd_query_command", fake_run_sqlcmd)
    monkeypatch.setattr(restore_module, "log_event", lambda _logger, **kwargs: messages.append(kwargs["message"]))
    run = lambda: restore_module.run_restore_database(  # noqa: E731
        config=config, db_ops_config=app_config, backup_file=backup,
        ensure_certificate=False, ensure_credential=False, logger=object())
    return run, commands, messages


def test_switched_off_the_check_is_not_sent_and_the_run_says_so(tmp_path, monkeypatch):
    run, commands, messages = _restore(tmp_path, monkeypatch, checkdb=False)

    result = run()

    assert result["status"] == "SUCCESS"
    assert not any("CHECKDB" in command for command in commands)
    assert result["checkdb_status"] == "SKIPPED"
    assert any("dbcc-checkdb skipped" in m and "checkdb_false" in m for m in messages)


def test_switched_on_the_check_runs(tmp_path, monkeypatch):
    run, commands, _ = _restore(tmp_path, monkeypatch)

    assert run()["checkdb_status"] != "SKIPPED"
    assert any("CHECKDB" in command for command in commands)


def test_a_failed_check_is_its_own_failure(tmp_path, monkeypatch):
    run, _, _ = _restore(tmp_path, monkeypatch, checkdb_fails=True)

    with pytest.raises(restore_module.IntegrityCheckFailed, match="1823"):
        run()


def test_a_failed_check_is_recorded_check_failed_not_failed():
    assert restore_module._failed_status(restore_module.IntegrityCheckFailed("x")) == "CHECK_FAILED"
    assert restore_module._failed_status(RuntimeError("x")) == "FAILED"


def test_the_message_tells_a_failed_check_from_a_failed_restore():
    outputs = [{"per_database_restore_status": {"APPINST": "CHECK_FAILED", "SALESDB": "CHECK_FAILED",
                                                "APP": "SUCCESS", "Broken": "FAILED"},
                "per_database_error": {"APPINST": "Msg 1823 ... Msg 7928 ...", "SALESDB": "Msg 1823",
                                       "Broken": "Msg 3203 read failure"}}]

    text = backup_cli._restore_failure_text(outputs, ["Broken", "APPINST", "SALESDB"])

    assert "restore failed for 1 database(s): Broken" in text
    assert "2 database(s) restored and recovered, but the integrity check (DBCC CHECKDB) failed: APPINST, SALESDB" in text
    assert "Msg 1823, 7928" in text and "checkdb: false" in text


def test_only_a_failed_check_never_says_restore_failed():
    outputs = [{"per_database_restore_status": {"APPINST": "CHECK_FAILED"}, "per_database_error": {}}]

    assert "restore failed" not in backup_cli._restore_failure_text(outputs, ["APPINST"])


def test_the_reference_describes_the_switch(tmp_path):
    entry = shared_objects.describe("restore_entry")
    assert "checkdb" in [f["field"] for f in entry["fields"]]
    assert shared_objects.check_record("restore_entry", {"restore_id": "R", "checkdb": False},
                                       where="t") == [] or all(
        f["field"] != "checkdb" for f in shared_objects.check_record(
            "restore_entry", {"restore_id": "R", "checkdb": False}, where="t"))
