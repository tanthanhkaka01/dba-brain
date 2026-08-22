from __future__ import annotations
from db_ops.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, _log_progress, _write_temp_powershell_script  # noqa: F401 - one definition

import dataclasses
import datetime as dt
import json
import logging
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

from db_ops.backup_restore.config import BackupRestoreConfig, load_restore_config
from db_ops.common import data_sources, remote_exec
from db_ops.lib.shell import powershell_executable
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


def build_cmdkey_command(*, credential_target: str, username: str, password_env: str) -> list[str] | None:
    if not credential_target and not username and not password_env:
        return None
    if not credential_target or not username or not password_env:
        raise ValueError("credential_target, username, and password_env are all required when credential setup is enabled.")
    password = resolve_password_ref(password_env)
    if not password:
        raise RuntimeError(f"Password ref not found in environment or secret_text.json: {password_env}")
    return [
        "cmdkey",
        f"/add:{credential_target}",
        f"/user:{username}",
        f"/pass:{password}",
    ]


def resolve_password_ref(password_ref: str) -> str:
    env_value = os.getenv(password_ref, "").strip()
    if env_value:
        return env_value
    secrets = data_sources.load_secret_text()
    return str(secrets.get(password_ref, "")).strip()


def build_copy_cmdkey_commands(config: BackupRestoreConfig) -> list[list[str]]:
    commands = []
    prod_cmd = build_cmdkey_command(
        credential_target=config.prod_smb_credential_target,
        username=config.prod_smb_username,
        password_env=config.prod_smb_password_env,
    )
    if prod_cmd:
        commands.append(prod_cmd)
    if not config.is_linux:
        vm_cmd = build_cmdkey_command(
            credential_target=config.vm_credential_target,
            username=config.vm_username,
            password_env=config.vm_password_env,
        )
        if vm_cmd:
            commands.append(vm_cmd)
    return commands


def should_use_powershell_unc_copy(config: BackupRestoreConfig) -> bool:
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


def list_recent_backup_files(config: BackupRestoreConfig | None = None, *, now: float | None = None) -> list[Path]:
    restore_config = config or load_restore_config()
    cutoff, end_ts = _copy_window_timestamps(restore_config, now=now)
    return _scan_backup_files(
        source_dir=restore_config.source_backup_dir,
        patterns=restore_config.copy_file_patterns,
        cutoff=cutoff,
        end_ts=end_ts,
    )


def _copy_window_timestamps(config: BackupRestoreConfig, *, now: float | None = None) -> tuple[float | None, float | None]:
    start_ts = config.copy_window_start_utc.timestamp() if config.copy_window_start_utc is not None else None
    end_ts = config.copy_window_end_utc.timestamp() if config.copy_window_end_utc is not None else None
    if start_ts is None and config.copy_recent_hours > 0:
        start_ts = (time.time() if now is None else now) - (config.copy_recent_hours * 60 * 60)
    return start_ts, end_ts


def _scan_backup_files(*, source_dir: Path, patterns: tuple[str, ...], cutoff: float | None, end_ts: float | None = None) -> list[Path]:
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
    return [path for _, path in sorted(files.values(), key=lambda item: (item[0], str(item[1]).lower()))]


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


