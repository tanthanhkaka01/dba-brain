"""A share-driven restore hands STOPAT in the source's clock and is held to where the target says it stopped (0.39.0, 1.363 / 1.367).

1.363 found it on the restore from the repository: a SQL Server backup's header times and its log
records are in the SOURCE instance's clock, STOPAT is read in that clock, and the restore handed it
UTC - so from a Windows server on +07 every point in time landed seven hours early, and the restore
said done. That path was fixed then; the share-driven path (full, diff and each log applied by
``sqlcmd`` from a share) handed UTC all the same. It reads the offset the way the other path does -
a file named by its UTC start (dbabrain's ``_YYYYMMDD_HHMMSSZ``) against its header's
``BackupFinishDate`` - off the headers its chain check already reads, which now carry that date.

And 1.367's rule, *a restore is held to where it stopped*, reaches this path: after the check opens
each database, the target's own record (``restorehistory``, answered by ``verify-restore`` as
``restored_to``) is compared with what the database's restore planned - the moment of a STOPAT, or the
end of the chain's last file - and a database that opened clean somewhere else fails the run, before
the retention cleanup. A record nobody could read is *not read*, never a difference.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import restore_chain_check as cc
from dbabrain.backup_restore.cli import _held_to_the_plan
from dbabrain.backup_restore.restore_database import _restore_step, _where_to_stop

CANDIDATE = SimpleNamespace(restore_database_name="SALESDB_COPY", source_database_name="APP")


def _head(name, kind, finished):
    return {"path": f"E:\\SQLBK_IMPORT\\APP\\{name}", "backup_type": kind, "finished_at": finished}


#: dbabrain's own names (UTC start, ``Z``) on a +07 source: each header ends 7 hours "later".
HEADS = [_head("APP_FULL_20261112_040000Z.bak", "1", "2026-11-12 11:03:10"),
         _head("APP_LOG_20261112_050000Z.trn", "2", "2026-11-12 12:00:04"),
         _head("APP_LOG_20261112_060000Z.trn", "2", "2026-11-12 13:00:05"),
         _head("APP_LOG_20261112_070000Z.trn", "2", "2026-11-12 14:00:03")]
LOGS = ["log1", "log2", "log3"]


def _utc(text):
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------- #
# The headers carry the finish date
# --------------------------------------------------------------------------- #
def test_the_header_batch_reads_the_finish_date_as_a_datetime_with_its_seconds():
    sql = cc.build_header_sql(["a.bak"])

    assert "ALTER TABLE #h ALTER COLUMN c19 datetime NULL;" in sql
    assert sql.index("ALTER COLUMN c19") < sql.index("INSERT INTO #h EXEC")
    assert "COALESCE(CONVERT(nvarchar(19), c19, 120), N'')" in sql


def test_a_header_line_gives_its_finish_date_and_an_older_line_without_one_still_reads():
    line9 = f"{cc.MARKER}0|2|1|APP|100|200|0||2026-11-12 13:00:05"
    line8 = f"{cc.MARKER}1|2|1|APP|200|300|0|"

    heads, errors = cc.parse_headers(line9 + "\n" + line8, ["a.trn", "b.trn"])

    assert errors == []
    assert heads[0]["finished_at"] == "2026-11-12 13:00:05" and heads[0]["last_lsn"] == 200
    assert heads[1]["finished_at"] is None and heads[1]["first_lsn"] == 200


# --------------------------------------------------------------------------- #
# The moment, in the source's clock
# --------------------------------------------------------------------------- #
def test_a_moment_is_handed_to_sql_server_in_the_sources_clock():
    stopat, expect = _where_to_stop(CANDIDATE, LOGS, HEADS, _utc("2026-11-12 05:30:00"))

    assert stopat == "2026-11-12 12:30:00", "05:30 UTC is 12:30 on the +07 source"
    assert expect == {"database_name": "SALESDB_COPY", "source_database": "APP", "moment": "2026-11-12 12:30:00",
                      "stopat": True, "chain_ends": "2026-11-12 14:00:03"}


def test_the_step_carries_that_moment_and_sql_server_is_handed_it_with_no_offset(tmp_path):
    from dbabrain.backup_restore.restore_database import build_restore_candidate
    from dbabrain.common.restorestep.sqlserver import build_statements
    from tests.test_backup_restore import make_config

    config = make_config(tmp_path)
    candidate = build_restore_candidate(config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak", config)
    log = config.vm_import_unc / "APPDB_Prod" / "LOG" / "log3.trn"

    in_source = _restore_step("log", candidate, config, path=log, recovery=True,
                              stopat_utc=_utc("2026-11-12 05:30:00"), stopat_source="2026-11-12 12:30:00")
    unknown = _restore_step("log", candidate, config, path=log, recovery=True, stopat_utc=_utc("2026-11-12 05:30:00"))

    assert in_source["stopat"] == "2026-11-12 12:30:00"
    assert unknown["stopat"] == "2026-11-12T05:30:00+00:00", "an offset nobody could read: as it was handed"
    (text,) = build_statements("log", in_source, [str(in_source["backup_path"])])
    assert "2026-11-12T12:30:00" in text and "STOPAT" in text


def test_a_name_with_no_utc_stamp_leaves_the_moment_as_it_was_handed():
    """A backup another tool wrote stamps local time and carries no marker: the offset cannot be read
    off it, and nothing is guessed - as on the other path."""
    heads = [_head("APP_FULL_20261112_110000.bak", "1", "2026-11-12 11:03:10"),
             _head("APP_LOG_20261112_130000.trn", "2", "2026-11-12 13:00:05")]

    stopat, expect = _where_to_stop(CANDIDATE, ["log"], heads, _utc("2026-11-12 05:30:00"))

    assert stopat is None
    assert expect["stopat"] is True and expect["moment"] == "2026-11-12T05:30:00+00:00"


def test_a_moment_past_the_newest_log_is_held_to_the_chains_end_with_no_stopat():
    stopat, expect = _where_to_stop(CANDIDATE, LOGS, HEADS, _utc("2026-11-12 09:00:00"))

    assert stopat is None
    assert expect["stopat"] is False and expect["chain_ends"] == "2026-11-12 14:00:03"


def test_a_latest_restore_is_held_to_the_end_of_its_chain():
    stopat, expect = _where_to_stop(CANDIDATE, LOGS, HEADS, None)

    assert stopat is None and expect["stopat"] is False and expect["chain_ends"] == "2026-11-12 14:00:03"


def test_headers_that_could_not_be_read_hold_it_to_nothing():
    stopat, expect = _where_to_stop(CANDIDATE, LOGS, None, None)

    assert stopat is None and expect["chain_ends"] == "" and expect["stopat"] is False


# --------------------------------------------------------------------------- #
# Held to the target's own record
# --------------------------------------------------------------------------- #
def _output(expect):
    return {"results": [{"status": "SUCCESS", "database_name": expect["database_name"], "expect": expect},
                        {"status": "FAILED", "database_name": "OTHER"}]}


def _verified(**restored_to):
    return {"databases": [{"database_name": "SALESDB_COPY", "state": "ONLINE", "ok": True,
                           **({"restored_to": restored_to} if restored_to else {})}]}


def test_a_database_at_the_end_of_its_chain_is_landed_and_not_off_target():
    _stopat, expect = _where_to_stop(CANDIDATE, LOGS, HEADS, None)

    landed, off = _held_to_the_plan(_output(expect), _verified(last_backup_finished="2026-11-12 14:00:03"))

    assert off == [] and landed[0]["verdict"] == "restored to the end of its chain"


def test_a_database_that_stopped_short_of_its_chain_is_named():
    _stopat, expect = _where_to_stop(CANDIDATE, LOGS, HEADS, None)

    landed, off = _held_to_the_plan(_output(expect), _verified(last_backup_finished="2026-11-12 12:00:04"))

    assert landed[0]["verdict"] == "NOT at the end of its chain"
    assert off == ["SALESDB_COPY: its chain ends 2026-11-12 14:00:03, the last backup the target applied ended "
                   "2026-11-12 12:00:04"]


def test_a_stopat_the_target_did_not_record_is_named_and_one_it_did_is_not():
    _stopat, expect = _where_to_stop(CANDIDATE, LOGS, HEADS, _utc("2026-11-12 05:30:00"))

    _landed, right = _held_to_the_plan(_output(expect), _verified(stop_at="2026-11-12 12:30:00"))
    _landed, wrong = _held_to_the_plan(_output(expect), _verified(stop_at="2026-11-12 05:30:00"))

    assert right == []
    assert wrong == ["SALESDB_COPY: asked to stop at 2026-11-12 12:30:00, the target recorded 2026-11-12 05:30:00"]


@pytest.mark.parametrize("output", [{}, {"results": [{"status": "SUCCESS"}]}, None])
def test_a_restore_that_planned_no_stop_point_is_held_to_nothing(output):
    assert _held_to_the_plan(output, _verified(stop_at="x")) == ([], [])


def test_a_target_that_gave_no_record_is_not_read_not_a_difference():
    _stopat, expect = _where_to_stop(CANDIDATE, LOGS, HEADS, None)

    landed, off = _held_to_the_plan(_output(expect), _verified())

    assert off == [] and landed[0]["verdict"] == "not read"


# --------------------------------------------------------------------------- #
# The workflow fails a run whose database opened somewhere else
# --------------------------------------------------------------------------- #
def test_the_workflow_fails_before_the_cleanup_when_a_database_stopped_short(tmp_path, monkeypatch):
    import dataclasses

    from dbabrain.backup_restore import cli
    from dbabrain.backup_restore.config import DatabaseRestoreMapping
    from dbabrain.backup_restore.copy_backup import CopyBackupResult
    from dbabrain.lib.config import DbaBrainConfig
    from tests.test_backup_restore import make_config

    _stopat, expect = _where_to_stop(CANDIDATE, LOGS, HEADS, None)
    config = dataclasses.replace(
        make_config(tmp_path), restore_id="NIGHTLY",
        databases=(DatabaseRestoreMapping(source_database="APP", target_database="SALESDB_COPY"),),
        restore_all_databases=False, restore_database_name="", vm_credential_target="192.0.2.10",
        restore_sql_username="sa", restore_sql_password_env="TARGET_SA", restore_sql_instance_on_vm="localhost,1433")
    deleted = []
    monkeypatch.setattr(cli, "run_target_preflight", lambda config, logger=None, **_: None)
    monkeypatch.setattr(cli, "run_copy_backup", lambda step_config, logger=None, force=False, catalog=None: CopyBackupResult(
        returncode=0, source_backup_dir=tmp_path, local_import_dir=tmp_path, files_considered=2,
        copied=2, skipped=0, file_results=()))
    monkeypatch.setattr(cli, "run_restore_all_latest", lambda **_: {
        "status": "SUCCESS", "per_database_restore_status": {"SALESDB_COPY": "SUCCESS"},
        "results": [{"status": "SUCCESS", "database_name": "SALESDB_COPY", "expect": expect}]})
    monkeypatch.setattr(cli, "run_delete_backup", lambda *a, **k: deleted.append(1))
    monkeypatch.setattr(cli, "_load_secrets", lambda **_: {"TARGET_SA": "x"})
    monkeypatch.setattr(cli.common_cli, "run", lambda op, request: {
        "ok": True, "checked": 1, "failed": 0,
        "databases": [{"database_name": "SALESDB_COPY", "state": "ONLINE", "ok": True,
                       "restored_to": {"last_backup_finished": "2026-11-12 12:00:04"}}]})

    with pytest.raises(cli.RestoreWorkflowError, match="restored and checked, but not to what was asked") as failed:
        cli.run_restore_workflow(
            restore_configs=[config],
            app_config=DbaBrainConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                                      sqlite_path=tmp_path / "runtime" / "dbabrain.sqlite"))

    assert "NIGHTLY: SALESDB_COPY: its chain ends 2026-11-12 14:00:03" in str(failed.value)
    assert "Retention cleanup was not run" in str(failed.value) and deleted == []
