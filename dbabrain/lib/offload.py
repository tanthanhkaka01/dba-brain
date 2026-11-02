"""Where a backup goes after it is written, and where a restore reads it from (0.34.0).

The operator, 2026-10-30: the backup flow copies each run's files (FULL, DIFF, LOG/WAL) to a shared
folder or another VM - **the repository** - checks checksum and bytes there as a restore does,
scans them if asked, and only then deletes the files on the database server. A restore can read
from the repository instead. *Every field is mandatory: an entry that lacks one gets it added.*

So every backup entry states the ``offload_*`` fields and every restore entry states
``restore_from``. ``upgrade-config`` (``stated-integrity``) and the registrars write them into an
entry that lacks them, with the values that keep today's flow - offload off, restore from the
server - so nothing changes until an entry turns the new flow on (R50: never a code default).

The rules are here once, for every reader: the backup loader, the restore loaders, the registrars,
``upgrade-config``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dbabrain.lib import errors
# One limit for every copy - a restore's and an offload's (lib.integrity).
from dbabrain.lib.integrity import MAX_COPY_MAX_RETRY

#: Scanners the offload can run on the repository. ``none`` scans nothing.
SCANNERS = ("none", "clamav")
#: Where a restore entry reads its backups from.
RESTORE_FROM = ("server", "repository")
STATED_RESTORE_FROM = "server"

#: Every backup entry's offload fields, with the value written into an entry that states none -
#: the value that keeps the flow it had: no offload.
OFFLOAD_FIELDS: dict[str, Any] = {
    "offload_enabled": False,
    "offload_server_id": "",
    "offload_folder": "",
    "offload_copy_max_retry": 3,
    "offload_scan": "none",
    "offload_delete_source": False,
    "offload_source_share": "",
    "offload_source_username": "",
    "offload_source_password_ref": "",
    # How long the repository keeps each copy, in seconds, by the level of its file type
    # (lib.integrity.LEVEL_OF_FILE_TYPE): FULL / base / RMAN L0 (and the certificate, control file
    # and spfile that travel with a full), DIFF / incremental / RMAN L1, LOG / WAL / archivelog.
    # 0 keeps it for good - the value written, so nothing is ever deleted on the operator's behalf.
    "offload_retention_full": 0,
    "offload_retention_diff": 0,
    "offload_retention_log": 0,
    # How the offload pass runs (0.34.0, the operator 2026-10-31: *every config complete and usable,
    # dynamic, nothing hard-coded*). Written as the values the pass was first built with.
    "offload_files_per_pass": 100,               # catalog rows one pass takes per entry
    "offload_copy_retry_interval_seconds": 30,   # pause between two copies of one file in a run
    "offload_retry_interval_seconds": 3600,      # a failed copy is tried again by a pass after this
    "offload_run_timeout_seconds": 86400,        # a run that died is reaped after this
    "offload_copy_timeout_seconds": 86400,       # one copy-files call (a round of copies)
    "offload_hash_timeout_seconds": 21600,       # one file-checksum call
    "offload_stall_seconds": 300,                # a read or write that stops moving this long fails its file
}

#: The run fields: name -> (lowest, highest) accepted. A pause of 0 is allowed; a timeout is not.
RUN_FIELD_RANGES: dict[str, tuple[int, int | None]] = {
    "offload_files_per_pass": (1, 10000),
    "offload_copy_retry_interval_seconds": (0, 3600),
    "offload_retry_interval_seconds": (0, None),
    "offload_run_timeout_seconds": (60, None),
    "offload_copy_timeout_seconds": (60, None),
    "offload_hash_timeout_seconds": (60, None),
    "offload_stall_seconds": (10, 86400),
}

#: The retention field that governs each level.
RETENTION_FIELD_OF_LEVEL = {"full": "offload_retention_full", "diff": "offload_retention_diff",
                            "log": "offload_retention_log"}


class OffloadConfigError(errors.ConfigError):
    """An entry's offload or restore_from fields are missing, mistyped or contradict each other."""