def list_recent_backup_files_with_powershell(
    config: BackupRestoreConfig,
    *,
    now: float | None = None,
) -> list[Path]:
    cutoff_iso = ""
    cutoff_ts, end_ts = _copy_window_timestamps(config, now=now)
    if cutoff_ts is not None:
        cutoff_iso = dt.datetime.fromtimestamp(cutoff_ts, tz=dt.timezone.utc).isoformat()
    end_iso = "" if end_ts is None else dt.datetime.fromtimestamp(end_ts, tz=dt.timezone.utc).isoformat()
    script = r"""
param(
    [string] $SourceRoot,
    [string] $PatternsJson,
    [string] $CutoffIso,
    [string] $EndIso
)
$ErrorActionPreference = 'Stop'
$patterns = [string[]] @(ConvertFrom-Json -InputObject $PatternsJson)
$cutoff = $null
if ($CutoffIso) {
    $cutoff = [DateTimeOffset]::Parse($CutoffIso).UtcDateTime
}
$end = $null
if ($EndIso) {
    $end = [DateTimeOffset]::Parse($EndIso).UtcDateTime
}
$allFiles = Get-ChildItem -LiteralPath $SourceRoot -Recurse -File -Include $patterns
$files = New-Object System.Collections.Generic.List[object]
foreach ($file in @($allFiles | Sort-Object LastWriteTimeUtc, FullName)) {
    if ($cutoff -ne $null -and $file.LastWriteTimeUtc -lt $cutoff) {
        continue
    }
    if ($end -ne $null -and $file.LastWriteTimeUtc -gt $end) {
        continue
    }
    $files.Add([pscustomobject]@{
        full_name = $file.FullName
        last_write_time_utc = $file.LastWriteTimeUtc
    }) | Out-Null
}
$files | ConvertTo-Json -Depth 4 -Compress
""".strip()
    script_path = _write_temp_powershell_script(script)
    cmd = [
        powershell_executable(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        str(config.prod_backup_share),
        json.dumps(list(config.copy_file_patterns)),
        cutoff_iso,
        end_iso,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, check=False, text=True)
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        details = [f"PowerShell backup scan failed with exit code {result.returncode}."]
        if result.stdout.strip():
            details.append(f"stdout:\n{result.stdout.strip()}")
        if result.stderr.strip():
            details.append(f"stderr:\n{result.stderr.strip()}")
        raise RuntimeError("\n".join(details))
    raw_output = result.stdout.strip()
    if not raw_output:
        return []
    payload = json.loads(raw_output)
    rows = payload if isinstance(payload, list) else [payload]
    return [Path(str(item["full_name"])) for item in rows if isinstance(item, dict) and item.get("full_name")]


def copy_recent_backup_files_with_powershell(
    config: BackupRestoreConfig,
    *,
    now: float | None = None,
) -> tuple[CopyBackupFileResult, ...]:
    selected_files = list_recent_backup_files_with_powershell(config, now=now)
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


def ssh_access_json(config: BackupRestoreConfig) -> dict[str, object]:
    """The restore target as the JSON access object ``db_ops.common.remote_exec`` takes.

    One place turns a ``BackupRestoreConfig`` into remote-access inputs, so the SSH
    details of a Linux restore target are described once rather than at each call site.
    """
    return {
        "method": "ssh",
        "host": config.vm_credential_target,
        "username": config.vm_username,
        "platform": "linux",
        "auth_type": "password" if config.vm_password_env else "key",
        "password_ref": config.vm_password_env or "",
        "timeout_seconds": config.remote_command_timeout_seconds or remote_exec.DEFAULT_SESSION_TIMEOUT_SECONDS,
    }


def open_ssh_connection(config: BackupRestoreConfig):
    """Return an open paramiko SSHClient for the VM target. Caller must close it.

    The connect goes through ``common.remote_exec``; a raw paramiko client comes back
    because the restore paths use SFTP and incremental channel reads directly on it.
    """
    if not config.is_linux:
        raise RuntimeError(
            f"Target context mismatch: restore_id={config.restore_id} target_host={config.vm_credential_target} "
            "target_os_type=windows cannot execute remote_exec_type=ssh."
        )
    session = remote_exec.open_session(ssh_access_json(config))
    try:
        return session.client
    except remote_exec.RemoteExecError as exc:
        raise RuntimeError(str(exc)) from exc


def _sftp_makedirs(sftp, path: str) -> None:
    parts = PurePosixPath(path).parts
    current = ""
    for part in parts:
        current = str(PurePosixPath(current) / part) if current else part
        try:
            sftp.mkdir(current)
        except OSError:
            pass


def _prepare_linux_base_import_dir(ssh, config: BackupRestoreConfig) -> None:
    """Ensure the Linux import base dir exists and is traversable+writable by the SSH user."""
    linux_import = str(config.vm_import_unc).replace("\\", "/")
    username = config.vm_username
    # Fast path: plain mkdir (works if parent chain is already writable by this user)
    _, out, _ = ssh.exec_command(f"mkdir -p {shlex.quote(linux_import)}")
    out.read()  # drain
    if out.channel.recv_exit_status() == 0:
        return
    # Need sudo. Also fix ancestor dirs that might block traverse (e.g. /var/opt/mssql/backup owned by mssql).
    ancestors = []
    current = PurePosixPath("/")
    for part in PurePosixPath(linux_import).parts[1:]:  # skip root '/'
        current = current / part
        ancestors.append(str(current))
    # chmod o+x on every ancestor except the leaf (which we chown to tuser)
    ancestor_dirs = ancestors[:-1]
    chmod_part = ""
    if ancestor_dirs:
        chmod_part = "sudo chmod o+x " + " ".join(shlex.quote(d) for d in ancestor_dirs) + " && "
    cmd = (
        f"sudo mkdir -p {shlex.quote(linux_import)} && "
        f"{chmod_part}"
        f"sudo chown {shlex.quote(username)} {shlex.quote(linux_import)}"
    )
    _, out, err = ssh.exec_command(cmd)
    stdout_data = out.read().decode("utf-8", errors="replace")
    stderr_data = err.read().decode("utf-8", errors="replace")
    rc = out.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(
            f"Could not prepare Linux import directory {linux_import}: "
            f"{stderr_data.strip() or stdout_data.strip()}"
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

    with open_ssh_connection(config) as ssh:
        _prepare_linux_base_import_dir(ssh, config)
        with ssh.open_sftp() as sftp:
            for source_file in selected_files:
                relative_parts = source_file.relative_to(root).parts
                target_posix = str(linux_import.joinpath(*relative_parts))
                source_size = source_file.stat().st_size
                # Inspect the FINAL destination (not just the temp/staging dir) so an
                # already-imported backup of the same size is never re-transferred, and a
                # changed one is replaced atomically rather than overwritten in place.
                existing_size: int | None = None
                try:
                    existing_size = sftp.stat(target_posix).st_size
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
                _sftp_makedirs(sftp, parent)
                started = time.monotonic()
                try:
                    src_mtime = source_file.stat().st_mtime
                    if is_replace:
                        # Stage to a temp name in the destination dir, then atomically
                        # move over the old file so a valid backup is never left partial.
                        staged_posix = f"{target_posix}.dbops_partial"
                        sftp.put(str(source_file), staged_posix)
                        try:
                            sftp.utime(staged_posix, (src_mtime, src_mtime))
                        except OSError:
                            pass
                        _sftp_atomic_replace(sftp, staged_posix, target_posix)
                        status = "REPLACED"
                    else:
                        sftp.put(str(source_file), target_posix)
                        # Preserve the source mtime so the restore's log-chain filter
                        # (which compares log vs full backup file times) works on Linux.
                        try:
                            sftp.utime(target_posix, (src_mtime, src_mtime))
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


def _sftp_atomic_replace(sftp, staged_posix: str, target_posix: str) -> None:
    """Atomically move ``staged_posix`` onto ``target_posix`` on the SFTP server.

    Prefers the OpenSSH ``posix-rename`` extension (overwrites in one syscall); falls
    back to remove+rename when the server lacks it."""
    try:
        sftp.posix_rename(staged_posix, target_posix)
        return
    except (IOError, OSError, AttributeError):
        pass
    try:
        sftp.remove(target_posix)
    except (IOError, OSError):
        pass
    sftp.rename(staged_posix, target_posix)


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


def _smbclient_auth_file(config: BackupRestoreConfig) -> Path:
    """Write an smbclient auth file so the password never appears in process args."""
    username = config.prod_smb_username or ""
    domain = ""
    for separator in ("\\", "/"):
        if separator in username:
            domain, username = username.split(separator, 1)
            break
    password = resolve_password_ref(config.prod_smb_password_env) if config.prod_smb_password_env else ""
    handle = tempfile.NamedTemporaryFile("w", suffix=".smbauth", delete=False, encoding="utf-8")
    with handle:
        handle.write(f"username = {username}\n")
        handle.write(f"password = {password}\n")
        if domain:
            handle.write(f"domain = {domain}\n")
    return Path(handle.name)


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


def _run_smbclient_command(args: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    start_new_session = os.name != "nt"
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=start_new_session,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if start_new_session:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
        else:
            proc.kill()
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(
            cmd=args,
            timeout=timeout_seconds,
            output=stdout,
            stderr=stderr,
        ) from exc
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def _smbclient_remote_backups(host: str, share: str, authfile: Path, remote_dir: str) -> dict[str, int]:
    """Authoritative ``{relative_path: size_bytes}`` for ``.bak``/``.trn`` files under
    ``remote_dir`` (recurse), via ``smbclient ls``. The relative path is backslash-separated
    and relative to ``remote_dir`` (e.g. ``APPDB_STG\\FULL\\x.bak``), mirroring the staging
    layout, so we can detect BOTH a truncated download (staged size < this) AND a file that
    is missing entirely (the recurse+mget aborted on an earlier file). smbclient's recurse
    exit code is unreliable, so completeness must be verified by listing, not the return code."""
    commands = ["recurse ON", "prompt OFF"]
    if remote_dir:
        commands.append(f'cd "{remote_dir}"')
    commands.append("ls")
    args = ["smbclient", f"//{host}/{share}", "-A", str(authfile), "-c", "; ".join(commands)]
    try:
        proc = _run_smbclient_command(args, timeout_seconds=_SMB_LIST_TIMEOUT_SECONDS)
    except (subprocess.SubprocessError, OSError):
        return {}
    return {item.relative_path: item.size_bytes for item in _parse_smbclient_ls(proc.stdout, remote_dir=remote_dir)}


def _parse_smbclient_ls(output: str, *, remote_dir: str) -> list[_RemoteBackup]:
    # `remote_dir` arrives POSIX-separated (`_parse_unc_share` returns `a/b/c`) while smbclient
    # prints its directory headers with backslashes (`\a\b\c\<db>\FULL`). Normalizing is not
    # cosmetic: the prefix below is what makes a path *relative*, and when it fails to match,
    # every file keeps its full sub-path and stages several directories too deep — where the
    # restore does not look, with no error anywhere.
    #
    # It went unnoticed because every share in use had a ONE-segment sub-path
    # (`\\host\SQLBK\APPDB-DB$APPDB`), which has no separator to disagree about. The first
    # multi-segment one (`\\host\D$\DBA\SqlBK\<instance>`, reaching a maintenance-plan tree that
    # has no share of its own) staged `DBA\SqlBK\<instance>\<db>\LOG\...` instead of
    # `<db>\LOG\...`.
    normalized = str(remote_dir or "").replace("/", "\\").strip("\\")
    prefix = f"\\{normalized}\\" if normalized else "\\"
    current_rel = ""  # directory (relative to remote_dir, backslash-separated) of subsequent entries
    backups: list[_RemoteBackup] = []
    for line in output.splitlines():
        if line.startswith("\\"):  # directory header, e.g. '\APPDB-DB$APPDB\APPDB_STG\FULL'
            header = line.rstrip()
            current_rel = header[len(prefix):] if header.startswith(prefix) else header.lstrip("\\")
            continue
        # entry: "  NAME            A   11121102848  Wed Jun 24 01:01:11 2026"
        match = re.match(r"\s+(\S+)\s+([AHSRDN]+)\s+(\d+)\s+\w{3}\s+\w{3}\s+", line)
        if not match:
            continue
        name, flags, size = match.group(1), match.group(2), int(match.group(3))
        if "D" in flags or not name.lower().endswith((".bak", ".trn")):
            continue
        relative_path = f"{current_rel}\\{name}" if current_rel else name
        backups.append(_RemoteBackup(
            relative_path=relative_path,
            size_bytes=size,
            backup_timestamp=_backup_time_from_name(name),
        ))
    return backups


def _pending_remote_backups(
    local_dir: Path, remote_backups: dict[str, int], cutoff: float | None
) -> list[tuple[str, int, int | None]]:
    """Recent remote backups (filename time >= ``cutoff``) not yet fully staged in
    ``local_dir`` — either missing entirely or truncated. Returns
    ``(relative_path, expected_bytes, staged_bytes_or_None)``."""
    pending: list[tuple[str, int, int | None]] = []
    for rel, expected in remote_backups.items():
        name = rel.rsplit("\\", 1)[-1]
        backup_time = _backup_time_from_name(name)
        if cutoff is not None and backup_time is not None and backup_time < cutoff:
            continue
        local = local_dir / Path(rel.replace("\\", "/"))
        if not local.exists():
            pending.append((rel, expected, None))
        elif local.stat().st_size < expected:
            pending.append((rel, expected, local.stat().st_size))
    return pending


def _smbclient_get_file(host: str, share: str, authfile: Path, remote_path: str,
                        local_parent: Path, local_name: str) -> None:
    """Re-download a single file with a targeted GET. ``remote_path`` is share-relative
    and backslash-separated; the file is written as ``local_parent/local_name``."""
    local_parent.mkdir(parents=True, exist_ok=True)
    commands = ["prompt OFF", f"lcd {local_parent}", f'get "{remote_path}" "{local_name}"']
    args = ["smbclient", f"//{host}/{share}", "-A", str(authfile), "-c", "; ".join(commands)]
    _run_smbclient_command(args, timeout_seconds=_SMB_DOWNLOAD_TIMEOUT_SECONDS)


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
    """Parse the backup time encoded in a file name (..._YYYYMMDD_HHMMSS.bak/.trn)."""
    match = _BACKUP_TIMESTAMP_RE.search(name)
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return None


def _format_copy_window(config: BackupRestoreConfig) -> str:
    start_ts, end_ts = _copy_window_timestamps(config)
    start = dt.datetime.fromtimestamp(start_ts, tz=dt.timezone.utc).isoformat() if start_ts is not None else "unbounded"
    end = dt.datetime.fromtimestamp(end_ts, tz=dt.timezone.utc).isoformat() if end_ts is not None else "unbounded"
    return f"window_start_utc={start} window_end_utc={end}"


def _smbclient_download_recent_to_staging(config: BackupRestoreConfig, *, logger: logging.Logger | None) -> Path:
    """Linux source read: download recent backup files from the Windows SMB share
    into a local staging dir via smbclient, preserving the per-database layout
    (<db>/FULL, <db>/LOG) so the restore step finds them. When specific databases
    are configured, only those subdirectories are fetched."""
    raise RuntimeError("Legacy recursive smbclient staging is disabled; use selected-file staging.")
    host, share, subpath = _parse_unc_share(config.prod_backup_share)
    staging = Path(tempfile.mkdtemp(prefix=f"db_ops_smb_{config.source_id or 'src'}_"))
    authfile = _smbclient_auth_file(config)
    timeref = staging / ".timeref"
    timeref.write_text("")
    cutoff, _end_ts = _copy_window_timestamps(config)
    if cutoff is not None:
        os.utime(timeref, (cutoff, cutoff))
    _rid = f"restore_id={config.restore_id} " if config.restore_id else ""
    base = subpath.replace("/", "\\") if subpath else ""
    db_names = [mapping.source_database for mapping in config.databases] if config.databases else [None]
    try:
        for db in db_names:
            remote_dir = f"{base}\\{db}" if (base and db) else (db or base)
            local_dir = (staging / db) if db else staging
            local_dir.mkdir(parents=True, exist_ok=True)
            commands = ["recurse ON", "prompt OFF", f"lcd {local_dir}"]
            if remote_dir:
                commands.append(f'cd "{remote_dir}"')
            if cutoff is not None:
                commands.append(f"newer {timeref}")
            # Legacy bulk download disabled: with recurse ON, smbclient
            # applies the mget mask to subdirectory names too, so a mask like
            # *.bak never descends into FULL/LOG. We pull everything recent, then
            # _scan_backup_files filters by copy_file_patterns locally.
            commands.append("ls")
            smb_args = ["smbclient", f"//{host}/{share}", "-A", str(authfile), "-c", "; ".join(commands)]
            _log_progress(logger, f"{_rid}copy-backup source_id={config.source_id} smbclient_download remote={remote_dir or '/'} local={local_dir}")
            # smbclient's exit code is unreliable with recurse+mget, so we validate
            # by checking the downloaded files below rather than the return code.
            # Bulk transfer: a full-instance recurse (databases=[]) can be many GB, so
            # this must NOT use the short remote_command_timeout (meant for quick SSH
            # commands) — that made restores time out at 60s.
            subprocess.run(
                smb_args, capture_output=True, text=True,
                timeout=_SMB_DOWNLOAD_TIMEOUT_SECONDS,
            )
            # A recurse+mget can silently FAIL PART-WAY: it truncates a file still being
            # written on the source (a FULL backup grabbed mid-write) AND, when it aborts on
            # that file, never reaches later databases — so files are either truncated or
            # missing entirely, and smbclient's exit code won't reveal it. Reconcile the
            # staging dir against the share's authoritative listing: re-fetch every recent
            # backup that is missing or short with a targeted GET, then drop any truncated
            # leftover that still cannot be completed so a bad .bak is never restored.
            remote_backups = _smbclient_remote_backups(host, share, authfile, remote_dir)
            if remote_backups:
                for attempt in range(1, _SMB_MAX_DOWNLOAD_ATTEMPTS + 1):
                    pending = _pending_remote_backups(local_dir, remote_backups, cutoff)
                    if not pending:
                        break
                    for rel, expected_bytes, staged_bytes in pending:
                        remote_path = f"{remote_dir}\\{rel}" if remote_dir else rel
                        local_target = local_dir / Path(rel.replace("\\", "/"))
                        _log_progress(
                            logger,
                            f"{_rid}copy-backup source_id={config.source_id} smbclient_refetch "
                            f"file={rel} staged_bytes={'missing' if staged_bytes is None else staged_bytes} "
                            f"expected_bytes={expected_bytes} attempt={attempt}",
                        )
                        _smbclient_get_file(host, share, authfile, remote_path, local_target.parent, local_target.name)
                for rel, expected_bytes, staged_bytes in _pending_remote_backups(local_dir, remote_backups, cutoff):
                    message = (
                        f"{_rid}copy-backup source_id={config.source_id} smbclient_incomplete "
                        f"file={rel} staged_bytes={'missing' if staged_bytes is None else staged_bytes} "
                        f"expected_bytes={expected_bytes} reason=download_unrecoverable"
                    )
                    if logger:
                        log_event(logger, level="critical", message=message)
                    print(message, flush=True)
                    local_target = local_dir / Path(rel.replace("\\", "/"))
                    if local_target.exists():  # truncated leftover — never restore a partial .bak
                        local_target.unlink(missing_ok=True)
    finally:
        try:
            authfile.unlink()
        except OSError:
            pass
        timeref.unlink(missing_ok=True)
    # smbclient does not preserve mtime, but the restore log-chain filter selects
    # logs by file time relative to the full backup. Recover each backup's real
    # time from its filename so that filter still excludes pre-full logs.
    for path in staging.rglob("*"):
        if path.is_file():
            mtime = _backup_time_from_name(path.name)
            if mtime is not None:
                os.utime(path, (mtime, mtime))
    if not any(path.is_file() for path in staging.rglob("*")):
        raise RuntimeError(
            f"smbclient download from //{host}/{share} returned no files "
            "(check source credentials, path, and copy_recent_hours)."
        )
    return staging


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
            _, stdout, _ = ssh.exec_command(
                f'find {shlex.quote(dest_root)} -type f -printf "%s %p\\n" 2>/dev/null || true'
            )
            lines = stdout.read().decode("utf-8", errors="replace").splitlines()
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
    authfile = _smbclient_auth_file(config)
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
            commands = ["recurse ON", "prompt OFF"]
            if remote_dir:
                commands.append(f'cd "{remote_dir}"')
            commands.append("ls")
            args = ["smbclient", f"//{host}/{share}", "-A", str(authfile), "-c", "; ".join(commands)]
            _log_progress(
                logger,
                (
                    f"{_rid}copy-backup source_id={config.source_id} smbclient_list_start "
                    f"remote={remote_dir or '/'} timeout_seconds={_SMB_LIST_TIMEOUT_SECONDS} {_format_copy_window(config)}"
                ),
            )
            proc = _run_smbclient_command(args, timeout_seconds=_SMB_LIST_TIMEOUT_SECONDS)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"smbclient list failed for //{host}/{share}/{remote_dir}: "
                    f"{proc.stderr.strip() or proc.stdout.strip() or f'exit_code={proc.returncode}'}"
                )
            remote_backups = _parse_smbclient_ls(proc.stdout, remote_dir=remote_dir)
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
                    _smbclient_get_file(host, share, authfile, remote_path, local_target.parent, local_target.name)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(
                        f"smbclient download timed out after {_SMB_DOWNLOAD_TIMEOUT_SECONDS}s file={backup.relative_path}"
                    ) from exc
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
    finally:
        try:
            authfile.unlink()
        except OSError:
            pass
    if total_selected == 0:
        raise RuntimeError(
            f"smbclient source scan selected no files from //{host}/{share} "
            f"{_format_copy_window(config)} patterns={','.join(config.copy_file_patterns)}"
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
        credential_commands = build_copy_cmdkey_commands(restore_config)
        _log_progress(logger, f"{_rid}copy-backup source_id={restore_config.source_id} smb_credential_commands={len(credential_commands)}")
        # cmdkey is Windows-only. On Linux, SMB auth is handled by smbclient (-A
        # authfile) and the target copy by SSH/sftp, so skip the cmdkey setup.
        if not _running_on_linux():
            for credential_cmd in credential_commands:
                subprocess.run(credential_cmd, check=True, text=True)

        copy_engine = (
            "smbclient"
            if restore_config.is_linux and _running_on_linux() and _is_unc_share(restore_config.prod_backup_share)
            else "sftp" if restore_config.is_linux
            else "powershell" if should_use_powershell_unc_copy(restore_config)
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
                list_recent_backup_files_with_powershell(restore_config)
                if copy_engine == "powershell"
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
