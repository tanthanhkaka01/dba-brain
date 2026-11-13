"""Retention cleanup of a restore target's import folder - the share-driven restore's DELETE step.

Wired into ``restore-workflow`` and ``delete-backup`` (``backup_restore/cli.py``). It chooses what is
past its retention *and* no longer needed by a chain (:func:`obsolete_only`) from a listing of the
folder - over SSH on a Linux target, through the share on a Windows one, from the filesystem when
the folder is on this machine.

**It deletes by verified commands only** (0.38.0, 1.329). Until then each engine removed its files
its own way - ``rm -f`` over SSH, ``smb-delete`` with a list, ``unlink`` here - with nothing held to
anything. Now every engine hands its selection to :func:`_delete_selected`: a file a proven copy
recorded on this target is deleted by ``delete-verified-files`` with the bytes and sha256 that copy
measured there; **a file no copy recorded is kept and named**; ``force`` (an operator, by hand)
deletes each of those by its own ``delete-file`` command. A Windows import folder is reached
through its host (the target's ``cmd_access`` in the inventory, the folder's local path
``vm_import_local``): a share has nothing to hash a file on, so a target reachable only as a share
keeps its files and says why.
"""

from __future__ import annotations
from dbabrain.backup_restore.shell_quoting import _BACKUP_TIMESTAMP_RE, backup_time_from_name, _log_progress, _write_temp_powershell_script  # noqa: F401 - one definition

import dataclasses
import datetime as dt
import logging
import os
import shlex
import stat
import sys
import time
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from dbabrain.backup_restore.config import BackupRestoreConfig, load_restore_config, validate_restore_target_is_not_source
from dbabrain.backup_restore.copy_backup import open_ssh_connection, share_login_request, store_share_logins
from dbabrain.logging_ops import log_event


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
    cleanup_retention: int
    files_considered: int
    deleted: int
    file_results: tuple[DeleteBackupFileResult, ...]
    skipped: int = 0
    #: Files past retention that no proven copy recorded - kept, each named in ``file_results``.
    kept_unknown: int = 0


#: Folder names that say what KIND of backup a file is rather than what it belongs to. A staging
#: tree is `<source>/<database>/<type>/<file>`, so the database is one level above these.
_BACKUP_TYPE_FOLDERS = frozenset({
    "full", "diff", "differential", "incr", "incremental", "log", "archivelog", "arch", "redo",
})


def _chain_group(path: Any) -> str:
    """Which restore chain a staged file belongs to — in practice, which database.

    One staging directory holds every database copied from a source, and a chain is per database:
    `Orders`'s full anchors `Orders`'s logs and nothing else. Keyed on the folder NAME rather
    than its full path so a configuration that stages fulls and logs under different roots
    (`vm_import_linux_path` / `vm_import_linux_log_path`) still resolves to one chain.

    A layout with no type folder — everything loose in one directory — yields the same key for
    every file, which is the whole directory as one chain: the behaviour before this existed.
    """
    parts = [part for part in re.split(r"[\\/]+", str(path).strip()) if part]
    parts = parts[:-1]  # the file itself
    if parts and parts[-1].lower() in _BACKUP_TYPE_FOLDERS:
        parts = parts[:-1]
    return parts[-1].lower() if parts else ""


#: Folder names and name markers for the two kinds that hang off a full. A SQL Server
#: differential is a ``.bak`` like the full it depends on (`<DB>/DIFF/<DB>_DIFF_<stamp>Z.bak`), so
#: the extension alone cannot tell them apart.
_DIFF_FOLDERS = frozenset({"diff", "differential", "incr", "incremental"})
_LOG_FOLDERS = frozenset({"log", "archivelog", "arch", "redo"})
_DIFF_MARKERS = ("_diff_", "_incr_")
_LOG_MARKERS = ("_log_",)


def backup_kind(path: Any) -> str:
    """``"full"``, ``"diff"`` or ``"log"`` for a staged file, from its folder, its name, its extension.

    Only a full anchors a chain. Classified by extension alone, the newest DIFF ``.bak`` became the
    anchor and the FULL it restores onto was deleted as obsolete (review 0.25.0, B4.1).
    """
    parts = [part for part in re.split(r"[\\/]+", str(path).strip()) if part]
    name = parts[-1].lower() if parts else ""
    folder = parts[-2].lower() if len(parts) > 1 else ""
    if name.endswith(".trn") or folder in _LOG_FOLDERS or any(m in name for m in _LOG_MARKERS):
        return "log"
    if folder in _DIFF_FOLDERS or any(m in name for m in _DIFF_MARKERS):
        return "diff"
    return "full"


