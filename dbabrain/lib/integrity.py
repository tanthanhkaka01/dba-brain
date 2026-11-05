"""What a backup file is, and what a restore must prove about it before it runs - one place for both.

Two things live here, and they are the same subject (0.34.0, 1.211):

* **The words the backup catalog stores.** ``file_type``, the verdicts, the check results and the
  virus-scan status are each one list, read by the store's ``CHECK`` constraints
  (``dbabrain.db.backup_catalog``) and by every writer. A second spelling of ``LOG`` in one writer
  would be a row nobody can group with the rest.
* **The ``integrity`` object on a restore entry** - parsed once here, like ``time_window`` and
  ``cleanup_retention``, never by each app. ``copy_max_retry`` is in it, and **it is stated on every
  entry** (the operator, 2026-10-29: *mandatory, default 3, declared in full in the shared object
  json*): ``upgrade-config`` writes it into each entry that has none, ``restore-add`` writes it into
  each new one, and the reference marks the block required, so ``check-objects`` names an entry
  without it. An entry that states nothing is never quietly given 3 at the moment of a copy (rules
  R50): :func:`require_restore_integrity` refuses it where the gate reads it.

Imports nothing from ``dbabrain`` but ``lib`` (rules R04's mirror for ``lib``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from dbabrain.lib import errors

SHA256 = "sha256"
ALGORITHMS = (SHA256,)


def sha256_of_file(path) -> str:
    """The sha256 of a file this process can read, a megabyte at a time - one definition for the
    copy gate's local side and common's file transfers (it was written twice)."""
    import hashlib
    from pathlib import Path

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
CHECKSUM_LENGTH = {SHA256: 64}

ENGINES = ("sqlserver", "postgresql", "oracle", "mysql")
BACKUP_LEVELS = ("full", "diff", "log")

#: What the engine wrote, not just the level: a PostgreSQL FULL is several files of different kinds,
#: and a restore has to know which is which.
FILE_TYPES = (
    "FULL", "DIFF", "LOG",                                  # SQL Server: .bak, differential .bak, .trn
    "FULL_BASE", "INCR_BASE", "MANIFEST", "WAL",            # PostgreSQL
    "RMAN_L0", "RMAN_L1", "ARCHIVELOG", "CONTROLFILE", "SPFILE",  # Oracle
    "CERT",                                                 # a backup certificate a restore stages
)

BACKUP_RUN_STATUSES = ("RUNNING", "DONE", "ERROR")
ENGINE_VERIFY_RESULTS = ("passed", "failed", "not_run")
#: ``size_failed``: hashed, but its size in bytes could not be read - every file records its size
#: (the operator, 2026-10-29), and a file that does not is not ``ok``.
BACKUP_FILE_VERDICTS = ("ok", "verify_failed", "hash_failed", "size_failed")
BACKUP_FILE_STATES = ("present", "pruned", "missing", "mismatch")
BASELINES = ("backup", "backfill")

CHECK_RESULTS = ("match", "mismatch", "missing")
CHECKED_BY = ("scrub", "restore", "copy_only", "vault", "av_scan", "backfill", "offload")
CHECK_LOCATIONS = ("source", "vault", "repository")

#: What a restore ENTRY may state as its ``action`` (1.216).
RESTORE_ACTIONS = ("restore", "copy_only")
#: What a run row in ``restore_runs`` may be: an entry's action, or the repository copy a backup
#: entry makes of its own files (``offload``, 1.240) - a copy no entry asks for by action, so it is
#: never an entry's word, and never a restore or a drill.
ACTION_OFFLOAD = "offload"
RUN_ACTIONS = (*RESTORE_ACTIONS, ACTION_OFFLOAD)
#: Where a backup file's repository copy stands (``backup_files.offload_state``; NULL = the entry
#: does not offload). ``pending`` - written, not yet copied; ``verified`` - copied off the host and
#: proven there by checksum and bytes (and scanned clean when the entry scans); ``failed`` - the
#: copy could not be proven, the source is kept and the next pass tries again; ``quarantined`` - the
#: copy was proven but a scan flagged it: never a restore's, never a reason to delete the source;
#: ``expired`` - the repository's retention removed the copy (it WAS verified; offload_expired_at).
OFFLOAD_STATES = ("pending", "verified", "failed", "quarantined", "expired")
RESTORE_PHASES = ("planning", "copying", "verified", "restoring", "verifying_db",
                  "copied", "done", "refused", "error")
#: One file of a restore or a copy-only run. `copying` while it is in flight; the three that end
#: with the copy proven are `match` (already on the target, not copied), `copied`, `recopied`.
RUN_FILE_VERDICTS = ("copying", "match", "copied", "recopied", "source_mismatch", "source_missing",
                     "uncatalogued_refused", "copy_failed_after_retries")
PROVEN_VERDICTS = ("match", "copied", "recopied")
ATTEMPT_RESULTS = ("match", "mismatch", "copy_error")

#: NULL in the store means never scanned; these are the answers a scan can give.
AV_SCAN_STATUSES = ("clean", "infected", "error", "skipped")
AV_SCAN_COLUMNS = ("av_scan_status", "av_scanned_at", "av_scan_tool", "av_scan_tool_version",
                   "av_scan_signature_version", "av_scan_server_id", "av_scan_result_text")

# --------------------------------------------------------------------------- #
# The `integrity` object on a restore entry
# --------------------------------------------------------------------------- #
DEFAULT_COPY_MAX_RETRY = 3
#: Above this a broken link is retried for most of a night; ten copies of a 60 GB FULL that never
#: arrive intact is a fault to report, not to keep paying for.
MAX_COPY_MAX_RETRY = 10
DEFAULT_COPY_RETRY_INTERVAL_SECONDS = 30
UNCATALOGUED_CHOICES = ("compare_copy", "refuse")

#: How many of an entry's older files each backup run re-hashes (0.35.0, 1.218 - the plan of
#: 2026-10-29, B.4). Ransomware encrypts last week's backups, not tonight's: within days every
#: file on disk has been re-read. A backup entry's `scrub_files_per_run` changes it; 0 is off.
SCRUB_FILES_PER_RUN = 20


#: What ransomware leaves in a folder it encrypted (0.35.0, 1.219 - the plans of 2026-10-29 A3.4 and
#: 2026-10-30): the C3RB3R wave's `read-me3.txt` and `*.L0CK3D`, and the generic notes and
#: extensions. No engine writes any of these, so one in a backup folder is an attack, not a backup.
RANSOM_MARKERS = ("read-me*.txt", "*.L0CK3D", "*README*", "*DECRYPT*", "*RESTORE_FILES*",
                  "*HOW_TO_RECOVER*", "*.locked", "*.encrypted", "*.crypt", "*.crypted")

#: A new file this many times bigger or smaller than its own history's median is reported (1.219,
#: BACKUP_SIZE_ANOMALY): encrypted data does not compress, so a FULL that suddenly triples is a sign,
#: and one that shrinks to a third is a backup that lost most of what it should hold.
SIZE_ANOMALY_FACTOR = 3.0
#: Fewer earlier files than this is no history to judge by.
SIZE_HISTORY_MIN = 5


def size_anomaly(size: int | None, history: list[int]) -> str:
    """Why ``size`` is far from ``history`` (the same database and type, newest first); empty when not."""
    sizes = sorted(int(s) for s in history if s)
    if size is None or len(sizes) < SIZE_HISTORY_MIN:
        return ""
    median = sizes[len(sizes) // 2]
    if median and (size > SIZE_ANOMALY_FACTOR * median or size * SIZE_ANOMALY_FACTOR < median):
        return f"{size:,} bytes against a median of {median:,} over its last {len(sizes)} backups"
    return ""


def scrub_files_per_run(entry: dict[str, Any] | None, *, label: str = "backup") -> int:
    """The entry's `scrub_files_per_run`, 0 to 1000; absent is :data:`SCRUB_FILES_PER_RUN`."""
    value = (entry or {}).get("scrub_files_per_run", SCRUB_FILES_PER_RUN)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
        raise IntegrityConfigError(f"{label}: scrub_files_per_run must be a whole number 0 to 1000, "
                                   f"got {value!r} (0 switches the scrub off).")
    return value

