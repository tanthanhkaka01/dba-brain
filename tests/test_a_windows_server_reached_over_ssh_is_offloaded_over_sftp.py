"""A Windows database server reached over SSH is offloaded over SFTP, nothing set up on it (2026-11-11).

Why it matters: the operator chose to read a Windows SQL Server over the SSH it already answers rather than
open a share on it (*do not change the database hosts*): its administrative share D$ refuses its
local account (Windows' remote UAC restriction), and a share is a change on the host. Windows'
OpenSSH serves SFTP, which takes a drive path as ``/D:/...``; its shell is PowerShell, where ``cat``
is Get-Content - text, not a backup's bytes - so such a source is read over SFTP only.
"""

from __future__ import annotations

import pytest

from dbabrain.common import filecopy


@pytest.mark.parametrize("path, served", [
    (r"D:\SQLBK\APPDB\LOG\APPDB_LOG_1.trn", "/D:/SQLBK/APPDB/LOG/APPDB_LOG_1.trn"),
    ("F:/SQLBK_DBOPS/APP/FULL/APP_FULL_1.bak", "/F:/SQLBK_DBOPS/APP/FULL/APP_FULL_1.bak"),
    ("/var/lib/postgresql/backup/dbops/wal/0001", "/var/lib/postgresql/backup/dbops/wal/0001"),
    ("relative/name", "relative/name"),
])
def test_a_windows_drive_path_is_named_the_way_windows_openssh_serves_it_and_no_other_path_changes(path, served):
    assert filecopy.sftp_path(path) == served


class _Sftp:
    def __init__(self):
        self.asked = []

    def stat(self, path):
        self.asked.append(("stat", path))
        return type("S", (), {"st_size": 3})()

    def open(self, path, mode):
        self.asked.append(("open", path))
        return type("H", (), {"read": lambda self, n: b"", "close": lambda self: None})()


def test_the_sftp_reader_opens_a_windows_backup_by_its_served_path():
    sftp = _Sftp()
    reader = filecopy._SftpReader(sftp, r"D:\SQLBK_DBOPS\APP\LOG\a.trn")
    assert reader.size == 3
    assert sftp.asked == [("stat", "/D:/SQLBK_DBOPS/APP/LOG/a.trn"), ("open", "/D:/SQLBK_DBOPS/APP/LOG/a.trn")]


def test_the_offload_reads_a_windows_server_over_ssh_and_only_over_sftp():
    import inspect

    from dbabrain.backup_restore import offload

    source = inspect.getsource(offload._run_one)
    assert 'source.access != "ssh"' in source, "a Windows server over SSH needs no share"
    assert 'entry.offload.transfer == "cat"' in source and "offload_transfer: sftp" in source