@dataclass(frozen=True)
class BackupOffload:
    """One backup entry's repository step, as stated."""

    enabled: bool
    server_id: str
    folder: str
    copy_max_retry: int
    scan: str
    delete_source: bool
    source_share: str
    source_username: str
    source_password_ref: str
    #: Seconds the repository keeps a copy, per level; 0 = for good.
    retention_full: int = 0
    retention_diff: int = 0
    retention_log: int = 0
    #: How the pass runs - see OFFLOAD_FIELDS.
    files_per_pass: int = 100
    copy_retry_interval_seconds: int = 30
    retry_interval_seconds: int = 3600
    run_timeout_seconds: int = 86400
    copy_timeout_seconds: int = 86400
    hash_timeout_seconds: int = 21600
    stall_seconds: int = 300

    def retention_of(self, file_type: str) -> int:
        """Seconds a copy of ``file_type`` is kept on the repository (0 = for good). A file with no
        level (a certificate, a control file, an spfile) belongs to the full it travels with."""
        from dbabrain.lib.integrity import LEVEL_OF_FILE_TYPE

        level = LEVEL_OF_FILE_TYPE.get(str(file_type or "").upper(), "full")
        return {"full": self.retention_full, "diff": self.retention_diff, "log": self.retention_log}[level]

    def as_record(self) -> dict[str, Any]:
        return {"offload_enabled": self.enabled, "offload_server_id": self.server_id,
                "offload_folder": self.folder, "offload_copy_max_retry": self.copy_max_retry,
                "offload_scan": self.scan, "offload_delete_source": self.delete_source,
                "offload_source_share": self.source_share, "offload_source_username": self.source_username,
                "offload_source_password_ref": self.source_password_ref,
                "offload_retention_full": self.retention_full, "offload_retention_diff": self.retention_diff,
                "offload_retention_log": self.retention_log,
                **{name: getattr(self, name[len("offload_"):]) for name in RUN_FIELD_RANGES}}


def parse_backup_offload(entry: dict[str, Any] | None, *, label: str = "backup",
                         db_type: str = "") -> BackupOffload | None:
    """An entry's offload fields; ``None`` when it states **none** of them (an entry from before
    0.34.0 that ``upgrade-config`` has not reached). Some but not all is refused by name - a half
    written block is a mistake, not an older entry."""
    raw = entry or {}
    present = [name for name in OFFLOAD_FIELDS if name in raw]
    if not present:
        return None
    missing = [name for name in OFFLOAD_FIELDS if name not in raw]
    if missing:
        raise OffloadConfigError(f"{label}: states {', '.join(present[:2])}... but not {', '.join(missing)} - "
                                 "every offload field is stated (upgrade-config writes the ones missing).")

    def flag(name: str) -> bool:
        value = raw[name]
        if not isinstance(value, bool):
            raise OffloadConfigError(f"{label}: {name} must be true or false, got {value!r}.")
        return value

    def text(name: str) -> str:
        value = raw[name]
        if not isinstance(value, str):
            raise OffloadConfigError(f"{label}: {name} must be text, got {value!r}.")
        return value.strip()

    retry = raw["offload_copy_max_retry"]
    if isinstance(retry, bool) or not isinstance(retry, int) or not 0 <= retry <= MAX_COPY_MAX_RETRY:
        raise OffloadConfigError(f"{label}: offload_copy_max_retry must be a whole number 0 to "
                                 f"{MAX_COPY_MAX_RETRY}, got {retry!r}.")
    retention = {}
    for name in RETENTION_FIELD_OF_LEVEL.values():
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise OffloadConfigError(f"{label}: {name} must be whole seconds, 0 or more (0 keeps the copy "
                                     f"for good), got {value!r}.")
        retention[name] = value
    run_values: dict[str, int] = {}
    for name, (low, high) in RUN_FIELD_RANGES.items():
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
            span = f"{low} to {high}" if high is not None else f"{low} or more"
            raise OffloadConfigError(f"{label}: {name} must be a whole number, {span}, got {value!r}.")
        run_values[name[len("offload_"):]] = value
    scan = text("offload_scan").lower()
    if scan not in SCANNERS:
        raise OffloadConfigError(f"{label}: offload_scan must be one of {', '.join(SCANNERS)}, got {raw['offload_scan']!r}.")
    parsed = BackupOffload(
        enabled=flag("offload_enabled"), server_id=text("offload_server_id"), folder=text("offload_folder"),
        copy_max_retry=retry, scan=scan, delete_source=flag("offload_delete_source"),
        source_share=text("offload_source_share"), source_username=text("offload_source_username"),
        source_password_ref=text("offload_source_password_ref"),
        retention_full=retention["offload_retention_full"], retention_diff=retention["offload_retention_diff"],
        retention_log=retention["offload_retention_log"], **run_values)
    if parsed.enabled:
        empty = [name for name, value in (("offload_server_id", parsed.server_id), ("offload_folder", parsed.folder))
                 if not value]
        if empty:
            raise OffloadConfigError(f"{label}: offload_enabled is true, so {' and '.join(empty)} must name "
                                     "the repository and the folder its copies go to.")
        if parsed.source_share and not (parsed.source_username and parsed.source_password_ref):
            raise OffloadConfigError(f"{label}: offload_source_share needs offload_source_username and "
                                     "offload_source_password_ref - the login the worker reads the share with.")
    if parsed.delete_source and str(db_type or "").strip().lower() == "oracle":
        # Deleting a piece behind RMAN's back leaves RMAN listing it, and the next RESTORE picks it.
        raise OffloadConfigError(f"{label}: offload_delete_source cannot be true on Oracle - RMAN owns its "
                                 "pieces; delete them with RMAN (DELETE OBSOLETE), never by file.")
    if parsed.delete_source and not parsed.enabled:
        raise OffloadConfigError(f"{label}: offload_delete_source is true but offload_enabled is false - a "
                                 "source file is deleted only after its copy on the repository is proven.")
    return parsed


