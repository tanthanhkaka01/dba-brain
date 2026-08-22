"""Moving named files must refuse rather than half-succeed.

Every rule tested here is a failure this repository has already had somewhere else. A backup
piece that arrives truncated looks complete to everything downstream — ``copy_backup`` learned
that from smbclient and now re-fetches and finally refuses such files. A half-written file under
the real name is worse than no file, which is why ``transfer`` stages beside the destination and
renames atomically. This module is the manual-operations version of the same job, so it inherits
the same rules, and these tests are what stops them being quietly relaxed later.

The local packing path is exercised directly; the remote paths are argument-validation only,
because reaching a host is what the integration run against the CLOUD lab is for.
"""

from __future__ import annotations

import hashlib
import tarfile
import zipfile

import pytest

from db_ops.common import file_transfer


def _write(directory, name, content=b"backup-piece"):
    path = directory / name
    path.write_bytes(content)
    return path


def test_packing_named_files_reports_a_checksum_of_what_it_actually_wrote(tmp_path):
    _write(tmp_path, "a.bkp")
    _write(tmp_path, "b.bkp", b"second-piece")
    archive = tmp_path / "out" / "set.tar"

    result = file_transfer.pack_files({
        "files": [str(tmp_path / "a.bkp"), str(tmp_path / "b.bkp")],
        "archive_path": str(archive),
    })

    assert result["ok"] is True
    assert result["file_count"] == 2
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert result["sha256"] == digest
    assert result["bytes"] == archive.stat().st_size
    with tarfile.open(archive) as opened:
        assert sorted(opened.getnames()) == ["a.bkp", "b.bkp"]


def test_a_folder_can_be_narrowed_to_the_files_that_matter(tmp_path):
    _write(tmp_path, "keep.bkp")
    _write(tmp_path, "ignore.log")
    archive = tmp_path / "set.zip"

    result = file_transfer.pack_files({
        "folder": str(tmp_path), "include": "*.bkp",
        "archive_path": str(archive), "format": "zip",
    })

    assert result["file_count"] == 1
    with zipfile.ZipFile(archive) as opened:
        assert opened.namelist() == ["keep.bkp"]


def test_per_member_checksums_are_off_unless_asked_for(tmp_path):
    """Hashing every member re-reads every byte, and these sets are measured in GB — so the
    expensive answer has to be requested, never assumed."""
    _write(tmp_path, "a.bkp")
    request = {"files": [str(tmp_path / "a.bkp")], "archive_path": str(tmp_path / "s.tar")}

    assert file_transfer.pack_files(dict(request))["files"] is None

    request["archive_path"] = str(tmp_path / "s2.tar")
    request["checksum_files"] = True
    members = file_transfer.pack_files(request)["files"]
    assert members[0]["sha256"] == hashlib.sha256(b"backup-piece").hexdigest()


def test_an_existing_archive_is_never_replaced_by_accident(tmp_path):
    _write(tmp_path, "a.bkp")
    archive = _write(tmp_path, "set.tar", b"someone-elses-archive")
    request = {"files": [str(tmp_path / "a.bkp")], "archive_path": str(archive)}

    with pytest.raises(file_transfer.FileTransferError, match="overwrite"):
        file_transfer.pack_files(dict(request))
    assert archive.read_bytes() == b"someone-elses-archive"

    request["overwrite"] = True
    assert file_transfer.pack_files(request)["ok"] is True


def test_a_missing_source_file_names_itself_instead_of_producing_a_short_archive(tmp_path):
    with pytest.raises(file_transfer.FileTransferError, match="do not exist"):
        file_transfer.pack_files({
            "files": [str(tmp_path / "absent.bkp")],
            "archive_path": str(tmp_path / "set.tar"),
        })


def test_an_archive_that_would_be_empty_is_an_error_not_an_empty_archive(tmp_path):
    """An empty archive is the shape of a successful transfer of nothing, and it is exactly what
    an operator would restore from without noticing."""
    (tmp_path / "sub").mkdir()
    with pytest.raises(file_transfer.FileTransferError, match="empty"):
        file_transfer.pack_files({
            "folder": str(tmp_path), "include": "*.nothing",
            "archive_path": str(tmp_path / "set.tar"),
        })


def test_files_and_folder_are_mutually_exclusive(tmp_path):
    with pytest.raises(file_transfer.FileTransferError, match="exactly one"):
        file_transfer.pack_files({
            "files": [str(tmp_path / "a.bkp")], "folder": str(tmp_path),
            "archive_path": str(tmp_path / "set.tar"),
        })


def test_include_without_folder_is_rejected_rather_than_silently_ignored(tmp_path):
    """A filter that does nothing is worse than an error: the operator believes it narrowed the
    set and ships whatever the unfiltered list happened to be."""
    with pytest.raises(file_transfer.FileTransferError, match="include"):
        file_transfer.pack_files({
            "files": [str(tmp_path / "a.bkp")], "include": "*.bkp",
            "archive_path": str(tmp_path / "set.tar"),
        })


def test_an_unknown_archive_format_is_refused_before_anything_is_written(tmp_path):
    _write(tmp_path, "a.bkp")
    with pytest.raises(file_transfer.FileTransferError, match="tar, tar.gz or zip"):
        file_transfer.pack_files({
            "files": [str(tmp_path / "a.bkp")],
            "archive_path": str(tmp_path / "set.rar"), "format": "rar",
        })
    assert not (tmp_path / "set.rar").exists()


def test_sending_a_file_that_is_not_there_fails_before_a_connection_is_opened(tmp_path):
    """Resolving a host costs an SSH connect; a caller that named a file that does not exist
    should not pay for one, and should not appear in the host's auth log either."""
    with pytest.raises(file_transfer.FileTransferError, match="does not exist"):
        file_transfer.send_file({
            "target": "no-such-server", "local_path": str(tmp_path / "absent.bkp"),
            "remote_path": "/tmp/absent.bkp",
        })


def test_a_relative_local_path_resolves_against_the_tool_root_not_the_working_directory():
    """The daemon's cwd is not something a caller can predict, so a relative path that meant one
    file on the master and another on the worker would be a silent mis-delivery."""
    resolved = file_transfer._resolve_local("runtime/incoming/x.bkp")
    assert resolved.is_absolute()
    assert resolved == file_transfer.TOOL_ROOT / "runtime" / "incoming" / "x.bkp"
