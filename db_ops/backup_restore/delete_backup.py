"""OBSOLETE — superseded by ``delete-file`` / ``delete-files`` in :mod:`db_ops.common.deletefiles`.

Still wired into ``restore-workflow`` (``backup_restore/cli.py``), which the daemon schedules, so it
is marked rather than removed: deleting live scheduled behaviour is its own change, not a side
effect of writing the replacement. Nothing new should call it.

What the replacement does differently, and why each difference exists:

* **The caller chooses, then deletes.** This module computes the set inside the deleting process
  from ``copy_recent_hours`` and the clock, so "what would this remove" has no answer short of
  reading the code. ``list-backup-files`` lists, the caller decides, ``delete-files`` takes the
  explicit paths.
* **Anywhere, not one UNC share.** This reaches a Windows import folder over SMB or SSH. Backups
  on this estate also sit on an Ubuntu VM, in a container and on an Oracle Cloud host;
  :mod:`db_ops.common.hostcmd` reaches all of them.
* **No patterns.** ``*.bak``/``*.trn`` under a configured root is one wrong root away from deleting
  the backup being restored from. The replacement refuses a wildcard outright.
* **A response, not a returncode.** Per file: deleted / not_found / skipped / failed with a reason,
  so a partial failure can be retried on exactly the paths that failed.
"""

from __future__ import annotations
from db_ops.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, _log_progress, _write_temp_powershell_script  # noqa: F401 - one definition

import dataclasses
import datetime as dt
import json
import logging
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import re
from pathlib import Path, PurePosixPath
from typing import Any

from db_ops.backup_restore.config import BackupRestoreConfig, load_restore_config, validate_restore_target_is_not_source
from db_ops.backup_restore.copy_backup import build_cmdkey_command, open_ssh_connection
from db_ops.lib.shell import powershell_executable
from db_ops.logging_ops import log_event


@dataclasses.dataclass(frozen=True)
class DeleteBackupFileResult:
    target_file: Path
    status: str
    bytes: int
    reason: str = ""


@dataclasses.dataclass(frozen=True)
class DeleteBackupResult:
    returncode: int
    target_backup_dir: Path
    delete_older_than_hours: int
    files_considered: int
    deleted: int
    file_results: tuple[DeleteBackupFileResult, ...]
    skipped: int = 0



def obsolete_only(candidates: list[tuple[Any, float, int]]) -> set[str]:
    """Of files already past the age gate, the ones that are also **obsolete**.

    ``candidates`` are ``(path, backup_timestamp, size)``; the answer is their paths as text, so
    each engine intersects its own selection with it without changing how it deletes.

    Obsolete here means **a newer full exists**. This is a restore *staging* directory: files land
    in it to be restored from and are measured in hours, not weeks, so the age gate is
    ``copy_recent_hours``. The second condition is not a second age — that would be the same
    question asked twice — it is the chain: never delete the newest full, or anything at or after
    it, because that is what the next restore starts from. It is the same rule the backup scripts
    apply to their own directories ("older than the window AND older than the newest FULL"), which
    is deliberate: one rule about chains across the whole app.

    A recovery window of days was tried here first and was wrong in a way worth recording. Against
    a staging folder whose files are hours old, every full sits inside the window, so nothing is
    ever obsolete and the cleanup silently stops running — the folder fills up while the command
    reports success every night.

    Classification is by extension: ``.trn`` is a log, ``.bak`` a full. That is all a staging
    directory carries, it is what the restore side has always keyed on here, and reading real
    headers would need a login this cleanup does not have.
    """
    fulls = [(stamp, str(path)) for path, stamp, _size in candidates
             if not str(path).lower().endswith(".trn")]
    if not fulls:
        # Only logs staged, with no full among them to anchor on. The age gate alone decides:
        # holding them forever would defeat the cleanup, and there is no chain here to protect.
        return {str(path) for path, _stamp, _size in candidates}

    # Ties are broken by path so that exactly one full is the anchor. Two files sharing a timestamp
    # is not a corner case here: `vm_import_unc` is an SMB share, where mtime resolution can be two
    # seconds, and copies land in the same tick routinely. Compared on the timestamp alone, tied
    # fulls are each "not older than the newest" and every one of them is kept - the cleanup stops
    # deleting anything and reports success while the share fills up.
    anchor = max(fulls)
    anchor_stamp = anchor[0]
    obsolete = set()
    for path, stamp, _size in candidates:
        text = str(path)
        if text.lower().endswith(".trn"):
            # A log at the same instant as the newest full may belong to the chain that starts
            # there, so only a strictly older one is spared.
            if stamp < anchor_stamp:
                obsolete.add(text)
        elif (stamp, text) < anchor:
            obsolete.add(text)
    return obsolete


