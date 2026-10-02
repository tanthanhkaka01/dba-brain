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

**And, for an entry that asks, once more before each database's first RESTORE** (0.26.0 §1.76,
:func:`check_restore_room`, ``space_check.measure_restore``). The copy's check counts backup files,
and a compressed backup's size is not the size of the database inside it: a 56.8 GiB chain passed,
and the restore built 366.6 GB of files and left the target at 96%. The backup states the files it
holds, so the restore can be measured from that - on the target's own SQL Server, which is the only
place the data path means anything when the instance runs in a container. It is **off unless the
entry says so** (the operator, 2026-10-02): the engineer who sets up a restore knows the disk has to
hold the database, and the copy at x2 is the rule every engine shares.
"""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable

from db_ops.backup_restore import copy_backup
from db_ops.backup_restore.config import BackupRestoreConfig
from db_ops.backup_restore.copy_backup import open_ssh_connection
from db_ops.lib import restore_space


class RestoreSpaceRefused(RuntimeError):
    """The restore was stopped because the files will not fit, or could not be measured."""


@dataclasses.dataclass(frozen=True)
class CopyMeasure:
    """What one copy is about to write, and where."""

    #: Bytes the target gains - the number the rule is asked about.
    to_write: int
    #: Bytes of the selection the target already holds at their size, so the copy leaves them.
    staged: int
    #: Bytes that land in **this node's** temp folder before they reach the target - a Linux
    #: node's copy fetches the whole of what it transfers there first; 0 for every other copy.
    through_node: int = 0


def measure_copy(config: BackupRestoreConfig, *, recopy: bool = False,
                 report: object | None = None) -> CopyMeasure | None:
    """What this copy will write, or ``None`` when it cannot be counted.

    Counted with the **same selection, read the same way, as the copy**
    (:func:`db_ops.backup_restore.copy_backup.selected_backup_sizes`): a number produced by a
    different rule answers a different question. Until 0.26.0 this walked the source as a path
    whatever the copy did. On a Linux node the source is a share read through ``smbclient`` and a
    UNC path is no folder: it found nothing, counted **0 bytes** and passed - every restore the
    worker ran was "checked" and none was measured. A source that cannot be read is ``None`` now,
    and the caller decides what an unmeasured restore is worth.

    **Less what is already staged** (0.26.0 §1.76): a file on the target at its size is left alone
    by the copy, so it takes no room. The second run of the 100.250 drill counted its 56.8 GiB chain
    again with every file of it already there, and with 21 GiB free every scheduled run after the
    first would have been refused for files it was not going to write. ``recopy`` is a forced copy,
    which writes each of them again (:func:`db_ops.lib.restore_space.remaining_bytes`).
    """
    try:
        incoming = copy_backup.selected_backup_sizes(config)
    except Exception as exc:  # noqa: BLE001 - whatever stopped the reading, the bytes are not known.
        # One unreadable file makes the total a lower bound, and a lower bound that passes the
        # check is exactly the failure this module exists to prevent.
        if callable(report):
            report(f"the source could not be read: {' '.join(str(exc).split())[:300]}")
        return None
    # Only the Linux copy writes a staged file again when forced (beside itself, then moved over);
    # the Windows one leaves a file of the same size alone whatever it is told.
    rewrites = bool(recopy and config.is_linux)
    on_target = _staged_sizes(config, incoming)
    to_write, staged = restore_space.remaining_bytes(incoming, on_target, recopy=rewrites)
    if staged and callable(report):
        report(f"{restore_space.format_gib(staged)} of the {len(incoming)} selected file(s) is already "
               "staged at its size and is not counted"
               + (" - a forced copy writes each again, one at a time, so the largest is" if rewrites else ""))
    through_node = 0
    if copy_backup.copy_engine(config) == copy_backup.ENGINE_SMBCLIENT:
        # Forced, it fetches the whole selection; otherwise only what the target lacks.
        through_node = sum(size for key, size in incoming.items()
                           if recopy or on_target.get(key) != size)
    return CopyMeasure(to_write=to_write, staged=staged, through_node=through_node)


def measure_incoming_bytes(config: BackupRestoreConfig, *, recopy: bool = False,
                           report: object | None = None) -> int | None:
    """The bytes this copy adds to the target, or ``None`` when they cannot be counted."""
    measured = measure_copy(config, recopy=recopy, report=report)
    return None if measured is None else measured.to_write


def node_staging_dir() -> Path:
    """Where a Linux node's copy puts what it fetched from the share before sending it on."""
    return Path(tempfile.gettempdir())


