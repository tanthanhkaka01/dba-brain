from __future__ import annotations

import dataclasses
import os
import socket
import subprocess
import time
from pathlib import Path

from db_ops.backup_restore.config import BackupRestoreConfig
from db_ops.backup_restore.copy_backup import build_cmdkey_command, resolve_password_ref
from db_ops.common import remote_exec
from db_ops.lib.shell import POWERSHELL_NOT_FOUND_HINT, powershell_executable


class PreflightError(RuntimeError):
    """
    Raised when restore target preflight validation fails.

    The message is always actionable — it names the host, share, and steps to fix.
    It is intended to replace raw OS errors like '[WinError 53] The network path was not found'.
    """


def run_target_preflight(
    config: BackupRestoreConfig,
    *,
    logger: object | None = None,
) -> BackupRestoreConfig | None:
    """
    Validate (and where possible, prepare) the restore target before the workflow runs.

    Windows targets — SMB/UNC:
        1. Sets up SMB credentials via cmdkey.
        2. Checks whether the UNC share root (e.g. \\\\198.51.100.129\\SQLBK_IMPORT) is accessible.
        3. If not, attempts to create the local directory and SMB share on the remote host via
           PowerShell Invoke-Command (WinRM port 5985).
        4. If WinRM is unavailable, falls back to the Windows admin share (e.g. \\\\host\\C$)
           to create the import directory and returns a patched config with vm_import_unc pointing
           to the admin share path. The local path (vm_import_local) is unchanged so restore SQL
           still references the correct on-VM path.
        5. Raises PreflightError with a clear, actionable message if neither method succeeds.

    Linux targets — SSH/SFTP:
        No-op — returns None immediately. Directory creation on Linux is handled lazily inside
        _prepare_linux_base_import_dir() during run_copy_backup via sudo mkdir over SSH.

    Returns None to use the original config unchanged, or a BackupRestoreConfig with vm_import_unc
    replaced by the admin share fallback path when the named SMB share is inaccessible but the
    admin share is available.

    Only runs when os.name == 'nt' for Windows targets; skipped otherwise.
    """
    if config.is_linux:
        return None
    if os.name != "nt":
        return None
    return _run_windows_unc_preflight(config, logger=logger)


def _run_windows_unc_preflight(
    config: BackupRestoreConfig,
    *,
    logger: object | None = None,
) -> BackupRestoreConfig | None:
    host, share_name, unc_root = _parse_unc(config.vm_import_unc)
    local_root = _local_share_root(config.vm_import_unc, config.vm_import_local)

    _log(logger, f"preflight restore_id={config.restore_id} host={host} unc_root={unc_root}")

    # Step 1: Set up SMB credentials so Windows can authenticate to the share.
    _setup_smb_credentials(config, host=host, logger=logger)

    # Step 2: Check UNC share root accessibility.
    if _unc_accessible(unc_root):
        _log(logger, f"preflight restore_id={config.restore_id} unc_accessible=yes share={unc_root}")
        return None

    # Step 3: Share is not accessible. Attempt to create local dir + SMB share on the remote host
    # via PowerShell Invoke-Command (WinRM). This is the same remoting mechanism used by sqlcmd
    # execution in restore_database.py, so WinRM is already a pre-requisite for the workflow.
    _log(
        logger,
        (
            f"preflight restore_id={config.restore_id} unc_accessible=no share={unc_root} "
            f"attempting_create host={host} share_name={share_name} local_root={local_root}"
        ),
    )
    created, winrm_detail = _try_create_windows_share_via_remote_ps(
        config=config,
        host=host,
        local_dir=str(local_root),
        share_name=share_name,
    )

    if created:
        # Allow a moment for Windows to publish the new share.
        for _ in range(3):
            time.sleep(1)
            if _unc_accessible(unc_root):
                _log(
                    logger,
                    f"preflight restore_id={config.restore_id} unc_share_created=yes share={unc_root}",
                )
                return None
        winrm_detail = "share reported created but UNC still not accessible after 3 s"

    # Step 4: WinRM unavailable or failed. Fall back to admin share (\\host\C$) to create the
    # import directory directly. vm_import_local path is unchanged so restore SQL still works.
    _log(
        logger,
        (
            f"preflight restore_id={config.restore_id} winrm_result=failed detail={winrm_detail!r} "
            f"attempting_admin_share_fallback host={host}"
        ),
    )
    admin_override = _try_admin_share_fallback(config, host=host, logger=logger)
    if admin_override is not None:
        return admin_override

    # Step 5: Both WinRM and admin share failed. Raise with precise diagnostics.
    port_status = _check_smb_port_detail(host)
    try:
        drive_letter = config.vm_import_local.drive.rstrip(":").upper()
        admin_share_root = f"\\\\{host}\\{drive_letter}$"
    except Exception:
        admin_share_root = f"\\\\{host}\\C$"

    raise PreflightError(
        f"Windows import share not accessible — cannot start restore workflow.\n"
        f"\n"
        f"  restore_id    : {config.restore_id}\n"
        f"  target_host   : {host}\n"
        f"  unc_share     : {unc_root}\n"
        f"  local_path    : {local_root}\n"
        f"  smb_port_445  : {port_status}\n"
        f"  winrm_create  : {winrm_detail}\n"
        f"  admin_share   : not accessible ({admin_share_root} denied or unavailable)\n"
        f"\n"
        f"To fix manually — run on {host} as Administrator:\n"
        f"  New-Item -ItemType Directory -Path '{local_root}' -Force\n"
        f"  New-SmbShare -Name '{share_name}' -Path '{local_root}' -FullAccess 'Everyone'\n"
        f"\n"
        f"Also verify:\n"
        f"  - Port 445 (SMB) is open between this machine and {host}\n"
        f"  - Windows Firewall → 'File and Printer Sharing' is enabled on {host}\n"
        f"  - vm_username='{config.vm_username}' has Read+Write access to the share\n"
        f"  - WinRM (port 5985) is enabled on {host} for PowerShell remoting (required for sqlcmd too)"
    )


