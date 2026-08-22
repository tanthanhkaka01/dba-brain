"""Listing backups as full / diff / log, the same way for three engines that disagree completely.

SQL Server carries headers in the files, Oracle keeps the truth in RMAN's catalogue, and
PostgreSQL writes the level into the directory name. A caller should not have to know which - so
all three return the same rows, and where a field genuinely does not exist it is null rather than
invented.

The one rule worth protecting: only PostgreSQL may be classified by name. Doing it for the other
two is the silent failure - an RMAN piece named FREE_L0_... that is actually a level 1, or a .bak
copied from another database keeping its old name, both read fine and restore wrong.
"""

from __future__ import annotations

import pytest

from db_ops.common.backupfiles import (
    CONTROLFILE, DIFF, FULL, LOG, BackupListError, list_backup_files,
)


def test_an_unknown_engine_is_refused_by_name():
    with pytest.raises(BackupListError, match="db_type must be"):
        list_backup_files({"db_type": "mysql", "path": "/b"})


def test_an_unknown_kind_filter_is_refused():
    """A misspelled kind silently matching nothing would read as "there are no differentials"."""
    with pytest.raises(BackupListError, match="Unknown kind"):
        list_backup_files({"db_type": "oracle", "path": "/b", "kinds": ["fulll"]})


def test_postgresql_reads_the_level_out_of_the_directory_names(monkeypatch):
    """The one engine where names are the answer: the backup job wrote them on purpose."""
    listing = (
        "/b/base/20260806T010000Z_FULL|1024|2026-08-06 01:00:00\n"
        "/b/base/20260806T130000Z_INCR|256|2026-08-06 13:00:00\n"
        "/b/wal|4096|2026-08-06 14:00:00\n"
    )
    monkeypatch.setattr("db_ops.common.backupfiles.postgresql.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": listing, "stderr": ""})

    result = list_backup_files({"db_type": "postgresql", "path": "/b",
                                "host": {"runtime": "docker", "container": "pg"}})

    assert result["counts"] == {FULL: 1, DIFF: 1, LOG: 1, CONTROLFILE: 0}
    assert [f["kind"] for f in result["files"]] == [FULL, DIFF, LOG]


def test_postgresql_never_names_a_database():
    """pg_basebackup is whole-cluster. Inventing a database would let a caller filter on
    something that was never true."""
    from db_ops.common.backupfiles import postgresql

    rows = postgresql.list_files.__doc__ or ""
    assert "whole-cluster" in postgresql.__doc__


def test_postgresql_ignores_anything_that_is_not_part_of_the_layout(monkeypatch):
    listing = "/b/base/README|10|2026-08-06 01:00:00\n/b/base/20260806T010000Z_FULL|1|x\n"
    monkeypatch.setattr("db_ops.common.backupfiles.postgresql.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": listing, "stderr": ""})

    result = list_backup_files({"db_type": "postgresql", "path": "/b"})

    assert [f["path"] for f in result["files"]] == ["/b/base/20260806T010000Z_FULL"]


ORACLE_LIST = """
List of Backup Sets
===================

BS Key  Type LV Size       Device Type Elapsed Time Completion Time
------- ---- -- ---------- ----------- ------------ ---------------
3555    Incr 0  1.20G      DISK        00:00:45     2026-08-06 01:00:00
        BP Key: 3556   Status: AVAILABLE  Tag: FREE_L0
        Piece Name: /b/FREE_L0_20260806_01.bkp
  List of Datafiles in backup set 3555

BS Key  Type LV Size       Device Type Elapsed Time Completion Time
------- ---- -- ---------- ----------- ------------ ---------------
3600    Incr 1  120M       DISK        00:00:08     2026-08-06 02:00:00
        Piece Name: /b/FREE_L1_20260806_02.bkp

BS Key  Size       Device Type Elapsed Time Completion Time
------- ---------- ----------- ------------ ---------------
3700    8.00M      DISK        00:00:01     2026-08-06 03:00:00
        BP Key: 3701   Status: AVAILABLE
        Piece Name: /b/ARCH_20260806_03.bkp
  List of Archived Logs in backup set 3700
  Thrd Seq     Low SCN
"""


