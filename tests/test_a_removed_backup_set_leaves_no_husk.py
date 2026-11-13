"""A backup set the source has pruned leaves nothing behind in the staging copy - not even its folder.

On the 0.26 node the PostgreSQL lab restore failed in most hours of 2026-10-03 with
``pg_combinebackup: could not open file ".../<stamp>_INCR/global/pg_control"``. The source keeps
two hours of hourly fulls; each hour the mirror removed the files of the set the source had
pruned - and left its directories. The listing reads a PostgreSQL backup off its directory name,
and a directory with no ``backup_manifest`` is dated by its own mtime, which was the removal: the
empty husk read as the newest full, and the restore combined it with every incremental after it.

Two fixes, each enough on its own: the directories the source no longer has leave the staging with
their files, and the listing does not count a backup directory that holds no file.

The first was the mirror's own ``rmdir`` until 0.38.0 (1.329). The staging removes nothing now: it
names the pruned set's files and its folders, deepest first, and the restore deletes each file by a
verified command and each emptied folder by ``delete-empty-folders`` - or does not run. So what is
held here is the naming, its order, and that the restore asks for exactly what was named.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import stat as st
import subprocess
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import restore_script
from dbabrain.common import backup_copy
from dbabrain.common.backupfiles import postgresql


class _Sftp:
    """A filesystem as a dict: directories hold names; ``rmdir`` refuses a non-empty one. Every
    delete asked of it is recorded - the write probe's own apart."""

    def __init__(self, files: dict[str, int], dirs: set[str]):
        self.files = dict(files)
        self.dirs = set(dirs)
        self.deletes: list[str] = []

    def _children(self, path):
        path = path.rstrip("/")
        names = {}
        for name, size in self.files.items():
            if posixpath.dirname(name) == path:
                names[posixpath.basename(name)] = (size, False)
        for name in self.dirs:
            if posixpath.dirname(name) == path:
                names[posixpath.basename(name)] = (0, True)
        return names

    def listdir_attr(self, path):
        if path.rstrip("/") not in self.dirs:
            raise IOError(2, "No such file")

        class _E:
            def __init__(self, filename, size, isdir):
                self.filename, self.st_size, self.st_mtime = filename, size, 0
                self.st_mode = (st.S_IFDIR if isdir else st.S_IFREG) | 0o644

        return [_E(name, size, isdir) for name, (size, isdir) in self._children(path).items()]

    def stat(self, path):
        if path.rstrip("/") in self.dirs or path in self.files:
            return object()
        raise IOError(2, "No such file")

    def mkdir(self, path):
        self.dirs.add(path.rstrip("/"))

    def open(self, path, mode="r"):
        self.files[path] = 1
        files = self.files

        class _F:
            def write(self, data):
                files[path] = len(data)

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        return _F()

    def remove(self, path):
        if not path.endswith(".dbabrain_write_probe"):
            self.deletes.append(path)
        self.files.pop(path)

    def rmdir(self, path):
        self.deletes.append(path)
        if self._children(path):
            raise OSError(39, "Directory not empty")
        self.dirs.discard(path.rstrip("/"))

    def close(self):
        pass


class _Session:
    def __init__(self, sftp):
        self._sftp = sftp

    def sftp(self):
        return self._sftp


def _tree(root, sets):
    """Files and directories of backup sets: ``{"<stamp>_FULL": ["global/pg_control", ...]}``."""
    files, dirs = {}, {root, f"{root}/base", f"{root}/base/{next(iter(sets))}/pg_tblspc"}
    for name, members in sets.items():
        for member in members:
            path = f"{root}/base/{name}/{member}"
            files[path] = 10
            parent = posixpath.dirname(path)
            while parent != root:
                dirs.add(parent)
                parent = posixpath.dirname(parent)
    return files, dirs


LIVE = {"20261003T020031Z_FULL": ["backup_manifest", "global/pg_control", "base/5/1259"]}
PRUNED = {"20261003T000013Z_FULL": ["backup_manifest", "global/pg_control", "base/5/1259"],
          "20261003T001019Z_INCR": ["backup_manifest", "global/pg_control"]}


def _prepared():
    """The staging still holds two sets the source has pruned; returns (the answer, the target)."""
    source = _Sftp(*_tree("/src", LIVE))
    files, dirs = _tree("/dst", {**LIVE, **PRUNED})
    files["/dst/" + backup_copy.STAGING_MARKER] = 1
    target = _Sftp(files, dirs)
    result = backup_copy.prepare_staging(source_session=_Session(source), source_dir="/src",
                                         target_session=_Session(target), target_dir="/dst")
    return result, target


def test_the_staging_names_the_files_and_the_folders_of_a_set_the_source_pruned():
    result, _target = _prepared()

    assert result.absent == sorted(f"base/{name}/{member}" for name, members in PRUNED.items()
                                   for member in members)
    # Deepest first - the order they have to be removed in: a parent is empty by the time it is tried.
    assert result.absent_dirs == [
        "base/20261003T000013Z_FULL/base/5",
        "base/20261003T000013Z_FULL/base",
        "base/20261003T000013Z_FULL/global",
        "base/20261003T001019Z_INCR/global",
        "base/20261003T000013Z_FULL",
        "base/20261003T001019Z_INCR"]
    assert result.as_dict()["folders_absent_at_source"] == result.absent_dirs
    # The live set is not named - its files, and the directory it holds empty by design.
    assert not any("020031Z" in rel for rel in result.absent + result.absent_dirs)


