"""A Windows share is reached through ``common.cli``, one way per platform, by every app (R10).

Until 0.24.0 the SQL Server restore reached its backup shares itself - ``smbclient`` on a Linux
worker, PowerShell over a UNC path on a Windows master, ``cmdkey`` first - three ways in one app, and
a fourth would have grown in the next app to meet a share. They are ``smb-list``, ``smb-get``,
``smb-delete`` and ``smb-credential`` now. These hold what the move must not lose:

* the listing is relative to the folder listed, whatever the depth of the sub-path - the bug that
  staged a whole tree several folders too deep, silently, came from exactly that comparison;
* the login never reaches a command line, and the file that carries it to ``smbclient`` is gone
  after the call (the app's version left one per run in the temp folder);
* ``smb-delete`` deletes the files named and nothing else, and a file it could not delete is an
  answer, not an exception that hides the rest;
* the app still decides what goes: a dry run on a Windows target deletes nothing - which the
  PowerShell engine it replaced did not honour.
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

import pytest

from db_ops.common import smb

_LISTING = (
    "  .                          D        0  Wed Jun 24 01:00:00 2026\n"
    "\\DBA\\SqlBK\\INST\\APPDB\\FULL\n"
    "  APPDB_FULL_20260624_010005.bak      A   11121102848  Wed Jun 24 01:01:11 2026\n"
    "  readme.txt                          A   12  Wed Jun 24 01:01:11 2026\n"
    "\\DBA\\SqlBK\\INST\\APPDB\\LOG\n"
    "  APPDB_LOG_20260624_080001.trn       A   1048576  Wed Jun 24 08:00:02 2026\n"
    "  archive                             D        0  Wed Jun 24 08:00:02 2026\n"
)


# --------------------------------------------------------------------------- #
# smbclient
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("subpath", ["DBA/SqlBK/INST", "DBA\\SqlBK\\INST", "\\DBA\\SqlBK\\INST\\"])
def test_a_multi_segment_folder_lists_relative_to_itself_whatever_its_separators(subpath):
    files = smb.parse_ls(_LISTING, subpath=subpath)

    assert [item["path"] for item in files] == [
        "APPDB\\FULL\\APPDB_FULL_20260624_010005.bak", "APPDB\\FULL\\readme.txt",
        "APPDB\\LOG\\APPDB_LOG_20260624_080001.trn"]
    assert files[0]["size_bytes"] == 11121102848
    assert files[0]["modified_epoch"] is not None


class _Popen:
    seen: list[dict] = []

    def __init__(self, args, **_kwargs):
        auth = Path(args[args.index("-A") + 1])
        _Popen.seen.append({"args": list(args), "auth": auth, "auth_text": auth.read_text(encoding="utf-8")})
        self.pid, self.returncode = 1, 0

    def communicate(self, timeout=None):
        return _LISTING, ""


@pytest.fixture()
def smbclient(monkeypatch):
    _Popen.seen = []
    monkeypatch.setattr(smb.subprocess, "Popen", _Popen)
    return _Popen.seen


def test_the_password_goes_to_smbclient_in_a_file_that_is_gone_afterwards(smbclient):
    answer = smb.listing({"host": "192.0.2.10", "share": "D$", "path": "DBA/SqlBK/INST", "backend": "smbclient",
                          "username": "CORP\\backup", "password": "not-on-argv"})

    [call] = smbclient
    assert "not-on-argv" not in " ".join(call["args"])
    assert call["args"][:2] == ["smbclient", "//192.0.2.10/D$"]
    assert "password = not-on-argv" in call["auth_text"] and "domain = CORP" in call["auth_text"]
    assert not call["auth"].exists()
    assert answer["count"] == 3


def test_a_listing_can_keep_only_the_suffixes_asked_for(smbclient):
    answer = smb.listing({"host": "h", "share": "s", "path": "DBA/SqlBK/INST", "backend": "smbclient",
                          "suffixes": [".BAK", ".trn"]})

    assert [item["name"] for item in answer["files"]] == [
        "APPDB_FULL_20260624_010005.bak", "APPDB_LOG_20260624_080001.trn"]


def test_a_listing_the_share_refused_is_an_error_that_says_what_the_share_said(monkeypatch):
    class Refused(_Popen):
        def __init__(self, args, **kwargs):
            super().__init__(args, **kwargs)
            self.returncode = 1

        def communicate(self, timeout=None):
            return "", "NT_STATUS_LOGON_FAILURE"

    monkeypatch.setattr(smb.subprocess, "Popen", Refused)

    with pytest.raises(smb.SmbError, match="NT_STATUS_LOGON_FAILURE"):
        smb.listing({"host": "h", "share": "s", "backend": "smbclient"})


# --------------------------------------------------------------------------- #
# The UNC way (Windows), on a folder standing in for the share
# --------------------------------------------------------------------------- #
@pytest.fixture()
def unc(monkeypatch, tmp_path):
    root = tmp_path / "share"
    (root / "APPDB" / "FULL").mkdir(parents=True)
    (root / "APPDB" / "LOG").mkdir(parents=True)
    (root / "APPDB" / "FULL" / "a.bak").write_bytes(b"x" * 10)
    (root / "APPDB" / "LOG" / "b.trn").write_bytes(b"x" * 3)
    monkeypatch.setattr(smb, "_unc", lambda host, share, subpath="": root / subpath.replace("\\", os.sep))
    return root


def test_the_unc_way_answers_in_the_same_shape_as_smbclient(unc):
    answer = smb.listing({"host": "h", "share": "s", "backend": "unc", "path": "APPDB"})

    assert sorted((item["path"], item["size_bytes"]) for item in answer["files"]) == [
        ("FULL\\a.bak" if os.sep == "\\" else "FULL/a.bak", 10), ("LOG\\b.trn" if os.sep == "\\" else "LOG/b.trn", 3)]
    assert all(isinstance(item["modified_epoch"], float) for item in answer["files"])


def test_a_fetched_file_is_where_the_request_said_with_its_size(unc, tmp_path):
    answer = smb.get({"host": "h", "share": "s", "backend": "unc", "remote_path": "APPDB/FULL/a.bak",
                      "local_path": str(tmp_path / "stage" / "APPDB" / "a.bak")})

    assert (answer["bytes"], answer["exit_code"]) == (10, 0)
    assert (tmp_path / "stage" / "APPDB" / "a.bak").read_bytes() == b"x" * 10


def test_a_delete_removes_exactly_the_files_named_and_reports_one_it_could_not(unc):
    answer = smb.delete({"host": "h", "share": "s", "backend": "unc",
                         "paths": ["APPDB/LOG/b.trn", "APPDB/LOG/not_there.trn"]})

    assert [(item["path"], item["status"]) for item in answer["results"]] == [
        ("APPDB\\LOG\\b.trn", "DELETED"), ("APPDB\\LOG\\not_there.trn", "FAILED")]
    assert (answer["deleted"], answer["failed"]) == (1, 1)
    assert (unc / "APPDB" / "FULL" / "a.bak").exists()


def test_a_delete_names_its_files_and_is_never_a_pattern():
    with pytest.raises(smb.SmbError, match='needs "paths"'):
        smb.delete({"host": "h", "share": "s", "paths": "APPDB/*"})


# --------------------------------------------------------------------------- #
# The stored login
# --------------------------------------------------------------------------- #
def test_off_windows_there_is_no_login_to_store_and_that_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(smb.os, "name", "posix")

    answer = smb.register_credential({"target": "h", "username": "u", "password": "p"})

    assert answer["registered"] is False


def test_on_windows_the_login_is_stored_with_its_domain(monkeypatch):
    ran = []
    monkeypatch.setattr(smb.os, "name", "nt")
    monkeypatch.setattr(smb.subprocess, "run",
                        lambda args, **_kw: ran.append(args) or subprocess.CompletedProcess(args, 0, "", ""))

    answer = smb.register_credential({"target": "192.0.2.10", "username": "CORP\\backup", "password": "p"})

    assert answer["registered"] is True
    assert ran == [["cmdkey", "/add:192.0.2.10", "/user:CORP\\backup", "/pass:p"]]


# --------------------------------------------------------------------------- #
# The app keeps deciding what goes
# --------------------------------------------------------------------------- #
def _share_answers(root: Path, deleted: list):
    """``common.cli`` for a Windows import share standing in as a local folder."""

    def call(command, request, **_kwargs):
        if command == "smb-list":
            files = [{"path": str(path.relative_to(root)).replace("/", "\\"), "name": path.name,
                      "size_bytes": path.stat().st_size, "modified_epoch": path.stat().st_mtime}
                     for path in sorted(root.rglob("*")) if path.is_file()]
            return {"files": files, "count": len(files)}
        raise AssertionError(command)

    def call_allowing_failure(command, request, **_kwargs):
        assert command == "smb-delete"
        deleted.extend(request["paths"])
        return True, {"results": [{"path": item, "status": "DELETED", "bytes": 1, "error": ""}
                                  for item in request["paths"]]}, ""

    return call, call_allowing_failure


@pytest.fixture()
def staged(tmp_path, monkeypatch):
    from db_ops.backup_restore import share

    root = tmp_path / "import"
    (root / "APPDB" / "FULL").mkdir(parents=True)
    (root / "APPDB" / "LOG").mkdir(parents=True)
    old_full = root / "APPDB" / "FULL" / "APPDB_FULL_20260601_010000.bak"
    new_full = root / "APPDB" / "FULL" / "APPDB_FULL_20260620_010000.bak"
    old_log = root / "APPDB" / "LOG" / "APPDB_LOG_20260601_020000.trn"
    for path in (old_full, new_full, old_log):
        path.write_bytes(b"x")
    stamp = 1_780_000_000.0
    os.utime(old_full, (stamp, stamp))
    os.utime(old_log, (stamp + 60, stamp + 60))
    os.utime(new_full, (stamp + 600, stamp + 600))
    deleted: list[str] = []
    call, allowing = _share_answers(root, deleted)
    monkeypatch.setattr(share.common_cli, "run", call)
    monkeypatch.setattr(share.common_cli, "run_allowing_failure", allowing)
    return {"root": root, "deleted": deleted, "now": stamp + 3 * 86400}


def _target(root: Path):
    import dataclasses

    from db_ops.backup_restore.config import BackupRestoreConfig

    return dataclasses.replace(
        BackupRestoreConfig(
            prod_backup_share=root.parent / "prod", vm_import_unc=root, vm_import_local=Path(r"E:\SQLBK_IMPORT"),
            vm_log_unc=root.parent / "logs", vm_log_local=Path(r"E:\LOGS"), prod_smb_credential_target="",
            prod_smb_username="", prod_smb_password_env="", vm_credential_target="", vm_username="",
            vm_password_env="", restore_sql_instance_on_vm="localhost", source_database_name="APPDB",
            restore_database_name="APPDB_DR", restore_data_file_on_vm=Path(r"D:\d.mdf"),
            restore_log_file_on_vm=Path(r"D:\l.ldf"), cleanup_retention=86400),
    )


#: 0.24.0 as released: on a Linux node the cleanup of a Windows target's share deletes nothing. The
#: files `smb-list` names are joined into a POSIX `Path`, which does not split at "\\", and the
#: obsolete chain is read by listing the UNC path, which a Linux node cannot - so every file is held
#: back. Safe (nothing wrong is deleted) and named in the release notes; fixed in 0.24.1. Strict, so
#: the fix turns these red until the mark goes.
_LINUX_NODE_KEEPS_EVERYTHING = pytest.mark.xfail(
    sys.platform != "win32", strict=True,
    reason="0.24.0 known limitation: a Linux node's cleanup of a Windows share deletes nothing (0.24.1)")


@_LINUX_NODE_KEEPS_EVERYTHING
def test_a_windows_cleanup_deletes_only_what_is_aged_and_behind_the_newest_full(staged):
    from db_ops.backup_restore.delete_backup import delete_old_target_backup_files_on_share

    results = delete_old_target_backup_files_on_share(_target(staged["root"]), now=staged["now"])

    assert sorted(Path(item.replace("\\", "/")).name for item in staged["deleted"]) == [
        "APPDB_FULL_20260601_010000.bak", "APPDB_LOG_20260601_020000.trn"]
    assert {item.status for item in results} == {"DELETED", "SKIPPED"}


@_LINUX_NODE_KEEPS_EVERYTHING
def test_a_dry_run_on_a_windows_target_deletes_nothing(staged):
    from db_ops.backup_restore.delete_backup import delete_old_target_backup_files_on_share

    results = delete_old_target_backup_files_on_share(_target(staged["root"]), dry_run=True, now=staged["now"])

    assert staged["deleted"] == []
    assert sorted(item.status for item in results) == ["DRY_RUN", "DRY_RUN", "SKIPPED"]