INTEGRITY_FIELDS = ("algorithm", "copy_max_retry", "copy_retry_interval_seconds",
                    "verify_source_before_copy", "uncatalogued", "engine_verify_before_restore")


class IntegrityConfigError(errors.ConfigError):
    """A restore entry's ``integrity`` block is absent where it is needed, or says something invalid."""


@dataclass(frozen=True)
class RestoreIntegrity:
    """How a restore or a copy-only run proves each file before it is used."""

    algorithm: str = SHA256
    #: Copies after the first, when the copy's hash differs from the source's. 3 = up to 4 copies.
    copy_max_retry: int = DEFAULT_COPY_MAX_RETRY
    copy_retry_interval_seconds: int = DEFAULT_COPY_RETRY_INTERVAL_SECONDS
    #: Hash the source on its own server before the copy, and refuse a file that differs from what
    #: the backup recorded - a changed backup is an incident, not a transfer fault.
    verify_source_before_copy: bool = True
    #: A file the catalog has no row for: compare source and copy (`compare_copy`), or refuse it.
    uncatalogued: str = "compare_copy"
    #: The engine checks the staged copy too (VERIFYONLY, pg_verifybackup, RMAN VALIDATE).
    engine_verify_before_restore: bool = False

    def as_record(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in INTEGRITY_FIELDS}


