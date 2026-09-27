from __future__ import annotations
from db_ops.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, backup_time_from_name, _log_progress, _write_temp_powershell_script  # noqa: F401 - one definition

import dataclasses
import datetime as dt
import fnmatch
import logging
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

from db_ops.backup_restore.config import BackupRestoreConfig, load_restore_config
from db_ops.lib import data_sources
from db_ops.lib.remote_host import RemoteHost
from db_ops.logging_ops import log_event


ROBOCOPY_SUCCESS_MAX_EXIT_CODE = 7


@dataclasses.dataclass(frozen=True)
class CopyBackupFileResult:
    source_file: Path
    target_file: Path
    status: str
    bytes: int


@dataclasses.dataclass(frozen=True)
class CopyBackupResult:
    returncode: int
    source_backup_dir: Path
    local_import_dir: Path
    files_considered: int
    copied: int
    skipped: int
    file_results: tuple[CopyBackupFileResult, ...]
    failed: int = 0
    replaced: int = 0


def share_login_request(*, credential_target: str, username: str, password_env: str) -> dict[str, str] | None:
    """The ``smb-credential`` request for one share login, the password resolved here - or ``None``
    when no login is configured. ``cmdkey`` itself runs in ``common`` since 0.24.0 (rules R10)."""
    if not credential_target and not username and not password_env:
        return None
    if not credential_target or not username or not password_env:
        raise ValueError("credential_target, username, and password_env are all required when credential setup is enabled.")
    password = resolve_password_ref(password_env)
    if not password:
        raise RuntimeError(f"Password ref not found in environment or secret_text.json: {password_env}")
    return {"target": credential_target, "username": username, "password": password}


def store_share_logins(requests: list[dict[str, str]]) -> None:
    """Store each login Windows needs for the UNC paths that follow (``common.cli smb-credential``)."""
    from db_ops.backup_restore import share

    for request in requests:
        share.store_login(**request)


def resolve_password_ref(password_ref: str) -> str:
    env_value = os.getenv(password_ref, "").strip()
    if env_value:
        return env_value
    secrets = data_sources.load_secret_text()
    return str(secrets.get(password_ref, "")).strip()


def copy_share_login_requests(config: BackupRestoreConfig) -> list[dict[str, str]]:
    """The share logins a copy needs stored: the source share's, and a Windows target's."""
    requests = []
    prod = share_login_request(
        credential_target=config.prod_smb_credential_target,
        username=config.prod_smb_username,
        password_env=config.prod_smb_password_env,
    )
    if prod:
        requests.append(prod)
    if not config.is_linux:
        vm = share_login_request(
            credential_target=config.vm_credential_target,
            username=config.vm_username,
            password_env=config.vm_password_env,
        )
        if vm:
            requests.append(vm)
    return requests


def should_list_the_share(config: BackupRestoreConfig) -> bool:
    """A Windows node copying UNC to UNC lists the source through ``smb-list``."""
    return os.name == "nt" and str(config.prod_backup_share).startswith("\\\\") and str(config.vm_import_unc).startswith("\\\\")


def build_robocopy_command(config: BackupRestoreConfig) -> list[str]:
    _validate_unc_source(config.prod_backup_share)
    cmd = [
        config.robocopy_path,
        str(config.prod_backup_share),
        str(config.vm_import_unc),
        "/E",
        "/Z",
        "/FFT",
        "/R:3",
        "/W:5",
    ]
    if config.robocopy_log_path:
        cmd.append(f"/LOG:{config.robocopy_log_path}")
    return cmd


def list_backup_files(source_dir: Path, patterns: tuple[str, ...]) -> list[Path]:
    return _scan_backup_files(source_dir=source_dir, patterns=patterns, cutoff=None)


def mapped_database_folders(config: BackupRestoreConfig) -> list[str]:
    """The folders a copy reads: one per mapped source database, as the entry spells it - or ``[]``
    when the entry maps none, which means everything on the share.

    The backup jobs write ``<share>/<database>/<FULL|DIFF|LOG>/``, and the restore looks for
    ``<import>/<source_database>/FULL``. The Linux node's copy (``smbclient``) always read only the
    mapped folders; the Windows node's two did not, and took every recent file on the WHOLE share
    - on 2026-09-27, an entry mapping two databases of a production server would have staged the third's 30 G
    full and a day of its logs into a lab VM with 17 G free. Reading ``<share>/<spelling>`` also
    stages it under that spelling, which is the one the restore then asks the Linux target for.
    """
    return [mapping.source_database for mapping in config.databases if mapping.source_database]


