"""A restore lands on a SQL Server running natively on Windows (0.39.0).

The operator, 2026-11-14: test restores into SQL Server installed on Windows - the lab host, SQL Server 2025
Developer, a named instance on a static TCP port - from the repository's encrypted copies, to the newest
point and to a point in time. The script-driven restore knew a Linux host and a container on one; a
Windows target fell through as ``linux``: ``df`` and ``chmod`` handed to cmd.exe, a staging path refused
for not starting with ``/``, a ``cat`` channel opened into a host that has no ``cat``.

What holds it now, each part read from the target's inventory record and never guessed from a path:

* the host block is ``windows`` (PowerShell, no sudo) when the record says ``platform: windows`` and the
  entry names no container - and what that shape cannot do is refused before anything is copied;
* the logins the staging commands take say ``platform: windows``: the staging is a drive path, its free
  space is asked of the drive in PowerShell, the engine's access is the folder's ACL (nothing chmodded),
  and every file is written over SFTP, never through ``cat``;
* SQL Server is handed the staging in its own form (backslashes); the clean-up reads a Windows listing
  in the staging's stated form, so the marker is found and each file is deleted by its recorded facts.

Measured on the lab host's Windows OpenSSH on 2026-11-14, in a probe folder removed afterwards: SFTP takes
``C:/x``, ``/C:/x`` and ``C:\\x`` alike, ``posix-rename`` replaces a file, a plain ``rename`` refuses an
existing name, ``utime`` sets a time, and a command reaches cmd.exe. The PowerShell this module sends is
run for real at the end of this file, where the machine running the suite is a Windows one.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import backup, restore_by_id, restore_script, verified_cleanup
from dbabrain.common import backup_copy, cli_backup_copy, filecopy, filetransfer, listfiles, repoencrypt
from dbabrain.common.hostcmd import WINDOWS, Host, wrap
from dbabrain.lib import restore_space
from dbabrain.lib.restore import landing as restore_landing
from dbabrain.lib.restore import windows as windows_target

STAGE = "C:/dbabrain_restore/ACME_MSSQL_TO_WIN"
WINDOWS_LOGIN = {"host": "win-sql.example.test", "port": 22, "username": "corp\\dba", "password": "pw",
                 "platform": "windows"}


# --------------------------------------------------------------------------- #
# The rules: what a Windows staging is, and what a Windows target takes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("written", ["C:/dbabrain_restore/R", "C:\\dbabrain_restore\\R", "C:/dbabrain_restore/R/",
                                     "/C:/dbabrain_restore/R", "C:\\dbabrain_restore/R\\"])
def test_a_windows_staging_is_carried_with_forward_slashes_whichever_way_it_was_written(written):
    assert windows_target.drive_path(written, field="target_backup_dir") == "C:/dbabrain_restore/R"
    assert windows_target.sftp_form(written, field="target_backup_dir") == "/C:/dbabrain_restore/R"
    assert windows_target.sql_server_path(written) == "C:\\dbabrain_restore\\R"


@pytest.mark.parametrize("written, words", [
    ("dbabrain_restore/R", "drive path"),
    ("/home/dba/restore/R", "drive path"),
    ("\\\\server\\share\\R", "drive path"),
    ("C:/dbabrain_restore/../Windows", "'..'"),
    ("C:/dbabrain_restore/R/a.bak:stream", "':' past its drive"),
    ("C:/dbabrain_restore/*", "one place"),
])
def test_what_is_not_one_place_on_a_drive_is_refused_by_name(written, words):
    with pytest.raises(ValueError, match=words):
        windows_target.drive_path(written, field="target_backup_dir")


def test_a_windows_target_takes_sql_server_natively_over_ssh_into_a_drive_path():
    assert windows_target.refusal(db_type="sqlserver", access="ssh", container="", target_dir=STAGE) == ""


@pytest.mark.parametrize("over, words", [
    ({"db_type": "postgresql"}, "postgresql restore onto Windows is not built"),
    ({"db_type": "oracle"}, "oracle restore onto Windows is not built"),
    ({"container": "mssql_lab"}, "state no target_container"),
    ({"access": "winrm"}, "must be ssh"),
    ({"target_dir": "/opt/db_ops/restore/R"}, "drive path"),
    ({"visible_dir": "/var/opt/mssql/restore"}, "target_visible_dir"),
])
def test_each_shape_a_windows_target_cannot_take_is_named(over, words):
    rule = {"db_type": "sqlserver", "access": "ssh", "container": "", "target_dir": STAGE, **over}
    assert words in windows_target.refusal(**rule)


def test_a_drive_path_on_a_target_that_is_not_windows_is_refused():
    assert "platform windows" in windows_target.linux_refusal(target_dir=STAGE)
    assert windows_target.linux_refusal(target_dir="/opt/db_ops/restore/R") == ""


# --------------------------------------------------------------------------- #
# The host block: chosen from the target's record
# --------------------------------------------------------------------------- #
def _target(**over):
    values = {"host": "win-sql.example.test", "port": 22, "username": "corp\\dba", "key_file": None,
              "password_ref": "WIN_SSH", "platform": "windows", "access": "ssh", "container_name": ""}
    values.update(over)
    return SimpleNamespace(**values)


def _job(**over):
    values = {"restore_id": "ACME_MSSQL_TO_WIN", "label": "ACME_MSSQL_TO_WIN (sqlserver)", "db_type": "sqlserver",
              "server_id": "SRC", "target_server_id": "WIN-MSSQL2025", "target_container": "",
              "target_backup_dir": STAGE, "target_visible_dir": ""}
    values.update(over)
    return SimpleNamespace(**values)


def test_a_windows_record_makes_the_host_block_windows_with_no_sudo(monkeypatch):
    monkeypatch.setattr(backup, "resolve_ssh_target", lambda *a, **k: _target())

    block = restore_by_id._host_block(_job(), data_dir=None, load_secrets=lambda: {"WIN_SSH": "pw"})

    assert block == {"runtime": "windows", "access": "ssh", "host": "win-sql.example.test", "port": 22,
                     "username": "corp\\dba", "password": "pw"}


def test_a_linux_record_still_makes_the_block_it_always_made(monkeypatch):
    monkeypatch.setattr(backup, "resolve_ssh_target", lambda *a, **k: _target(platform="linux"))
    job = _job(target_container="mssql_lab", target_backup_dir="/opt/db_ops/restore/R")

    block = restore_by_id._host_block(job, data_dir=None, load_secrets=lambda: {"WIN_SSH": "pw"})

    assert block["runtime"] == "docker" and block["sudo"] is True and block["container"] == "mssql_lab"


@pytest.mark.parametrize("job_over, target_over, words", [
    ({"db_type": "postgresql"}, {}, "not built"),
    ({"target_container": "mssql_lab"}, {}, "target_container"),
    ({}, {"access": "winrm"}, "must be ssh"),
    ({"target_backup_dir": "/opt/db_ops/restore/R"}, {}, "drive path"),
    ({}, {"platform": "linux"}, "does not say platform windows"),
])
def test_what_a_target_cannot_take_is_refused_before_anything_is_copied(monkeypatch, job_over, target_over, words):
    monkeypatch.setattr(backup, "resolve_ssh_target", lambda *a, **k: _target(**target_over))

    with pytest.raises(restore_by_id.RestoreByIdError, match=words) as raised:
        restore_by_id._host_block(_job(**job_over), data_dir=None, load_secrets=lambda: pytest.fail("asked"))
    assert "Nothing was copied" in str(raised.value)


# --------------------------------------------------------------------------- #
# SQL Server reads the staging by its own form of the path
# --------------------------------------------------------------------------- #
WINDOWS_HOST = {"runtime": "windows", "access": "ssh", "host": "win-sql.example.test"}


def test_the_engine_reads_the_staging_and_the_vault_pair_by_windows_paths():
    assert restore_by_id._engine_dir(STAGE, WINDOWS_HOST) == "C:\\dbabrain_restore\\ACME_MSSQL_TO_WIN"
    assert restore_by_id._engine_dir("/opt/db_ops/restore/R", {"runtime": "docker"}) == "/opt/db_ops/restore/R"

    step = restore_by_id._key_from_vault(_job(), name="db_ops_backup_cert", password="pw", target={},
                                         host=WINDOWS_HOST, directory=STAGE, dry_run=True)

    assert step["request"]["cer_path"] == "C:\\dbabrain_restore\\ACME_MSSQL_TO_WIN\\__dbabrain_cert\\db_ops_backup_cert.cer"
    assert step["request"]["pvk_path"].endswith("\\__dbabrain_cert\\db_ops_backup_cert.pvk")


def test_the_plan_lists_and_restores_the_windows_staging_by_sql_servers_own_path(monkeypatch):
    asked: list[dict] = []
    full = {"path": "C:\\dbabrain_restore\\ACME_MSSQL_TO_WIN\\APP\\FULL\\APP_FULL_20261114_010000Z.bak",
            "database_name": "APP", "finished_at": "2026-11-14T08:00:05", "checkpoint_lsn": 0, "last_lsn": 0}

    def listing(request):
        asked.append(request)
        files = [full] if request["kinds"] == ["full"] else []
        return {"files": files, "newest_finished_at": files[0]["finished_at"] if files else None}

    monkeypatch.setattr(restore_by_id, "_list_backup_files", listing)
    monkeypatch.setattr(restore_by_id, "_locked", lambda: False)
    job = _job(env={"MSSQL_USER": "sa", "MSSQL_PORT": "14330"}, env_secrets={}, overwrite_existing=False,
               landing=restore_landing.parse_landing(
                   {"database_mappings": [{"source_database": "APP", "target_database": "APP"}]},
                   label="test", db_type="sqlserver"), checkdb=True)

    steps = restore_by_id._plan_sqlserver(job, {}, point_in_time="", host=WINDOWS_HOST, data_dir=None)

    assert {request["path"] for request in asked} == {"C:\\dbabrain_restore\\ACME_MSSQL_TO_WIN"}
    restore = next(step for step in steps if step["op"] == "restore-full")["request"]
    assert restore["backup_path"] == full["path"] and restore["relocate"] == {}
    assert restore["target"]["port"] == 14330


def test_a_windows_staging_written_with_backslashes_loads_with_forward_ones(tmp_path):
    import json

    from conftest import stated

    entry = stated({
        "restore_id": "ACME_MSSQL_TO_WIN", "db_type": "sqlserver", "server_id": "SRC",
        "target_server_id": "WIN-MSSQL2025", "backup_dir": "C:\\SQLBK", "script": "sqlserver",
        "source_backup_host_dir": "C:\\SQLBK", "target_backup_dir": "C:\\dbabrain_restore\\ACME_MSSQL_TO_WIN",
        "cleanup_retention": 86400, "time_window": {"from_hour": 0, "to_hour": 23},
        "overwrite_existing": False, "action": "restore", "restore_from": "repository",
        "env": {"MSSQL_USER": "sa"},
        "database_mappings": [{"source_database": "APP", "target_database": "APP"}]})
    path = tmp_path / "restore_config.json"
    path.write_text(json.dumps({"backup_restore": {"restores": [entry]}}), encoding="utf-8")

    job = restore_script.load_script_restores(path)[0]

    # Three levels by "/": the staging-depth check passes it, as it passes a Linux one.
    assert job.target_backup_dir == STAGE


# --------------------------------------------------------------------------- #
# The logins and host blocks the app hands to common
# --------------------------------------------------------------------------- #
def test_the_login_of_a_windows_target_says_so_and_its_host_block_is_powershell(monkeypatch):
    import dbabrain.lib.data_sources as data_sources

    monkeypatch.setattr(data_sources, "resolve_ssh_password", lambda **_k: "pw")

    login = restore_script._ssh_login(_target(), data_dir=None, key=None, key_base64=None)
    linux = restore_script._ssh_login(_target(platform="linux"), data_dir=None, key=None, key_base64=None)

    assert login["platform"] == "windows" and "platform" not in linux
    assert restore_script._ssh_host_block(login) == {
        "runtime": "windows", "access": "ssh", "host": "win-sql.example.test", "port": 22,
        "username": "corp\\dba", "password": "pw"}
    assert restore_script._ssh_host_block(linux)["runtime"] == "linux"


# --------------------------------------------------------------------------- #
# The clean-up after a restore that worked
# --------------------------------------------------------------------------- #
def _cleanup_job():
    return SimpleNamespace(restore_id="ACME_MSSQL_TO_WIN", label="ACME_MSSQL_TO_WIN (sqlserver)", cleanup_retention=3600,
                           target_backup_dir=STAGE, staging_marker=".dbops-staging", target_server_id="WIN-MSSQL2025")


def test_the_clean_up_reads_a_windows_listing_in_the_stagings_own_form(monkeypatch):
    windows_dir = STAGE.replace("/", "\\")
    listing = {"folder_found": True, "truncated": False,
               "files": [{"path": f"{windows_dir}\\.dbops-staging", "type": "file", "size_bytes": 69,
                          "mtime_utc": "2026-11-01T00:00:00Z"},
                         {"path": f"{windows_dir}\\APP\\FULL\\APP_FULL_1.bak", "type": "file", "size_bytes": 10,
                          "mtime_utc": "2026-11-01T00:00:00Z"}],
               "folders": [f"{windows_dir}\\APP", f"{windows_dir}\\APP\\FULL"]}
    calls: dict = {}
    monkeypatch.setattr(restore_script, "_ssh_login", lambda *a, **k: dict(WINDOWS_LOGIN))
    monkeypatch.setattr(restore_script.common_cli, "run",
                        lambda command, request, **_k: listing if command == "list-files" else pytest.fail(command))

    def delete_files(**kwargs):
        calls["files"] = kwargs
        return {"deleted": list(kwargs["paths"]), "already_gone": [], "kept": [], "not_deleted": []}

    def delete_husks(**kwargs):
        calls["husks"] = kwargs
        return {"removed": list(kwargs["folders"]), "not_removed": []}

    monkeypatch.setattr(verified_cleanup, "delete_files", delete_files)
    monkeypatch.setattr(verified_cleanup, "delete_husks", delete_husks)
    catalog = SimpleNamespace(staged_file_facts=lambda **k: {p: {"size_bytes": 10, "sha256": "a" * 64}
                                                            for p in k["paths"]})

    result = restore_script.prune_staged_backups(_cleanup_job(), target=_target(), catalog=catalog, now=1.9e9)

    assert result["pruned"] == 1 and "error" not in result
    assert calls["files"]["paths"] == [f"{STAGE}/APP/FULL/APP_FULL_1.bak"]
    assert calls["files"]["windows"] is True and calls["files"]["host"]["runtime"] == "windows"
    assert calls["husks"]["folders"] == [f"{STAGE}/APP", f"{STAGE}/APP/FULL"]


def test_a_windows_staging_with_no_marker_is_left_and_the_way_to_mark_it_is_said_in_powershell(monkeypatch):
    monkeypatch.setattr(restore_script, "_ssh_login", lambda *a, **k: dict(WINDOWS_LOGIN))
    monkeypatch.setattr(restore_script.common_cli, "run", lambda *a, **k: {
        "folder_found": True, "truncated": False, "folders": [],
        "files": [{"path": "C:\\dbabrain_restore\\ACME_MSSQL_TO_WIN\\payroll.xlsx", "type": "file",
                   "size_bytes": 1, "mtime_utc": "2026-11-01T00:00:00Z"}]})
    monkeypatch.setattr(verified_cleanup, "delete_files", lambda **k: pytest.fail("nothing is deleted"))

    result = restore_script.prune_staged_backups(_cleanup_job(), target=_target(), catalog=None, now=1.9e9)

    assert "New-Item -ItemType File" in result["error"] and "touch" not in result["error"]


def test_the_copies_a_restore_staged_on_windows_are_deleted_there_by_their_facts(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(restore_script, "_ssh_login", lambda *a, **k: dict(WINDOWS_LOGIN))

    def delete_files(**kwargs):
        seen.update(kwargs)
        return {"deleted": list(kwargs["paths"]), "already_gone": [], "kept": [], "not_deleted": []}

    monkeypatch.setattr(verified_cleanup, "delete_files", delete_files)
    files = [{"path": f"{STAGE}/APP/FULL/APP_FULL_1.bak", "size_bytes": 10, "sha256": "b" * 64}]

    result = restore_script.delete_decrypted_staging(_cleanup_job(), target=_target(), files=files)

    assert result["deleted"] == 1 and seen["windows"] is True
    assert seen["host"]["runtime"] == "windows" and seen["must_be_under"] == STAGE


# --------------------------------------------------------------------------- #
# common: the staging on a Windows session
# --------------------------------------------------------------------------- #
class _Out:
    def __init__(self, text: str):
        self._text = text
        self.channel = SimpleNamespace(recv_exit_status=lambda: 0)

    def read(self):
        return self._text.encode("utf-8")


class _Written:
    def __init__(self, files: dict, path: str):
        self.files, self.path = files, path
        files[path] = bytearray()

    def write(self, data):
        self.files[self.path].extend(data if isinstance(data, (bytes, bytearray)) else str(data).encode())

    def settimeout(self, _seconds):
        pass

    def set_pipelined(self, _on):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _WindowsSftp:
    """Windows OpenSSH's SFTP as measured: drives under ``/``, ``posix-rename`` replaces, ``rename`` refuses."""

    def __init__(self, files=None, dirs=()):
        self.files: dict[str, bytearray] = {path: bytearray(data) for path, data in (files or {}).items()}
        self.dirs = {"/", "/C:", *dirs}
        self.times: dict[str, int] = {}

    def stat(self, path):
        if path.rstrip("/") in self.dirs or path in self.files:
            return SimpleNamespace(st_size=len(self.files.get(path, b"")))
        raise IOError(2, "No such file")

    def mkdir(self, path):
        assert path.rstrip("/") != "/C:", "a drive is never made"
        self.dirs.add(path.rstrip("/"))

    def listdir_attr(self, path):
        import posixpath
        import stat as st

        base = path.rstrip("/")
        if base not in self.dirs:
            raise IOError(2, "No such file")
        entries = [SimpleNamespace(filename=posixpath.basename(name), st_size=len(data), st_mtime=0,
                                   st_mode=st.S_IFREG | 0o644)
                   for name, data in self.files.items() if posixpath.dirname(name) == base]
        entries += [SimpleNamespace(filename=posixpath.basename(name), st_size=0, st_mtime=0, st_mode=st.S_IFDIR | 0o755)
                    for name in self.dirs if name not in ("/", base) and posixpath.dirname(name) == base]
        return entries

    def open(self, path, mode="r"):
        parent = path.rsplit("/", 1)[0]
        if parent not in self.dirs:
            raise IOError(2, "No such file")
        return _Written(self.files, path)

    def posix_rename(self, old, new):
        self.files[new] = self.files.pop(old)

    def rename(self, old, new):
        if new in self.files:
            raise IOError("Failure")
        self.files[new] = self.files.pop(old)

    def remove(self, path):
        self.files.pop(path)

    def utime(self, path, times):
        self.stat(path)
        self.times[path] = times[1]


