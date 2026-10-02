"""A file already staged is not counted twice, and a restore is measured by the database it builds
when its entry asks.

On 2026-10-01 the 100.250 drill passed its space check - *56.8 GiB to copy, x2 = 113.7 GiB needed,
452.3 GiB free - fits* - and the restore then built one database at **366.6 GB** of data and log
files: a compressed FULL of 30.1 GiB expands about twelvefold. The target was left at 96%, 21 GiB
free. And the next run of the same entry would have been refused, because the check counted the
chain again with every file of it already on the target (0.26.0 §1.76).

Two things came of it:

* **before the copy, only the files still to stage are counted** - what the target already holds at
  its size is left alone by the copy, so it takes no room. Always.
* **before each database's first RESTORE, the database can be measured** - the files the backup says
  it holds, less the files the restore writes over, asked of the target's own SQL Server. **Only
  when the entry says** ``space_check.measure_restore: true``. The operator's ruling (2026-10-02):
  the engineer who sets up a restore knows the disk has to hold the database; by default the rule is
  the copy at x2 and nothing else, on every engine. An entry that asks is held to it - a shortfall
  is refused, and so is a measurement that cannot be made, unless it also says ``on_unknown:
  proceed``.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from db_ops.backup_restore import copy_backup, space
from db_ops.backup_restore.config import BackupRestoreConfig, DatabaseRestoreMapping
from db_ops.lib import restore_space
from db_ops.lib.config import DbOpsConfig

GIB = 1024 ** 3


# --------------------------------------------------------------------------- #
# Before the copy: what is staged is not counted
# --------------------------------------------------------------------------- #
def test_a_file_already_staged_at_its_size_is_not_counted():
    to_write, staged = restore_space.remaining_bytes(
        {"DB/FULL/a.bak": 30 * GIB, "DB/LOG/b.trn": 1 * GIB}, {"DB/FULL/a.bak": 30 * GIB})

    assert (to_write, staged) == (1 * GIB, 30 * GIB)


def test_a_staged_file_of_another_size_is_written_again_whole():
    """The copy replaces it, so it is counted - a half-copied FULL is not room already paid for."""
    to_write, staged = restore_space.remaining_bytes({"DB/FULL/a.bak": 30 * GIB}, {"DB/FULL/a.bak": 12 * GIB})

    assert (to_write, staged) == (30 * GIB, 0)


def test_a_forced_copy_needs_room_for_the_largest_file_it_rewrites():
    """A forced copy writes each staged file again beside itself and moves it over the old one, one
    at a time: what it needs is the largest of them, not their sum and not nothing."""
    incoming = {"DB/FULL/a.bak": 30 * GIB, "DB/DIFF/b.bak": 4 * GIB, "DB/LOG/c.trn": 1 * GIB}
    staged = {"DB/FULL/a.bak": 30 * GIB, "DB/DIFF/b.bak": 4 * GIB}

    to_write, _staged = restore_space.remaining_bytes(incoming, staged, recopy=True)

    assert to_write == 1 * GIB + 30 * GIB


def _entry(tmp_path: Path, **overrides) -> BackupRestoreConfig:
    values = dict(
        prod_backup_share=tmp_path / "share", vm_import_unc=tmp_path / "import",
        vm_import_local=Path(r"E:\SQLBK_IMPORT"), vm_log_unc=tmp_path / "log",
        vm_log_local=Path(r"E:\LOGS"), prod_smb_credential_target="", prod_smb_username="",
        prod_smb_password_env="", vm_credential_target="", vm_username="", vm_password_env="",
        restore_sql_instance_on_vm="localhost", restore_id="DRILL", copy_recent_hours=192,
        databases=(DatabaseRestoreMapping("PAYROLL_MAIN", "Payroll_Main"),))
    values.update(overrides)
    return BackupRestoreConfig(**values)


def _backup(root: Path, kind: str, hours_ago: float, suffix: str, size: int) -> Path:
    when = time.time() - hours_ago * 3600
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime(when))
    path = root / "PAYROLL_MAIN" / kind / f"PAYROLL_MAIN_{kind}_{stamp}Z.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (when, when))
    return path


def test_the_second_run_of_a_drill_counts_only_what_it_still_has_to_copy(tmp_path):
    """The run that found it: the chain was on the target, 21 GiB were free, and the check asked for
    twice the chain again."""
    config = _entry(tmp_path)
    full = _backup(config.prod_backup_share, "FULL", 48, "bak", 4000)
    _backup(config.prod_backup_share, "LOG", 2, "trn", 300)
    assert space.measure_incoming_bytes(config) == 4300, "a first run counts every file of the chain"

    staged = config.vm_import_unc / full.relative_to(config.prod_backup_share)
    staged.parent.mkdir(parents=True)
    staged.write_bytes(full.read_bytes())
    said: list[str] = []

    assert space.measure_incoming_bytes(config, report=said.append) == 300
    assert said and "already staged" in said[0], "the log says what was left out, and why"


def test_a_linux_target_is_asked_with_the_listing_the_copy_itself_skips_by(tmp_path, monkeypatch):
    """One listing decides both, so the check and the copy cannot disagree about a file."""
    config = _entry(tmp_path, vm_platform="linux", vm_credential_target="192.0.2.252",
                    vm_username="tuser", vm_import_local=Path("/import"))
    full = _backup(config.prod_backup_share, "FULL", 48, "bak", 4000)
    _backup(config.prod_backup_share, "LOG", 2, "trn", 300)
    listed = {full.relative_to(config.prod_backup_share).as_posix().lower(): 4000}
    monkeypatch.setattr(copy_backup, "_remote_destination_sizes", lambda config, *, logger=None: listed)

    assert space.measure_incoming_bytes(config) == 300
    assert space.measure_incoming_bytes(config, recopy=True) == 300 + 4000, \
        "forced, the staged FULL is written again beside itself"


# --------------------------------------------------------------------------- #
# Before the first RESTORE: the database the backup holds
# --------------------------------------------------------------------------- #
def test_the_incident_would_now_be_refused_by_the_database_it_builds():
    """366.6 GiB of files, about 394 GiB free once the chain and the first database were there."""
    verdict = restore_space.judge_restore(int(366.6 * GIB), 0, 394 * GIB, 2.0)

    assert not verdict.ok
    assert "366.6 GiB of database files to create" in verdict.text
    assert "SHORT BY" in verdict.text


def test_the_same_restore_fits_where_the_entry_asks_for_no_margin():
    assert restore_space.judge_restore(int(366.6 * GIB), 0, 394 * GIB, 1.0).ok


def test_a_drill_run_again_over_its_own_last_restore_adds_only_what_the_database_grew_by():
    """Counted whole, every run after the first is refused on a disk the first one filled."""
    verdict = restore_space.judge_restore(367 * GIB, 366 * GIB, 21 * GIB, 2.0)

    assert verdict.ok
    assert verdict.added_bytes == 1 * GIB
    assert "1.0 GiB added" in verdict.text


def test_a_database_that_shrank_needs_nothing():
    assert restore_space.judge_restore(10 * GIB, 40 * GIB, 0, 2.0).ok


PATHS = dict(data_path="/var/opt/mssql/data/Payroll_Main.mdf",
             log_path="/var/opt/mssql/data/Payroll_Main_log.ldf")


def test_the_batch_reads_the_file_list_of_every_backup_it_is_given():
    """A differential lists the files as they were when it was taken, and they only grow."""
    sql = space.restore_room_sql(database="Payroll_Main", backups=["/import/full.bak", "/import/diff.bak"], **PATHS)

    assert sql.count("RESTORE FILELISTONLY FROM DISK") == 2
    assert "/import/full.bak" in sql and "/import/diff.bak" in sql
    assert "sys.dm_os_volume_stats" in sql, "free space as the instance sees its own volume"
    assert "DB_ID(N'Payroll_Main')" in sql


def test_the_batch_changes_nothing_on_the_target():
    sql = space.restore_room_sql(database="Payroll_Main", backups=["/import/full.bak"], **PATHS).upper()

    for verb in ("RESTORE DATABASE", "RESTORE LOG", "ALTER ", "DROP ", "CREATE ", "UPDATE "):
        assert verb not in sql


def test_a_quote_in_a_name_or_a_path_cannot_end_its_literal():
    """The path sits inside the EXEC string, two literals deep - doubled twice, as in the restore."""
    sql = space.restore_room_sql(database="O'Brien", backups=["/import/it's.bak"],
                                 data_path="/data/O'Brien.mdf", log_path="/data/O'Brien_log.ldf")

    assert "DB_ID(N'O''Brien')" in sql
    assert "N''/import/it''''s.bak''" in sql
    assert "N'/data/O''Brien.mdf'" in sql


def test_the_answer_is_found_among_whatever_else_sqlcmd_prints():
    stdout = ("Changed database context to 'master'.\n"
              "                                        \n"
              "----------------------------------------\n"
              f"DBOPS_ROOM|{393 * GIB}|{366 * GIB}|{21 * GIB}|/var/opt/mssql            \n")

    assert space.parse_restore_room(stdout) == {
        "created": 393 * GIB, "overwritten": 366 * GIB, "free": 21 * GIB, "volume": "/var/opt/mssql"}


def test_a_number_the_instance_could_not_read_is_unknown_not_zero():
    """Zero free bytes and "could not read the free bytes" are different answers."""
    measured = space.parse_restore_room("DBOPS_ROOM|1000|0||\n")

    assert measured["created"] == 1000 and measured["free"] is None
    assert space.parse_restore_room("no marker at all")["created"] is None


def _room(config, stdout: str = "", *, raises: Exception | None = None, said: list[str] | None = None):
    def run_sql(sql: str) -> str:
        if raises is not None:
            raise raises
        return stdout

    return space.check_restore_room(
        config, database="Payroll_Main", backups=["/import/full.bak"], run_sql=run_sql,
        log=(said if said is not None else []).append, **PATHS)


def _asking(tmp_path, **rule) -> BackupRestoreConfig:
    """An entry that says ``measure_restore: true`` - the only kind whose restore is measured."""
    return _entry(tmp_path, space_check=restore_space.SpaceCheck(measure_restore=True, **rule))


def test_an_entry_that_does_not_ask_is_not_measured_and_its_target_is_sent_nothing(tmp_path):
    """The default, on every engine: the copy at x2 is the rule, and the engineer who set the restore
    up knows the disk must hold the database (the operator, 2026-10-02)."""
    said: list[str] = []

    result = _room(_entry(tmp_path), raises=AssertionError("the target was asked"), said=said)

    assert result == {"checked": False, "reason": "not_asked"}
    assert said == [], "nothing to say about a question nobody asked"


def test_measuring_is_asked_for_in_the_entry_s_space_check(tmp_path):
    rule = restore_space.parse_space_check({"space_check": {"measure_restore": True}})

    assert rule.measure_restore is True and rule.factor == 2.0
    with pytest.raises(restore_space.RestoreSpaceError, match="measure_restore must be true or false"):
        restore_space.parse_space_check({"space_check": {"measure_restore": "yes"}})


def test_a_restore_that_would_not_fit_is_refused_and_says_what_can_be_changed(tmp_path):
    with pytest.raises(space.RestoreSpaceRefused) as caught:
        _room(_asking(tmp_path), f"DBOPS_ROOM|{366 * GIB}|0|{394 * GIB}|/var/opt/mssql\n")

    message = str(caught.value)
    assert "database=Payroll_Main" in message and "SHORT BY" in message
    assert "/var/opt/mssql" in message, "the volume that is short, as the instance names it"
    assert "space_check.factor" in message
    assert "was not restored" in message


def test_a_restore_that_fits_returns_what_it_measured(tmp_path):
    said: list[str] = []

    result = _room(_asking(tmp_path), f"DBOPS_ROOM|{10 * GIB}|0|{394 * GIB}|D:\\\n", said=said)

    assert result["ok"] is True and result["new_bytes"] == 10 * GIB
    assert result["required_bytes"] == 20 * GIB, "the entry's factor, as for the copy - x2 by default"
    assert any("fits" in line for line in said)


UNMEASURED = [
    ("DBOPS_ROOM|1000|0||\n", None, "the free space of the volume"),
    ("DBOPS_ROOM||0|5000|/\n", None, "the size of the files the backup holds"),
    ("", RuntimeError("sqlcmd command failed with exit code 1."), "the size of the files the backup holds"),
]


@pytest.mark.parametrize("stdout, raises, missing", UNMEASURED)
def test_an_entry_that_asked_is_refused_when_the_measurement_cannot_be_made(
        tmp_path, stdout, raises, missing):
    """It asked by name, so "could not" is not "fits": an old instance has no volume view, a login
    may not read it - and such an entry leaves the field out, or says proceed."""
    with pytest.raises(space.RestoreSpaceRefused) as caught:
        _room(_asking(tmp_path), stdout, raises=raises)

    message = str(caught.value)
    assert missing in message and "measure_restore" in message
    assert "on_unknown" in message, "what the entry can say instead"
    assert "was not restored" in message


@pytest.mark.parametrize("stdout, raises, missing", UNMEASURED)
def test_an_entry_may_accept_an_unmeasured_restore_and_every_run_says_so(
        tmp_path, stdout, raises, missing):
    said: list[str] = []

    result = _room(_asking(tmp_path, on_unknown="proceed"), stdout, raises=raises, said=said)

    assert result["checked"] is False and result["reason"] == "unknown"
    assert any(missing in line and "on_unknown=proceed" in line for line in said)


def test_turning_the_check_off_asks_the_target_nothing(tmp_path):
    config = _entry(tmp_path, space_check=restore_space.SpaceCheck(enabled=False, measure_restore=True))
    said: list[str] = []

    result = _room(config, raises=AssertionError("the target was asked"), said=said)

    assert result == {"checked": False, "reason": "disabled"}
    assert any("disabled" in line for line in said)


# --------------------------------------------------------------------------- #
# Where it is asked: after the plan, before the first RESTORE
# --------------------------------------------------------------------------- #
def _restore(tmp_path: Path, monkeypatch, room_answer: str, *, asks: bool = True) -> tuple[list, object]:
    import db_ops.backup_restore.restore_database as restore_module

    config = _entry(tmp_path, databases=(), source_database_name="Payroll_Main",
                    restore_database_name="Payroll_Main_DR",
                    space_check=restore_space.SpaceCheck(measure_restore=asks))
    backup = config.vm_import_unc / "Payroll_Main" / "FULL" / "latest.bak"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    app_config = DbOpsConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime",
                             sqlite_path=tmp_path / "runtime" / "db_ops.sqlite")
    calls: list = []

    def fake_run_sqlcmd(cmd, **_kwargs):
        calls.append(cmd)
        measuring = space.ROOM_MARKER in str(getattr(cmd, "sql", ""))
        return subprocess.CompletedProcess(cmd, 0, room_answer if measuring else "complete", "")

    monkeypatch.setattr(restore_module, "run_sqlcmd_query_command", fake_run_sqlcmd)

    def run():
        return restore_module.run_restore_database(
            config=config, db_ops_config=app_config, backup_file=backup,
            ensure_certificate=False, ensure_credential=False)

    return calls, run


def test_the_room_is_asked_before_the_first_restore(tmp_path, monkeypatch):
    calls, run = _restore(tmp_path, monkeypatch, f"DBOPS_ROOM|{10 * GIB}|0|{394 * GIB}|D:\\\n")

    result = run()

    assert result["status"] == "SUCCESS" and result["restore_room"]["ok"] is True
    assert space.ROOM_MARKER in calls[0].sql
    assert calls[1].restore_step[0] == "full", "the full restore is the next thing sent"


def test_an_entry_that_does_not_ask_goes_straight_to_its_restore(tmp_path, monkeypatch):
    calls, run = _restore(tmp_path, monkeypatch, "", asks=False)

    result = run()

    assert result["status"] == "SUCCESS" and result["restore_room"] == {"checked": False, "reason": "not_asked"}
    assert calls[0].restore_step[0] == "full", "the first thing the target is sent is the restore"
    assert not any(space.ROOM_MARKER in str(getattr(cmd, "sql", "")) for cmd in calls)


def test_a_refused_database_gets_no_restore_statement_at_all(tmp_path, monkeypatch):
    calls, run = _restore(tmp_path, monkeypatch, f"DBOPS_ROOM|{366 * GIB}|0|{94 * GIB}|D:\\\n")

    with pytest.raises(space.RestoreSpaceRefused):
        run()

    assert len(calls) == 1, "only the measurement reached the target"
