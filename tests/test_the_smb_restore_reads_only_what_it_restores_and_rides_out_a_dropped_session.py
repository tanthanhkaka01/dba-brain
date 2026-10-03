"""The SMB restore copies only the databases it restores, and one dropped SSH session is not a failed database.

Found on 2026-09-27, restoring two of a production server's databases into a lab VM - the first real run of this
path on 0.24.0:

* **the copy read the whole share.** The Linux node's copy (``smbclient``) reads only the mapped
  database folders; the Windows node's two read every recent file, so an entry mapping two databases
  would have staged the third's 30 G full and a day of its logs into a VM with 17 G free;
* **a dead share said only "selected no files".** The server's Agent share had had no new file for
  eight days - dbabrain's own backup job writes that server's backups elsewhere now - and nothing in
  the message pointed there;
* **the chain was checked one SSH session per file**, and one session the lab dropped
  (``WinError 10054``) failed the whole database after 2 min 46 s of checking;
* **the target needed sqlcmd on the host.** The lab VM has only Docker; ``run-sqlcmd`` now takes a
  ``container`` and runs that container's own ``sqlcmd``.
"""

from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path

import pytest

from conftest import patch_restore

from db_ops.backup_restore import copy_backup, restore_database
from db_ops.backup_restore.config import BackupRestoreConfig, DatabaseRestoreMapping
from db_ops.common import sqlcmd_run
from db_ops.lib.remote_host import RemoteError, RemoteHost


def _config(share: Path, tmp_path: Path, **overrides) -> BackupRestoreConfig:
    config = BackupRestoreConfig(
        prod_backup_share=share, vm_import_unc=tmp_path / "imp", vm_import_local=Path("/imp"),
        vm_log_unc=tmp_path / "log", vm_log_local=Path("/log"), prod_smb_credential_target="",
        prod_smb_username="", prod_smb_password_env="", vm_credential_target="192.0.2.251",
        vm_username="tuser", vm_password_env="", restore_sql_instance_on_vm="localhost,1433",
        vm_platform="linux", restore_id="R1", copy_recent_hours=24,
        databases=(DatabaseRestoreMapping("APPDB", "AppDb"),))
    return dataclasses.replace(config, **overrides)


def _backup(share: Path, relative: str, *, age_hours: float = 1.0) -> Path:
    path = share / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp = time.time() - age_hours * 3600
    os.utime(path, (stamp, stamp))
    return path


# --------------------------------------------------------------------------- #
# The copy reads the mapped databases only
# --------------------------------------------------------------------------- #
def test_the_windows_nodes_scan_reads_only_the_mapped_database_folders(tmp_path):
    share = tmp_path / "share"
    mine = _backup(share, "APPDB/FULL/APPDB_FULL_20260926_180006Z.bak")
    _backup(share, "SALESDB/FULL/SALESDB_FULL_20260926_180006Z.bak")
    _backup(share, "SALESDB/LOG/SALESDB_LOG_20260926_230000Z.trn")

    selected = copy_backup.list_recent_backup_files(_config(share, tmp_path))
    assert [path.name for path in selected] == [mine.name]


def test_an_entry_that_maps_no_database_still_takes_everything_it_finds(tmp_path):
    share = tmp_path / "share"
    _backup(share, "APPDB/FULL/a.bak")
    _backup(share, "SALESDB/FULL/b.bak")
    assert len(copy_backup.list_recent_backup_files(_config(share, tmp_path, databases=()))) == 2


def test_the_share_listing_asks_for_each_mapped_folder_and_never_the_whole_share(tmp_path, monkeypatch):
    from db_ops.backup_restore import share as share_module

    asked: list[str] = []

    def fake_list(unc, **_kwargs):
        asked.append(str(unc))
        return [{"path": "FULL\\APPDB_FULL_1.bak", "name": "APPDB_FULL_1.bak",
                 "modified_epoch": time.time() - 60}]

    monkeypatch.setattr(share_module, "list_files", fake_list)
    config = _config(Path(r"\\192.0.2.250\SQLBK_DBOPS"), tmp_path)
    selected = copy_backup.list_recent_backup_files_on_share(config)
    assert asked == [r"\\192.0.2.250\SQLBK_DBOPS\APPDB"]
    assert [str(path) for path in selected] == [r"\\192.0.2.250\SQLBK_DBOPS\APPDB\FULL\APPDB_FULL_1.bak"]


def test_a_share_that_stopped_being_written_is_named_as_such(tmp_path, monkeypatch):
    from db_ops.backup_restore import share as share_module

    eight_days = time.time() - 8 * 86400
    monkeypatch.setattr(share_module, "list_files", lambda unc, **_kw: [
        {"path": "LOG\\APPDB_LOG_20260919_073000.trn", "name": "APPDB_LOG_20260919_073000.trn",
         "modified_epoch": eight_days}])
    hint = copy_backup.newest_backup_hint(_config(Path(r"\\192.0.2.250\APPDB-DB$APPDB"), tmp_path))
    assert r"APPDB\LOG\APPDB_LOG_20260919_073000.trn" in hint
    assert "7.0 day(s) before the window opens" in hint
    assert "no longer writes its backups to this share" in hint


