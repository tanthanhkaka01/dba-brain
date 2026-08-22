from __future__ import annotations

import json
from dataclasses import dataclass, field
import datetime as dt
from pathlib import Path, PureWindowsPath
from db_ops.lib.paths import DEFAULT_DATA_DIR
from typing import Any

from db_ops.lib.notify import (
    NOTIFY_RULE_NAMES,
    NotifyConfig,
    NotifyConfigError,
    NotifyRule,
    parse_notify_config,
)
from db_ops.lib.time_window import TimeWindow, parse_time_window_config
from db_ops.config import DEFAULT_CONFIG_PATH


DEFAULT_RESTORE_CONFIG_PATH = DEFAULT_DATA_DIR / "restore_config.json"

# Restore entries for these engines are driven by a shell script asset, not by the SQL Server
# engine in restore_database.py. They live in the same ``restores`` list so an operator sees one
# list of restores with one shape (restore_id + time_window), but they carry none of the SQL
# Server fields (SMB share, .bak paths, sqlcmd) that parse_restore_config requires - so the
# SQL Server parser must skip them rather than fail on the missing keys.
SCRIPT_RESTORE_DB_TYPES = {"oracle", "postgresql", "mysql"}


def is_script_restore(item: dict[str, Any]) -> bool:
    """Whether this entry is driven by a shell script rather than the SQL Server engine.

    Declaring a ``script`` is what makes an entry script-driven — the engine is not the
    deciding factor. SQL Server can be either: the production flow reads an SMB share and
    drives sqlcmd through ``restore_database.py``, while a container-to-container drill is the
    same shape as the Oracle/PostgreSQL ones and reuses their machinery (host-to-host
    transfer, staging into the target container, env_secrets). Keying off ``db_type`` alone
    would force every SQL Server entry down one path or the other.
    """
    entry = item or {}
    if str(entry.get("script") or "").strip():
        return True
    return str(entry.get("db_type") or "").strip().lower() in SCRIPT_RESTORE_DB_TYPES


# backup_restore notifies by default: an entry with no ``notify`` object behaves exactly as
# it did before the object existed. The shape itself is db_ops.lib.notify's.
# How long a restore keeps the backup files it staged on the TARGET. This is the target's own
# retention, unrelated to the source's: the source decides how far back it can recover from, the
# target only needs enough to run its next restore, and a staging directory that only ever grows
# eventually fills the disk it restores onto.
#
# 8 days by default because the full backup is weekly: the newest full is therefore never more
# than 7 days old, so an 8-day cutoff always keeps that full and every incremental chained to it.
# Over-deleting is self-correcting rather than destructive - the transfer compares against the
# source and re-copies anything missing - but a restore in between would fail, so the margin is
# deliberate rather than tight.
DEFAULT_TARGET_RETENTION_SECONDS = 8 * 24 * 3600


def parse_target_retention_seconds(entry: dict, *, context: str) -> int:
    """Read ``target_retention_seconds`` from a restore entry.

    **0 does not disable the cleanup** — it removes the *age* gate, so every staged file becomes a
    candidate and the chain rule alone decides (see ``delete_backup.obsolete_only``: never the
    newest full, nor anything at or after it). That is "clear what the next restore does not need",
    which is what :data:`clear_staging_after_restore` asks for, not "keep everything". The older
    wording here and on the dataclass field said the opposite and was believed for long enough to
    matter — the 187.249 staging directory reached 16.7 GB under a 14-day window.
    """
    raw = entry.get("target_retention_seconds")
    if raw is None:
        return DEFAULT_TARGET_RETENTION_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{context}.target_retention_seconds must be a whole number of seconds.") from None
    if value < 0:
        raise ValueError(f"{context}.target_retention_seconds must not be negative (0 = no age gate).")
    return value


BACKUP_RESTORE_NOTIFY_DEFAULTS = NotifyConfig(
    logging_on_run=NotifyRule(enabled=True),
    alert_on_error=NotifyRule(enabled=True),
)


