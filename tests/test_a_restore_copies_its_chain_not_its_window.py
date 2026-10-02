"""A SQL Server restore copies the chain it will apply, not every backup of its copy window.

On 2026-10-01 the 100.250 drill (`ORDERS_MAIN`, `PAYROLL_MAIN`, `VRS_PROD`) was refused by its own space
check: **527.9 GiB to copy**. The restore would have applied one FULL, one DIFF and 28 LOGs -
**56.8 GiB**. The copy took every file written in `copy_recent_hours`, and a weekly FULL makes that
eight days of daily DIFFs and LOGs; the restore picked its chain only afterwards, among what had
already crossed (0.26.0 §1.74).

The chain is the restore's own rule, applied before the copy: the newest FULL at or before the
moment, the newest DIFF after it and at or before the moment, the LOGs after that - for a point in
time, up to and including the first LOG that reaches it. A database the chain cannot be settled for
is still copied by its window, so a layout this rule does not know is no worse off than before.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import time
from pathlib import Path

from db_ops.backup_restore import copy_backup
from db_ops.backup_restore.config import BackupRestoreConfig, DatabaseRestoreMapping
from db_ops.lib.sqlserver_backup_chain import Candidate, restore_chain

DAY = 86400.0


def _c(name: str, kind: str, ts: float, db: str = "PAYROLL_MAIN") -> Candidate:
    return Candidate(item=name, database=db, kind=kind, timestamp=ts)


# Eight days of HRMS-shaped backups: a weekly FULL on day 0 and day 7, a DIFF at 20:00 every day,
# a LOG every hour.
WEEK = ([_c(f"FULL_d{d}", "FULL", d * DAY) for d in (0, 7)]
        + [_c(f"DIFF_d{d}", "DIFF", d * DAY + 20 * 3600) for d in range(8)]
        + [_c(f"LOG_d{d}_h{h:02d}", "LOG", d * DAY + h * 3600) for d in range(8) for h in range(24)])


def test_latest_takes_the_newest_full_its_newest_diff_and_the_logs_after_it():
    chosen = restore_chain(WEEK).selected

    assert chosen[0] == "FULL_d7"
    assert chosen[1] == "DIFF_d7"
    assert list(chosen[2:]) == ["LOG_d7_h20", "LOG_d7_h21", "LOG_d7_h22", "LOG_d7_h23"]


def test_a_point_in_time_before_the_newest_full_takes_the_full_before_it():
    """The moment decides the FULL, not "the newest": day 5 needs day 0's FULL."""
    moment = 5 * DAY + 22 * 3600 + 1800
    chosen = restore_chain(WEEK, until=moment).selected

    assert chosen[0] == "FULL_d0"
    assert chosen[1] == "DIFF_d5"
    # From the DIFF's hour to the first LOG at or after the moment, which is the one holding it.
    assert list(chosen[2:]) == ["LOG_d5_h20", "LOG_d5_h21", "LOG_d5_h22", "LOG_d5_h23"]


def test_the_log_that_carries_the_moment_is_taken_and_nothing_after_it():
    moment = 6 * DAY + 21 * 3600 + 60
    chosen = restore_chain(WEEK, until=moment).selected

    assert chosen[-1] == "LOG_d6_h22"
    assert "LOG_d6_h23" not in chosen


def test_a_database_with_no_full_before_the_moment_is_left_to_the_window():
    choice = restore_chain([_c("LOG_x", "LOG", 10.0)], until=100.0)

    assert choice.selected == ()
    assert choice.unresolved == ("PAYROLL_MAIN",)


def test_a_chain_without_a_diff_starts_its_logs_at_the_full():
    files = [_c("FULL", "FULL", 0.0), _c("LOG_before", "LOG", -60.0), _c("LOG_after", "LOG", 60.0)]

    assert restore_chain(files).selected == ("FULL", "LOG_after")


def test_each_database_gets_its_own_chain():
    files = WEEK + [_c("APP_FULL", "FULL", 3 * DAY, db="ORDERS_MAIN"),
                    _c("APP_LOG", "LOG", 3 * DAY + 60, db="ORDERS_MAIN")]
    chosen = restore_chain(files).selected

    assert "APP_FULL" in chosen and "APP_LOG" in chosen and "FULL_d7" in chosen


# --------------------------------------------------------------------------- #
# The copy, and the space check that counts the same list
# --------------------------------------------------------------------------- #
def _config(share: Path, tmp_path: Path, **overrides) -> BackupRestoreConfig:
    config = BackupRestoreConfig(
        prod_backup_share=share, vm_import_unc=tmp_path / "imp", vm_import_local=Path("/imp"),
        vm_log_unc=tmp_path / "log", vm_log_local=Path("/log"), prod_smb_credential_target="",
        prod_smb_username="", prod_smb_password_env="", vm_credential_target="192.0.2.252",
        vm_username="tuser", vm_password_env="", restore_sql_instance_on_vm="localhost,1433",
        vm_platform="linux", restore_id="R1", copy_recent_hours=192,
        databases=(DatabaseRestoreMapping("PAYROLL_MAIN", "Payroll_Main"),))
    return dataclasses.replace(config, **overrides)


def _write(share: Path, kind: str, when: dt.datetime, suffix: str) -> Path:
    stamp = when.strftime("%Y%m%d_%H%M%S")
    path = share / "PAYROLL_MAIN" / kind / f"PAYROLL_MAIN_{kind}_{stamp}Z.{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


def _eight_days(share: Path) -> dict[str, Path]:
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - dt.timedelta(days=7, hours=12)
    made = {"old_full": _write(share, "FULL", start, "bak"),
            "full": _write(share, "FULL", now - dt.timedelta(days=4), "bak")}
    for day in range(8):
        made[f"diff{day}"] = _write(share, "DIFF", start + dt.timedelta(days=day, hours=2), "bak")
    for hour in range(0, 8 * 24, 6):
        made[f"log{hour}"] = _write(share, "LOG", start + dt.timedelta(hours=hour, minutes=30), "trn")
    return made


