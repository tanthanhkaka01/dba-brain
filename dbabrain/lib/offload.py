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

import copy
from dataclasses import dataclass, field
from typing import Any

from dbabrain.lib import cleanup_retention as R
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
#: How bytes travel over SSH - ``copy-files``' two transfers, the one list (lib.restore.copy_mode).
from dbabrain.lib.restore.copy_mode import TRANSFERS  # noqa: E402 - beside the fields that use it

OFFLOAD_FIELDS: dict[str, Any] = {
    "offload_enabled": False,
    "offload_server_id": "",
    "offload_folder": "",
    "offload_copy_max_retry": 3,
    "offload_scan": "none",
    "offload_delete_source": False,
    # Whether the source keeps its newest chain once offload_delete_source releases (2026-11-11, the
    # operator: *a running server never restores - no backup needs to stay on the server_id*). true
    # (the value written, as it ran): the newest chain of each database stays on the server. false:
    # every file goes once its repository copy is verified (and encrypted, when the entry encrypts),
    # but what the next backup reads - the newest base backup's backup_manifest, which an incremental
    # chains onto.
    "offload_source_keeps_newest_chain": True,
    # The first backup the repository takes, as a UTC moment (2026-11-11, the operator: *only from the
    # new FULL - the old backups keep the old behaviour and fade out*): a file the entry wrote before it
    # is never copied, its server's own retention keeps owning it. "" (the value written) takes every
    # file still on the server, as an entry switched on has always done.
    "offload_files_since": "",
    # Whether a copy may replace a file already at its name on the repository (0.37.0, the operator
    # 2026-11-10: *required, and 0 - do not overwrite - is the default*). false leaves it as it is; the
    # repository's hash still proves it, or the row fails with the reason.
    "offload_copy_overwrite": False,
    # Whether each proven copy is encrypted with age AT the repository, then proven by decrypting it,
    # before the plain file goes - there and at the source (0.37.0, the operator 2026-11-10: *one
    # encryption for every database type, at the shared repository; the option required, off by
    # default*). The key encrypts for offload_encrypt_key_days from when it is made (*90 days by
    # default; past it the old key only decrypts*), then a new one is made; no key is ever deleted.
    "offload_encrypt": False,
    "offload_encrypt_key_days": 90,
    # How the bytes travel over SSH to and from the repository (0.37.0, the operator 2026-11-10:
    # *standardise SFTP and cat in the common cli, set in the shared config*): "cat" - a plain cat
    # channel with a large window, read ahead (43 MB/s measured into the repository's NFS folder) -
    # or "sftp" (25 MB/s there), for a login that allows SFTP and no command.
    "offload_transfer": "cat",
    # Whether each pass reads the server's repository folder and fails when it holds what the catalog
    # does not know, lacks a proven copy, or holds one of other bytes (0.37.0, the operator 2026-11-10:
    # *a stray file in /mnt/nas/<server_id> fails the backup run and is reported to its group*). It
    # reads and reports only - nothing is deleted - so it is written on (lib.repository_check).
    "offload_repository_check": True,
    "offload_source_share": "",
    "offload_source_username": "",
    "offload_source_password_ref": "",
    # How long the repository keeps each copy, by its own file type, in seconds (0.37.0, the operator
    # 2026-11-11: *one array - the file type and how long it is kept*; *dbabrain is seconds
    # throughout*): {"FULL": 4320000, "LOG": 8640000, "default": "keep"}. A type not named - and
    # every type without "default" - is kept for good; "keep" says so outright. Written {}: nothing is
    # ever deleted on the operator's behalf. See parse_repository_retention.
    "offload_retention": {},
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

#: The three fields offload_retention replaced (0.37.0), one per level: seconds, 0 for good.
#: upgrade-config turns them into offload_retention (retention_from_levels) and drops them.
LEGACY_RETENTION_FIELDS = ("offload_retention_full", "offload_retention_diff", "offload_retention_log")
RETENTION_FIELD = "offload_retention"
#: Types the repository's retention never names, and why: each is kept or goes with something else.
RETENTION_NOT_NAMED = {
    "CERT": "a certificate is never copied to the repository",
    "MANIFEST": "a manifest goes with the base backup of its run",
    "CONTROLFILE": "a control file goes with the full of its run",
    "SPFILE": "an spfile goes with the full of its run",
}


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
    #: Seconds the repository keeps a copy, per file type; ``None`` (a type not named, "keep") = for good.
    retention: R.Retention = field(default_factory=lambda: R.Retention(default=None))
    #: How the pass runs - see OFFLOAD_FIELDS.
    files_per_pass: int = 100
    copy_retry_interval_seconds: int = 30
    retry_interval_seconds: int = 3600
    run_timeout_seconds: int = 86400
    copy_timeout_seconds: int = 86400
    hash_timeout_seconds: int = 21600
    stall_seconds: int = 300
    #: Whether a copy may replace a file already at its name on the repository.
    copy_overwrite: bool = False
    #: Whether each proven copy is encrypted with age at the repository, and a key's lifetime in days.
    encrypt: bool = False
    encrypt_key_days: int = 90
    #: How the bytes travel over SSH: "cat" or "sftp" (common.filecopy.TRANSFERS).
    transfer: str = "cat"
    #: Whether each pass checks the server's repository folder holds exactly its proven copies.
    repository_check: bool = True
    #: Whether a release leaves the newest chain on the server (false: only the newest base's manifest).
    source_keeps_newest_chain: bool = True
    #: The first backup copied, "YYYY-MM-DDTHH:MM:SSZ"; "" = every file still on the server.
    files_since: str = ""

    def retention_of(self, file_type: str) -> int | None:
        """Seconds a copy of ``file_type`` is kept on the repository; ``None`` keeps it for good. A file
        with no level of its own (a control file, an spfile, a manifest) goes with the full of its
        run, so it is held to that full's window - never one that would keep the full for ever."""
        from dbabrain.lib.integrity import LEVEL_OF_FILE_TYPE

        kind = str(file_type or "").strip().upper()
        if kind in LEVEL_OF_FILE_TYPE:
            return self.retention.window_for(kind)
        for base in R.BASE_TYPE.values():
            if base in self.retention.windows:
                return self.retention.windows[base]
        return self.retention.default

    def shortest_retention(self) -> int | None:
        """The shortest window stated - ``None`` when every type is kept for good."""
        stated = [w for w in [*self.retention.windows.values(), self.retention.default] if w is not None]
        return min(stated) if stated else None

    def as_record(self) -> dict[str, Any]:
        return {"offload_enabled": self.enabled, "offload_server_id": self.server_id,
                "offload_folder": self.folder, "offload_copy_max_retry": self.copy_max_retry,
                "offload_scan": self.scan, "offload_delete_source": self.delete_source,
                "offload_copy_overwrite": self.copy_overwrite,
                "offload_encrypt": self.encrypt, "offload_encrypt_key_days": self.encrypt_key_days,
                "offload_transfer": self.transfer, "offload_repository_check": self.repository_check,
                "offload_source_keeps_newest_chain": self.source_keeps_newest_chain,
                "offload_files_since": self.files_since,
                "offload_source_share": self.source_share, "offload_source_username": self.source_username,
                "offload_source_password_ref": self.source_password_ref,
                RETENTION_FIELD: retention_as_config(self.retention),
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
    old = [name for name in LEGACY_RETENTION_FIELDS if name in raw]
    if old:
        raise OffloadConfigError(f"{label}: {', '.join(old)} {'is' if len(old) == 1 else 'are'} replaced by "
                                 f"{RETENTION_FIELD} (seconds per file type) - run upgrade-config, which "
                                 "writes the same windows there.")
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
    retention = parse_repository_retention(raw[RETENTION_FIELD], label=label, db_type=db_type)
    run_values: dict[str, int] = {}
    for name, (low, high) in RUN_FIELD_RANGES.items():
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
            span = f"{low} to {high}" if high is not None else f"{low} or more"
            raise OffloadConfigError(f"{label}: {name} must be a whole number, {span}, got {value!r}.")
        run_values[name[len("offload_"):]] = value
    key_days = raw["offload_encrypt_key_days"]
    if isinstance(key_days, bool) or not isinstance(key_days, int) or not 1 <= key_days <= 3650:
        raise OffloadConfigError(f"{label}: offload_encrypt_key_days must be a whole number of days, 1 to "
                                 f"3650 (a key encrypts that long, then only decrypts), got {key_days!r}.")
    transfer = text("offload_transfer").lower()
    if transfer not in TRANSFERS:
        raise OffloadConfigError(f"{label}: offload_transfer must be one of {', '.join(TRANSFERS)}, "
                                 f"got {raw['offload_transfer']!r}.")
    scan = text("offload_scan").lower()
    if scan not in SCANNERS:
        raise OffloadConfigError(f"{label}: offload_scan must be one of {', '.join(SCANNERS)}, got {raw['offload_scan']!r}.")
    parsed = BackupOffload(
        enabled=flag("offload_enabled"), server_id=text("offload_server_id"), folder=text("offload_folder"),
        copy_max_retry=retry, scan=scan, delete_source=flag("offload_delete_source"),
        source_share=text("offload_source_share"), source_username=text("offload_source_username"),
        source_password_ref=text("offload_source_password_ref"),
        retention=retention, copy_overwrite=flag("offload_copy_overwrite"),
        encrypt=flag("offload_encrypt"), encrypt_key_days=key_days, transfer=transfer,
        repository_check=flag("offload_repository_check"),
        source_keeps_newest_chain=flag("offload_source_keeps_newest_chain"),
        files_since=_moment(text("offload_files_since"), label=f"{label}.offload_files_since"), **run_values)
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
    if parsed.encrypt and not parsed.enabled:
        raise OffloadConfigError(f"{label}: offload_encrypt is true but offload_enabled is false - a file is "
                                 "encrypted on the repository, after its copy there is proven.")
    if parsed.delete_source and not parsed.enabled:
        raise OffloadConfigError(f"{label}: offload_delete_source is true but offload_enabled is false - a "
                                 "source file is deleted only after its copy on the repository is proven.")
    return parsed


def _moment(value: str, *, label: str) -> str:
    """A UTC moment as the catalog writes them (``YYYY-MM-DDTHH:MM:SSZ``), or ``""``. The catalog's
    times compare as text, so another spelling of the same moment would compare wrong - refused."""
    import re
    from datetime import datetime

    if value == "":
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise OffloadConfigError(f"{label} must be a UTC moment written YYYY-MM-DDTHH:MM:SSZ (or \"\" for every "
                                 f"file still on the server), got {value!r}.")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise OffloadConfigError(f"{label}: {value!r} is not a real moment.") from None
    return value


def parse_repository_retention(raw: Any, *, label: str = "backup", db_type: str = "") -> R.Retention:
    """``offload_retention``: seconds per file type on the repository (0.37.0).

    ``{"FULL": 4320000, "LOG": 8640000, "default": "keep"}`` - each key a file type its engine writes
    (the catalog's own words, ``lib.cleanup_retention.ENGINE_FILE_TYPES``) or ``default``; each value
    whole seconds above 0, or ``"keep"``. A type not named takes ``default``; with no ``default`` it
    is kept for good, so ``{}`` deletes nothing. Refused: a type its engine never writes (a typo
    would otherwise keep that type for good without a word), one the repository never holds apart
    (``RETENTION_NOT_NAMED``), 0 (in ``cleanup_retention`` it means *the chain rule alone*: on the
    repository that leaves only the newest chain - say the seconds), and an incremental that needs
    its whole chain kept other than its base (``chain_member_split``: a set that looks present and
    restores nothing)."""
    where = f"{label}.{RETENTION_FIELD}"
    if not isinstance(raw, dict):
        raise OffloadConfigError(f"{where} must be an object of file type -> seconds or \"keep\" - e.g. "
                                 f"{{\"FULL\": 4320000, \"LOG\": 8640000}}; {{}} keeps every copy for good. "
                                 f"Got {raw!r}.")
    from dbabrain.lib.integrity import LEVEL_OF_FILE_TYPE

    engine = R.ENGINE_ALIASES.get(str(db_type or "").strip().lower(), str(db_type or "").strip().lower())
    written = R.ENGINE_FILE_TYPES.get(engine, R.ALL_FILE_TYPES)
    allowed = [t for t in written if t in LEVEL_OF_FILE_TYPE]
    windows: dict[str, int | None] = {}
    default: int | None = None
    for key, value in raw.items():
        name = str(key).strip()
        if value is None or isinstance(value, bool) or (isinstance(value, str) and value.strip().lower() != R.KEEP) \
                or not isinstance(value, (int, str)):
            raise OffloadConfigError(f"{where}.{name} must be whole seconds above 0 or \"keep\", got {value!r}.")
        window = None if isinstance(value, str) else int(value)
        if window is not None and window <= 0:
            raise OffloadConfigError(f"{where}.{name} is {value!r}: a window is whole seconds above 0, or \"keep\" "
                                     "for good - 0 would leave only the newest chain on the repository.")
        if name.lower() == R.DEFAULT_KEY:
            default = window
            continue
        upper = name.upper()
        if upper in RETENTION_NOT_NAMED:
            raise OffloadConfigError(f"{where}: {name} is not named here - {RETENTION_NOT_NAMED[upper]}.")
        if upper not in allowed:
            whose = f"a {engine} backup writes" if engine in R.ENGINE_FILE_TYPES else "dbabrain knows"
            raise OffloadConfigError(f"{where}: {name!r} is not a file type {whose} - one of "
                                     f"{', '.join(allowed)}, or default.")
        if upper in windows:
            raise OffloadConfigError(f"{where}: {upper} is named twice.")
        windows[upper] = window
    retention = R.Retention(windows=windows, default=default)
    if engine in R.ENGINE_FILE_TYPES:
        split = R.chain_member_split(retention, engine=engine)
        if split:
            raise OffloadConfigError(f"{where}: {split}.")
    return retention


def retention_problems(value: Any) -> list[str]:
    """What is wrong with an ``offload_retention`` value, for the reference's checker (no engine known)."""
    try:
        parse_repository_retention(value, label="backup")
    except OffloadConfigError as exc:
        return [str(exc)]
    return []


def retention_as_config(retention: R.Retention) -> dict[str, Any]:
    """The value as an entry states it: each type named, and ``default`` only when one was given."""
    value: dict[str, Any] = {kind: (R.KEEP if w is None else w) for kind, w in retention.windows.items()}
    if retention.default is not None:
        value[R.DEFAULT_KEY] = retention.default
    return value


def retention_from_levels(entry: dict[str, Any], *, db_type: str = "") -> dict[str, Any]:
    """``offload_retention`` from the three level fields it replaced: each of the engine's types by
    its level, 0 as "keep"; an incremental that needs its chain left to its base's window. All three
    0 (every entry when they were replaced) is ``{}``."""
    from dbabrain.lib.integrity import LEVEL_OF_FILE_TYPE

    levels = dict(zip(("full", "diff", "log"), (entry.get(name) for name in LEGACY_RETENTION_FIELDS)))
    seconds = {level: (v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None)
               for level, v in levels.items()}
    if all(v is None for v in seconds.values()):
        return {}
    engine = R.ENGINE_ALIASES.get(str(db_type or "").strip().lower(), str(db_type or "").strip().lower())
    types = [t for t in R.ENGINE_FILE_TYPES.get(engine, R.ALL_FILE_TYPES) if t in LEVEL_OF_FILE_TYPE]
    chain = set(R.CHAIN_MEMBERS.values())
    return {t: (R.KEEP if seconds[LEVEL_OF_FILE_TYPE[t]] is None else seconds[LEVEL_OF_FILE_TYPE[t]])
            for t in types if t not in chain}


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
        label = str(entry.get("backup_id") or f"backups[{index}]")
        old = [name for name in LEGACY_RETENTION_FIELDS if name in entry]
        if old:
            # 0.37.0: the three level fields become one per-type object, the same windows.
            if RETENTION_FIELD not in entry:
                entry[RETENTION_FIELD] = retention_from_levels(entry, db_type=str(entry.get("db_type") or ""))
            for name in old:
                del entry[name]
            changes.append({"where": f"backup_restore.backups[{label}]", "field": RETENTION_FIELD,
                            "value": f"{entry[RETENTION_FIELD]} - from {', '.join(old)}, the same windows"})
        added = [name for name in OFFLOAD_FIELDS if name not in entry]
        for name in added:
            entry[name] = copy.deepcopy(OFFLOAD_FIELDS[name])
        if added:
            changes.append({"where": f"backup_restore.backups[{label}]", "field": "offload_*",
                            "value": f"{len(added)} field(s) stated - offload off, as the entry ran"})
    for index, entry in enumerate(section.get("restores") if isinstance(section.get("restores"), list) else []):
        if isinstance(entry, dict) and "restore_from" not in entry:
            entry["restore_from"] = STATED_RESTORE_FROM
            label = str(entry.get("restore_id") or f"restores[{index}]")
            changes.append({"where": f"backup_restore.restores[{label}]", "field": "restore_from",
                            "value": f"{STATED_RESTORE_FROM}, stated - what the entry has always done"})
    return changes