def _staged_sizes(config: BackupRestoreConfig, incoming: dict[str, int]) -> dict[str, int]:
    """``{staging key: size}`` for the selected files the target already holds.

    Anything that cannot be read is simply not staged: the copy would then write it, and counting
    it errs toward refusing, never toward a copy that does not fit.
    """
    if not incoming:
        return {}
    if config.is_linux:
        # The listing the Linux copy itself skips by (its keys are lower-cased), so the two cannot
        # disagree about a file.
        listed = copy_backup._remote_destination_sizes(config, logger=None)
        return {key: listed[key.lower()] for key in incoming if key.lower() in listed}
    sizes: dict[str, int] = {}
    for key in incoming:
        try:
            sizes[key] = (Path(config.vm_import_unc) / key).stat().st_size
        except (OSError, ValueError):
            continue
    return sizes


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
    """``df`` on the target, over the SSH session the copy itself uses - the command and its reading
    are :mod:`db_ops.lib.restore_space`'s, shared with the copy between two hosts."""
    # **The same expression the copy uses** (`copy_backup.py`, the `linux_import` line). A Linux
    # path held in a `Path` on a Windows node renders with backslashes — `\opt\db_ops\...` — and
    # `df` on the target then answers "no such file", which this function could only report as
    # "could not measure". Measured on 2026-09-19: every Linux-target restore driven from the
    # Windows master was refused for that reason, which is safe and entirely the wrong reason.
    target = str(PurePosixPath(str(config.vm_import_unc or config.vm_import_local).replace("\\", "/")))
    client = None
    try:
        client = open_ssh_connection(config)
        stdout = client.run(linux_free_space_command(target)).stdout
    except Exception:  # noqa: BLE001 - any failure to reach the target is "could not measure".
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - closing a broken session must not mask the reading.
                pass
    return restore_space.parse_df_free_bytes(stdout)


#: One definition, in ``lib``: the copy between two hosts asks the same question (``common``).
linux_free_space_command = restore_space.free_space_command


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
    recopy: bool = False,
) -> dict[str, object]:
    """Refuse the restore unless the incoming files fit, with room to spare.

    Returns what it measured, so the caller can log or report it. Raises
    :class:`RestoreSpaceRefused` when the answer is no — or when it could not be measured and the
    entry has not said that is acceptable. ``recopy`` says the copy is forced, and so writes again
    the files it would otherwise find staged.
    """
    rule = config.space_check
    where = f"restore_id={config.restore_id or config.target_id}"
    if not rule.enabled:
        _say(logger, log, f"{where} space check: disabled by space_check.enabled=false")
        return {"checked": False, "reason": "disabled"}

    measured = measure_copy(
        config, recopy=recopy,
        report=lambda message: _say(logger, log, f"{where} space check: {message}"))
    incoming = None if measured is None else measured.to_write
    if measured is not None and measured.through_node:
        _check_node_staging(measured.through_node, where=where, logger=logger, log=log)
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


def _check_node_staging(staged_here: int, *, where: str, logger: object | None,
                        log: object | None) -> None:
    """Refuse a copy whose files do not fit in this node's own temp folder on their way through.

    A Linux node cannot hand a share to the target: it fetches what it will transfer into its temp
    folder, sends it on, and removes it. Nothing measured that folder, and on a container worker
    it is the disk the runtime store shares - the one that, full, stopped the store and every
    daemon writing to it (2026-09-27).
    Asked only whether the bytes **fit**, without the entry's factor: they are there for the length
    of the copy and then gone, and a check added to a path that runs every night refuses only what
    could not have worked. A folder that cannot be measured is said and does not stop the copy.
    """
    folder = node_staging_dir()
    free = _local_free_bytes(folder)
    size = restore_space.format_gib(staged_here)
    if free is None:
        _say(logger, log, f"{where} space check: {size} passes through {folder} on this node; its "
                          "free space could not be read - not measured")
        return
    if free < staged_here:
        raise RestoreSpaceRefused(
            f"{where} will not fit on this node: {size} is fetched into {folder} here before it "
            f"goes to the target, and {restore_space.format_gib(free)} is free there - SHORT BY "
            f"{restore_space.format_gib(staged_here - free)}. Free that much on this node, or point "
            "its temp folder (TMPDIR) at a larger disk. Nothing was copied.")
    _say(logger, log, f"{where} space check: {size} passes through {folder} on this node, "
                      f"{restore_space.format_gib(free)} free there - fits")