def _setup_smb_credentials(
    config: BackupRestoreConfig,
    *,
    host: str,
    logger: object | None,
) -> None:
    if not config.vm_credential_target or not config.vm_username or not config.vm_password_env:
        return
    try:
        cmd = build_cmdkey_command(
            credential_target=config.vm_credential_target,
            username=config.vm_username,
            password_env=config.vm_password_env,
        )
        if cmd:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            _log(logger, f"preflight restore_id={config.restore_id} smb_credentials=ok target={host}")
    except Exception as exc:
        raise PreflightError(
            f"Cannot set up SMB credentials for Windows restore target {host}.\n"
            f"  vm_username     : {config.vm_username}\n"
            f"  vm_password_env : {config.vm_password_env}\n"
            f"  detail          : {exc}"
        ) from exc


def _try_create_windows_share_via_remote_ps(
    config: BackupRestoreConfig,
    *,
    host: str,
    local_dir: str,
    share_name: str,
) -> tuple[bool, str]:
    """
    Create a local directory and SMB share on a remote Windows host using
    PowerShell Invoke-Command (WinRM). Returns (success, detail_message).

    WinRM must be enabled on the remote host (port 5985). This is the same
    remoting mechanism already used for sqlcmd execution in the restore step,
    so if WinRM works for restore it should work here too.
    """
    if not config.vm_username or not config.vm_password_env:
        return False, "vm_username or vm_password_env not set — cannot authenticate for remote share creation"

    password = resolve_password_ref(config.vm_password_env)
    if not password:
        return False, f"password not found for vm_password_env={config.vm_password_env}"

    # Remote script: create local dir if missing, create SMB share if missing.
    remote_script = (
        f"$d = {_ps_quote(local_dir)}; "
        f"$s = {_ps_quote(share_name)}; "
        f"if (!(Test-Path $d)) {{ New-Item -ItemType Directory -Path $d -Force | Out-Null }}; "
        f"if (!(Get-SmbShare -Name $s -ErrorAction SilentlyContinue)) "
        f"{{ New-SmbShare -Name $s -Path $d -FullAccess 'Everyone' | Out-Null }}; "
        f"Write-Output 'OK'"
    )
    # Transport, credential handling and the Invoke-Command wrapper are shared —
    # db_ops.common.remote_exec. Preflight only owns the script above and the verdict below.
    try:
        result = remote_exec.run_script(
            {
                "method": "winrm",
                "host": host,
                "username": config.vm_username,
                "password": password,
                "timeout_seconds": 60,
            },
            remote_script,
        )
    except remote_exec.RemoteTimeoutError:
        return False, f"PowerShell Invoke-Command timed out after 60 s (WinRM may be unavailable on {host})"
    except remote_exec.RemoteExecError as exc:
        detail = str(exc)
        if POWERSHELL_NOT_FOUND_HINT in detail:
            return False, POWERSHELL_NOT_FOUND_HINT
        return False, detail[:300]
    except Exception as exc:  # noqa: BLE001 - preflight reports, it never aborts the restore.
        return False, f"unexpected error: {exc}"

    if result.ok and "OK" in result.stdout:
        return True, "created via PowerShell Invoke-Command (WinRM)"
    detail = (result.stderr or result.stdout or "non-zero exit without output").strip()
    if "WinRM" in detail or "5985" in detail or "WSMan" in detail or "Access is denied" in detail:
        return False, (
            f"WinRM not reachable or Access Denied — enable WinRM on {host}: 'winrm quickconfig'. "
            f"Detail: {detail[:200]}"
        )
    return False, f"exit {result.exit_code}: {detail[:300]}"