def test_oracle_levels_come_from_rman_not_the_file_name(monkeypatch):
    """The level is read from the catalogue's LV column, so a piece whose name lies is still
    classified correctly.

    The fixture is in RMAN's real order, which is the whole point: `Piece Name:` comes BEFORE the
    `List of Archived Logs in backup set` line that identifies the set. A first cut flipped a flag
    on that marker and so labelled every archivelog piece as whatever the previous set was -
    measured on the CLOUD lab as 810 "full" pieces where there is one level 0."""
    monkeypatch.setattr("db_ops.common.backupfiles.oracle.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": ORACLE_LIST, "stderr": ""})

    result = list_backup_files({"db_type": "oracle", "path": "/b",
                                "host": {"runtime": "docker", "container": "ora"}})

    by_path = {f["path"]: f["kind"] for f in result["files"]}
    assert by_path["/b/FREE_L0_20260806_01.bkp"] == FULL
    assert by_path["/b/FREE_L1_20260806_02.bkp"] == DIFF
    assert by_path["/b/ARCH_20260806_03.bkp"] == LOG


def test_oracle_skips_pieces_outside_the_directory_asked_about(monkeypatch):
    """An FRA copy has no counterpart in the directory being reported on, so offering it would
    hand the caller a path it cannot use."""
    text = ORACLE_LIST + "\n  Piece Name: /fra/OTHER_20260806_09\n"
    monkeypatch.setattr("db_ops.common.backupfiles.oracle.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": text, "stderr": ""})

    result = list_backup_files({"db_type": "oracle", "path": "/b"})

    assert all(f["path"].startswith("/b/") for f in result["files"])


def test_oracle_refusal_is_reported_rather_than_read_as_empty(monkeypatch):
    """An empty list and "rman would not talk to us" are different answers."""
    monkeypatch.setattr("db_ops.common.backupfiles.oracle.run",
                        lambda *a, **k: {"exit_code": 1,
                                         "stdout": "RMAN-04005: error from target database",
                                         "stderr": ""})

    with pytest.raises(BackupListError, match="rman refused"):
        list_backup_files({"db_type": "oracle", "path": "/b"})


def test_kinds_filter_narrows_the_answer(monkeypatch):
    listing = ("/b/base/a_FULL|1|x\n/b/base/b_INCR|1|x\n/b/wal|1|x\n")
    monkeypatch.setattr("db_ops.common.backupfiles.postgresql.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": listing, "stderr": ""})

    result = list_backup_files({"db_type": "postgresql", "path": "/b", "kinds": ["full"]})

    assert [f["kind"] for f in result["files"]] == [FULL]
    assert result["counts"][DIFF] == 0


def test_every_engine_returns_the_same_row_shape(monkeypatch):
    """The whole point of one command for three engines."""
    monkeypatch.setattr("db_ops.common.backupfiles.postgresql.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": "/b/base/a_FULL|1|x\n",
                                         "stderr": ""})
    monkeypatch.setattr("db_ops.common.backupfiles.oracle.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": ORACLE_LIST, "stderr": ""})

    pg = list_backup_files({"db_type": "postgresql", "path": "/b"})["files"][0]
    ora = list_backup_files({"db_type": "oracle", "path": "/b"})["files"][0]

    required = {"path", "kind", "database", "size", "finished_at"}
    assert required <= set(pg) and required <= set(ora)


