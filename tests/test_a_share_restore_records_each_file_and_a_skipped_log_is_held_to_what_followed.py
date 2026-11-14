"""A share-driven restore records each file it applies, and a log it passes over is held to what followed (0.39.0, 1.367).

The operator, 2026-11-13: *dbabrain must keep the full chain of files a restore used, so it can be
shown which file went into which restore*. The restore from the repository has written
``restore_chain_files`` since 1.367; the share-driven SQL Server path - full, diff and each log applied
by ``sqlcmd`` from a share - wrote nothing. It does now, one row per file on the copy's run, each
``planned`` before its database's first RESTORE and then ``applied``, ``skipped`` or ``failed``.

**Skipped**, a result of its own (the operator chose it, 2026-11-14, over recording such a log as
``failed`` - a restore that worked would show a failed file every night and teach its reader to look
past one - or as ``planned``, which reads as never reached). SQL Server refuses two kinds of log the
restore passes over:

- **Msg 4326** - the log ends before the database's restore point (a log taken the same minute as,
  just before, the full). It holds nothing the database needs: ``skipped`` with that reason.
- **Msg 4305** - the log begins AFTER the restore point. Either the file is out of the chain - a later
  log then goes in, and it is ``skipped`` - or a log is MISSING before it.

**The defect read on the way.** Every 4305 was passed over and the database came out SUCCESS as long
as one log had gone in. Behind a real gap every later log is 4305 too: all passed over, the database
recovered at the gap - hours short of what was asked - and the run said done. The chain check before
the restore catches a gap only when it can read the backups' headers; when it cannot it says *not
judged* and the restore goes on. Now a 4305 after which no later log goes in fails the database,
naming the log - and its row, and those after it, say ``failed``.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import patch_restore
from test_backup_restore import make_config

from dbabrain.backup_restore.restore_database import (
    ChainRecord,
    _chain_rows,
    build_restore_candidate,
    run_restore_log,
)
from dbabrain.db import backup_catalog
from dbabrain.db.backup_catalog import BackupCatalogStore

TOO_RECENT = ("Msg 4305, Level 16, State 1. The log in this backup set begins at LSN 200, "
              "which is too recent to apply to the database.")
TOO_EARLY = ("Msg 4326, Level 16, State 1. The log in this backup set terminates at LSN 90, "
             "which is too early to apply to the database.")


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _run(catalog):
    return catalog.start_restore_run(action="restore", restore_id="SHARE", source_server_id="SRC",
                                     target_server_id="TGT", copy_max_retry=3)


def _row(name, step="restore-log"):
    return {"source_server_id": "SRC", "target_server_id": "TGT", "database_name": "APP", "restored_as": "APP",
            "step": step, "file_name": name, "file_path": f"E:\\SQLBK_IMPORT\\APP\\{name}"}


# --------------------------------------------------------------------------- #
# The catalog: skipped is a result, and a run's chain grows database by database
# --------------------------------------------------------------------------- #
def test_skipped_is_a_result_and_keeps_its_reason_with_no_applied_time(catalog):
    run = _run(catalog)
    catalog.plan_chain(run, [_row("APP_FULL.bak", "restore-full"), _row("APP_LOG_1.trn")])

    catalog.set_chain_step(run, [2], result="skipped", error_text="Msg 4326: ends before the restore point")

    row = catalog.chain_of(run)[1]
    assert (row["result"], row["error_text"], row["applied_at"]) == (
        "skipped", "Msg 4326: ends before the restore point", None)


def test_the_store_itself_refuses_a_result_outside_the_list_by_a_check_of_ours(catalog):
    run = _run(catalog)
    catalog.plan_chain(run, [_row("APP_LOG_1.trn")])

    with sqlite3.connect(catalog.sqlite_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="ck_restore_chain_files_result"):
            conn.execute("UPDATE restore_chain_files SET result = 'ignored'")


def test_the_live_stores_column_check_is_replaced_by_the_named_one_listing_skipped():
    """The live store (PostgreSQL) holds the table with version 8's column CHECK, named by PostgreSQL:
    the migration drops it and adds ours with today's list - the way ``offload`` joined the run actions."""
    assert ("restore_chain_files", "result", backup_catalog.CHAIN_RESULTS) in backup_catalog.WIDENED_CHECKS
    assert backup_catalog.CATALOG_SCHEMA_VERSION == 9
    assert "'skipped'" in backup_catalog._named_check("restore_chain_files", "result", backup_catalog.CHAIN_RESULTS)


