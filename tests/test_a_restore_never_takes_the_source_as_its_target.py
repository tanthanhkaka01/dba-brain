"""A restore names its target; nothing about the target is derived from the source.

Owner decision 2026-10-01 (review 0.25.0, B4.5 / G2): a restore states which source goes to which
target; the code never works it out and never falls back.

Before it, an entry with only `target_container` meant "a container on the source host", and the
SQL Server plan then connected to `source host : source port` - the source instance itself - and
ran `RESTORE ... REPLACE` there: production overwritten with its own last backup. The scheduler's
path (`restore_by_id`) had no target-is-not-source check at all.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from db_ops.backup_restore import restore_by_id
from db_ops.backup_restore.backup import BackupTarget


def _job(**over):
    values = dict(restore_id="R", label="R (sqlserver)", db_type="sqlserver", server_id="SRC",
                  target_server_id="SRC", target_container="lab_b", target_backup_dir="",
                  target_visible_dir="/in", backup_dir="/out", env={"MSSQL_PORT": "11433"},
                  env_secrets={})
    values.update(over)
    return SimpleNamespace(**values)


@pytest.fixture
def estate(monkeypatch):
    """Source SRC: host 192.0.2.10, container prod, SQL port 1433."""
    import db_ops.backup_restore.backup as backup
    import db_ops.lib.data_sources as data_sources

    monkeypatch.setattr(backup, "resolve_ssh_target", lambda sid, **_k: BackupTarget(
        server_id=sid, host="192.0.2.10", port=22, username="u", container_name="prod",
        key_file=None, password_ref=None))
    monkeypatch.setattr(data_sources, "load_db_instances", lambda _d=None: [
        {"server_id": "SRC", "db_type": "sqlserver", "port": 1433}])


def test_the_source_container_is_refused_as_the_target(estate):
    with pytest.raises(restore_by_id.RestoreByIdError, match="the target is the source instance"):
        restore_by_id.assert_target_is_not_source(
            _job(target_container="prod"), {"host": "192.0.2.10"}, data_dir=None)


def test_no_container_on_the_source_machine_is_the_source_itself(estate):
    with pytest.raises(restore_by_id.RestoreByIdError, match="the target is the source instance"):
        restore_by_id.assert_target_is_not_source(
            _job(target_container=""), {"host": "192.0.2.10"}, data_dir=None)


def test_the_source_port_on_the_source_machine_is_refused(estate):
    with pytest.raises(restore_by_id.RestoreByIdError, match="overwrite production"):
        restore_by_id.assert_target_is_not_source(
            _job(env={"MSSQL_PORT": "1433"}), {"host": "192.0.2.10"}, data_dir=None)


def test_another_container_on_another_port_is_allowed(estate):
    restore_by_id.assert_target_is_not_source(_job(), {"host": "192.0.2.10"}, data_dir=None)


def test_another_machine_is_allowed(estate):
    restore_by_id.assert_target_is_not_source(
        _job(target_server_id="TGT", target_container="prod", env={}), {"host": "192.0.2.50"},
        data_dir=None)


def test_the_guard_runs_before_any_step(monkeypatch, estate):
    from db_ops.backup_restore import restore_script

    ran = []
    monkeypatch.setattr(restore_script, "load_script_restores",
                        lambda _p=None: [_job(target_container="prod")])
    monkeypatch.setattr(restore_by_id, "_host_block", lambda j, **_: {"host": "192.0.2.10"})
    monkeypatch.setattr(restore_by_id, "_execute", lambda op, request: ran.append(op) or {"ok": True})
    monkeypatch.setitem(restore_by_id._PLANNERS, "sqlserver",
                        lambda *a, **k: ran.append("planned") or [])

    with pytest.raises(restore_by_id.RestoreByIdError, match="the target is the source instance"):
        restore_by_id.restore_by_id({"restore_id": "R"})
    assert ran == []


def test_no_port_is_guessed(monkeypatch):
    """It was 1433 when nothing said otherwise, and the source's port when the target was unnamed."""
    import db_ops.lib.data_sources as data_sources

    monkeypatch.setattr(data_sources, "load_db_instances", lambda _d=None: [
        {"server_id": "SRC", "db_type": "sqlserver", "port": 1433}])

    with pytest.raises(restore_by_id.RestoreByIdError, match="port is not stated"):
        restore_by_id._sqlserver_port(_job(target_server_id="TGT", env={}), data_dir=None)


def test_the_target_directory_is_never_the_sources(monkeypatch):
    with pytest.raises(restore_by_id.RestoreByIdError, match="target_visible_dir"):
        restore_by_id._visible_dir(_job(target_visible_dir=""))
    assert restore_by_id._visible_dir(
        _job(target_server_id="TGT", target_visible_dir="", target_backup_dir="/staged")) == "/staged"


def test_the_host_block_never_falls_back_to_the_source():
    with pytest.raises(restore_by_id.RestoreByIdError, match="names no target_server_id"):
        restore_by_id._host_block(_job(target_server_id=""), data_dir=None)
