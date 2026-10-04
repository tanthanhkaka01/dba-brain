"""Every engine copies its chain or its window, and the restore still finds what it applies.

The operator, 2026-10-03: check `window` and `chain` for SQL Server, PostgreSQL and Oracle - all
three engines must work, with both kinds of copy. Until that day only the share-driven SQL Server
restore read `copy_selection`; a copy to another machine ignored it - PostgreSQL and Oracle always
took their chain, SQL Server always took the whole directory.

The rule now, the same for every engine and both ways to a target:

* ``chain`` - the backups the restore applies. SQL Server: each database's newest FULL, its newest
  DIFF and the LOGs after them, and ``_cert/``. PostgreSQL: the newest ``_FULL``, its ``_INCR``s and
  ``wal/``. Oracle: the pieces RMAN names.
* ``window`` - the chain AND every other file the source wrote in ``copy_recent_hours``: every
  restore point of the window, never fewer backups than the chain.
* a point in time copies everything, in either mode.

Each test below names the exact files that travel, from layouts shaped like the 0.26 labs'.
"""

from __future__ import annotations

import posixpath
import stat as st

import pytest

from db_ops.common import backup_copy

NOW = 1_759_469_400          # 2026-10-03 05:30:00Z
HOURS = 3600
WINDOW = 2                   # copy_recent_hours for the window cases


# --------------------------------------------------------------------------- #
# The three sources: relative path -> age in hours
# --------------------------------------------------------------------------- #
POSTGRESQL = {
    "base/20261003T030018Z_FULL/backup_manifest": 2.5,
    "base/20261003T030018Z_FULL/global/pg_control": 2.5,
    "base/20261003T031022Z_INCR/backup_manifest": 2.3,
    "base/20261003T031022Z_INCR/global/pg_control": 2.3,
    "base/20261003T040018Z_FULL/backup_manifest": 1.5,
    "base/20261003T040018Z_FULL/global/pg_control": 1.5,
    "base/20261003T041026Z_INCR/backup_manifest": 1.3,
    "base/20261003T041026Z_INCR/global/pg_control": 1.3,
    "base/20261003T050007Z_FULL/backup_manifest": 0.5,
    "base/20261003T050007Z_FULL/global/pg_control": 0.5,
    "base/20261003T051015Z_INCR/backup_manifest": 0.3,
    "base/20261003T051015Z_INCR/global/pg_control": 0.3,
    "wal/000000010000000000000041": 2.4,
    "wal/000000010000000000000042": 1.2,
    "wal/000000010000000000000043": 0.1,
}
SQLSERVER = {
    "LABTEST/FULL/LABTEST_FULL_20261003_030012Z.bak": 2.5,
    "LABTEST/FULL/LABTEST_FULL_20261003_040015Z.bak": 1.5,
    "LABTEST/FULL/LABTEST_FULL_20261003_050004Z.bak": 0.5,
    "LABTEST/DIFF/LABTEST_DIFF_20261003_041024Z.bak": 1.3,
    "LABTEST/DIFF/LABTEST_DIFF_20261003_051012Z.bak": 0.3,
    "LABTEST/LOG/LABTEST_LOG_20261003_032001Z.trn": 2.2,
    "LABTEST/LOG/LABTEST_LOG_20261003_043006Z.trn": 1.0,
    "LABTEST/LOG/LABTEST_LOG_20261003_052026Z.trn": 0.16,
    "LABTEST/LOG/LABTEST_LOG_20261003_052519Z.trn": 0.08,
    "_cert/db_ops_backup_cert.cer": 13.0,
    "_cert/db_ops_backup_cert.pvk": 13.0,
}
ORACLE = {
    "FREE_L0_20261003_jgsu1usa_600_1_1.bkp": 1.5,       # the level 0 before the newest
    "arch_FREE_20261003_k8hq12qh_648_1_1.bkp": 1.8,
    "autobackup_c-1514744796-20261003-27": 1.8,
    "spfile_FREE_20261003_kalq12ql_650_1_1.bkp": 1.8,
    "FREE_L0_20261003_kgsu23us_656_1_1.bkp": 0.5,       # the newest level 0
    "FREE_L0_20261003_kh3v23v3_657_1_1.bkp": 0.5,
    "FREE_L1_20261003_klsh34hs_661_1_1.bkp": 0.3,
    "arch_FREE_20261003_kq654556_666_1_1.bkp": 0.15,
    "autobackup_c-1514744796-20261003-2c": 0.1,
    "FREE_L0_20261002_ab12cd34_100_1_1.bkp": 20.0,      # yesterday's, outside every window
}
ORACLE_DIR = "/opt/db_ops/backup/ORACLE_LAB"
#: What RMAN answers for the newest level 0 onward (the catalog query of oracle_chain_include).
ORACLE_CHAIN = ("FREE_L0_20261003_kgsu23us_656_1_1.bkp", "FREE_L0_20261003_kh3v23v3_657_1_1.bkp",
                "FREE_L1_20261003_klsh34hs_661_1_1.bkp", "arch_FREE_20261003_kq654556_666_1_1.bkp",
                "autobackup_c-1514744796-20261003-2c")