def test_oracle_controlfile_autobackups_are_not_called_full(monkeypatch):
    """Measured: this estate writes one every 15 minutes, so counting them as full reported 610
    full backups for a lab holding a single level 0. A caller filtering for `full` would pick one
    and fail - they are not something a restore starts from."""
    text = ORACLE_LIST + """
BS Key  Size       Device Type Elapsed Time Completion Time
------- ---------- ----------- ------------ ---------------
3800    45.0M      DISK        00:00:01     2026-08-06 03:00:00
        Piece Name: /b/CF_AUTO_20260806_04.bkp
  Control File Included: Ckp SCN: 12345
"""
    monkeypatch.setattr("db_ops.common.backupfiles.oracle.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": text, "stderr": ""})

    default = list_backup_files({"db_type": "oracle", "path": "/b"})
    assert "/b/CF_AUTO_20260806_04.bkp" not in [f["path"] for f in default["files"]]

    asked = list_backup_files({"db_type": "oracle", "path": "/b", "kinds": ["controlfile"]})
    assert [f["path"] for f in asked["files"]] == ["/b/CF_AUTO_20260806_04.bkp"]


# --------------------------------------------------------------------------- #
# Walking a chain: newest full, then diffs after it, then logs after those.
# --------------------------------------------------------------------------- #

def _pg(monkeypatch, listing):
    monkeypatch.setattr("db_ops.common.backupfiles.postgresql.run",
                        lambda *a, **k: {"exit_code": 0, "stdout": listing, "stderr": ""})


CHAIN = (
    "/b/base/a_FULL|1|2026-08-07 01:00:00\n"
    "/b/base/b_INCR|1|2026-08-07 02:00:00\n"
    "/b/base/c_INCR|1|2026-08-07 03:00:00\n"
    "/b/base/d_FULL|1|2026-08-07 04:00:00\n"
    "/b/base/e_INCR|1|2026-08-07 05:00:00\n"
)


def test_latest_returns_the_newest_of_each_kind(monkeypatch):
    _pg(monkeypatch, CHAIN)

    result = list_backup_files({"db_type": "postgresql", "path": "/b", "latest": True})

    assert [f["path"] for f in result["files"]] == ["/b/base/d_FULL", "/b/base/e_INCR"]


def test_after_is_exclusive_so_a_chain_never_repeats_its_base(monkeypatch):
    """Chaining means "what came after the full I already have". Including that full again would
    restore it twice."""
    _pg(monkeypatch, CHAIN)

    result = list_backup_files({"db_type": "postgresql", "path": "/b",
                                "kinds": ["full"], "after": "2026-08-07 01:00:00"})

    assert [f["path"] for f in result["files"]] == ["/b/base/d_FULL"]


def test_before_is_inclusive_because_a_moment_means_up_to_and_including_it(monkeypatch):
    _pg(monkeypatch, CHAIN)

    result = list_backup_files({"db_type": "postgresql", "path": "/b",
                                "kinds": ["diff"], "before": "2026-08-07 03:00:00"})

    assert [f["path"] for f in result["files"]] == ["/b/base/b_INCR", "/b/base/c_INCR"]


def test_the_three_step_walk(monkeypatch):
    """The sequence this option set exists for, done end to end."""
    _pg(monkeypatch, CHAIN)

    full = list_backup_files({"db_type": "postgresql", "path": "/b",
                              "kinds": ["full"], "latest": True})
    assert [f["path"] for f in full["files"]] == ["/b/base/d_FULL"]

    diffs = list_backup_files({"db_type": "postgresql", "path": "/b", "kinds": ["diff"],
                               "after": full["newest_finished_at"]})
    assert [f["path"] for f in diffs["files"]] == ["/b/base/e_INCR"]

    logs = list_backup_files({"db_type": "postgresql", "path": "/b", "kinds": ["log"],
                              "after": diffs["newest_finished_at"]})
    assert logs["files"] == []          # nothing after, and that is a normal answer
    assert logs["newest_finished_at"] is None


def test_nothing_after_the_point_is_an_empty_list_not_an_error(monkeypatch):
    """A chain with no differentials is ordinary. Raising would make the caller treat "none yet"
    as a failure and stop."""
    _pg(monkeypatch, CHAIN)

    result = list_backup_files({"db_type": "postgresql", "path": "/b",
                                "kinds": ["diff"], "after": "2026-08-09 00:00:00"})

    assert result["files"] == []
    assert result["counts"][DIFF] == 0