class _WindowsSession:
    access = SimpleNamespace(platform="windows")

    def __init__(self, sftp=None, free=436817887232):
        self._sftp = sftp or _WindowsSftp()
        self.free = free
        self.streams: list[str] = []

    def sftp(self):
        return self._sftp

    def open_stream(self, command, **_k):
        self.streams.append(command)
        return None, _Out(f"FREE {self.free}\r\n"), None

    @property
    def client(self):
        raise AssertionError("a Windows target has no cat to write a channel into")

    def close(self):
        pass


def _script_in(command: str) -> str:
    assert command.startswith("powershell -NoProfile -NonInteractive -EncodedCommand ")
    return base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16-le")


def test_the_free_space_of_a_windows_staging_is_asked_of_its_drive_in_powershell():
    session = _WindowsSession(free=123456)

    assert backup_copy._target_free_bytes(session, "/C:/dbabrain_restore/R") == 123456
    script = _script_in(session.streams[0])
    assert "GetPathRoot('C:\\dbabrain_restore\\R')" in script and "AvailableFreeSpace" in script


def test_nothing_is_chmodded_on_windows_and_the_answer_says_nothing_was_opened():
    session, said = _WindowsSession(), []

    assert backup_copy.open_for_the_engine(session, "/C:/dbabrain_restore/R", log=said.append) is False
    assert session.streams == [] and "ACL" in said[0]