# --------------------------------------------------------------------------- #
# Fakes: an SFTP tree with times, and the source's answers to `ls`, `find` and RMAN
# --------------------------------------------------------------------------- #
class _Sftp:
    def __init__(self, files: dict[str, tuple[int, float]], dirs: set[str]):
        self.files, self.dirs = dict(files), set(dirs)

    def listdir_attr(self, path):
        path = path.rstrip("/")
        if path not in self.dirs:
            raise IOError(2, "No such file")

        class _E:
            def __init__(self, filename, size, mtime, isdir):
                self.filename, self.st_size, self.st_mtime = filename, size, mtime
                self.st_mode = (st.S_IFDIR if isdir else st.S_IFREG) | 0o644

        found = [_E(posixpath.basename(p), size, mtime, False)
                 for p, (size, mtime) in self.files.items() if posixpath.dirname(p) == path]
        found += [_E(posixpath.basename(d), 0, 0, True) for d in self.dirs if posixpath.dirname(d) == path]
        return found

    def stat(self, path):
        if path.rstrip("/") in self.dirs or path in self.files:
            return object()
        raise IOError(2, "No such file")

    def mkdir(self, path):
        self.dirs.add(path.rstrip("/"))

    def open(self, path, mode="r"):
        files = self.files

        class _F:
            def write(self, _data):
                files[path] = (1, NOW)

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        return _F()

    def remove(self, path):
        self.files.pop(path)

    def rmdir(self, path):
        self.dirs.discard(path.rstrip("/"))

    def close(self):
        pass


def _tree(root: str, layout: dict[str, float]) -> _Sftp:
    files = {f"{root}/{rel}": (10, NOW - age * HOURS) for rel, age in layout.items()}
    dirs = {root}
    for path in files:
        parent = posixpath.dirname(path)
        while parent.startswith(root):
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    return _Sftp(files, dirs)


class _Out:
    def __init__(self, text):
        self._text = text.encode("utf-8")

    def read(self):
        return self._text


class _Source:
    """The source host: what the chain is chosen from, and the SFTP the copy walks."""

    def __init__(self, root: str, layout: dict[str, float]):
        self.root, self.layout = root, layout
        self._sftp = _tree(root, layout)

    def open_stream(self, command, timeout_seconds=None):
        if "rman target" in command:                       # Oracle: the preview names pieces
            text = "".join(f"  Piece Name: {ORACLE_DIR}/{name}\n" for name in ORACLE_CHAIN)
        elif "v$backup_piece" in command:                  # Oracle: the catalog from that L0 on
            text = "".join(f"{ORACLE_DIR}/{name}\n" for name in ORACLE_CHAIN)
        elif command.startswith("ls -1d"):                 # PostgreSQL: the backup directories
            names = sorted({rel.split("/")[1] for rel in self.layout if rel.startswith("base/")})
            text = "".join(f"{self.root}/base/{name}\n" for name in names)
        else:                                              # SQL Server: every file and its time
            text = "".join(f"{rel}|{NOW - age * HOURS:.3f}\n" for rel, age in self.layout.items())
        return None, _Out(text), None

    def sftp(self):
        return self._sftp


