"""A backup cleanup deletes only what its entry put there, and keeps exactly the window it was given.

Two retention gaps (review 0.25.0, B4.2 and B4.4):

* **The restore target's cleanup recursed its whole import root.** Every `*.bak` / `*.trn` under it
  past the retention went - so a root set one level too high, onto the target host's own backup or
  data directory, had the host's own backups deleted. The guard only refused a root that was the
  source or one component deep, so `/data` passed. Now a cleanup touches only
  `<root>/<a mapped database>/`, where the copy writes; an entry that maps none needs a root three
  levels below `/`.
* **A retention under a day became 14 days.** The planner reasoned in whole days, so 7200 s was
  0 days and the caller replaced 0 with the 14-day default - a two-hour lab retention kept two
  weeks of backups, and the lab disk filled. The planner takes the seconds now.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from db_ops.backup_restore import delete_backup
from db_ops.backup_restore.config import BackupRestoreConfig, DatabaseRestoreMapping
from db_ops.lib.backupfiles_retention import plan_retention


def _config(root: str, **overrides) -> BackupRestoreConfig:
    config = BackupRestoreConfig(
        prod_backup_share=Path(r"\192.0.2.250\SQLBK"), vm_import_unc=Path(root), vm_import_local=Path(root),
        vm_log_unc=Path("/log"), vm_log_local=Path("/log"), prod_smb_credential_target="",
        prod_smb_username="", prod_smb_password_env="", vm_credential_target="192.0.2.252",
        vm_username="tuser", vm_password_env="", restore_sql_instance_on_vm="localhost,1433",
        vm_platform="linux", restore_id="R1",
        databases=(DatabaseRestoreMapping("PAYROLL_MAIN", "Payroll_Main"),))
    return dataclasses.replace(config, **overrides)


def test_only_the_mapped_database_folders_can_be_cleaned():
    config = _config("/var/opt/mssql")

    assert delete_backup._staged_by_this_entry(config, "/var/opt/mssql/PAYROLL_MAIN/FULL/a.bak")
    assert not delete_backup._staged_by_this_entry(config, "/var/opt/mssql/backup/own_nightly.bak")
    assert not delete_backup._staged_by_this_entry(config, "/var/opt/mssql/PAYROLL_MAINX/FULL/a.bak")


def test_an_entry_with_no_database_needs_a_deep_root():
    with pytest.raises(ValueError, match="too broad"):
        delete_backup._validate_safe_target_delete_root(_config("/data", databases=()))
    delete_backup._validate_safe_target_delete_root(_config("/opt/db_ops/staging", databases=()))


def test_the_local_engine_leaves_the_host_s_own_backups_alone(tmp_path):
    root = tmp_path / "import"
    mine = root / "PAYROLL_MAIN" / "LOG" / "PAYROLL_MAIN_LOG_20200101_000000Z.trn"
    theirs = root / "nightly" / "OWN_DB_LOG_20200101_000000Z.trn"
    for path in (mine, theirs):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
    config = _config(str(root), vm_platform="windows", cleanup_retention=60)

    aged = [p for p in delete_backup.list_old_target_backup_files(config)
            if delete_backup._staged_by_this_entry(config, p)]

    assert mine in aged and theirs not in aged


def _file(kind: str, hours_ago: float) -> dict:
    finished = datetime.now() - timedelta(hours=hours_ago)
    return {"path": f"/b/{kind.upper()}/{kind}_{int(hours_ago * 60)}.bak", "kind": kind,
            "database_name": "db", "size_bytes": 1,
            "finished_at": finished.strftime("%Y-%m-%d %H:%M:%S")}


# An old chain (30 h) and a new one (1 h): under two hours the old chain is obsolete.
FILES = [_file("full", 30), _file("log", 29), _file("full", 1), _file("log", 0.5)]


def test_a_two_hour_retention_is_two_hours_not_fourteen_days():
    two_hours = plan_retention(FILES, retention_seconds=7200)
    fourteen_days = plan_retention(FILES, retention_days=14)

    assert two_hours["window"] == "7200-second"
    assert {row["path"] for row in two_hours["obsolete"]} == {FILES[0]["path"], FILES[1]["path"]}
    assert fourteen_days["obsolete"] == []


def test_whole_days_read_as_before():
    plan = plan_retention(FILES, retention_seconds=8 * 86400)

    assert plan["window"] == "8-day" and plan["retention_days"] == 8