def test_a_windows_staging_is_made_marked_and_measured_like_a_linux_one():
    session = _WindowsSession()
    rule = restore_space.parse_space_check({"space_check": {"enabled": True, "factor": 2.0, "on_unknown": "refuse"}})

    result = backup_copy.prepare_target(target_session=session, target_dir="/C:/dbabrain_restore/R",
                                        incoming=[("APP/FULL/APP_FULL_1.bak", 5000)], space_check=rule)

    assert "/C:/dbabrain_restore/R/" + backup_copy.STAGING_MARKER in session.sftp().files
    assert "/C:/dbabrain_restore/R/APP/FULL" in session.sftp().dirs
    assert result.space["ok"] is True and result.space["free_bytes"] == 436817887232


def test_a_folder_that_is_someone_elses_is_refused_in_the_hosts_own_words():
    sftp = _WindowsSftp({"/C:/dbabrain_restore/R/payroll.xlsx": b"x"}, {"/C:/dbabrain_restore", "/C:/dbabrain_restore/R"})

    with pytest.raises(backup_copy.StagingDirError) as refused:
        backup_copy.prepare_target(target_session=_WindowsSession(sftp), target_dir="/C:/dbabrain_restore/R",
                                   incoming=[("APP/FULL/APP_FULL_1.bak", 5000)])
    assert "New-Item -ItemType File -Path 'C:\\dbabrain_restore\\R\\.dbabrain-staging'" in str(refused.value)