def stated_restore_integrity() -> dict[str, Any]:
    """The block ``upgrade-config`` writes into an existing entry that states none: what the entry has
    always done, ``uncatalogued: compare_copy`` included - an upgrade states behaviour, never changes it."""
    return RestoreIntegrity().as_record()


def new_restore_integrity() -> dict[str, Any]:
    """The block ``restore-add`` writes into a NEW entry (0.35.0, 1.249 - gap G9 *never drill an
    unverified file*): ``uncatalogued: refuse``. Every backup a new entry restores was written by a
    dbabrain job that catalogued it, or by an older one ``catalog-backfill`` can catalogue; a file
    neither recorded is not one to drill. The operator, 2026-11-02: new entries only - the entries
    already running keep ``compare_copy`` until their backups are backfilled and each is switched."""
    return RestoreIntegrity(uncatalogued="refuse").as_record()


def _bool(raw: dict[str, Any], name: str, default: bool, label: str) -> bool:
    value = raw.get(name, default)
    if not isinstance(value, bool):
        # Strictly a boolean: "yes" read as true is a check nobody stated.
        raise IntegrityConfigError(f"{label}: integrity.{name} must be true or false, got {value!r}.")
    return value


def _int(raw: dict[str, Any], name: str, default: int, label: str, *, low: int, high: int) -> int:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise IntegrityConfigError(f"{label}: integrity.{name} must be a whole number, got {value!r}.")
    if not low <= value <= high:
        raise IntegrityConfigError(f"{label}: integrity.{name} must be {low} to {high}, got {value}.")
    return value


def parse_restore_integrity(entry: dict[str, Any] | None, *, label: str = "restore") -> RestoreIntegrity | None:
    """Read an entry's ``integrity`` block; ``None`` when the entry states none.

    ``None`` is not 3: the caller that copies refuses it (:func:`require_restore_integrity`). A
    block that is present is held field by field - an unknown key, a wrong type, a retry count
    outside 0..10 - because a misspelled ``copy_max_retries`` would otherwise leave the real field at
    whatever this module chose.
    """
    raw = (entry or {}).get("integrity")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise IntegrityConfigError(f"{label}: integrity must be an object, e.g. "
                                   f"{{\"copy_max_retry\": {DEFAULT_COPY_MAX_RETRY}}}.")
    unknown = sorted(name for name in raw if name not in INTEGRITY_FIELDS and not str(name).startswith("_"))
    if unknown:
        raise IntegrityConfigError(f"{label}: unknown integrity field(s) {', '.join(unknown)}; "
                                   f"it has {', '.join(INTEGRITY_FIELDS)}.")
    if "copy_max_retry" not in raw:
        raise IntegrityConfigError(
            f"{label}: integrity.copy_max_retry is required - how many times a copy whose checksum "
            f"differs from the source is copied again ({DEFAULT_COPY_MAX_RETRY} is the default "
            "upgrade-config and restore-add write).")
    algorithm = str(raw.get("algorithm") or SHA256).strip().lower()
    if algorithm not in ALGORITHMS:
        raise IntegrityConfigError(f"{label}: integrity.algorithm must be one of {', '.join(ALGORITHMS)}, "
                                   f"got {raw.get('algorithm')!r}.")
    uncatalogued = str(raw.get("uncatalogued") or "compare_copy").strip().lower()
    if uncatalogued not in UNCATALOGUED_CHOICES:
        raise IntegrityConfigError(f"{label}: integrity.uncatalogued must be one of "
                                   f"{', '.join(UNCATALOGUED_CHOICES)}, got {raw.get('uncatalogued')!r}.")
    return RestoreIntegrity(
        algorithm=algorithm,
        copy_max_retry=_int(raw, "copy_max_retry", DEFAULT_COPY_MAX_RETRY, label, low=0, high=MAX_COPY_MAX_RETRY),
        copy_retry_interval_seconds=_int(raw, "copy_retry_interval_seconds",
                                         DEFAULT_COPY_RETRY_INTERVAL_SECONDS, label, low=0, high=3600),
        verify_source_before_copy=_bool(raw, "verify_source_before_copy", True, label),
        uncatalogued=uncatalogued,
        engine_verify_before_restore=_bool(raw, "engine_verify_before_restore", False, label),
    )