def obsolete_only(candidates: list[tuple[Any, float, int]]) -> set[str]:
    """Of files already past the age gate, the ones that are also **obsolete**.

    ``candidates`` are ``(path, backup_timestamp, size)``; the answer is their paths as text, so
    each engine intersects its own selection with it without changing how it deletes.

    Obsolete here means **a newer full exists**. This is a restore *staging* directory: files land
    in it to be restored from and are measured in hours, not weeks, so the age gate is
    ``cleanup_retention``. The second condition is not a second age — that would be the same
    question asked twice — it is the chain: never delete the newest full, or anything at or after
    it, because that is what the next restore starts from. It is the same rule the backup scripts
    apply to their own directories ("older than the window AND older than the newest FULL"), which
    is deliberate: one rule about chains across the whole app.

    A recovery window of days was tried here first and was wrong in a way worth recording. Against
    a staging folder whose files are hours old, every full sits inside the window, so nothing is
    ever obsolete and the cleanup silently stops running — the folder fills up while the command
    reports success every night.

    Classification is by folder, name and extension (:func:`backup_kind`): a DIFF is a ``.bak``
    too, and only a FULL anchors a chain. Reading real headers would need a login this cleanup
    does not have.
    """
    fulls = [(stamp, str(path)) for path, stamp, _size in candidates
             if backup_kind(path) == "full"]
    if not fulls:
        # Only logs staged, with no full among them to anchor on. The age gate alone decides:
        # holding them forever would defeat the cleanup, and there is no chain here to protect.
        return {str(path) for path, _stamp, _size in candidates}

    # Ties are broken by path so that exactly one full is the anchor. Two files sharing a timestamp
    # is not a corner case here: `vm_import_unc` is an SMB share, where mtime resolution can be two
    # seconds, and copies land in the same tick routinely. Compared on the timestamp alone, tied
    # fulls are each "not older than the newest" and every one of them is kept - the cleanup stops
    # deleting anything and reports success while the share fills up.
    #
    # PER CHAIN, not per directory. One anchor for the whole staging tree meant the newest full of
    # ANY database decided every other database's fate: measured 2026-09-14 on a tree holding four,
    # where Sessions's 5 MB full taken 89 seconds later made Orders's 30 GB full "obsolete" while
    # the logs that need it were kept as still_needed - a chain left with no anchor, which is the
    # one outcome this function exists to prevent. Caught by a dry run, not by the suite.
    anchors: dict[str, tuple[float, str]] = {}
    for stamp, text in fulls:
        key = _chain_group(text)
        if key not in anchors or (stamp, text) > anchors[key]:
            anchors[key] = (stamp, text)
    # A chain with no full of its own falls back to the tree's newest, which is what every chain
    # did before. Holding those files instead would be safer still and would also let a database
    # whose full has already been cleaned accumulate logs for ever.
    fallback = max(fulls)

    obsolete = set()
    for path, stamp, _size in candidates:
        text = str(path)
        anchor = anchors.get(_chain_group(text), fallback)
        if backup_kind(text) != "full":
            # A log or diff at the same instant as the newest full may belong to the chain that
            # starts there, so only a strictly older one is obsolete.
            if stamp < anchor[0]:
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


def _staged_by_this_entry(config: BackupRestoreConfig, path: object) -> bool:
    """Is ``path`` under ``<import root>/<a mapped database>/`` - where this entry's copy puts files?

    The cleanup recursed every ``*.bak`` / ``*.trn`` under the import root, so a root set one level
    too high - the target host's own backup or data directory - had the host's own backups deleted
    by age (review 0.25.0, B4.2). The copy writes under the mapped databases' folders only
    (`copy_backup.mapped_database_folders`), so that is all the cleanup may touch. An entry that maps
    no database is limited by the depth rule in `_validate_safe_target_delete_root` instead.
    """
    names = {mapping.source_database.strip().lower() for mapping in config.databases
             if mapping.source_database.strip()}
    if not names:
        return True
    root = str(config.vm_import_unc).replace("\\", "/").rstrip("/").lower()
    text = str(path).replace("\\", "/").lower()
    if not text.startswith(root + "/"):
        return False
    return text[len(root) + 1:].split("/", 1)[0] in names


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
    cutoff = _cutoff_timestamp(restore_config.cleanup_retention, now=now)
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


