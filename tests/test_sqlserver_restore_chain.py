"""Choosing a SQL Server restore chain, especially for a point in time.

This is the module that decides whether a recovery lands on the right second, so the cases here
are the awkward ones on purpose. All of them are silent failures in the shape the chain is usually
picked - by sorting file names:

* a differential whose base full was superseded by a later full still sorts last, and restores
  cleanly onto the wrong base;
* a full taken *after* the target moment sorts newest and looks like the obvious choice, while its
  data already contains changes past the moment being recovered to;
* a point in time past the end of the logs has no chain at all, and rounding it down produces a
  restore that succeeds and is hours short of what was asked for.

None of those raise an error at the instance. They produce a database that looks restored.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from db_ops.common.restore.sqlserver import (
    DIFF,
    FULL,
    LOG,
    BackupHeader,
    RestoreChainError,
    build_restore_statements,
    parse_headers,
    select_chain,
)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 6, hour, minute)


def _h(path, kind, first, last, *, checkpoint=0, base=0, finish=None, db="APPDB"):
    return BackupHeader(path=path, database_name=db, backup_type=kind,
                        first_lsn=first, last_lsn=last, checkpoint_lsn=checkpoint,
                        database_backup_lsn=base, backup_finish_date=finish)


# A day with two fulls, differentials against each, and hourly logs.
FULL_01 = _h("full_01.bak", FULL, 100, 110, checkpoint=100, finish=_at(1))
DIFF_03 = _h("diff_03.bak", DIFF, 120, 130, base=100, finish=_at(3))
FULL_05 = _h("full_05.bak", FULL, 200, 210, checkpoint=200, finish=_at(5))
DIFF_07 = _h("diff_07.bak", DIFF, 220, 230, base=200, finish=_at(7))
LOGS = [
    _h("log_02.trn", LOG, 110, 120, finish=_at(2)),
    _h("log_04.trn", LOG, 130, 200, finish=_at(4)),
    _h("log_06.trn", LOG, 210, 220, finish=_at(6)),
    _h("log_08.trn", LOG, 230, 240, finish=_at(8)),
    _h("log_09.trn", LOG, 240, 250, finish=_at(9)),
]
ALL = [FULL_01, DIFF_03, FULL_05, DIFF_07, *LOGS]


# --------------------------------------------------------------------------- #
# The base.
# --------------------------------------------------------------------------- #

def test_the_newest_full_before_the_moment_is_the_base():
    chain = select_chain(ALL, database="APPDB", point_in_time=_at(8, 30))
    assert chain.full is FULL_05


def test_a_full_taken_after_the_moment_is_never_the_base():
    """It sorts newest and looks obvious, but its data already contains changes past the moment,
    and no amount of log restoring removes them."""
    chain = select_chain(ALL, database="APPDB", point_in_time=_at(4))
    assert chain.full is FULL_01


def test_no_usable_full_is_refused_rather_than_approximated():
    with pytest.raises(RestoreChainError, match="no FULL backup"):
        select_chain(ALL, database="APPDB", point_in_time=_at(0, 30))


# --------------------------------------------------------------------------- #
# The differential, which is where file-name sorting goes wrong.
# --------------------------------------------------------------------------- #

def test_a_differential_chained_to_a_different_full_is_not_used():
    """DIFF_03's base is FULL_01. Restoring to 08:30 uses FULL_05, so DIFF_03 must be ignored -
    it sorts perfectly well and would restore cleanly onto the wrong base."""
    chain = select_chain(ALL, database="APPDB", point_in_time=_at(8, 30))
    assert chain.diff is DIFF_07
    assert DIFF_03 not in chain.ordered


def test_the_differential_is_matched_by_lsn_not_by_time():
    """DIFF_03 is newer than FULL_01 and older than FULL_05; only its database_backup_lsn says
    which one it belongs to."""
    chain = select_chain(ALL, database="APPDB", point_in_time=_at(4))
    assert chain.full is FULL_01
    assert chain.diff is DIFF_03


def test_a_chain_with_no_usable_differential_still_restores():
    chain = select_chain([FULL_05, *LOGS], database="APPDB", point_in_time=_at(8, 30))
    assert chain.diff is None
    assert chain.full is FULL_05


# --------------------------------------------------------------------------- #
# The logs, and the moment.
# --------------------------------------------------------------------------- #

def test_logs_stop_at_the_one_containing_the_moment():
    """One short recovers to an earlier second than asked; one further cannot be applied at all,
    because a log cannot follow RECOVERY."""
    chain = select_chain(ALL, database="APPDB", point_in_time=_at(8, 30))
    assert [h.path for h in chain.logs] == ["log_08.trn", "log_09.trn"]
    assert chain.stopat == _at(8, 30)


def test_only_logs_after_the_base_are_applied():
    """DIFF_07 ends at LSN 230, so log_06 (which ends at 220) is already inside it."""
    chain = select_chain(ALL, database="APPDB", point_in_time=_at(8, 30))
    assert "log_06.trn" not in [h.path for h in chain.logs]


def test_a_moment_past_the_end_of_the_logs_is_refused():
    """Rounding down produces a restore that succeeds and is hours short of what was asked for,
    with nothing in the outcome to say so."""
    with pytest.raises(RestoreChainError, match="no log backup covers"):
        select_chain(ALL, database="APPDB", point_in_time=_at(23))


def test_without_a_moment_every_log_after_the_base_is_applied():
    chain = select_chain(ALL, database="APPDB", point_in_time=None)
    assert [h.path for h in chain.logs] == ["log_08.trn", "log_09.trn"]
    assert chain.stopat is None


def test_another_database_in_the_same_headers_is_ignored():
    """A shared backup directory holds every database's files; picking by name is not optional."""
    other = _h("other_full.bak", FULL, 900, 910, checkpoint=900, finish=_at(7), db="PAYROLL")
    chain = select_chain([*ALL, other], database="APPDB", point_in_time=_at(8, 30))
    assert other not in chain.ordered