def require_restore_integrity(entry: dict[str, Any] | None, *, label: str = "restore") -> RestoreIntegrity:
    """The entry's ``integrity``, or a refusal naming the entry - for the code that copies (R50)."""
    parsed = parse_restore_integrity(entry, label=label)
    if parsed is None:
        raise IntegrityConfigError(
            f"{label} states no integrity block, so nothing says how often a copy that arrives "
            f"different is copied again. Run upgrade-config (it writes "
            f"{{\"copy_max_retry\": {DEFAULT_COPY_MAX_RETRY}, ...}} into every entry) or state it.")
    return parsed


#: What an entry does, written into every entry (0.34.0, 1.216): ``restore`` - copy, gate, RESTORE,
#: verify, cleanup - or ``copy_only`` - copy from ``server_id`` to the target, gate, and stop.
ACTION_RESTORE, ACTION_COPY_ONLY = RESTORE_ACTIONS
#: The value ``upgrade-config`` writes into an entry that states none: what every entry did before.
STATED_ACTION = ACTION_RESTORE


def parse_restore_action(entry: dict[str, Any] | None, *, label: str = "restore") -> str | None:
    """An entry's ``action``; ``None`` when it states none - the scheduler refuses that
    (:func:`require_restore_action`), never reading it as a restore."""
    raw = (entry or {}).get("action")
    if raw is None:
        return None
    value = str(raw).strip().lower() if isinstance(raw, str) else raw
    if value not in RESTORE_ACTIONS:
        raise IntegrityConfigError(f"{label}: action must be one of {', '.join(RESTORE_ACTIONS)}, got {raw!r}.")
    return value


def require_restore_action(action: str | None, *, label: str = "restore") -> str:
    """The action an entry stated, or a refusal naming the entry (R50)."""
    if action is None:
        raise IntegrityConfigError(
            f"{label} states no action, so nothing says whether it restores or only copies. Run "
            f"upgrade-config (it writes \"action\": \"{STATED_ACTION}\" into every entry) or state it.")
    return action


def state_restore_integrity(document: dict[str, Any]) -> list[dict[str, str]]:
    """Write the stated block, and the stated ``action``, into every restore entry of a
    ``restore_config.json`` that has none.

    ``upgrade-config``'s ``stated-integrity`` step. An entry that states either keeps it, every
    field as it is - the step adds, it never overrides. Returns one change per field written.
    """
    changes: list[dict[str, str]] = []
    section = document.get("backup_restore") if isinstance(document, dict) else None
    restores = section.get("restores") if isinstance(section, dict) else None
    for index, entry in enumerate(restores if isinstance(restores, list) else []):
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("restore_id") or f"restores[{index}]")
        if "action" not in entry:
            entry["action"] = STATED_ACTION
            changes.append({"where": f"backup_restore.restores[{label}]", "field": "action",
                            "value": f"{STATED_ACTION}, stated - what the entry has always done"})
        if "integrity" in entry:
            continue
        entry["integrity"] = stated_restore_integrity()
        changes.append({"where": f"backup_restore.restores[{label}]", "field": "integrity",
                        "value": f"copy_max_retry {DEFAULT_COPY_MAX_RETRY} and the other defaults, stated"})
    return changes


