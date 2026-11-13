"""The copy gate: one staged file, proven by checksum before it is used (0.34.0, 1.211).

The operator, 2026-10-29: *a restore stores the checksum before the copy and after it; only when
they match does the restore run; when they differ, copy again over it, with a copy max retry in the
restore config* - and the same for a copy-only run between two ``server_id``s. This module is that
rule for one file, and nothing else:

1. **The source, on its own server** (``hash_source``). Not there → ``source_missing``. Different from
   what its backup recorded → ``source_mismatch``: the backup itself changed after it was written,
   which copying again cannot fix, so nothing is copied. A file no backup recorded is compared copy
   against source (``compare_copy``) or refused (``refuse``), as the entry's ``integrity`` says.
2. **The target, before the copy** (``hash_target``). Already the same bytes → ``match``, no copy.
3. **Copy, then hash the target** - up to ``copy_max_retry + 1`` attempts. Equal → ``copied`` on the
   first attempt, ``recopied`` on a later one. Different, or a copy that failed on the way (a
   network error, a refused login), is an attempt spent: wait ``copy_retry_interval_seconds`` and copy
   again **over** what is there. Out of attempts → ``copy_failed_after_retries``.

**"Equal" is the checksum AND the size in bytes** (1.245; the operator's scenario, 2026-10-30:
*re-hash at the destination, compare checksum and bytes*). Two files of different sizes cannot share
a SHA-256 in practice, so the size adds no strength to the hash - it is there because it is the
second, independent reading of the file the scenario asks for, and because a size read from another
call (``stat`` beside ``sha256sum``) catches a hasher that answered for the wrong file. A size that
was not read (``None``) is not a difference; a size that differs is, and the attempt says so.

Every step is written to the catalog **as it happens** (``restore_run_files``, ``copy_attempts``):
a run that dies on attempt 2 leaves attempt 1 and the source's hash behind, which is the evidence.

The transport is injected - three callables and a clock - so the decisions are tested on their own,
with no network, and the restore paths (robocopy, smbclient + SFTP, ``copy-backup-dir``) plug their
own copy in.

**What raises instead of deciding:** a source that cannot be *asked* (its server unreachable). That
is not a verdict about this file - it is a run that cannot proceed, and the caller ends the run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dbabrain.lib import integrity as I
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.db.store import utc_now_text

#: ``hash_*(path) -> (sha256, size_bytes) | None`` - None when the file is not there.
Hasher = Callable[[str], "tuple[str, int | None] | None"]
#: ``hash_*(paths) -> {path: (sha256, size_bytes)}`` - a path not in the answer is not there. One
#: call per batch: on a real host each call is a ``common.cli file-checksum`` round trip, and 100.250's
#: eight-day chain is about 1,500 files.
BatchHasher = Callable[[list[str]], "dict[str, tuple[str, int | None]]"]
#: ``copy(file) -> bytes_copied`` - raises on any failure (network, login, disk).
Copier = Callable[["FileToCopy"], "int | None"]


@dataclass(frozen=True)
class FileToCopy:
    """One file of a run, as the planner found it - complete, so its rows read on their own."""

    source_server_id: str
    target_server_id: str
    file_name: str
    file_type: str
    source_path: str
    target_path: str
    database_name: str = ""
    engine: str = ""
    #: What the backup recorded (``backup_files.checksum_after_backup``); None when uncatalogued.
    checksum_after_backup: str | None = None
    #: The size it recorded with it (``backup_files.size_bytes``) - what a copy proven against the
    #: catalog alone (``confirm_on_target``) is held to beside the checksum (1.245).
    size_after_backup: int | None = None
    #: Each side's path on that server itself, through a container's mount (the catalog keeps both).
    source_host_path: str = ""
    target_host_path: str = ""
    file_id: int | None = None
    chain_position: int | None = None
    backup_taken_at: str | None = None


@dataclass
class Attempt:
    attempt_no: int
    result: str                       # match | mismatch | copy_error
    checksum_target_after_copy: str | None = None
    size_target_after_copy: int | None = None
    bytes_copied: int | None = None
    error_text: str = ""


@dataclass
class FileOutcome:
    verdict: str
    checksum_source_before_copy: str | None = None
    checksum_target_before_copy: str | None = None
    checksum_target_after_copy: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    run_file_id: int | None = None
    reason: str = ""

    @property
    def proven(self) -> bool:
        """Only these three may be used by a restore - the store holds the same rule as a CHECK."""
        return self.verdict in I.PROVEN_VERDICTS


def same_bytes(found: "tuple[str, int | None] | None", sha256: str | None, size: int | None) -> bool:
    """A hasher's answer equal to an expected checksum AND size (1.245). A size either side did not
    read is no difference - the checksum still decides; a size both read must be the same."""
    if found is None or not sha256 or not found[0] or found[0].lower() != sha256.lower():
        return False
    return found[1] is None or size is None or int(found[1]) == int(size)


def _size_note(found: "tuple[str, int | None] | None", size: int | None) -> str:
    if found is None or found[1] is None or size is None or int(found[1]) == int(size):
        return ""
    return f"{found[1]} bytes on the target, {size} at the source"


class _NoRecord:
    """Stands in for the catalog when a caller records nothing (a dry plan)."""

    def add_run_file(self, **_values: Any) -> None:
        return None

    def set_run_file(self, *_args: Any, **_values: Any) -> None:
        return None

    def record_copy_attempt(self, **_values: Any) -> None:
        return None


def copy_verified(
    item: FileToCopy,
    *,
    integrity: I.RestoreIntegrity,
    hash_source: Hasher,
    hash_target: Hasher,
    copy: Copier,
    catalog: BackupCatalogStore | None = None,
    restore_run_id: int | None = None,
    copy_engine: str = "",
    sleep: Callable[[float], None] = time.sleep,
) -> FileOutcome:
    """Prove one file onto the target - :func:`copy_verified_batch` with a batch of one."""

    def one(hasher: Hasher) -> BatchHasher:
        def batch(paths: list[str]) -> dict[str, tuple[str, int | None]]:
            found = {}
            for path in paths:
                answer = hasher(path)
                if answer is not None:
                    found[path] = answer
            return found
        return batch

    return copy_verified_batch(
        [item], integrity=integrity, hash_sources=one(hash_source), hash_targets=one(hash_target),
        copy=copy, catalog=catalog, restore_run_id=restore_run_id, copy_engine=copy_engine,
        sleep=sleep)[0]


def copy_verified_batch(
    items: list[FileToCopy],
    *,
    integrity: I.RestoreIntegrity,
    hash_sources: BatchHasher,
    hash_targets: BatchHasher,
    copy: Copier,
    catalog: BackupCatalogStore | None = None,
    restore_run_id: int | None = None,
    copy_engine: str = "",
    sleep: Callable[[float], None] = time.sleep,
    copy_said: Callable[[FileToCopy], str | None] | None = None,
) -> list[FileOutcome]:
    """Prove every file of a run onto the target; one outcome per item, in order.

    The rules are the module docstring's, file by file; only the timing is shared: the sources are
    hashed in one call, the targets before the copy in one, and each round of copies is followed by
    one hash of everything it copied. A round's failures are copied again together, after one pause -
    so ``copy_max_retry`` is the number of rounds after the first, for every file alike.

    ``copy_said`` is for a copier whose files travel together when the round is hashed (the restore's
    staging copy): what the round said about one file that did not arrive - "will not fit", the
    transfer's own error. Without it the attempt records only that the file is not there, and the
    reason a person needs is on nobody's screen (0.38.0, 1.329).
    """
    if integrity is None:
        # R50: the entry states it, or the gate does not run - never a silent 3.
        raise I.IntegrityConfigError("no integrity stated for this run.")
    record: Any = catalog if (catalog is not None and restore_run_id is not None) else _NoRecord()
    outcomes: list[FileOutcome] = [FileOutcome(verdict="copying") for _ in items]

    def base(item: FileToCopy) -> dict[str, Any]:
        return {
            "restore_run_id": restore_run_id, "file_id": item.file_id,
            "source_server_id": item.source_server_id, "target_server_id": item.target_server_id,
            "database_name": item.database_name, "engine": item.engine, "file_name": item.file_name,
            "file_type": item.file_type, "source_path": item.source_path, "target_path": item.target_path,
            "source_host_path": item.source_host_path, "target_host_path": item.target_host_path,
            "backup_taken_at": item.backup_taken_at, "chain_position": item.chain_position,
            "checksum_algo": integrity.algorithm, "checksum_after_backup": item.checksum_after_backup,
            "copy_max_retry": integrity.copy_max_retry,
        }

    # 1. The sources, where they are. Unreachable raises: the run cannot go on, for any file.
    sources = hash_sources([item.source_path for item in items])
    stored: dict[int, str] = {}
    sha_of: dict[int, str] = {}
    size_of: dict[int, int | None] = {}
    side: dict[int, dict[str, Any]] = {}
    candidates: list[int] = []
    for index, item in enumerate(items):
        outcome = outcomes[index]
        found = sources.get(item.source_path)
        if found is None:
            outcome.verdict = "source_missing"
            outcome.reason = f"{item.source_path} is not on {item.source_server_id}"
            outcome.run_file_id = record.add_run_file(**base(item), verdict=outcome.verdict, source_on_disk=0,
                                                      source_checked_at=utc_now_text())
            continue
        source_sha, source_size = found
        outcome.checksum_source_before_copy = source_sha
        # The source held to what dbabrain stored for it - the backup's checksum, or for a file no
        # backup recorded, this very hash (it is what the copy will be held to).
        stored[index] = item.checksum_after_backup or source_sha
        sha_of[index] = source_sha
        size_of[index] = source_size
        side[index] = {"source_on_disk": 1, "source_checksum_matches": int(source_sha == stored[index]),
                       "source_checked_at": utc_now_text(), "size_source_bytes": source_size,
                       "checksum_source_before_copy": source_sha}
        if item.checksum_after_backup is None and integrity.uncatalogued == "refuse":
            outcome.verdict = "uncatalogued_refused"
            outcome.reason = "no backup recorded this file, and the entry refuses one"
            outcome.run_file_id = record.add_run_file(**base(item), verdict=outcome.verdict, **side[index])
            continue
        if (integrity.verify_source_before_copy and item.checksum_after_backup is not None
                and source_sha != item.checksum_after_backup):
            outcome.verdict = "source_mismatch"
            outcome.reason = (f"{item.source_path} on {item.source_server_id} hashes {source_sha[:12]}... but "
                              f"its backup recorded {item.checksum_after_backup[:12]}... - the backup changed "
                              "after it was written")
            outcome.run_file_id = record.add_run_file(**base(item), verdict=outcome.verdict, **side[index])
            continue
        candidates.append(index)

    # 2. Already on the target, the same bytes: nothing to copy.
    before = hash_targets([items[index].target_path for index in candidates]) if candidates else {}
    pending: list[int] = []
    for index in candidates:
        item, outcome = items[index], outcomes[index]
        found = before.get(item.target_path)
        before_sha = found[0] if found else None
        outcome.checksum_target_before_copy = before_sha
        outcome.run_file_id = record.add_run_file(**base(item), checksum_target_before_copy=before_sha,
                                                  verdict="copying", **side[index])
        if same_bytes(found, sha_of[index], size_of[index]):
            outcome.verdict = "match"
            outcome.checksum_target_after_copy = before_sha
            record.set_run_file(outcome.run_file_id, checksum_target_after_copy=before_sha,
                                size_target_bytes=found[1], verdict="match", target_on_disk=1,
                                target_checksum_matches=int(before_sha == stored[index]),
                                target_checked_at=utc_now_text())
        else:
            pending.append(index)

    # 3. Rounds: copy everything pending, hash it in one call, keep what is still wrong.
    copy_started = {index: utc_now_text() for index in pending}
    total = integrity.copy_max_retry + 1
    for attempt_no in range(1, total + 1):
        if not pending:
            break
        attempts: dict[int, Attempt] = {}
        started: dict[int, str] = {}
        copied: list[int] = []
        for index in pending:
            started[index] = utc_now_text()
            attempt = attempts[index] = Attempt(attempt_no=attempt_no, result="copy_error")
            try:
                attempt.bytes_copied = copy(items[index])
                copied.append(index)
            except Exception as exc:  # noqa: BLE001 - a failed copy is an attempt spent, recorded with its text
                attempt.error_text = f"{type(exc).__name__}: {exc}"[:2000]
        after = hash_targets([items[index].target_path for index in copied]) if copied else {}
        still: list[int] = []
        for index in pending:
            item, outcome, attempt = items[index], outcomes[index], attempts[index]
            if index in copied:
                found = after.get(item.target_path)
                said = str(copy_said(item) or "").strip() if copy_said is not None else ""
                if found is None:
                    attempt.error_text = (f"{item.target_path} is not on {item.target_server_id} after the copy"
                                          + (f" - {said}" if said else ""))[:2000]
                else:
                    attempt.checksum_target_after_copy, attempt.size_target_after_copy = found
                    attempt.result = "match" if same_bytes(found, sha_of[index], size_of[index]) else "mismatch"
                    if attempt.result == "mismatch" and found[0] == sha_of[index]:
                        # The hash agrees and the size does not: the two readings disagree about one
                        # file, which is never proof - named, so the row says which one failed.
                        attempt.error_text = _size_note(found, size_of[index])
                    if attempt.result == "mismatch" and said:
                        # What is there is not the source's bytes, and the round said why it is not.
                        attempt.error_text = (f"{attempt.error_text} - {said}" if attempt.error_text else said)[:2000]
            outcome.attempts.append(attempt)
            record.record_copy_attempt(
                run_file_id=outcome.run_file_id, attempt_no=attempt_no, source_server_id=item.source_server_id,
                target_server_id=item.target_server_id, file_name=item.file_name, file_type=item.file_type,
                source_path=item.source_path, target_path=item.target_path,
                source_host_path=item.source_host_path, target_host_path=item.target_host_path,
                started_at=started[index], ended_at=utc_now_text(), bytes_copied=attempt.bytes_copied,
                copy_engine=copy_engine, checksum_source_before_copy=sha_of[index],
                checksum_target_after_copy=attempt.checksum_target_after_copy,
                size_target_after_copy=attempt.size_target_after_copy, result=attempt.result,
                error_text=attempt.error_text)
            if attempt.checksum_target_after_copy is not None:
                outcome.checksum_target_after_copy = attempt.checksum_target_after_copy
            if attempt.result == "match":
                outcome.verdict = "copied" if attempt_no == 1 else "recopied"
            else:
                still.append(index)
        pending = still
        if pending and attempt_no < total and integrity.copy_retry_interval_seconds:
            sleep(integrity.copy_retry_interval_seconds)

    for index in pending:
        outcome = outcomes[index]
        outcome.verdict = "copy_failed_after_retries"
        last = outcome.attempts[-1]
        outcome.reason = (f"{items[index].file_name}: {total} attempt(s), none arrived equal to the source"
                          + (f" - last: {last.error_text}" if last.error_text else ""))

    # The target as the last attempt left it, held to the stored checksum - once per copied file.
    for index in copy_started:
        outcome = outcomes[index]
        last_after = outcome.attempts[-1].checksum_target_after_copy
        record.set_run_file(
            outcome.run_file_id, copy_attempts=len(outcome.attempts), copy_started_at=copy_started[index],
            copy_ended_at=utc_now_text(), checksum_target_after_copy=outcome.checksum_target_after_copy,
            size_target_bytes=outcome.attempts[-1].size_target_after_copy, verdict=outcome.verdict,
            target_on_disk=int(last_after is not None),
            target_checksum_matches=None if last_after is None else int(last_after == stored[index]),
            target_checked_at=utc_now_text())
    return outcomes


def summarize(outcomes: list[FileOutcome]) -> dict[str, Any]:
    """The run-level answer: counts per verdict, and whether every file is proven (the restore's gate)."""
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.verdict] = counts.get(outcome.verdict, 0) + 1
    unproven = [outcome.reason or outcome.verdict for outcome in outcomes if not outcome.proven]
    return {"files": len(outcomes), "verdicts": counts, "proven": not unproven,
            "recopied": counts.get("recopied", 0), "unproven": unproven[:20]}