def newest_backup_hint(config: BackupRestoreConfig, *, now: float | None = None) -> str:
    """What to add when the copy window holds nothing: the newest matching file there IS, and how
    long before the window it was written.

    "Selected no files" read like a window that was set too narrow. On 2026-09-27 it meant the
    share was dead: a production server's Agent share had had no new file for eight days, because dbabrain's
    own backup job writes that server's backups somewhere else now - and every entry for that server still
    named the old share. Only asked on this failure, so it may list the folders it reads.
    """
    from db_ops.backup_restore import share

    root = str(config.prod_backup_share).replace("/", "\\").rstrip("\\")
    patterns = [pattern.lower() for pattern in config.copy_file_patterns]
    folders = mapped_database_folders(config)
    newest: tuple[float, str] | None = None
    try:
        password = resolve_password_ref(config.prod_smb_password_env) if config.prod_smb_password_env else ""
        for listed in ([f"{root}\\{name}" for name in folders] if folders else [root]):
            for item in share.list_files(listed, username=config.prod_smb_username or "", password=password):
                modified = item.get("modified_epoch")
                name = str(item.get("name") or "").lower()
                if modified is None or not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
                    continue
                if newest is None or float(modified) > newest[0]:
                    newest = (float(modified), f"{listed}\\{item['path']}")
    except Exception as exc:  # noqa: BLE001 - the hint must never replace the error it explains.
        return f" - and the share could not be listed to say more ({exc})"
    if newest is None:
        return " - nothing under it matches the entry's copy_file_patterns at all"
    written = dt.datetime.fromtimestamp(newest[0], tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    cutoff, _end = _copy_window_timestamps(config, now=now)
    if cutoff is None or newest[0] >= cutoff:
        return f" - the newest matching file there is {newest[1]}, written {written}"
    days = (cutoff - newest[0]) / 86400
    return (f" - the newest matching file there is {newest[1]}, written {written}, {days:.1f} day(s) "
            "before the window opens: the source no longer writes its backups to this share, or the "
            "entry names the wrong one")


def list_recent_backup_files(config: BackupRestoreConfig | None = None, *, now: float | None = None) -> list[Path]:
    restore_config = config or load_restore_config()
    cutoff, end_ts = _copy_window_timestamps(restore_config, now=now)
    root = restore_config.source_backup_dir
    folders = mapped_database_folders(restore_config)
    found: list[tuple[float, Path]] = []
    for source_dir in ([root / name for name in folders] if folders else [root]):
        found.extend(_scan_backup_files_with_mtime(
            source_dir=source_dir,
            patterns=restore_config.copy_file_patterns,
            cutoff=cutoff,
            end_ts=end_ts,
        ))
    return [path for _, path in sorted(found, key=lambda item: (item[0], str(item[1]).lower()))]


def _copy_window_timestamps(config: BackupRestoreConfig, *, now: float | None = None) -> tuple[float | None, float | None]:
    start_ts = config.copy_window_start_utc.timestamp() if config.copy_window_start_utc is not None else None
    end_ts = config.copy_window_end_utc.timestamp() if config.copy_window_end_utc is not None else None
    if start_ts is None and config.copy_recent_hours > 0:
        start_ts = (time.time() if now is None else now) - (config.copy_recent_hours * 60 * 60)
    return start_ts, end_ts


def _scan_backup_files(*, source_dir: Path, patterns: tuple[str, ...], cutoff: float | None, end_ts: float | None = None) -> list[Path]:
    return [path for _, path in _scan_backup_files_with_mtime(
        source_dir=source_dir, patterns=patterns, cutoff=cutoff, end_ts=end_ts)]


def _scan_backup_files_with_mtime(*, source_dir: Path, patterns: tuple[str, ...], cutoff: float | None,
                                  end_ts: float | None = None) -> list[tuple[float, Path]]:
    """``(mtime, path)`` for every file under ``source_dir`` matching a pattern, in the window,
    oldest first. A folder that is not there answers nothing - a mapped database with no backups
    yet is the restore's to report, not the copy's."""
    if not source_dir.is_dir():
        return []
    seen: set[str] = set()
    files: dict[str, tuple[float, Path]] = {}
    for pattern in patterns:
        for path in source_dir.rglob(pattern):
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                file_stat = path.stat()
            except OSError:
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            if cutoff is not None and file_stat.st_mtime < cutoff:
                continue
            if end_ts is not None and file_stat.st_mtime > end_ts:
                continue
            files[key] = (file_stat.st_mtime, path)
    return sorted(files.values(), key=lambda item: (item[0], str(item[1]).lower()))


def copy_backup_file(source_file: Path, *, source_root: Path, target_root: Path) -> CopyBackupFileResult:
    relative_path = source_file.relative_to(source_root)
    target_file = target_root / relative_path
    source_size = source_file.stat().st_size

    if target_file.exists() and target_file.stat().st_size == source_size:
        return CopyBackupFileResult(
            source_file=source_file,
            target_file=target_file,
            status="SKIPPED_EXISTS",
            bytes=source_size,
        )

    target_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_file, target_file)
    return CopyBackupFileResult(
        source_file=source_file,
        target_file=target_file,
        status="COPIED",
        bytes=source_size,
    )