def test_a_runs_chain_grows_one_database_after_another_in_order(catalog):
    run = _run(catalog)

    first = catalog.append_chain(run, [_row("APP_FULL.bak", "restore-full"), _row("APP_LOG_1.trn")])
    second = catalog.append_chain(run, [{**_row("HR_FULL.bak", "restore-full"), "database_name": "HR"}])

    assert (first, second) == ([1, 2], [3])
    assert [(row["position"], row["file_name"], row["result"]) for row in catalog.chain_of(run)] == [
        (1, "APP_FULL.bak", "planned"), (2, "APP_LOG_1.trn", "planned"), (3, "HR_FULL.bak", "planned")]


def test_the_record_ties_a_file_to_its_catalogued_backup_and_a_broken_store_is_said_not_raised(catalog, monkeypatch):
    file_id = catalog.record_backup_file(
        server_id="SRC", database_name="APP", engine="sqlserver", file_name="APP_LOG_1.trn", file_type="LOG",
        file_path="C:\\SQLBK\\APP\\LOG\\APP_LOG_1.trn", checksum_after_backup="a" * 64, verdict="ok",
        size_bytes=10, backup_ended_at="2026-11-13T01:00:00Z")
    record = ChainRecord(catalog, _run(catalog), "SRC", "TGT")

    assert record.plan([_row("APP_LOG_1.trn")]) == [1]
    row = catalog.chain_of(record.restore_run_id)[0]
    assert (row["file_id"], row["file_type"], row["backup_ended_at"]) == (file_id, "LOG", "2026-11-13T01:00:00Z")

    said = []
    patch_restore(monkeypatch, "_emit_restore_log", lambda _logger, message, **_kw: said.append(message))
    broken = ChainRecord(object(), 7, "SRC", "TGT")
    assert broken.plan([_row("APP_LOG_1.trn")]) == []
    broken.mark([1], "applied")      # nothing to write to, and nothing raised
    assert said and said[0].startswith("restore-db chain not-recorded ")


# --------------------------------------------------------------------------- #
# The rows one database is about to apply
# --------------------------------------------------------------------------- #
def test_the_rows_are_full_diff_then_each_log_by_the_path_the_target_reads(tmp_path):
    import datetime

    config = make_config(tmp_path)
    folder = config.vm_import_unc / "APPDB_Prod"
    candidate = build_restore_candidate(folder / "FULL" / "latest.bak", config)
    logs = [folder / "LOG" / "log_1.trn", folder / "LOG" / "log_2.trn"]
    moment = datetime.datetime(2026, 11, 13, 1, 30, tzinfo=datetime.timezone.utc)
    record = ChainRecord(None, 1, "SRC", "TGT")

    latest = _chain_rows(record, candidate, config, folder / "DIFF" / "diff.bak", logs, None)
    pitr = _chain_rows(record, candidate, config, None, logs, moment)

    assert [(row["step"], row["file_name"]) for row in latest] == [
        ("restore-full", "latest.bak"), ("restore-diff", "diff.bak"),
        ("restore-log", "log_1.trn"), ("restore-log", "log_2.trn")]
    assert all(row["file_path"].startswith("E:\\SQLBK_IMPORT") for row in latest)
    assert [(row["with_recovery"], row["stopat"]) for row in latest] == [(0, None)] * 4, \
        "a latest restore recovers in a step of its own, after the chain"
    assert [(row["with_recovery"], row["stopat"]) for row in pitr] == [
        (0, None), (0, None), (1, "2026-11-13T01:30:00Z")]