#: A file past its retention that no proven copy recorded: kept, and named in the result.
KEPT = "KEPT"


def _catalog() -> Any:
    """The backup catalog, or ``None`` when this node has no store to read - then nothing has facts
    and every selected file is kept and named."""
    try:
        from dbabrain.db.backup_catalog import BackupCatalogStore
        from dbabrain.lib.config import load_config

        return BackupCatalogStore.from_config(load_config())
    except Exception:  # noqa: BLE001 - no catalog is "no facts", never a delete without them.
        return None


def _windows_target_host(config: BackupRestoreConfig) -> tuple[dict[str, Any] | None, str]:
    """``(host block, "")`` for the Windows target's own host, or ``(None, why not)``.

    From the inventory: the target server's ``cmd_access`` (WinRM or SSH) - the same login the
    metrics collectors use. A share cannot run the script a verified delete is.
    """
    target_id = str(config.target_id or "").strip()
    if not target_id:
        return None, "the entry names no target_server_id to reach the host by"
    try:
        from dbabrain.backup_restore.backup import _load_secrets, resolve_ssh_target
        from dbabrain.backup_restore.spec_builder import host_block

        target = resolve_ssh_target(target_id, label=f"restore_id={config.restore_id or '?'}",
                                    require_container=False)
        return host_block(target, secrets=_load_secrets(data_dir=None, key=None, key_base64=None)), ""
    except Exception as exc:  # noqa: BLE001 - said as the reason every file is kept.
        return None, f"{target_id} cannot be reached as a host ({exc})"


def _delete_selected(config: BackupRestoreConfig, selected: list[tuple[Any, int]], *,
                     host: dict[str, Any] | None, why_no_host: str = "", host_path_of: Any,
                     must_be_under: str, windows: bool, dry_run: bool, force: bool,
                     catalog: Any = None, logger: logging.Logger | None = None,
                     ) -> list[DeleteBackupFileResult]:
    """Delete the selected files - each by a verified command, or not at all (module docstring).

    ``selected`` is ``(the path as this engine lists it, bytes)``; ``host_path_of`` turns one into
    the path on the target host itself. Without a ``host`` nothing can be held to its facts, so
    every file is kept with ``why_no_host``.
    """
    from dbabrain.backup_restore import verified_cleanup

    _rid = f"restore_id={config.restore_id} " if config.restore_id else ""
    if not selected:
        return []
    if host is None:
        return [DeleteBackupFileResult(target_file=Path(str(path)), status=KEPT, bytes=size,
                                       reason=f"kept: {why_no_host}") for path, size in selected]
    host_paths = {str(path): str(host_path_of(path)) for path, _size in selected}
    sizes = {host_paths[str(path)]: size for path, size in selected}
    listed_as = {host_path: Path(listed) for listed, host_path in host_paths.items()}
    wanted = list(host_paths.values())
    facts: dict[str, dict[str, Any]] = {}
    store = catalog if catalog is not None else _catalog()
    if store is not None:
        # Recorded under the path the copy wrote to - the host's own, or the engine's listing of it.
        try:
            found = store.staged_file_facts(target_server_id=str(config.target_id or ""),
                                            paths=sorted(set(wanted) | set(host_paths)))
        except Exception as exc:  # noqa: BLE001 - a store that cannot be read gives no facts: nothing goes.
            return [DeleteBackupFileResult(target_file=Path(str(path)), status=KEPT, bytes=size,
                                           reason=f"kept: the catalog could not be read ({exc})")
                    for path, size in selected]
        for listed, host_path in host_paths.items():
            row = found.get(host_path) or found.get(listed)
            if row:
                facts[host_path] = row
    cleanup = verified_cleanup.delete_files(host=host, paths=wanted, facts=facts, must_be_under=must_be_under,
                                            windows=windows, force=force, dry_run=dry_run)
    results: list[DeleteBackupFileResult] = []

    def add(host_path: str, status: str, reason: str = "") -> None:
        results.append(DeleteBackupFileResult(target_file=listed_as[host_path], status=status,
                                              bytes=sizes.get(host_path, 0), reason=reason))
        _log_progress(logger, f"{_rid}delete-backup source_id={config.source_id} cleanup_{status.lower()} "
                              f"file={host_path}" + (f" reason={reason}" if reason else ""))

    for host_path in cleanup["deleted"]:
        add(host_path, "DELETED")
    for one in cleanup["forced"]:
        add(one["path"], "DELETED", f"forced: {one.get('size_bytes')} bytes, sha256 {one.get('sha256')}")
    for host_path in cleanup["already_gone"]:
        add(host_path, "NOT_FOUND", "already gone")
    for host_path in cleanup["would_delete"]:
        add(host_path, "DRY_RUN", "would be deleted")
    for one in cleanup["kept"]:
        add(one["path"], KEPT, one["reason"])
    for one in cleanup["not_deleted"]:
        add(one["path"], "FAILED", f"{one['status']} - {one['reason']}")
    return results


