import os
import dataclasses
import datetime
import subprocess
from pathlib import Path

import pytest

from db_ops.lib.shell import is_powershell_executable
from db_ops.backup_restore.config import (
    BackupRestoreConfig,
    DatabaseRestoreMapping,
    load_restore_configs,
    parse_restore_config,
    validate_restore_target_is_not_source,
)
from db_ops.backup_restore.cli import _build_end_event, _build_restore_mapping, _summarize_command_output, parse_args, run_restore_workflow
from db_ops.backup_restore.copy_backup import (
    CopyBackupFileResult,
    CopyBackupResult,
    build_cmdkey_command,
    build_copy_cmdkey_commands,
    build_robocopy_command,
    copy_backup_file,
    copy_backup_file_with_logging,
    list_recent_backup_files,
    run_copy_backup,
    _pending_remote_backups,
    _parse_smbclient_ls,
    _selected_remote_backups,
    _smbclient_download_selected_to_staging,
    _smbclient_remote_backups,
)
from db_ops.backup_restore.delete_backup import (
    DeleteBackupResult,
    delete_old_target_backup_files_via_ssh,
    list_old_target_backup_files,
    run_delete_backup,
)
from db_ops.backup_restore.events import emit_backup_restore_event, _format_restore_workflow_telegram_message, _format_telegram_message
from db_ops.backup_restore.certificate import (
    BackupCertificate,
    build_add_certificate_command,
    build_add_certificate_sql,
    ensure_source_certificate,
    parse_backup_certificate,
    _run_add_certificate_command,
)
from db_ops.backup_restore.restore_database import (
    build_restore_candidate,
    build_restore_full_sql,
    build_restore_log_stopat_sql,
    build_recovery_sql,
    build_recovery_if_restoring_sql,
    build_set_recovery_model_full_sql,
    build_restore_sql,
    build_sqlcmd_query_command,
    ensure_source_certificate_with_events,
    find_latest_full_backups,
    find_restore_diff_backup,
    find_restore_diff_backup_for_pitr,
    find_restore_log_backups,
    find_restore_log_backups_for_pitr,
    get_full_backup_for_pitr,
    get_latest_full_backup,
    get_latest_full_backup_for_database,
    parse_point_in_time,
    run_restore_database,
    run_restore_diff,
    run_restore_full,
    run_restore_log,
    run_restore_server,
    run_sqlcmd_query_command,
    summarize_restore_stdout,
    vm_unc_to_local_path,
)
from db_ops.backup_restore.sanitize import sanitize_text, sanitize_value
from db_ops.config import DbOpsConfig, TelegramConfig
from db_ops.db import DbOpsStore
from db_ops.backup_restore.verify_restore import build_checkdb_sql


#: The PowerShell copy engine is chosen by `should_use_powershell_unc_copy`, which begins
#: `os.name == "nt"`: a Windows orchestrator copying between two SMB shares. On Linux the engine is
#: `smbclient` or `sftp` instead, so these two tests describe a path that does not exist there —
#: they are platform-specific by subject, not by accident, and skipping is the honest report.
windows_orchestrator_only = pytest.mark.skipif(
    os.name != "nt", reason="the PowerShell copy engine only exists on a Windows orchestrator")


def make_config(tmp_path: Path) -> BackupRestoreConfig:
    return BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"E:\SQLBK_IMPORT"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"E:\LOGS"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        source_database_name="APPDB_Prod",
        restore_database_name="APPDB_Prod_DR",
        restore_data_file_on_vm=Path(r"D:\MSSQL\DATA\APPDB_Prod_DR.mdf"),
        restore_log_file_on_vm=Path(r"D:\MSSQL\DATA\APPDB_Prod_DR_log.ldf"),
    )


_SAMPLE_RECURSE_LS = (
    "  .                          D        0  Wed Jun 24 01:00:00 2026\n"
    "  APPDB_Prod                  D        0  Wed Jun 24 01:00:00 2026\n"
    "  APPDB_STG                   D        0  Wed Jun 24 01:00:00 2026\n"
    "\n"
    "\\APPDB-DB$APPDB\\APPDB_Prod\\FULL\n"
    "  APPDB-DB$APPDB_APPDB_Prod_FULL_20260624_010005.bak      A  11121102848  Wed Jun 24 01:01:11 2026\n"
    "\\APPDB-DB$APPDB\\APPDB_STG\\FULL\n"
    "  APPDB-DB$APPDB_APPDB_STG_FULL_20260624_010147.bak       A     70000000  Wed Jun 24 01:01:50 2026\n"
    "\\APPDB-DB$APPDB\\APPDB_STG\\LOG\n"
    "  APPDB-DB$APPDB_APPDB_STG_LOG_20260624_080001.trn        A      1048576  Wed Jun 24 08:00:02 2026\n"
    "\n\t\t536866303 blocks of size 4096. 431419863 blocks available\n"
)


def test_smbclient_remote_backups_parses_recurse_relative_paths(monkeypatch):
    monkeypatch.setattr(
        "db_ops.backup_restore.copy_backup._run_smbclient_command",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=_SAMPLE_RECURSE_LS, stderr=""),
    )
    backups = _smbclient_remote_backups("h", "s", Path("auth"), "APPDB-DB$APPDB")
    assert backups["APPDB_Prod\\FULL\\APPDB-DB$APPDB_APPDB_Prod_FULL_20260624_010005.bak"] == 11121102848
    assert backups["APPDB_STG\\FULL\\APPDB-DB$APPDB_APPDB_STG_FULL_20260624_010147.bak"] == 70000000
    assert backups["APPDB_STG\\LOG\\APPDB-DB$APPDB_APPDB_STG_LOG_20260624_080001.trn"] == 1048576
    # directory entries are excluded
    assert all(rel.lower().endswith((".bak", ".trn")) for rel in backups)


def test_selected_remote_backups_filters_by_window_and_pattern():
    rows = _parse_smbclient_ls(
        (
            "\\APPDB-DB$APPDB\\APPDB_Prod\\FULL\n"
            "  APPDB_Prod_FULL_20260608_010000.bak      A  10  Mon Jun 08 01:00:00 2026\n"
            "  APPDB_Prod_FULL_20260609_010000.bak      A  10  Tue Jun 09 01:00:00 2026\n"
            "\\APPDB-DB$APPDB\\APPDB_Prod\\LOG\n"
            "  APPDB_Prod_LOG_20260608_043000.trn       A  20  Mon Jun 08 04:30:00 2026\n"
            "  note.txt                                A  30  Mon Jun 08 04:30:00 2026\n"
        ),
        remote_dir="APPDB-DB$APPDB",
    )
    start = datetime.datetime(2026, 6, 7, 5, 0, tzinfo=datetime.timezone.utc).timestamp()
    end = datetime.datetime(2026, 6, 8, 5, 0, tzinfo=datetime.timezone.utc).timestamp()

    selected, skipped = _selected_remote_backups(rows, cutoff=start, end_ts=end, patterns=("*.bak", "*.trn"))

    assert [item.relative_path for item in selected] == [
        "APPDB_Prod\\FULL\\APPDB_Prod_FULL_20260608_010000.bak",
        "APPDB_Prod\\LOG\\APPDB_Prod_LOG_20260608_043000.trn",
    ]
    assert {reason for _, reason in skipped} == {"after_copy_window"}


def test_smbclient_selected_staging_lists_first_and_downloads_only_selected(tmp_path, monkeypatch):
    import db_ops.backup_restore.copy_backup as copy_module

    config = dataclasses.replace(
        make_config(tmp_path),
        prod_backup_share=Path(r"\\192.0.2.250\SQLBK\APPDB-DB$APPDB"),
        copy_window_start_utc=datetime.datetime(2026, 6, 7, 5, 0, tzinfo=datetime.timezone.utc),
        copy_window_end_utc=datetime.datetime(2026, 6, 8, 5, 0, tzinfo=datetime.timezone.utc),
    )
    commands = []
    downloads = []
    listing = (
        "\\APPDB-DB$APPDB\\APPDB_Prod\\FULL\n"
        "  APPDB_Prod_FULL_20260608_010000.bak      A  10  Mon Jun 08 01:00:00 2026\n"
        "  APPDB_Prod_FULL_20260609_010000.bak      A  10  Tue Jun 09 01:00:00 2026\n"
        "\\APPDB-DB$APPDB\\APPDB_Prod\\LOG\n"
        "  APPDB_Prod_LOG_20260608_043000.trn       A  20  Mon Jun 08 04:30:00 2026\n"
    )

    def fake_run(args, *, timeout_seconds):
        commands.append(args[-1])
        return subprocess.CompletedProcess(args, 0, listing, "")

    def fake_get(_host, _share, _authfile, remote_path, local_parent, local_name):
        downloads.append(remote_path)
        local_parent.mkdir(parents=True, exist_ok=True)
        size = 20 if remote_path.endswith(".trn") else 10
        (local_parent / local_name).write_bytes(b"x" * size)

    monkeypatch.setattr(copy_module, "_run_smbclient_command", fake_run)
    monkeypatch.setattr(copy_module, "_smbclient_get_file", fake_get)
    monkeypatch.setattr(copy_module, "_remote_destination_sizes", lambda config, *, logger=None: {})

    staging, pre_skipped, total_selected = _smbclient_download_selected_to_staging(config, logger=None)
    assert pre_skipped == []
    assert total_selected == 2

    assert all("mget *" not in command for command in commands)
    assert commands and commands[0].endswith('cd "APPDB-DB$APPDB"; ls')
    assert downloads == [
        "APPDB-DB$APPDB\\APPDB_Prod\\FULL\\APPDB_Prod_FULL_20260608_010000.bak",
        "APPDB-DB$APPDB\\APPDB_Prod\\LOG\\APPDB_Prod_LOG_20260608_043000.trn",
    ]
    assert (staging / "APPDB_Prod" / "FULL" / "APPDB_Prod_FULL_20260608_010000.bak").exists()


def test_smbclient_selected_staging_skips_files_already_in_destination(tmp_path, monkeypatch):
    import db_ops.backup_restore.copy_backup as copy_module

    config = dataclasses.replace(
        make_config(tmp_path),
        prod_backup_share=Path(r"\\192.0.2.250\SQLBK\APPDB-DB$APPDB"),
        vm_import_unc=Path("/opt/import/ACME"),
        copy_window_start_utc=datetime.datetime(2026, 6, 7, 5, 0, tzinfo=datetime.timezone.utc),
        copy_window_end_utc=datetime.datetime(2026, 6, 10, 5, 0, tzinfo=datetime.timezone.utc),
    )
    listing = (
        "\\APPDB-DB$APPDB\\APPDB_Prod\\FULL\n"
        "  APPDB_Prod_FULL_20260608_010000.bak      A  10  Mon Jun 08 01:00:00 2026\n"
        "  APPDB_Prod_FULL_20260609_010000.bak      A  10  Tue Jun 09 01:00:00 2026\n"
    )
    downloads = []

    def fake_get(_host, _share, _authfile, remote_path, local_parent, local_name):
        downloads.append(remote_path)
        local_parent.mkdir(parents=True, exist_ok=True)
        (local_parent / local_name).write_bytes(b"x" * 10)

    monkeypatch.setattr(
        copy_module,
        "_run_smbclient_command",
        lambda args, *, timeout_seconds: subprocess.CompletedProcess(args, 0, listing, ""),
    )
    monkeypatch.setattr(copy_module, "_smbclient_get_file", fake_get)
    # The 0608 backup already exists in the final destination at the same size; the 0609 does not.
    monkeypatch.setattr(
        copy_module,
        "_remote_destination_sizes",
        lambda config, *, logger=None: {"appdb_prod/full/appdb_prod_full_20260608_010000.bak": 10},
    )

    staging, pre_skipped, total_selected = _smbclient_download_selected_to_staging(config, logger=None)

    assert total_selected == 2
    assert len(pre_skipped) == 1
    assert pre_skipped[0].status == "SKIPPED_EXISTS"
    # Only the missing backup was downloaded from SMB.
    assert downloads == ["APPDB-DB$APPDB\\APPDB_Prod\\FULL\\APPDB_Prod_FULL_20260609_010000.bak"]


def test_smbclient_selected_staging_fails_fast_when_no_files_selected(tmp_path, monkeypatch):
    import db_ops.backup_restore.copy_backup as copy_module

    config = dataclasses.replace(
        make_config(tmp_path),
        prod_backup_share=Path(r"\\192.0.2.250\SQLBK\APPDB-DB$APPDB"),
        copy_window_start_utc=datetime.datetime(2026, 6, 8, 0, 0, tzinfo=datetime.timezone.utc),
        copy_window_end_utc=datetime.datetime(2026, 6, 8, 5, 0, tzinfo=datetime.timezone.utc),
    )
    listing = (
        "\\APPDB-DB$APPDB\\APPDB_Prod\\FULL\n"
        "  APPDB_Prod_FULL_20260609_010000.bak      A  10  Tue Jun 09 01:00:00 2026\n"
    )
    monkeypatch.setattr(
        copy_module,
        "_run_smbclient_command",
        lambda args, *, timeout_seconds: subprocess.CompletedProcess(args, 0, listing, ""),
    )
    monkeypatch.setattr(copy_module, "_remote_destination_sizes", lambda config, *, logger=None: {})

    with pytest.raises(RuntimeError, match="selected no files"):
        _smbclient_download_selected_to_staging(config, logger=None)


def test_pending_remote_backups_flags_missing_and_truncated(tmp_path):
    # The APPDB_Prod copy truncated and aborted, so APPDB_STG was never downloaded.
    remote = {
        "APPDB_Prod\\FULL\\db_FULL_20260624_010005.bak": 11_000_000_000,  # truncated in staging
        "APPDB_STG\\FULL\\db_FULL_20260624_010147.bak": 70_000_000,        # missing entirely
        "SALESDB_Prod\\FULL\\db_FULL_20260624_010000.bak": 500,              # already complete
        "OLD\\FULL\\db_FULL_20260101_010000.bak": 999,                   # older than cutoff
    }
    p1 = tmp_path / "APPDB_Prod" / "FULL"
    p1.mkdir(parents=True)
    (p1 / "db_FULL_20260624_010005.bak").write_bytes(b"x" * 100)
    p2 = tmp_path / "SALESDB_Prod" / "FULL"
    p2.mkdir(parents=True)
    (p2 / "db_FULL_20260624_010000.bak").write_bytes(b"x" * 500)

    cutoff = datetime.datetime(2026, 6, 23).timestamp()
    pending = _pending_remote_backups(tmp_path, remote, cutoff)
    by_rel = {rel: staged for rel, _, staged in pending}
    assert set(by_rel) == {
        "APPDB_Prod\\FULL\\db_FULL_20260624_010005.bak",
        "APPDB_STG\\FULL\\db_FULL_20260624_010147.bak",
    }
    assert by_rel["APPDB_STG\\FULL\\db_FULL_20260624_010147.bak"] is None  # missing
    assert by_rel["APPDB_Prod\\FULL\\db_FULL_20260624_010005.bak"] == 100  # truncated


def test_pending_remote_backups_empty_when_all_complete(tmp_path):
    remote = {"SALESDB_Prod\\FULL\\db_FULL_20260624_010000.bak": 500}
    p = tmp_path / "SALESDB_Prod" / "FULL"
    p.mkdir(parents=True)
    (p / "db_FULL_20260624_010000.bak").write_bytes(b"x" * 500)
    assert _pending_remote_backups(tmp_path, remote, datetime.datetime(2026, 6, 23).timestamp()) == []


def test_get_latest_full_backup_selects_newest_bak(tmp_path):
    config = make_config(tmp_path)
    full_dir = config.full_backup_dir
    full_dir.mkdir(parents=True)
    older = full_dir / "older.bak"
    newer = full_dir / "newer.bak"
    older.write_text("older", encoding="utf-8")
    newer.write_text("newer", encoding="utf-8")

    import os
    import time

    now = time.time()
    os.utime(older, (now - 120, now - 120))
    os.utime(newer, (now - 60, now - 60))

    assert get_latest_full_backup(config) == newer


def test_get_latest_full_backup_raises_when_empty(tmp_path):
    config = make_config(tmp_path)
    config.full_backup_dir.mkdir(parents=True)

    with pytest.raises(FileNotFoundError):
        get_latest_full_backup(config)


def test_vm_unc_to_local_path_preserves_relative_folder(tmp_path):
    config = make_config(tmp_path)
    backup_unc = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"

    assert vm_unc_to_local_path(backup_unc, config) == Path(r"E:\SQLBK_IMPORT\APPDB_Prod\FULL\latest.bak")


def test_build_restore_sql_uses_vm_local_paths_and_escapes_literals(tmp_path):
    config = BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"E:\SQLBK_IMPORT"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"E:\LOGS"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        source_database_name="APPDB_Prod",
        restore_database_name="DR]Name",
        restore_data_file_on_vm=Path(r"D:\MSSQL\DATA\DR's.mdf"),
        restore_log_file_on_vm=Path(r"D:\MSSQL\DATA\DR's_log.ldf"),
    )

    sql = build_restore_sql(r"E:\SQLBK_IMPORT\full's.bak", config)

    assert "RESTORE DATABASE [DR]]Name]" in sql
    assert "FROM DISK = N''E:\\SQLBK_IMPORT\\full''s.bak''" in sql
    assert "TO N''D:\\MSSQL\\DATA\\DR''s.mdf''" in sql


def test_build_restore_full_sql_uses_norecovery_and_final_steps_are_separate(tmp_path):
    config = make_config(tmp_path)
    backup = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    candidate = build_restore_candidate(backup, config)

    full_sql = build_restore_full_sql(candidate.backup_file_on_vm, config, candidate=candidate)

    assert "NORECOVERY" in full_sql
    assert "WITH RECOVERY" not in full_sql
    assert "SET MULTI_USER" not in full_sql
    assert build_recovery_sql(candidate).splitlines()[1] == "RESTORE DATABASE [APPDB_Prod_DR] WITH RECOVERY;"
    assert build_set_recovery_model_full_sql(candidate) == "ALTER DATABASE [APPDB_Prod_DR] SET RECOVERY FULL;"


