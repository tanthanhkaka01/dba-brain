"""A PostgreSQL staging folder that holds several chains restores the one asked for.

A latest restore copies only the newest chain; a point-in-time restore copies everything, so the
staging folder then holds every chain the source kept - on the 0.26 lab, a full every hour at :00
with its incremental at :10. The plan must still take exactly one chain: the newest full at or
before the moment, and only the incrementals of THAT full - an incremental named after it but
belonging to a later full would make pg_combinebackup build a directory that is not the database.

The plan reads each backup's finish from its ``backup_manifest`` mtime, so the copy must keep that
mtime. The tar stream did (read on the node, 2026-10-03: every staged manifest to the second of its
source); its per-file SFTP fallback did not: every piece it copied was dated by the copy, so the
chain copied last read as the newest whatever its age, and a point in time found no full before it.

Both are gone since 0.38.0 (1.329): the bytes travel by ``copy-files``, which dates a file by its
arrival. So the time is a step of its own - ``prepare_staging`` answers each file still to travel
with its source's mtime, and ``finish_staging`` sets it on the staged copy - and that is what is
held here now, with the rule the next run reads it by.
"""

from __future__ import annotations

import posixpath
import stat as st
from types import SimpleNamespace

from dbabrain.backup_restore import restore_by_id as rbi
from dbabrain.common import backup_copy
from dbabrain.common.backupfiles import list_backup_files

#: The 0.26 lab's staging after a copy of everything - names and manifest times as read on the node
#: (2026-10-03, host clock +07: the 03:00Z full finished at 10:00:19 there).
STAGED = (
    "/s/base/20261003T030018Z_FULL|4096|2026-10-03 10:00:19 +0700\n"
    "/s/base/20261003T031022Z_INCR|4096|2026-10-03 10:10:22 +0700\n"
    "/s/base/20261003T040018Z_FULL|4096|2026-10-03 11:00:18 +0700\n"
    "/s/base/20261003T041026Z_INCR|4096|2026-10-03 11:10:26 +0700\n"
    "/s/base/20261003T050007Z_FULL|4096|2026-10-03 12:00:07 +0700\n"
    "/s/base/20261003T051015Z_INCR|4096|2026-10-03 12:10:15 +0700\n"
    "/s/wal|4096|2026-10-03 12:14:00 +0700\n"
)


def _plan(monkeypatch, staged: str, *, point_in_time: str = "") -> list[str]:
    """The backups the real plan restores, over the real listing of ``staged``."""
    monkeypatch.setattr("dbabrain.common.backupfiles.postgresql.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": staged, "stderr": ""})
    monkeypatch.setattr(rbi, "_list_backup_files", lambda request: list_backup_files(request))
    monkeypatch.setattr(rbi, "_visible_dir", lambda job: "/s")
    job = SimpleNamespace(restore_id="LAB251_PG_TO_LAB252", env={})
    step = rbi._plan_postgresql(job, {}, point_in_time=point_in_time,
                                host={"runtime": "docker", "container": "c"}, data_dir=None)[0]
    request = step["request"]
    paths = request.get("backup_paths") or [request["backup_path"]]
    return [posixpath.basename(p) for p in paths]


def test_latest_takes_the_newest_full_and_only_its_incremental(monkeypatch):
    assert _plan(monkeypatch, STAGED) == ["20261003T050007Z_FULL", "20261003T051015Z_INCR"]


def test_a_moment_between_two_chains_takes_the_older_chain_whole(monkeypatch):
    """11:30 +07 = 04:30Z: the 04:00Z full and its 04:10Z incremental - not the 03:10Z one, which
    belongs to the full before, and not the 05:00Z chain, which is after the moment."""
    assert _plan(monkeypatch, STAGED, point_in_time="2026-10-03 04:30:00 +00:00") == [
        "20261003T040018Z_FULL", "20261003T041026Z_INCR"]


def test_a_moment_between_a_full_and_its_incremental_takes_the_full_alone(monkeypatch):
    """04:05Z: the 04:10Z incremental finished after the moment; WAL replay reaches it instead."""
    assert _plan(monkeypatch, STAGED, point_in_time="2026-10-03 04:05:00 +00:00") == [
        "20261003T040018Z_FULL"]