def delete_old_target_backup_files_on_share(
    config: BackupRestoreConfig,
    dry_run: bool = False,
    *,
    now: float | None = None,
    force: bool = False,
    logger: logging.Logger | None = None,
) -> tuple[DeleteBackupFileResult, ...]:
    """Clean a Windows target's import folder: listed through its share (``smb-list``), deleted
    through its host.

    The two conditions the other engines apply, decided here: aged past the retention (by last
    write, as the PowerShell scan read it until 0.24.0) and obsolete by the chain. Each chosen file
    is then deleted on the target's own host, by its local path under ``vm_import_local``, held to
    its recorded facts (:func:`_delete_selected`) - never through the share, which has nothing to
    hash it on. ``dry_run`` deletes nothing.
    """
    from dbabrain.backup_restore import share
    from dbabrain.backup_restore.copy_backup import resolve_password_ref

    cutoff = _cutoff_timestamp(config.cleanup_retention, now=now)
    password = resolve_password_ref(config.vm_password_env) if config.vm_password_env else ""
    root = str(config.vm_import_unc).replace("/", "\\").rstrip("\\")
    # A share's paths are Windows paths wherever this runs. As `Path` they were POSIX paths on a
    # Linux node - one component, no split at "\\" - and the chain was read by walking the UNC path,
    # which a Linux node cannot: every aged file was held back and the share was never cleaned
    # (0.24.0, found by `ci`). The listing below is the whole share, and it is all this reads.
    listed: list[tuple[PureWindowsPath, float | None, int]] = []
    for item in share.list_files(config.vm_import_unc, username=config.vm_username or "",
                                 password=password, suffixes=(".bak", ".trn")):
        modified = item.get("modified_epoch")
        listed.append((PureWindowsPath(root + "\\" + str(item["path"]).replace("/", "\\")),
                       None if modified is None else float(modified), int(item.get("size_bytes") or 0)))
    aged = sorted(((modified, path) for path, modified, _size in listed
                   if modified is not None and (cutoff is None or modified <= cutoff)
                   and _staged_by_this_entry(config, path)),
                  key=lambda item: (item[0], str(item[1]).lower()))
    aged_paths = [path for _, path in aged]
    # The chain over the WHOLE share, not the aged part - see `_all_target_backup_files`.
    obsolete = obsolete_only([(path, _backup_timestamp(path, fallback_mtime=modified or 0.0), size)
                              for path, modified, size in listed])
    deletable = [path for path in aged_paths if str(path) in obsolete]
    held_back = [path for path in aged_paths if str(path) not in obsolete]
    results: list[DeleteBackupFileResult] = [
        DeleteBackupFileResult(target_file=path, status="SKIPPED", bytes=0, reason="still_needed")
        for path in held_back
    ]
    if not deletable:
        return tuple(results)
    sizes = {str(path): size for path, _modified, size in listed}
    local_root = str(config.vm_import_local).replace("/", "\\").rstrip("\\")

    def on_the_host(path: Any) -> str:
        return local_root + "\\" + str(path)[len(root):].lstrip("\\")

    host, why = _windows_target_host(config)
    results.extend(_delete_selected(
        config, [(path, sizes.get(str(path), 0)) for path in deletable], host=host, why_no_host=why,
        host_path_of=on_the_host, must_be_under=local_root, windows=True, dry_run=dry_run, force=force,
        logger=logger))
    return tuple(results)