def _all_target_backup_files(root: Path) -> list[tuple[Path, float, int]]:
    """Every ``.bak``/``.trn`` under ``root`` with its timestamp and size.

    The obsolete check needs the whole directory, not the age-selected part of it. Judged on the
    selection alone, the newest full is whichever survivor happens to be newest *in that subset* —
    and since the real newest full is exactly what the age gate filters out, a single aged file
    always looked like its own anchor and was never obsolete. The cleanup then deleted nothing,
    for the most plausible-looking reason.
    """
    found: list[tuple[Path, float, int]] = []
    for pattern in ("*.bak", "*.trn"):
        for path in root.rglob(pattern):
            try:
                info = path.stat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            found.append((path, _backup_timestamp(path, fallback_mtime=info.st_mtime),
                          info.st_size))
    return found


def _split_by_obsolete(paths: list[Path], *, root: Path) -> tuple[list[Path], list[Path]]:
    """Split the age-selected ``paths`` into (delete, hold back) by the obsolete condition.

    ``root`` is the whole staging directory, and it is the argument that matters: the chain is
    computed over everything in there, then intersected with what the age gate chose.
    """
    obsolete = obsolete_only(_all_target_backup_files(root))
    return ([path for path in paths if str(path) in obsolete],
            [path for path in paths if str(path) not in obsolete])


def list_old_target_backup_files(config: BackupRestoreConfig | None = None, *, now: float | None = None) -> list[Path]:
    restore_config = config or load_restore_config()
    cutoff = _cutoff_timestamp(restore_config.copy_recent_hours, now=now)
    files: dict[str, tuple[float, Path]] = {}
    for pattern in ("*.bak", "*.trn"):
        for path in restore_config.vm_import_unc.rglob(pattern):
            try:
                file_stat = path.stat()
            except OSError:
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            backup_ts = _backup_timestamp(path, fallback_mtime=file_stat.st_mtime)
            if cutoff is None or backup_ts <= cutoff:
                files[str(path).lower()] = (backup_ts, path)
    return [path for _, path in sorted(files.values(), key=lambda item: (item[0], str(item[1]).lower()))]


def delete_target_backup_file(target_file: Path, *, target_root: Path) -> DeleteBackupFileResult:
    target_file.relative_to(target_root)
    file_size = target_file.stat().st_size
    target_file.unlink()
    return DeleteBackupFileResult(target_file=target_file, status="DELETED", bytes=file_size)