def require_backup_offload(offload: BackupOffload | None, *, label: str = "backup") -> BackupOffload:
    """The entry's offload, or a refusal naming the entry (R50)."""
    if offload is None:
        raise OffloadConfigError(f"{label} states no offload fields. Run upgrade-config (it writes "
                                 "offload_enabled false and the rest) or state them.")
    return offload


def parse_restore_from(entry: dict[str, Any] | None, *, label: str = "restore") -> str | None:
    """A restore entry's ``restore_from``; ``None`` when unstated."""
    raw = (entry or {}).get("restore_from")
    if raw is None:
        return None
    value = raw.strip().lower() if isinstance(raw, str) else raw
    if value not in RESTORE_FROM:
        raise OffloadConfigError(f"{label}: restore_from must be one of {', '.join(RESTORE_FROM)}, got {raw!r}.")
    return value


def require_restore_from(value: str | None, *, label: str = "restore") -> str:
    if value is None:
        raise OffloadConfigError(f"{label} states no restore_from. Run upgrade-config (it writes "
                                 f"\"restore_from\": \"{STATED_RESTORE_FROM}\") or state it.")
    return value


def state_offload(document: dict[str, Any]) -> list[dict[str, str]]:
    """Write the missing offload fields into every backup entry and ``restore_from`` into every
    restore entry of a ``restore_config.json`` - the values that keep today's flow. What is stated
    is left as it is. One change per entry written."""
    changes: list[dict[str, str]] = []
    section = document.get("backup_restore") if isinstance(document, dict) else None
    if not isinstance(section, dict):
        return changes
    for index, entry in enumerate(section.get("backups") if isinstance(section.get("backups"), list) else []):
        if not isinstance(entry, dict):
            continue
        added = [name for name in OFFLOAD_FIELDS if name not in entry]
        for name in added:
            entry[name] = OFFLOAD_FIELDS[name]
        if added:
            label = str(entry.get("backup_id") or f"backups[{index}]")
            changes.append({"where": f"backup_restore.backups[{label}]", "field": "offload_*",
                            "value": f"{len(added)} field(s) stated - offload off, as the entry ran"})
    for index, entry in enumerate(section.get("restores") if isinstance(section.get("restores"), list) else []):
        if isinstance(entry, dict) and "restore_from" not in entry:
            entry["restore_from"] = STATED_RESTORE_FROM
            label = str(entry.get("restore_id") or f"restores[{index}]")
            changes.append({"where": f"backup_restore.restores[{label}]", "field": "restore_from",
                            "value": f"{STATED_RESTORE_FROM}, stated - what the entry has always done"})
    return changes