def test_the_hint_never_replaces_the_error_it_explains(tmp_path, monkeypatch):
    from db_ops.backup_restore import share as share_module

    def broken(unc, **_kw):
        raise share_module.ShareError("NT_STATUS_LOGON_FAILURE")

    monkeypatch.setattr(share_module, "list_files", broken)
    hint = copy_backup.newest_backup_hint(_config(Path(r"\\h\s"), tmp_path))
    assert "could not be listed" in hint and "NT_STATUS_LOGON_FAILURE" in hint


# --------------------------------------------------------------------------- #
# The chain is checked in one session, and a session that never opened is asked again
# --------------------------------------------------------------------------- #
class _OneSession:
    def __init__(self, missing: list[str]) -> None:
        self.scripts: list[str] = []
        self.missing = missing

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def run_script(self, script: str, **_kw):
        self.scripts.append(script)
        return type("Run", (), {"stdout": "".join(f"{path}\n" for path in self.missing)})()


def test_the_whole_chain_is_checked_in_one_session(tmp_path, monkeypatch):
    chain = [Path("/imp/APPDB/FULL/f.bak")] + [Path(f"/imp/APPDB/LOG/{n}.trn") for n in range(97)]
    session = _OneSession(missing=["/imp/APPDB/LOG/5.trn"])
    patch_restore(monkeypatch, "open_ssh_connection", lambda config: session)

    missing = restore_database._missing_backup_paths(_config(tmp_path, tmp_path), chain)

    assert len(session.scripts) == 1
    assert missing == {"/imp/appdb/log/5.trn"}


def _answers(*answers):
    calls = list(answers)
    made: list[str] = []

    def call(command, request, timeout_seconds=None):
        made.append(command)
        return calls.pop(0)

    return call, made


def test_a_session_that_never_opened_is_asked_again(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    call, made = _answers(
        (False, {}, "SSH connection to 192.0.2.251:22 failed. Detail: [WinError 10054] reset"),
        (False, {}, "SSH connect to tuser@192.0.2.251:22 timed out after 30 seconds."),
        (True, {"exit_code": 0, "stdout": "yes", "stderr": ""}, ""))
    result = RemoteHost(host="192.0.2.251", username="tuser", password="pw", call=call).run("true")
    assert result.ok and len(made) == 3


def test_it_gives_up_after_the_last_try_and_says_why(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    refused = (False, {}, "SSH connection to 192.0.2.251:22 failed. Detail: refused")
    call, made = _answers(refused, refused, refused)
    with pytest.raises(RemoteError, match="refused"):
        RemoteHost(host="192.0.2.251", username="tuser", password="pw", call=call).run("true")
    assert len(made) == 3


def test_a_wrong_password_is_not_asked_again(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    call, made = _answers((False, {}, "SSH authentication failed for tuser@192.0.2.251:22: bad"))
    with pytest.raises(RemoteError):
        RemoteHost(host="192.0.2.251", username="tuser", password="pw", call=call).run("true")
    assert len(made) == 1


# --------------------------------------------------------------------------- #
# sqlcmd inside the target's container
# --------------------------------------------------------------------------- #
def test_a_container_runs_its_own_sqlcmd():
    request = {"instance": "localhost,1433", "sql": "SELECT 1", "container": "MSSQL_1433",
               "username": "sa", "password": "pw"}
    argv = sqlcmd_run.local_argv(request)
    # `-e SQLCMDPASSWORD` names the variable only; the value comes from docker's environment (F11.2).
    assert argv[:6] == ["docker", "exec", "-e", "SQLCMDPASSWORD", "MSSQL_1433", sqlcmd_run.CONTAINER_SQLCMD]
    assert argv[6:8] == ["-S", "localhost,1433"]
    assert "pw" not in argv


def test_a_named_sqlcmd_path_is_used_inside_the_container_as_given():
    assert sqlcmd_run.sqlcmd_words({"container": "c", "sqlcmd_path": "/opt/mssql-tools/bin/sqlcmd"}) == [
        "docker", "exec", "c", "/opt/mssql-tools/bin/sqlcmd"]
    assert sqlcmd_run.sqlcmd_words({"sqlcmd_path": "sqlcmd"}) == ["sqlcmd"]


def test_a_windows_host_with_a_container_is_refused_with_the_reason():
    with pytest.raises(sqlcmd_run.SqlcmdRunError, match="a Windows host runs its own sqlcmd"):
        sqlcmd_run.run_sqlcmd({"instance": "localhost", "sql": "SELECT 1", "via": "winrm",
                               "container": "c", "host": {"host": "192.0.2.10"}})


def test_the_restore_hands_its_target_container_to_every_sqlcmd_it_runs(tmp_path):
    config = _config(tmp_path, tmp_path, sql_container="MSSQL_1433")
    command = restore_database.build_sqlcmd_query_command(sql="SELECT 1", config=config)
    request = restore_database._sqlcmd_request(command, config, via="ssh")
    assert request["container"] == "MSSQL_1433"
    without = restore_database._sqlcmd_request(
        command, dataclasses.replace(config, sql_container=""), via="ssh")
    assert "container" not in without