def list_recent_backup_files_on_share(
    config: BackupRestoreConfig,
    *,
    now: float | None = None,
) -> list[Path]:
    """The recent backups on a Windows source share, read through ``common.cli smb-list`` (R10).

    The same selection the PowerShell scan made until 0.24.0 - in the mapped database folders only
    since 0.24.1 (:func:`mapped_database_folders`): every file whose NAME matches a copy
    pattern (case-insensitive, as ``Get-ChildItem -Include`` matched) and whose last write falls in
    the copy window - oldest first, then by full path.
    """
    from db_ops.backup_restore import share

    cutoff_ts, end_ts = _copy_window_timestamps(config, now=now)
    password = resolve_password_ref(config.prod_smb_password_env) if config.prod_smb_password_env else ""
    root = str(config.prod_backup_share).replace("/", "\\").rstrip("\\")
    patterns = [pattern.lower() for pattern in config.copy_file_patterns]
    chosen: list[tuple[float, Path]] = []
    # One listing per mapped database folder, as the Linux node's copy does - never the whole
    # share, whose other databases this entry does not restore (mapped_database_folders).
    folders = mapped_database_folders(config)
    for listed in ([f"{root}\\{name}" for name in folders] if folders else [root]):
        for item in share.list_files(listed, username=config.prod_smb_username or "", password=password):
            name = str(item.get("name") or "").lower()
            modified = item.get("modified_epoch")
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns) or modified is None:
                continue
            if cutoff_ts is not None and float(modified) < cutoff_ts:
                continue
            if end_ts is not None and float(modified) > end_ts:
                continue
            chosen.append((float(modified), Path(listed + "\\" + str(item["path"]))))
    return [path for _, path in sorted(chosen, key=lambda item: (item[0], str(item[1]).lower()))]


def copy_recent_backup_files_on_share(
    config: BackupRestoreConfig,
    *,
    now: float | None = None,
) -> tuple[CopyBackupFileResult, ...]:
    selected_files = list_recent_backup_files_on_share(config, now=now)
    return tuple(
        copy_backup_file(
            source_file,
            source_root=config.prod_backup_share,
            target_root=config.vm_import_unc,
        )
        for source_file in selected_files
    )


def target_file_for_source(source_file: Path, *, source_root: Path, target_root: Path) -> Path:
    return target_root / source_file.relative_to(source_root)


def copy_backup_file_with_logging(
    source_file: Path,
    *,
    source_root: Path,
    target_root: Path,
    source_id: str,
    restore_id: str = "",
    logger: logging.Logger | None,
    force: bool = False,
) -> CopyBackupFileResult:
    target_file = target_file_for_source(source_file, source_root=source_root, target_root=target_root)
    source_size = source_file.stat().st_size
    _rid = f"restore_id={restore_id} " if restore_id else ""
    if not force and target_file.exists() and target_file.stat().st_size == source_size:
        result = CopyBackupFileResult(
            source_file=source_file,
            target_file=target_file,
            status="SKIPPED_EXISTS",
            bytes=source_size,
        )
        _log_progress(
            logger,
            (
                f"{_rid}copy-backup source_id={source_id} file_copy_skip "
                f"file={source_file} target={target_file} reason=already_exists_or_same_size"
            ),
        )
        return result

    _log_progress(
        logger,
        (
            f"{_rid}copy-backup source_id={source_id} file_copy_start "
            f"file={source_file} target={target_file} size_bytes={source_size}"
        ),
    )
    started = time.monotonic()
    try:
        result = copy_backup_file(source_file, source_root=source_root, target_root=target_root)
    except Exception as exc:
        _log_progress(
            logger,
            (
                f"{_rid}copy-backup source_id={source_id} file_copy_failed "
                f"file={source_file} target={target_file} error={_format_log_value(exc)}"
            ),
        )
        return CopyBackupFileResult(
            source_file=source_file,
            target_file=target_file,
            status="FAILED",
            bytes=source_size,
        )
    duration_seconds = time.monotonic() - started
    _log_progress(
        logger,
        (
            f"{_rid}copy-backup source_id={source_id} file_copy_done "
            f"file={source_file} target={target_file} size_bytes={result.bytes} "
            f"duration_seconds={duration_seconds:.3f}"
        ),
    )
    return result


#: How long opening a session to the Linux restore target may take; a command itself is unbounded.
SSH_CONNECT_TIMEOUT_SECONDS = 30


def open_ssh_connection(config: BackupRestoreConfig) -> RemoteHost:
    """The Linux restore target, reached through ``common.cli`` - ``run-cmd``, ``push-file``.

    Until 0.24.0 this handed out a raw paramiko client from ``common.remote_exec``, which made this
    app one that imports ``common`` (rules R03). The password is resolved here, as the session
    used to - the environment first, then the secret store - and travels in each request on stdin.
    Nothing is open between calls, so closing it (or leaving the ``with``) costs nothing.
    """
    from db_ops.transport import common_cli

    if not config.is_linux:
        raise RuntimeError(
            f"Target context mismatch: restore_id={config.restore_id} target_host={config.vm_credential_target} "
            "target_os_type=windows cannot execute remote_exec_type=ssh."
        )
    password = resolve_password_ref(config.vm_password_env) if config.vm_password_env else ""
    if config.vm_password_env and not password:
        raise RuntimeError(f"password not found for vm_password_env={config.vm_password_env}")
    return RemoteHost(
        host=config.vm_credential_target, username=config.vm_username, password=password,
        auth_type="password" if config.vm_password_env else "key",
        connect_timeout_seconds=config.remote_command_timeout_seconds or SSH_CONNECT_TIMEOUT_SECONDS,
        call=common_cli.run_allowing_failure)


