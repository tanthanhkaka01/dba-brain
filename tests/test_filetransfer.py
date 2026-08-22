"""Packing a backup set once and moving one archive, with the arrival proven.

A backup set is thousands of small files, and per-file SFTP across two internet hops measured
10 KB/s on this estate - eight hours for 362 MB, with the link never the limit. So the set is
packed where it already lives and one archive travels.

The sha256 is not decoration. Size alone catches a truncated copy and nothing else; an archive
that arrived corrupted restores and is wrong, which is the failure this whole layer exists to make
impossible. It is hashed where it was made, hashed again where it landed, and a mismatch is
refused rather than retried - a retry that succeeds hides a link doing this occasionally.
"""

from __future__ import annotations

import pytest

from db_ops.common import filetransfer as ft
from db_ops.common.hostcmd import parse_host


def _linux(**over):
    return parse_host(dict({"runtime": "linux", "host": "h", "username": "u"}, **over))


def _windows():
    return parse_host({"runtime": "windows", "host": "vm", "username": "u"})


# --------------------------------------------------------------------------- #
# The pack command, per platform.
# --------------------------------------------------------------------------- #

def test_a_linux_folder_is_tarred_from_inside_itself():
    """`-C <folder> .` keeps the archive free of the absolute path, so it can be unpacked
    anywhere - which is the whole point of moving it to another machine."""
    command = ft._pack_command(_linux(), archive="/tmp/a.tar", folder="/b/dbops",
                               files=[], fmt=ft.TAR)
    assert "-C /b/dbops ." in command


def test_windows_uses_compress_archive_for_zip():
    """`zip` does not exist on Windows at all - that is Compress-Archive. Picking the wrong verb
    fails with "not recognized as a cmdlet", which reads like a broken host."""
    command = ft._pack_command(_windows(), archive=r"D:\a.zip", folder=r"D:\bak",
                               files=[], fmt=ft.ZIP)
    assert command.startswith("Compress-Archive ")


def test_windows_uses_tar_exe_for_tar():
    """tar.exe ships with Windows 10/2019 onward and behaves."""
    command = ft._pack_command(_windows(), archive=r"D:\a.tar", folder=r"D:\bak",
                               files=[], fmt=ft.TAR)
    assert command.startswith("tar -cf ")


def test_a_named_file_list_is_packed_by_name():
    command = ft._pack_command(_linux(), archive="/tmp/a.tar", folder="",
                               files=["/b/one.bkp", "/b/two.bkp"], fmt=ft.TAR)
    assert "one.bkp" in command and "two.bkp" in command
    assert "-C /b " in command


def test_paths_with_spaces_survive_the_command():
    command = ft._pack_command(_linux(), archive="/tmp/a.tar", folder="/b/my backups",
                               files=[], fmt=ft.TAR)
    assert "my backups" in command


def test_folder_and_files_together_are_refused(monkeypatch):
    """Both is a request that says two things; neither says nothing."""
    with pytest.raises(ft.FileTransferError, match="exactly one of folder or files"):
        ft.pack({"archive_path": "/tmp/a.tar", "folder": "/b", "files": ["/b/x"],
                 "host": {"runtime": "linux", "host": "h", "username": "u"}})


def test_neither_folder_nor_files_is_refused():
    with pytest.raises(ft.FileTransferError, match="exactly one of folder or files"):
        ft.pack({"archive_path": "/tmp/a.tar",
                 "host": {"runtime": "linux", "host": "h", "username": "u"}})


def test_an_unknown_format_is_refused_by_name():
    with pytest.raises(ft.FileTransferError, match="format must be one of"):
        ft.pack({"archive_path": "/tmp/a.7z", "folder": "/b", "format": "7z",
                 "host": {"runtime": "linux", "host": "h", "username": "u"}})


def test_a_failed_pack_reports_the_hosts_own_error(monkeypatch):
    """"packing failed" with nothing else sends an operator to read the wrong logs."""
    monkeypatch.setattr(ft, "run", lambda *a, **k: {
        "exit_code": 2, "stdout": "", "stderr": "tar: /b: Cannot open: Permission denied"})

    with pytest.raises(ft.FileTransferError, match="Permission denied"):
        ft.pack({"archive_path": "/tmp/a.tar", "folder": "/b",
                 "host": {"runtime": "linux", "host": "h", "username": "u"}})


def test_a_successful_pack_reports_the_hash_and_size(monkeypatch):
    answers = iter([
        {"exit_code": 0, "stdout": "", "stderr": ""},          # the tar
        {"exit_code": 0, "stdout": "abc123  /tmp/a.tar\n", "stderr": ""},   # sha256sum
        {"exit_code": 0, "stdout": "4096\n", "stderr": ""},    # stat
    ])
    monkeypatch.setattr(ft, "run", lambda *a, **k: next(answers))

    result = ft.pack({"archive_path": "/tmp/a.tar", "folder": "/b",
                      "host": {"runtime": "linux", "host": "h", "username": "u"}})

    assert result["sha256"] == "abc123"
    assert result["size"] == 4096


# --------------------------------------------------------------------------- #
# Arrival is proven, not assumed.
# --------------------------------------------------------------------------- #

def test_a_hash_mismatch_is_refused_and_says_not_to_restore_from_it():
    with pytest.raises(ft.FileTransferError) as excinfo:
        ft._verify("aaa", "bbb", where="pulling /tmp/a.tar")

    assert "do not restore from it" in str(excinfo.value)


def test_matching_hashes_pass_quietly():
    assert ft._verify("AAA", "aaa", where="x") is None


def test_a_missing_hash_does_not_fail_the_transfer():
    """A host that could not hash is a gap in proof, not proof of corruption - the result says
    verified: false and the caller decides."""
    assert ft._verify("", "abc", where="x") is None


def test_pull_requires_both_ends_named():
    with pytest.raises(ft.FileTransferError, match="both required"):
        ft.pull({"remote_path": "/tmp/a.tar",
                 "host": {"runtime": "linux", "host": "h", "username": "u"}})


def test_push_refuses_a_local_file_that_is_not_there(tmp_path):
    """Otherwise SFTP fails halfway with an error naming the remote path, sending an operator to
    check the wrong machine."""
    with pytest.raises(ft.FileTransferError, match="local_path does not exist"):
        ft.push({"local_path": str(tmp_path / "nope.tar"), "remote_path": "/tmp/a.tar",
                 "host": {"runtime": "linux", "host": "h", "username": "u"}})


def test_the_local_hash_is_the_real_sha256_of_the_bytes(tmp_path):
    import hashlib

    path = tmp_path / "a.bin"
    path.write_bytes(b"backup bytes")

    assert ft._sha256_local(str(path)) == hashlib.sha256(b"backup bytes").hexdigest()


def test_a_remote_parent_never_follows_the_local_operating_system():
    """Regression. `pathlib.Path` picks its flavour from the machine executing the code, so a
    Windows worker computing a Linux target's parent turned `/b/one.bkp` into `\b` - a directory
    that exists on neither end. The target's runtime decides the flavour."""
    linux = ft._pack_command(_linux(), archive="/tmp/a.tar", folder="",
                             files=["/b/one.bkp", "/b/two.bkp"], fmt=ft.TAR)
    assert "-C /b " in linux

    windows = ft._pack_command(_windows(), archive=r"D:\a.tar", folder="",
                               files=[r"D:\bak\one.bak"], fmt=ft.TAR)
    assert r'-C "D:\bak"' in windows