def test_find_latest_full_backups_selects_newest_per_database_folder(tmp_path):
    config = BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"C:\MSSQL\SQLBK_IMPORT\ACME-192-0-2-250"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"C:\MSSQL\SQLBK_IMPORT\ACME-192-0-2-250"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
    )
    app_full = config.vm_import_unc / "APPDB-DB$APPDB" / "SALESDB_Prod" / "FULL"
    appdb_full = config.vm_import_unc / "APPDB-DB$APPDB" / "APPDB_Prod" / "FULL"
    app_full.mkdir(parents=True)
    appdb_full.mkdir(parents=True)
    app_old = app_full / "app_old.bak"
    app_new = app_full / "app_new.bak"
    appdb_new = appdb_full / "appdb_new.bak"
    for path in [app_old, app_new, appdb_new]:
        path.write_text(path.name, encoding="utf-8")

    import os

    os.utime(app_old, (1_700_000_000, 1_700_000_000))
    os.utime(app_new, (1_700_000_100, 1_700_000_100))
    os.utime(appdb_new, (1_700_000_200, 1_700_000_200))

    # Compared as a set: the property is *newest per database folder*, and the order of the list
    # follows the folder name, which is not what this test is about. It was a list comparison until
    # a scrub renamed one folder and flipped the alphabetical order — the check failed while the
    # behaviour it names was unchanged, which is a test asserting on the wrong thing.
    assert set(find_latest_full_backups(config, now=1_700_000_300)) == {app_new, appdb_new}


def test_build_restore_candidate_derives_database_and_paths_from_backup_folder(tmp_path):
    config = BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"C:\MSSQL\SQLBK_IMPORT\ACME-192-0-2-250"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"C:\MSSQL\SQLBK_IMPORT\ACME-192-0-2-250"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
    )
    backup = config.vm_import_unc / "APPDB-DB$APPDB" / "SALESDB_Prod" / "FULL" / "latest.bak"

    candidate = build_restore_candidate(backup, config)

    assert candidate.source_key == r"APPDB-DB$APPDB\SALESDB_Prod"
    assert candidate.restore_database_name == "SALESDB_Prod"
    assert candidate.backup_file_on_vm == Path(r"C:\MSSQL\SQLBK_IMPORT\ACME-192-0-2-250\APPDB-DB$APPDB\SALESDB_Prod\FULL\latest.bak")
    assert candidate.restore_data_file_on_vm == Path(r"D:\MSSQL\DATA\SALESDB_Prod.mdf")


def test_build_restore_candidate_uses_explicit_target_database_mapping(tmp_path):
    config = BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"C:\MSSQL\SQLBK_IMPORT\ACME-192-0-2-250"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"C:\MSSQL\SQLBK_IMPORT\ACME-192-0-2-250"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        databases=(DatabaseRestoreMapping(source_database="SALESDB_Prod", target_database="APP_DR"),),
    )
    backup = config.vm_import_unc / "APPDB-DB$APPDB" / "SALESDB_Prod" / "FULL" / "latest.bak"

    candidate = build_restore_candidate(backup, config)

    assert candidate.source_database_name == "SALESDB_Prod"
    assert candidate.restore_database_name == "APP_DR"
    assert candidate.restore_data_file_on_vm == Path(r"D:\MSSQL\DATA\APP_DR.mdf")


def test_build_sqlcmd_query_command_runs_on_vm_when_vm_target_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_PASSWORD", "vm-secret")
    config = BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"E:\SQLBK_IMPORT"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"E:\LOGS"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="VM_IP",
        vm_username=r"VM_NAME\vmadmin",
        vm_password_env="VM_PASSWORD",
        restore_sql_instance_on_vm="localhost",
        source_database_name="APPDB_Prod",
        restore_database_name="APPDB_Prod_DR",
        restore_data_file_on_vm=Path(r"D:\MSSQL\DATA\APPDB_Prod_DR.mdf"),
        restore_log_file_on_vm=Path(r"D:\MSSQL\DATA\APPDB_Prod_DR_log.ldf"),
    )

    cmd = build_sqlcmd_query_command(sql="SELECT 1;", config=config)

    assert is_powershell_executable(cmd[0])
    assert cmd[1:4] == ["-NoProfile", "-ExecutionPolicy", "Bypass"]
    assert "Invoke-Command -ComputerName 'VM_IP' -Credential $credential" in cmd[-1]
    assert "ConvertTo-SecureString 'vm-secret'" in cmd[-1]
    assert "@('-E')" in cmd[-1]
    assert "ScriptBlock {;\n    param(" not in cmd[-1]
    assert "ScriptBlock {\n    param(" in cmd[-1]
    assert "& $SqlcmdPath -S $SqlInstance -C @sqlAuthArgs @timeoutArgs -b -Q $Sql" in cmd[-1]
    assert "$timeoutArgs = @('-l', '60', '-t', '0')" in cmd[-1]
    assert "'localhost'" in cmd[-1]


def test_build_sqlcmd_query_command_uses_sql_auth_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_PASSWORD", "vm-secret")
    monkeypatch.setenv("RESTORE_SQL_PASSWORD", "sql-secret")
    config = BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"E:\SQLBK_IMPORT"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"E:\LOGS"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="VM_IP",
        vm_username=r"VM_NAME\vmadmin",
        vm_password_env="VM_PASSWORD",
        restore_sql_instance_on_vm="localhost",
        restore_sql_username="restore_user",
        restore_sql_password_env="RESTORE_SQL_PASSWORD",
    )

    cmd = build_sqlcmd_query_command(sql="SELECT 1;", config=config)

    assert "@('-U', 'restore_user', '-P', 'sql-secret')" in cmd[-1]
    assert " -E " not in cmd[-1]


def test_sqlcmd_guard_rejects_windows_command_for_linux_restore_id(tmp_path):
    import db_ops.backup_restore.restore_database as restore_module

    config = dataclasses.replace(
        make_config(tmp_path),
        restore_id="ACME_TO_SQLSERVER_198_51_100_31",
        target_id="SQLSERVER-VM-TEST-198-51-100-31",
        vm_platform="linux",
        vm_credential_target="198.51.100.31",
    )
    wrong_cmd = [
        "powershell",
        "-Command",
        "Invoke-Command -ComputerName '198.51.100.129' -ScriptBlock { sqlcmd }",
    ]

    with pytest.raises(RuntimeError, match="target_os_type=linux"):
        restore_module.run_sqlcmd_query_command(wrong_cmd, config=config)


def test_linux_restore_command_uses_long_login_and_unlimited_query_timeout(tmp_path):
    config = dataclasses.replace(
        make_config(tmp_path),
        vm_platform="linux",
        vm_credential_target="198.51.100.31",
        sql_login_timeout_seconds=120,
        sql_query_timeout_seconds=0,
        restore_command_timeout_seconds=0,
    )

    cmd = build_sqlcmd_query_command(sql="RESTORE LOG [db] FROM DISK = N'/tmp/log.trn';", config=config)

    assert cmd[0] == "__ssh_sqlcmd__"
    assert cmd[-4:] == ["-l", "120", "-t", "0"]
    assert config.restore_command_timeout_seconds == 0