class _Target:
    def __init__(self):
        self._sftp = _Sftp({"/dst/" + backup_copy.STAGING_MARKER: (1, NOW)}, {"/dst"})

    def sftp(self):
        return self._sftp


def _copied(monkeypatch, engine: str, layout: dict[str, float], *, selection: str,
            point_in_time: str = "") -> list[str]:
    """The files one copy moves: backup-chain's include, then copy-backup-dir with the window."""
    root = ORACLE_DIR if engine == "oracle" else "/src"
    source = _Source(root, layout)
    include = backup_copy.chain_include(engine, source, source_dir=root, backup_dir=root,
                                        container="ORACLE_LAB", point_in_time=point_in_time)
    moved: list[str] = []
    monkeypatch.setattr(backup_copy, "_stream_files",
                        lambda **kw: moved.extend(rel for rel, _size in kw["files"]) or True)
    backup_copy.sync_backup_dir(
        source_session=source, source_dir=root, target_session=_Target(), target_dir="/dst",
        include=include, window_since=(NOW - WINDOW * HOURS) if selection == "window" else None)
    return sorted(moved)


def _within(layout: dict[str, float], hours: float) -> set[str]:
    return {rel for rel, age in layout.items() if age <= hours}


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #
PG_CHAIN = {rel for rel in POSTGRESQL
            if "20261003T050007Z_FULL" in rel or "20261003T051015Z_INCR" in rel or rel.startswith("wal/")}


def test_postgresql_chain_moves_the_newest_full_its_incremental_and_the_wal(monkeypatch):
    assert _copied(monkeypatch, "postgresql", POSTGRESQL, selection="chain") == sorted(PG_CHAIN)


def test_postgresql_window_adds_every_backup_of_the_window_to_the_chain(monkeypatch):
    moved = _copied(monkeypatch, "postgresql", POSTGRESQL, selection="window")

    assert moved == sorted(PG_CHAIN | _within(POSTGRESQL, WINDOW))
    assert "base/20261003T040018Z_FULL/backup_manifest" in moved          # the restore point before
    assert "base/20261003T030018Z_FULL/backup_manifest" not in moved      # older than the window


# --------------------------------------------------------------------------- #
# SQL Server (a copy to another machine)
# --------------------------------------------------------------------------- #
SQL_CHAIN = {"LABTEST/FULL/LABTEST_FULL_20261003_050004Z.bak",
             "LABTEST/DIFF/LABTEST_DIFF_20261003_051012Z.bak",
             "LABTEST/LOG/LABTEST_LOG_20261003_052026Z.trn",
             "LABTEST/LOG/LABTEST_LOG_20261003_052519Z.trn",
             "_cert/db_ops_backup_cert.cer", "_cert/db_ops_backup_cert.pvk"}


def test_sqlserver_chain_moves_the_newest_full_diff_the_logs_after_and_the_certificate(monkeypatch):
    """It took the whole directory until 2026-10-03, whatever the entry asked for."""
    assert _copied(monkeypatch, "sqlserver", SQLSERVER, selection="chain") == sorted(SQL_CHAIN)


def test_sqlserver_window_adds_every_backup_of_the_window_to_the_chain(monkeypatch):
    moved = _copied(monkeypatch, "sqlserver", SQLSERVER, selection="window")

    assert moved == sorted(SQL_CHAIN | _within(SQLSERVER, WINDOW))
    assert "LABTEST/FULL/LABTEST_FULL_20261003_040015Z.bak" in moved
    assert "LABTEST/FULL/LABTEST_FULL_20261003_030012Z.bak" not in moved