def parse_backup_restore_notify(
    entry: dict[str, Any], *, context: str, inherit: NotifyConfig | None = None
) -> NotifyConfig:
    """The ``notify`` object of one backup entry, backup sub-job or restore entry."""
    try:
        return parse_notify_config(
            entry, context=context, defaults=BACKUP_RESTORE_NOTIFY_DEFAULTS, inherit=inherit
        )
    except NotifyConfigError as exc:
        raise ValueError(str(exc)) from exc


def merge_notify_configs(blocks: list[NotifyConfig]) -> NotifyConfig:
    """The notify object for an event covering several entries at once (a multi-entry workflow).

    One entry's block must not decide for the others, so a rule is only switched off when
    **every** entry switches it off — otherwise an entry that expected the message would lose
    it because a different entry happened to be quiet. The destination follows the first
    entry that still wants the message.
    """
    if not blocks:
        return BACKUP_RESTORE_NOTIFY_DEFAULTS
    if len(blocks) == 1:
        return blocks[0]
    merged: dict[str, NotifyRule] = {}
    for name in NOTIFY_RULE_NAMES:
        rules = [getattr(block, name) for block in blocks]
        enabled = [rule for rule in rules if rule.enabled]
        merged[name] = enabled[0] if enabled else rules[0]
    return NotifyConfig(**merged)

RESTORE_PARSER_DEFAULTS = {
    "copy_file_patterns": ["*.bak", "*.trn"],
    "copy_recent_hours": 24,
    "target_retention_seconds": None,
    "full_backup_subdir": "FULL",
    "sqlcmd_path": "sqlcmd",
    "robocopy_path": "robocopy",
    "prod_smb_credential_target": "",
    "prod_smb_username": "",
    "prod_smb_password_env": "",
    "vm_credential_target": "",
    "vm_username": "",
    "vm_password_env": "",
    "restore_sql_instance_on_vm": "localhost",
    "restore_data_dir_on_vm": r"D:\MSSQL\DATA",
    "restore_sql_username": "",
    "restore_sql_password_env": "",
    "sql_login_timeout_seconds": 60,
    "sql_query_timeout_seconds": 0,
    "remote_command_timeout_seconds": 60,
    "restore_command_timeout_seconds": 0,
    "certificate_api_url": "",
    "certificate_api_token_ref": "TOKEN_192_0_2_112_VAULT",
    "certificate_api_verify_tls": False,
}


@dataclass(frozen=True)
class DatabaseRestoreMapping:
    source_database: str
    target_database: str = ""