# --------------------------------------------------------------------------- #
# The logs: what became of each, and a 4305 held to what followed it
# --------------------------------------------------------------------------- #
@pytest.fixture
def logs_run(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    folder = config.vm_import_unc / "APPDB_Prod"
    candidate = build_restore_candidate(folder / "FULL" / "latest.bak", config)
    patch_restore(monkeypatch, "log_event", lambda *_a, **_k: None)

    def go(answers: list[str]):
        logs = [folder / "LOG" / f"log_{n}.trn" for n in range(1, len(answers) + 1)]
        replies = iter(answers)

        def step(**_kwargs):
            reply = next(replies)
            if reply != "ok":
                raise RuntimeError(reply)
            return {"step": "restore-log", "status": "SUCCESS", "stdout": "", "stderr": ""}

        patch_restore(monkeypatch, "_run_restore_step", step)
        said: list[tuple[int, str, str]] = []
        outcome = run_restore_log(config=config, candidate=candidate, selected_backups=logs, logger=object(),
                                  on_file=lambda index, result, reason="": said.append((index, result, reason)))
        return outcome, said

    return go


def _results(said):
    return sorted((index, result) for index, result, _reason in said)


def test_each_log_applied_is_said_applied(logs_run):
    outcome, said = logs_run(["ok", "ok"])

    assert outcome["status"] == "SUCCESS" and _results(said) == [(1, "applied"), (2, "applied")]


def test_a_log_that_ends_before_the_restore_point_is_skipped_with_its_reason(logs_run):
    outcome, said = logs_run([TOO_EARLY, "ok"])

    assert outcome["status"] == "SUCCESS" and _results(said) == [(1, "skipped"), (2, "applied")]
    assert said[0][2].startswith("Msg 4326: the log ends before the database's restore point")


def test_a_too_recent_log_a_later_log_went_in_after_is_a_file_out_of_the_chain_and_skipped(logs_run):
    outcome, said = logs_run(["ok", TOO_RECENT, "ok"])

    assert outcome["status"] == "SUCCESS"
    assert _results(said) == [(1, "applied"), (2, "skipped"), (3, "applied")]
    assert "a later log of the chain went in - a file out of the chain" in dict(
        ((index, reason) for index, _result, reason in said))[2]


def test_a_too_recent_log_nothing_went_in_after_fails_the_database_where_it_used_to_say_success(logs_run):
    """The defect: L1 in, a log missing, L2 and L3 refused as too recent - the database was recovered at
    L1's end and the run said SUCCESS."""
    with pytest.raises(RuntimeError, match="missing required earlier LOG backup / LSN gap") as failed:
        logs_run(["ok", TOO_RECENT, TOO_RECENT])

    text = str(failed.value)
    assert "log_2.trn and the 1 log(s) after it were refused as too recent to apply (Msg 4305)" in text
    assert "1 log(s) applied before it" in text and "short of what was asked" in text


def test_the_rows_of_the_gap_say_failed_and_name_where_the_log_is_missing(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    folder = config.vm_import_unc / "APPDB_Prod"
    candidate = build_restore_candidate(folder / "FULL" / "latest.bak", config)
    patch_restore(monkeypatch, "log_event", lambda *_a, **_k: None)
    replies = iter(["ok", TOO_RECENT, TOO_RECENT])

    def step(**_kwargs):
        reply = next(replies)
        if reply != "ok":
            raise RuntimeError(reply)
        return {"step": "restore-log", "status": "SUCCESS", "stdout": "", "stderr": ""}

    patch_restore(monkeypatch, "_run_restore_step", step)
    said = []
    with pytest.raises(RuntimeError):
        run_restore_log(config=config, candidate=candidate, logger=object(),
                        selected_backups=[folder / "LOG" / f"log_{n}.trn" for n in (1, 2, 3)],
                        on_file=lambda index, result, reason="": said.append((index, result, reason)))

    assert _results(said) == [(1, "applied"), (2, "failed"), (3, "failed")]
    assert all(reason.endswith("a log is missing before log_2.trn") for index, _r, reason in said if index > 1)


def test_every_log_refused_as_too_recent_fails_with_no_log_applied(logs_run):
    with pytest.raises(RuntimeError, match="no log applied"):
        logs_run([TOO_RECENT, TOO_RECENT])


def test_any_other_error_still_fails_the_restore_at_that_log(logs_run):
    with pytest.raises(RuntimeError, match="Msg 3201"):
        logs_run(["ok", "Msg 3201: cannot open backup device", "ok"])


def test_a_record_that_raises_never_stops_the_restore(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    folder = config.vm_import_unc / "APPDB_Prod"
    candidate = build_restore_candidate(folder / "FULL" / "latest.bak", config)
    patch_restore(monkeypatch, "log_event", lambda *_a, **_k: None)
    patch_restore(monkeypatch, "_run_restore_step",
                  lambda **_k: {"step": "restore-log", "status": "SUCCESS", "stdout": "", "stderr": ""})

    def broken(*_args):
        raise OSError("the store is down")

    outcome = run_restore_log(config=config, candidate=candidate, logger=object(),
                              selected_backups=[folder / "LOG" / "log_1.trn"], on_file=broken)

    assert outcome["status"] == "SUCCESS"


# --------------------------------------------------------------------------- #
# The whole database: its rows written before the first RESTORE, then marked
# --------------------------------------------------------------------------- #
def _restore_database(tmp_path, monkeypatch, catalog, *, full_fails=False):
    import dataclasses
    import subprocess

    from dbabrain.backup_restore import restore_database as restore_module
    from dbabrain.lib.config import DbaBrainConfig

    config = dataclasses.replace(make_config(tmp_path), source_id="SRC", target_id="TGT",
                                 restore_sql_instance_on_vm="localhost", checkdb=False)
    backup = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    app_config = DbaBrainConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                                sqlite_path=tmp_path / "runtime" / "dbabrain.sqlite")

    patch_restore(monkeypatch, "run_sqlcmd_query_command",
                  lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, "complete", ""))
    if full_fails:
        def full(**_kwargs):
            raise RuntimeError("Msg 3201: cannot open backup device")

        patch_restore(monkeypatch, "run_restore_full", full)
    patch_restore(monkeypatch, "log_event", lambda *_a, **_k: None)
    # Whether TGT may be restored onto is another file's (the production-target rule); here it is.
    patch_restore(monkeypatch, "assert_target_is_allowed", lambda **_k: None)
    record = ChainRecord(catalog, _run(catalog), "SRC", "TGT")
    run = lambda: restore_module.run_restore_database(  # noqa: E731
        config=config, dbabrain_config=app_config, backup_file=backup, ensure_certificate=False,
        ensure_credential=False, logger=object(), chain=record)
    return run, record


def test_a_restored_databases_full_is_written_planned_then_applied(tmp_path, monkeypatch, catalog):
    run, record = _restore_database(tmp_path, monkeypatch, catalog)

    assert run()["status"] == "SUCCESS"

    rows = catalog.chain_of(record.restore_run_id)
    assert [(row["position"], row["step"], row["file_name"], row["result"]) for row in rows] == [
        (1, "restore-full", "latest.bak", "applied")]
    assert rows[0]["file_path"].startswith("E:\\SQLBK_IMPORT") and rows[0]["applied_at"]


def test_a_full_that_fails_is_failed_with_the_engines_error(tmp_path, monkeypatch, catalog):
    run, record = _restore_database(tmp_path, monkeypatch, catalog, full_fails=True)

    with pytest.raises(Exception, match="Msg 3201"):
        run()

    row = catalog.chain_of(record.restore_run_id)[0]
    assert row["result"] == "failed" and "Msg 3201" in row["error_text"]