def _prepare_linux_base_import_dir(remote: RemoteHost, config: BackupRestoreConfig) -> None:
    """Ensure the Linux import base dir exists and is traversable+writable by the SSH user."""
    linux_import = str(config.vm_import_unc).replace("\\", "/")
    username = config.vm_username
    # Fast path: plain mkdir (works if parent chain is already writable by this user)
    if remote.run(f"mkdir -p {shlex.quote(linux_import)}").ok:
        return
    # Need root. Also fix ancestor dirs that might block traverse (e.g. /var/opt/mssql/backup owned
    # by mssql). One line under `sudo`, the login's password on stdin (run-cmd's own).
    ancestors = []
    current = PurePosixPath("/")
    for part in PurePosixPath(linux_import).parts[1:]:  # skip root '/'
        current = current / part
        ancestors.append(str(current))
    # chmod o+x on every ancestor except the leaf (which we chown to tuser)
    ancestor_dirs = ancestors[:-1]
    chmod_part = ""
    if ancestor_dirs:
        chmod_part = "chmod o+x " + " ".join(shlex.quote(d) for d in ancestor_dirs) + " && "
    cmd = (
        f"mkdir -p {shlex.quote(linux_import)} && "
        f"{chmod_part}"
        f"chown {shlex.quote(username)} {shlex.quote(linux_import)}"
    )
    result = remote.run(cmd, sudo=True)
    if not result.ok:
        raise RuntimeError(
            f"Could not prepare Linux import directory {linux_import}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _copy_backup_files_via_sftp(
    config: BackupRestoreConfig,
    selected_files: list[Path],
    *,
    logger: logging.Logger | None,
    force: bool = False,
    source_root: Path | None = None,
) -> tuple[CopyBackupFileResult, ...]:
    # source_root is the base the selected files are relative to. On Windows it is
    # the UNC share; on Linux it is the local smbclient staging dir.
    root = source_root or config.prod_backup_share
    linux_import = PurePosixPath(str(config.vm_import_unc).replace("\\", "/"))
    results: list[CopyBackupFileResult] = []
    _rid = f"restore_id={config.restore_id} " if config.restore_id else ""

    with open_ssh_connection(config) as remote:
        _prepare_linux_base_import_dir(remote, config)
        for source_file in selected_files:
            relative_parts = source_file.relative_to(root).parts
            target_posix = str(linux_import.joinpath(*relative_parts))
            source_size = source_file.stat().st_size
            # Inspect the FINAL destination (not just the temp/staging dir) so an
            # already-imported backup of the same size is never re-transferred, and a
            # changed one is replaced atomically rather than overwritten in place.
            existing_size: int | None = None
            try:
                existing_size = remote.stat(target_posix).st_size
            except FileNotFoundError:
                existing_size = None
            except OSError:
                existing_size = None
            if not force and existing_size is not None and existing_size == source_size:
                results.append(CopyBackupFileResult(
                    source_file=source_file,
                    target_file=Path(target_posix),
                    status="SKIPPED_EXISTS",
                    bytes=source_size,
                ))
                _log_progress(
                    logger,
                    (
                        f"{_rid}copy-backup source_id={config.source_id} copy_skip_existing "
                        f"file={source_file} target={target_posix} size_bytes={source_size} "
                        f"reason=destination_same_size"
                    ),
                )
                continue
            is_replace = existing_size is not None
            if is_replace:
                _log_progress(
                    logger,
                    (
                        f"{_rid}copy-backup source_id={config.source_id} copy_replace_size_mismatch "
                        f"file={source_file} target={target_posix} source_bytes={source_size} "
                        f"destination_bytes={existing_size} action=copy_then_atomic_move"
                    ),
                )
            _log_progress(
                logger,
                (
                    f"{_rid}copy-backup source_id={config.source_id} copy_start "
                    f"file={source_file} target={target_posix} size_bytes={source_size} "
                    f"mode={'replace' if is_replace else 'new'}"
                ),
            )
            parent = str(PurePosixPath(target_posix).parent)
            remote.mkdirs(parent)
            started = time.monotonic()
            try:
                src_mtime = source_file.stat().st_mtime
                if is_replace:
                    # Stage to a temp name in the destination dir, then atomically
                    # move over the old file so a valid backup is never left partial.
                    staged_posix = f"{target_posix}.dbops_partial"
                    remote.put(source_file, staged_posix)
                    try:
                        remote.set_mtime(staged_posix, src_mtime)
                    except OSError:
                        pass
                    remote.rename(staged_posix, target_posix)
                    status = "REPLACED"
                else:
                    remote.put(source_file, target_posix)
                    # Preserve the source mtime so the restore's log-chain filter
                    # (which compares log vs full backup file times) works on Linux.
                    try:
                        remote.set_mtime(target_posix, src_mtime)
                    except OSError:
                        pass
                    status = "COPIED"
                duration_seconds = time.monotonic() - started
                results.append(CopyBackupFileResult(
                    source_file=source_file,
                    target_file=Path(target_posix),
                    status=status,
                    bytes=source_size,
                ))
                _log_progress(
                    logger,
                    (
                        f"{_rid}copy-backup source_id={config.source_id} copy_done "
                        f"file={source_file} target={target_posix} size_bytes={source_size} "
                        f"status={status} duration_seconds={duration_seconds:.3f}"
                    ),
                )
            except Exception as exc:
                results.append(CopyBackupFileResult(
                    source_file=source_file,
                    target_file=Path(target_posix),
                    status="FAILED",
                    bytes=source_size,
                ))
                _log_progress(
                    logger,
                    f"{_rid}copy-backup source_id={config.source_id} copy_failed file={source_file} error={_format_log_value(exc)}",
                )
    return tuple(results)


