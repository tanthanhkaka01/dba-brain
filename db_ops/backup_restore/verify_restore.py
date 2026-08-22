from __future__ import annotations

import subprocess

from db_ops.backup_restore.config import BackupRestoreConfig, load_restore_config
from db_ops.backup_restore.restore_database import build_restore_candidate, find_latest_full_backups, _escape_identifier


def build_checkdb_sql(database_name: str | BackupRestoreConfig) -> str:
    if isinstance(database_name, BackupRestoreConfig):
        database_name = database_name.restore_database_name
    return f"DBCC CHECKDB ([{_escape_identifier(database_name)}]) WITH NO_INFOMSGS;"


def build_checkdb_all_sql(config: BackupRestoreConfig | None = None) -> str:
    restore_config = config or load_restore_config()
    backups = find_latest_full_backups(restore_config)
    if not backups:
        raise FileNotFoundError(f"No latest FULL .bak files found under {restore_config.vm_import_unc}.")
    return "\n".join(build_checkdb_sql(build_restore_candidate(backup, restore_config).restore_database_name) for backup in backups)


def run_verify_restore(config: BackupRestoreConfig | None = None) -> subprocess.CompletedProcess[str]:
    restore_config = config or load_restore_config()
    sql = build_checkdb_all_sql(restore_config)
    from db_ops.backup_restore.restore_database import build_sqlcmd_query_command

    cmd = build_sqlcmd_query_command(sql=sql, config=restore_config)
    return subprocess.run(cmd, check=True, text=True)
