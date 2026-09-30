"""Measure the two numbers the free-space rule needs, and refuse a restore that will not fit.

The rule is :mod:`db_ops.lib.restore_space` and it touches no filesystem. This module is the half
that reaches: it adds up the backup files a copy is about to move, reads the free space where they
are going, and raises before the first byte is written.

**Why before the copy and not during it.** A copy that runs out of room has already done the damage:
the partial files are on the disk, the engine that shares that disk is already failing, and the
cleanup runs on a host that may no longer be answering. On 2026-09-17 that host was the one carrying
the runtime store, and the daemon died with it. The only useful moment for this question is before
anything has been written.

**Why it refuses rather than warns.** A warning in a scheduled run is read after the outage, by
which time it is evidence and not a brake.
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from db_ops.backup_restore.config import BackupRestoreConfig
from db_ops.backup_restore.copy_backup import list_recent_backup_files, open_ssh_connection
from db_ops.lib import restore_space


class RestoreSpaceRefused(RuntimeError):
    """The restore was stopped because the files will not fit, or could not be measured."""


def measure_incoming_bytes(config: BackupRestoreConfig) -> int | None:
    """Total size of the files this copy would move, or ``None`` when it cannot be counted.

    Counted with the **same selection the copy uses** — same window, same patterns — because a
    number produced by a different rule answers a different question. Where the source is not
    directly readable from this process (a Windows share listed over PowerShell, an SMB source read
    through smbclient), the sizes are not available here and the answer is ``None`` rather than a
    guess: the caller decides what an unmeasured restore is worth.
    """
    try:
        files = list_recent_backup_files(config)
    except OSError:
        return None
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            # One unreadable file makes the total a lower bound, and a lower bound that passes the
            # check is exactly the failure this module exists to prevent.
            return None
    return total


def measure_target_free_bytes(config: BackupRestoreConfig) -> int | None:
    """Free bytes where the files will land, or ``None`` when the target cannot be asked."""
    if config.is_linux:
        return _linux_free_bytes(config)
    return _local_free_bytes(config.vm_import_unc)


def _local_free_bytes(path: Path) -> int | None:
    """Free space at a path this process can see — a UNC share, or a local directory."""
    probe = Path(path)
    while True:
        try:
            return shutil.disk_usage(probe).free
        except (OSError, ValueError):
            # The leaf may not exist yet; the filesystem it will live on still answers.
            if probe.parent == probe:
                return None
            probe = probe.parent


def _linux_free_bytes(config: BackupRestoreConfig) -> int | None:
    """``df`` on the target, over the SSH session the copy itself uses.

    ``-P`` for the portable one-line-per-filesystem format and ``-k`` for 1024-byte blocks: without
    both, a long device name wraps onto its own line and the column that gets parsed is the wrong
    one — which reads as a target with almost no free space, or with far too much.
    """
    # **The same expression the copy uses** (`copy_backup.py`, the `linux_import` line). A Linux
    # path held in a `Path` on a Windows node renders with backslashes — `\opt\db_ops\...` — and
    # `df` on the target then answers "no such file", which this function could only report as
    # "could not measure". Measured on 2026-09-19: every Linux-target restore driven from the
    # Windows master was refused for that reason, which is safe and entirely the wrong reason.
    target = str(PurePosixPath(str(config.vm_import_unc or config.vm_import_local).replace("\\", "/")))
    client = None
    try:
        client = open_ssh_connection(config)
        answer = client.run(linux_free_space_command(target))
        lines = [line for line in answer.stdout.splitlines() if line.strip()]
    except Exception:  # noqa: BLE001 - any failure to reach the target is "could not measure".
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - closing a broken session must not mask the reading.
                pass
    if len(lines) < 2:
        return None
    columns = lines[-1].split()
    if len(columns) < 4:
        return None
    try:
        return int(columns[3]) * 1024
    except ValueError:
        return None


def linux_free_space_command(target: str) -> str:
    """``df`` at ``target``, or at the nearest folder above it that exists.

    The staging folder is made by the copy, which runs after this check - so on a target that has
    never been restored to, it is not there yet, and ``df`` on it answers "no such file". The local
    measurement already climbed to the nearest existing parent; the Linux one did not, and the
    first restore onto every rebuilt lab was refused as "could not measure" (the 0.25.0 soak,
    2026-09-29: ``.250``'s ``SQLBK_IMPORT`` folder had gone with the rebuild). The filesystem the
    folder will live on is the one that answers.
    """
    return (f"p={_posix_quote(target)}; "
            'while [ ! -e "$p" ] && [ "$p" != / ]; do p=$(dirname "$p"); done; '
            'df -Pk "$p"')


def _posix_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def target_description(config: BackupRestoreConfig) -> str:
    """Where the files land, spelled the way that target would spell it."""
    if config.is_linux:
        return str(PurePosixPath(
            str(config.vm_import_unc or config.vm_import_local).replace("\\", "/")))
    return str(config.vm_import_unc)


def check_free_space(
    config: BackupRestoreConfig,
    *,
    logger: object | None = None,
    log: object | None = None,
) -> dict[str, object]:
    """Refuse the restore unless the incoming files fit, with room to spare.

    Returns what it measured, so the caller can log or report it. Raises
    :class:`RestoreSpaceRefused` when the answer is no — or when it could not be measured and the
    entry has not said that is acceptable.
    """
    rule = config.space_check
    where = f"restore_id={config.restore_id or config.target_id}"
    if not rule.enabled:
        _say(logger, log, f"{where} space check: disabled by space_check.enabled=false")
        return {"checked": False, "reason": "disabled"}

    incoming = measure_incoming_bytes(config)
    free = measure_target_free_bytes(config)

    if incoming is None or free is None:
        missing = "the size of the incoming files" if incoming is None else "the target's free space"
        detail = (f"{where} space check: could not read {missing} "
                  f"(source={config.source_backup_dir}, target="
                  f"{target_description(config)})")
        if rule.on_unknown == "proceed":
            _say(logger, log, detail + " - proceeding, because space_check.on_unknown=proceed")
            return {"checked": False, "reason": "unknown", "incoming_bytes": incoming,
                    "free_bytes": free}
        raise RestoreSpaceRefused(
            detail + ". Refusing: an unmeasured restore is the one that filled the disk. Set "
            'space_check {"on_unknown": "proceed"} on this entry to accept that, or '
            '{"enabled": false} to turn the check off.')

    verdict = restore_space.judge(incoming, free, rule.factor)
    _say(logger, log, f"{where} space check: {verdict.text}")
    if not verdict.ok:
        raise RestoreSpaceRefused(
            f"{where} will not fit: {verdict.text}. Free "
            f"{restore_space.format_gib(verdict.shortfall_bytes)} on "
            f"{target_description(config)}, lower "
            f"space_check.factor (now {verdict.factor:g}), or restore somewhere else. Nothing was "
            "copied.")
    return {
        "checked": True,
        "ok": True,
        "incoming_bytes": verdict.incoming_bytes,
        "free_bytes": verdict.free_bytes,
        "required_bytes": verdict.required_bytes,
        "factor": verdict.factor,
        "detail": verdict.text,
    }


def _say(logger: object | None, log: object | None, message: str) -> None:
    if callable(log):
        log(message)
        return
    if logger is not None:
        try:
            logger.info(message)  # type: ignore[attr-defined]
            return
        except Exception:  # noqa: BLE001 - a logger that cannot log must not stop the check.
            pass
    print(message)


__all__ = [
    "RestoreSpaceRefused",
    "check_free_space",
    "measure_incoming_bytes",
    "measure_target_free_bytes",
]