#: The columns ``RESTORE FILELISTONLY`` answers with - the table the full restore itself reads
#: (``common/restorestep/sqlserver.py``), so the two take the same servers.
_FILELIST_COLUMNS = """
    LogicalName nvarchar(128), PhysicalName nvarchar(260), [Type] char(1),
    FileGroupName nvarchar(128) NULL, Size numeric(20, 0), MaxSize numeric(20, 0), FileId bigint,
    CreateLSN numeric(25, 0) NULL, DropLSN numeric(25, 0) NULL, UniqueId uniqueidentifier,
    ReadOnlyLSN numeric(25, 0) NULL, ReadWriteLSN numeric(25, 0) NULL, BackupSizeInBytes bigint,
    SourceBlockSize int, FileGroupId int, LogGroupGUID uniqueidentifier NULL,
    DifferentialBaseLSN numeric(25, 0) NULL, DifferentialBaseGUID uniqueidentifier,
    IsReadOnly bit, IsPresent bit, TDEThumbprint varbinary(32) NULL, SnapshotUrl nvarchar(360) NULL
""".strip()

#: The line the measurement answers on, found in whatever else ``sqlcmd`` prints.
ROOM_MARKER = "DBOPS_ROOM|"


def restore_room_sql(*, database: str, backups: list[str], data_path: str, log_path: str) -> str:
    """One read-only batch, on the target: what the restore creates, what it overwrites, what is free.

    * **created** - the sum of the file sizes each backup lists (``RESTORE FILELISTONLY``), the
      largest of them: a differential lists the files as they were when it was taken, and they only
      grow between the two.
    * **overwritten** - the database's own files at exactly the two paths the restore moves to.
      Those are written over in place; a file anywhere else is not counted, so the answer errs
      toward needing more.
    * **free** - on the volume the data path is on, as the instance sees it
      (``sys.dm_os_volume_stats``). Asked of the instance and not of the host because the path is
      the instance's: in a container it is not a path on the host at all.

    Each part is tried on its own: one that cannot be read leaves its number empty, and the caller
    says which it was.
    """
    literal = str(database).replace("'", "''")
    data = str(data_path).replace("'", "''")
    log = str(log_path).replace("'", "''")
    # A path inside the EXEC string sits two literals deep, so its quote is doubled twice - as in
    # the full restore's own batch.
    nested = [str(path).replace("'", "''''") for path in backups]
    reads = "\n".join(
        "    DELETE FROM @filelist;\n"
        f"    INSERT INTO @filelist EXEC('RESTORE FILELISTONLY FROM DISK = N''{path}''');\n"
        "    SELECT @size = SUM(CAST(Size AS bigint)) FROM @filelist;\n"
        "    IF @size > ISNULL(@created, 0) SET @created = @size;"
        for path in nested)
    return f"""
SET NOCOUNT ON;
DECLARE @filelist TABLE ({_FILELIST_COLUMNS});
DECLARE @created bigint, @size bigint, @overwritten bigint, @free bigint, @volume nvarchar(512);
BEGIN TRY
{reads}
END TRY
BEGIN CATCH
    SET @created = NULL;
END CATCH;
BEGIN TRY
    SELECT @overwritten = ISNULL(SUM(CAST(size AS bigint)) * 8192, 0)
    FROM sys.master_files
    WHERE database_id = DB_ID(N'{literal}')
      AND LOWER(physical_name) IN (LOWER(N'{data}'), LOWER(N'{log}'));
END TRY
BEGIN CATCH
    SET @overwritten = 0;
END CATCH;
BEGIN TRY
    SELECT TOP (1) @free = vs.available_bytes, @volume = vs.volume_mount_point
    FROM sys.master_files AS mf
    CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) AS vs
    WHERE LEFT(LOWER(N'{data}'), LEN(vs.volume_mount_point)) = LOWER(vs.volume_mount_point)
    ORDER BY LEN(vs.volume_mount_point) DESC;
END TRY
BEGIN CATCH
    SET @free = NULL;
END CATCH;
SELECT N'{ROOM_MARKER}'
    + COALESCE(CONVERT(nvarchar(30), @created), N'') + N'|'
    + COALESCE(CONVERT(nvarchar(30), @overwritten), N'') + N'|'
    + COALESCE(CONVERT(nvarchar(30), @free), N'') + N'|'
    + COALESCE(@volume, N'');
""".strip()