class _Sftp:
    """Files with their mtimes; ``utime`` sets one and is recorded."""

    def __init__(self, files: dict[str, tuple[int, int]], dirs: set[str]):
        self.files = dict(files)          # path -> (size, mtime)
        self.dirs = set(dirs)
        self.timed: list[tuple[str, tuple[int, int]]] = []

    def listdir_attr(self, path):
        path = path.rstrip("/")
        if path not in self.dirs:
            raise IOError(2, "No such file")
        entries = []
        for name, (size, mtime) in self.files.items():
            if posixpath.dirname(name) == path:
                entries.append((posixpath.basename(name), size, mtime, False))
        for name in self.dirs:
            if posixpath.dirname(name) == path:
                entries.append((posixpath.basename(name), 0, 0, True))

        class _E:
            def __init__(self, filename, size, mtime, isdir):
                self.filename, self.st_size, self.st_mtime = filename, size, mtime
                self.st_mode = (st.S_IFDIR if isdir else st.S_IFREG) | 0o644

        return [_E(*entry) for entry in entries]

    def stat(self, path):
        if path.rstrip("/") in self.dirs or path in self.files:
            return object()
        raise IOError(2, "No such file")

    def mkdir(self, path):
        self.dirs.add(path.rstrip("/"))

    def open(self, path, mode="r"):
        sftp = self

        class _F:
            def prefetch(self, _size):
                pass

            def write(self, _data):
                sftp.files[path] = (1, 1_900_000_000)

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        return _F()

    def utime(self, path, times):
        if path not in self.files:
            raise IOError(2, "No such file")      # what SFTP answers for a path that is not there
        self.timed.append((path, tuple(times)))
        size, _mtime = self.files[path]
        self.files[path] = (size, int(times[1]))

    def remove(self, path):
        self.files.pop(path)

    def rmdir(self, path):
        self.dirs.discard(path.rstrip("/"))

    def close(self):
        pass


class _Session:
    def __init__(self, sftp):
        self._sftp = sftp

    def sftp(self):
        return self._sftp


#: Two manifests and the second each backup finished at - what the plan dates a backup by.
FINISHED = {"base/20261003T030018Z_FULL/backup_manifest": 1_759_460_419,
            "base/20261003T050007Z_FULL/backup_manifest": 1_759_467_607}
COPIED_AT = 1_900_000_000      # the time a copied file carries until it is given its source's


def _two_manifests() -> _Sftp:
    return _Sftp({f"/src/{rel}": (10, mtime) for rel, mtime in FINISHED.items()},
                 {"/src", "/src/base", "/src/base/20261003T030018Z_FULL",
                  "/src/base/20261003T050007Z_FULL"})


def _land(target: _Sftp, pending) -> None:
    """What ``copy-files`` leaves on the target: each file whole, and dated by its arrival."""
    for rel, size, _mtime in pending:
        target.files[f"/dst/{rel}"] = (size, COPIED_AT)


def test_each_copied_piece_is_given_its_sources_time():
    """Dated by the copy, the 03:00Z manifest copied after the 05:00Z one read as the newest backup
    on the target. The staging answers each file to travel with its source's time, and the step
    after the copy sets it."""
    target = _Sftp({}, set())
    prepared = backup_copy.prepare_staging(source_session=_Session(_two_manifests()), source_dir="/src",
                                           target_session=_Session(target), target_dir="/dst")
    assert {rel: mtime for rel, _size, mtime in prepared.pending} == FINISHED
    _land(target, prepared.pending)

    answer = backup_copy.finish_staging(_Session(target), "/dst",
                                        [(rel, mtime) for rel, _size, mtime in prepared.pending])

    assert answer == {"timed": 2, "not_timed": []}
    assert sorted(target.timed) == sorted((f"/dst/{rel}", (mtime, mtime)) for rel, mtime in FINISHED.items())
    for rel, mtime in FINISHED.items():
        assert target.files[f"/dst/{rel}"][1] == mtime, rel


def test_a_piece_that_is_not_there_to_be_timed_is_answered_and_the_others_are_still_timed():
    """A file the copy could not land has no time to be given. That is an answer, not an exception:
    whether the copy stands is the checksum gate's to say, and the pieces that did land must not
    keep the copy's date because one did not."""
    landed, missing = sorted(FINISHED)
    target = _Sftp({f"/dst/{landed}": (10, COPIED_AT)}, {"/dst"})

    answer = backup_copy.finish_staging(_Session(target), "/dst", sorted(FINISHED.items()))

    assert answer["timed"] == 1 and target.files[f"/dst/{landed}"][1] == FINISHED[landed]
    assert answer["not_timed"] == [{"path": f"/dst/{missing}", "reason": "[Errno 2] No such file"}]


def test_a_piece_whose_source_time_is_not_known_is_left_as_it_landed():
    target = _Sftp({"/dst/a.bak": (10, COPIED_AT)}, {"/dst"})

    answer = backup_copy.finish_staging(_Session(target), "/dst", [("a.bak", None)])

    assert answer == {"timed": 0, "not_timed": []} and target.timed == []