def _format_log_value(value: object) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "/")




def _running_on_linux() -> bool:
    return os.name != "nt"


def _is_unc_share(path: Path) -> bool:
    text = str(path)
    return text.startswith("\\\\") or text.startswith("//")


def _parse_unc_share(unc: Path) -> tuple[str, str, str]:
    """Split \\\\host\\share\\sub\\dir into (host, share, subpath_posix)."""
    parts = [segment for segment in str(unc).replace("\\", "/").split("/") if segment]
    if len(parts) < 2:
        raise RuntimeError(f"Invalid SMB share path: {unc}")
    return parts[0], parts[1], "/".join(parts[2:])


# Timeout for an smbclient bulk download (staging recent backups). Generous because
# a full-instance recurse can be many GB; bounded so a hung smbclient still fails.
# (The short remote_command_timeout is for quick SSH commands, not this transfer.)
_SMB_DOWNLOAD_TIMEOUT_SECONDS = 3600
# Quick directory listing (size validation), not a bulk transfer.
_SMB_LIST_TIMEOUT_SECONDS = 600
# Targeted re-fetch attempts for a backup that downloaded short (partial/in-progress).
_SMB_MAX_DOWNLOAD_ATTEMPTS = 3



@dataclasses.dataclass(frozen=True)
class _RemoteBackup:
    relative_path: str
    size_bytes: int
    backup_timestamp: float | None


def _share_login(config: BackupRestoreConfig) -> tuple[str, str]:
    """The source share's login, the password resolved here - ``common`` reads no configuration."""
    password = resolve_password_ref(config.prod_smb_password_env) if config.prod_smb_password_env else ""
    return config.prod_smb_username or "", password


def _list_remote_backups(config: BackupRestoreConfig, host: str, share_name: str,
                         remote_dir: str) -> list[_RemoteBackup]:
    """Every ``.bak`` / ``.trn`` under ``remote_dir`` on the share, relative to it (``smb-list``)."""
    from db_ops.backup_restore import share

    username, password = _share_login(config)
    unc = _unc_of(host, share_name, remote_dir)
    try:
        files = share.list_files(unc, username=username, password=password, recurse=True,
                                 suffixes=(".bak", ".trn"), timeout_seconds=_SMB_LIST_TIMEOUT_SECONDS)
    except share.ShareError as exc:
        raise RuntimeError(f"smbclient list failed for //{host}/{share_name}/{remote_dir}: {exc}") from exc
    return [_RemoteBackup(relative_path=str(item["path"]), size_bytes=int(item["size_bytes"]),
                          backup_timestamp=_backup_time_from_name(str(item["name"])))
            for item in files]


def _get_remote_backup(config: BackupRestoreConfig, host: str, share_name: str, remote_path: str,
                       local_target: Path) -> None:
    """One backup from the share to ``local_target`` (``smb-get``); its size is checked by the caller."""
    from db_ops.backup_restore import share

    username, password = _share_login(config)
    share.get_file(_unc_of(host, share_name), remote_path, local_target, username=username,
                   password=password, timeout_seconds=_SMB_DOWNLOAD_TIMEOUT_SECONDS)


def _unc_of(host: str, share_name: str, subpath: str = "") -> str:
    """``\\\\host\\share[\\subpath]`` - how a share is named to ``smb-list`` / ``smb-get``."""
    return "\\\\" + host + "\\" + share_name + ("\\" + subpath if subpath else "")


def _remote_backup_matches_window(
    backup: _RemoteBackup,
    *,
    cutoff: float | None,
    end_ts: float | None,
    require_timestamp: bool,
) -> tuple[bool, str]:
    if backup.backup_timestamp is None:
        return (False, "missing_backup_timestamp") if require_timestamp else (True, "no_timestamp_available")
    if cutoff is not None and backup.backup_timestamp < cutoff:
        return False, "before_copy_window"
    if end_ts is not None and backup.backup_timestamp > end_ts:
        return False, "after_copy_window"
    return True, "selected"


def _selected_remote_backups(
    backups: list[_RemoteBackup],
    *,
    cutoff: float | None,
    end_ts: float | None,
    patterns: tuple[str, ...],
) -> tuple[list[_RemoteBackup], list[tuple[_RemoteBackup, str]]]:
    selected: list[_RemoteBackup] = []
    skipped: list[tuple[_RemoteBackup, str]] = []
    require_timestamp = cutoff is not None or end_ts is not None
    for backup in sorted(backups, key=lambda item: (item.backup_timestamp or 0.0, item.relative_path.lower())):
        path = PurePosixPath(backup.relative_path.replace("\\", "/"))
        if not any(path.match(pattern) for pattern in patterns):
            skipped.append((backup, "pattern_mismatch"))
            continue
        matched, reason = _remote_backup_matches_window(
            backup,
            cutoff=cutoff,
            end_ts=end_ts,
            require_timestamp=require_timestamp,
        )
        if matched:
            selected.append(backup)
        else:
            skipped.append((backup, reason))
    return selected, skipped


def _backup_time_from_name(name: str) -> float | None:
    """Parse the backup time encoded in a file name (..._YYYYMMDD_HHMMSS[Z].bak/.trn)."""
    return backup_time_from_name(name)