def parse_restore_room(stdout: str) -> dict[str, object]:
    """``created`` / ``overwritten`` / ``free`` (bytes, or ``None`` for one not read) and ``volume``."""
    line = next((text for text in str(stdout or "").splitlines() if ROOM_MARKER in text), "")
    parts = line[line.find(ROOM_MARKER) + len(ROOM_MARKER):].strip().split("|", 3) if line else []
    parts += [""] * (4 - len(parts))

    def number(text: str) -> int | None:
        text = text.strip()
        return int(text) if text.isdigit() else None

    return {"created": number(parts[0]), "overwritten": number(parts[1]),
            "free": number(parts[2]), "volume": parts[3].strip()}


def check_restore_room(
    config: BackupRestoreConfig,
    *,
    database: str,
    backups: list[str],
    data_path: str,
    log_path: str,
    run_sql: Callable[[str], str],
    logger: object | None = None,
    log: object | None = None,
) -> dict[str, object]:
    """For an entry that asks: refuse one database's restore unless the files it creates fit (§1.76).

    **Only when the entry says** ``space_check.measure_restore: true`` (the operator, 2026-10-02).
    Without it nothing is asked and nothing is said: the copy's check at the entry's factor is the
    rule, and the target is not sent a batch it was not asked for.

    Asked after the copy and before that database's first RESTORE, of the target's own SQL Server,
    through ``run_sql`` - the channel the restore is about to use, so a target the measurement
    cannot reach is one the restore could not reach either. Raises :class:`RestoreSpaceRefused` on
    a measured shortfall, by the entry's factor.

    **A measurement that could not be made is held to** ``space_check.on_unknown``, as the copy's
    is: the entry asked for it by name, so "could not" is refused unless the entry also says
    ``proceed`` - which every such run then says in the log. The free space is read from a view an
    instance older than 2008 R2 SP1 does not have and a login without ``VIEW SERVER STATE`` may not
    read; an entry for such a target leaves ``measure_restore`` out, or says ``proceed``.
    """
    rule = config.space_check
    where = f"restore_id={config.restore_id or config.target_id} database={database}"
    if not rule.enabled:
        _say(logger, log, f"{where} restore room: disabled by space_check.enabled=false")
        return {"checked": False, "reason": "disabled"}
    if not rule.measure_restore:
        return {"checked": False, "reason": "not_asked"}

    problem = ""
    try:
        measured = parse_restore_room(run_sql(restore_room_sql(
            database=database, backups=backups, data_path=data_path, log_path=log_path)))
    except Exception as exc:  # noqa: BLE001 - whatever stopped the measurement, it is "not measured".
        measured = {"created": None, "overwritten": None, "free": None, "volume": ""}
        problem = f" ({' '.join(str(exc).split())[:200]})"
    created, free = measured["created"], measured["free"]
    if created is None or free is None:
        missing = ("the size of the files the backup holds" if created is None
                   else f"the free space of the volume {data_path} is on")
        detail = f"{where} restore room: could not read {missing}{problem}"
        if rule.on_unknown == "proceed":
            _say(logger, log, detail + " - not measured, restoring anyway, because "
                                       "space_check.on_unknown=proceed")
            return {"checked": False, "reason": "unknown", "new_bytes": created, "free_bytes": free}
        raise RestoreSpaceRefused(
            detail + ". Refusing: this entry asks for its restore to be measured "
            '(space_check.measure_restore). Set {"on_unknown": "proceed"} to restore unmeasured, or '
            "leave measure_restore out. This database was not restored; its staged backups are kept.")

    verdict = restore_space.judge_restore(
        int(created), int(measured["overwritten"] or 0), int(free), rule.factor)
    volume = str(measured["volume"] or data_path)
    _say(logger, log, f"{where} restore room: {verdict.text} on {volume}")
    if not verdict.ok:
        raise RestoreSpaceRefused(
            f"{where} will not fit: {verdict.text}. Free "
            f"{restore_space.format_gib(verdict.shortfall_bytes)} on {volume}, lower "
            f"space_check.factor (now {verdict.factor:g}), or restore somewhere else. This "
            "database was not restored; its staged backups are kept.")
    return {
        "checked": True,
        "ok": True,
        "new_bytes": verdict.new_bytes,
        "replaced_bytes": verdict.replaced_bytes,
        "free_bytes": verdict.free_bytes,
        "required_bytes": verdict.required_bytes,
        "factor": verdict.factor,
        "volume": volume,
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
    "check_restore_room",
    "CopyMeasure",
    "measure_copy",
    "measure_incoming_bytes",
    "measure_target_free_bytes",
    "parse_restore_room",
    "restore_room_sql",
]