@pytest.mark.parametrize("login, folder, answer", [
    (WINDOWS_LOGIN, "C:\\dbabrain_restore\\R", "/C:/dbabrain_restore/R"),
    ({**WINDOWS_LOGIN, "platform": "linux"}, "/opt/db_ops/restore/R", "/opt/db_ops/restore/R"),
    ({k: v for k, v in WINDOWS_LOGIN.items() if k != "platform"}, "/opt/db_ops/restore/R", "/opt/db_ops/restore/R"),
])
def test_a_staging_folder_is_taken_in_the_form_its_logins_sftp_takes(login, folder, answer):
    assert cli_backup_copy._folder(login, folder, role="target", field="target_dir") == answer


@pytest.mark.parametrize("login, folder, words", [
    ({k: v for k, v in WINDOWS_LOGIN.items() if k != "platform"}, STAGE, 'does not say "platform": "windows"'),
    (WINDOWS_LOGIN, "/opt/db_ops/restore/R", "drive path"),
    ({**WINDOWS_LOGIN, "platform": "macos"}, STAGE, "linux, windows"),
])
def test_a_staging_folder_of_the_other_kind_is_refused_before_a_host_is_reached(monkeypatch, login, folder, words):
    monkeypatch.setattr(cli_backup_copy, "_open", lambda *a, **k: pytest.fail("no host may be reached"))

    with pytest.raises(ValueError, match=words):
        cli_backup_copy._prepare_staging({"target": login, "target_dir": folder,
                                          "incoming": [{"path": "APP/FULL/a.bak", "size_bytes": 1}]})