def _format_copy_window(config: BackupRestoreConfig) -> str:
    start_ts, end_ts = _copy_window_timestamps(config)
    start = dt.datetime.fromtimestamp(start_ts, tz=dt.timezone.utc).isoformat() if start_ts is not None else "unbounded"
    end = dt.datetime.fromtimestamp(end_ts, tz=dt.timezone.utc).isoformat() if end_ts is not None else "unbounded"
    return f"window_start_utc={start} window_end_utc={end}"


def _remote_destination_sizes(config: BackupRestoreConfig, *, logger: logging.Logger | None) -> dict[str, int]:
    """Return ``{relative_posix_path_lower: size_bytes}`` for files already present in the
    final import destination (``config.vm_import_unc``) on the Linux target, fetched once
    via a single SSH ``find``. Relative paths are POSIX and relative to ``vm_import_unc``,
    matching the staging/sftp layout, so the smbclient staging step can decide — BEFORE
    transferring anything — whether a backup is already imported. Returns ``{}`` when the
    destination does not exist yet or the listing fails (callers then fall back to copying)."""
    dest_root = str(config.vm_import_unc).replace("\\", "/").rstrip("/")
    sizes: dict[str, int] = {}
    try:
        with open_ssh_connection(config) as ssh:
            answer = ssh.run(
                f'find {shlex.quote(dest_root)} -type f -printf "%s %p\\n" 2>/dev/null || true'
            )
            lines = answer.stdout.splitlines()
    except Exception as exc:  # noqa: BLE001 - missing destination must not abort the copy.
        _log_progress(
            logger,
            f"copy-backup source_id={config.source_id} destination_scan_skipped reason={_format_log_value(exc)}",
        )
        return {}
    prefix = dest_root + "/"
    for line in lines:
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        try:
            size = int(parts[0])
        except ValueError:
            continue
        path = parts[1].strip()
        rel = path[len(prefix):] if path.startswith(prefix) else path
        sizes[rel.replace("\\", "/").lower()] = size
    return sizes


def _smbclient_download_selected_to_staging(
    config: BackupRestoreConfig,
    *,
    logger: logging.Logger | None,
    force: bool = False,
) -> tuple[Path, list[CopyBackupFileResult], int]:
    """List first, filter remote backups, then download only selected files one by one.

    Files that already exist in the FINAL destination (``vm_import_unc`` on the target)
    with a matching size are skipped at the source — never downloaded from SMB — so a
    re-run only transfers missing or changed backups. Returns
    ``(staging_dir, pre_skipped_results, total_selected)`` where ``pre_skipped_results``
    are the destination-existing files (status ``SKIPPED_EXISTS``) and ``total_selected``
    is the count of files matching the copy window (skipped or not)."""
    host, share, subpath = _parse_unc_share(config.prod_backup_share)
    staging = Path(tempfile.mkdtemp(prefix=f"db_ops_smb_{config.source_id or 'src'}_"))
    cutoff, end_ts = _copy_window_timestamps(config)
    _rid = f"restore_id={config.restore_id} " if config.restore_id else ""
    base = subpath.replace("/", "\\") if subpath else ""
    db_names = [mapping.source_database for mapping in config.databases] if config.databases else [None]
    total_selected = 0
    pre_skipped: list[CopyBackupFileResult] = []
    dest_sizes = {} if force else _remote_destination_sizes(config, logger=logger)
    linux_import = str(config.vm_import_unc).replace("\\", "/").rstrip("/")
    try:
        for db in db_names:
            remote_dir = f"{base}\\{db}" if (base and db) else (db or base)
            local_dir = (staging / db) if db else staging
            local_dir.mkdir(parents=True, exist_ok=True)
            _log_progress(
                logger,
                (
                    f"{_rid}copy-backup source_id={config.source_id} smbclient_list_start "
                    f"remote={remote_dir or '/'} timeout_seconds={_SMB_LIST_TIMEOUT_SECONDS} {_format_copy_window(config)}"
                ),
            )
            remote_backups = _list_remote_backups(config, host, share, remote_dir)
            selected, skipped = _selected_remote_backups(
                remote_backups,
                cutoff=cutoff,
                end_ts=end_ts,
                patterns=config.copy_file_patterns,
            )
            total_selected += len(selected)
            _log_progress(
                logger,
                (
                    f"{_rid}copy-backup source_id={config.source_id} smbclient_list_done "
                    f"remote={remote_dir or '/'} scanned_files={len(remote_backups)} "
                    f"selected_files={len(selected)} skipped_files={len(skipped)}"
                ),
            )
            for backup, reason in skipped:
                _log_progress(
                    logger,
                    f"{_rid}copy-backup source_id={config.source_id} smbclient_skip file={backup.relative_path} reason={reason}",
                )
            for index, backup in enumerate(selected, start=1):
                remote_path = f"{remote_dir}\\{backup.relative_path}" if remote_dir else backup.relative_path
                local_target = local_dir / Path(backup.relative_path.replace("\\", "/"))
                # Destination-relative path (POSIX) mirrors the sftp target layout:
                # <db>/<relative> when databases are configured, else just <relative>.
                rel_backup = backup.relative_path.replace("\\", "/")
                dest_rel = f"{db}/{rel_backup}" if db else rel_backup
                dest_target = f"{linux_import}/{dest_rel}"
                existing_dest = dest_sizes.get(dest_rel.lower())
                if not force and existing_dest is not None and existing_dest == backup.size_bytes:
                    pre_skipped.append(CopyBackupFileResult(
                        source_file=Path(dest_target),
                        target_file=Path(dest_target),
                        status="SKIPPED_EXISTS",
                        bytes=backup.size_bytes,
                    ))
                    _log_progress(
                        logger,
                        (
                            f"{_rid}copy-backup source_id={config.source_id} copy_skip_existing "
                            f"file={backup.relative_path} target={dest_target} size_bytes={backup.size_bytes} "
                            f"reason=destination_same_size sequence={index} total={len(selected)}"
                        ),
                    )
                    continue
                if not force and existing_dest is not None:
                    _log_progress(
                        logger,
                        (
                            f"{_rid}copy-backup source_id={config.source_id} copy_replace_size_mismatch "
                            f"file={backup.relative_path} target={dest_target} source_bytes={backup.size_bytes} "
                            f"destination_bytes={existing_dest} action=download_then_atomic_move"
                        ),
                    )
                _log_progress(
                    logger,
                    (
                        f"{_rid}copy-backup source_id={config.source_id} smbclient_download_start "
                        f"file={backup.relative_path} remote={remote_path} local={local_target} "
                        f"size_bytes={backup.size_bytes} sequence={index} total={len(selected)} "
                        f"timeout_seconds={_SMB_DOWNLOAD_TIMEOUT_SECONDS}"
                    ),
                )
                started = time.monotonic()
                try:
                    _get_remote_backup(config, host, share, remote_path, local_target)
                except Exception as exc:
                    raise RuntimeError(f"smbclient download failed file={backup.relative_path}: {exc}") from exc
                actual_size = local_target.stat().st_size if local_target.exists() else -1
                if actual_size != backup.size_bytes:
                    local_target.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"smbclient download size mismatch file={backup.relative_path} "
                        f"expected_bytes={backup.size_bytes} actual_bytes={actual_size}"
                    )
                if backup.backup_timestamp is not None:
                    os.utime(local_target, (backup.backup_timestamp, backup.backup_timestamp))
                _log_progress(
                    logger,
                    (
                        f"{_rid}copy-backup source_id={config.source_id} smbclient_download_done "
                        f"file={backup.relative_path} local={local_target} size_bytes={actual_size} "
                        f"duration_seconds={time.monotonic() - started:.3f}"
                    ),
                )
    except Exception:
        # A failed staging leaves nothing behind: it used to leave the staging folder AND the
        # login file, which now lives and dies inside common.cli smb-list / smb-get.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if total_selected == 0:
        raise RuntimeError(
            f"smbclient source scan selected no files from //{host}/{share} "
            f"{_format_copy_window(config)} patterns={','.join(config.copy_file_patterns)}"
            + newest_backup_hint(config)
        )
    _log_progress(
        logger,
        (
            f"{_rid}copy-backup source_id={config.source_id} smbclient_stage_summary "
            f"selected={total_selected} skipped_existing={len(pre_skipped)} "
            f"to_transfer={total_selected - len(pre_skipped)}"
        ),
    )
    return staging, pre_skipped, total_selected