# --------------------------------------------------------------------------- #
# The receipt a backup script prints for each file it wrote (0.34.0, 1.212)
# --------------------------------------------------------------------------- #
#: One line per file, tab-separated, the path LAST so a space in it needs no quoting:
#:   FILE<TAB>db=APPDB<TAB>type=FULL<TAB>size=123<TAB>sha256=<hex><TAB>verify=passed<TAB>
#:       host_path=/opt/backup/APPDB/x.bak<TAB>container=mssql<TAB>path=/var/opt/mssql/backup/APPDB/x.bak
#: ``verify`` is passed | failed | not_run, and ``note=`` (optional, before path) says why.
#: ``encrypted`` is yes | no - what the script wrote the file with; absent (an older script) is
#: unknown, never "no".
#: ``path`` is where the engine wrote it (inside ``container`` when there is one); ``host_path`` the
#: same file on the server itself, through the container's mount - empty for a file in no mount.
#: ``start`` (0.35.0, 1.254) is when that file's backup began, UTC ``YYYY-MM-DDTHH:MM:SSZ``: the run's
#: start is the first database's, and a run of twenty databases put hours between the two. Absent
#: (an older script, a WAL segment the server archived itself) is unknown.
FILE_LINE_PREFIX = "FILE\t"
_UTC_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

#: What level a file's type belongs to - a run's level is its files'.
LEVEL_OF_FILE_TYPE = {
    "FULL": "full", "FULL_BASE": "full", "RMAN_L0": "full",
    "DIFF": "diff", "INCR_BASE": "diff", "RMAN_L1": "diff",
    "LOG": "log", "WAL": "log", "ARCHIVELOG": "log",
}


def parse_file_lines(text: str) -> list[dict[str, Any]]:
    """Every ``FILE`` line of a backup script's output, as the catalog's own words.

    A line whose hash is missing or not 64 hex characters keeps ``sha256: None`` - the script could
    not hash what it wrote, and the catalog records that as ``hash_failed`` rather than dropping
    the file: a backup nobody can verify is a finding, not a gap.
    """
    files: list[dict[str, Any]] = []
    for raw in str(text or "").splitlines():
        line = raw.rstrip("\r")
        if not line.startswith(FILE_LINE_PREFIX):
            continue
        head, sep, path = line.partition("\tpath=")
        if not sep or not path:
            continue
        fields = dict(part.partition("=")[::2] for part in head.split("\t")[1:])
        sha = str(fields.get("sha256") or "").strip().lower()
        valid = len(sha) == CHECKSUM_LENGTH[SHA256] and all(c in "0123456789abcdef" for c in sha)
        size = str(fields.get("size") or "").strip()
        verify = str(fields.get("verify") or "not_run").strip().lower()
        encrypted = str(fields.get("encrypted") or "").strip().lower()
        started = str(fields.get("start") or "").strip()
        files.append({
            "path": path,
            "file_name": path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1],
            "file_folder": path.replace("\\", "/").rstrip("/").rsplit("/", 1)[0] if "/" in path.replace("\\", "/") else "",
            "database_name": str(fields.get("db") or ""),
            # No container: the path the engine wrote IS the server's own.
            "host_path": str(fields.get("host_path") if "host_path" in fields else path),
            "container_name": str(fields.get("container") or ""),
            "file_type": str(fields.get("type") or "").strip().upper(),
            "size_bytes": int(size) if size.isdigit() else None,
            "sha256": sha if valid else None,
            "engine_verify": verify if verify in ENGINE_VERIFY_RESULTS else "not_run",
            "engine_verify_text": str(fields.get("note") or ""),
            "encrypted": {"yes": 1, "no": 0}.get(encrypted),
            # Anything but a UTC stamp is unknown, never a guess: the catalog falls back to the run's.
            "backup_started_at": started if _UTC_STAMP.match(started) else None,
        })
    return files


#: The file a type's stored checksum was taken of, when it is not the catalogued path itself: a
#: PostgreSQL base backup is a directory, and its checksum is its manifest's.
HASHED_MEMBER = {"FULL_BASE": "backup_manifest", "INCR_BASE": "backup_manifest"}


def hashed_path(file_type: str, path: str) -> str:
    """The path whose sha256 is the catalogued checksum of a file of ``file_type`` at ``path``."""
    member = HASHED_MEMBER.get(str(file_type or "").upper())
    if not member or not path:
        return path
    sep = "\\" if "\\" in path and "/" not in path else "/"
    return path.rstrip("/\\") + sep + member