def test_finish_staging_on_windows_times_each_file_and_opens_nothing(monkeypatch):
    sftp = _WindowsSftp({"/C:/dbabrain_restore/R/APP/FULL/a.bak": b"x"},
                        {"/C:/dbabrain_restore", "/C:/dbabrain_restore/R", "/C:/dbabrain_restore/R/APP",
                         "/C:/dbabrain_restore/R/APP/FULL"})
    session = _WindowsSession(sftp)
    monkeypatch.setattr(cli_backup_copy, "_open", lambda login, role: session)

    answer = cli_backup_copy._finish_staging({"target": WINDOWS_LOGIN, "target_dir": "C:/dbabrain_restore/R",
                                              "files": [{"path": "APP/FULL/a.bak", "mtime": 1790000000}],
                                              "open_for_engine": True})

    assert answer == {"timed": 1, "not_timed": [], "opened_for_engine": False}
    assert sftp.times == {"/C:/dbabrain_restore/R/APP/FULL/a.bak": 1790000000} and session.streams == []


# --------------------------------------------------------------------------- #
# common: the bytes onto a Windows target - over SFTP, never a cat channel
# --------------------------------------------------------------------------- #
PLAIN = b"a SQL Server backup, in the clear " * 64


class _Reader:
    def __init__(self, data: bytes):
        self._data, self.size = io.BytesIO(data), len(data)

    def read(self, count=-1):
        return self._data.read(count)

    def finish(self):
        return ""

    def close(self):
        return ""