def _try_admin_share_fallback(
    config: BackupRestoreConfig,
    *,
    host: str,
    logger: object | None = None,
) -> BackupRestoreConfig | None:
    """
    Fall back to the Windows admin share (e.g. \\\\host\\C$) when the named SMB share
    is missing and WinRM is unavailable.

    Creates the import directory via the admin share and returns a patched config with
    vm_import_unc replaced by the admin share path. vm_import_local is unchanged so
    restore SQL still references the correct on-VM local path.

    Returns None if the admin share is not accessible or directory creation fails.
    Scoped to this restore_id only — does not affect other targets.
    """
    try:
        admin_root, admin_unc = _derive_admin_share_paths(host, config.vm_import_local)
    except ValueError as exc:
        _log(logger, f"preflight restore_id={config.restore_id} admin_share_skipped reason={exc}")
        return None

    if not _unc_accessible(admin_root):
        _log(logger, f"preflight restore_id={config.restore_id} admin_share_accessible=no root={admin_root}")
        return None

    _log(logger, f"preflight restore_id={config.restore_id} admin_share_accessible=yes root={admin_root} creating={admin_unc}")

    try:
        Path(admin_unc).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log(logger, f"preflight restore_id={config.restore_id} admin_share_mkdir_failed error={exc}")
        return None

    test_file = Path(admin_unc) / ".db_ops_preflight_test"
    try:
        test_file.write_bytes(b"")
        test_file.unlink(missing_ok=True)
    except OSError as exc:
        _log(logger, f"preflight restore_id={config.restore_id} admin_share_write_test_failed error={exc}")
        return None

    _log(logger, f"preflight restore_id={config.restore_id} admin_share_fallback=yes effective_unc={admin_unc}")
    return dataclasses.replace(config, vm_import_unc=Path(admin_unc))


def _derive_admin_share_paths(host: str, local_path: Path) -> tuple[str, str]:
    """
    Return (admin_share_root, admin_share_unc) for a local Windows path.

    Example:
        host       = "198.51.100.129"
        local_path = C:\\MSSQL\\SQLBK_IMPORT\\ACME-192-0-2-250
        returns    = (
            "\\\\198.51.100.129\\C$",
            "\\\\198.51.100.129\\C$\\MSSQL\\SQLBK_IMPORT\\ACME-192-0-2-250",
        )
    """
    drive = local_path.drive  # e.g. "C:" on Windows, "" for Linux-style paths
    if not drive or ":" not in drive:
        raise ValueError(f"Cannot derive admin share: local_path has no drive letter: {local_path}")
    drive_letter = drive.rstrip(":").upper()
    admin_root = f"\\\\{host}\\{drive_letter}$"
    relative = str(local_path)[len(drive):]  # e.g. "\MSSQL\SQLBK_IMPORT\ACME-192-0-2-250"
    return admin_root, f"{admin_root}{relative}"


def _check_smb_port_detail(host: str) -> str:
    """Return a diagnostic string about TCP port 445 (SMB) reachability."""
    try:
        with socket.create_connection((host, 445), timeout=5):
            return "open"
    except ConnectionRefusedError:
        return "refused — SMB service not running or port 445 closed on host"
    except TimeoutError:
        return "timeout — host unreachable or port 445 blocked by firewall"
    except OSError as exc:
        return f"error — {exc}"


def _parse_unc(unc_path: Path) -> tuple[str, str, str]:
    """
    Parse \\\\host\\share[\\subdir...] into (host, share_name, unc_root).

    unc_root = '\\\\host\\share' (no trailing backslash, no subdirectory).
    """
    s = str(unc_path).replace("/", "\\")
    if not s.startswith("\\\\"):
        raise PreflightError(
            f"Windows restore target expected a UNC path (\\\\host\\share\\...), got: {unc_path}"
        )
    parts = [p for p in s[2:].split("\\") if p]
    if len(parts) < 2:
        raise PreflightError(
            f"UNC path must be \\\\host\\share[\\...], cannot extract host and share from: {unc_path}"
        )
    host = parts[0]
    share = parts[1]
    return host, share, f"\\\\{host}\\{share}"


def _local_share_root(unc_path: Path, local_path: Path) -> Path:
    """
    Derive the local path that corresponds to the UNC share root.

    Example:
        unc_path   = \\\\198.51.100.129\\SQLBK_IMPORT\\ACME-192-0-2-250
        local_path = C:\\MSSQL\\SQLBK_IMPORT\\ACME-192-0-2-250
        result     = C:\\MSSQL\\SQLBK_IMPORT   (the local path of the share root)
    """
    s = str(unc_path).replace("/", "\\")
    if s.startswith("\\\\"):
        unc_parts = [p for p in s[2:].split("\\") if p]
        subdir_depth = len(unc_parts) - 2  # directories below \\host\share
    else:
        subdir_depth = 0
    local_parts = local_path.parts
    if subdir_depth > 0 and len(local_parts) > subdir_depth:
        return Path(*local_parts[: len(local_parts) - subdir_depth])
    return local_path


def _unc_accessible(unc_root: str) -> bool:
    """Return True if the UNC share root is reachable from this machine."""
    return os.path.exists(unc_root)


def _ps_quote(value: str) -> str:
    """Wrap value in PowerShell single-quoted literal (escapes embedded single quotes as '')."""
    return "'" + value.replace("'", "''") + "'"


def _log(logger: object | None, message: str) -> None:
    from db_ops.logging_ops import log_event
    if logger:
        log_event(logger, level="logging", message=message)
    print(message, flush=True)