def delete_old_target_backup_files_via_ssh(
    config: BackupRestoreConfig,
    *,
    now: float | None = None,
    logger: logging.Logger | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> tuple[DeleteBackupFileResult, ...]:
    cutoff = _cutoff_timestamp(config.cleanup_retention, now=now)
    linux_import = str(config.vm_import_unc).replace("\\", "/")
    results: list[DeleteBackupFileResult] = []
    _rid = f"restore_id={config.restore_id} " if config.restore_id else ""

    with open_ssh_connection(config) as ssh:
        # List all .bak/.trn files with mtime and size; filter by cutoff in Python.
        answer = ssh.run(
            f'find {shlex.quote(linux_import)} -type f \\( -name "*.bak" -o -name "*.trn" \\) '
            f'-printf "%T@ %s %p\\n" 2>/dev/null || true'
        )
        lines = answer.stdout.splitlines()
        files_to_delete = []
        scanned: list[tuple[str, float, int]] = []
        _log_progress(
            logger=logger,
            message=(
                f"{_rid}delete-backup source_id={config.source_id} cleanup_scan "
                f"root={linux_import} cleanup_retention={config.cleanup_retention} scanned_files={len(lines)}"
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
            if not _staged_by_this_entry(config, fpath):
                continue
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

        # Each by its own verified delete (0.38.0): the listing above chose; this holds every file to
        # what the copy that staged it measured on this host - `rm -f` held it to nothing.
        results.extend(_delete_selected(
            config, [(fpath, size) for fpath, size, _backup_ts in files_to_delete], host=ssh.host_block(),
            host_path_of=str, must_be_under=linux_import.rstrip("/"), windows=False, dry_run=dry_run,
            force=force, logger=logger))

    return tuple(results)


def target_share_login_request(config: BackupRestoreConfig) -> dict[str, str] | None:
    """The Windows target's share login, for ``smb-credential``; ``None`` on a Linux target."""
    if config.is_linux:
        return None
    return share_login_request(
        credential_target=config.vm_credential_target,
        username=config.vm_username,
        password_env=config.vm_password_env,
    )


def should_clean_through_the_share(config: BackupRestoreConfig) -> bool:
    """A Windows node cleaning a UNC import folder goes through ``smb-list`` / ``smb-delete``."""
    return os.name == "nt" and str(config.vm_import_unc).startswith("\\\\")


def run_delete_backup(
    config: BackupRestoreConfig | None = None,
    *,
    logger: logging.Logger | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> DeleteBackupResult:
    """Retention cleanup on the restore target.

    ``force`` (an operator, by hand - never a scheduled run): a chosen file no proven copy recorded
    is deleted too, by its own forced command; without it such a file is kept and named.

    ``dry_run`` reaches every engine, and it has to: `restore-workflow --dry-run` passed the flag
    to the copy and the restore and not to this, so a dry run of a real drill deleted 26 files
    on 2026-09-05 while reporting itself as a plan. They were past retention and would have gone
    on the next real run, which is luck rather than design - a dry run may not change anything.
    """
    if not dry_run:
        # Nothing deleted while a lockdown is on (1.220).
        from dbabrain.db.lockdown_store import Locked, lockdown_on

        if lockdown_on():
            raise Locked("delete-backup is refused: a lockdown is on (control.cli lockdown status).")
    restore_config = config or load_restore_config()
    _validate_safe_target_delete_root(restore_config)
    validate_restore_target_is_not_source(restore_config)
    _rid = f"restore_id={restore_config.restore_id} " if restore_config.restore_id else ""
    _log_progress(
        logger,
        (
            f"{_rid}delete-backup source_id={restore_config.source_id} start "
            f"cleanup_root={restore_config.vm_import_unc} cleanup_retention={restore_config.cleanup_retention} "
            f"platform={restore_config.vm_platform}"
        ),
    )

    credential_request = target_share_login_request(restore_config)
    _log_progress(logger, f"{_rid}delete-backup source_id={restore_config.source_id} target_smb_credential_commands={1 if credential_request else 0}")
    if credential_request:
        store_share_logins([credential_request])

    if restore_config.is_linux:
        delete_engine = "ssh"
    elif should_clean_through_the_share(restore_config):
        delete_engine = "share"
    else:
        delete_engine = "python"

    _log_progress(logger, f"{_rid}delete-backup source_id={restore_config.source_id} scanning engine={delete_engine}")
    if delete_engine == "ssh":
        file_results = delete_old_target_backup_files_via_ssh(
            restore_config, logger=logger, dry_run=dry_run, force=force)
    elif delete_engine == "share":
        file_results = delete_old_target_backup_files_on_share(
            restore_config, dry_run=dry_run, force=force, logger=logger)
    else:
        aged_files = [path for path in list_old_target_backup_files(restore_config)
                      if _staged_by_this_entry(restore_config, path)]
        selected_files, held_back = _split_by_obsolete(aged_files, root=restore_config.vm_import_unc)
        _log_progress(logger, f"{_rid}delete-backup source_id={restore_config.source_id} cleanup_root={restore_config.vm_import_unc} cleanup_retention={restore_config.cleanup_retention} aged_files={len(aged_files)} selected_files={len(selected_files)} still_needed={len(held_back)}")
        file_results_list: list[DeleteBackupFileResult] = [
            DeleteBackupFileResult(target_file=path, status="SKIPPED", bytes=0,
                                   reason="still_needed")
            for path in held_back
        ]
        # The folder is on this machine (or a UNC path this node opens): the files are deleted
        # through the target's own host all the same - this machine when the path is local.
        root = str(restore_config.vm_import_unc)
        if root.startswith("\\\\"):
            host, why = _windows_target_host(restore_config)
            local_root = str(restore_config.vm_import_local).replace("/", "\\").rstrip("\\")

            def on_the_host(path: Any, _root: str = root.rstrip("\\/"), _local: str = local_root) -> str:
                return _local + str(path)[len(_root):].replace("/", "\\")
        else:
            host, why = {"runtime": "windows" if os.name == "nt" else "linux"}, ""
            local_root, on_the_host = root.rstrip("\\/"), str
        sized = []
        for target_file in selected_files:
            try:
                sized.append((target_file, target_file.stat().st_size))
            except OSError:
                sized.append((target_file, 0))
        file_results_list.extend(_delete_selected(
            restore_config, sized, host=host, why_no_host=why, host_path_of=on_the_host,
            must_be_under=local_root, windows=os.name == "nt" or root.startswith("\\\\"), dry_run=dry_run,
            force=force, logger=logger))
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
        cleanup_retention=restore_config.cleanup_retention,
        files_considered=len(file_results),
        deleted=sum(1 for item in file_results if item.status == "DELETED"),
        file_results=file_results,
        skipped=sum(1 for item in file_results if item.status == "SKIPPED"),
        kept_unknown=sum(1 for item in file_results if item.status == KEPT),
    )


def _cutoff_timestamp(seconds: int, *, now: float | None) -> float | None:
    """The instant a file must be older than to be a candidate. ``0`` removes the age gate.

    Seconds, because that is the unit every interval in dbabrain is stated in. It took hours until
    2026-09-11, and the retention (in seconds) was converted with ``max(1, seconds // 3600)`` on
    the way in - so any configured value under an hour silently became one hour, and nothing said
    so. A setting the tool quietly replaces is worse than one it refuses.
    """
    if seconds <= 0:
        return None
    return (time.time() if now is None else now) - seconds


def _backup_timestamp(path: Path, *, fallback_mtime: float) -> float:
    stamped = backup_time_from_name(path.name)
    return fallback_mtime if stamped is None else stamped


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
    # An entry that maps no database has no folder to keep the cleanup in (`_staged_by_this_entry`),
    # so its root must be deep: `/data` passed and the cleanup recursed the whole of it (review
    # 0.25.0, B4.2). Three directories below `/` - `/opt/db_ops/staging` - and anything deeper.
    if config.is_linux and not config.databases and len(parts) < 4:
        raise ValueError(
            f"Unsafe delete-backup config: target backup folder {config.vm_import_unc} is too broad for an "
            "entry that maps no database - use a directory at least three levels below / , or map the "
            "databases the entry restores.")


def _normalize_path_for_compare(path: Path, is_linux: bool) -> str:
    s = str(path).strip()
    if is_linux:
        s = s.replace("\\", "/")  # WindowsPath renders linux paths with backslashes
        return s.rstrip("/").lower()
    return s.rstrip("\\/").lower().replace("/", "\\")




