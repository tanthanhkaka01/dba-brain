"""A full SQL Server restore over a database ONLINE on its target runs only when the entry says so.

``REPLACE`` overwrites whatever holds the name, and the full template first sets the database
``SINGLE_USER WITH ROLLBACK IMMEDIATE``: an entry aimed at the wrong target threw out its users and
then overwrote their data, and nothing asked (owner decision G2.10, 2026-10-01). The batch now
starts with a guard that raises when the database is ONLINE, unless the request carries
``overwrite_existing: true`` - which every drill that runs again over its own last restore states.
A database RESTORING from an earlier run, or absent, is restored as before.

G2.11, phase 1: a script entry that names no ``env.MSSQL_USER`` still logs in as ``sa`` in this
release, and the answer says so.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from db_ops.backup_restore import restore_by_id
from db_ops.backup_restore.restore_script import load_script_restores
from db_ops.common.restorestep import sqlserver as mssql

FILES = {"data": "/var/opt/mssql/data/HR.mdf", "log": "/var/opt/mssql/data/HR.ldf"}


def _full(**request) -> str:
    return mssql.build_statements("full", {"database_name": "HR", "move_files": FILES, **request},
                                  ["/in/HR/FULL/hr.bak"])[0]


def test_the_guard_comes_before_anything_the_restore_does():
    text = _full()

    guard = text.index("DATABASEPROPERTYEX(N'HR', N'Status') = N'ONLINE'")
    assert guard < text.index("SET SINGLE_USER") < text.index("RESTORE DATABASE")
    assert "RAISERROR" in text[guard:text.index("SET SINGLE_USER")]
    assert "overwrite_existing: true" in text


def test_a_stated_overwrite_has_no_guard():
    assert "N'ONLINE'" not in _full(overwrite_existing=True)


def test_only_a_full_with_replace_is_guarded():
    diff = mssql.build_statements("diff", {"database_name": "HR"}, ["/in/HR/DIFF/d.bak"])[0]
    without_replace = _full(replace=False)

    assert "N'ONLINE'" not in diff and "N'ONLINE'" not in without_replace


def test_the_drill_entry_s_word_reaches_the_restore(monkeypatch):
    from db_ops.backup_restore import restore_database

    config = SimpleNamespace(overwrite_existing=True)
    candidate = SimpleNamespace(restore_database_name="HR", restore_data_file_on_vm="d", restore_log_file_on_vm="l")
    monkeypatch.setattr(restore_database, "_target_path_str", lambda path, cfg: str(path))

    fields = restore_database._restore_step("full", candidate, config, path="/in/hr.bak")

    assert fields["overwrite_existing"] is True


def _entry(**over) -> dict:
    entry = {"restore_id": "LAB_TO_LAB", "db_type": "sqlserver", "server_id": "SRC",
             "target_server_id": "TGT", "backup_dir": "/out", "script": "sqlserver",
             "target_backup_dir": "/opt/db_ops/staging/lab", "source_backup_host_dir": "/opt/out",
             "cleanup_retention": 86400, "time_window": {"from_hour": 0, "to_hour": 23}}
    entry.update(over)
    return entry


def _load(tmp_path, entry: dict):
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": [entry]}}), encoding="utf-8")
    return load_script_restores(path)[0]


def test_a_script_entry_reads_the_field_and_defaults_to_refusing(tmp_path):
    assert _load(tmp_path, _entry(overwrite_existing=True)).overwrite_existing is True
    assert _load(tmp_path, _entry()).overwrite_existing is False
    assert _load(tmp_path, _entry(overwrite_existing="yes")).overwrite_existing is False


@pytest.fixture
def planned(monkeypatch):
    def plan(**job_fields):
        job = SimpleNamespace(restore_id="R", server_id="SRC", target_server_id="TGT", target_container="",
                              target_backup_dir="/in", target_visible_dir="", backup_dir="/out",
                              env_secrets={"MSSQL_PASSWORD": "REF"}, **job_fields)
        monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda j, **_: 1433)
        monkeypatch.setattr(restore_by_id, "_secret", lambda ref, secrets, where: "pw")
        monkeypatch.setattr(restore_by_id, "_list_backup_files", lambda request: {
            "files": [{"path": "/in/DB/FULL/f.bak", "database_name": "DB", "kind": "full"}]
            if "full" in request.get("kinds", []) else [], "newest_finished_at": "2026-10-01T00:00:00Z"})
        return restore_by_id._plan_sqlserver(job, {}, point_in_time="", host={"host": "h"}, data_dir=None)
    return plan


def test_the_script_path_passes_the_entry_s_word(planned):
    steps = planned(env={"MSSQL_USER": "restorer"}, overwrite_existing=True)

    full = next(step for step in steps if step["op"] == "restore-full")
    assert full["request"]["overwrite_existing"] is True
    assert full["request"]["target"]["username"] == "restorer"
    assert not any(step["op"] == "warning" for step in steps)


def test_an_unnamed_login_still_runs_as_sa_and_says_so(planned):
    steps = planned(env={}, overwrite_existing=False)

    warning = next(step["warning"] for step in steps if step["op"] == "warning")
    full = next(step for step in steps if step["op"] == "restore-full")
    assert "env.MSSQL_USER" in warning and "G2.11" in warning
    assert full["request"]["target"]["username"] == "sa"
    assert full["request"]["overwrite_existing"] is False