def sqlserver_file_type(relative_path: str) -> str:
    """The ``file_type`` of a SQL Server backup file from its place in the layout every backup script
    writes - ``<DB>/FULL|DIFF|LOG/<file>`` and ``_cert/<name>.cer|.pvk``. A ``.trn`` is a log whatever
    folder it sits in."""
    parts = [part.upper() for part in str(relative_path).replace("\\", "/").split("/") if part]
    name = parts[-1] if parts else ""
    if name.endswith((".CER", ".PVK")) or "_CERT" in parts:
        return "CERT"
    if name.endswith(".TRN") or "LOG" in parts[:-1]:
        return "LOG"
    if "DIFF" in parts[:-1]:
        return "DIFF"
    return "FULL"


def script_file_type(db_type: str, relative: str) -> str:
    """A backup file's ``file_type`` from its place in the layout the engine scripts write: ``base/`` and
    ``wal/`` for PostgreSQL, RMAN's piece names for Oracle, SQL Server's ``<DB>/FULL|DIFF|LOG``.

    A best reading where the receipt is not there - a staged copy, a backup taken before the catalog
    (``catalog-backfill``). The catalog's own type wins whenever the backup recorded the file. One
    Oracle case it cannot see: the level 0/1 run's controlfile piece is written under the level's
    FORMAT, so its name reads as that level.
    """
    path = str(relative or "").replace("\\", "/")
    name = path.rstrip("/").rsplit("/", 1)[-1].upper()
    engine = str(db_type or "").lower()
    if engine == "postgresql":
        if path.startswith("wal/") or "/wal/" in path:
            return "WAL"
        if name == "BACKUP_MANIFEST":
            return "MANIFEST"
        return "INCR_BASE" if "_INCR" in path.upper() else "FULL_BASE"
    if engine == "oracle":
        if name.startswith("C-") or name.startswith("AUTOBACKUP_"):
            return "CONTROLFILE"      # RMAN's autobackup: c-<dbid>-<yyyymmdd>-<nn>, autobackup_%F
        for marker, kind in (("CONTROLFILE", "CONTROLFILE"), ("CTL_", "CONTROLFILE"), ("SPFILE", "SPFILE"),
                             ("ARCH", "ARCHIVELOG"), ("L1", "RMAN_L1"), ("INCR", "RMAN_L1")):
            if marker in name:
                return kind
        return "RMAN_L0"
    return sqlserver_file_type(path)


#: The inventory's `environment` values that mean a production instance (1.223).
PRODUCTION_ENVIRONMENTS = ("prod", "production")


def production_target_refusal(environment: str | None, *, allowed: bool, restore_id: str, target: str) -> str:
    """Why a restore into ``target`` is refused, or ``""`` (0.35.0, 1.223 - the plan of 2026-10-29, A3.3).

    A restore REPLACEs what it lands on, and an entry mis-edited to name a production instance as its
    target would overwrite it at the next scheduled drill. R47's spirit: the target is named *and*
    allowed - a production target needs the entry's own ``allow_production_target: true``. An
    instance the inventory does not mark (no ``environment``) is not judged here.
    """
    if allowed or str(environment or "").strip().lower() not in PRODUCTION_ENVIRONMENTS:
        return ""
    return (f"{restore_id}: the target {target} is a production instance (environment "
            f"{environment!r} in db_instances.json). Refused: a restore overwrites what it lands on. "
            "If this target is meant, the entry says \"allow_production_target\": true.")


