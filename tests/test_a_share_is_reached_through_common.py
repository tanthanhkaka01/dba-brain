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
import subprocess
from pathlib import Path

import pytest

from dbabrain.common import smb

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


def test_a_forced_delete_removes_the_one_file_named_and_says_when_it_could_not(unc):
    """0.38.0: a share has no host to hash a file on, so this is the forced delete of ONE stray
    file and nothing else - a file dbabrain recorded goes through its host, held to its facts."""
    answer = smb.delete({"host": "h", "share": "s", "backend": "unc", "paths": ["APPDB/LOG/b.trn"], "force": True})
    assert [(item["path"], item["status"]) for item in answer["results"]] == [("APPDB\\LOG\\b.trn", "DELETED")]
    missing = smb.delete({"host": "h", "share": "s", "backend": "unc", "paths": ["APPDB/LOG/not_there.trn"],
                          "force": True})
    assert (missing["deleted"], missing["failed"]) == (0, 1)
    assert (unc / "APPDB" / "FULL" / "a.bak").exists()


def test_a_delete_through_a_share_is_refused_without_force_for_two_files_and_for_a_pattern(unc):
    share = {"host": "h", "share": "s", "backend": "unc"}
    with pytest.raises(smb.SmbError, match="refused"):
        smb.delete({**share, "paths": ["APPDB/FULL/a.bak"]})
    with pytest.raises(smb.SmbError, match="ONE file per command; got 2"):
        smb.delete({**share, "paths": ["APPDB/FULL/a.bak", "APPDB/LOG/b.trn"], "force": True})
    with pytest.raises(smb.SmbError, match='needs "paths"'):
        smb.delete({**share, "paths": "APPDB/*", "force": True})
    with pytest.raises(smb.SmbError, match="never a pattern"):
        smb.delete({**share, "paths": ["APPDB/*"], "force": True})
    assert (unc / "APPDB" / "FULL" / "a.bak").exists() and (unc / "APPDB" / "LOG" / "b.trn").exists()


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
LOCAL_ROOT = "E:\\SQLBK_IMPORT"
HOST = {"runtime": "windows", "access": "winrm", "host": "192.0.2.40", "username": "u", "password": "p"}


def _share_answers(root: Path, asked: list):
    """``common.cli`` for a Windows import folder: listed through its share (a local folder stands
    in for it), deleted through its host - each file by a verified command (0.38.0)."""

    def call(command, request, **_kwargs):
        if command == "smb-list":
            files = [{"path": str(path.relative_to(root)).replace("/", "\\"), "name": path.name,
                      "size_bytes": path.stat().st_size, "modified_epoch": path.stat().st_mtime}
                     for path in sorted(root.rglob("*")) if path.is_file()]
            return {"files": files, "count": len(files)}
        raise AssertionError(command)

    def call_allowing_failure(command, request, **_kwargs):
        assert command == "delete-verified-files", f"{command}: nothing is deleted through the share"
        asked.append(request)
        status = "skipped" if request.get("dry_run") else "deleted"
        return True, {"files": [{"path": item["path"], "status": status} for item in request["files"]]}, ""

    return call, call_allowing_failure


def _facts_for_everything(**kwargs):
    """A catalog whose proven copies recorded every staged file asked about."""
    return {path: {"size_bytes": 1, "sha256": "a" * 64} for path in kwargs["paths"]}