def test_a_file_copied_onto_windows_is_written_over_sftp_even_when_cat_is_stated(monkeypatch):
    session = _WindowsSession(_WindowsSftp())
    monkeypatch.setattr(filecopy, "_ssh", lambda login, role: session)
    monkeypatch.setattr(filecopy, "_reader_factory",
                        lambda request, **k: (lambda path: _Reader(PLAIN), lambda: None, "cat"))
    target = f"{STAGE}/APP/FULL/APP_FULL_1.bak"

    answer = filecopy.copy_files({
        "source": {"host": "repo.example.test"}, "target": WINDOWS_LOGIN, "transfer": "cat", "overwrite": True,
        "files": [{"source": "/mnt/nas/SRC/APP/FULL/APP_FULL_1.bak", "target": target,
                   "file_name": "APP_FULL_1.bak", "size_bytes": len(PLAIN), "sha256": hashlib.sha256(PLAIN).hexdigest()}]})

    assert answer["copied"] == 1 and answer["files"][0]["target"] == target
    assert bytes(session.sftp().files["/" + target]) == PLAIN
    assert not any(path.endswith(filecopy.PARTIAL_SUFFIX) for path in session.sftp().files)


def test_copy_files_holds_each_target_to_its_logins_kind_of_path():
    line = {"source": "/mnt/nas/a.bak", "file_name": "a.bak", "size_bytes": 1, "sha256": "c" * 64}

    assert filecopy._files({"files": [{**line, "target": f"{STAGE}/a.bak"}]}, windows=True)[0]["target"] == f"{STAGE}/a.bak"
    with pytest.raises(filecopy.FileCopyError, match="drive path"):
        filecopy._files({"files": [{**line, "target": "/opt/x/a.bak"}]}, windows=True)
    with pytest.raises(filecopy.FileCopyError, match='"platform": "windows"'):
        filecopy._files({"files": [{**line, "target": f"{STAGE}/a.bak"}]})