def sqlserver_chain_break(full: dict[str, Any], diff: dict[str, Any] | None,
                          logs: list[dict[str, Any]]) -> str:
    """The first break in a SQL Server restore chain, in words naming the piece; ``""`` when it holds
    (0.35.0, 1.250 - gap G10, *chain completeness*).

    Read from ``RESTORE HEADERONLY``'s LSNs, which the listing already takes:

    * a DIFF belongs to the FULL whose ``CheckpointLSN`` is its ``DifferentialBaseLSN`` - a DIFF taken
      after another FULL restores onto nothing;
    * the first log that ends after the FULL / DIFF must start at or before where it ends;
    * each log starts exactly where the one before it ended - a missing ``.trn`` is a gap.

    A log that ends before the FULL / DIFF is not a break (SQL Server would only call it too early).
    An LSN the header did not give (0) is not judged: a check that guesses is worse than none.
    """
    def lsn(item: dict[str, Any] | None, key: str) -> int:
        try:
            return int((item or {}).get(key) or 0)
        except (TypeError, ValueError):
            return 0

    def name(item: dict[str, Any]) -> str:
        return str(item.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1]

    if diff:
        base, checkpoint = lsn(diff, "differential_base_lsn"), lsn(full, "checkpoint_lsn")
        if base and checkpoint and base != checkpoint:
            return (f"the DIFF {name(diff)} is based on another FULL (its base LSN {base}, the FULL "
                    f"{name(full)} has checkpoint LSN {checkpoint})")
    anchor = diff or full
    previous_end = lsn(anchor, "last_lsn")
    started = False
    for item in logs:
        first, last = lsn(item, "first_lsn"), lsn(item, "last_lsn")
        if not first or not last or not previous_end:
            return ""
        if not started:
            if last <= previous_end:
                continue
            if first > previous_end:
                return (f"the logs start at LSN {first} ({name(item)}) after the "
                        f"{'DIFF' if diff else 'FULL'} {name(anchor)} ends at LSN {previous_end} - a log "
                        "backup between them is missing")
            started = True
        elif first != previous_end:
            return (f"a log backup is missing before {name(item)}: it starts at LSN {first}, the one "
                    f"before it ended at LSN {previous_end}")
        previous_end = last
    return ""


def postgresql_wal_break(start_wal: str, segments: list[str], *, to_latest: bool = True) -> str:
    """The first hole in the WAL a PostgreSQL restore replays from its base backup's start segment;
    ``""`` when it holds or cannot be judged (0.35.0, 1.250 - gap G10).

    A recovery to *latest* replays segment after segment and stops at the first one missing - and then
    opens, as if that were the end: the drill reports done and has restored an older state. So:

    * the start segment (``backup_label``'s START WAL) must be archived;
    * with ``to_latest``, every segment from it to the newest archived one, without a hole. A
      point-in-time restore is only held to the start: a hole after its target does not matter, and
      which segment holds the target cannot be read from a name.

    Not judged (``""``): no start or no segments given, another timeline after the start (a
    promotion - its history decides), or a segment number past 0xFF (a segment size other than
    16 MB, where the numbering differs).
    """
    start = str(start_wal or "").strip().upper()
    names = sorted({str(name).strip().upper() for name in segments or [] if WAL_SEGMENT_NAME.match(str(name).strip().upper())})
    if not WAL_SEGMENT_NAME.match(start) or not names:
        return ""
    timeline = start[:8]
    after = [name for name in names if name >= start]
    if any(name[:8] != timeline for name in after):
        return ""
    if start not in after:
        return f"the WAL segment the base backup starts from, {start}, is not archived"
    if not to_latest:
        return ""

    def number(name: str) -> int | None:
        log, seg = int(name[8:16], 16), int(name[16:24], 16)
        return None if seg > 0xFF else log * 0x100 + seg

    previous = number(start)
    for name in after[1:]:
        current = number(name)
        if previous is None or current is None:
            return ""
        if current != previous + 1:
            missing = previous + 1
            return (f"WAL segment {timeline}{missing // 0x100:08X}{missing % 0x100:08X} is missing - "
                    f"recovery would stop before {name} and open on an older state")
        previous = current
    return ""


#: A PostgreSQL WAL segment's name: 24 hex digits (timeline, log, segment). ``.partial`` and
#: ``.history`` files are not segments a restore replays.
WAL_SEGMENT_NAME = re.compile(r"^[0-9A-F]{24}$")


def file_verdict(item: dict[str, Any]) -> str:
    """``ok`` | ``hash_failed`` | ``size_failed`` | ``verify_failed`` for one parsed ``FILE`` line."""
    if not item.get("sha256"):
        return "hash_failed"
    if item.get("size_bytes") is None:
        return "size_failed"
    if item.get("engine_verify") == "failed":
        return "verify_failed"
    return "ok"


def check_list(column: str, values: tuple[str, ...]) -> str:
    """``column IN ('a', 'b')`` for a ``CHECK`` - the store's constraints are built from the lists above."""
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"{column} IN ({quoted})"