@pytest.fixture()
def staged(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from dbabrain.backup_restore import delete_backup, share

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
    asked: list[dict] = []
    call, allowing = _share_answers(root, asked)
    monkeypatch.setattr(share.common_cli, "run", call)
    monkeypatch.setattr(share.common_cli, "run_allowing_failure", allowing)
    # The target's own host, from the inventory; and a catalog that recorded the staged files.
    monkeypatch.setattr(delete_backup, "_windows_target_host", lambda config: (HOST, ""))
    monkeypatch.setattr(delete_backup, "_catalog",
                        lambda: SimpleNamespace(staged_file_facts=_facts_for_everything))
    return {"root": root, "asked": asked, "now": stamp + 3 * 86400}


def _target(root: Path):
    import dataclasses

    from dbabrain.backup_restore.config import BackupRestoreConfig

    return dataclasses.replace(
        BackupRestoreConfig(
            prod_backup_share=root.parent / "prod", vm_import_unc=root, vm_import_local=Path(r"E:\SQLBK_IMPORT"),
            vm_log_unc=root.parent / "logs", vm_log_local=Path(r"E:\LOGS"), prod_smb_credential_target="",
            prod_smb_username="", prod_smb_password_env="", vm_credential_target="", vm_username="",
            vm_password_env="", restore_sql_instance_on_vm="localhost", source_database_name="APPDB",
            restore_database_name="APPDB_DR", restore_data_file_on_vm=Path(r"D:\d.mdf"),
            restore_log_file_on_vm=Path(r"D:\l.ldf"), cleanup_retention=86400, target_id="TGT-01"),
    )


def _names(request: dict) -> list[str]:
    return sorted(item["file_name"] for item in request["files"])


def test_a_windows_cleanup_deletes_only_what_is_aged_and_behind_the_newest_full(staged):
    from dbabrain.backup_restore.delete_backup import delete_old_target_backup_files_on_share

    results = delete_old_target_backup_files_on_share(_target(staged["root"]), now=staged["now"])

    assert len(staged["asked"]) == 1, "one verified request, through the host"
    request = staged["asked"][0]
    assert _names(request) == ["APPDB_FULL_20260601_010000.bak", "APPDB_LOG_20260601_020000.trn"]
    # Each file by its local path on the target, fenced to the import folder, with its facts.
    assert all(item["path"].startswith(LOCAL_ROOT + "\\APPDB\\") and item["must_be_under"] == LOCAL_ROOT
               and item["size_bytes"] == 1 and len(item["sha256"]) == 64 for item in request["files"])
    assert request["host"] == HOST
    assert {item.status for item in results} == {"DELETED", "SKIPPED"}


def test_a_dry_run_on_a_windows_target_deletes_nothing(staged):
    from dbabrain.backup_restore.delete_backup import delete_old_target_backup_files_on_share

    results = delete_old_target_backup_files_on_share(_target(staged["root"]), dry_run=True, now=staged["now"])

    assert [request["dry_run"] for request in staged["asked"]] == [True]
    assert sorted(item.status for item in results) == ["DRY_RUN", "DRY_RUN", "SKIPPED"]


def test_a_staged_file_no_copy_recorded_is_kept_and_named(staged, monkeypatch):
    from types import SimpleNamespace

    from dbabrain.backup_restore import delete_backup

    monkeypatch.setattr(delete_backup, "_catalog", lambda: SimpleNamespace(staged_file_facts=lambda **_k: {}))
    results = delete_backup.delete_old_target_backup_files_on_share(_target(staged["root"]), now=staged["now"])

    assert staged["asked"] == [], "a file with no recorded facts is not asked to be deleted"
    kept = [item for item in results if item.status == delete_backup.KEPT]
    assert len(kept) == 2 and all("not in the catalog" in item.reason for item in kept)


def test_a_target_reachable_only_as_a_share_keeps_its_files_and_says_why(staged, monkeypatch):
    from dbabrain.backup_restore import delete_backup

    monkeypatch.setattr(delete_backup, "_windows_target_host",
                        lambda config: (None, "TGT-01 cannot be reached as a host (no cmd_access)"))
    results = delete_backup.delete_old_target_backup_files_on_share(_target(staged["root"]), now=staged["now"])

    assert staged["asked"] == []
    kept = [item for item in results if item.status == delete_backup.KEPT]
    assert len(kept) == 2 and all("cannot be reached as a host" in item.reason for item in kept)


def test_the_chain_is_read_from_the_share_listing_never_by_walking_the_path(monkeypatch):
    """0.24.0 as released: on a Linux node this deleted nothing. The chain was read by walking the
    UNC path, which a Linux node cannot, and the listed names were joined into a `Path` - POSIX
    there, no split at a backslash - so every aged file was held back as `still_needed` and the
    share filled. `ci` found it (Linux runners; the soak node was Windows). Here the share is one
    nothing on this machine serves, as it is for a Linux node: the listing is all there is, and it
    is enough. (On Linux the tests above also hold the separator.)"""
    from types import SimpleNamespace

    from dbabrain.backup_restore import delete_backup, share, verified_cleanup

    def walked(root):
        raise AssertionError(f"the cleanup walked {root} instead of reading the share's listing")

    listing = [
        {"path": "APPDB\\FULL\\APPDB_FULL_20260601_010000.bak", "modified_epoch": 1_780_000_000.0, "size_bytes": 1},
        {"path": "APPDB\\LOG\\APPDB_LOG_20260601_020000.trn", "modified_epoch": 1_780_000_060.0, "size_bytes": 1},
        {"path": "APPDB\\FULL\\APPDB_FULL_20260620_010000.bak", "modified_epoch": 1_780_000_600.0, "size_bytes": 1},
    ]
    asked: list[dict] = []

    def delete_files(**kwargs):
        asked.append(kwargs)
        return {"deleted": list(kwargs["paths"]), "already_gone": [], "would_delete": [], "not_deleted": [],
                "kept": [], "forced": []}

    monkeypatch.setattr(delete_backup, "_all_target_backup_files", walked)
    monkeypatch.setattr(share, "list_files", lambda *args, **kwargs: listing)
    monkeypatch.setattr(delete_backup, "_windows_target_host", lambda config: (HOST, ""))
    monkeypatch.setattr(delete_backup, "_catalog",
                        lambda: SimpleNamespace(staged_file_facts=_facts_for_everything))
    monkeypatch.setattr(verified_cleanup, "delete_files", delete_files)

    results = delete_backup.delete_old_target_backup_files_on_share(
        _target(Path("\\\\192.0.2.40\\SQLBK_IMPORT\\APPDB_STAGE")), now=1_780_000_000.0 + 3 * 86400)

    assert sorted(item.rsplit("\\", 1)[-1] for item in asked[0]["paths"]) == [
        "APPDB_FULL_20260601_010000.bak", "APPDB_LOG_20260601_020000.trn"]
    assert asked[0]["windows"] is True and asked[0]["must_be_under"] == LOCAL_ROOT
    assert {item.status for item in results} == {"DELETED", "SKIPPED"}