def test_the_staging_itself_removes_neither_the_files_nor_the_folders():
    """It was a mirror: each such file removed over SFTP and each folder by ``rmdir``, held to
    nothing. Naming is all it does now; the deletes are the restore's, each by a verified command."""
    _result, target = _prepared()

    assert target.deletes == []
    assert "/dst/base/20261003T000013Z_FULL/global/pg_control" in target.files
    assert "/dst/base/20261003T001019Z_INCR" in target.dirs
    assert "/dst/base/20261003T020031Z_FULL/global/pg_control" in target.files
    assert "/dst/base/20261003T020031Z_FULL/pg_tblspc" in target.dirs


def test_the_restore_deletes_what_was_named_the_files_first_then_each_folder(monkeypatch):
    """The husk must still never reach a restore. What the staging named goes in two requests on
    the target: the files by the facts of the copy that staged them, then the folders in the order
    they were named - every one fenced to the staging folder."""
    result, _target = _prepared()
    asked: list[tuple[str, dict]] = []

    def run_allowing_failure(command, request, **_kwargs):
        asked.append((command, request))
        key = "files" if command == "delete-verified-files" else "folders"
        return True, {key: [{"path": one["path"], "status": "deleted"} for one in request[key]]}, ""

    monkeypatch.setattr(restore_script.common_cli, "run_allowing_failure", run_allowing_failure)
    catalog = SimpleNamespace(staged_file_facts=lambda *, target_server_id, paths: {
        path: {"size_bytes": 10, "sha256": "a" * 64} for path in paths})
    job = SimpleNamespace(label="PG_A_TO_B (postgresql)", target_server_id="ACME-PG-B")

    cleared = restore_script._clear_absent_at_source(
        job, result.as_dict(), target_host={"runtime": "linux", "access": "ssh", "host": "192.0.2.50"},
        target_dir="/dst", catalog=catalog)

    assert cleared == len(result.absent) == 5
    assert [command for command, _request in asked] == ["delete-verified-files", "delete-empty-folders"]
    assert [one["path"] for one in asked[0][1]["files"]] == [f"/dst/{rel}" for rel in result.absent]
    assert asked[1][1]["folders"] == [{"path": f"/dst/{rel}", "must_be_under": "/dst"}
                                      for rel in result.absent_dirs]


def test_a_husk_that_could_not_be_removed_stops_the_restore_by_name(monkeypatch):
    """Left in place, the empty ``<stamp>_FULL`` reads as the newest full - the failure this file is
    about - so a folder that will not go is the restore's refusal, not a line in a log."""
    result, _target = _prepared()

    def run_allowing_failure(command, request, **_kwargs):
        if command == "delete-verified-files":
            return True, {"files": [{"path": one["path"], "status": "deleted"} for one in request["files"]]}, ""
        return False, {"folders": [{"path": one["path"], "status": "refused",
                                    "reason": "the folder holds 1 entry"} for one in request["folders"]]}, ""

    monkeypatch.setattr(restore_script.common_cli, "run_allowing_failure", run_allowing_failure)
    catalog = SimpleNamespace(staged_file_facts=lambda *, target_server_id, paths: {
        path: {"size_bytes": 10, "sha256": "a" * 64} for path in paths})
    job = SimpleNamespace(label="PG_A_TO_B (postgresql)", target_server_id="ACME-PG-B")

    with pytest.raises(restore_script.StagingNotClearError) as refused:
        restore_script._clear_absent_at_source(
            job, result.as_dict(), target_host={"runtime": "linux", "access": "ssh", "host": "192.0.2.50"},
            target_dir="/dst", catalog=catalog)

    assert "/dst/base/20261003T000013Z_FULL/base/5 (refused - the folder holds 1 entry)" in str(refused.value)
    assert "Nothing is restored over them" in str(refused.value)


def test_the_listing_does_not_count_a_backup_folder_that_holds_no_file():
    captured = {}
    postgresql_run = postgresql.run
    try:
        postgresql.run = lambda _host, command, timeout=0: captured.setdefault("c", command) and {"stdout": ""}
        postgresql.list_files({"path": "/opt/db_ops/backup/pg", "host": {}})
    finally:
        postgresql.run = postgresql_run

    assert "find \"$d\" -type f -print -quit" in captured["c"]


# The listing runs on the Linux host that holds the backups. On Windows `bash` on PATH is as
# likely WSL's as Git's, and WSL cannot see a C:/ path - so it runs where CI runs, on POSIX.
@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                    reason="runs the listing in a POSIX shell, as the backup host does")
def test_the_listing_skips_the_husk_when_it_runs(tmp_path):
    root = tmp_path / "pg"
    (root / "base" / "20261003T020031Z_FULL" / "global").mkdir(parents=True)
    (root / "base" / "20261003T020031Z_FULL" / "global" / "pg_control").write_text("x")
    (root / "base" / "20261003T000013Z_FULL" / "global").mkdir(parents=True)     # the husk
    (root / "wal").mkdir()
    captured = {}
    postgresql_run = postgresql.run
    try:
        postgresql.run = lambda _host, command, timeout=0: captured.setdefault("c", command) and {"stdout": ""}
        postgresql.list_files({"path": root.as_posix(), "host": {}})
    finally:
        postgresql.run = postgresql_run

    listed = subprocess.run(["bash", "-c", captured["c"]], capture_output=True, text=True).stdout

    assert "20261003T020031Z_FULL" in listed
    assert "20261003T000013Z_FULL" not in listed