def run_copy_backup(
    config: BackupRestoreConfig | None = None,
    *,
    logger: logging.Logger | None = None,
    force: bool = False,
) -> CopyBackupResult:
    restore_config = config or load_restore_config()
    _validate_unc_source(restore_config.prod_backup_share)
    if not restore_config.is_linux:
        _validate_unc_source(restore_config.vm_import_unc)
    started = time.monotonic()
    found_count = 0
    file_results_list: list[CopyBackupFileResult] = []
    failed_before_copy = 0
    _rid = f"restore_id={restore_config.restore_id} " if restore_config.restore_id else ""
    try:
        _log_progress(
            logger,
            (
                f"{_rid}copy-backup source_id={restore_config.source_id} start "
                f"source={restore_config.prod_backup_share} target={restore_config.vm_import_unc} "
                f"hours={restore_config.copy_recent_hours} patterns={','.join(restore_config.copy_file_patterns)} "
                f"platform={restore_config.vm_platform} force={force} {_format_copy_window(restore_config)}"
            ),
        )
        _log_progress(
            logger,
            (
                f"{_rid}copy-backup source_id={restore_config.source_id} scan_filter "
                f"hours={restore_config.copy_recent_hours} patterns={','.join(restore_config.copy_file_patterns)} "
                f"{_format_copy_window(restore_config)}"
            ),
        )
        credential_requests = copy_share_login_requests(restore_config)
        _log_progress(logger, f"{_rid}copy-backup source_id={restore_config.source_id} smb_credential_commands={len(credential_requests)}")
        # A stored login is Windows-only. On Linux every smb-list / smb-get carries the login
        # itself (smbclient), and the target copy goes over SSH, so there is nothing to store.
        if not _running_on_linux():
            store_share_logins(credential_requests)

        copy_engine = (
            "smbclient"
            if restore_config.is_linux and _running_on_linux() and _is_unc_share(restore_config.prod_backup_share)
            else "sftp" if restore_config.is_linux
            else "share" if should_list_the_share(restore_config)
            else "python"
        )
        _log_progress(logger, f"{_rid}copy-backup source_id={restore_config.source_id} scanning engine={copy_engine}")

        if copy_engine == "smbclient":
            # db_ops runs on Linux and cannot read the Windows UNC share directly:
            # list/filter/download files locally via smbclient, then sftp them to the target.
            # Files already present in the final destination are skipped at the source and
            # never downloaded; pre_skipped carries them into the summary.
            staging, pre_skipped, total_selected = _smbclient_download_selected_to_staging(
                restore_config, logger=logger, force=force
            )
            try:
                selected_files = _scan_backup_files(
                    source_dir=staging,
                    patterns=restore_config.copy_file_patterns,
                    cutoff=restore_config.copy_window_start_utc.timestamp() if restore_config.copy_window_start_utc else None,
                    end_ts=restore_config.copy_window_end_utc.timestamp() if restore_config.copy_window_end_utc else None,
                )
                found_count = total_selected
                reason = " reason=no_matching_files" if found_count == 0 else ""
                _log_progress(
                    logger,
                    (
                        f"{_rid}copy-backup source_id={restore_config.source_id} scan_completed "
                        f"found_files={found_count} skipped_existing={len(pre_skipped)} "
                        f"staged_files={len(selected_files)}{reason}"
                    ),
                )
                file_results_list = list(pre_skipped)
                file_results_list.extend(_copy_backup_files_via_sftp(
                    restore_config, selected_files, logger=logger, force=force, source_root=staging
                ))
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        elif copy_engine == "sftp":
            selected_files = list_recent_backup_files(restore_config)
            found_count = len(selected_files)
            reason = " reason=no_matching_files" if found_count == 0 else ""
            _log_progress(logger, f"{_rid}copy-backup source_id={restore_config.source_id} scan_completed found_files={found_count}{reason}")
            file_results_list = list(_copy_backup_files_via_sftp(restore_config, selected_files, logger=logger, force=force))
        else:
            selected_files = (
                list_recent_backup_files_on_share(restore_config)
                if copy_engine == "share"
                else list_recent_backup_files(restore_config)
            )
            found_count = len(selected_files)
            reason = " reason=no_matching_files" if found_count == 0 else ""
            _log_progress(logger, f"{_rid}copy-backup source_id={restore_config.source_id} scan_completed found_files={found_count}{reason}")
            for source_file in selected_files:
                result = copy_backup_file_with_logging(
                    source_file,
                    source_root=restore_config.prod_backup_share,
                    target_root=restore_config.vm_import_unc,
                    source_id=restore_config.source_id,
                    restore_id=restore_config.restore_id,
                    logger=logger,
                    force=force,
                )
                file_results_list.append(result)

        file_results = tuple(file_results_list)
        _write_copy_log(restore_config, file_results)
    except Exception as exc:
        failed_before_copy = 1
        _log_progress(
            logger,
            f"{_rid}copy-backup source_id={restore_config.source_id} exit_early reason={_format_log_value(exc)}",
        )
        raise
    finally:
        duration_seconds = time.monotonic() - started
        copied_count = sum(1 for item in file_results_list if item.status == "COPIED")
        replaced_count = sum(1 for item in file_results_list if item.status == "REPLACED")
        skipped_existing_count = sum(1 for item in file_results_list if item.status == "SKIPPED_EXISTS")
        failed_count = failed_before_copy + sum(1 for item in file_results_list if item.status == "FAILED")
        _log_progress(
            logger,
            (
                f"{_rid}copy-backup source_id={restore_config.source_id} completed "
                f"found_count={found_count} copied_count={copied_count} "
                f"skipped_existing_count={skipped_existing_count} replaced_count={replaced_count} "
                f"failed_count={failed_count} duration_seconds={duration_seconds:.3f}"
            ),
        )
    file_results = tuple(file_results_list)
    failed_count = sum(1 for item in file_results if item.status == "FAILED")
    return CopyBackupResult(
        returncode=1 if failed_count else 0,
        source_backup_dir=restore_config.prod_backup_share,
        local_import_dir=restore_config.vm_import_unc,
        files_considered=found_count,
        copied=sum(1 for item in file_results if item.status in ("COPIED", "REPLACED")),
        skipped=sum(1 for item in file_results if item.status == "SKIPPED_EXISTS"),
        file_results=file_results,
        failed=failed_count,
        replaced=sum(1 for item in file_results if item.status == "REPLACED"),
    )


def _write_copy_log(config: BackupRestoreConfig, file_results: tuple[CopyBackupFileResult, ...]) -> None:
    if not config.robocopy_log_path or config.is_linux:
        return
    config.robocopy_log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"copy_recent_hours={config.copy_recent_hours}",
        f"copy_file_patterns={','.join(config.copy_file_patterns)}",
    ]
    lines.extend(f"{item.status}|{item.bytes}|{item.source_file}|{item.target_file}" for item in file_results)
    with config.robocopy_log_path.open("a", encoding="utf-8") as file:
        file.write("\n".join(lines))
        file.write("\n")




def _validate_unc_source(source: object) -> None:
    text = str(source)
    if not text.startswith("\\\\"):
        raise ValueError(
            "source_backup_dir must be a UNC SMB path like \\\\server\\SQLBK. "
            "Mapped drives such as Z: are not safe for scheduled automation."
        )