def test_the_copy_takes_the_chain_and_leaves_the_rest_of_the_window(tmp_path):
    share = tmp_path / "share"
    made = _eight_days(share)

    selected = copy_backup.list_recent_backup_files(_config(share, tmp_path))
    names = [p.name for p in selected]

    assert made["full"].name in names and made["old_full"].name not in names
    diffs = [n for n in names if "_DIFF_" in n]
    assert len(diffs) == 1, diffs
    every_file = sum(1 for p in share.rglob("*") if p.is_file())
    assert len(selected) < every_file / 3


def test_the_copy_reports_the_chain_it_chose(tmp_path):
    share = tmp_path / "share"
    _eight_days(share)
    said: list[str] = []

    copy_backup.list_recent_backup_files(_config(share, tmp_path), report=said.append)

    assert said and said[0].startswith("PAYROLL_MAIN: FULL 1 + DIFF 1 + LOG ")


def test_a_point_in_time_copy_keeps_the_log_written_after_the_moment(tmp_path):
    """The window used to end AT the moment, which drops the LOG that carries it."""
    share = tmp_path / "share"
    moment = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=10)
    _write(share, "FULL", moment - dt.timedelta(days=1), "bak")
    carrier = _write(share, "LOG", moment + dt.timedelta(minutes=15), "trn")
    _write(share, "LOG", moment + dt.timedelta(hours=1), "trn")

    names = [p.name for p in copy_backup.list_recent_backup_files(
        _config(share, tmp_path, copy_window_end_utc=moment))]

    assert carrier.name in names
    assert len([n for n in names if n.endswith(".trn")]) == 1


def test_a_layout_with_no_full_folder_is_still_copied_by_its_window(tmp_path):
    share = tmp_path / "share"
    loose = share / "PAYROLL_MAIN" / "PAYROLL_MAIN_20260930_010000Z.bak"
    loose.parent.mkdir(parents=True)
    loose.write_bytes(b"x")
    os.utime(loose, (time.time() - 3600, time.time() - 3600))

    assert copy_backup.list_recent_backup_files(_config(share, tmp_path)) == [loose]


def _nothing_staged(monkeypatch) -> None:
    """The target holds none of the files yet - what is staged is not counted (0.26.0 §1.76), and
    asking a Linux target is an SSH session these tests do not have."""
    monkeypatch.setattr(copy_backup, "_remote_destination_sizes", lambda config, *, logger=None: {})


def test_the_space_check_counts_the_chain(tmp_path, monkeypatch):
    from db_ops.backup_restore import space

    share = tmp_path / "share"
    _eight_days(share)
    config = _config(share, tmp_path)
    _nothing_staged(monkeypatch)

    assert space.measure_incoming_bytes(config) == len(copy_backup.list_recent_backup_files(config))


# --------------------------------------------------------------------------- #
# copy_selection: the entry chooses the chain or the whole window
# --------------------------------------------------------------------------- #
def test_the_default_is_the_chain(tmp_path):
    assert _config(tmp_path / "share", tmp_path).copy_selection == "chain"


def test_window_copies_every_file_of_the_range_and_the_space_check_counts_it(tmp_path, monkeypatch):
    """The operator (2026-10-01): one field on the restore entry says whether only the files the
    restore needs are copied, or every file in the range."""
    from db_ops.backup_restore import space

    share = tmp_path / "share"
    _eight_days(share)
    _nothing_staged(monkeypatch)
    window = _config(share, tmp_path, copy_selection="window")
    chain = _config(share, tmp_path)

    every_file = sum(1 for p in share.rglob("*") if p.is_file())
    assert len(copy_backup.list_recent_backup_files(window)) == every_file
    assert len(copy_backup.list_recent_backup_files(chain)) < every_file
    assert space.measure_incoming_bytes(window) == every_file


def test_the_window_selection_says_so(tmp_path):
    share = tmp_path / "share"
    _eight_days(share)
    said: list[str] = []

    copy_backup.list_recent_backup_files(_config(share, tmp_path, copy_selection="window"),
                                         report=said.append)

    assert said and said[0].startswith("copy_selection=window")


def test_the_entry_s_choice_is_read_from_restore_config_and_a_misspelling_is_refused(tmp_path):
    import json

    import pytest

    from db_ops.backup_restore.config import load_restore_configs

    def write(entry_value):
        entry = {"restore_id": "R1", "server_id": "SRC", "target_server_id": "TGT", "active": True,
                 "cleanup_retention": 691200,
                 "source": {"backup_share": r"\\192.0.2.250\SQLBK", "username": "u", "password_ref": "P"},
                 "target": {"vm_platform": "linux", "credential_target": "192.0.2.252", "username": "tuser",
                            "password_ref": "T", "sql_instance": "localhost,1433",
                            "vm_import_linux_path": "/opt/imp", "vm_import_linux_log_path": "/opt/imp"},
                 "database_mappings": [{"source_database": "A", "target_database": "A"}]}
        if entry_value is not None:
            entry["copy_selection"] = entry_value
        path = tmp_path / "restore_config.json"
        path.write_text(json.dumps({"backup_restore": {"copy_selection": "chain", "restores": [entry]}}),
                        encoding="utf-8")
        return path

    assert load_restore_configs(write(None))[0].copy_selection == "chain"
    assert load_restore_configs(write("WINDOW"))[0].copy_selection == "window"
    with pytest.raises(ValueError, match="copy_selection"):
        load_restore_configs(write("windows"))