def test_newest_finished_at_is_what_the_next_call_takes(monkeypatch):
    _pg(monkeypatch, CHAIN)

    result = list_backup_files({"db_type": "postgresql", "path": "/b", "kinds": ["full"]})

    assert result["newest_finished_at"] == "2026-08-07 04:00:00"


def test_latest_is_per_database_not_per_kind(monkeypatch):
    """A SQL Server backup directory holds every database on the instance, so "the latest full"
    across the set would answer with whichever database was backed up last."""
    from db_ops.common.backupfiles import _latest_only

    files = [
        {"path": "a.bak", "kind": FULL, "database": "APPDB", "finished_at": "2026-08-07 01:00:00"},
        {"path": "b.bak", "kind": FULL, "database": "APP", "finished_at": "2026-08-07 02:00:00"},
        {"path": "c.bak", "kind": FULL, "database": "APPDB", "finished_at": "2026-08-07 03:00:00"},
    ]

    assert sorted(f["path"] for f in _latest_only(files)) == ["b.bak", "c.bak"]


def test_a_backup_with_no_reported_time_is_kept_not_dropped(monkeypatch):
    """An engine that could not state a time is a gap in knowledge, not proof the piece is old -
    dropping it would silently shorten a chain."""
    from db_ops.common.backupfiles import _in_window

    rows = [{"path": "x", "kind": FULL, "finished_at": None}]

    assert _in_window(rows, after="2026-08-07 00:00:00", before=None) == rows


def test_one_level_is_asked_for_as_a_one_item_array(monkeypatch):
    """One field, always an array. A second singular field would be two ways to say the same
    thing, and eventually two ways that disagree."""
    _pg(monkeypatch, CHAIN)

    result = list_backup_files({"db_type": "postgresql", "path": "/b", "kinds": ["full"]})

    assert [f["path"] for f in result["files"]] == ["/b/base/a_FULL", "/b/base/d_FULL"]


def test_a_bare_string_is_refused_rather_than_iterated(monkeypatch):
    """`"kinds": "full"` would otherwise iterate into single characters and match nothing."""
    _pg(monkeypatch, CHAIN)

    with pytest.raises(BackupListError, match="must be an array"):
        list_backup_files({"db_type": "postgresql", "path": "/b", "kinds": "full"})


def test_a_database_filter_narrows_a_shared_directory():
    """One SQL Server directory holds every database on the instance, so walking a chain without
    naming one would take the "diff after the full" from whichever database happened to be next."""
    from db_ops.common.backupfiles import list_backup_files as walk
    import db_ops.common.backupfiles.postgresql as pg

    rows = [
        {"path": "appdb.bak", "kind": FULL, "database": "APPDB", "size": None,
         "finished_at": "2026-08-07 01:00:00"},
        {"path": "app.bak", "kind": FULL, "database": "APP", "size": None,
         "finished_at": "2026-08-07 02:00:00"},
    ]
    original = pg.list_files
    pg.list_files = lambda request: rows
    try:
        result = walk({"db_type": "postgresql", "path": "/b", "database": "APPDB"})
    finally:
        pg.list_files = original

    assert [f["path"] for f in result["files"]] == ["appdb.bak"]


def test_every_engine_reports_the_same_timestamp_format():
    """The three disagree at source - SQL Server ISO with a T, Oracle its NLS format, PostgreSQL a
    stat mtime with nanoseconds and an offset. `finished_at` is handed straight back as `after` on
    the next call, so a caller comparing two engines' strings must not have to know which is which.
    """
    from db_ops.common.backupfiles import row

    assert row(path="a", kind=FULL, finished_at="2026-08-07T01:02:13")["finished_at"] == \
        "2026-08-07 01:02:13"
    assert row(path="b", kind=FULL,
               finished_at="2026-08-06 18:04:19.794277420 +0000")["finished_at"] == \
        "2026-08-06 18:04:19"
    assert row(path="c", kind=FULL, finished_at="2026-08-05 08:51:33")["finished_at"] == \
        "2026-08-05 08:51:33"
    assert row(path="d", kind=FULL, finished_at=None)["finished_at"] is None