def test_a_piece_given_its_sources_time_does_not_travel_again_and_an_older_one_does():
    """The rule the next run reads the time by. Same size is not enough when the staged copy is
    OLDER than the source file: a rebuilt PostgreSQL cluster names its WAL from ...0001 again, 16 MB
    each like the previous cluster's still staged, and skipping them replayed the old cluster's WAL
    ("WAL file is from different database system", 2026-09-24)."""
    kept, rebuilt = sorted(FINISHED)
    staged = {f"/dst/{kept}": (10, FINISHED[kept]),                    # the time finish_staging set
              f"/dst/{rebuilt}": (10, FINISHED[rebuilt] - 3600),       # the same size, an hour older
              "/dst/" + backup_copy.STAGING_MARKER: (1, 1)}
    target = _Sftp(staged, {d.replace("/src", "/dst", 1) for d in _two_manifests().dirs})

    prepared = backup_copy.prepare_staging(source_session=_Session(_two_manifests()), source_dir="/src",
                                           target_session=_Session(target), target_dir="/dst")

    assert prepared.pending == [(rebuilt, 10, FINISHED[rebuilt])]
    assert prepared.skipped == 1


# --------------------------------------------------------------------------- #
# Which files actually travel: the newest chain for latest, everything for a point in time
# --------------------------------------------------------------------------- #
CHAINS = ("20261003T030018Z_FULL", "20261003T031022Z_INCR", "20261003T040018Z_FULL",
          "20261003T041026Z_INCR", "20261003T050007Z_FULL", "20261003T051015Z_INCR")
WAL = ("wal/000000010000000000000041", "wal/000000010000000000000042",
       "wal/000000010000000000000043")


def _backup_files(root: str, names) -> dict[str, tuple[int, int]]:
    files = {}
    for number, name in enumerate(names):
        for member in ("backup_manifest", "global/pg_control"):
            files[f"{root}/base/{name}/{member}"] = (100 + number, 1_759_460_000 + number * 600)
    return files


def _dirs(root: str, names) -> set[str]:
    return {root, f"{root}/base", f"{root}/wal"} | {
        d for name in names for d in (f"{root}/base/{name}", f"{root}/base/{name}/global")}


class _Source:
    """The source host: the `ls` the chain is chosen from, and the SFTP the copy walks."""

    def __init__(self, sftp, root):
        self._sftp, self._root = sftp, root

    def open_stream(self, command, timeout_seconds=None):
        listing = "".join(f"{self._root}/base/{name}\n" for name in CHAINS)

        class _Out:
            def read(self):
                return listing.encode("utf-8")

        return None, _Out(), None

    def sftp(self):
        return self._sftp


def _transferred(*, point_in_time: str) -> list[str]:
    """What still has to travel when the staging already holds the 03:00Z chain and one segment -
    the files ``prepare_staging`` answers as pending, which ``copy-files`` is then handed."""
    source_files = {**_backup_files("/src", CHAINS),
                    **{f"/src/{w}": (16, 1_759_467_000) for w in WAL}}
    source = _Sftp(source_files, _dirs("/src", CHAINS))
    staged = {path.replace("/src/", "/dst/", 1): meta for path, meta in source_files.items()
              if "030018Z" in path or "031022Z" in path or path.endswith("41")}
    staged["/dst/" + backup_copy.STAGING_MARKER] = (1, 1)
    target = _Sftp(staged, _dirs("/dst", CHAINS[:2]))
    session = _Source(source, "/src")
    include = backup_copy.chain_include("postgresql", session, source_dir="/src",
                                        point_in_time=point_in_time)
    prepared = backup_copy.prepare_staging(source_session=session, source_dir="/src",
                                           target_session=_Source(target, "/dst"), target_dir="/dst",
                                           include=include)
    return sorted(rel for rel, _size, _mtime in prepared.pending)


def test_latest_moves_the_newest_chain_and_the_new_wal_only():
    assert _transferred(point_in_time="") == [
        "base/20261003T050007Z_FULL/backup_manifest",
        "base/20261003T050007Z_FULL/global/pg_control",
        "base/20261003T051015Z_INCR/backup_manifest",
        "base/20261003T051015Z_INCR/global/pg_control",
        "wal/000000010000000000000042",
        "wal/000000010000000000000043",
    ]


def test_a_point_in_time_moves_every_chain_the_staging_does_not_already_hold():
    """A moment before the newest full needs an older chain, so a point in time copies the whole
    directory - and still skips what is already staged (the 03:00Z chain, segment 41)."""
    assert _transferred(point_in_time="2026-10-03 04:30:00 +00:00") == [
        "base/20261003T040018Z_FULL/backup_manifest",
        "base/20261003T040018Z_FULL/global/pg_control",
        "base/20261003T041026Z_INCR/backup_manifest",
        "base/20261003T041026Z_INCR/global/pg_control",
        "base/20261003T050007Z_FULL/backup_manifest",
        "base/20261003T050007Z_FULL/global/pg_control",
        "base/20261003T051015Z_INCR/backup_manifest",
        "base/20261003T051015Z_INCR/global/pg_control",
        "wal/000000010000000000000042",
        "wal/000000010000000000000043",
    ]