def test_an_encrypted_copy_is_decrypted_onto_windows_over_sftp_and_proven():
    pytest.importorskip("pyrage")
    import pyrage
    from pyrage import x25519

    identity = x25519.Identity.generate()
    sealed = pyrage.encrypt(PLAIN, [identity.to_public()])
    session = _WindowsSession(_WindowsSftp())
    target = f"{STAGE}/APP/FULL/APP_FULL_1.bak"

    answer = repoencrypt.fetch_decrypted_file({
        "source": {"host": "repo.example.test"}, "path": "/mnt/nas/SRC/APP/FULL/APP_FULL_1.bak.age",
        "size_bytes": len(sealed), "sha256": hashlib.sha256(sealed).hexdigest(),
        "target": WINDOWS_LOGIN, "target_path": target, "file_name": "APP_FULL_1.bak",
        "plain_size_bytes": len(PLAIN), "plain_sha256": hashlib.sha256(PLAIN).hexdigest(),
        "identity": str(identity), "transfer": "cat", "overwrite": True},
        open_reader=lambda host, where: io.BytesIO(sealed), open_target=lambda login: session)

    assert answer["restored"] is True and answer["target_path"] == target
    assert bytes(session.sftp().files["/" + target]) == PLAIN
    assert not any(path.endswith(filecopy.PARTIAL_SUFFIX) for path in session.sftp().files)


def test_a_posix_path_for_a_windows_target_is_refused_before_anything_is_read():
    with pytest.raises(repoencrypt.RepoEncryptError, match="drive path"):
        repoencrypt.fetch_decrypted_file({
            "source": {}, "path": "/mnt/nas/a.bak.age", "size_bytes": 1, "sha256": "d" * 64,
            "target": WINDOWS_LOGIN, "target_path": "/opt/x/a.bak", "file_name": "a.bak",
            "plain_size_bytes": 1, "plain_sha256": "e" * 64, "identity": "AGE-SECRET-KEY-1" + "Q" * 58,
            "transfer": "sftp", "overwrite": True},
            open_reader=lambda *a: pytest.fail("read"), open_target=lambda *a: pytest.fail("opened"))