def test_sqlserver_a_database_with_no_full_is_copied_whole(monkeypatch):
    """The chain cannot be settled without a FULL; a narrowed copy that guessed would fail the
    restore, a whole one only costs time - and the other database still gets its chain."""
    layout = {**SQLSERVER, "ORPHAN/LOG/ORPHAN_LOG_20261003_052000Z.trn": 0.1,
              "ORPHAN/DIFF/ORPHAN_DIFF_20261003_040000Z.bak": 1.5}

    moved = _copied(monkeypatch, "sqlserver", layout, selection="chain")

    assert moved == sorted(SQL_CHAIN | {"ORPHAN/LOG/ORPHAN_LOG_20261003_052000Z.trn",
                                        "ORPHAN/DIFF/ORPHAN_DIFF_20261003_040000Z.bak"})


def test_sqlserver_a_layout_without_full_diff_log_folders_is_copied_whole(monkeypatch):
    flat = {"APPDB_20261003_050000Z.bak": 0.5, "APPDB_20261002_050000Z.bak": 24.5}

    assert _copied(monkeypatch, "sqlserver", flat, selection="chain") == sorted(flat)


# --------------------------------------------------------------------------- #
# Oracle
# --------------------------------------------------------------------------- #
def test_oracle_chain_moves_the_pieces_rman_names(monkeypatch):
    assert _copied(monkeypatch, "oracle", ORACLE, selection="chain") == sorted(ORACLE_CHAIN)


def test_oracle_chain_reaches_back_to_the_level_0_the_newest_log_can_recover(monkeypatch):
    """A level 0 taken after the newest archived-log backup is one a DUPLICATE cannot use: with no
    UNTIL it recovers through that log and stops, so it needs the level 0 before. On the 0.26.0 lab
    (2026-10-04) the chain was cut at the new level 0 (SCN 2899140) while the newest log backup
    ended at 2898894, and every run failed RMAN-06023 until the next archivelog job. RMAN is now
    asked for the preview UNTIL that SCN, so it names the level 0 the restore will really use."""
    asked: list[str] = []
    older_l0, newer_l0 = "FREE_L0_20261003_old_1316_1_1.bkp", "FREE_L0_20261004_new_1330_1_1.bkp"

    class _LabSource:
        def open_stream(self, command, timeout_seconds=None):
            asked.append(command)
            if "v$backup_redolog" in command:            # the newest log backup in the directory
                text = "   2898894\n"
            elif "rman target" in command:                # the level 0 RMAN would use up to there
                piece = older_l0 if "UNTIL SCN 2898894" in command else newer_l0
                text = f"  Piece Name: {ORACLE_DIR}/{piece}\n"
            else:                                         # the catalog from that piece's set on
                names = [older_l0, newer_l0] if older_l0 in command else [newer_l0]
                text = "".join(f"{ORACLE_DIR}/{name}\n" for name in names)
            return None, _Out(text), None

    include = backup_copy.oracle_chain_include(_LabSource(), backup_dir=ORACLE_DIR,
                                               container="ORACLE_LAB")

    assert any("RESTORE DATABASE UNTIL SCN 2898894 PREVIEW" in command for command in asked)
    assert set(include) == {older_l0, newer_l0}


def test_no_sql_sent_through_printf_carries_a_format_character():
    """The Oracle queries reach sqlplus as `printf <text> | sqlplus`, and printf reads `%` as a
    format: a `LIKE '<dir>/%'` failed the newest-log query on the lab (2026-10-04, "printf: `;':
    invalid format character") and the chain silently fell back to the newest level 0."""
    for text in (backup_copy._ORACLE_NEWEST_LOG_SQL.format(directory=ORACLE_DIR),
                 backup_copy._ORACLE_CHAIN_SQL.format(handles="'x'"),
                 backup_copy._ORACLE_PREVIEW):
        assert "%" not in text