def test_long_running_linux_restore_has_no_default_command_deadline(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = dataclasses.replace(
        make_config(tmp_path),
        vm_platform="linux",
        vm_credential_target="198.51.100.31",
    )
    cmd = build_sqlcmd_query_command(sql="RESTORE DATABASE [db] FROM DISK = N'/tmp/full.bak';", config=config)
    captured = {}

    class FakeChannel:
        def __init__(self):
            self.polls = 0
            self.closed = False

        def exec_command(self, remote_cmd):
            captured["remote_cmd"] = remote_cmd

        def exit_status_ready(self):
            self.polls += 1
            return self.polls > 4

        def recv_ready(self):
            return False

        def recv_stderr_ready(self):
            return False

        def recv_exit_status(self):
            return 0

        def close(self):
            self.closed = True

    channel = FakeChannel()

    class FakeTransport:
        def open_session(self, timeout=None):
            captured["open_timeout"] = timeout
            return channel

    class FakeSsh:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_transport(self):
            return FakeTransport()

    monkeypatch.setattr(restore_module, "open_ssh_connection", lambda _config: FakeSsh())
    monkeypatch.setattr(restore_module.time, "sleep", lambda _seconds: None)

    result = restore_module._run_sqlcmd_via_ssh(cmd, config)

    assert result.returncode == 0
    assert captured["open_timeout"] == 60
    assert "-l 60 -t 0" in captured["remote_cmd"]
    assert channel.closed is False


def test_transient_login_timeout_retries_before_restore_command(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = dataclasses.replace(
        make_config(tmp_path),
        vm_platform="linux",
        vm_credential_target="198.51.100.31",
    )
    cmd = build_sqlcmd_query_command(sql="RESTORE LOG [db] FROM DISK = N'/tmp/log.trn';", config=config)
    calls = []
    results = [
        subprocess.CompletedProcess(cmd, 1, "", "Login timeout expired\nTCP Provider: Error code 0x102"),
        subprocess.CompletedProcess(cmd, 0, "RESTORE LOG successfully processed", ""),
    ]

    monkeypatch.setattr(
        restore_module,
        "_execute_sqlcmd_once",
        lambda *_args, **_kwargs: calls.append(1) or results.pop(0),
    )
    monkeypatch.setattr(restore_module.time, "sleep", lambda _seconds: None)

    result = run_sqlcmd_query_command(
        cmd,
        config=config,
        progress_step="restore-log",
        restore_id=config.restore_id,
    )

    assert result.returncode == 0
    assert len(calls) == 2


def test_two_restore_ids_dispatch_sql_to_their_own_remote_executor(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    windows = dataclasses.replace(
        make_config(tmp_path),
        restore_id="ACME_TO_SQLSERVER_198_51_100_129",
        target_id="SQLSERVER-VM-TEST-198-51-100-129",
        vm_platform="windows",
        vm_credential_target="198.51.100.129",
    )
    linux = dataclasses.replace(
        make_config(tmp_path),
        restore_id="ACME_TO_SQLSERVER_198_51_100_31",
        target_id="SQLSERVER-VM-TEST-198-51-100-31",
        vm_platform="linux",
        vm_credential_target="198.51.100.31",
    )
    calls = []
    monkeypatch.setattr(restore_module, "log_event", lambda *_args, **_kwargs: None)

    monkeypatch.setattr(
        restore_module,
        "_run_sqlcmd_query_command_streaming",
        lambda cmd, **kwargs: calls.append(("powershell", "198.51.100.129", cmd))
        or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    monkeypatch.setattr(
        restore_module,
        "_run_sqlcmd_via_ssh",
        lambda cmd, config: calls.append(("ssh", config.vm_credential_target, cmd))
        or subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    windows_cmd = build_sqlcmd_query_command(sql="SELECT 129;", config=windows)
    linux_cmd = build_sqlcmd_query_command(sql="SELECT 31;", config=linux)
    run_sqlcmd_query_command(
        windows_cmd,
        config=windows,
        logger=object(),
        progress_step="restore-full",
        restore_id=windows.restore_id,
    )
    run_sqlcmd_query_command(
        linux_cmd,
        config=linux,
        logger=object(),
        progress_step="restore-full",
        restore_id=linux.restore_id,
    )

    assert calls[0][0:2] == ("powershell", "198.51.100.129")
    assert calls[1][0:2] == ("ssh", "198.51.100.31")
    assert all(not (executor == "powershell" and host == "198.51.100.31") for executor, host, _ in calls)
    assert "198.51.100.129" not in calls[1][2]


def test_build_robocopy_command_uses_orchestrator_to_vm_paths(tmp_path):
    config = BackupRestoreConfig(
        prod_backup_share=Path(r"\\192.0.2.250\SQLBK"),
        vm_import_unc=Path(r"\\VM_IP\E$\SQLBK_IMPORT"),
        vm_import_local=Path(r"E:\SQLBK_IMPORT"),
        vm_log_unc=Path(r"\\VM_IP\E$\LOGS"),
        vm_log_local=Path(r"E:\LOGS"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        source_database_name="APPDB_Prod",
        restore_database_name="APPDB_Prod_DR",
        restore_data_file_on_vm=Path(r"D:\MSSQL\DATA\APPDB_Prod_DR.mdf"),
        restore_log_file_on_vm=Path(r"D:\MSSQL\DATA\APPDB_Prod_DR_log.ldf"),
    )

    cmd = build_robocopy_command(config)

    assert cmd[:3] == ["robocopy", str(config.prod_backup_share), str(config.vm_import_unc)]
    assert "/Z" in cmd
    assert "/R:3" in cmd
    assert r"/LOG:\\VM_IP\E$\LOGS\copy_sqlbk.log" in cmd


def test_build_robocopy_command_rejects_mapped_drive_source(tmp_path):
    config = make_config(tmp_path)
    config = BackupRestoreConfig(
        prod_backup_share=Path("Z:\\SQLBK"),
        vm_import_unc=config.vm_import_unc,
        vm_import_local=config.vm_import_local,
        vm_log_unc=config.vm_log_unc,
        vm_log_local=config.vm_log_local,
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm=config.restore_sql_instance_on_vm,
        source_database_name=config.source_database_name,
        restore_database_name=config.restore_database_name,
        restore_data_file_on_vm=config.restore_data_file_on_vm,
        restore_log_file_on_vm=config.restore_log_file_on_vm,
    )

    with pytest.raises(ValueError, match="UNC SMB path"):
        build_robocopy_command(config)


def test_build_cmdkey_command_uses_password_from_environment(monkeypatch):
    monkeypatch.setenv("SQLBK_SMB_PASSWORD", "SecretPassword")

    assert build_cmdkey_command(
        credential_target="192.0.2.251",
        username="sqlbackupuser",
        password_env="SQLBK_SMB_PASSWORD",
    ) == [
        "cmdkey",
        "/add:192.0.2.251",
        "/user:sqlbackupuser",
        "/pass:SecretPassword",
    ]


def test_build_copy_cmdkey_commands_includes_prod_and_vm_credentials(tmp_path, monkeypatch):
    config = BackupRestoreConfig(
        prod_backup_share=Path(r"\\192.0.2.250\SQLBK"),
        vm_import_unc=Path(r"\\VM_IP\E$\SQLBK_IMPORT"),
        vm_import_local=Path(r"E:\SQLBK_IMPORT"),
        vm_log_unc=Path(r"\\VM_IP\E$\LOGS"),
        vm_log_local=Path(r"E:\LOGS"),
        prod_smb_credential_target="192.0.2.250",
        prod_smb_username=r"192.0.2.250\appdbadmin",
        prod_smb_password_env="PROD_SQLBK_PASSWORD",
        vm_credential_target="VM_IP",
        vm_username=r"VM_NAME\vmadmin",
        vm_password_env="VM_PASSWORD",
        restore_sql_instance_on_vm="localhost",
        source_database_name="APPDB_Prod",
        restore_database_name="APPDB_Prod_DR",
        restore_data_file_on_vm=Path(r"D:\MSSQL\DATA\APPDB_Prod_DR.mdf"),
        restore_log_file_on_vm=Path(r"D:\MSSQL\DATA\APPDB_Prod_DR_log.ldf"),
    )
    monkeypatch.setenv("PROD_SQLBK_PASSWORD", "prod-secret")
    monkeypatch.setenv("VM_PASSWORD", "vm-secret")

    assert build_copy_cmdkey_commands(config) == [
        ["cmdkey", "/add:192.0.2.250", r"/user:192.0.2.250\appdbadmin", "/pass:prod-secret"],
        ["cmdkey", "/add:VM_IP", r"/user:VM_NAME\vmadmin", "/pass:vm-secret"],
    ]


def test_parse_restore_config_keeps_zero_copy_recent_hours(tmp_path):
    config = parse_restore_config(
        {
            "prod_backup_share": r"\\192.0.2.250\SQLBK",
            "vm_import_unc": str(tmp_path / "import_unc"),
            "vm_import_local": r"E:\SQLBK_IMPORT",
            "vm_log_unc": str(tmp_path / "logs_unc"),
            "vm_log_local": r"E:\LOGS",
            "copy_recent_hours": 0,
            "prod_smb_credential_target": "",
            "prod_smb_username": "",
            "prod_smb_password_env": "",
            "vm_credential_target": "",
            "vm_username": "",
            "vm_password_env": "",
            "restore_sql_instance_on_vm": "localhost",
            "source_database_name": "APPDB_Prod",
            "restore_database_name": "APPDB_Prod_DR",
            "restore_data_file_on_vm": r"D:\MSSQL\DATA\APPDB_Prod_DR.mdf",
            "restore_log_file_on_vm": r"D:\MSSQL\DATA\APPDB_Prod_DR_log.ldf",
        }
    )

    assert config.copy_recent_hours == 0


def test_parse_restore_config_reads_source_certificate_api_url(tmp_path):
    config = parse_restore_config(
        {
            "source": {
                "id": "ACME-192-0-2-250",
                "backup_share": r"\\192.0.2.250\SQLBK",
                "api_link_get_cer": "https://vault/v1/secret/data/sqlserver/cert.json",
            },
            "target": {
                "vm_import_unc": str(tmp_path / "import_unc"),
                "vm_import_local": r"E:\SQLBK_IMPORT",
                "vm_log_unc": str(tmp_path / "logs_unc"),
                "vm_log_local": r"E:\LOGS",
            },
        }
    )

    assert config.certificate_api_url == "https://vault/v1/secret/data/sqlserver/cert.json"
    assert config.certificate_api_token_ref == "TOKEN_192_0_2_112_VAULT"


def test_parse_restore_config_target_sql_instance_overrides_default(tmp_path):
    config = parse_restore_config(
        {
            "source": {
                "id": "ACME-192-0-2-250",
                "backup_share": r"\\192.0.2.250\SQLBK",
            },
            "target": {
                "vm_import_unc": str(tmp_path / "import_unc"),
                "vm_import_local": r"C:\SQLBK_IMPORT",
                "vm_log_unc": str(tmp_path / "logs_unc"),
                "vm_log_local": r"C:\SQLBK_IMPORT",
                "sql_instance": "localhost,1433",
                "restore_data_dir": r"C:\MSSQL\DATA\MSSQLSERVER",
            },
        }
    )

    assert config.restore_sql_instance_on_vm == "localhost,1433"
    assert config.restore_data_dir_on_vm == Path(r"C:\MSSQL\DATA\MSSQLSERVER")


def test_validate_restore_target_rejects_source_server_as_target(tmp_path):
    config = BackupRestoreConfig(
        prod_backup_share=Path(r"\\192.0.2.250\SQLBK"),
        vm_import_unc=Path(r"\\198.51.100.129\SQLBK_IMPORT"),
        vm_import_local=Path(r"C:\SQLBK_IMPORT"),
        vm_log_unc=tmp_path / "logs",
        vm_log_local=Path(r"C:\SQLBK_IMPORT"),
        prod_smb_credential_target="192.0.2.250",
        prod_smb_username="source_user",
        prod_smb_password_env="SOURCE_PASSWORD",
        vm_credential_target="192.0.2.250",
        vm_username="target_user",
        vm_password_env="TARGET_PASSWORD",
        restore_sql_instance_on_vm="localhost,1433",
    )

    with pytest.raises(ValueError, match="Unsafe restore config"):
        validate_restore_target_is_not_source(config)


def test_validate_restore_target_rejects_source_sql_instance(tmp_path):
    config = BackupRestoreConfig(
        prod_backup_share=Path(r"\\192.0.2.250\SQLBK"),
        vm_import_unc=Path(r"\\198.51.100.129\SQLBK_IMPORT"),
        vm_import_local=Path(r"C:\SQLBK_IMPORT"),
        vm_log_unc=tmp_path / "logs",
        vm_log_local=Path(r"C:\SQLBK_IMPORT"),
        prod_smb_credential_target="192.0.2.250",
        prod_smb_username="source_user",
        prod_smb_password_env="SOURCE_PASSWORD",
        vm_credential_target="198.51.100.129",
        vm_username="target_user",
        vm_password_env="TARGET_PASSWORD",
        restore_sql_instance_on_vm="192.0.2.250,1433",
    )

    with pytest.raises(ValueError, match="restore SQL instance points at source"):
        validate_restore_target_is_not_source(config)


def test_run_restore_database_stops_before_recovery_when_full_fails(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    backup = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    calls = []

    def fail_full(_cmd, **_kwargs):
        calls.append(_cmd[-1])
        raise RuntimeError("Msg 3013, Level 16\nRESTORE DATABASE is terminating abnormally.")

    monkeypatch.setattr("db_ops.backup_restore.restore_database.run_sqlcmd_query_command", fail_full)

    with pytest.raises(RuntimeError, match="Msg 3013"):
        run_restore_database(
            config=config,
            db_ops_config=app_config,
            backup_file=backup,
            ensure_certificate=False,
            ensure_credential=False,
        )

    assert len(calls) == 1
    assert "NORECOVERY" in calls[0]
    assert "WITH RECOVERY" not in calls[0]


def test_run_sqlcmd_query_command_treats_restore_error_text_as_failure(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = "Msg 3013, Level 16\nRESTORE LOG is terminating abnormally."
        stderr = ""

    monkeypatch.setattr("db_ops.backup_restore.restore_database.subprocess.run", lambda *args, **kwargs: FakeResult())

    with pytest.raises(RuntimeError, match="Msg 3013"):
        run_sqlcmd_query_command(["sqlcmd", "-Q", "RESTORE"])


def test_run_restore_database_logs_execution_timeline_and_sanitizes_secrets(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    config = BackupRestoreConfig(
        prod_backup_share=config.prod_backup_share,
        vm_import_unc=config.vm_import_unc,
        vm_import_local=config.vm_import_local,
        vm_log_unc=config.vm_log_unc,
        vm_log_local=config.vm_log_local,
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        restore_sql_username="restore_user",
        restore_sql_password_env="RESTORE_SQL_PASSWORD",
        source_id="SRC1",
        target_id="TGT1",
    )
    monkeypatch.setenv("RESTORE_SQL_PASSWORD", "super-secret")
    backup = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    messages = []
    commands = []

    def fake_run_sqlcmd(cmd, **_kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "10 percent processed.\ncomplete", "")

    monkeypatch.setattr(restore_module, "run_sqlcmd_query_command", fake_run_sqlcmd)
    monkeypatch.setattr(restore_module, "log_event", lambda _logger, **kwargs: messages.append(kwargs["message"]))

    result = run_restore_database(
        config=config,
        db_ops_config=app_config,
        backup_file=backup,
        ensure_certificate=False,
        ensure_credential=False,
        logger=object(),
    )

    assert result["status"] == "SUCCESS"
    assert commands and "super-secret" in commands[0]
    joined = "\n".join(messages)
    assert "restore-db start source_id=SRC1 target_id=TGT1" in joined
    assert "restore-db backup-chain selected database=APPDB_Prod" in joined
    assert "restore-db restore-plan database=APPDB_Prod" in joined
    assert "restore-db restore-full start database=APPDB_Prod" in joined
    assert "restore-db restore-full success database=APPDB_Prod" in joined
    assert "restore-db restore-diff skipped database=APPDB_Prod" in joined
    assert "reason=no_diff_backup" in joined
    assert "restore-db restore-log skipped database=APPDB_Prod" in joined
    assert "restore-db recovery start database=APPDB_Prod" in joined
    assert "restore-db recovery success database=APPDB_Prod" in joined
    assert "restore-db set-recovery-model start database=APPDB_Prod" in joined
    assert "restore-db set-recovery-model success database=APPDB_Prod" in joined
    assert "restore-db dbcc-checkdb start database=APPDB_Prod" in joined
    assert "restore-db dbcc-checkdb success database=APPDB_Prod" in joined
    assert "restore-db completed database=APPDB_Prod" in joined
    assert "super-secret" not in joined


def test_run_sqlcmd_query_command_streams_restore_progress(monkeypatch):
    messages = []

    class FakeStdout:
        def __iter__(self):
            return iter(["10 percent processed.\n", "20 percent processed.\n"])

    class FakeProcess:
        stdout = FakeStdout()

        def wait(self):
            return 0

    monkeypatch.setattr("db_ops.backup_restore.restore_database.subprocess.Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr("db_ops.backup_restore.restore_database.log_event", lambda _logger, **kwargs: messages.append(kwargs["message"]))

    result = run_sqlcmd_query_command(
        ["sqlcmd", "-Q", "RESTORE"],
        logger=object(),
        progress_step="restore-full",
        progress_database="SALESDB_Prod",
    )

    assert result.returncode == 0
    assert "restore-db restore-full progress database=SALESDB_Prod percent=10" in messages
    assert "restore-db restore-full progress database=SALESDB_Prod percent=20" in messages


def test_restore_log_backups_are_logged_one_file_at_a_time(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    backup = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    candidate = build_restore_candidate(backup, config)
    logs = [
        config.vm_import_unc / "APPDB_Prod" / "LOG" / "one.trn",
        config.vm_import_unc / "APPDB_Prod" / "LOG" / "two.trn",
    ]
    messages = []

    monkeypatch.setattr(
        restore_module,
        "run_sqlcmd_query_command",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, "RESTORE LOG complete", ""),
    )
    monkeypatch.setattr(restore_module, "log_event", lambda _logger, **kwargs: messages.append(kwargs["message"]))

    result = run_restore_log(config=config, candidate=candidate, selected_backups=logs, logger=object())

    assert result["status"] == "SUCCESS"
    joined = "\n".join(messages)
    assert "restore-db restore-log start database=APPDB_Prod" in joined
    assert "sequence=1 total=2" in joined
    assert "sequence=2 total=2" in joined
    assert "restore-db restore-log success database=APPDB_Prod" in joined


def test_pitr_restore_log_stopat_recovery_only_on_final_log(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    full = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    logs = [
        config.vm_import_unc / "APPDB_Prod" / "LOG" / "log_001.trn",
        config.vm_import_unc / "APPDB_Prod" / "LOG" / "log_002.trn",
    ]
    candidate = build_restore_candidate(full, config)
    sql_texts = []

    def fake_run_step(**kwargs):
        sql_texts.append(kwargs["sql"])
        return {"step": "restore-log", "status": "SUCCESS", "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(restore_module, "_run_restore_step", fake_run_step)
    stopat = datetime.datetime(2026, 6, 8, 5, 0, tzinfo=datetime.timezone.utc)

    run_restore_log(config=config, candidate=candidate, selected_backups=logs, stopat_utc=stopat)

    assert "STOPAT" not in sql_texts[0]
    assert "WITH NORECOVERY" in sql_texts[0]
    assert "STOPAT" in sql_texts[1]
    assert "WITH RECOVERY" in sql_texts[1]


def test_restore_log_failure_is_not_hidden(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    full = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    log = config.vm_import_unc / "APPDB_Prod" / "LOG" / "log_001.trn"
    candidate = build_restore_candidate(full, config)
    messages = []

    monkeypatch.setattr(
        restore_module,
        "run_sqlcmd_query_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("The log in this backup set begins at LSN 200, which is too recent to apply.")
        ),
    )
    monkeypatch.setattr(restore_module, "log_event", lambda _logger, **kwargs: messages.append(kwargs["message"]))

    with pytest.raises(RuntimeError, match="too recent to apply"):
        run_restore_log(config=config, candidate=candidate, selected_backups=[log], logger=object())

    assert "reason=lsn_gap" in "\n".join(messages)


def test_restore_log_msg_4305_skips_and_tries_next_log(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    full = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    logs = [
        config.vm_import_unc / "APPDB_Prod" / "LOG" / "APPDB-DB$APPDB_SALESDB_Prod_LOG_20260608_010000.trn",
        config.vm_import_unc / "APPDB_Prod" / "LOG" / "APPDB-DB$APPDB_SALESDB_Prod_LOG_20260608_011500.trn",
    ]
    candidate = build_restore_candidate(full, config)
    calls = []
    messages = []

    def fake_run_step(**kwargs):
        calls.append(kwargs["metadata"]["file"])
        if len(calls) == 1:
            raise RuntimeError(
                "Msg 4305, Level 16, State 1. The log in this backup set begins at LSN 200, "
                "which is too recent to apply to the database."
            )
        return {"step": "restore-log", "status": "SUCCESS", "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(restore_module, "_run_restore_step", fake_run_step)
    monkeypatch.setattr(restore_module, "log_event", lambda _logger, **kwargs: messages.append(kwargs["message"]))

    result = run_restore_log(config=config, candidate=candidate, selected_backups=logs, logger=object())

    assert result["status"] == "SUCCESS"
    assert len(calls) == 2
    assert str(vm_unc_to_local_path(logs[0], config)) in calls[0]
    assert str(vm_unc_to_local_path(logs[1], config)) in calls[1]
    assert "reason=msg_4305_too_recent_try_next_log" in "\n".join(messages)


def test_restore_log_all_msg_4305_fails_with_lsn_gap(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    full = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    log = config.vm_import_unc / "APPDB_Prod" / "LOG" / "log_001.trn"
    candidate = build_restore_candidate(full, config)

    monkeypatch.setattr(
        restore_module,
        "_run_restore_step",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "Msg 4305, Level 16, State 1. The log in this backup set begins at LSN 200, "
                "which is too recent to apply to the database."
            )
        ),
    )

    with pytest.raises(RuntimeError, match="missing required earlier LOG backup / LSN gap"):
        run_restore_log(config=config, candidate=candidate, selected_backups=[log])


def test_msg_4305_skip_applies_only_to_restore_log_not_full_or_diff(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    full = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    diff = config.vm_import_unc / "APPDB_Prod" / "DIFF" / "diff.bak"
    candidate = build_restore_candidate(full, config)

    monkeypatch.setattr(
        restore_module,
        "run_sqlcmd_query_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "Msg 4305, Level 16, State 1. The log in this backup set begins at LSN 200, "
                "which is too recent to apply to the database."
            )
        ),
    )

    with pytest.raises(RuntimeError, match="Msg 4305"):
        run_restore_full(config=config, candidate=candidate)
    with pytest.raises(RuntimeError, match="Msg 4305"):
        run_restore_diff(config=config, candidate=candidate, selected_backup=diff)


def test_restore_log_non_4305_error_fails_immediately(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    full = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    logs = [
        config.vm_import_unc / "APPDB_Prod" / "LOG" / "log_001.trn",
        config.vm_import_unc / "APPDB_Prod" / "LOG" / "log_002.trn",
    ]
    candidate = build_restore_candidate(full, config)
    calls = []

    def fake_run_step(**kwargs):
        calls.append(kwargs["metadata"]["file"])
        raise RuntimeError("Msg 9001, Level 16, State 1. Non-4305 failure.")

    monkeypatch.setattr(restore_module, "_run_restore_step", fake_run_step)

    with pytest.raises(RuntimeError, match="Msg 9001"):
        run_restore_log(config=config, candidate=candidate, selected_backups=logs)
    assert len(calls) == 1


def test_restore_log_timeout_checks_history_and_does_not_duplicate_applied_log(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    full = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    log = config.vm_import_unc / "APPDB_Prod" / "LOG" / "log_001.trn"
    candidate = build_restore_candidate(full, config)
    restore_calls = []
    resume_checks = []

    def timeout_once(**kwargs):
        restore_calls.append(kwargs["metadata"]["file"])
        raise restore_module.RestoreCommandTimeoutError("timed out", command_started=True)

    monkeypatch.setattr(restore_module, "_run_restore_step", timeout_once)
    monkeypatch.setattr(
        restore_module,
        "_inspect_log_restore_resume_state",
        lambda **kwargs: resume_checks.append(kwargs["current_backup"]) or {
            "resume_decision": "confirmed_last_log_restored",
            "last_confirmed_log": str(kwargs["current_backup"]),
        },
    )

    result = run_restore_log(config=config, candidate=candidate, selected_backups=[log])

    assert result["status"] == "SUCCESS"
    assert len(restore_calls) == 1
    assert resume_checks == [vm_unc_to_local_path(log, config)]
    assert result["results"][0]["status"] == "SUCCESS_RESUMED"


def test_restore_log_timeout_unsafe_resume_fails_explicitly(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    full = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    log = config.vm_import_unc / "APPDB_Prod" / "LOG" / "log_001.trn"
    candidate = build_restore_candidate(full, config)
    restore_calls = []

    def timeout_once(**kwargs):
        restore_calls.append(kwargs["metadata"]["file"])
        raise restore_module.RestoreCommandTimeoutError("timed out", command_started=True)

    monkeypatch.setattr(restore_module, "_run_restore_step", timeout_once)
    monkeypatch.setattr(
        restore_module,
        "_inspect_log_restore_resume_state",
        lambda **_kwargs: {
            "resume_decision": "unsafe",
            "last_confirmed_log": "unknown",
        },
    )

    with pytest.raises(RuntimeError, match="reason=restore_timeout_resume_unsafe"):
        run_restore_log(config=config, candidate=candidate, selected_backups=[log])

    assert len(restore_calls) == 1


def test_resume_check_requires_exact_log_identity(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    full = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    current_log = config.vm_import_local / "APPDB_Prod" / "LOG" / "log_002.trn"
    candidate = build_restore_candidate(full, config)
    output = (
        "DBOPS_RESUME|0|42|100000|200000|2|"
        + str(current_log)
    )
    monkeypatch.setattr(
        restore_module,
        "run_sqlcmd_query_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, ""),
    )

    result = restore_module._inspect_log_restore_resume_state(
        config=config,
        candidate=candidate,
        current_backup=current_log,
        logger=None,
    )

    assert result["resume_decision"] == "confirmed_last_log_restored"
    assert result["last_confirmed_log"] == str(current_log)


def test_resume_check_rejects_restoring_state_without_exact_log_identity(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = make_config(tmp_path)
    full = config.vm_import_unc / "APPDB_Prod" / "FULL" / "latest.bak"
    current_log = config.vm_import_local / "APPDB_Prod" / "LOG" / "log_002.trn"
    previous_log = config.vm_import_local / "APPDB_Prod" / "LOG" / "log_001.trn"
    candidate = build_restore_candidate(full, config)
    output = (
        "DBOPS_RESUME|1|41|1|99999|2|"
        + str(previous_log)
    )
    monkeypatch.setattr(
        restore_module,
        "run_sqlcmd_query_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, ""),
    )

    result = restore_module._inspect_log_restore_resume_state(
        config=config,
        candidate=candidate,
        current_backup=current_log,
        logger=None,
    )

    assert result["resume_decision"] == "unsafe"
    assert result["last_confirmed_log"] == str(previous_log)


def test_restore_stdout_summary_parses_large_sql_server_restore_output():
    output = "\n".join(
        [
            "10 percent processed.",
            "Processed 984248 pages for database 'APPDB_Prod', file 'APPDB_Prod' on file 1.",
            "Processed 2 pages for database 'APPDB_Prod', file 'APPDB_Prod_log' on file 1.",
            "RESTORE DATABASE successfully processed 984250 pages in 52.472 seconds (146.542 MB/sec).",
        ]
    )

    summary = summarize_restore_stdout(output)

    assert summary["pages"] == 984250
    assert summary["sql_duration_seconds"] == 52.472
    assert summary["throughput_mb_sec"] == 146.542


def test_restore_sanitizer_redacts_sensitive_text():
    text = (
        "sqlcmd -S localhost -U restore -P plain-secret "
        "-P abc123@ -P 'abc123@' -P \"abc123@\" "
        "@('-U', 'sa', '-P', 'abc123@') "
        "Password=connection-secret; "
        "ConvertTo-SecureString 'vm-secret' -AsPlainText -Force "
        "DECRYPTION BY PASSWORD = N'cert-secret' /pass:cmd-secret"
    )

    sanitized = sanitize_text(text)

    assert "abc123@" not in sanitized
    assert "plain-secret" not in sanitized
    assert "connection-secret" not in sanitized
    assert "vm-secret" not in sanitized
    assert "cert-secret" not in sanitized
    assert "cmd-secret" not in sanitized
    assert "-P ***" in sanitized
    assert "-P '***'" in sanitized
    assert "-P \"***\"" in sanitized
    assert "ConvertTo-SecureString '***'" in sanitized
    assert sanitized.count("***") >= 5


def test_restore_sanitizer_redacts_json_result_values():
    sanitized = sanitize_value(
        {
            "command": ["sqlcmd", "-P", "sql-secret"],
            "private_key_password": "cert-secret",
            "sql": "DECRYPTION BY PASSWORD = N'cert-secret'",
            "stderr": "Password=connection-secret;",
        }
    )

    assert "sql-secret" not in str(sanitized)
    assert "cert-secret" not in str(sanitized)
    assert "connection-secret" not in str(sanitized)
    assert "***" in str(sanitized)


def test_runtime_stdout_bridge_sanitizes_before_writing(tmp_path):
    from io import StringIO

    from db_ops.logging_ops.runtime_stdout import TeeStdout

    stream = StringIO()
    log_path = tmp_path / "restore_workflow_runtime.log"
    tee = TeeStdout(stream, log_path, app_name="restore_workflow", sanitizer=sanitize_text)

    tee.write("sqlcmd -S localhost -U sa -P abc123@\n")
    tee.write("ConvertTo-SecureString 'abc123@' -AsPlainText -Force\n")

    console_text = stream.getvalue()
    log_text = log_path.read_text(encoding="utf-8")
    assert "abc123@" not in console_text
    assert "abc123@" not in log_text
    assert "-P ***" in console_text
    assert "ConvertTo-SecureString '***'" in log_text


def test_build_add_certificate_sql_checks_name_or_thumbprint_before_create():
    certificate = BackupCertificate(
        certificate_name="APPDB_PROD_100_250_2026",
        thumbprint="0xFA247A43A377B01DD94EDA3CAF46CAD1FF68E019",
        certificate_base64="Y2VydA==",
        private_key_base64="cHZr",
        private_key_password="pass'word",
    )

    sql = build_add_certificate_sql(certificate)

    assert "FROM sys.certificates" in sql
    assert "name = N'APPDB_PROD_100_250_2026'" in sql
    assert "CONVERT(varchar(66), thumbprint, 1) = '0xFA247A43A377B01DD94EDA3CAF46CAD1FF68E019'" in sql
    assert "CREATE CERTIFICATE [APPDB_PROD_100_250_2026]" in sql
    assert "DECRYPTION BY PASSWORD = N'pass''word'" in sql


def test_build_add_certificate_command_runs_on_vm_and_writes_cert_files(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_PASSWORD", "vm-secret")
    config = BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"E:\SQLBK_IMPORT"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"E:\LOGS"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="VM_IP",
        vm_username=r"VM_NAME\vmadmin",
        vm_password_env="VM_PASSWORD",
        restore_sql_instance_on_vm="localhost",
    )
    certificate = BackupCertificate(
        certificate_name="APPDB_PROD_100_250_2026",
        thumbprint="FA247A43A377B01DD94EDA3CAF46CAD1FF68E019",
        certificate_base64="Y2VydA==",
        private_key_base64="cHZr",
        private_key_password="secret",
    )

    cmd = build_add_certificate_command(certificate=certificate, config=config)

    assert is_powershell_executable(cmd[0])
    assert cmd[1:4] == ["-NoProfile", "-ExecutionPolicy", "Bypass"]
    assert "Invoke-Command -ComputerName 'VM_IP' -Credential $credential" in cmd[-1]
    assert "ScriptBlock {;\n    param(" not in cmd[-1]
    assert "ScriptBlock {\n    param(" in cmd[-1]
    assert "WriteAllBytes($cerPath, [Convert]::FromBase64String($CerBase64))" in cmd[-1]
    assert "& $SqlcmdPath -S $SqlInstance -C @SqlAuthArgs -b -Q $Sql" in cmd[-1]
    assert "CREATE CERTIFICATE [APPDB_PROD_100_250_2026]" in cmd[-1]


def test_parse_backup_certificate_reads_vault_payload_data():
    certificate = parse_backup_certificate(
        {
            "backup_cert_base64": "Y2VydA==",
            "backup_private_key_base64": "cHZr",
            "certificate_name": "APPDB_PROD_100_250_2026",
            "private_key_password": "secret",
            "thumbprint": "0xFA247A43A377B01DD94EDA3CAF46CAD1FF68E019",
        }
    )

    assert certificate.certificate_name == "APPDB_PROD_100_250_2026"
    assert certificate.private_key_password == "secret"


def test_ensure_source_certificate_dry_run_skips_api_call(tmp_path):
    config = BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"E:\SQLBK_IMPORT"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"E:\LOGS"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        certificate_api_url="https://vault/v1/secret/data/sqlserver/cert.json",
    )

    assert ensure_source_certificate(config=config, dry_run=True)["status"] == "DRY_RUN"


def test_ensure_source_certificate_with_events_skips_alert_when_api_not_configured(tmp_path, monkeypatch):
    config = BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"E:\SQLBK_IMPORT"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"E:\LOGS"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
    )
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
        telegram=TelegramConfig(
            enabled=True,
            bot_token="token",
            level_chat_map={"logging": "group-log", "critical": "group-critical"},
        ),
    )
    result = ensure_source_certificate_with_events(config=config, app_config=app_config, dry_run=False)
    store = DbOpsStore(app_config.sqlite_path)
    rows = store.fetch_recent_job_runs(limit=5)
    send_messages = store.fetch_pending_telegram_send_messages(limit=10)

    assert result["status"] == "SKIPPED_NO_CERTIFICATE_API"
    assert send_messages == []
    assert rows == []


#: level -> chat, the routing both event tests below assume.
_ROUTES = {"logging": "group-log", "critical": "group-critical"}


def test_ensure_source_certificate_with_events_pushes_critical_on_error(tmp_path, monkeypatch):
    # Routing is the Telegram app's answer, fetched through its CLI, so stub this app's client
    # rather than app_config's TelegramConfig groups.
    monkeypatch.setattr(
        "db_ops.lib.telegram_route.telegram_route",
        lambda level, **_: {"enabled": True, "alert": bool(_ROUTES.get(level)),
                            "chat_id": _ROUTES.get(level, "")},
    )

    config = BackupRestoreConfig(
        prod_backup_share=tmp_path / "prod_share",
        vm_import_unc=tmp_path / "vm_import_unc",
        vm_import_local=Path(r"E:\SQLBK_IMPORT"),
        vm_log_unc=tmp_path / "vm_logs",
        vm_log_local=Path(r"E:\LOGS"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        source_id="SRC1",
        target_id="TGT1",
        certificate_api_url="https://vault/v1/secret/data/sqlserver/cert.json",
    )
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
        telegram=TelegramConfig(
            enabled=True,
            bot_token="token",
            level_chat_map={"logging": "group-log", "critical": "group-critical"},
        ),
    )

    def fail_certificate(**kwargs):
        raise RuntimeError("cert failed")

    monkeypatch.setattr("db_ops.backup_restore.certificate.ensure_source_certificate", fail_certificate)
    with pytest.raises(RuntimeError, match="cert failed"):
        ensure_source_certificate_with_events(config=config, app_config=app_config, dry_run=False)

    store = DbOpsStore(app_config.sqlite_path)
    rows = store.fetch_recent_job_runs(limit=2)
    send_messages = store.fetch_pending_telegram_send_messages(limit=10)

    assert [row["job_code"] for row in reversed(rows)] == [
        "backup_restore.restore-latest.cert_start",
        "backup_restore.restore-latest.cert_error",
    ]
    assert send_messages[-1]["tlgchat_id"] == "group-critical"
    assert "certificate import failed" in send_messages[-1]["message_text"]


def test_run_add_certificate_command_reports_stdout_stderr(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = "CERT_IMPORT: creating cert dir: C:\\SQLBK\\__db_ops_cert"
        stderr = "CREATE CERTIFICATE failed"

    monkeypatch.setattr(
        "db_ops.backup_restore.certificate.subprocess.run",
        lambda *args, **kwargs: FakeResult(),
    )

    with pytest.raises(RuntimeError) as exc:
        _run_add_certificate_command(["powershell", "-Command", "fail"])

    assert "Certificate import command failed with exit code 1" in str(exc.value)
    assert "CERT_IMPORT: creating cert dir" in str(exc.value)
    assert "CREATE CERTIFICATE failed" in str(exc.value)


def test_run_add_certificate_command_reports_timeout(monkeypatch):
    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0],
            timeout=kwargs["timeout"],
            output="partial stdout",
            stderr="partial stderr",
        )

    monkeypatch.setattr("db_ops.backup_restore.certificate.subprocess.run", timeout_run)

    with pytest.raises(RuntimeError) as exc:
        _run_add_certificate_command(["powershell", "-Command", "hang"], timeout_seconds=5)

    assert "timed out after 5 seconds" in str(exc.value)
    assert "partial stdout" in str(exc.value)
    assert "partial stderr" in str(exc.value)


def test_copy_backup_cli_accepts_hours_override():
    args = parse_args(["copy-backup", "--config", "config.json", "--source-id", "ACME-192-0-2-250", "--hours", "24"])

    assert args.command == "copy-backup"
    assert args.source_id == "ACME-192-0-2-250"
    assert args.hours == 24


def test_restore_workflow_cli_accepts_defaults():
    args = parse_args(["restore-workflow", "--config", "config.json"])

    assert args.command == "restore-workflow"
    assert args.copy_hours == 24
    # None, not 48: the flag is now an override. Unset means "use this entry's own
    # target_retention_seconds", so one entry can keep a day and another eight without
    # the scheduled command having to pass anything.
    assert args.delete_hours is None


def test_restore_workflow_orchestrates_existing_steps_in_order(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    calls = []

    def fake_copy(step_config, logger=None, force=False):
        calls.append(("copy-backup", step_config.copy_recent_hours))
        return CopyBackupResult(
            returncode=0,
            source_backup_dir=step_config.prod_backup_share,
            local_import_dir=step_config.vm_import_unc,
            files_considered=1,
            copied=1,
            skipped=0,
            file_results=(),
        )

    def fake_restore(**kwargs):
        calls.append(("restore-latest", kwargs["config"].source_id))
        return {"status": "SUCCESS"}

    def fake_delete(step_config, logger=None):
        calls.append(("delete-backup", step_config.copy_recent_hours))
        return DeleteBackupResult(
            returncode=0,
            target_backup_dir=step_config.vm_import_unc,
            delete_older_than_hours=step_config.copy_recent_hours,
            files_considered=1,
            deleted=1,
            file_results=(),
        )

    monkeypatch.setattr("db_ops.backup_restore.cli.run_copy_backup", fake_copy)
    monkeypatch.setattr("db_ops.backup_restore.cli.run_restore_all_latest", fake_restore)
    monkeypatch.setattr("db_ops.backup_restore.cli.run_delete_backup", fake_delete)
    monkeypatch.setattr("db_ops.backup_restore.cli.run_target_preflight", lambda config, logger=None: None)

    result = run_restore_workflow(restore_configs=[config], app_config=app_config)

    # 192h = the 8-day default retention, resolved from the entry rather than a CLI default.
    assert calls == [("copy-backup", 24), ("restore-latest", config.source_id), ("delete-backup", 192)]
    assert result["overall_workflow_status"] == "SUCCESS"


def test_import_certificate_cli_accepts_source_id():
    args = parse_args(["import-certificate", "--config", "config.json", "--source-id", "ACME-192-0-2-250"])

    assert args.command == "import-certificate"
    assert args.source_id == "ACME-192-0-2-250"


def test_restore_latest_end_event_counts_stdout_restore_errors_as_failed():
    level, message, metadata = _build_end_event(
        command="restore-latest",
        output={
            "status": "SUCCESS",
            "sources_considered": 1,
            "sources": [
                {
                    "source_id": "SRC1",
                    "results": [
                        {
                            "status": "SUCCESS",
                            "source_id": "SRC1",
                            "database_name": "SALESDB_Prod",
                            "stdout": "RESTORE DATABASE successfully processed",
                            "stderr": "",
                        },
                        {
                            "status": "SUCCESS",
                            "source_id": "SRC1",
                            "database_name": "APPDB_Prod",
                            "stdout": "Msg 3287, Level 16, State 1\nRESTORE DATABASE is terminating abnormally.",
                            "stderr": "",
                            "backup_file_on_vm": r"C:\SQLBK\bad.bak",
                        },
                    ],
                }
            ],
        },
    )

    assert level == "critical"
    assert "restore_success=1/2" in message
    assert metadata["restore_success"] == 1
    assert metadata["restore_total"] == 2
    assert metadata["restore_failed"] == 1
    assert metadata["failed_databases"][0]["database_name"] == "APPDB_Prod"


def test_delete_backup_cli_requires_hours_and_accepts_source_id():
    args = parse_args(["delete-backup", "--config", "config.json", "--source-id", "ACME-192-0-2-250", "--hours", "24"])

    assert args.command == "delete-backup"
    assert args.source_id == "ACME-192-0-2-250"
    assert args.hours == 24


def test_load_restore_configs_reads_sources_array(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        """
{
  "backup_restore": {
    "copy_recent_hours": 12,
    "vm_credential_target": "198.51.100.129",
    "vm_username": "198.51.100.129\\\\administrator",
    "sources": [
      {
        "source_id": "ACME-192-0-2-250",
        "prod_backup_share": "\\\\\\\\192.0.2.250\\\\SQLBK",
        "vm_import_unc": "\\\\\\\\198.51.100.129\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250",
        "vm_import_local": "C:\\\\MSSQL\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250",
        "vm_log_unc": "\\\\\\\\198.51.100.129\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250",
        "vm_log_local": "C:\\\\MSSQL\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250"
      },
      {
        "source_id": "ACME-192-0-2-248",
        "prod_backup_share": "\\\\\\\\192.0.2.248\\\\SQLBK",
        "vm_import_unc": "\\\\\\\\198.51.100.129\\\\SQLBK_IMPORT\\\\ACME-192-0-2-248",
        "vm_import_local": "C:\\\\MSSQL\\\\SQLBK_IMPORT\\\\ACME-192-0-2-248",
        "vm_log_unc": "\\\\\\\\198.51.100.129\\\\SQLBK_IMPORT\\\\ACME-192-0-2-248",
        "vm_log_local": "C:\\\\MSSQL\\\\SQLBK_IMPORT\\\\ACME-192-0-2-248",
        "copy_recent_hours": 24
      }
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )

    configs = load_restore_configs(config_file)

    assert [config.source_id for config in configs] == ["ACME-192-0-2-250", "ACME-192-0-2-248"]
    assert configs[0].copy_recent_hours == 12
    assert configs[1].copy_recent_hours == 24
    assert configs[0].vm_username == r"198.51.100.129\administrator"


def test_load_restore_configs_reads_active_flag_from_restores(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        """
{
  "backup_restore": {
    "restores": [
      {
        "restore_id": "RESTORE_ACTIVE",
        "active": true,
        "prod_backup_share": "\\\\\\\\source\\\\SQLBK",
        "vm_import_unc": "\\\\\\\\target\\\\SQLBK_IMPORT\\\\active",
        "vm_import_local": "C:\\\\SQLBK_IMPORT\\\\active",
        "vm_log_unc": "\\\\\\\\target\\\\SQLBK_IMPORT\\\\active",
        "vm_log_local": "C:\\\\SQLBK_IMPORT\\\\active"
      },
      {
        "restore_id": "RESTORE_INACTIVE",
        "active": false,
        "prod_backup_share": "\\\\\\\\source\\\\SQLBK",
        "vm_import_unc": "\\\\\\\\target\\\\SQLBK_IMPORT\\\\inactive",
        "vm_import_local": "C:\\\\SQLBK_IMPORT\\\\inactive",
        "vm_log_unc": "\\\\\\\\target\\\\SQLBK_IMPORT\\\\inactive",
        "vm_log_local": "C:\\\\SQLBK_IMPORT\\\\inactive"
      }
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )

    configs = load_restore_configs(config_file)

    assert [config.restore_id for config in configs] == ["RESTORE_ACTIVE", "RESTORE_INACTIVE"]
    assert [config.active for config in configs] == [True, False]


def test_load_restore_configs_reads_source_target_database_pairs(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        """
{
  "backup_restore": {
    "copy_recent_hours": 24,
    "target": {
      "id": "RESTORE-VM-192-168-163-128",
      "credential_target": "198.51.100.129",
      "username": "198.51.100.129\\\\administrator",
      "password_env": "VM_PASSWORD",
      "sql_instance": "localhost",
      "restore_data_dir": "D:\\\\MSSQL\\\\DATA"
    },
    "sources": [
      {
        "source": {
          "id": "ACME-192-0-2-250",
          "backup_share": "\\\\\\\\192.0.2.250\\\\SQLBK",
          "credential_target": "192.0.2.250",
          "username": "192.0.2.250\\\\appdbadmin",
          "password_env": "PROD_SQLBK_PASSWORD"
        },
        "target": {
          "vm_import_unc": "\\\\\\\\198.51.100.129\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250",
          "vm_import_local": "C:\\\\MSSQL\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250",
          "vm_log_unc": "\\\\\\\\198.51.100.129\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250",
          "vm_log_local": "C:\\\\MSSQL\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250"
        },
        "databases": [
          {"source_database": "SALESDB_Prod", "target_database": "APP_DR"},
          {"source_database": "APPDB_Prod", "target_database": "APPDB_Prod_DR"}
        ]
      }
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )

    configs = load_restore_configs(config_file)

    assert configs[0].source_id == "ACME-192-0-2-250"
    assert configs[0].target_id == "RESTORE-VM-192-168-163-128"
    assert configs[0].prod_backup_share == Path(r"\\192.0.2.250\SQLBK")
    assert configs[0].vm_username == r"198.51.100.129\administrator"
    assert configs[0].databases == (
        DatabaseRestoreMapping(source_database="SALESDB_Prod", target_database="APP_DR"),
        DatabaseRestoreMapping(source_database="APPDB_Prod", target_database="APPDB_Prod_DR"),
    )


def test_load_restore_configs_keeps_common_target_when_per_source_target_fields_are_blank(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        """
{
  "backup_restore": {
    "target": {
      "id": "RESTORE-VM-192-168-163-128",
      "credential_target": "198.51.100.129",
      "username": "198.51.100.129\\\\administrator",
      "password_env": "VM_PASSWORD",
      "sql_instance": "localhost",
      "sql_username": "restore_login",
      "sql_password_env": "RESTORE_SQL_PASSWORD",
      "restore_data_dir": "D:\\\\MSSQL\\\\DATA"
    },
    "sources": [
      {
        "source": {
          "id": "ACME-192-0-2-250",
          "backup_share": "\\\\\\\\192.0.2.250\\\\SQLBK"
        },
        "target": {
          "vm_import_unc": "\\\\\\\\198.51.100.129\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250",
          "vm_import_local": "C:\\\\MSSQL\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250",
          "vm_log_unc": "\\\\\\\\198.51.100.129\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250",
          "vm_log_local": "C:\\\\MSSQL\\\\SQLBK_IMPORT\\\\ACME-192-0-2-250",
          "credential_target": "",
          "username": "",
          "password_env": "",
          "sql_username": "",
          "sql_password_env": ""
        }
      }
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )

    configs = load_restore_configs(config_file)

    assert configs[0].vm_credential_target == "198.51.100.129"
    assert configs[0].vm_username == r"198.51.100.129\administrator"
    assert configs[0].restore_sql_instance_on_vm == "localhost"
    assert configs[0].restore_sql_username == "restore_login"
    assert configs[0].restore_sql_password_env == "RESTORE_SQL_PASSWORD"


def test_list_recent_backup_files_filters_by_pattern_and_mtime(tmp_path):
    source = tmp_path / "source"
    full_dir = source / "APPDB_Prod" / "FULL"
    log_dir = source / "APPDB_Prod" / "LOG"
    full_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)

    recent_bak = full_dir / "recent.bak"
    recent_trn = log_dir / "recent.trn"
    old_bak = full_dir / "old.bak"
    ignored_txt = full_dir / "ignored.txt"
    for path in [recent_bak, recent_trn, old_bak, ignored_txt]:
        path.write_text(path.name, encoding="utf-8")

    import os

    now = 1_800_000_000
    os.utime(recent_bak, (now - 60, now - 60))
    os.utime(recent_trn, (now - 120, now - 120))
    os.utime(old_bak, (now - 90_000, now - 90_000))
    os.utime(ignored_txt, (now - 60, now - 60))

    config = make_config(tmp_path)
    config = BackupRestoreConfig(
        prod_backup_share=source,
        vm_import_unc=config.vm_import_unc,
        vm_import_local=config.vm_import_local,
        vm_log_unc=config.vm_log_unc,
        vm_log_local=config.vm_log_local,
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        source_database_name="APPDB_Prod",
        restore_database_name="APPDB_Prod_DR",
        restore_data_file_on_vm=Path(r"D:\MSSQL\DATA\APPDB_Prod_DR.mdf"),
        restore_log_file_on_vm=Path(r"D:\MSSQL\DATA\APPDB_Prod_DR_log.ldf"),
        copy_recent_hours=24,
    )

    assert list_recent_backup_files(config, now=now) == [recent_trn, recent_bak]


def test_list_recent_backup_files_honors_explicit_point_in_time_window(tmp_path):
    source = tmp_path / "source"
    full_dir = source / "APPDB_Prod" / "FULL"
    full_dir.mkdir(parents=True)
    before = full_dir / "before.bak"
    inside = full_dir / "inside.bak"
    after = full_dir / "after.bak"
    for path in [before, inside, after]:
        path.write_text(path.name, encoding="utf-8")

    import os

    start = datetime.datetime(2026, 6, 7, 5, 0, tzinfo=datetime.timezone.utc)
    end = datetime.datetime(2026, 6, 8, 5, 0, tzinfo=datetime.timezone.utc)
    os.utime(before, (start.timestamp() - 1, start.timestamp() - 1))
    os.utime(inside, (start.timestamp() + 3600, start.timestamp() + 3600))
    os.utime(after, (end.timestamp() + 1, end.timestamp() + 1))

    config = dataclasses.replace(
        make_config(tmp_path),
        prod_backup_share=source,
        copy_recent_hours=24,
        copy_window_start_utc=start,
        copy_window_end_utc=end,
    )

    assert list_recent_backup_files(config, now=datetime.datetime(2026, 6, 27, tzinfo=datetime.timezone.utc).timestamp()) == [inside]


def test_copy_backup_file_preserves_relative_folder_and_skips_existing(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_file = source_root / "APPDB_Prod" / "FULL" / "latest.bak"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("backup", encoding="utf-8")

    copied = copy_backup_file(source_file, source_root=source_root, target_root=target_root)

    target_file = target_root / "APPDB_Prod" / "FULL" / "latest.bak"
    assert copied.status == "COPIED"
    assert copied.target_file == target_file
    assert target_file.read_text(encoding="utf-8") == "backup"

    skipped = copy_backup_file(source_file, source_root=source_root, target_root=target_root)

    assert skipped.status == "SKIPPED_EXISTS"


@windows_orchestrator_only
def test_run_copy_backup_uses_powershell_for_unc_sources(monkeypatch):
    config = BackupRestoreConfig(
        prod_backup_share=Path(r"\\192.0.2.250\SQLBK"),
        vm_import_unc=Path(r"\\198.51.100.129\SQLBK_IMPORT\ACME-192-0-2-250"),
        vm_import_local=Path(r"C:\MSSQL\SQLBK_IMPORT\ACME-192-0-2-250"),
        vm_log_unc=Path(r"\\198.51.100.129\SQLBK_IMPORT\ACME-192-0-2-250"),
        vm_log_local=Path(r"C:\MSSQL\SQLBK_IMPORT\ACME-192-0-2-250"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
    )
    calls = []
    fake_source = Path(r"\\192.0.2.250\SQLBK\db\FULL\x.bak")
    fake_result = CopyBackupFileResult(
        source_file=fake_source,
        target_file=Path(r"\\198.51.100.129\SQLBK_IMPORT\ACME-192-0-2-250\db\FULL\x.bak"),
        status="COPIED",
        bytes=6,
    )

    monkeypatch.setattr(
        "db_ops.backup_restore.copy_backup.list_recent_backup_files_with_powershell",
        lambda restore_config: calls.append(restore_config.source_id) or [fake_source],
    )
    monkeypatch.setattr(
        "db_ops.backup_restore.copy_backup.copy_backup_file_with_logging",
        lambda *args, **kwargs: fake_result,
    )
    monkeypatch.setattr("db_ops.backup_restore.copy_backup._write_copy_log", lambda *args: None)

    result = run_copy_backup(config)

    assert calls == [config.source_id]
    assert result.files_considered == 1
    assert result.copied == 1


def test_copy_backup_file_with_logging_emits_start_and_done(tmp_path, capsys):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_file = source_root / "db" / "FULL" / "latest.bak"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("backup", encoding="utf-8")

    result = copy_backup_file_with_logging(
        source_file,
        source_root=source_root,
        target_root=target_root,
        source_id="SRC",
        logger=None,
    )

    output = capsys.readouterr().out
    assert result.status == "COPIED"
    assert "copy-backup source_id=SRC file_copy_start" in output
    assert "file_copy_done" in output
    assert "size_bytes=6" in output
    assert "duration_seconds=" in output


def test_copy_backup_file_with_logging_emits_skip(tmp_path, capsys):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_file = source_root / "db" / "FULL" / "latest.bak"
    target_file = target_root / "db" / "FULL" / "latest.bak"
    source_file.parent.mkdir(parents=True)
    target_file.parent.mkdir(parents=True)
    source_file.write_text("backup", encoding="utf-8")
    target_file.write_text("backup", encoding="utf-8")

    result = copy_backup_file_with_logging(
        source_file,
        source_root=source_root,
        target_root=target_root,
        source_id="SRC",
        logger=None,
    )

    output = capsys.readouterr().out
    assert result.status == "SKIPPED_EXISTS"
    assert "copy-backup source_id=SRC file_copy_skip" in output
    assert "reason=already_exists_or_same_size" in output


def test_copy_backup_file_with_logging_emits_failed(tmp_path, monkeypatch, capsys):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_file = source_root / "db" / "FULL" / "latest.bak"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("backup", encoding="utf-8")

    def fail_copy(*_args, **_kwargs):
        raise RuntimeError("copy exploded")

    monkeypatch.setattr("db_ops.backup_restore.copy_backup.copy_backup_file", fail_copy)

    result = copy_backup_file_with_logging(
        source_file,
        source_root=source_root,
        target_root=target_root,
        source_id="SRC",
        logger=None,
    )

    output = capsys.readouterr().out
    assert result.status == "FAILED"
    assert "copy-backup source_id=SRC file_copy_failed" in output
    assert "error=copy exploded" in output


@windows_orchestrator_only
def test_run_copy_backup_logs_scan_and_final_summary(monkeypatch, capsys):
    config = BackupRestoreConfig(
        prod_backup_share=Path(r"\\prod\SQLBK"),
        vm_import_unc=Path(r"\\vm\SQLBK_IMPORT\SRC"),
        vm_import_local=Path(r"C:\SQLBK_IMPORT\SRC"),
        vm_log_unc=Path(r"\\vm\SQLBK_IMPORT\SRC"),
        vm_log_local=Path(r"C:\SQLBK_IMPORT\SRC"),
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        source_id="SRC",
    )
    selected = [Path(r"\\prod\SQLBK\db\FULL\a.bak"), Path(r"\\prod\SQLBK\db\FULL\b.bak")]
    results = [
        CopyBackupFileResult(selected[0], Path(r"\\vm\SQLBK_IMPORT\SRC\db\FULL\a.bak"), "COPIED", 10),
        CopyBackupFileResult(selected[1], Path(r"\\vm\SQLBK_IMPORT\SRC\db\FULL\b.bak"), "SKIPPED_EXISTS", 20),
    ]

    monkeypatch.setattr("db_ops.backup_restore.copy_backup.list_recent_backup_files_with_powershell", lambda *_args, **_kwargs: selected)
    monkeypatch.setattr("db_ops.backup_restore.copy_backup.copy_backup_file_with_logging", lambda source_file, **_kwargs: results[selected.index(source_file)])
    monkeypatch.setattr("db_ops.backup_restore.copy_backup._write_copy_log", lambda *args: None)

    result = run_copy_backup(config)

    output = capsys.readouterr().out
    assert result.files_considered == 2
    assert result.copied == 1
    assert result.skipped == 1
    assert "copy-backup source_id=SRC scan_filter hours=24 patterns=*.bak,*.trn" in output
    assert "copy-backup source_id=SRC scan_completed found_files=2" in output
    assert "copy-backup source_id=SRC completed found_count=2 copied_count=1 skipped_existing_count=1 replaced_count=0 failed_count=0" in output


def test_backup_restore_cli_restore_workflow_uses_restore_workflow_logs(tmp_path, monkeypatch, capsys):
    import db_ops.backup_restore.cli as cli_module

    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    calls = {"patch_stdout": [], "setup_app_logger": []}

    monkeypatch.setattr(cli_module, "load_config", lambda _path: app_config)
    monkeypatch.setattr(cli_module, "load_restore_configs", lambda _path: [make_config(tmp_path)])
    monkeypatch.setattr(cli_module, "emit_backup_restore_event", lambda **_kwargs: None)
    monkeypatch.setattr(cli_module, "log_function_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli_module,
        "run_restore_workflow",
        lambda **_kwargs: {
            "status": "SUCCESS",
            "overall_workflow_status": "SUCCESS",
            "databases_considered": 2,
            "success": 2,
            "failed": 0,
            "skipped": 0,
            "duration_seconds": 52.472,
            "restore-latest": {"sources": [{"results": [{"steps": [{"command": ["sqlcmd"]}]}]}]},
        },
    )
    monkeypatch.setattr(
        cli_module,
        "patch_stdout",
        lambda path, app_name, **_kwargs: calls["patch_stdout"].append((Path(path).name, app_name)),
    )
    monkeypatch.setattr(
        cli_module,
        "setup_app_logger",
        lambda config, app_name, log_scope, **_kwargs: calls["setup_app_logger"].append((app_name, log_scope)) or object(),
    )

    exit_code = cli_module.main(["restore-workflow", "--config", "config.json"])

    assert exit_code == 0
    assert calls["patch_stdout"][0] == ("restore_workflow_runtime.log", "restore_workflow")
    assert calls["setup_app_logger"][0] == ("restore_workflow", "restore_workflow")
    assert calls["patch_stdout"][0][0] != "backup_restore_runtime.log"
    stdout = capsys.readouterr().out.strip()
    assert stdout.startswith("restore-workflow completed status=SUCCESS")
    assert not stdout.startswith("{")
    assert '"results": [' not in stdout
    assert '"steps": [' not in stdout


def test_restore_workflow_without_restore_id_runs_only_active_configs(tmp_path, monkeypatch):
    import db_ops.backup_restore.cli as cli_module

    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    active = dataclasses.replace(make_config(tmp_path), restore_id="RESTORE_ACTIVE", active=True)
    inactive = dataclasses.replace(make_config(tmp_path), restore_id="RESTORE_INACTIVE", active=False)
    workflow_restore_ids = []

    def fake_run_restore_workflow(**kwargs):
        workflow_restore_ids.extend(config.restore_id for config in kwargs["restore_configs"])
        return {
            "status": "SUCCESS",
            "overall_workflow_status": "SUCCESS",
            "restore_mode": "LATEST",
            "restore_count": len(kwargs["restore_configs"]),
            "mappings": [_build_restore_mapping(config) for config in kwargs["restore_configs"]],
            "databases_considered": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "duration_seconds": 1.0,
            "per_restore_results": [],
        }

    monkeypatch.setattr(cli_module, "load_config", lambda _path: app_config)
    monkeypatch.setattr(cli_module, "load_restore_configs", lambda _path: [active, inactive])
    monkeypatch.setattr(cli_module, "emit_backup_restore_event", lambda **_kwargs: None)
    monkeypatch.setattr(cli_module, "log_function_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "patch_stdout", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "setup_app_logger", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_module, "run_restore_workflow", fake_run_restore_workflow)

    assert cli_module.main(["restore-workflow", "--config", "config.json"]) == 0

    assert workflow_restore_ids == ["RESTORE_ACTIVE"]


def test_restore_workflow_with_restore_id_runs_requested_config_even_if_inactive(tmp_path, monkeypatch):
    import db_ops.backup_restore.cli as cli_module

    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    active = dataclasses.replace(make_config(tmp_path), restore_id="RESTORE_ACTIVE", active=True)
    inactive = dataclasses.replace(make_config(tmp_path), restore_id="RESTORE_INACTIVE", active=False)
    workflow_restore_ids = []

    def fake_run_restore_workflow(**kwargs):
        workflow_restore_ids.extend(config.restore_id for config in kwargs["restore_configs"])
        return {
            "status": "SUCCESS",
            "overall_workflow_status": "SUCCESS",
            "restore_mode": "LATEST",
            "restore_count": len(kwargs["restore_configs"]),
            "mappings": [_build_restore_mapping(config) for config in kwargs["restore_configs"]],
            "databases_considered": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "duration_seconds": 1.0,
            "per_restore_results": [],
        }

    monkeypatch.setattr(cli_module, "load_config", lambda _path: app_config)
    monkeypatch.setattr(cli_module, "load_restore_configs", lambda _path: [active, inactive])
    monkeypatch.setattr(cli_module, "emit_backup_restore_event", lambda **_kwargs: None)
    monkeypatch.setattr(cli_module, "log_function_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "patch_stdout", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_module, "setup_app_logger", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_module, "run_restore_workflow", fake_run_restore_workflow)

    assert cli_module.main(["restore-workflow", "--config", "config.json", "--restore-id", "RESTORE_INACTIVE"]) == 0

    assert workflow_restore_ids == ["RESTORE_INACTIVE"]


def test_list_old_target_backup_files_filters_by_target_mtime(tmp_path):
    config = make_config(tmp_path)
    target = config.vm_import_unc
    old_file = target / "APPDB_Prod" / "FULL" / "old.bak"
    recent_file = target / "APPDB_Prod" / "FULL" / "recent.bak"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("old", encoding="utf-8")
    recent_file.write_text("recent", encoding="utf-8")

    import os

    now = 1_800_000_000
    os.utime(old_file, (now - 90_000, now - 90_000))
    os.utime(recent_file, (now - 60, now - 60))

    assert list_old_target_backup_files(config, now=now) == [old_file]


def test_list_old_target_backup_files_prefers_backup_timestamp_in_filename(tmp_path):
    config = dataclasses.replace(make_config(tmp_path), copy_recent_hours=0)
    target = config.vm_import_unc
    old_by_name = target / "APPDB-DB$APPDB" / "SALESDB_Prod" / "FULL" / "APPDB-DB$APPDB_SALESDB_Prod_FULL_20260621_010000.bak"
    old_by_name.parent.mkdir(parents=True)
    old_by_name.write_text("old", encoding="utf-8")

    import os

    now = datetime.datetime(2026, 6, 27, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
    os.utime(old_by_name, (now, now))

    assert list_old_target_backup_files(config, now=now) == [old_by_name]


def test_linux_delete_backup_quotes_paths_with_dollar(tmp_path, monkeypatch):
    import db_ops.backup_restore.delete_backup as delete_module

    config = dataclasses.replace(
        make_config(tmp_path),
        vm_platform="linux",
        vm_credential_target="192.0.2.249",
        vm_import_unc=Path("/opt/mssql2025/backup/SQLBK_IMPORT/ACME-192-0-2-250"),
        copy_recent_hours=48,
    )
    old_ts = datetime.datetime(2026, 6, 21, 1, 0, tzinfo=datetime.timezone.utc).timestamp()
    target = "/opt/mssql2025/backup/SQLBK_IMPORT/ACME-192-0-2-250/APPDB-DB$APPDB/SALESDB_Prod/FULL/APPDB-DB$APPDB_SALESDB_Prod_FULL_20260621_010000.bak"
    # A newer full behind it. On its own a file is the newest full in the directory and the
    # obsolete condition holds it back, so the deletion this test is about would never be reached.
    # The subject here is the quoting of a path containing `$`, not the retention rule.
    newer_ts = datetime.datetime(2026, 6, 26, 1, 0, tzinfo=datetime.timezone.utc).timestamp()
    newer = "/opt/mssql2025/backup/SQLBK_IMPORT/ACME-192-0-2-250/APPDB-DB$APPDB/SALESDB_Prod/FULL/APPDB-DB$APPDB_SALESDB_Prod_FULL_20260626_010000.bak"
    commands = []

    class FakeChannel:
        def recv_exit_status(self):
            return 0

    class FakeStream:
        def __init__(self, text=""):
            self.text = text
            self.channel = FakeChannel()

        def read(self):
            return self.text.encode("utf-8")

    class FakeSsh:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def exec_command(self, command):
            commands.append(command)
            if command.startswith("find "):
                return None, FakeStream(
                    f"{old_ts} 100 {target}\n{newer_ts} 100 {newer}\n"), FakeStream("")
            return None, FakeStream(""), FakeStream("")

    monkeypatch.setattr(delete_module, "open_ssh_connection", lambda _config: FakeSsh())
    now = datetime.datetime(2026, 6, 27, 0, 0, tzinfo=datetime.timezone.utc).timestamp()

    result = delete_old_target_backup_files_via_ssh(config, now=now)

    # The newest full is held back; the older one is what gets the `rm`.
    assert sorted(row.status for row in result) == ["DELETED", "SKIPPED"]
    rm_command = next(command for command in commands if command.startswith("rm "))
    assert "rm -f -- '" in rm_command
    assert "$APPDB" in rm_command


def test_run_delete_backup_deletes_only_old_files_from_target(tmp_path):
    config = make_config(tmp_path)
    old_target = config.vm_import_unc / "APPDB_Prod" / "FULL" / "old.bak"
    recent_target = config.vm_import_unc / "APPDB_Prod" / "FULL" / "recent.bak"
    old_source = config.prod_backup_share / "APPDB_Prod" / "FULL" / "old_source.bak"
    old_target.parent.mkdir(parents=True)
    old_source.parent.mkdir(parents=True)
    old_target.write_text("old", encoding="utf-8")
    recent_target.write_text("recent", encoding="utf-8")
    old_source.write_text("source", encoding="utf-8")

    import os

    now = 1_800_000_000
    for path in [old_target, old_source]:
        os.utime(path, (now - 90_000, now - 90_000))
    os.utime(recent_target, (now - 60, now - 60))

    import db_ops.backup_restore.delete_backup as delete_backup

    original_time = delete_backup.time.time
    delete_backup.time.time = lambda: now
    try:
        result = run_delete_backup(config)
    finally:
        delete_backup.time.time = original_time

    assert result.deleted == 1
    assert not old_target.exists()
    assert recent_target.exists()
    assert old_source.exists()


def test_run_delete_backup_rejects_target_overlapping_source(tmp_path):
    config = make_config(tmp_path)
    unsafe = BackupRestoreConfig(
        prod_backup_share=tmp_path / "same",
        vm_import_unc=tmp_path / "same",
        vm_import_local=config.vm_import_local,
        vm_log_unc=config.vm_log_unc,
        vm_log_local=config.vm_log_local,
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        copy_recent_hours=24,
    )

    with pytest.raises(ValueError, match="overlaps the source"):
        run_delete_backup(unsafe)


def test_run_delete_backup_zero_hours_deletes_all_target_files(tmp_path):
    config = make_config(tmp_path)
    delete_all_config = BackupRestoreConfig(
        prod_backup_share=config.prod_backup_share,
        vm_import_unc=config.vm_import_unc,
        vm_import_local=config.vm_import_local,
        vm_log_unc=config.vm_log_unc,
        vm_log_local=config.vm_log_local,
        prod_smb_credential_target="",
        prod_smb_username="",
        prod_smb_password_env="",
        vm_credential_target="",
        vm_username="",
        vm_password_env="",
        restore_sql_instance_on_vm="localhost",
        copy_recent_hours=0,
    )
    old_target = delete_all_config.vm_import_unc / "APPDB_Prod" / "FULL" / "old.bak"
    recent_target = delete_all_config.vm_import_unc / "APPDB_Prod" / "FULL" / "recent.bak"
    old_target.parent.mkdir(parents=True)
    old_target.write_text("old", encoding="utf-8")
    recent_target.write_text("recent", encoding="utf-8")

    result = run_delete_backup(delete_all_config)

    # copy_recent_hours=0 opens the age gate on everything; the obsolete condition still holds the
    # newest full back, because that is what the next restore starts from. Before that condition
    # existed this deleted both files and left the staging directory with nothing to restore from.
    assert result.deleted == 1
    assert result.skipped == 1
    assert not old_target.exists()
    assert recent_target.exists()


def test_build_checkdb_sql_targets_restore_database(tmp_path):
    config = make_config(tmp_path)

    assert build_checkdb_sql(config) == "DBCC CHECKDB ([APPDB_Prod_DR]) WITH NO_INFOMSGS;"


def test_emit_backup_restore_event_writes_sqlite_and_pushes_telegram(tmp_path, monkeypatch):
    # Routing is the Telegram app's answer, fetched through its CLI, so stub this app's client
    # rather than app_config's TelegramConfig groups.
    monkeypatch.setattr(
        "db_ops.lib.telegram_route.telegram_route",
        lambda level, **_: {"enabled": True, "alert": bool(_ROUTES.get(level)),
                            "chat_id": _ROUTES.get(level, "")},
    )

    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
        telegram=TelegramConfig(
            enabled=True,
            bot_token="token",
            level_chat_map={"logging": "group-log", "critical": "group-critical"},
        ),
    )
    emit_backup_restore_event(
        app_config=app_config,
        command="copy-backup",
        phase="START",
        level="logging",
        message="started sources=1",
        metadata={"source_ids": ["SRC1"]},
    )

    store = DbOpsStore(app_config.sqlite_path)
    rows = store.fetch_recent_job_runs(limit=1)
    send_messages = store.fetch_pending_telegram_send_messages(limit=10)

    assert rows[0]["job_code"] == "backup_restore.copy-backup.start"
    assert rows[0]["level"] == "logging"
    assert rows[0]["status"] == "START"
    assert send_messages[0]["tlgchat_id"] == "group-log"
    assert "backup_restore.copy-backup START" in send_messages[0]["message_text"]


def test_emit_backup_restore_event_swallows_telegram_failure(tmp_path, monkeypatch):
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
        telegram=TelegramConfig(
            enabled=True,
            bot_token="token",
            level_chat_map={"critical": "group-critical"},
        ),
    )

    emit_backup_restore_event(
        app_config=app_config,
        command="restore-latest",
        phase="ERROR",
        level="critical",
        message="failed",
        error_text="boom",
    )

    store = DbOpsStore(app_config.sqlite_path)
    rows = store.fetch_recent_job_runs(limit=1)
    with store.connect() as conn:
        row = conn.execute("SELECT error_text FROM job_runs WHERE log_id = ?;", (rows[0]["log_id"],)).fetchone()

    assert rows[0]["job_code"] == "backup_restore.restore-latest.error"
    assert rows[0]["level"] == "critical"
    assert row["error_text"] == "boom"


# ── PITR tests ────────────────────────────────────────────────────────────────

def test_parse_point_in_time_converts_to_utc():
    dt = parse_point_in_time("2026-05-30 18:00:00 +07:00")
    assert dt.tzinfo == datetime.timezone.utc
    assert dt.year == 2026 and dt.month == 5 and dt.day == 30
    assert dt.hour == 11 and dt.minute == 0 and dt.second == 0


def test_parse_point_in_time_june_8_plus_7_converts_to_utc():
    dt = parse_point_in_time("2026-06-08 12:00:00 +07:00")
    assert dt == datetime.datetime(2026, 6, 8, 5, 0, 0, tzinfo=datetime.timezone.utc)


def test_parse_point_in_time_accepts_iso_separator():
    dt = parse_point_in_time("2026-05-30T18:00:00+07:00")
    assert dt == parse_point_in_time("2026-05-30 18:00:00 +07:00")


def test_parse_point_in_time_rejects_missing_timezone():
    with pytest.raises(ValueError, match="timezone offset"):
        parse_point_in_time("2026-05-30 18:00:00")


def test_parse_point_in_time_rejects_invalid_format():
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_point_in_time("not-a-date")


def test_parse_point_in_time_utc_offset_zero():
    dt = parse_point_in_time("2026-05-30 11:00:00 +00:00")
    assert dt.hour == 11 and dt.utcoffset().total_seconds() == 0


def test_build_restore_log_stopat_sql_contains_stopat(tmp_path):
    config = make_config(tmp_path)
    full_dir = tmp_path / "vm_import_unc" / "APPDB_Prod" / "FULL"
    full_dir.mkdir(parents=True)
    bak = full_dir / "APPDB_Prod.bak"
    bak.touch()
    candidate = build_restore_candidate(bak, config)
    stopat = datetime.datetime(2026, 5, 30, 11, 0, 0, tzinfo=datetime.timezone.utc)
    sql = build_restore_log_stopat_sql(Path(r"E:\SQLBK_IMPORT\APPDB_Prod\LOG\log1.trn"), candidate, stopat)
    assert "STOPAT" in sql
    assert "2026-05-30T11:00:00" in sql
    assert "RESTORE LOG" in sql
    assert "WITH RECOVERY" in sql


def test_pitr_full_backup_selection_uses_latest_full_before_point_in_time(tmp_path):
    import os

    config = dataclasses.replace(make_config(tmp_path), copy_recent_hours=0)
    full_dir = config.vm_import_unc / "APPDB_Prod" / "FULL"
    full_dir.mkdir(parents=True)
    old_full = full_dir / "APPDB_Prod_FULL_20260608_010000.bak"
    newer_full = full_dir / "APPDB_Prod_FULL_20260609_010000.bak"
    for path in [old_full, newer_full]:
        path.write_text(path.name, encoding="utf-8")
    os.utime(old_full, (datetime.datetime(2026, 6, 8, 1, 0, tzinfo=datetime.timezone.utc).timestamp(),) * 2)
    os.utime(newer_full, (datetime.datetime(2026, 6, 9, 1, 0, tzinfo=datetime.timezone.utc).timestamp(),) * 2)

    pit = parse_point_in_time("2026-06-08 12:00:00 +07:00")

    assert get_latest_full_backup_for_database(config, DatabaseRestoreMapping("APPDB_Prod", "APPDB_Prod_DR")) == newer_full
    assert get_full_backup_for_pitr(config, DatabaseRestoreMapping("APPDB_Prod", "APPDB_Prod_DR"), pit) == old_full


def test_pitr_full_backup_selection_fails_when_no_full_before_point_in_time(tmp_path):
    import os

    config = make_config(tmp_path)
    full_dir = config.vm_import_unc / "APPDB_Prod" / "FULL"
    full_dir.mkdir(parents=True)
    newer_full = full_dir / "APPDB_Prod_FULL_20260609_010000.bak"
    newer_full.write_text("newer", encoding="utf-8")
    os.utime(newer_full, (datetime.datetime(2026, 6, 9, 1, 0, tzinfo=datetime.timezone.utc).timestamp(),) * 2)

    pit = parse_point_in_time("2026-06-08 12:00:00 +07:00")

    with pytest.raises(FileNotFoundError, match="No FULL backup found for PITR"):
        get_full_backup_for_pitr(config, DatabaseRestoreMapping("APPDB_Prod", "APPDB_Prod_DR"), pit)


def test_windows_latest_restore_chain_selects_full_diff_and_logs(tmp_path):
    import os

    config = make_config(tmp_path)
    db_dir = config.vm_import_unc / "APPDB_Prod"
    full = db_dir / "FULL" / "full.bak"
    diff = db_dir / "DIFF" / "diff.bak"
    logs = [db_dir / "LOG" / "log_001.trn", db_dir / "LOG" / "log_002.trn"]
    for path in [full, diff, *logs]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    for path, mtime in zip([full, diff, *logs], [100.0, 200.0, 300.0, 400.0]):
        os.utime(path, (mtime, mtime))

    candidate = build_restore_candidate(full, config)
    selected_diff = find_restore_diff_backup(config, candidate)

    assert selected_diff == diff
    assert find_restore_log_backups(config, candidate, selected_diff) == logs


def test_windows_latest_restore_chain_selects_logs_without_diff(tmp_path):
    import os

    config = make_config(tmp_path)
    db_dir = config.vm_import_unc / "APPDB_Prod"
    full = db_dir / "FULL" / "full.bak"
    logs = [db_dir / "LOG" / "log_001.trn", db_dir / "LOG" / "log_002.trn"]
    for path in [full, *logs]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    for path, mtime in zip([full, *logs], [100.0, 200.0, 300.0]):
        os.utime(path, (mtime, mtime))

    candidate = build_restore_candidate(full, config)

    assert find_restore_diff_backup(config, candidate) is None
    assert find_restore_log_backups(config, candidate) == logs


def test_linux_latest_restore_chain_discovers_remote_diff_and_logs(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    config = dataclasses.replace(
        make_config(tmp_path),
        vm_platform="linux",
        vm_import_unc=Path("/var/opt/mssql/backup/import"),
        vm_import_local=Path("/var/opt/mssql/backup/import"),
        restore_data_dir_on_vm=Path("/var/opt/mssql/data"),
    )
    full = Path("/var/opt/mssql/backup/import/APPDB_Prod/FULL/full.bak")
    diff = Path("/var/opt/mssql/backup/import/APPDB_Prod/DIFF/diff.bak")
    logs = [
        Path("/var/opt/mssql/backup/import/APPDB_Prod/LOG/log_001.trn"),
        Path("/var/opt/mssql/backup/import/APPDB_Prod/LOG/log_002.trn"),
    ]
    remote_files = {
        str(full): 100.0,
        str(diff): 200.0,
        str(logs[0]): 300.0,
        str(logs[1]): 400.0,
    }

    monkeypatch.setattr(
        restore_module,
        "_linux_file_mtime",
        lambda _config, path: remote_files.get(str(path)),
    )
    monkeypatch.setattr(
        restore_module,
        "_find_linux_files_with_mtime",
        lambda _config, directory, pattern: [
            (mtime, path)
            for path, mtime in remote_files.items()
            if str(Path(path).parent) == str(directory) and Path(path).match(pattern)
        ],
    )

    candidate = build_restore_candidate(full, config)
    selected_diff = find_restore_diff_backup(config, candidate)
    selected_logs = find_restore_log_backups(config, candidate, selected_diff)

    assert candidate.backup_file_on_vm == full
    assert selected_diff == diff
    assert selected_logs == logs


def test_restore_workflow_cli_accepts_point_in_time():
    args = parse_args(["restore-workflow", "--config", "config.json", "--point-in-time", "2026-05-30 18:00:00 +07:00"])
    assert args.point_in_time == "2026-05-30 18:00:00 +07:00"


def test_restore_workflow_cli_point_in_time_defaults_to_none():
    args = parse_args(["restore-workflow", "--config", "config.json"])
    assert args.point_in_time is None


def test_find_restore_log_backups_for_pitr_selects_correct_chain(tmp_path):
    config = make_config(tmp_path)
    full_dir = tmp_path / "vm_import_unc" / "APPDB_Prod" / "FULL"
    log_dir = tmp_path / "vm_import_unc" / "APPDB_Prod" / "LOG"
    full_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    bak = full_dir / "APPDB_Prod.bak"
    bak.touch()

    # Create log files with known mtimes (epoch seconds)
    import os
    base_ts = 1748649600.0  # 2025-05-30 12:00:00 UTC
    log_files = []
    for i, offset in enumerate([3600, 7200, 10800, 14400]):  # +1h, +2h, +3h, +4h after base
        f = log_dir / f"log_{i:03d}.trn"
        f.touch()
        mtime = base_ts + offset
        os.utime(f, (mtime, mtime))
        log_files.append((mtime, f))

    # Set full backup mtime to base_ts (so all logs are after it)
    os.utime(bak, (base_ts, base_ts))

    candidate = build_restore_candidate(bak, config)

    # Point in time = base + 9000s = midway through log[1] (+7200) and log[2] (+10800)
    # So we expect: log[0], log[1], log[2] (log[2] has mtime > pit_ts and spans it)
    pit_utc = datetime.datetime.fromtimestamp(base_ts + 9000, tz=datetime.timezone.utc)
    selected = find_restore_log_backups_for_pitr(config, candidate, None, pit_utc)

    assert len(selected) == 3
    assert selected[0] == log_files[0][1]
    assert selected[1] == log_files[1][1]
    assert selected[2] == log_files[2][1]


def test_pitr_restore_plan_selects_old_full_diff_and_logs(tmp_path):
    import os

    config = make_config(tmp_path)
    db_dir = config.vm_import_unc / "APPDB_Prod"
    old_full = db_dir / "FULL" / "old_full.bak"
    newer_full = db_dir / "FULL" / "newer_full.bak"
    diff = db_dir / "DIFF" / "diff.bak"
    log1 = db_dir / "LOG" / "log_before_pit.trn"
    log2 = db_dir / "LOG" / "log_after_pit.trn"
    for path in [old_full, newer_full, diff, log1, log2]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
    for path, stamp in [
        (old_full, "2026-06-08T01:00:00+00:00"),
        (newer_full, "2026-06-09T01:00:00+00:00"),
        (diff, "2026-06-08T04:00:00+00:00"),
        (log1, "2026-06-08T04:30:00+00:00"),
        (log2, "2026-06-08T06:00:00+00:00"),
    ]:
        ts = datetime.datetime.fromisoformat(stamp).timestamp()
        os.utime(path, (ts, ts))

    pit = parse_point_in_time("2026-06-08 12:00:00 +07:00")
    database = DatabaseRestoreMapping("APPDB_Prod", "APPDB_Prod_DR")
    selected_full = get_full_backup_for_pitr(config, database, pit)
    candidate = build_restore_candidate(selected_full, config, database=database)
    selected_diff = find_restore_diff_backup_for_pitr(config, candidate, pit)
    selected_logs = find_restore_log_backups_for_pitr(config, candidate, selected_diff, pit)

    assert selected_full == old_full
    assert selected_diff == diff
    assert selected_logs == [log1, log2]


def test_pitr_diff_and_log_candidates_before_selected_full_are_ignored(tmp_path):
    import os

    config = make_config(tmp_path)
    db_dir = config.vm_import_unc / "APPDB_Prod"
    full = db_dir / "FULL" / "full.bak"
    old_diff = db_dir / "DIFF" / "old_diff.bak"
    new_diff = db_dir / "DIFF" / "new_diff.bak"
    old_log = db_dir / "LOG" / "old_log.trn"
    log1 = db_dir / "LOG" / "log_001.trn"
    log2 = db_dir / "LOG" / "log_002.trn"
    for path in [full, old_diff, new_diff, old_log, log1, log2]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
    times = {
        full: "2026-06-08T01:00:00+00:00",
        old_diff: "2026-06-08T00:30:00+00:00",
        new_diff: "2026-06-08T02:00:00+00:00",
        old_log: "2026-06-08T00:45:00+00:00",
        log1: "2026-06-08T02:30:00+00:00",
        log2: "2026-06-08T06:00:00+00:00",
    }
    for path, stamp in times.items():
        ts = datetime.datetime.fromisoformat(stamp).timestamp()
        os.utime(path, (ts, ts))

    pit = datetime.datetime(2026, 6, 8, 5, 0, tzinfo=datetime.timezone.utc)
    candidate = build_restore_candidate(full, config)
    selected_diff = find_restore_diff_backup_for_pitr(config, candidate, pit)
    selected_logs = find_restore_log_backups_for_pitr(config, candidate, selected_diff, pit)

    assert selected_diff == new_diff
    assert old_diff != selected_diff
    assert selected_logs == [log1, log2]
    assert old_log not in selected_logs


def test_pitr_full_diff_log_selection_uses_logs_after_restored_diff(tmp_path):
    import os

    config = make_config(tmp_path)
    db_dir = config.vm_import_unc / "APPDB_Prod"
    full = db_dir / "FULL" / "full.bak"
    diff = db_dir / "DIFF" / "diff.bak"
    log_before_diff = db_dir / "LOG" / "before_diff.trn"
    log_after_diff = db_dir / "LOG" / "after_diff.trn"
    log_covering_pit = db_dir / "LOG" / "covering_pit.trn"
    for path in [full, diff, log_before_diff, log_after_diff, log_covering_pit]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
    times = {
        full: "2026-06-08T01:00:00+00:00",
        log_before_diff: "2026-06-08T01:30:00+00:00",
        diff: "2026-06-08T03:00:00+00:00",
        log_after_diff: "2026-06-08T03:30:00+00:00",
        log_covering_pit: "2026-06-08T06:00:00+00:00",
    }
    for path, stamp in times.items():
        ts = datetime.datetime.fromisoformat(stamp).timestamp()
        os.utime(path, (ts, ts))

    pit = datetime.datetime(2026, 6, 8, 5, 0, tzinfo=datetime.timezone.utc)
    candidate = build_restore_candidate(full, config)
    selected_diff = find_restore_diff_backup_for_pitr(config, candidate, pit)
    selected_logs = find_restore_log_backups_for_pitr(config, candidate, selected_diff, pit)

    assert selected_diff == diff
    assert selected_logs == [log_after_diff, log_covering_pit]


def test_find_restore_log_backups_for_pitr_raises_when_target_beyond_chain(tmp_path):
    config = make_config(tmp_path)
    full_dir = tmp_path / "vm_import_unc" / "APPDB_Prod" / "FULL"
    log_dir = tmp_path / "vm_import_unc" / "APPDB_Prod" / "LOG"
    full_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    bak = full_dir / "APPDB_Prod.bak"
    bak.touch()

    import os
    base_ts = 1748649600.0
    log_file = log_dir / "log_000.trn"
    log_file.touch()
    os.utime(log_file, (base_ts + 3600, base_ts + 3600))
    os.utime(bak, (base_ts, base_ts))

    candidate = build_restore_candidate(bak, config)
    # Point in time far in the future — no log covers it
    pit_utc = datetime.datetime.fromtimestamp(base_ts + 99999, tz=datetime.timezone.utc)

    with pytest.raises(ValueError, match="beyond the available log chain"):
        find_restore_log_backups_for_pitr(config, candidate, None, pit_utc)


def test_find_restore_log_backups_for_pitr_stops_at_exact_boundary(tmp_path):
    # Regression: when the target lands exactly on a log's backup timestamp, that log is the last
    # one selected. Previously the ">" rule kept going and pulled in the next log -- which, when the
    # chain has a gap (a stray far-future backup left in the LOG dir), is non-contiguous and gets
    # rejected by SQL Server with Msg 4305, leaving the DB stuck in RESTORING.
    import os

    config = make_config(tmp_path)
    full_dir = tmp_path / "vm_import_unc" / "APPDB_Prod" / "FULL"
    log_dir = tmp_path / "vm_import_unc" / "APPDB_Prod" / "LOG"
    full_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    bak = full_dir / "APPDB_Prod.bak"
    bak.touch()

    base_ts = 1748649600.0  # 2025-05-30 12:00:00 UTC
    # Contiguous logs at +1h, +2h, +3h, then a stray backup two weeks later.
    offsets = [3600, 7200, 10800, 3600 + 14 * 86400]
    log_files = []
    for i, offset in enumerate(offsets):
        f = log_dir / f"log_{i:03d}.trn"
        f.touch()
        mtime = base_ts + offset
        os.utime(f, (mtime, mtime))
        log_files.append((mtime, f))
    os.utime(bak, (base_ts, base_ts))

    candidate = build_restore_candidate(bak, config)
    # Target lands exactly on log[2]'s timestamp (+3h).
    pit_utc = datetime.datetime.fromtimestamp(base_ts + 10800, tz=datetime.timezone.utc)
    selected = find_restore_log_backups_for_pitr(config, candidate, None, pit_utc)

    assert selected == [log_files[0][1], log_files[1][1], log_files[2][1]]
    assert log_files[3][1] not in selected  # the stray far-future log must not be pulled in


def test_build_recovery_if_restoring_sql_only_recovers_when_restoring():
    candidate = build_restore_candidate(
        Path("/vm/vm_import_unc/APPDB_Prod/FULL/APPDB_Prod.bak"),
        make_config(Path("/vm")),
    )
    sql = build_recovery_if_restoring_sql(candidate)
    assert "DATABASEPROPERTYEX(N'APPDB_Prod_DR', N'Status') = N'RESTORING'" in sql
    assert "RESTORE DATABASE [APPDB_Prod_DR] WITH RECOVERY;" in sql
    assert "ALTER DATABASE [APPDB_Prod_DR] SET MULTI_USER;" in sql


def test_restore_workflow_pitr_passes_point_in_time_to_restore(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    captured = {}

    def fake_copy(step_config, logger=None, force=False):
        captured["copy_window_start_utc"] = step_config.copy_window_start_utc
        captured["copy_window_end_utc"] = step_config.copy_window_end_utc
        return CopyBackupResult(returncode=0, source_backup_dir=tmp_path, local_import_dir=tmp_path, files_considered=1, copied=1, skipped=0, file_results=())

    def fake_restore(config, db_ops_config, dry_run, logger, point_in_time_utc):
        captured["point_in_time_utc"] = point_in_time_utc
        return {"status": "SUCCESS", "overall_status": "SUCCESS", "databases_considered": 0, "per_database_restore_status": {}}

    def fake_delete(step_config, logger=None):
        return DeleteBackupResult(returncode=0, target_backup_dir=tmp_path, delete_older_than_hours=48, files_considered=0, deleted=0, file_results=())

    monkeypatch.setattr("db_ops.backup_restore.cli.run_copy_backup", fake_copy)
    monkeypatch.setattr("db_ops.backup_restore.cli.run_restore_all_latest", fake_restore)
    monkeypatch.setattr("db_ops.backup_restore.cli.run_delete_backup", fake_delete)
    monkeypatch.setattr("db_ops.backup_restore.cli.run_target_preflight", lambda config, logger=None: None)

    pit = parse_point_in_time("2026-05-30 18:00:00 +07:00")
    run_restore_workflow(
        restore_configs=[config],
        app_config=app_config,
        point_in_time_utc=pit,
    )

    assert captured["point_in_time_utc"] == pit
    assert captured["copy_window_start_utc"] == pit - datetime.timedelta(hours=24)
    assert captured["copy_window_end_utc"] == pit


def test_restore_workflow_pitr_no_matching_copy_files_fails_fast(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )

    def fake_copy(step_config, logger=None, force=False):
        return CopyBackupResult(returncode=0, source_backup_dir=tmp_path, local_import_dir=tmp_path, files_considered=0, copied=0, skipped=0, file_results=())

    monkeypatch.setattr("db_ops.backup_restore.cli.run_copy_backup", fake_copy)
    monkeypatch.setattr("db_ops.backup_restore.cli.run_target_preflight", lambda config, logger=None: None)

    pit = parse_point_in_time("2026-05-30 18:00:00 +07:00")
    with pytest.raises(RuntimeError, match="copy-backup selected no files"):
        run_restore_workflow(
            restore_configs=[config],
            app_config=app_config,
            point_in_time_utc=pit,
        )


def test_restore_workflow_emits_phase_progress_logs(tmp_path, monkeypatch):
    import db_ops.backup_restore.cli as cli_module

    config = make_config(tmp_path)
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    messages = []

    monkeypatch.setattr(cli_module, "log_event", lambda _logger, level, message: messages.append(message))
    monkeypatch.setattr(cli_module, "run_target_preflight", lambda config, logger=None: None)
    monkeypatch.setattr(
        cli_module,
        "run_copy_backup",
        lambda config, logger=None, force=False: CopyBackupResult(
            returncode=0,
            source_backup_dir=config.prod_backup_share,
            local_import_dir=config.vm_import_unc,
            files_considered=1,
            copied=1,
            skipped=0,
            file_results=(),
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "run_restore_all_latest",
        lambda **_kwargs: {"status": "SUCCESS", "overall_status": "SUCCESS", "per_database_restore_status": {"APP": "SUCCESS"}},
    )
    monkeypatch.setattr(
        cli_module,
        "run_delete_backup",
        lambda config, logger=None: DeleteBackupResult(
            returncode=0,
            target_backup_dir=config.vm_import_unc,
            delete_older_than_hours=config.copy_recent_hours,
            files_considered=1,
            deleted=1,
            file_results=(),
        ),
    )

    run_restore_workflow(restore_configs=[config], app_config=app_config, logger=object())

    joined = "\n".join(messages)
    assert "restore-workflow calculating-copy-window start" in joined
    assert "restore-workflow copy-backup start" in joined
    assert "restore-workflow restore-preparation start" in joined
    assert "restore-workflow restore-execution start" in joined
    assert "restore-workflow delete-backup start" in joined
    assert "elapsed_seconds=" in joined


# ── Telegram restore-workflow message format tests ─────────────────────────────

def test_format_restore_workflow_telegram_message_latest_start():
    metadata = {
        "app": "backup_restore",
        "command": "restore-workflow",
        "phase": "START",
        "restore_id": "ACME_TO_SQLSERVER_198_51_100_31",
        "target_id": "SQLSERVER-VM-TEST-198-51-100-31",
        "target_host": "198.51.100.31",
        "restore_mode": "LATEST",
    }
    text = _format_restore_workflow_telegram_message(level="logging", message="ignored", metadata=metadata)
    assert "Restore workflow started." in text
    assert "restore_id=ACME_TO_SQLSERVER_198_51_100_31" in text
    assert "target=SQLSERVER-VM-TEST-198-51-100-31 / 198.51.100.31" in text
    assert "restore_mode=LATEST" in text
    assert "point_in_time" not in text


def test_a_scheduled_run_reports_its_status_instead_of_a_bare_status_field():
    """Every "Restore workflow finished." message the scheduler ever sent ended in `status=`.

    The formatter looks for output.status and then status; the scheduler's END metadata carried
    neither — only ``output_status``, which nothing reads. Both restore engines now publish
    ``status``, so the field says what happened.
    """
    metadata = {
        "app": "backup_restore", "command": "restore-workflow", "phase": "END",
        "restore_id": "CLOUD_MSSQL_TO_CLOUD2", "restore_mode": "LATEST",
        "status": "done", "output_status": "SUCCESS",
    }
    text = _format_restore_workflow_telegram_message(level="logging", message="ignored", metadata=metadata)

    assert "Restore workflow finished. status=done" in text


def test_a_script_driven_restore_names_its_target_like_an_engine_restore_does():
    """Two SQL Server restores, two engines, one message format.

    Driven from the metadata the scheduler really builds, not from a hand-written dict: the
    formatter reads ``target_id``/``target_host`` and the script branch published only
    ``server_id``/``target_container``, so a test of the formatter alone would have passed while
    the message that ships still had no target in it.
    """
    from db_ops.backup_restore.restore_script import ScriptRestore
    from db_ops.backup_restore.workflow import script_restore_metadata
    from db_ops.lib.time_window import TimeWindow

    job = ScriptRestore(
        restore_id="CLOUD_MSSQL_TO_CLOUD2", db_type="sqlserver",
        server_id="CLOUD-203-0-113-188-MSSQL-1433", backup_dir="/var/opt/mssql/backup/dbops",
        script="assets/restore/sqlserver/mssql_restore.sh", time_window=TimeWindow(),
        target_container="mssql_ha_cloud2-primary",
        target_server_id="CLOUD2-203-0-113-121-HOST",
    )
    metadata = {**script_restore_metadata(job), "command": "restore-workflow", "phase": "START"}

    text = _format_restore_workflow_telegram_message(level="logging", message="ignored", metadata=metadata)

    assert "target=CLOUD2-203-0-113-121-HOST / mssql_ha_cloud2-primary" in text
    assert "restore_id=CLOUD_MSSQL_TO_CLOUD2" in text


def test_an_in_place_drill_names_the_source_host_as_its_target():
    """No target_server_id means the restore happens on the source's own host. Leaving the
    target blank there would read as "we do not know where this restored to"."""
    from db_ops.backup_restore.restore_script import ScriptRestore
    from db_ops.backup_restore.workflow import script_restore_metadata
    from db_ops.lib.time_window import TimeWindow

    job = ScriptRestore(
        restore_id="CLOUD_PG_RESTORE_DRILL", db_type="postgresql",
        server_id="CLOUD-203-0-113-188-PG", backup_dir="/backup",
        script="x.sh", time_window=TimeWindow(), target_container="pg_ha-primary",
    )

    assert script_restore_metadata(job)["target_id"] == "CLOUD-203-0-113-188-PG"


def test_format_restore_workflow_telegram_message_pitr_start():
    metadata = {
        "app": "backup_restore",
        "command": "restore-workflow",
        "phase": "START",
        "restore_id": "ACME_TO_SQLSERVER_198_51_100_31",
        "target_id": "SQLSERVER-VM-TEST-198-51-100-31",
        "target_host": "198.51.100.31",
        "restore_mode": "POINT_IN_TIME",
        "point_in_time_original": "2026-05-30 18:00:00 +07:00",
        "point_in_time_utc": "2026-05-30T11:00:00+00:00",
    }
    text = _format_restore_workflow_telegram_message(level="logging", message="ignored", metadata=metadata)
    assert "Restore workflow started." in text
    assert "restore_id=ACME_TO_SQLSERVER_198_51_100_31" in text
    assert "restore_mode=POINT_IN_TIME" in text
    assert "point_in_time=2026-05-30 18:00:00 +07:00" in text
    assert "point_in_time_utc=2026-05-30T11:00:00+00:00" in text


def test_format_restore_workflow_telegram_message_end_latest():
    metadata = {
        "app": "backup_restore",
        "command": "restore-workflow",
        "phase": "END",
        "restore_id": "ACME_TO_SQLSERVER_198_51_100_31",
        "target_id": "SQLSERVER-VM-TEST-198-51-100-31",
        "target_host": "198.51.100.31",
        "restore_mode": "LATEST",
        "output": {"status": "SUCCESS"},
    }
    text = _format_restore_workflow_telegram_message(level="logging", message="ignored", metadata=metadata)
    assert "Restore workflow finished." in text
    assert "status=SUCCESS" in text
    assert "restore_mode=LATEST" in text
    assert "point_in_time" not in text


def test_format_restore_workflow_telegram_message_error():
    metadata = {
        "app": "backup_restore",
        "command": "restore-workflow",
        "phase": "ERROR",
        "restore_id": "ACME_TO_SQLSERVER_198_51_100_31",
        "target_id": "SQLSERVER-VM-TEST-198-51-100-31",
        "target_host": "198.51.100.31",
        "restore_mode": "LATEST",
        "current_phase": "copy-backup",
        "exception_type": "RuntimeError",
        "exception_message": "No decryption key provided.",
        "stdout_tail": "copy-backup exit_early reason=No decryption key provided.",
        "stderr_tail": "ERROR: No decryption key provided.",
        "log_file_path": "logs/restore_workflow_runtime.log",
    }
    text = _format_restore_workflow_telegram_message(level="critical", message="failed: boom", metadata=metadata)
    assert "Restore workflow FAILED." in text
    assert "restore_mode=LATEST" in text
    assert "current_phase=copy-backup" in text
    assert "exception_type=RuntimeError" in text
    assert "exception_message=No decryption key provided." in text
    assert "stdout_tail=copy-backup exit_early reason=No decryption key provided." in text
    assert "stderr_tail=ERROR: No decryption key provided." in text
    assert "log_file_path=logs/restore_workflow_runtime.log" in text


def test_format_telegram_message_dispatches_restore_workflow():
    metadata = {
        "app": "backup_restore",
        "command": "restore-workflow",
        "phase": "START",
        "restore_id": "MY_RESTORE",
        "target_id": "TGT",
        "target_host": "10.0.0.1",
        "restore_mode": "LATEST",
    }
    text = _format_telegram_message(level="logging", message="any", metadata=metadata)
    assert "Restore workflow started." in text
    assert "restore_mode=LATEST" in text


def test_format_telegram_message_non_restore_workflow_uses_json():
    metadata = {
        "app": "backup_restore",
        "command": "copy-backup",
        "phase": "START",
        "source_ids": ["SRC1"],
    }
    text = _format_telegram_message(level="logging", message="started sources=1", metadata=metadata)
    assert "copy-backup" in text
    assert "SRC1" in text
    # Non-restore-workflow still uses the JSON payload format
    assert "{" in text


# ── CLI event metadata tests ───────────────────────────────────────────────────

def _make_cli_restore_workflow_calls(tmp_path, monkeypatch, extra_argv=None):
    import db_ops.backup_restore.cli as cli_module
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    captured_events = []

    cfg = dataclasses.replace(
        make_config(tmp_path),
        restore_id="ACME_TO_SQLSERVER_TEST",
        source_id="SRC",
        target_id="TGT-VM",
        vm_credential_target="10.0.0.1",
    )

    def fake_emit(**kwargs):
        captured_events.append(kwargs)

    monkeypatch.setattr(cli_module, "load_config", lambda _: app_config)
    monkeypatch.setattr(cli_module, "load_restore_configs", lambda _: [cfg])
    monkeypatch.setattr(cli_module, "emit_backup_restore_event", fake_emit)
    monkeypatch.setattr(cli_module, "log_function_call", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "patch_stdout", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "setup_app_logger", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_module,
        "run_restore_workflow",
        lambda **_: {
            "status": "SUCCESS",
            "overall_workflow_status": "SUCCESS",
            "restore_mode": "LATEST",
            "databases_considered": 1,
            "success": 1,
            "failed": 0,
            "skipped": 0,
            "duration_seconds": 10.0,
        },
    )

    argv = ["restore-workflow", "--config", "config.json"] + (extra_argv or [])
    cli_module.main(argv)
    return captured_events


def test_cli_restore_workflow_start_event_has_restore_mode_latest(tmp_path, monkeypatch):
    events = _make_cli_restore_workflow_calls(tmp_path, monkeypatch)
    start_event = next(e for e in events if e.get("phase") == "START")
    meta = start_event["metadata"]
    assert meta["restore_mode"] == "LATEST"
    assert "point_in_time_utc" not in meta
    assert meta["restore_id"] == "ACME_TO_SQLSERVER_TEST"
    assert meta["target_id"] == "TGT-VM"
    assert meta["target_host"] == "10.0.0.1"


def test_cli_restore_workflow_end_event_has_restore_mode_and_target(tmp_path, monkeypatch):
    events = _make_cli_restore_workflow_calls(tmp_path, monkeypatch)
    end_event = next(e for e in events if e.get("phase") == "END")
    meta = end_event["metadata"]
    assert meta["restore_mode"] == "LATEST"
    assert meta["restore_id"] == "ACME_TO_SQLSERVER_TEST"
    assert meta["target_id"] == "TGT-VM"
    assert meta["target_host"] == "10.0.0.1"


def test_cli_restore_workflow_start_event_pitr_has_pit_fields(tmp_path, monkeypatch):
    import db_ops.backup_restore.cli as cli_module
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    captured_events = []

    cfg = dataclasses.replace(
        make_config(tmp_path),
        restore_id="ACME_TO_SQLSERVER_TEST",
        source_id="SRC",
        target_id="TGT-VM",
        vm_credential_target="10.0.0.1",
    )

    def fake_emit(**kwargs):
        captured_events.append(kwargs)

    monkeypatch.setattr(cli_module, "load_config", lambda _: app_config)
    monkeypatch.setattr(cli_module, "load_restore_configs", lambda _: [cfg])
    monkeypatch.setattr(cli_module, "emit_backup_restore_event", fake_emit)
    monkeypatch.setattr(cli_module, "log_function_call", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "patch_stdout", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "setup_app_logger", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_module,
        "run_restore_workflow",
        lambda **kw: {
            "status": "SUCCESS",
            "overall_workflow_status": "SUCCESS",
            "restore_mode": "POINT_IN_TIME",
            "point_in_time_utc": kw.get("point_in_time_utc", datetime.datetime(2026, 5, 30, 11, 0, 0, tzinfo=datetime.timezone.utc)).isoformat(),
            "databases_considered": 1,
            "success": 1,
            "failed": 0,
            "skipped": 0,
            "duration_seconds": 10.0,
        },
    )

    cli_module.main(["restore-workflow", "--config", "config.json", "--point-in-time", "2026-05-30 18:00:00 +07:00"])

    start_event = next(e for e in captured_events if e.get("phase") == "START")
    meta = start_event["metadata"]
    assert meta["restore_mode"] == "POINT_IN_TIME"
    assert "point_in_time_utc" in meta
    assert meta["point_in_time_original"] == "2026-05-30 18:00:00 +07:00"
    assert meta["restore_id"] == "ACME_TO_SQLSERVER_TEST"


# ── restore_id scoping regression tests ───────────────────────────────────────

def _make_two_target_configs(tmp_path: Path):
    base = make_config(tmp_path)
    cfg1 = dataclasses.replace(
        base,
        restore_id="RESTORE_TO_TARGET_1",
        source_id="SRC",
        target_id="TARGET_1",
        vm_credential_target="10.0.0.1",
    )
    cfg2 = dataclasses.replace(
        base,
        restore_id="RESTORE_TO_TARGET_2",
        source_id="SRC",
        target_id="TARGET_2",
        vm_credential_target="10.0.0.2",
    )
    return cfg1, cfg2


def _fake_workflow_ops(monkeypatch, called_targets):
    def fake_copy(step_config, logger=None, force=False):
        called_targets.append(("copy", step_config.target_id))
        return CopyBackupResult(returncode=0, source_backup_dir=step_config.prod_backup_share, local_import_dir=step_config.vm_import_unc, files_considered=1, copied=1, skipped=0, file_results=())

    def fake_restore(config, db_ops_config, dry_run, logger, point_in_time_utc=None):
        called_targets.append(("restore", config.target_id))
        return {"status": "SUCCESS", "per_database_restore_status": {}}

    def fake_delete(step_config, logger=None):
        called_targets.append(("delete", step_config.target_id))
        return DeleteBackupResult(returncode=0, target_backup_dir=step_config.vm_import_unc, delete_older_than_hours=48, files_considered=0, deleted=0, file_results=())

    monkeypatch.setattr("db_ops.backup_restore.cli.run_copy_backup", fake_copy)
    monkeypatch.setattr("db_ops.backup_restore.cli.run_restore_all_latest", fake_restore)
    monkeypatch.setattr("db_ops.backup_restore.cli.run_delete_backup", fake_delete)
    monkeypatch.setattr("db_ops.backup_restore.cli.run_target_preflight", lambda config, logger=None: None)


def test_restore_workflow_scoping_only_runs_selected_restore_id(tmp_path, monkeypatch):
    cfg1, cfg2 = _make_two_target_configs(tmp_path)
    app_config = DbOpsConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime", sqlite_path=tmp_path / "runtime" / "db_ops.sqlite")
    called_targets = []
    _fake_workflow_ops(monkeypatch, called_targets)

    run_restore_workflow(restore_configs=[cfg1], app_config=app_config)

    targets_called = [t for _, t in called_targets]
    assert "TARGET_1" in targets_called
    assert "TARGET_2" not in targets_called


def test_restore_workflow_scoping_second_restore_id_only_runs_second_target(tmp_path, monkeypatch):
    cfg1, cfg2 = _make_two_target_configs(tmp_path)
    app_config = DbOpsConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime", sqlite_path=tmp_path / "runtime" / "db_ops.sqlite")
    called_targets = []
    _fake_workflow_ops(monkeypatch, called_targets)

    run_restore_workflow(restore_configs=[cfg2], app_config=app_config)

    targets_called = [t for _, t in called_targets]
    assert "TARGET_2" in targets_called
    assert "TARGET_1" not in targets_called


def test_restore_workflow_multi_config_runs_both_targets(tmp_path, monkeypatch):
    cfg1, cfg2 = _make_two_target_configs(tmp_path)
    app_config = DbOpsConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime", sqlite_path=tmp_path / "runtime" / "db_ops.sqlite")
    called_targets = []
    _fake_workflow_ops(monkeypatch, called_targets)

    result = run_restore_workflow(restore_configs=[cfg1, cfg2], app_config=app_config)

    targets_called = [t for _, t in called_targets]
    assert "TARGET_1" in targets_called
    assert "TARGET_2" in targets_called
    assert result["restore_count"] == 2


def test_restore_workflow_windows_then_linux_keeps_per_restore_executor(tmp_path, monkeypatch):
    import db_ops.backup_restore.restore_database as restore_module

    windows = dataclasses.replace(
        make_config(tmp_path),
        restore_id="ACME_TO_SQLSERVER_198_51_100_129",
        target_id="SQLSERVER-VM-TEST-198-51-100-129",
        vm_platform="windows",
        vm_credential_target="198.51.100.129",
    )
    linux = dataclasses.replace(
        make_config(tmp_path),
        restore_id="ACME_TO_SQLSERVER_198_51_100_31",
        target_id="SQLSERVER-VM-TEST-198-51-100-31",
        vm_platform="linux",
        vm_credential_target="198.51.100.31",
    )
    app_config = DbOpsConfig(
        log_dir=tmp_path / "logs",
        runtime_dir=tmp_path / "runtime",
        sqlite_path=tmp_path / "runtime" / "db_ops.sqlite",
    )
    executor_calls = []
    workflow_logger = object()

    monkeypatch.setattr("db_ops.backup_restore.cli.run_target_preflight", lambda config, logger=None: None)
    monkeypatch.setattr("db_ops.backup_restore.cli.log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(restore_module, "log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "db_ops.backup_restore.cli.run_copy_backup",
        lambda config, logger=None, force=False: CopyBackupResult(
            returncode=0,
            source_backup_dir=config.prod_backup_share,
            local_import_dir=config.vm_import_unc,
            files_considered=1,
            copied=1,
            skipped=0,
            file_results=(),
        ),
    )
    monkeypatch.setattr(
        "db_ops.backup_restore.cli.run_delete_backup",
        lambda config, logger=None: DeleteBackupResult(
            returncode=0,
            target_backup_dir=config.vm_import_unc,
            delete_older_than_hours=config.copy_recent_hours,
            files_considered=0,
            deleted=0,
            file_results=(),
        ),
    )
    monkeypatch.setattr(
        restore_module,
        "_run_sqlcmd_query_command_streaming",
        lambda cmd, **kwargs: executor_calls.append(("powershell", "198.51.100.129", cmd))
        or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    monkeypatch.setattr(
        restore_module,
        "_run_sqlcmd_via_ssh",
        lambda cmd, config: executor_calls.append(("ssh", config.vm_credential_target, cmd))
        or subprocess.CompletedProcess(cmd, 0, "", ""),
    )

    def fake_restore(*, config, **_kwargs):
        cmd = build_sqlcmd_query_command(sql=f"SELECT '{config.restore_id}';", config=config)
        run_sqlcmd_query_command(
            cmd,
            config=config,
            logger=workflow_logger,
            progress_step="restore-full",
            restore_id=config.restore_id,
        )
        return {
            "status": "SUCCESS",
            "per_database_restore_status": {config.restore_id: "SUCCESS"},
        }

    monkeypatch.setattr("db_ops.backup_restore.cli.run_restore_all_latest", fake_restore)

    result = run_restore_workflow(
        restore_configs=[windows, linux],
        app_config=app_config,
        logger=workflow_logger,
    )

    assert result["status"] == "SUCCESS"
    assert executor_calls[0][0:2] == ("powershell", "198.51.100.129")
    assert executor_calls[1][0:2] == ("ssh", "198.51.100.31")
    assert all("198.51.100.129" not in cmd for executor, _, cmd in executor_calls if executor == "ssh")


def test_restore_workflow_summary_includes_mappings_and_per_restore_results(tmp_path, monkeypatch):
    cfg1, cfg2 = _make_two_target_configs(tmp_path)
    app_config = DbOpsConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime", sqlite_path=tmp_path / "runtime" / "db_ops.sqlite")
    called_targets = []
    _fake_workflow_ops(monkeypatch, called_targets)

    result = run_restore_workflow(restore_configs=[cfg1, cfg2], app_config=app_config)

    assert result["restore_count"] == 2
    mappings = result["mappings"]
    assert len(mappings) == 2
    assert mappings[0]["restore_id"] == "RESTORE_TO_TARGET_1"
    assert mappings[0]["target_id"] == "TARGET_1"
    assert mappings[1]["restore_id"] == "RESTORE_TO_TARGET_2"
    assert mappings[1]["target_id"] == "TARGET_2"

    per = result["per_restore_results"]
    assert len(per) == 2
    assert per[0]["restore_id"] == "RESTORE_TO_TARGET_1"
    assert per[0]["status"] == "SUCCESS"
    assert per[1]["restore_id"] == "RESTORE_TO_TARGET_2"
    assert per[1]["status"] == "SUCCESS"


def test_restore_workflow_single_config_includes_mapping(tmp_path, monkeypatch):
    cfg1, _ = _make_two_target_configs(tmp_path)
    app_config = DbOpsConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime", sqlite_path=tmp_path / "runtime" / "db_ops.sqlite")
    called_targets = []
    _fake_workflow_ops(monkeypatch, called_targets)

    result = run_restore_workflow(restore_configs=[cfg1], app_config=app_config)

    assert result["restore_count"] == 1
    assert len(result["mappings"]) == 1
    assert result["mappings"][0]["restore_id"] == "RESTORE_TO_TARGET_1"
    assert len(result["per_restore_results"]) == 1
    assert result["per_restore_results"][0]["restore_id"] == "RESTORE_TO_TARGET_1"


# ── START/FINISH metadata includes mappings ────────────────────────────────────

def test_cli_restore_workflow_start_event_has_mappings_for_single_config(tmp_path, monkeypatch):
    events = _make_cli_restore_workflow_calls(tmp_path, monkeypatch)
    start_event = next(e for e in events if e.get("phase") == "START")
    meta = start_event["metadata"]
    assert "mappings" in meta
    assert len(meta["mappings"]) == 1
    assert meta["mappings"][0]["restore_id"] == "ACME_TO_SQLSERVER_TEST"
    assert meta["mappings"][0]["source_id"] == "SRC"
    assert meta["mappings"][0]["target_id"] == "TGT-VM"
    assert meta["restore_count"] == 1


def test_cli_restore_workflow_end_event_has_per_restore_results_from_output(tmp_path, monkeypatch):
    import db_ops.backup_restore.cli as cli_module
    app_config = DbOpsConfig(log_dir=tmp_path / "logs", runtime_dir=tmp_path / "runtime", sqlite_path=tmp_path / "runtime" / "db_ops.sqlite")
    captured_events = []

    cfg = dataclasses.replace(make_config(tmp_path), restore_id="ACME_TO_SQLSERVER_TEST", source_id="SRC", target_id="TGT-VM", vm_credential_target="10.0.0.1")

    monkeypatch.setattr(cli_module, "load_config", lambda _: app_config)
    monkeypatch.setattr(cli_module, "load_restore_configs", lambda _: [cfg])
    monkeypatch.setattr(cli_module, "emit_backup_restore_event", lambda **kw: captured_events.append(kw))
    monkeypatch.setattr(cli_module, "log_function_call", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "patch_stdout", lambda *a, **k: None)
    monkeypatch.setattr(cli_module, "setup_app_logger", lambda *a, **k: object())
    monkeypatch.setattr(
        cli_module,
        "run_restore_workflow",
        lambda **_: {
            "status": "SUCCESS",
            "overall_workflow_status": "SUCCESS",
            "restore_mode": "LATEST",
            "restore_count": 1,
            "mappings": [{"restore_id": "ACME_TO_SQLSERVER_TEST", "source_id": "SRC", "target_id": "TGT-VM", "target_host": "10.0.0.1"}],
            "databases_considered": 3,
            "success": 3,
            "failed": 0,
            "skipped": 0,
            "duration_seconds": 30.0,
            "per_restore_results": [{"restore_id": "ACME_TO_SQLSERVER_TEST", "source_id": "SRC", "target_id": "TGT-VM", "status": "SUCCESS", "databases_considered": 3, "success": 3, "failed": 0, "skipped": 0}],
        },
    )

    cli_module.main(["restore-workflow", "--config", "config.json"])

    end_event = next(e for e in captured_events if e.get("phase") == "END")
    meta = end_event["metadata"]
    assert meta.get("databases_considered") == 3
    assert meta.get("success") == 3
    assert meta.get("failed") == 0
    assert meta.get("per_restore_results") is not None
    assert meta["per_restore_results"][0]["restore_id"] == "ACME_TO_SQLSERVER_TEST"


# ── Telegram START/FINISH format with mappings ─────────────────────────────────

def test_format_restore_workflow_telegram_start_includes_all_mappings():
    metadata = {
        "app": "backup_restore",
        "command": "restore-workflow",
        "phase": "START",
        "restore_mode": "LATEST",
        "restore_count": 2,
        "mappings": [
            {"restore_id": "RESTORE_1", "source_id": "SRC", "target_id": "TGT-1", "target_host": "10.0.0.1", "target_os_type": "windows"},
            {"restore_id": "RESTORE_2", "source_id": "SRC", "target_id": "TGT-2", "target_host": "10.0.0.2", "target_os_type": "linux"},
        ],
    }
    text = _format_restore_workflow_telegram_message(level="logging", message="ignored", metadata=metadata)
    assert "Restore workflow started." in text
    assert "restore_mode=LATEST" in text
    assert "restore_count=2" in text
    assert "restore_id=RESTORE_1" in text
    assert "target_id=TGT-1" in text
    assert "target_host=10.0.0.1" in text
    assert "target_os_type=windows" in text
    assert "restore_id=RESTORE_2" in text
    assert "target_host=10.0.0.2" in text
    assert "point_in_time" not in text


def test_format_restore_workflow_telegram_start_pitr_includes_point_in_time():
    metadata = {
        "app": "backup_restore",
        "command": "restore-workflow",
        "phase": "START",
        "restore_mode": "POINT_IN_TIME",
        "point_in_time_original": "2026-05-30 18:00:00 +07:00",
        "point_in_time_utc": "2026-05-30T11:00:00+00:00",
        "restore_count": 1,
        "mappings": [{"restore_id": "RESTORE_1", "source_id": "SRC", "target_id": "TGT-1"}],
    }
    text = _format_restore_workflow_telegram_message(level="logging", message="ignored", metadata=metadata)
    assert "restore_mode=POINT_IN_TIME" in text
    assert "point_in_time=2026-05-30 18:00:00 +07:00" in text
    assert "point_in_time_utc=2026-05-30T11:00:00+00:00" in text
    assert "restore_count=1" in text


def test_format_restore_workflow_telegram_end_includes_per_restore_results():
    metadata = {
        "app": "backup_restore",
        "command": "restore-workflow",
        "phase": "END",
        "restore_mode": "LATEST",
        "restore_count": 2,
        "mappings": [
            {"restore_id": "RESTORE_1", "source_id": "SRC", "target_id": "TGT-1", "target_host": "10.0.0.1"},
            {"restore_id": "RESTORE_2", "source_id": "SRC", "target_id": "TGT-2", "target_host": "10.0.0.2"},
        ],
        "databases_considered": 10,
        "success": 10,
        "failed": 0,
        "skipped": 0,
        "duration_seconds": 370.314,
        "per_restore_results": [
            {"restore_id": "RESTORE_1", "source_id": "SRC", "target_id": "TGT-1", "target_host": "10.0.0.1", "status": "SUCCESS", "databases_considered": 5, "success": 5, "failed": 0, "skipped": 0},
            {"restore_id": "RESTORE_2", "source_id": "SRC", "target_id": "TGT-2", "target_host": "10.0.0.2", "status": "SUCCESS", "databases_considered": 5, "success": 5, "failed": 0, "skipped": 0},
        ],
        "output": {"status": "SUCCESS"},
    }
    text = _format_restore_workflow_telegram_message(level="logging", message="ignored", metadata=metadata)
    assert "Restore workflow finished. status=SUCCESS" in text
    assert "restore_mode=LATEST" in text
    assert "restore_count=2" in text
    assert "databases_considered=10" in text
    assert "duration_seconds=370.314" in text
    assert "restore_id=RESTORE_1" in text
    assert "restore_id=RESTORE_2" in text
    assert "target_host=10.0.0.1" in text
    assert "target_host=10.0.0.2" in text


def test_format_restore_workflow_telegram_latest_does_not_show_point_in_time_with_mappings():
    metadata = {
        "app": "backup_restore",
        "command": "restore-workflow",
        "phase": "START",
        "restore_mode": "LATEST",
        "restore_count": 1,
        "mappings": [{"restore_id": "RESTORE_1", "source_id": "SRC", "target_id": "TGT-1"}],
    }
    text = _format_restore_workflow_telegram_message(level="logging", message="ignored", metadata=metadata)
    assert "point_in_time" not in text


def test_build_restore_mapping_includes_target_host_and_os():
    cfg = dataclasses.replace(
        make_config(Path(".")),
        restore_id="MY_RESTORE",
        source_id="SRC",
        target_id="TGT",
        vm_credential_target="192.0.2.1",
        vm_platform="linux",
    )
    m = _build_restore_mapping(cfg)
    assert m["restore_id"] == "MY_RESTORE"
    assert m["source_id"] == "SRC"
    assert m["target_id"] == "TGT"
    assert m["target_host"] == "192.0.2.1"
    assert m["target_os_type"] == "linux"
