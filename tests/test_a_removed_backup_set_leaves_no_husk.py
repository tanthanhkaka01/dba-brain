"""A backup set the source has pruned leaves nothing behind in the staging copy - not even its folder.

On the 0.26 node the PostgreSQL lab restore failed in most hours of 2026-10-03 with
``pg_combinebackup: could not open file ".../<stamp>_INCR/global/pg_control"``. The source keeps
two hours of hourly fulls; each hour the mirror removed the files of the set the source had
pruned - and left its directories. The listing reads a PostgreSQL backup off its directory name,
and a directory with no ``backup_manifest`` is dated by its own mtime, which was the removal: the
empty husk read as the newest full, and the restore combined it with every incremental after it.

Two fixes, each enough on its own: the mirror removes the directories the source no longer has,
and the listing does not count a backup directory that holds no file.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import stat as st
import subprocess

import pytest

from db_ops.common import backup_copy
from db_ops.common.backupfiles import postgresql


class _Sftp:
    """A filesystem as a dict: directories hold names; ``rmdir`` refuses a non-empty one."""

    def __init__(self, files: dict[str, int], dirs: set[str]):
        self.files = dict(files)
        self.dirs = set(dirs)

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
        self.files.pop(path)

    def rmdir(self, path):
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


def test_the_mirror_removes_the_folder_of_a_set_the_source_pruned(monkeypatch):
    live = {"20261003T020031Z_FULL": ["backup_manifest", "global/pg_control", "base/5/1259"]}
    pruned = {"20261003T000013Z_FULL": ["backup_manifest", "global/pg_control", "base/5/1259"],
              "20261003T001019Z_INCR": ["backup_manifest", "global/pg_control"]}
    source = _Sftp(*_tree("/src", live))
    files, dirs = _tree("/dst", {**live, **pruned})
    files["/dst/" + backup_copy.STAGING_MARKER] = 1
    target = _Sftp(files, dirs)
    monkeypatch.setattr(backup_copy, "_stream_files", lambda **_kw: True)

    backup_copy.sync_backup_dir(source_session=_Session(source), source_dir="/src",
                                target_session=_Session(target), target_dir="/dst")

    left = sorted(d for d in target.dirs if d.startswith("/dst/base/"))
    assert "/dst/base/20261003T000013Z_FULL" not in left
    assert "/dst/base/20261003T001019Z_INCR" not in left
    assert not any("000013Z" in d or "001019Z" in d for d in left)
    # The live set is untouched - its files, and the directory it holds empty by design.
    assert "/dst/base/20261003T020031Z_FULL/global/pg_control" in target.files
    assert "/dst/base/20261003T020031Z_FULL/pg_tblspc" in target.dirs


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