def confirm_on_target(
    items: list[FileToCopy],
    *,
    integrity: I.RestoreIntegrity,
    hash_targets: BatchHasher,
    catalog: BackupCatalogStore | None = None,
    restore_run_id: int | None = None,
) -> tuple[list[FileOutcome], list[int]]:
    """Files already on the target, held to the checksum their backup RECORDED - no source read.

    For a catalogued file the copy on the target is proven by being the very bytes the backup wrote:
    the stronger proof, and it spares re-reading a source that may be an hour of SMB away. Answers the
    outcome of each item (``match`` when proven, ``copying`` otherwise) and the indexes still to copy -
    a file not on the target, not equal, or with no catalog checksum goes through
    :func:`copy_verified_batch` like any other.
    """
    if integrity is None:
        raise I.IntegrityConfigError("no integrity stated for this run.")
    record: Any = catalog if (catalog is not None and restore_run_id is not None) else _NoRecord()
    outcomes = [FileOutcome(verdict="copying") for _ in items]
    checkable = [index for index, item in enumerate(items) if item.checksum_after_backup]
    found = hash_targets([items[index].target_path for index in checkable]) if checkable else {}
    remaining = [index for index, item in enumerate(items) if not item.checksum_after_backup]
    for index in checkable:
        item = items[index]
        target = found.get(item.target_path)
        # A PostgreSQL base backup is hashed through its manifest, and its recorded size is the whole
        # directory's: held to the manifest's size, a backup already on the target never matched and
        # was copied again (found 2026-11-02, 1.215). The manifest's checksum covers every file.
        recorded = None if str(item.file_type or "").upper() in I.HASHED_MEMBER else item.size_after_backup
        if not same_bytes(target, item.checksum_after_backup, recorded):
            remaining.append(index)
            continue
        outcome = outcomes[index]
        outcome.verdict = "match"
        outcome.checksum_target_before_copy = outcome.checksum_target_after_copy = target[0]
        outcome.run_file_id = record.add_run_file(
            restore_run_id=restore_run_id, file_id=item.file_id, source_server_id=item.source_server_id,
            target_server_id=item.target_server_id, database_name=item.database_name, engine=item.engine,
            file_name=item.file_name, file_type=item.file_type, source_path=item.source_path,
            target_path=item.target_path, source_host_path=item.source_host_path,
            target_host_path=item.target_host_path, backup_taken_at=item.backup_taken_at,
            chain_position=item.chain_position, checksum_algo=integrity.algorithm,
            checksum_after_backup=item.checksum_after_backup, checksum_target_before_copy=target[0],
            checksum_target_after_copy=target[0], size_target_bytes=target[1],
            copy_max_retry=integrity.copy_max_retry, verdict="match", target_on_disk=1,
            target_checksum_matches=1, target_checked_at=utc_now_text())
    return outcomes, sorted(remaining)


def close_restore_runs(catalog: Any, runs: list[tuple[int, bool]], *, phase: str, error_text: str) -> None:
    """End each run row with the restore's outcome (``done`` / ``error``); a copy the gate refused
    stays ``refused``. Shared by the share-driven workflow and ``restore_by_id``."""
    if catalog is None:
        return
    for restore_run_id, proven in runs:
        try:
            if proven:
                catalog.set_restore_run(restore_run_id, phase=phase, ended_at=utc_now_text(), error_text=error_text)
            else:
                catalog.set_restore_run(restore_run_id, ended_at=utc_now_text(), error_text=error_text)
        except Exception:  # noqa: BLE001 - the record is not the restore
            continue


def record_run(catalog: Any, restore_run_id: int | None, **values: Any) -> None:
    """Move one run row on - its phase, its point in time, its verify counts (1.214) - and never fail
    the restore for it: the record is not the restore, as :func:`close_restore_runs` holds."""
    if catalog is None or restore_run_id is None:
        return
    try:
        catalog.set_restore_run(int(restore_run_id), **values)
    except Exception:  # noqa: BLE001 - the record is not the restore
        return