def test_an_unknown_database_is_refused():
    with pytest.raises(RestoreChainError, match="No backups found for database"):
        select_chain(ALL, database="NOPE", point_in_time=None)


# --------------------------------------------------------------------------- #
# Reading headers.
# --------------------------------------------------------------------------- #

def test_header_rows_are_read_by_sql_servers_own_type_codes():
    rows = [
        {"path": "a.bak", "DatabaseName": "APPDB", "BackupType": "1", "FirstLSN": 1, "LastLSN": 2,
         "CheckpointLSN": 1},
        {"path": "b.trn", "DatabaseName": "APPDB", "BackupType": "2", "FirstLSN": 2, "LastLSN": 3},
        {"path": "c.bak", "DatabaseName": "APPDB", "BackupType": "5", "FirstLSN": 2, "LastLSN": 3},
    ]
    assert [h.backup_type for h in parse_headers(rows)] == [FULL, LOG, DIFF]


def test_an_unrecognised_backup_type_is_dropped_not_guessed():
    """A file-copy-only or partial backup treated as a full restores cleanly and is wrong."""
    rows = [{"path": "x.bak", "DatabaseName": "APPDB", "BackupType": "4",
             "FirstLSN": 1, "LastLSN": 2}]
    assert parse_headers(rows) == []


# --------------------------------------------------------------------------- #
# The statements.
# --------------------------------------------------------------------------- #

def test_only_the_last_statement_recovers():
    """A chain that recovers early cannot have its remaining logs applied at all, and the only
    fix is to start the entire restore again."""
    chain = select_chain(ALL, database="APPDB", point_in_time=_at(8, 30))
    statements = build_restore_statements(chain, database="APPDB")

    assert all("NORECOVERY" in s for s in statements[:-1])
    assert "WITH RECOVERY" in statements[-1] or statements[-1].count("RECOVERY") == 1
    assert "NORECOVERY" not in statements[-1]


def test_stopat_goes_on_the_final_log_and_nowhere_else():
    """SQL Server accepts STOPAT on a full or a differential and silently ignores it there, which
    reads as a point-in-time restore that never happened."""
    chain = select_chain(ALL, database="APPDB", point_in_time=_at(8, 30))
    statements = build_restore_statements(chain, database="APPDB")

    with_stopat = [s for s in statements if "STOPAT" in s]
    assert len(with_stopat) == 1
    assert "log_09.trn" in with_stopat[0]
    assert with_stopat[0].startswith("RESTORE LOG")
    assert "2026-08-06T08:30:00" in with_stopat[0]


def test_no_stopat_when_no_moment_was_asked_for():
    chain = select_chain(ALL, database="APPDB", point_in_time=None)
    assert not any("STOPAT" in s for s in build_restore_statements(chain, database="APPDB"))


def test_move_is_emitted_once_on_the_full():
    """Repeating MOVE on a log is rejected by SQL Server; omitting it on the full puts the data
    files wherever the source had them, which on another machine is usually nowhere."""
    chain = select_chain(ALL, database="APPDB", point_in_time=_at(8, 30))
    statements = build_restore_statements(
        chain, database="APPDB",
        move={"APPDB": "/var/opt/mssql/data/APPDB.mdf", "APPDB_log": "/var/opt/mssql/data/APPDB.ldf"},
    )

    assert sum("MOVE" in s for s in statements) == 1
    assert "MOVE" in statements[0]


def test_identifiers_and_paths_are_quoted_against_injection():
    chain = select_chain(
        [_h("o'brien.bak", FULL, 1, 2, checkpoint=1, finish=_at(1), db="we[ird]")],
        database="we[ird]", point_in_time=None,
    )
    statement = build_restore_statements(chain, database="we[ird]")[0]

    assert "[we[ird]]]" in statement          # closing bracket doubled
    assert "N'o''brien.bak'" in statement     # quote doubled


# --------------------------------------------------------------------------- #
# The connection a restore needs.
# --------------------------------------------------------------------------- #

def test_the_restore_connection_has_no_statement_timeout(monkeypatch):
    """Measured, not assumed: leaving it unset capped every statement at 30 seconds and the first
    real restore died mid-chain with HYT00, leaving the database RESTORING - which reads like a
    broken restore rather than a default. `None` means "reuse the connect timeout" one layer down,
    so 0 has to be said out loud.
    """
    from db_ops.common.restore.sqlserver import runner

    captured = {}
    monkeypatch.setattr("db_ops.common.db_connect.connect_engine",
                        lambda **kwargs: captured.update(kwargs) or object())

    class _Spec:
        class target:
            host, port, username, password = "h", 1433, "sa", "p"

    runner.connect_target(_Spec())

    assert captured["statement_timeout_seconds"] == 0
    assert captured["autocommit"] is True      # RESTORE is refused inside a user transaction
    assert captured["database"] == "master"