@dataclass(frozen=True)
class BackupRestoreConfig:
    prod_backup_share: Path
    vm_import_unc: Path
    vm_import_local: Path
    vm_log_unc: Path
    vm_log_local: Path
    prod_smb_credential_target: str
    prod_smb_username: str
    prod_smb_password_env: str
    vm_credential_target: str
    vm_username: str
    vm_password_env: str
    restore_sql_instance_on_vm: str
    restore_sql_username: str = ""
    restore_sql_password_env: str = ""
    sql_login_timeout_seconds: int = 60
    sql_query_timeout_seconds: int = 0
    remote_command_timeout_seconds: int = 60
    restore_command_timeout_seconds: int = 0
    source_id: str = ""
    target_id: str = ""
    restore_id: str = ""
    vm_platform: str = "windows"
    restore_data_dir_on_vm: Path = Path(r"D:\MSSQL\DATA")
    copy_file_patterns: tuple[str, ...] = ("*.bak", "*.trn")
    copy_recent_hours: int = 24
    # Seconds of staged backup kept on the target after a restore (0 = no age gate; see
    # parse_target_retention_seconds).
    target_retention_seconds: int = DEFAULT_TARGET_RETENTION_SECONDS
    full_backup_subdir: str = "FULL"
    sqlcmd_path: str = "sqlcmd"
    robocopy_path: str = "robocopy"
    source_database_name: str = ""
    restore_database_name: str = ""
    restore_data_file_on_vm: Path | None = None
    restore_log_file_on_vm: Path | None = None
    databases: tuple[DatabaseRestoreMapping, ...] = ()
    certificate_api_url: str = ""
    certificate_api_token_ref: str = "TOKEN_192_0_2_112_VAULT"
    certificate_api_verify_tls: bool = False
    execution_mode: str = "sync"
    active: bool = True
    # Opt-in: also replay the source instance's server-level metadata around this restore. The
    # same block, the same parser and the same two phases the script path uses - see
    # db_ops.backup_restore.server_metadata. Absent means this entry behaves exactly as it did
    # before the capability existed.
    server_metadata: Any = None
    # Scheduling, mirroring a backup job: the workflow command runs the restore entries that are
    # due instead of every active one, and records each run so the next due check can read it.
    time_window: TimeWindow = field(default_factory=TimeWindow)
    # Per-entry notify object (db_ops.lib.notify): logging_on_run + alert_on_error.
    notify: NotifyConfig = field(default_factory=lambda: BACKUP_RESTORE_NOTIFY_DEFAULTS)
    copy_window_start_utc: dt.datetime | None = None
    copy_window_end_utc: dt.datetime | None = None

    @property
    def is_linux(self) -> bool:
        return self.vm_platform.lower() == "linux"

    @property
    def vm_copy_log_unc(self) -> Path:
        # A UNC path names a Windows share, so it is joined the way Windows joins. Built with the
        # local separator it becomes `\\VM_IP\E$\LOGS/copy_sqlbk.log` on the Ubuntu worker that
        # actually runs this — and it is then passed to `robocopy /LOG:`, which is stricter about
        # its arguments than the filesystem API is about its paths.
        return Path(str(PureWindowsPath(self.vm_log_unc) / "copy_sqlbk.log"))

    @property
    def full_backup_dir(self) -> Path:
        if self.source_database_name:
            return self.vm_import_unc / self.source_database_name / self.full_backup_subdir
        return self.vm_import_unc / self.full_backup_subdir

    @property
    def source_backup_dir(self) -> Path:
        return self.prod_backup_share

    @property
    def local_import_dir(self) -> Path:
        return self.vm_import_unc

    @property
    def robocopy_log_path(self) -> Path:
        return self.vm_copy_log_unc

    @property
    def source_database(self) -> str:
        return self.source_database_name

    @property
    def restore_database(self) -> str:
        return self.restore_database_name

    @property
    def data_file(self) -> Path:
        if self.restore_data_file_on_vm:
            return self.restore_data_file_on_vm
        return self.restore_data_dir_on_vm

    @property
    def log_file(self) -> Path:
        if self.restore_log_file_on_vm:
            return self.restore_log_file_on_vm
        return self.restore_data_dir_on_vm

    @property
    def sql_instance(self) -> str:
        return self.restore_sql_instance_on_vm


def load_restore_configs(config_path: str | Path | None = None) -> list[BackupRestoreConfig]:
    values = _load_default_restore_values()
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if path.exists():
        with path.open("r", encoding="utf-8-sig") as file:
            raw = json.load(file)
        section = raw.get("backup_restore") if isinstance(raw, dict) else None
        if section is None and _looks_like_restore_config(raw):
            section = raw
        if section is None:
            section = {}
        if isinstance(section, list):
            return [parse_restore_config({**values, **{key: value for key, value in item.items() if value is not None}}) for item in section]
        if not isinstance(section, dict):
            raise ValueError("backup_restore config must be a JSON object or array.")
        restores = section.get("restores")
        if restores is not None:
            return _parse_restore_items(values=values, section=section)
        sources = section.get("sources")
        if sources is not None:
            return _parse_restore_sources(values=values, section=section)
        values.update({key: value for key, value in section.items() if value is not None})

    if values.get("restores") is not None:
        return _parse_restore_items(values=RESTORE_PARSER_DEFAULTS, section=values)
    if values.get("sources") is not None:
        return _parse_restore_sources(values=RESTORE_PARSER_DEFAULTS, section=values)
    return [parse_restore_config(values)]


def load_restore_config(config_path: str | Path | None = None) -> BackupRestoreConfig:
    configs = load_restore_configs(config_path)
    if not configs:
        raise ValueError("backup_restore must contain at least one source.")
    return configs[0]


def _unc_host(path_text: str) -> str:
    """Best-effort host from a UNC path ``\\\\host\\share\\...`` (else empty)."""
    text = str(path_text or "").strip().replace("/", "\\")
    if text.startswith("\\\\"):
        rest = text[2:]
        host = rest.split("\\", 1)[0]
        return host.strip().lower()
    return ""


