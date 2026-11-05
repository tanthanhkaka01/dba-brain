"""A SQL Server restore chain is held to its LSNs before anything is restored (0.35.0, 1.250).

Gap G10 (2026-10-30), *chain completeness*: a missing ``.trn`` was found at ``RESTORE LOG`` - after the
FULL and the DIFF had been applied - and left the target half-restored, in RESTORING, with a message
that named an LSN and not the file. ``RESTORE HEADERONLY`` already gives every piece's LSNs when the
listing reads it; the chain is now checked from them first, and a break names the piece:

* a DIFF taken after another FULL (its ``DifferentialBaseLSN`` is not the FULL's ``CheckpointLSN``);
* a first log that starts after the FULL / DIFF ends - a log between them is missing;
* a log that does not start where the one before it ended.

What is not a break: a log that ends before the FULL / DIFF (SQL Server calls it too early, nothing
more), and an LSN the header did not give - not judged rather than guessed.
"""

from __future__ import annotations

import pytest

from dbabrain.lib import integrity as I

FULL = {"path": "/b/SALESDB/FULL/SALESDB_FULL_1.bak", "first_lsn": 900, "last_lsn": 1000, "checkpoint_lsn": 950}
DIFF = {"path": "/b/SALESDB/DIFF/SALESDB_DIFF_1.bak", "first_lsn": 1500, "last_lsn": 1600,
        "checkpoint_lsn": 1550, "differential_base_lsn": 950}


def _log(n, first, last):
    return {"path": f"D:\\SQLBK\\SALESDB\\LOG\\SALESDB_LOG_{n}.trn", "first_lsn": first, "last_lsn": last}


def test_an_unbroken_chain_holds():
    assert I.sqlserver_chain_break(FULL, DIFF, [_log(1, 1580, 1700), _log(2, 1700, 1800)]) == ""
    assert I.sqlserver_chain_break(FULL, None, [_log(1, 980, 1100), _log(2, 1100, 1200)]) == ""
    assert I.sqlserver_chain_break(FULL, None, []) == ""


def test_a_diff_of_another_full_is_named():
    stray = {**DIFF, "differential_base_lsn": 777}
    broken = I.sqlserver_chain_break(FULL, stray, [])
    assert "SALESDB_DIFF_1.bak is based on another FULL" in broken and "777" in broken


def test_a_missing_log_in_the_middle_names_the_file_after_the_gap():
    broken = I.sqlserver_chain_break(FULL, None, [_log(1, 980, 1100), _log(3, 1200, 1300)])
    assert "missing before SALESDB_LOG_3.trn" in broken and "1200" in broken and "1100" in broken


def test_logs_that_start_after_the_diff_ends_are_a_gap():
    broken = I.sqlserver_chain_break(FULL, DIFF, [_log(5, 1650, 1700)])
    assert "after the DIFF SALESDB_DIFF_1.bak ends at LSN 1600" in broken


def test_a_log_that_ends_before_the_full_is_too_early_not_a_break():
    assert I.sqlserver_chain_break(FULL, None, [_log(0, 800, 990), _log(1, 990, 1100)]) == ""


@pytest.mark.parametrize("log", [_log(1, 0, 1100), _log(1, 1100, 0)])
def test_an_lsn_the_header_did_not_give_is_not_judged(log):
    assert I.sqlserver_chain_break(FULL, None, [log, _log(9, 5000, 6000)]) == ""


def test_the_restore_plan_refuses_a_broken_chain_before_any_step(monkeypatch):
    from types import SimpleNamespace

    from dbabrain.backup_restore import restore_by_id

    listings = {"full": [FULL], "diff": [], "log": [_log(1, 980, 1100), _log(3, 1200, 1300)]}

    def listed(request):
        files = listings[request["kinds"][0]]
        return {"files": files, "newest_finished_at": "2026-11-01 01:00:00" if files else None}

    monkeypatch.setattr(restore_by_id, "_list_backup_files", listed)
    monkeypatch.setattr(restore_by_id, "_visible_dir", lambda job: "/b")
    monkeypatch.setattr(restore_by_id, "_sqlserver_port", lambda job, data_dir=None: 1433)
    job = SimpleNamespace(restore_id="SALES_DRILL", env={"MSSQL_DATABASES": "SALESDB", "MSSQL_USER": "dba"},
                          env_secrets={}, overwrite_existing=False)
    with pytest.raises(restore_by_id.RestoreByIdError,
                       match="SALESDB: the backup chain is broken - a log backup is missing before SALESDB_LOG_3.trn"):
        restore_by_id._plan_sqlserver(job, {}, point_in_time="", host={"host": "192.0.2.40"}, data_dir=None)