def test_oracle_window_adds_every_piece_of_the_window_to_the_chain(monkeypatch):
    moved = _copied(monkeypatch, "oracle", ORACLE, selection="window")

    assert moved == sorted(set(ORACLE_CHAIN) | _within(ORACLE, WINDOW))
    assert "FREE_L0_20261003_jgsu1usa_600_1_1.bkp" in moved              # the level 0 before
    assert "FREE_L0_20261002_ab12cd34_100_1_1.bkp" not in moved          # yesterday's


# --------------------------------------------------------------------------- #
# Every engine: a point in time copies everything, in either mode
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("selection", ["chain", "window"])
@pytest.mark.parametrize("engine,layout", [("postgresql", POSTGRESQL), ("sqlserver", SQLSERVER),
                                           ("oracle", ORACLE)])
def test_a_point_in_time_copies_everything(monkeypatch, engine, layout, selection):
    """A moment before the newest full needs an older chain, so every engine takes the whole
    directory for one - whichever copy the entry chose."""
    moved = _copied(monkeypatch, engine, layout, selection=selection,
                    point_in_time="2026-10-03 03:45:00 +00:00")

    assert moved == sorted(layout)


@pytest.mark.parametrize("engine,layout", [("postgresql", POSTGRESQL), ("sqlserver", SQLSERVER),
                                           ("oracle", ORACLE)])
def test_a_window_never_moves_fewer_files_than_the_chain(monkeypatch, engine, layout):
    chain = set(_copied(monkeypatch, engine, layout, selection="chain"))
    window = set(_copied(monkeypatch, engine, layout, selection="window"))

    assert chain <= window


# --------------------------------------------------------------------------- #
# The app hands the entry's choice to common
# --------------------------------------------------------------------------- #
def _entry(**extra):
    return {"restore_id": "LAB_PG", "db_type": "postgresql", "server_id": "SRC-PG",
            "target_server_id": "DST-PG", "backup_dir": "/opt/db_ops/backup/PG",
            "source_backup_host_dir": "/opt/db_ops/backup/PG", "target_backup_dir": "/opt/stage",
            "script": "assets/restore/postgresql/pg_restore_basebackup.sh",
            "time_window": {"repeat_interval": 3600}, "cleanup_retention": 7200, **extra}


def _parsed(**extra):
    from db_ops.backup_restore.restore_script import _script_restore

    entry = _entry(**extra)
    return _script_restore(0, entry, entry["restore_id"])


def test_a_script_entry_reads_its_copy_selection_and_refuses_a_misspelling():
    assert _parsed().copy_selection == "chain"
    job = _parsed(copy_selection="WINDOW", copy_recent_hours=6)
    assert (job.copy_selection, job.copy_recent_hours) == ("window", 6)
    with pytest.raises(ValueError, match="copy_selection"):
        _parsed(copy_selection="windows")


@pytest.mark.parametrize("selection,hours,asks_chain,window_hours", [
    ("chain", 24, True, None),
    ("window", 6, True, 6),
    ("window", 0, False, None),       # a window with no limit is the whole directory
])
def test_the_copy_asks_common_for_the_chain_and_the_window_the_entry_chose(
        monkeypatch, selection, hours, asks_chain, window_hours):
    from db_ops.backup_restore import restore_script

    job = _parsed(copy_selection=selection, copy_recent_hours=hours)
    sent: list[tuple[str, dict]] = []

    def run(command, request, **_kw):
        sent.append((command, request))
        return {"include": ["base/20261003T050007Z_FULL", "wal/"]} if command == "backup-chain" else {}

    monkeypatch.setattr(restore_script.common_cli, "run", run)
    monkeypatch.setattr(restore_script, "_ssh_login", lambda *_a, **_k: {"host": "192.0.2.10"})
    source = type("S", (), {"container_name": ""})()

    restore_script.transfer_backup_to_target(job, source=source, target=source, data_dir=None,
                                             prune=False)

    commands = [command for command, _request in sent]
    assert ("backup-chain" in commands) is asks_chain
    copy = dict(sent)["copy-backup-dir"]
    assert copy.get("window_hours") == window_hours
    assert copy["include"] == (["base/20261003T050007Z_FULL", "wal/"] if asks_chain else [])