def _instance_host(sql_instance: str) -> str:
    """Host part of a SQL Server instance spec ``host\\instance`` / ``host,port`` (else the whole thing)."""
    text = str(sql_instance or "").strip().lower()
    for sep in ("\\", ",", "/"):
        if sep in text:
            text = text.split(sep, 1)[0]
    return text.strip()


def validate_restore_target_is_not_source(config: BackupRestoreConfig) -> None:
    source_target = config.prod_smb_credential_target.strip().lower()
    vm_target = config.vm_credential_target.strip().lower()
    sql_instance = config.restore_sql_instance_on_vm.strip().lower()
    vm_import_unc = str(config.vm_import_unc).strip().lower()

    if source_target and vm_target and source_target == vm_target:
        raise ValueError(
            f"Unsafe restore config: source credential_target and target credential_target are both {config.vm_credential_target}."
        )
    if source_target and sql_instance and source_target in sql_instance:
        raise ValueError(
            f"Unsafe restore config: restore SQL instance points at source server {config.prod_smb_credential_target}."
        )
    if not config.is_linux and source_target and vm_import_unc.startswith(f"\\\\{source_target}\\"):
        raise ValueError(
            f"Unsafe restore config: vm_import_unc points at source server {config.prod_smb_credential_target}."
        )

    # Identity-based checks that still fire when credential_target is unset (e.g. Linux
    # SSH targets that need no cmdkey). These catch a restore_id that was mis-edited to
    # aim back at the source even though the credential_target guard above no-ops.
    source_id = str(config.source_id or "").strip().lower()
    target_id = str(config.target_id or "").strip().lower()
    if source_id and target_id and source_id == target_id:
        raise ValueError(
            f"Unsafe restore config: source_id and target_id are both '{config.source_id}'; "
            "the restore would target its own source."
        )
    share_host = _unc_host(str(config.prod_backup_share))
    restore_host = _instance_host(sql_instance)
    if share_host and restore_host and share_host == restore_host:
        raise ValueError(
            f"Unsafe restore config: the restore SQL instance host '{restore_host}' equals the "
            "backup source share host; the restore would overwrite a database on the source server."
        )