def test_the_vault_pair_is_pushed_into_a_folder_made_first_on_windows(tmp_path, monkeypatch):
    local = tmp_path / "db_ops_backup_cert.cer"
    local.write_bytes(b"certificate")
    sha = hashlib.sha256(b"certificate").hexdigest()
    ran: list[str] = []
    put: list[tuple[str, str]] = []
    monkeypatch.setattr(filetransfer, "open_session", lambda host: SimpleNamespace(
        sftp=lambda: SimpleNamespace(put=lambda a, b: put.append((a, b))), close=lambda: None))
    monkeypatch.setattr(filetransfer, "run", lambda host, command, **k: ran.append(command) or {"stdout": sha})
    remote = f"{STAGE}/__dbabrain_cert/db_ops_backup_cert.cer"

    answer = filetransfer.push({"local_path": str(local), "remote_path": remote, "host": WINDOWS_HOST})

    assert ran[0] == f"New-Item -ItemType Directory -Force -Path '{STAGE}/__dbabrain_cert' | Out-Null"
    assert put == [(str(local), remote)] and answer["verified"] is True


# --------------------------------------------------------------------------- #
# The PowerShell itself, run where it can be: on a Windows machine
# --------------------------------------------------------------------------- #
on_windows = pytest.mark.skipif(os.name != "nt", reason="PowerShell runs on a Windows machine")


@on_windows
def test_the_free_space_script_answers_on_this_windows_machine(tmp_path):
    folder = (tmp_path / "not_made_yet").as_posix()
    command = wrap(Host(runtime=WINDOWS), restore_space.windows_free_space_script(folder))

    ran = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
    free = restore_space.parse_windows_free_bytes(ran.stdout)

    assert ran.returncode == 0 and free is not None, ran.stderr
    assert abs(free - shutil.disk_usage(tmp_path).free) < (1 << 30)


@on_windows
def test_a_windows_listing_is_read_in_the_stagings_own_form_on_this_machine(tmp_path):
    root = tmp_path.resolve()  # the long name: Windows lists by it, not by an 8.3 short one
    (root / "APP" / "FULL").mkdir(parents=True)
    (root / ".dbops-staging").write_text("marker")
    (root / "APP" / "FULL" / "APP_FULL_1.bak").write_bytes(b"x" * 10)
    stated = root.as_posix()

    entries = listfiles._on_windows(Host(runtime=WINDOWS), stated, timeout=120, with_folders=True)
    read = sorted(restore_script._under_stated(entry["path"], stated) for entry in entries)

    assert read == [f"{stated}/.dbops-staging", f"{stated}/APP", f"{stated}/APP/FULL",
                    f"{stated}/APP/FULL/APP_FULL_1.bak"]


# --------------------------------------------------------------------------- #
# Found by the first restore onto the lab host's native instance (2026-11-14)
# --------------------------------------------------------------------------- #
def test_a_windows_backup_path_gives_the_files_own_name_to_its_chain_row_and_its_message():
    """The step reads its backup as a drive path with backslashes: split on "/" alone, the chain row's
    name was the whole path - tied to no catalogued backup file - and the message named the path."""
    from types import SimpleNamespace

    from dbabrain.backup_restore import restore_by_id

    path = "C:" + chr(92) + "dbabrain_restore" + chr(92) + "E" + chr(92) + "APP" + chr(92) + "LOG" + chr(92) + "APP_LOG_1.trn"
    step = {"op": "restore-log", "source_database": "APP", "chain": [{"path": path, "finished_at": "2026-11-14 11:00:00"}],
            "request": {"database_name": "APP", "backup_path": path, "with_recovery": True}}
    rows, _positions = restore_by_id._chain_rows(SimpleNamespace(server_id="S", target_server_id="T"), [step])

    assert rows[0]["file_name"] == "APP_LOG_1.trn" and rows[0]["file_path"] == path
    assert restore_by_id._plan_summary([step]) == "APP: log APP_LOG_1.trn"
    assert restore_by_id._file_name("/stage/APP/LOG/APP_LOG_1.trn/") == "APP_LOG_1.trn"