def _powershell_scan(config: BackupRestoreConfig, cutoff_iso: str) -> list[str]:
    """The age-selected paths under a UNC root, without deleting anything.

    Its own pass so the obsolete verdict is made in Python for all three engines. Two PowerShell
    invocations instead of one is the price of the three of them applying one rule.
    """
    script = r"""
param([string] $TargetRoot, [string] $CutoffIso)
$ErrorActionPreference = 'Stop'
$cutoff = $null
if ($CutoffIso) { $cutoff = [DateTimeOffset]::Parse($CutoffIso).UtcDateTime }
$files = Get-ChildItem -LiteralPath $TargetRoot -Recurse -File -Include '*.bak','*.trn'
foreach ($file in @($files | Sort-Object LastWriteTimeUtc, FullName)) {
    if ($cutoff -ne $null -and $file.LastWriteTimeUtc -gt $cutoff) { continue }
    $file.FullName
}
""".strip()
    script_path = _write_temp_powershell_script(script)
    cmd = [powershell_executable(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           str(script_path), str(config.vm_import_unc), cutoff_iso]
    try:
        result = subprocess.run(cmd, capture_output=True, check=False, text=True)
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"PowerShell target backup scan failed with exit code {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def delete_old_target_backup_files_with_powershell(
    config: BackupRestoreConfig,
    *,
    now: float | None = None,
) -> tuple[DeleteBackupFileResult, ...]:
    cutoff = _cutoff_timestamp(config.copy_recent_hours, now=now)
    cutoff_iso = "" if cutoff is None else dt.datetime.fromtimestamp(cutoff, tz=dt.timezone.utc).isoformat()
    script = r"""
param(
    [string] $TargetRoot,
    [string] $CutoffIso,
    [string] $AllowedFile
)
$ErrorActionPreference = 'Stop'
$cutoff = $null
if ($CutoffIso) {
    $cutoff = [DateTimeOffset]::Parse($CutoffIso).UtcDateTime
}
# The obsolete verdict is decided in Python and arrives as an explicit allow-list, so this engine
# applies exactly the same two conditions as the other two. Passed in a file rather than on the
# command line: a staging directory can hold thousands of paths and a command line cannot.
$allowed = $null
if ($AllowedFile) {
    $allowed = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($line in [System.IO.File]::ReadAllLines($AllowedFile)) {
        if ($line) { $allowed.Add($line) | Out-Null }
    }
}
$results = New-Object System.Collections.Generic.List[object]
$files = Get-ChildItem -LiteralPath $TargetRoot -Recurse -File -Include '*.bak','*.trn'
foreach ($file in @($files | Sort-Object LastWriteTimeUtc, FullName)) {
    if ($cutoff -ne $null -and $file.LastWriteTimeUtc -gt $cutoff) {
        continue
    }
    if ($allowed -ne $null -and -not $allowed.Contains($file.FullName)) {
        continue
    }
    $length = $file.Length
    Remove-Item -LiteralPath $file.FullName -Force
    $results.Add([pscustomobject]@{
        target_file = $file.FullName
        status = 'DELETED'
        bytes = $length
    }) | Out-Null
}
$results | ConvertTo-Json -Depth 4 -Compress
""".strip()
    # Scanned first so the obsolete verdict is computed here, next to the other two engines'.
    # Deciding inside the PowerShell would be a third copy of a rule that must be one rule.
    aged = _powershell_scan(config, cutoff_iso)
    keep_paths, _held = _split_by_obsolete([Path(item) for item in aged],
                                           root=config.vm_import_unc)
    allowed_file = Path(tempfile.mkstemp(prefix="db_ops_allowed_", suffix=".txt")[1])
    allowed_file.write_text("\n".join(str(path) for path in keep_paths), encoding="utf-8")

    script_path = _write_temp_powershell_script(script)
    cmd = [
        powershell_executable(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        str(config.vm_import_unc),
        cutoff_iso,
        str(allowed_file),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, check=False, text=True)
    finally:
        script_path.unlink(missing_ok=True)
        allowed_file.unlink(missing_ok=True)
    if result.returncode != 0:
        details = [f"PowerShell target backup delete failed with exit code {result.returncode}."]
        if result.stdout.strip():
            details.append(f"stdout:\n{result.stdout.strip()}")
        if result.stderr.strip():
            details.append(f"stderr:\n{result.stderr.strip()}")
        raise RuntimeError("\n".join(details))
    raw_output = result.stdout.strip()
    if not raw_output:
        return ()
    payload = json.loads(raw_output)
    rows = payload if isinstance(payload, list) else [payload]
    return tuple(
        DeleteBackupFileResult(
            target_file=Path(str(item["target_file"])),
            status=str(item["status"]),
            bytes=int(item["bytes"]),
        )
        for item in rows
    )


def delete_old_target_backup_files_via_ssh(
    config: BackupRestoreConfig,
    *,
    now: float | None = None,
    logger: logging.Logger | None = None,
) -> tuple[DeleteBackupFileResult, ...]:
    cutoff = _cutoff_timestamp(config.copy_recent_hours, now=now)
    linux_import = str(config.vm_import_unc).replace("\\", "/")
    results: list[DeleteBackupFileResult] = []
    _rid = f"restore_id={config.restore_id} " if config.restore_id else ""

    with open_ssh_connection(config) as ssh:
        # List all .bak/.trn files with mtime and size; filter by cutoff in Python.
        _, stdout, _ = ssh.exec_command(
            f'find {shlex.quote(linux_import)} -type f \\( -name "*.bak" -o -name "*.trn" \\) '
            f'-printf "%T@ %s %p\\n" 2>/dev/null || true'
        )
        lines = stdout.read().decode("utf-8", errors="replace").splitlines()
        files_to_delete = []
        scanned: list[tuple[str, float, int]] = []
        _log_progress(
            logger=logger,
            message=(
                f"{_rid}delete-backup source_id={config.source_id} cleanup_scan "
                f"root={linux_import} retention_hours={config.copy_recent_hours} scanned_files={len(lines)}"
            ),
        )
        for line in lines:
            parts = line.split(" ", 2)
            if len(parts) < 3:
                _log_progress(logger, f"{_rid}delete-backup source_id={config.source_id} cleanup_skip reason=parse_failed line={line}")
                continue
            try:
                mtime = float(parts[0])
                size = int(parts[1])
                fpath = parts[2].strip()
            except ValueError:
                _log_progress(logger, f"{_rid}delete-backup source_id={config.source_id} cleanup_skip reason=parse_failed line={line}")
                continue
            backup_ts = _backup_timestamp(Path(fpath), fallback_mtime=mtime)
            # Everything, not just the aged ones: the newest full is exactly what the age gate
            # filters out, so a chain computed on the survivors alone has no anchor to speak of.
            scanned.append((fpath, backup_ts, size))
            if cutoff is None or backup_ts <= cutoff:
                files_to_delete.append((fpath, size, backup_ts))
            else:
                results.append(DeleteBackupFileResult(target_file=Path(fpath), status="SKIPPED", bytes=size, reason="within_retention"))
                _log_progress(
                    logger,
                    (
                        f"{_rid}delete-backup source_id={config.source_id} cleanup_skip "
                        f"file={fpath} reason=within_retention timestamp_utc={_format_ts(backup_ts)}"
                    ),
                )

        # The SECOND condition, applied to what the age gate already chose. It can only remove
        # files from that set, never add one: age says "old enough to consider", obsolete says
        # "and nothing still needs it". A staged .bak that is the base of a differential inside
        # the window is old and required at the same time.
        obsolete = obsolete_only(scanned)
        kept_back = [row for row in files_to_delete if row[0] not in obsolete]
        files_to_delete = [row for row in files_to_delete if row[0] in obsolete]
        for fpath, size, backup_ts in kept_back:
            results.append(DeleteBackupFileResult(target_file=Path(fpath), status="SKIPPED",
                                                  bytes=size, reason="still_needed"))
            _log_progress(
                logger,
                (
                    f"{_rid}delete-backup source_id={config.source_id} cleanup_skip "
                    f"file={fpath} reason=still_needed timestamp_utc={_format_ts(backup_ts)}"
                ),
            )
        for fpath, size, backup_ts in files_to_delete:
            _log_progress(
                logger,
                (
                    f"{_rid}delete-backup source_id={config.source_id} cleanup_selected "
                    f"file={fpath} size_bytes={size} timestamp_utc={_format_ts(backup_ts)}"
                ),
            )

        for fpath, size, _backup_ts in files_to_delete:
            _, _, stderr = ssh.exec_command(f"rm -f -- {shlex.quote(fpath)}")
            err = stderr.read().decode("utf-8", errors="replace").strip()
            if err:
                results.append(DeleteBackupFileResult(target_file=Path(fpath), status="FAILED", bytes=size, reason=err))
                _log_progress(logger, f"{_rid}delete-backup source_id={config.source_id} cleanup_delete_failed file={fpath} reason={err}")
            else:
                results.append(DeleteBackupFileResult(target_file=Path(fpath), status="DELETED", bytes=size))
                _log_progress(logger, f"{_rid}delete-backup source_id={config.source_id} cleanup_deleted file={fpath} size_bytes={size}")

    return tuple(results)


def build_target_cmdkey_command(config: BackupRestoreConfig) -> list[str] | None:
    if config.is_linux:
        return None
    return build_cmdkey_command(
        credential_target=config.vm_credential_target,
        username=config.vm_username,
        password_env=config.vm_password_env,
    )


def should_use_powershell_unc_delete(config: BackupRestoreConfig) -> bool:
    return os.name == "nt" and str(config.vm_import_unc).startswith("\\\\")


def run_delete_backup(config: BackupRestoreConfig | None = None, *, logger: logging.Logger | None = None) -> DeleteBackupResult:
    restore_config = config or load_restore_config()
    _validate_safe_target_delete_root(restore_config)
    validate_restore_target_is_not_source(restore_config)
    _rid = f"restore_id={restore_config.restore_id} " if restore_config.restore_id else ""
    _log_progress(
        logger,
        (
            f"{_rid}delete-backup source_id={restore_config.source_id} start "
            f"cleanup_root={restore_config.vm_import_unc} retention_hours={restore_config.copy_recent_hours} "
            f"platform={restore_config.vm_platform}"
        ),
    )

    credential_cmd = build_target_cmdkey_command(restore_config)
    _log_progress(logger, f"{_rid}delete-backup source_id={restore_config.source_id} target_smb_credential_commands={1 if credential_cmd else 0}")
    if credential_cmd:
        subprocess.run(credential_cmd, check=True, text=True)

    if restore_config.is_linux:
        delete_engine = "ssh"
    elif should_use_powershell_unc_delete(restore_config):
        delete_engine = "powershell"
    else:
        delete_engine = "python"

    _log_progress(logger, f"{_rid}delete-backup source_id={restore_config.source_id} scanning engine={delete_engine}")
    if delete_engine == "ssh":
        file_results = delete_old_target_backup_files_via_ssh(restore_config, logger=logger)
    elif delete_engine == "powershell":
        file_results = delete_old_target_backup_files_with_powershell(restore_config)
    else:
        aged_files = list_old_target_backup_files(restore_config)
        selected_files, held_back = _split_by_obsolete(aged_files, root=restore_config.vm_import_unc)
        _log_progress(logger, f"{_rid}delete-backup source_id={restore_config.source_id} cleanup_root={restore_config.vm_import_unc} retention_hours={restore_config.copy_recent_hours} aged_files={len(aged_files)} selected_files={len(selected_files)} still_needed={len(held_back)}")
        file_results_list: list[DeleteBackupFileResult] = [
            DeleteBackupFileResult(target_file=path, status="SKIPPED", bytes=0,
                                   reason="still_needed")
            for path in held_back
        ]
        for index, target_file in enumerate(selected_files, start=1):
            result = delete_target_backup_file(target_file, target_root=restore_config.vm_import_unc)
            file_results_list.append(result)
            _log_progress(
                logger,
                (
                    f"{_rid}delete-backup source_id={restore_config.source_id} file={index}/{len(selected_files)} "
                    f"status={result.status} bytes={result.bytes} target={result.target_file}"
                ),
            )
        file_results = tuple(file_results_list)

    _log_progress(
        logger,
        (
            f"{_rid}delete-backup source_id={restore_config.source_id} finished "
            f"scanned_files={len(file_results)} deleted={sum(1 for item in file_results if item.status == 'DELETED')} "
            f"skipped={sum(1 for item in file_results if item.status == 'SKIPPED')} "
            f"failed={sum(1 for item in file_results if item.status == 'FAILED')}"
        ),
    )
    return DeleteBackupResult(
        returncode=1 if any(item.status == "FAILED" for item in file_results) else 0,
        target_backup_dir=restore_config.vm_import_unc,
        delete_older_than_hours=restore_config.copy_recent_hours,
        files_considered=len(file_results),
        deleted=sum(1 for item in file_results if item.status == "DELETED"),
        file_results=file_results,
        skipped=sum(1 for item in file_results if item.status == "SKIPPED"),
    )


def _cutoff_timestamp(hours: int, *, now: float | None) -> float | None:
    if hours <= 0:
        return None
    return (time.time() if now is None else now) - (hours * 60 * 60)


def _backup_timestamp(path: Path, *, fallback_mtime: float) -> float:
    match = _BACKUP_TIMESTAMP_RE.search(path.name)
    if not match:
        return fallback_mtime
    try:
        return dt.datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp()
    except ValueError:
        return fallback_mtime


def _format_ts(value: float) -> str:
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat()


def _validate_safe_target_delete_root(config: BackupRestoreConfig) -> None:
    target = _normalize_path_for_compare(config.vm_import_unc, config.is_linux)
    source = _normalize_path_for_compare(config.prod_backup_share, False)
    if not target:
        raise ValueError("Unsafe delete-backup config: vm_import_unc is empty.")
    sep = "/" if config.is_linux else "\\"
    if target == source or target.startswith(f"{source}{sep}") or source.startswith(f"{target}{sep}"):
        raise ValueError("Unsafe delete-backup config: target backup folder overlaps the source backup folder.")

    parts = PurePosixPath(target).parts if config.is_linux else config.vm_import_unc.parts
    if len(parts) < 2:
        raise ValueError(f"Unsafe delete-backup config: target backup folder is too broad: {config.vm_import_unc}")


def _normalize_path_for_compare(path: Path, is_linux: bool) -> str:
    s = str(path).strip()
    if is_linux:
        s = s.replace("\\", "/")  # WindowsPath renders linux paths with backslashes
        return s.rstrip("/").lower()
    return s.rstrip("\\/").lower().replace("/", "\\")