def _merge_source_config(*, values: dict[str, Any], common: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("backup_restore.sources entries must be objects.")
    merged = {**values, **common, **{key: value for key, value in item.items() if value is not None}}
    for nested_key in ("source", "target"):
        nested_common = _without_empty_values(common.get(nested_key)) if isinstance(common.get(nested_key), dict) else {}
        nested_item = _without_empty_values(item.get(nested_key)) if isinstance(item.get(nested_key), dict) else {}
        if nested_common or nested_item:
            merged[nested_key] = {**nested_common, **nested_item}
    return merged


def _without_empty_values(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if value is not None and str(value).strip() != ""}


def _parse_restore_items(*, values: dict[str, Any], section: dict[str, Any]) -> list[BackupRestoreConfig]:
    restores = section.get("restores")
    if not isinstance(restores, list):
        raise ValueError("backup_restore.restores must be an array.")
    if not restores:
        raise ValueError("backup_restore.restores must contain at least one entry.")
    restores = [item for item in restores if not (isinstance(item, dict) and is_script_restore(item))]
    if not restores:
        return []
    restore_ids = [str(item.get("restore_id", "")) for item in restores if isinstance(item, dict)]
    non_empty = [rid for rid in restore_ids if rid]
    dupes = sorted({rid for rid in non_empty if non_empty.count(rid) > 1})
    if dupes:
        raise ValueError(f"Duplicate restore_id values in backup_restore.restores: {dupes}")
    common = {key: value for key, value in section.items() if key not in ("restores", "sources") and value is not None}
    return [
        parse_restore_config(_merge_source_config(values=values, common=common, item=item))
        for item in restores
    ]


def _parse_restore_sources(*, values: dict[str, Any], section: dict[str, Any]) -> list[BackupRestoreConfig]:
    sources = section.get("sources")
    if not isinstance(sources, list):
        raise ValueError("backup_restore.sources must be an array.")
    common = {key: value for key, value in section.items() if key != "sources" and value is not None}
    return [
        parse_restore_config(_merge_source_config(values=values, common=common, item=item))
        for item in sources
    ]


def _load_default_restore_values() -> dict[str, Any]:
    values = dict(RESTORE_PARSER_DEFAULTS)
    if not DEFAULT_RESTORE_CONFIG_PATH.exists():
        return values
    with DEFAULT_RESTORE_CONFIG_PATH.open("r", encoding="utf-8-sig") as file:
        raw = json.load(file)
    section = raw.get("backup_restore") if isinstance(raw, dict) else None
    if section is None and _looks_like_restore_config(raw):
        section = raw
    if not isinstance(section, dict):
        raise ValueError(f"Default restore config must contain a backup_restore object: {DEFAULT_RESTORE_CONFIG_PATH}")
    values.update({key: value for key, value in section.items() if value is not None})
    return values


def _looks_like_restore_config(raw: object) -> bool:
    return isinstance(raw, dict) and ("restores" in raw or "sources" in raw or "source" in raw or "prod_backup_share" in raw)


def parse_restore_config(raw: dict[str, Any]) -> BackupRestoreConfig:
    values = _with_legacy_keys(_with_source_target_pair(raw))
    # Imported here, not at module scope: server_metadata imports common.sqlserver_instance,
    # which imports this module for TOOL_ROOT.
    from db_ops.backup_restore.server_metadata import parse_server_metadata

    restore_id_for_label = str(values.get("restore_id") or "")
    return BackupRestoreConfig(
        server_metadata=parse_server_metadata(
            values.get("server_metadata"),
            label=f"backup_restore.restores ({restore_id_for_label or '?'})",
            for_restore=True,
            # The engine path is SQL Server by construction - it drives sqlcmd - and unlike a
            # script entry it carries no db_type field to check against.
            db_type="sqlserver",
        ),
        source_id=str(values.get("source_id") or values.get("server_id") or values["prod_backup_share"]),
        target_id=str(values.get("target_id") or values.get("vm_credential_target") or values["vm_import_unc"]),
        restore_id=str(values.get("restore_id") or ""),
        vm_platform=str(values.get("vm_platform") or "windows").lower().strip(),
        prod_backup_share=Path(str(values["prod_backup_share"])),
        vm_import_unc=Path(str(values["vm_import_unc"])),
        vm_import_local=Path(str(values["vm_import_local"])),
        vm_log_unc=Path(str(values["vm_log_unc"])),
        vm_log_local=Path(str(values["vm_log_local"])),
        copy_file_patterns=_parse_patterns(values.get("copy_file_patterns")),
        copy_recent_hours=_parse_int(values.get("copy_recent_hours"), default=24),
        target_retention_seconds=parse_target_retention_seconds(values, context="backup_restore"),
        prod_smb_credential_target=str(values.get("prod_smb_credential_target") or ""),
        prod_smb_username=str(values.get("prod_smb_username") or ""),
        prod_smb_password_env=str(values.get("prod_smb_password_env") or ""),
        vm_credential_target=str(values.get("vm_credential_target") or ""),
        vm_username=str(values.get("vm_username") or ""),
        vm_password_env=str(values.get("vm_password_env") or ""),
        restore_sql_instance_on_vm=str(values.get("restore_sql_instance_on_vm") or "localhost"),
        restore_sql_username=str(values.get("restore_sql_username") or values.get("sql_username") or ""),
        restore_sql_password_env=str(values.get("restore_sql_password_env") or values.get("sql_password_env") or ""),
        sql_login_timeout_seconds=_parse_int(values.get("sql_login_timeout_seconds"), default=60),
        sql_query_timeout_seconds=_parse_int(values.get("sql_query_timeout_seconds"), default=0),
        remote_command_timeout_seconds=_parse_int(values.get("remote_command_timeout_seconds"), default=60),
        restore_command_timeout_seconds=_parse_int(values.get("restore_command_timeout_seconds"), default=0),
        restore_data_dir_on_vm=Path(str(values["restore_data_dir_on_vm"])),
        full_backup_subdir=str(values.get("full_backup_subdir") or "FULL"),
        sqlcmd_path=str(values.get("sqlcmd_path") or "sqlcmd"),
        robocopy_path=str(values.get("robocopy_path") or "robocopy"),
        source_database_name=str(values.get("source_database_name") or ""),
        restore_database_name=str(values.get("restore_database_name") or ""),
        restore_data_file_on_vm=Path(str(values["restore_data_file_on_vm"])) if values.get("restore_data_file_on_vm") else None,
        restore_log_file_on_vm=Path(str(values["restore_log_file_on_vm"])) if values.get("restore_log_file_on_vm") else None,
        databases=_parse_database_mappings(values.get("databases")),
        certificate_api_url=str(values.get("certificate_api_url") or values.get("api_link_get_cer") or ""),
        certificate_api_token_ref=str(values.get("certificate_api_token_ref") or "TOKEN_192_0_2_112_VAULT"),
        certificate_api_verify_tls=_parse_bool(values.get("certificate_api_verify_tls"), default=False),
        execution_mode=_parse_execution_mode(values.get("execution_mode")),
        active=_parse_bool(values.get("active"), default=True),
        time_window=parse_time_window_config(
            values, context=f"backup_restore.restores[{values.get('restore_id') or '?'}]"
        ).time_window,
        notify=parse_backup_restore_notify(
            values, context=f"backup_restore.restores[{values.get('restore_id') or '?'}]"
        ),
    )


def _with_source_target_pair(raw: dict[str, Any]) -> dict[str, Any]:
    values = dict(raw)
    source = values.pop("source", None) or {}
    target = values.pop("target", None) or {}
    if source:
        if not isinstance(source, dict):
            raise ValueError("backup_restore.sources[].source must be an object.")
        source_map = {
            "id": "source_id",
            "server_id": "source_id",
            "backup_share": "prod_backup_share",
            "credential_target": "prod_smb_credential_target",
            "username": "prod_smb_username",
            "password_env": "prod_smb_password_env",
            "certificate_api_url": "certificate_api_url",
            "api_link_get_cer": "certificate_api_url",
            "certificate_api_token_ref": "certificate_api_token_ref",
            "certificate_api_verify_tls": "certificate_api_verify_tls",
        }
        for old_key, new_key in source_map.items():
            if old_key in source and _should_apply_nested_value(values, new_key):
                values[new_key] = source[old_key]
        for key, value in source.items():
            if key not in source_map and key not in values:
                values[key] = value
    if target:
        if not isinstance(target, dict):
            raise ValueError("backup_restore.sources[].target must be an object.")
        target_map = {
            "id": "target_id",
            "vm_import_unc": "vm_import_unc",
            "vm_import_local": "vm_import_local",
            "vm_import_linux_path": "vm_import_linux_path",
            "vm_import_linux_log_path": "vm_import_linux_log_path",
            "vm_log_unc": "vm_log_unc",
            "vm_log_local": "vm_log_local",
            "credential_target": "vm_credential_target",
            "username": "vm_username",
            "password_env": "vm_password_env",
            "sql_instance": "restore_sql_instance_on_vm",
            "restore_sql_instance": "restore_sql_instance_on_vm",
            "sql_username": "restore_sql_username",
            "restore_sql_username": "restore_sql_username",
            "sql_password_env": "restore_sql_password_env",
            "restore_sql_password_env": "restore_sql_password_env",
            "sql_login_timeout_seconds": "sql_login_timeout_seconds",
            "sql_query_timeout_seconds": "sql_query_timeout_seconds",
            "remote_command_timeout_seconds": "remote_command_timeout_seconds",
            "restore_command_timeout_seconds": "restore_command_timeout_seconds",
            "restore_data_dir": "restore_data_dir_on_vm",
            "vm_platform": "vm_platform",
        }
        for old_key, new_key in target_map.items():
            if old_key in target and _should_apply_nested_value(values, new_key):
                values[new_key] = target[old_key]
        for key, value in target.items():
            if key not in target_map and key not in values:
                values[key] = value
    return values


def _should_apply_nested_value(values: dict[str, Any], key: str) -> bool:
    current = values.get(key)
    if current is None or str(current).strip() == "":
        return True
    return key in RESTORE_PARSER_DEFAULTS and current == RESTORE_PARSER_DEFAULTS[key]


def _with_legacy_keys(raw: dict[str, Any]) -> dict[str, Any]:
    values = dict(raw)
    legacy_map = {
        "source_backup_dir": "prod_backup_share",
        "local_import_dir": "vm_import_unc",
        "sql_instance": "restore_sql_instance_on_vm",
        "source_database": "source_database_name",
        "restore_database": "restore_database_name",
        "data_file": "restore_data_file_on_vm",
        "log_file": "restore_log_file_on_vm",
        "smb_credential_target": "prod_smb_credential_target",
        "smb_username": "prod_smb_username",
        "smb_password_env": "prod_smb_password_env",
    }
    for old_key, new_key in legacy_map.items():
        if new_key not in values and old_key in values:
            values[new_key] = values[old_key]
    if "vm_import_local" not in values and "local_import_dir" in values:
        values["vm_import_local"] = values["local_import_dir"]

    # Linux path fallbacks: vm_import_linux_path fills vm_import_unc/local when not set.
    linux_import = values.get("vm_import_linux_path")
    if linux_import:
        if not values.get("vm_import_unc"):
            values["vm_import_unc"] = linux_import
        if not values.get("vm_import_local"):
            values["vm_import_local"] = linux_import
    linux_log = values.get("vm_import_linux_log_path") or linux_import
    if linux_log:
        if not values.get("vm_log_unc"):
            values["vm_log_unc"] = linux_log
        if not values.get("vm_log_local"):
            values["vm_log_local"] = linux_log

    if "restore_data_dir_on_vm" not in values:
        legacy_data_file = values.get("restore_data_file_on_vm") or values.get("data_file")
        values["restore_data_dir_on_vm"] = (
            str(Path(str(legacy_data_file)).parent) if legacy_data_file else RESTORE_PARSER_DEFAULTS["restore_data_dir_on_vm"]
        )
    if "vm_log_unc" not in values:
        legacy_log = values.get("robocopy_log_path")
        values["vm_log_unc"] = str(Path(str(legacy_log)).parent) if legacy_log else str(values.get("vm_import_unc", ""))
    if "vm_log_local" not in values:
        values["vm_log_local"] = str(values["vm_log_unc"])
    return values


def _parse_patterns(value: Any) -> tuple[str, ...]:
    if value is None:
        return ("*.bak", "*.trn")
    if isinstance(value, str):
        patterns = [item.strip() for item in value.split(",")]
    elif isinstance(value, list):
        patterns = [str(item).strip() for item in value]
    else:
        raise ValueError("copy_file_patterns must be a list or comma-separated string.")
    result = tuple(pattern for pattern in patterns if pattern)
    if not result:
        raise ValueError("copy_file_patterns must contain at least one file pattern.")
    return result


def _parse_database_mappings(value: Any) -> tuple[DatabaseRestoreMapping, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("databases must be an array.")
    mappings: list[DatabaseRestoreMapping] = []
    for item in value:
        if isinstance(item, str):
            source_database = item.strip()
            target_database = ""
        elif isinstance(item, dict):
            source_database = str(item.get("source_database") or item.get("source") or item.get("name") or "").strip()
            target_database = str(item.get("target_database") or item.get("target") or item.get("restore_database") or "").strip()
        else:
            raise ValueError("database mapping must be a string or object.")
        if not source_database:
            raise ValueError("database mapping requires source_database.")
        if not target_database:
            raise ValueError(f"database mapping for {source_database} requires target_database.")
        mappings.append(DatabaseRestoreMapping(source_database=source_database, target_database=target_database))
    return tuple(mappings)


def _parse_int(value: Any, *, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    return int(value)


def _parse_execution_mode(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return "sync"
    mode = str(value).strip().lower()
    if mode not in ("sync", "unsync"):
        raise ValueError(f"execution_mode must be 'sync' or 'unsync', got: {value!r}")
    return mode


def _parse_bool(value: Any, *, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
