"""The repository copy: every backup file copied off its server, proven there, then released (0.34.0).

Items 1.240 (the copy), 1.246 (delete at the source only after a verified copy) and 1.247 (the
``verified`` state) of ``audits/20261030_gap_backup_restore_verification_scenario.md``. The operator,
2026-10-30: *each backup run's files go to a repository - a shared folder or another VM - proven
there by checksum and bytes as a restore's copy is, scanned if asked, and only then deleted on the
database server*. The entry's ``offload_*`` fields (``dbabrain.lib.offload``) say whether and where.

**A pass of its own, not a step inside the backup.** The scheduled workflow runs it after the
backups and restores. A 500 GB FULL takes hours to cross the network; inside the backup run it would
hold the backup job's claim - and the next LOG backup - for all of them, and a repository that is
down would turn every backup red. Here the backup is done and green when its files are written and
verified on their server, and the copy is its own run with its own row (``offload_job`` in
``job_runs``, an ``offload`` run in the catalog), one at a time per entry, resumed by the next pass
wherever the last one stopped.

**One file, one rule - the copy gate's** (``copy_gate.copy_verified_batch``): the source hashed on
its own server and held to the checksum its backup recorded (a changed backup is never copied), the
repository's copy hashed on the repository and held to the source's checksum AND size, copied again
up to ``offload_copy_max_retry`` times. Only then is the catalog row ``verified`` - with where the
copy is, the run that proved it, and when.

**The source is released only on proof, never on a copy's exit code** (the scenario's first
principle). With ``offload_delete_source``:

* the repository copy is hashed again right before the delete - a copy that changed or vanished
  since it was proven is ``failed``, and its source is kept;
* the newest chain of each database stays on the server - its newest full and everything after it -
  so the server can still restore its latest point by itself and an incremental still has its base
  (plan D8's proposed answer);
* a base backup (a directory) is deleted file by file - each by the path, bytes and sha256 its offload
  measured - and its folder only once it is empty (2026-11-11, the operator: *one file per delete, a
  folder only when empty*); the script's own sweep stays ``keep`` on an entry that releases;
* the delete is ``common.cli delete-verified-file``, ONE file per command (0.37.0): the request
  states the file's name, path, size and sha256 as its backup recorded them, fenced to the entry's
  backup folder, and the host deletes it only when all of them are still true; the row then says
  ``pruned`` with ``source_deleted_at`` - which the store accepts only for a ``verified`` copy;
* the copy itself (``copy-files``) carries each file's name, size and sha256 as measured on the
  source, and lands a file only when the bytes streamed match them.

**The repository keeps each copy by its file type** (``offload_retention``, seconds per file type -
the operator, 2026-10-31: *each file type's retention in the repository comes from config*; 2026-11-11:
*one array, the file type and how long*). See :func:`expiry_plan`: a copy expires by its own type's
age, never inside the newest chain of its database on the repository, a full never while a copy
that depends on it is kept, a certificate never. :func:`prune_repository` deletes it one verified
file per item - a plain copy or its ``.age`` files - and an emptied base backup folder after.

**Every value the pass runs by is the entry's** (``offload_files_per_pass``, the pauses, the
timeouts, ``offload_stall_seconds``) - nothing here is a number of its own.

**Not built yet, refused by name rather than ignored:** ``offload_scan: clamav`` (1.242 - the
scanner, decision V-3), and a repository that is not an SSH host.
"""

from __future__ import annotations

import posixpath
import re
import socket
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dbabrain.backup_restore import copy_gate, schedule
from dbabrain.backup_restore.backup import (
    BackupJob, BackupTarget, _load_secrets, load_backup_jobs, resolve_backup_target, resolve_ssh_target)
from dbabrain.backup_restore.events import emit_backup_restore_event
from dbabrain.db.backup_catalog import MANIFEST_KEPT, BackupCatalogStore
from dbabrain.db.store import DbaBrainStore, RunAlreadyClaimed, utc_now_text
from dbabrain.lib import errors
from dbabrain.lib import integrity as I
from dbabrain.lib import repository_keys as K
from dbabrain.lib.verified_cleanup import last_part as _last_part
from dbabrain.lib.version import __version__
from dbabrain.transport import common_cli

#: How much longer the transport's own process may live than the request it carries: the request's
#: timeout is the command's; this is only so the child is not killed in the same second it answers.
_PROCESS_MARGIN_SECONDS = 300


class OffloadRefused(errors.Refused):
    """The entry asks for something the repository copy does not do (yet), or cannot reach."""


@dataclass(frozen=True)
class _Entry:
    """One backup entry, as the offload reads it - the jobs share these fields."""

    backup_id: str
    server_id: str
    db_type: str
    backup_dir: str
    offload: Any
    jobs: tuple[BackupJob, ...]

    @property
    def first(self) -> BackupJob:
        return self.jobs[0]

    @property
    def retry_seconds(self) -> int:
        return int(self.offload.retry_interval_seconds)

    @property
    def timeout_seconds(self) -> int:
        return int(self.offload.run_timeout_seconds)


def entries_to_offload(config_path: str | Path) -> list[_Entry]:
    """Every backup entry whose offload is on, one per ``backup_id``."""
    grouped: dict[str, list[BackupJob]] = defaultdict(list)
    for job in load_backup_jobs(config_path):
        grouped[job.backup_id].append(job)
    entries = []
    for backup_id, jobs in grouped.items():
        offload = jobs[0].offload
        if offload is None or not offload.enabled or not any(j.active for j in jobs):
            continue
        entries.append(_Entry(backup_id=backup_id, server_id=jobs[0].server_id, db_type=jobs[0].db_type,
                              backup_dir=jobs[0].backup_dir, offload=offload, jobs=tuple(jobs)))
    return entries


def refusal(entry: _Entry) -> str:
    """Why this entry's offload cannot run as stated - '' when it can."""
    offload = entry.offload
    if offload.scan != "none":
        return (f"offload_scan {offload.scan!r} is not built yet (1.242, decision V-3): no copy can be "
                "called scanned, so none is verified. State offload_scan none until it is.")
    if offload.server_id == entry.server_id:
        return (f"offload_server_id is the backup's own server ({entry.server_id}) - a copy on the same "
                "host is lost with it; the repository is another machine.")
    if not offload.folder.startswith("/"):
        return f"offload_folder must be an absolute path on the repository host; got {offload.folder!r}."
    return ""


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _relative(file_path: str, root: str) -> str:
    """``file_path`` under ``root``, '/'-separated; '' when it is not under it. Case-insensitive for a
    Windows path, as Windows is."""
    windows = "\\" in file_path or (len(file_path) > 1 and file_path[1] == ":")
    norm = (lambda value: value.replace("\\", "/").rstrip("/"))
    path, base = norm(file_path), norm(root)
    if not base:
        return ""
    probe, prefix = (path.lower(), base.lower()) if windows else (path, base)
    if probe.startswith(prefix + "/"):
        return path[len(base) + 1:]
    return ""


def repository_path(entry: _Entry, row: dict[str, Any]) -> str:
    """Where a file's copy lives on the repository: ``<folder>/<server_id>/<db_name>/<the engine's own
    layout below that>`` (0.37.0, the operator 2026-11-10: *the repository tells server_id and database
    apart - <folder>/<server_id>/<db_name>/FULL, DIFF, LOG, ...*).

    The engine's own folders are kept below the database, so a restore from the repository reads the
    layout its script expects (``repository_source`` maps a server folder by the common ending):
    SQL Server's ``<DB>/FULL|DIFF|LOG/<file>`` already starts with the database and is kept as it is;
    PostgreSQL's ``base/`` and ``wal/`` belong to the whole cluster and go under ``cluster`` - the
    database name its scripts' receipts give every PostgreSQL row, PostgreSQL's own word for what a
    base backup copies (the operator 2026-11-11: *the folder that is ok and reasonable*);
    Oracle's RMAN pieces, flat in their folder, go under their database. A file outside the backup
    folder goes under ``<db_name>/<file_type>/``."""
    database = str(row.get("database_name") or "").strip() or I.INSTANCE_LEVEL
    relative = _relative(str(row.get("file_path") or ""), entry.backup_dir)
    if not relative:
        relative = f"{database}/{str(row.get('file_type') or 'FILES').upper()}/{row.get('file_name')}"
    elif relative.split("/", 1)[0].lower() != database.lower():
        relative = f"{database}/{relative}"
    return posixpath.join(entry.offload.folder.rstrip("/"), entry.server_id, relative)


def _source_root(row: dict[str, Any], entry: _Entry) -> str:
    """The backup folder as the SOURCE HOST names it - the fence a delete there is held to."""
    windows = _is_windows(entry)
    host_path = str(row.get("host_path") or row.get("file_path") or "")
    relative = _relative(str(row.get("file_path") or ""), entry.backup_dir)
    if not relative:
        # Outside the entry's backup folder (a WAL archive elsewhere): its own folder is the fence -
        # the backup folder would refuse it, and nothing wider is ever offered.
        path = str(row.get("file_path") if windows else host_path)
        cut = max(path.rfind("/"), path.rfind("\\"))
        return path[:cut] if cut > 0 else path
    if windows:
        return entry.backup_dir
    tail = "/" + relative
    return host_path[: -len(tail)] if host_path.endswith(tail) else entry.backup_dir


def _is_windows(entry: _Entry) -> bool:
    return entry.offload.source_share != "" or str(entry.backup_dir)[1:2] == ":"


def _share_request(entry: _Entry, secrets: dict[str, str]) -> dict[str, Any]:
    """``offload_source_share`` (``\\\\host\\share[\\sub]``) as copy-files' ``source_share``."""
    parts = [p for p in entry.offload.source_share.replace("/", "\\").split("\\") if p]
    if len(parts) < 2:
        raise OffloadRefused(f"{entry.backup_id}: offload_source_share must be \\\\host\\share[\\folder]; "
                             f"got {entry.offload.source_share!r}.")
    ref = entry.offload.source_password_ref
    if ref not in secrets:
        raise OffloadRefused(f"{entry.backup_id}: offload_source_password_ref {ref!r} is not in the secret store.")
    return {"host": parts[0], "share": parts[1], "subpath": "\\".join(parts[2:]),
            "username": entry.offload.source_username, "password": secrets[ref]}


def _share_path(entry: _Entry, row: dict[str, Any], share: dict[str, Any]) -> str:
    relative = _relative(str(row.get("file_path") or ""), entry.backup_dir)
    if not relative:
        raise OffloadRefused(f"{row.get('file_name')}: {row.get('file_path')} is not under the entry's "
                             f"backup_dir {entry.backup_dir} - no path for it on the share.")
    return "\\".join(filter(None, [share["subpath"], relative.replace("/", "\\")]))


# --------------------------------------------------------------------------- #
# One entry's pass
# --------------------------------------------------------------------------- #
class _Hosts:
    """The three ways one pass reaches its machines: hash on the source, hash on the repository,
    copy between them - each a ``common.cli`` call."""

    def __init__(self, entry: _Entry, *, source: BackupTarget, repository: BackupTarget, secrets: dict[str, str],
                 data_dir: Any) -> None:
        from dbabrain.backup_restore.spec_builder import host_block

        self.entry = entry
        self.source_host = host_block(source, secrets=secrets, data_dir=data_dir)
        self.repository_host = host_block(repository, secrets=secrets, data_dir=data_dir)
        self.share = _share_request(entry, secrets) if entry.offload.source_share else None
        login = {k: self.source_host[k] for k in ("host", "port", "username", "password", "key_file")}
        # A backup the database wrote inside its container (0.37.0): on the host it may sit in a
        # Docker volume under /var/lib/docker, which only root enters - the store's first offload
        # found "no file" there. It is read where it was written: listed, hashed, copied and
        # deleted INSIDE the container (docker exec, as the backup itself ran), by its path there.
        self.container = "" if self.share else str(getattr(source, "container_name", "") or "").strip()
        if self.container:
            self.source_host = {**self.source_host, "runtime": "docker", "container": self.container}
            login = {**login, "container": self.container}
        self.copy_source: dict[str, Any] = (
            {"source_share": {k: v for k, v in self.share.items() if k != "subpath"}} if self.share
            else {"source": login})
        self.target = {k: self.repository_host[k] for k in ("host", "port", "username", "password", "key_file")}
        self.copy_errors: dict[str, str] = {}
        #: Set by offload_entry when the pass encrypts on the way (1.330); each copied file's .age facts.
        self.sealing: Any = None
        self.copied_encrypted: dict[str, dict[str, Any]] = {}

    def hasher(self, host: dict[str, Any]):
        def batch(paths: list[str]) -> dict[str, tuple[str, int | None]]:
            if not paths:
                return {}
            timeout = int(self.entry.offload.hash_timeout_seconds)
            answer = common_cli.run("file-checksum", {"host": host, "paths": sorted(set(paths)),
                                                      "timeout_seconds": timeout},
                                    timeout_seconds=timeout + _PROCESS_MARGIN_SECONDS)
            return {f["path"]: (f["sha256"], f.get("size_bytes")) for f in answer.get("files") or []}
        return batch

    def copy_round(self, lines: list[dict[str, Any]]) -> None:
        """One ``copy-files`` for a round - each line a file's full facts (source, target, file_name,
        size_bytes, sha256); each file's own failure is kept for its row."""
        timeout = int(self.entry.offload.copy_timeout_seconds)
        answer = common_cli.run("copy-files", {
            **self.copy_source, "target": self.target, "timeout_seconds": timeout,
            "overwrite": bool(self.entry.offload.copy_overwrite),
            "transfer": self.entry.offload.transfer,
            "stall_seconds": int(self.entry.offload.stall_seconds),
            **({"encrypt": self.sealing.encrypt_block()} if self.sealing is not None else {}),
            "files": [dict(line) for line in lines]},
            timeout_seconds=timeout + _PROCESS_MARGIN_SECONDS, stream_stderr=True)
        for line in answer.get("files") or []:
            if not line.get("ok"):
                self.copy_errors[str(line.get("target"))] = str(line.get("error") or "not copied")
            elif self.sealing is not None:
                self.copied_encrypted[str(line["target"])] = {
                    k: line.get(k) for k in ("encrypted_path", "encrypted_size_bytes", "encrypted_sha256",
                                             "key_id", "age_version")}

    def sealed_hasher(self, plain_of: Any):
        """The gate's target hash when the pass encrypts on the way: each ``.age`` decrypted back, and the
        plaintext's sha256 and bytes are the copy's. A ``.age`` this pass did not write - one an earlier
        pass left whole - is measured first, then held to the same proof: adopted only if it proves."""
        timeout = int(self.entry.offload.hash_timeout_seconds)

        def batch(paths: list[str]) -> dict[str, tuple[str, int | None]]:
            facts = {p: self.copied_encrypted.pop(p) for p in paths if p in self.copied_encrypted}
            unknown = sorted({p for p in paths if p not in facts})
            if unknown:
                found = common_cli.run("file-checksum", {
                    "host": self.repository_host, "paths": [p + K.SUFFIX for p in unknown],
                    "timeout_seconds": timeout}, timeout_seconds=timeout + _PROCESS_MARGIN_SECONDS)
                for f in found.get("files") or []:
                    facts[str(f["path"])[: -len(K.SUFFIX)]] = {
                        "encrypted_path": f["path"], "encrypted_size_bytes": f["size_bytes"],
                        "encrypted_sha256": f["sha256"], "key_id": int(self.sealing.key["key_id"]),
                        "age_version": ""}
            answer: dict[str, tuple[str, int | None]] = {}
            for path, fact in facts.items():
                plain = plain_of(path)
                if plain is None:
                    continue
                proven = self.sealing.verify(host=self.target, transfer=self.entry.offload.transfer, path=path,
                                             facts=fact, plain=plain, timeout=timeout)
                if proven is None:
                    self.copy_errors[path] = f"{fact['encrypted_path']} does not decrypt to the backup: {fact.get('refused')}"[:1000]
                    continue
                answer[path] = proven
            return answer
        return batch


def _list_folder(hosts: _Hosts, host: dict[str, Any], folder: str) -> dict[str, Any]:
    """Every file under ``folder`` on ``host``, hashed there (``file-checksum``'s folder form)."""
    timeout = int(hosts.entry.offload.hash_timeout_seconds)
    return common_cli.run("file-checksum", {"host": host, "folder": folder, "timeout_seconds": timeout},
                          timeout_seconds=timeout + _PROCESS_MARGIN_SECONDS)


def _items(entry: _Entry, rows: list[dict[str, Any]], hosts: _Hosts) -> tuple[list[copy_gate.FileToCopy], dict[int, list[int]], dict[str, str]]:
    """The gate's files for these catalog rows; which items prove which row; each item's copy source.

    A base backup is a directory: each file in it is an item, hashed and copied on its own, and the
    row is proven when all of them are. Its ``backup_manifest`` is held to the checksum the backup
    recorded; the other files to the source as it is now (``compare_copy``) - the manifest is what
    ``pg_verifybackup`` holds them to, on the repository as on the server.
    """
    items: list[copy_gate.FileToCopy] = []
    of_row: dict[int, list[int]] = defaultdict(list)
    copy_source: dict[str, str] = {}
    for row in rows:
        file_type = str(row["file_type"])
        source_path = str(row.get("host_path") or row.get("file_path") or "")
        if hosts.share or getattr(hosts, "container", ""):
            # On the Windows host itself, or inside the container: the path the backup wrote.
            source_path = str(row.get("file_path") or "")
        target_path = repository_path(entry, row)
        common = dict(source_server_id=entry.server_id, target_server_id=entry.offload.server_id,
                      database_name=str(row.get("database_name") or ""), engine=str(row.get("engine") or ""),
                      file_id=int(row["file_id"]), backup_taken_at=row.get("backup_ended_at"),
                      chain_position=row.get("chain_position"))
        member = I.HASHED_MEMBER.get(file_type.upper())
        if member:
            listing = _list_folder(hosts, hosts.source_host, source_path)
            base = str(listing.get("host_folder") or source_path).rstrip("/")
            for found in listing.get("files") or []:
                path = str(found["path"])
                relative = path[len(base) + 1:] if path.startswith(base + "/") else posixpath.basename(path)
                is_manifest = relative == member
                items.append(copy_gate.FileToCopy(
                    **common, file_name=posixpath.basename(path), file_type="MANIFEST" if is_manifest else file_type,
                    source_path=path, target_path=f"{target_path}/{relative}",
                    checksum_after_backup=row.get("checksum_after_backup") if is_manifest else None,
                    source_host_path=path, target_host_path=f"{target_path}/{relative}"))
                of_row[int(row["file_id"])].append(len(items) - 1)
                copy_source[f"{target_path}/{relative}"] = path
            if not of_row.get(int(row["file_id"])):
                of_row[int(row["file_id"])] = []
            continue
        items.append(copy_gate.FileToCopy(
            **common, file_name=str(row["file_name"]), file_type=file_type, source_path=source_path,
            target_path=target_path, checksum_after_backup=row.get("checksum_after_backup"),
            size_after_backup=row.get("size_bytes"), source_host_path=str(row.get("host_path") or ""),
            target_host_path=target_path))
        of_row[int(row["file_id"])].append(len(items) - 1)
        copy_source[target_path] = (_share_path(entry, row, hosts.share) if hosts.share else source_path)
    return items, dict(of_row), copy_source


def offload_entry(entry: _Entry, *, catalog: BackupCatalogStore, hosts: _Hosts, rows: list[dict[str, Any]],
                  sleep=time.sleep, sealing: Any = None) -> dict[str, Any]:
    """Copy and prove ``rows`` onto the repository; mark each row; answer the counts.

    With ``sealing`` (1.330) each file is encrypted on the way and lands only as ``<file>.age``; a copy is
    proven by decrypting it back (``Sealing.verify``), and a verified row is recorded sealed - encrypted,
    proven, no plain file on the repository - in this same pass."""
    items, of_row, copy_source = _items(entry, rows, hosts)
    hosts.sealing = sealing
    hosts.copied_encrypted = {}
    run_id = catalog.start_restore_run(
        # To the microsecond: the run's natural key ends in its start, and an entry's passes come a
        # second apart - two short ones in one second were refused as the same run.
        started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        action=I.ACTION_OFFLOAD, restore_id=entry.backup_id, source_server_id=entry.server_id,
        target_server_id=entry.offload.server_id, target_folder=entry.offload.folder,
        copy_max_retry=entry.offload.copy_max_retry, phase="copying", node_host=socket.gethostname(),
        app_version=__version__)
    integrity = I.RestoreIntegrity(copy_max_retry=entry.offload.copy_max_retry,
                                   copy_retry_interval_seconds=int(entry.offload.copy_retry_interval_seconds),
                                   verify_source_before_copy=True, uncatalogued="compare_copy")
    pending: list[dict[str, Any]] = []
    source_hash = hosts.hasher(hosts.source_host)
    # What the gate measured on the source - each copy line carries it (0.37.0): copy-files takes a
    # file only with its name, bytes and sha256 stated, and lands it only when the stream matches.
    measured: dict[str, tuple[str, int | None]] = {}
    source_of = {item.target_path: item.source_path for item in items}

    def plain_of(target_path: str) -> tuple[str, int] | None:
        found = measured.get(source_of.get(target_path, ""))
        return (str(found[0]), int(found[1])) if found and found[0] and found[1] is not None else None

    repository_hash = (hosts.sealed_hasher(plain_of) if sealing is not None
                       else hosts.hasher(hosts.repository_host))

    def hash_sources(paths: list[str]) -> dict[str, tuple[str, int | None]]:
        found = source_hash(paths)
        measured.update(found)
        return found

    def copy(item: copy_gate.FileToCopy) -> int:
        sha256, size = measured.get(item.source_path) or (None, None)
        if not sha256 or size is None:
            # No fact, no copy - an attempt spent for this file, never a line without its facts.
            raise OffloadRefused(f"{item.source_path}: no size and sha256 measured on the source - not copied")
        pending.append({"source": copy_source[item.target_path], "target": item.target_path,
                        "file_name": _last_part(item.target_path), "size_bytes": int(size),
                        "sha256": str(sha256).lower()})
        return 0

    def hash_targets(paths: list[str]) -> dict[str, tuple[str, int | None]]:
        if pending:
            # The round's copies travel together, then the round is hashed (as restore_script does).
            round_files, pending[:] = list(pending), []
            try:
                hosts.copy_round(round_files)
            except Exception as exc:  # noqa: BLE001 - a round that failed is an attempt spent, per file
                for line in round_files:
                    hosts.copy_errors[line["target"]] = f"{type(exc).__name__}: {exc}"[:1000]
        return repository_hash(paths)

    try:
        outcomes = copy_gate.copy_verified_batch(
            items, integrity=integrity, hash_sources=hash_sources, hash_targets=hash_targets,
            copy=copy, catalog=catalog, restore_run_id=run_id, copy_engine="copy-files", sleep=sleep)
    except Exception as exc:
        catalog.set_restore_run(run_id, phase="error", ended_at=utc_now_text(),
                                error_text=f"{type(exc).__name__}: {exc}"[:2000])
        for row in rows:
            catalog.set_backup_file(int(row["file_id"]), offload_state="failed", offload_attempted_at=utc_now_text(),
                                    offload_error=f"{type(exc).__name__}: {exc}"[:1000])
        raise
    summary = copy_gate.summarize(outcomes)
    now = utc_now_text()
    verified, failed, missing = 0, [], 0
    sealed_rows, sealed_files, seal_failed = 0, 0, []
    for row in rows:
        file_id = int(row["file_id"])
        indexes = of_row.get(file_id) or []
        mine = [outcomes[i] for i in indexes]
        values: dict[str, Any] = {"offload_attempted_at": now, "offload_server_id": entry.offload.server_id,
                                  "offload_path": repository_path(entry, row)}
        if indexes and all(o.proven for o in mine):
            values.update(offload_state="verified", offload_run_id=run_id, offload_verified_at=now, offload_error="")
            catalog.set_backup_file(file_id, **values)
            manifest = next((o for o, i in zip(mine, indexes) if items[i].checksum_after_backup), mine[0])
            catalog.check_backup_file(file_id, checksum_now=manifest.checksum_target_after_copy,
                                      checked_by="offload", by_run_id=str(run_id), location="repository")
            verified += 1
            if sealing is not None:
                try:
                    sealed_files += sealing.record(entry, row, [items[i].target_path for i in indexes])
                    sealed_rows += 1
                except Exception as exc:  # noqa: BLE001 - this row's; the after-copy encryption takes it up
                    seal_failed.append(f"{row['file_name']}: {type(exc).__name__}: {exc}"[:600])
            continue
        reasons = [o.reason or o.verdict for o in mine if not o.proven]
        copy_error = next((hosts.copy_errors[items[i].target_path] for i in indexes
                           if items[i].target_path in hosts.copy_errors), "")
        text = "; ".join(filter(None, ["; ".join(reasons[:3]) or "no file found to copy", copy_error]))[:1000]
        catalog.set_backup_file(file_id, offload_state="failed", offload_error=text,
                                offload_attempted_at=now)
        if mine and all(o.verdict == "source_missing" for o in mine):
            # Gone from its server before it reached the repository: the catalog says so, and it
            # leaves the set the next pass copies - it cannot be copied again.
            catalog.check_backup_file(file_id, checksum_now=None, checked_by="offload", by_run_id=str(run_id))
            missing += 1
        failed.append(f"{row['file_name']}: {text}")
    catalog.set_restore_run(
        run_id, phase="copied" if summary["proven"] else "refused", ended_at=utc_now_text(),
        chain_files=summary["files"], files_matched=summary["verdicts"].get("match", 0),
        files_recopied=summary["recopied"], refused_reason="; ".join(summary["unproven"])[:2000])
    answer = {"offload_run_id": run_id, "files": len(rows), "verified": verified, "failed": failed,
              "source_missing": missing, "recopied": summary["recopied"]}
    if sealing is not None:
        answer["sealed"] = {"rows": sealed_rows + len(seal_failed), "encrypted": sealed_rows, "files": sealed_files,
                            "failed": seal_failed, "waiting": [], "key_id": int(sealing.key["key_id"]),
                            "on_the_way": True}
    return answer


def release_sources(entry: _Entry, *, catalog: BackupCatalogStore, hosts: _Hosts,
                    rows: list[dict[str, Any]], encrypted: set[int] | frozenset[int] = frozenset(),
                    sealed: dict[int, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Delete on the server the files whose repository copy is verified - after proving it again, and
    never the newest chain of a database (1.246).

    A copy held ENCRYPTED on the repository (0.37.0) is released too, since a restore decrypts a
    repository copy (1.294; the operator: *once encrypted ok, delete the old file - on the server_id
    too*). Its plain file is gone from the repository, so what is proven again is its ``.age`` files
    (``sealed``: every one already proven to decrypt to the backup's bytes) against the bytes and
    sha256 recorded when each was made. A row in ``encrypted`` but not in ``sealed`` - encrypted only in
    part, or not yet proven - stays at the source: no whole copy of it is proven anywhere else."""
    sealed = sealed or {}
    keep_chain = bool(getattr(entry.offload, "source_keeps_newest_chain", True))
    newest_full: dict[str, str] = {}
    for row in catalog.newest_full_per_database(backup_id=entry.backup_id):
        newest_full[str(row["database_name"] or "")] = str(row["at"] or "")
    keep, candidates = [], []
    for row in rows:
        if int(row["file_id"]) in encrypted and int(row["file_id"]) not in sealed:
            keep.append(f"{row['file_name']}: its repository copy is not wholly encrypted and proven yet - "
                        "kept at the source")
            continue
        at = str(row.get("backup_ended_at") or row.get("created_at") or "")
        floor = newest_full.get(str(row.get("database_name") or ""))
        if keep_chain and (not floor or at >= floor):
            keep.append(f"{row['file_name']}: in the newest chain of {row.get('database_name') or 'its database'}")
        else:
            candidates.append(row)
    if not candidates:
        return {"deleted": 0, "kept": len(keep), "refused": []}
    # A base backup directory: its files as its offload proved them, on both sides.
    members: dict[int, list[dict[str, Any]]] = {}
    refused: list[str] = []
    for row in list(candidates):
        if I.HASHED_MEMBER.get(str(row["file_type"]).upper()):
            members[int(row["file_id"])] = catalog.offload_members(int(row["file_id"]))
            if not members[int(row["file_id"])]:
                candidates.remove(row)
                refused.append(f"{row['file_name']}: no proof of its files from its offload - the source is kept")
    # Proven again, now: the copy a delete relies on is the copy as it is at this moment.
    found = hosts.hasher(hosts.repository_host)(
        [str(r["offload_path"]) for r in candidates
         if int(r["file_id"]) not in sealed and int(r["file_id"]) not in members]
        + [str(m["target_path"]) for r in candidates if int(r["file_id"]) not in sealed
           for m in members.get(int(r["file_id"]), ())]
        + [str(f["encrypted_path"]) for r in candidates for f in sealed.get(int(r["file_id"]), ())])
    to_delete = []
    for row in candidates:
        if int(row["file_id"]) in sealed:
            # The encrypted copy: each .age file as it was made. One of other bytes - or gone - keeps
            # the source, which is then the only whole copy; the repository check names that file.
            bad = [f for f in sealed[int(row["file_id"])]
                   if not copy_gate.same_bytes(found.get(str(f["encrypted_path"])), str(f["encrypted_sha256"]),
                                               int(f["encrypted_size_bytes"]))]
            if bad:
                refused.append(f"{row['file_name']}: its encrypted repository copy {bad[0]['encrypted_path']} is "
                               "not the bytes it was made with - the source is kept")
                continue
            to_delete.append(row)
            continue
        if int(row["file_id"]) in members:
            bad_member = [m for m in members[int(row["file_id"])]
                          if not copy_gate.same_bytes(found.get(str(m["target_path"])), m.get("checksum_target_after_copy"),
                                                      m.get("size_target_bytes") if m.get("size_target_bytes") is not None
                                                      else m.get("size_source_bytes"))]
            if bad_member:
                refused.append(f"{row['file_name']}: its repository copy {bad_member[0]['target_path']} is not the "
                               "bytes its offload proved - the source is kept")
                continue
            to_delete.append(row)
            continue
        now = found.get(str(row["offload_path"]))
        if not copy_gate.same_bytes(now, row.get("checksum_after_backup"), row.get("size_bytes")):
            result = catalog.check_backup_file(int(row["file_id"]), checksum_now=now[0] if now else None,
                                               size_now=now[1] if now else None, checked_by="offload",
                                               by_run_id="release", location="repository")
            if result == "match":
                result = f"{now[1]} bytes, not the {row.get('size_bytes')} recorded"
            catalog.set_backup_file(int(row["file_id"]), offload_state="failed",
                                    offload_error=f"the repository copy is {result} at the release check - "
                                                  "the source is kept and copied again")
            refused.append(f"{row['file_name']}: repository copy {result}")
            continue
        to_delete.append(row)
    deleted = 0
    for row in to_delete:
        if int(row["file_id"]) in members:
            newest = (not keep_chain and catalog.newest_base_directory(
                server_id=str(row.get("server_id") or entry.server_id),
                database_name=str(row.get("database_name") or "")) == int(row["file_id"]))
            manifest = I.HASHED_MEMBER.get(str(row["file_type"]).upper()) if newest else None
            done, why = _release_directory(entry, row, members[int(row["file_id"])], hosts, keep_member=manifest)
            if done and manifest:
                # The newest base: its data is gone, its manifest stays for the next incremental.
                catalog.set_backup_file(int(row["file_id"]), pruned_by=MANIFEST_KEPT)
                deleted += 1
            elif done:
                catalog.set_backup_file(int(row["file_id"]), state="pruned", on_disk=0,
                                        source_deleted_at=utc_now_text(),
                                        pruned_by="offload: released file by file after its copy was verified")
                deleted += 1
            else:
                refused.append(f"{row['file_name']}: {why}"[:500])
            continue
        # ONE file per command, with its every fact (0.37.0, the operator 2026-11-10): the host deletes
        # it only when its name, path, size and sha256 are still the ones its backup recorded - which
        # its repository copy was just proven to hold.
        try:
            request = release_request(entry, row, hosts)
        except OffloadRefused as exc:
            refused.append(f"{row['file_name']}: delete refused - {exc}"[:500])
            continue
        ok, answer, error = common_cli.run_allowing_failure(
            "delete-verified-file", request,
            timeout_seconds=int(entry.offload.hash_timeout_seconds) + _PROCESS_MARGIN_SECONDS)
        line = (answer or {}).get("file") or {}
        if ok and line.get("status") in ("deleted", "not_found"):
            catalog.set_backup_file(int(row["file_id"]), state="pruned", on_disk=0,
                                    source_deleted_at=utc_now_text(),
                                    pruned_by="offload: released after its copy was verified")
            deleted += 1
        else:
            # One file refused is that file's; the rest go on, each with its own command.
            refused.append(f"{row['file_name']}: delete {line.get('status') or 'refused'} - "
                           f"{line.get('reason') or error}"[:500])
    return {"deleted": deleted, "kept": len(keep), "refused": refused}


def _release_root(entry: _Entry, row: dict[str, Any], hosts: _Hosts) -> str:
    """The fence a delete at the source is held to: the row's own backup folder as that host names it."""
    return (str(entry.backup_dir).rstrip("/") if getattr(hosts, "container", "") and
            _relative(str(row.get("file_path") or ""), entry.backup_dir) else _source_root(row, entry))


def _release_directory(entry: _Entry, row: dict[str, Any], members: list[dict[str, Any]],
                       hosts: _Hosts, *, keep_member: str | None = None) -> tuple[bool, str]:
    """Delete a base backup directory at the source: each of its files, by the path, bytes and sha256
    its offload measured there, one verified file per item (``delete-verified-files``) - then the
    folder, which ``delete-empty-folder`` deletes only when nothing is left in it. ``(done, why not)``.

    ``keep_member``: the one file left (the newest base's ``backup_manifest``, which the next
    incremental chains onto) - the folder then stays too."""
    inside = bool(hosts.share) or bool(getattr(hosts, "container", ""))
    folder = str(row["file_path"] if inside else (row.get("host_path") or row["file_path"])).rstrip("/\\")
    root = _release_root(entry, row, hosts)
    items = []
    for member in members:
        path = str(member["source_path"] if inside else (member.get("source_host_path") or member["source_path"]))
        sha = str(member.get("checksum_source_before_copy") or "").strip().lower()
        size = member.get("size_source_bytes")
        if (not re.fullmatch(r"[0-9a-f]{64}", sha) or isinstance(size, bool) or not isinstance(size, int)
                or size < 0):
            return False, f"no bytes or sha256 recorded for {path} - nothing deleted"
        if not path.startswith(folder + "/"):
            return False, f"{path} is not inside {folder} - nothing deleted"
        if keep_member and _last_part(path) == keep_member:
            continue
        items.append({"path": path, "file_name": _last_part(path), "size_bytes": size, "sha256": sha,
                      "must_be_under": root})
    if keep_member and len(items) == len(members):
        return False, f"its {keep_member} is not among the files its offload proved - nothing deleted"
    timeout = int(entry.offload.hash_timeout_seconds)
    if not items:
        return True, ""
    ok, answer, error = common_cli.run_allowing_failure(
        "delete-verified-files", {"host": hosts.source_host, "files": items, "timeout_seconds": timeout},
        timeout_seconds=timeout * len(items) + _PROCESS_MARGIN_SECONDS)
    lines = {str(line.get("path")): line for line in (answer or {}).get("files") or []}
    bad = [(item["path"], lines.get(item["path"]) or {"status": "not answered"}) for item in items
           if (lines.get(item["path"]) or {}).get("status") not in ("deleted", "not_found")]
    if bad:
        path, line = bad[0]
        return False, f"{path} {line.get('status')} - {line.get('reason') or error or ''}"
    if keep_member:
        return True, ""
    ok, answer, error = common_cli.run_allowing_failure(
        "delete-empty-folder", {"host": hosts.source_host, "path": folder, "must_be_under": root,
                                "timeout_seconds": min(timeout, 3600)},
        timeout_seconds=min(timeout, 3600) + _PROCESS_MARGIN_SECONDS)
    status = ((answer or {}).get("folder") or {}).get("status")
    if status not in ("deleted", "not_found"):
        reason = ((answer or {}).get("folder") or {}).get("reason") or error or ""
        return False, f"its files are deleted, its folder is not - {status}: {reason}"
    return True, ""


def release_request(entry: _Entry, row: dict[str, Any], hosts: _Hosts) -> dict[str, Any]:
    """The ``delete-verified-file`` request for one catalog row - every fact from what its backup
    recorded, the fence its own backup folder on the source host; refused when a fact is missing."""
    inside = bool(hosts.share) or bool(getattr(hosts, "container", ""))
    path = str(row["file_path"] if inside else (row.get("host_path") or row["file_path"]))
    sha256 = str(row.get("checksum_after_backup") or "").strip().lower()
    size = row.get("size_bytes")
    name = str(row.get("file_name") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise OffloadRefused("no sha256 recorded for it - a file is deleted only with its checksum stated")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise OffloadRefused(f"no size recorded for it ({size!r}) - a file is deleted only with its bytes stated")
    if name != _last_part(path):
        raise OffloadRefused(f"its catalog name {name!r} is not the last part of {path!r}")
    root = _release_root(entry, row, hosts)
    return {"host": hosts.source_host, "path": path, "file_name": name, "size_bytes": size,
            "sha256": sha256, "must_be_under": root,
            "timeout_seconds": int(entry.offload.hash_timeout_seconds)}


def repository_source(locations: list[dict[str, Any]], host_dir: str) -> tuple[str, str]:
    """``(repository server_id, folder)`` holding the copies of ``host_dir``, a folder on the server.

    The repository keeps the server's own layout under ``<offload_folder>/<server_id>/``, so a copy's
    path and its file's path on the server end the same way. The part before that common ending is
    the same folder on each side; ``host_dir`` is translated through it. Read from the catalog's
    verified copies - no entry is looked up, and a server whose copies went to more than one
    repository is refused by name rather than half-read from one of them.
    """
    if not locations:
        raise OffloadRefused(f"no verified repository copy of {host_dir} - offload the backups first, or "
                             "state restore_from: server.")
    want = host_dir.replace("\\", "/").rstrip("/")
    found: set[tuple[str, str]] = set()
    for row in locations:
        server_side = str(row.get("host_path") or row.get("file_path") or "").replace("\\", "/").split("/")
        repo_side = str(row["offload_path"]).split("/")
        common = 0
        while (common < min(len(server_side), len(repo_side)) - 1
               and server_side[-1 - common] == repo_side[-1 - common]):
            common += 1
        if not common:
            continue
        server_root = "/".join(server_side[:-common]).rstrip("/")
        repo_root = "/".join(repo_side[:-common]).rstrip("/")
        if want == server_root or want.startswith(server_root + "/"):
            found.add((str(row["offload_server_id"]), repo_root + want[len(server_root):]))
    # Only the copies OF this folder decide: another entry of the same server may offload elsewhere.
    if not found:
        raise OffloadRefused(f"no verified repository copy lies under {host_dir} - the copies are of another "
                             "folder of this server.")
    if len(found) > 1:
        raise OffloadRefused(f"the copies of {host_dir} are in {len(found)} places "
                             f"({', '.join(sorted(f'{s}:{d}' for s, d in found))}) - one restore reads one.")
    return found.pop()


# --------------------------------------------------------------------------- #
# The repository's retention (offload_retention, seconds per file type)
# --------------------------------------------------------------------------- #
def _at(row: dict[str, Any]) -> str:
    return str(row.get("backup_ended_at") or row.get("created_at") or "")


def expiry_plan(rows: list[dict[str, Any]], offload: Any, *, now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Which repository copies have outlived their retention: ``(expire, kept_reasons)``.

    ``rows`` are an entry's verified copies, each with its ``chain_start`` (for a full: the start of
    the run that wrote it). The rules, per database:

    1. **By its own type's age.** ``offload.retention_of(file_type)`` seconds after the backup ended;
       ``None`` (a type not named, or ``"keep"``) keeps it for good.
    2. **Never the newest chain.** Nothing from the start of the newest full's run on: the
       repository can always restore its latest point, whatever the numbers say.
    3. **A full is the floor of its chain - and a full is every file of its run.** The pieces of one
       full (striped files, RMAN pieces) and the control file and spfile written with them are one
       unit: it expires only when every one of them is old AND every copy that depends on it - the
       DIFFs and LOGs after it, before the next full - expires too.
    4. **A database with no full on the repository expires nothing** - its LOGs are the only chain
       there, whatever their age.
    5. **A certificate never expires here.** Every encrypted backup made with it needs it, and a lost
       certificate makes all of them unreadable; it is removed by hand or not at all. A control file or
       spfile that belongs to no full on the repository is kept for the same reason.
    """
    expire: list[dict[str, Any]] = []
    kept: list[str] = []
    by_db: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_db[str(row.get("database_name") or "")].append(row)

    def old(row: dict[str, Any]) -> bool:
        keep_for = offload.retention_of(str(row["file_type"]))
        if keep_for is None or int(keep_for) <= 0 or not _at(row):
            return False
        ended = datetime.strptime(_at(row)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return (now - ended).total_seconds() > int(keep_for)

    def level_of(row: dict[str, Any]) -> str | None:
        return I.LEVEL_OF_FILE_TYPE.get(str(row["file_type"]).upper())

    def start_of(row: dict[str, Any]) -> str:
        return str(row.get("chain_start") or _at(row))

    for database, copies in by_db.items():
        copies.sort(key=lambda r: (_at(r), int(r["file_id"])))
        # One full = every full-level file of one run, keyed by that run's start.
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in copies:
            if level_of(row) == "full":
                groups[start_of(row)].append(row)
        if not groups:
            if copies:
                kept.append(f"{database or 'its database'}: no full on the repository - nothing of it expires")
            continue
        starts = sorted(groups)
        newest = starts[-1]
        dependents: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in copies:
            level = level_of(row)
            if level is None and str(row["file_type"]).upper() == "CERT":
                kept.append(f"{row['file_name']}: a certificate - never expired on the repository")
            elif level is None:
                # A control file or spfile travels with the full of its run, or stays.
                if start_of(row) in groups:
                    groups[start_of(row)].append(row)
                else:
                    kept.append(f"{row['file_name']}: belongs to no full on the repository - kept")
            elif level in ("diff", "log"):
                owner = max((s for s in starts if s <= _at(row)), default=None)
                if owner is None:
                    # Older than every full there: it depends on none of them, and goes by its own age.
                    if old(row):
                        expire.append(row)
                    continue
                dependents[owner].append(row)
        for start in starts:
            if start >= newest:
                continue                      # the newest chain: kept, and not news
            gone = [row for row in dependents[start] if old(row)]
            expire.extend(gone)
            members = groups[start]
            if not all(old(row) for row in members):
                continue
            if len(gone) < len(dependents[start]):
                kept.append(f"{', '.join(r['file_name'] for r in members)}: past its retention, kept - a copy "
                            "that depends on it is not")
                continue
            expire.extend(members)
    return expire, kept


def prune_repository(entry: _Entry, *, catalog: BackupCatalogStore, hosts: _Hosts,
                     now: datetime | None = None, encrypted: set[int] | frozenset[int] = frozenset(),
                     sealed: dict[int, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Delete on the repository the copies :func:`expiry_plan` names, and mark them ``expired``.

    One ``delete-verified-files`` per copy, each of its files an item with every fact - its path,
    name, bytes and sha256 - so the repository deletes a file only while it is still the one proven
    (0.37.0, the operator: *one file per delete, with its every fact*; *by an array of files*):
    a plain copy is held to its backup's facts, a base backup directory's files to the proofs its
    offload recorded, an ENCRYPTED copy's ``.age`` files (``sealed``) to the facts measured when
    each was made. A copy is expired when every one of its files is gone; then an emptied base
    backup folder goes too (``delete-empty-folder`` - a stray left in it keeps it, and the repository
    check names it). One copy's refusal is its own; the rest go on. A copy encrypted only in part is
    kept until the encryption finishes - neither set of facts covers all of it."""
    sealed = sealed or {}
    now = now or datetime.now(timezone.utc)
    expire, kept = _entry_expiry(entry, catalog, now=now)
    if not expire:
        return {"expired": 0, "kept": kept, "refused": []}
    fence = posixpath.join(entry.offload.folder.rstrip("/"), entry.server_id)
    timeout = int(entry.offload.hash_timeout_seconds)
    proofs: dict[str, dict[str, Any]] | None = None
    refused: list[str] = []
    expired = 0
    for row in expire:
        file_id, name = int(row["file_id"]), str(row["file_name"])
        directory = bool(I.HASHED_MEMBER.get(str(row["file_type"]).upper()))
        if file_id in encrypted and file_id not in sealed:
            kept.append(f"{name}: past its retention, kept - its repository copy is encrypted in part; it "
                        "expires once the encryption finishes")
            continue
        if file_id in sealed:
            facts = [(str(f["encrypted_path"]), f["encrypted_size_bytes"], f["encrypted_sha256"])
                     for f in sealed[file_id]]
        elif directory:
            if proofs is None:
                proofs = catalog.repository_proofs(server_id=entry.server_id,
                                                   repository_server_id=entry.offload.server_id)
            facts = [(path, proof.get("size_bytes"), proof.get("checksum_after_backup"))
                     for path, proof in proofs.items() if int(proof["file_id"]) == file_id]
        else:
            facts = [(str(row["offload_path"]), row.get("size_bytes"), row.get("checksum_after_backup"))]
        items, missing = [], []
        for path, size, sha in sorted(facts):
            sha_text = str(sha or "").strip().lower()
            if (isinstance(size, bool) or not isinstance(size, int) or size < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", sha_text)):
                missing.append(path)
                continue
            items.append({"path": path, "file_name": posixpath.basename(path), "size_bytes": size,
                          "sha256": sha_text, "must_be_under": fence})
        if missing or not items:
            refused.append(f"{name}: past its retention, nothing deleted - "
                           + (f"no bytes or sha256 recorded for {missing[0]}" if missing
                              else "no proof of its files on the repository"))
            continue
        try:
            ok, answer, error = common_cli.run_allowing_failure(
                "delete-verified-files", {"host": hosts.repository_host, "files": items,
                                          "timeout_seconds": timeout},
                timeout_seconds=timeout * len(items) + _PROCESS_MARGIN_SECONDS)
        except Exception as exc:  # noqa: BLE001 - this copy's own failure; the next pass tries again
            refused.append(f"{name}: delete refused on the repository - {type(exc).__name__}: {exc}"[:500])
            continue
        lines = {str(line.get("path")): line for line in (answer or {}).get("files") or []}
        bad = [(item["path"], lines.get(item["path"]) or {"status": "not answered"}) for item in items
               if (lines.get(item["path"]) or {}).get("status") not in ("deleted", "not_found")]
        if bad:
            path, line = bad[0]
            refused.append(f"{name}: {path} {line.get('status')} - {line.get('reason') or error or ''}"[:500])
            continue
        if directory:
            ok, answer, error = common_cli.run_allowing_failure(
                "delete-empty-folder", {"host": hosts.repository_host, "path": str(row["offload_path"]),
                                        "must_be_under": fence, "timeout_seconds": min(timeout, 3600)},
                timeout_seconds=min(timeout, 3600) + _PROCESS_MARGIN_SECONDS)
            folder = (answer or {}).get("folder") or {}
            if folder.get("status") not in ("deleted", "not_found"):
                refused.append(f"{name}: its files are deleted, its folder is not - {folder.get('status')}: "
                               f"{folder.get('reason') or error or ''}"[:500])
        catalog.set_backup_file(file_id, offload_state="expired", offload_expired_at=utc_now_text())
        expired += 1
    return {"expired": expired, "kept": kept, "refused": refused}


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #
def run_offloads(
    *,
    app_config: Any,
    config_path: str | Path,
    logger: Any = None,
    backup_id: str | None = None,
    data_dir: str | Path | None = None,
    key: str | None = None,
    key_base64: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    sleep=time.sleep,
) -> dict[str, Any]:
    """Every entry with ``offload_enabled``: copy what is still to copy, release what may be released.

    An entry with nothing to do writes nothing - no run row, no event - so a pass a second costs a
    few catalog reads per entry. A failed copy is tried again after ``offload_retry_interval_seconds``,
    not every second. ``limit`` overrides the entry's ``offload_files_per_pass`` for this pass only.
    """
    entries = [e for e in entries_to_offload(config_path) if not backup_id or e.backup_id == backup_id]
    summary: dict[str, Any] = {"entries": len(entries), "ran": 0, "succeeded": 0, "failed": 0, "jobs": []}
    if not entries:
        return summary
    store = DbaBrainStore.from_config(app_config)
    catalog = BackupCatalogStore.from_config(app_config)
    schedule.reap_stale_runs(
        store=store, app_config=app_config, logger=logger,
        timeouts={schedule.offload_job_code(e.backup_id): e.timeout_seconds for e in entries},
        notify={schedule.offload_job_code(e.backup_id): e.first.notify for e in entries})
    from dbabrain.db.lockdown_store import lockdown_on

    locked = lockdown_on(app_config)
    for entry in entries:
        retry_before = (datetime.now(timezone.utc) - timedelta(seconds=entry.retry_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        per_pass = int(limit or entry.offload.files_per_pass)
        rows = catalog.files_to_offload(backup_id=entry.backup_id, limit=per_pass, retry_before=retry_before,
                                        since=entry.offload.files_since)
        # The delete at the source waits out a lockdown (1.220); the copy off the host goes on.
        releasable = (catalog.files_to_release(backup_id=entry.backup_id, limit=per_pass,
                                               keep_newest_chain=entry.offload.source_keeps_newest_chain)
                      if entry.offload.delete_source and not locked else [])
        expirable = _expirable(entry, catalog)
        encryptable = _encryptable(entry, catalog, app_config)
        if not rows and not releasable and not expirable and not encryptable:
            continue
        if dry_run:
            summary["jobs"].append({"backup_id": entry.backup_id, "status": "dry-run", "to_copy": len(rows),
                                    "to_release": len(releasable), "to_expire": len(expirable),
                                    "to_encrypt": len(encryptable)})
            continue
        refresh = ((lambda entry=entry, per_pass=per_pass: catalog.files_to_release(
            backup_id=entry.backup_id, limit=per_pass, keep_newest_chain=entry.offload.source_keeps_newest_chain))
            if entry.offload.delete_source and not locked else None)
        summary["jobs"].append(_run_one(entry, rows=rows, releasable=releasable, expirable=expirable,
                                        store=store, catalog=catalog, refresh_release=refresh,
                                        app_config=app_config, logger=logger, data_dir=data_dir, key=key,
                                        key_base64=key_base64, sleep=sleep, summary=summary))
    return summary


def _merge_encrypt(on_the_way: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    if not on_the_way:
        return after
    merged = dict(on_the_way)
    for field in ("rows", "encrypted", "files"):
        merged[field] = int(on_the_way.get(field) or 0) + int(after.get(field) or 0)
    for field in ("failed", "waiting"):
        merged[field] = list(on_the_way.get(field) or []) + list(after.get(field) or [])
    merged["key_id"] = on_the_way.get("key_id") or after.get("key_id")
    return merged


def offloads_running_beside(store: Any, entry: _Entry) -> list[str]:
    """The other entries of ``entry``'s server whose offload is running now - read from the run table,
    where each offload run states its server_id. Unreadable is none: the check then runs as before."""
    import json

    prefix = schedule.OFFLOAD_JOB_PREFIX + "."
    try:
        with store.connect() as conn:
            rows = conn.execute("SELECT job_code, metadata_json FROM job_runs WHERE status = 'RUNNING' "
                                "AND job_code LIKE ?", (prefix + "%",)).fetchall()
    except Exception:  # noqa: BLE001 - a store that cannot say is not a reason to skip the check
        return []
    busy = []
    for row in rows:
        backup_id = str(row["job_code"])[len(prefix):]
        if backup_id == entry.backup_id:
            continue
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if isinstance(metadata, dict) and metadata.get("server_id") == entry.server_id:
            busy.append(backup_id)
    return sorted(busy)


def check_repository(entry: _Entry, *, catalog: BackupCatalogStore, app_config: Any, hosts: _Hosts) -> dict[str, Any]:
    """The server's repository folder against what the catalog proved there (0.37.0,
    ``lib.repository_check``): a stray, missing, resized or non-file entry fails the run, and the run's
    error event reaches the entry's backup/restore group. Listed without hashing (``list-files``)."""
    from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore
    from dbabrain.lib import repository_check as RC

    folder = posixpath.join(entry.offload.folder.rstrip("/"), entry.server_id)
    proofs = catalog.repository_proofs(server_id=entry.server_id, repository_server_id=entry.offload.server_id)
    # Only the encrypted files of copies still proven: an expired copy's .age files are gone by design.
    live = {int(proof["file_id"]) for proof in proofs.values()}
    sealed = [row for row in RepositoryEncryptionStore.from_config(app_config).files_for_server(
        server_id=entry.server_id, repository_server_id=entry.offload.server_id) if int(row["file_id"]) in live]
    expected = RC.expected_files(folder, proofs, sealed)
    timeout = int(entry.offload.hash_timeout_seconds)
    listing = common_cli.run("list-files", {"host": hosts.repository_host, "folder": folder,
                                            "timeout_seconds": min(timeout, 86400)},
                             timeout_seconds=min(timeout, 86400) + _PROCESS_MARGIN_SECONDS)
    if listing.get("truncated"):
        # A partial listing would call everything past its end missing - and hide a stray among them.
        message = (f"{folder}: holds more than {len(listing.get('files') or [])} entries - listed in part, "
                   "so not checked")
        return {"folder": folder, "clean": False, "files": listing.get("listed"), "message": message}
    found = RC.findings(expected, listing.get("files") or [])
    clean = RC.is_clean(found)
    return {"folder": folder, "clean": clean, "files": len(listing.get("files") or []),
            "expected": len(expected), "counts": {kind: len(found[kind]) for kind in RC.KINDS},
            # The first of each kind travel with the run; the message names the first three.
            "findings": {kind: found[kind][:20] for kind in RC.KINDS if found[kind]},
            "message": RC.describe(folder, found)}


def _encryptable(entry: _Entry, catalog: BackupCatalogStore, app_config: Any) -> list[dict[str, Any]]:
    """The entry's proven repository copies not yet wholly encrypted - none when it does not encrypt."""
    if not entry.offload.encrypt:
        return []
    from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore

    done = RepositoryEncryptionStore.from_config(app_config).done_rows(backup_id=entry.backup_id)
    return [row for row in catalog.repository_copies(backup_id=entry.backup_id) if int(row["file_id"]) not in done]


def _expirable(entry: _Entry, catalog: BackupCatalogStore) -> list[dict[str, Any]]:
    """The copies the repository's retention would remove now - none when every level keeps for good,
    without reading the catalog: an entry that never prunes costs nothing here."""
    offload = entry.offload
    shortest = offload.shortest_retention()
    if shortest is None:
        return []
    now = datetime.now(timezone.utc)
    # One cheap question a second before the whole plan: is any copy older than the shortest
    # retention at all? Most passes answer no, and read nothing more.
    before = (now - timedelta(seconds=shortest)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not catalog.has_repository_copy_before(backup_id=entry.backup_id, before=before):
        return []
    expire, _kept = _entry_expiry(entry, catalog, now=now)
    return expire


def _entry_expiry(entry: _Entry, catalog: BackupCatalogStore, *, now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """:func:`expiry_plan` over the server's copies - every entry's, so a LOG or DIFF entry sees the
    fulls its server's FULL entry wrote (the newest chain, what depends on what) - and only this
    entry's copies expired by this entry, each by this entry's retention. Before 2026-11-11 the plan
    read one entry's copies, and a LOG entry, with no full of its own, never expired anything."""
    expire, kept = expiry_plan(catalog.server_repository_copies(server_id=entry.server_id), entry.offload, now=now)
    own = [row for row in expire if str(row.get("backup_id") or "") == entry.backup_id]
    names = {str(row.get("file_name") or "") for row in catalog.repository_copies(backup_id=entry.backup_id)}
    return own, [line for line in kept if line.split(":", 1)[0] in names or line.split(":", 1)[0] == ""]


#: A copy this big - or a base backup directory, whose size is known only as it is copied - is a long
#: step: its own start message before its done one (1.306).
LONG_COPY_BYTES = 1024 ** 3


def is_long_copy(rows: list[dict[str, Any]]) -> bool:
    return (sum(int(r.get("size_bytes") or 0) for r in rows) >= LONG_COPY_BYTES
            or any(I.HASHED_MEMBER.get(str(r.get("file_type")).upper()) for r in rows))


def _key_until(app_config: Any, result: dict[str, Any]) -> str:
    key_id = (result.get("encrypt") or {}).get("key_id")
    if not key_id:
        return ""
    try:
        from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore

        return str((RepositoryEncryptionStore.from_config(app_config).key(int(key_id)) or {}).get("encrypt_until") or "")
    except Exception:  # noqa: BLE001 - a date the message cannot read is left out, never a failure
        return ""


def offload_flow_lines(entry: _Entry, result: dict[str, Any], *, error: str, status: str,
                       key_until: str = "") -> dict[str, Any]:
    """Steps 2-5 of a backup flow, as its message reads them (1.306, the format the operator approved
    2026-11-11): one line per step - what it did, of how many, where - a failed step named with why,
    a step the failure stopped "not run", a step with nothing to do this pass said so."""
    copied, encrypted = result.get("copy") or {}, result.get("encrypt") or {}
    released, expired = result.get("release") or {}, result.get("expire") or {}
    checked = result.get("repository_check") or {}
    stopped = bool(error)

    def idle(what: str) -> str:
        return f"- {what}: not run" if stopped else f"- {what}: nothing this pass"

    lines = []
    if copied:
        bad = copied.get("failed") or []
        lines.append(f"{'❌' if bad else '✅'} 2. Copy {copied.get('verified', 0)}/{copied.get('files', 0)} -> "
                     f"{entry.offload.server_id} · bytes + SHA-256 = server" + (f" - {bad[0]}"[:300] if bad else ""))
    else:
        lines.append(idle("2. Copy"))
    if not entry.offload.encrypt:
        lines.append("- 3. Encrypt: off for this entry")
    elif encrypted.get("waiting") and not encrypted.get("encrypted") and not encrypted.get("failed"):
        lines.append(f"⏳ 3. Encrypt: waiting - {encrypted['waiting'][0]}"[:300])
    elif encrypted.get("rows"):
        bad = encrypted.get("failed") or []
        lines.append(f"{'❌' if bad else '✅'} 3. Encrypt {encrypted.get('encrypted', 0)}/{encrypted.get('rows', 0)} · "
                     f"age key #{encrypted.get('key_id')}" + (f" (encrypts until {key_until[:10]})" if key_until else "")
                     + " · decrypted back = server" + (" · on the way" if encrypted.get("on_the_way") else "")
                     + (f" - {bad[0]}"[:300] if bad else ""))
    else:
        lines.append(idle("3. Encrypt"))
    if not entry.offload.delete_source:
        lines.append("- 4. Delete at the source: off for this entry")
    elif released:
        bad = released.get("refused") or []
        lines.append(f"{'❌' if bad else '✅'} 4. Delete at the source {released.get('deleted', 0)} on {entry.server_id}, "
                     "file by file" + (f" - {bad[0]}"[:300] if bad else ""))
    else:
        lines.append(idle("4. Delete at the source"))
    if expired:
        bad = expired.get("refused") or []
        lines.append(f"{'❌' if bad else '✅'} Retention: {expired.get('expired', 0)} copy(ies) past their window "
                     "deleted on the repository" + (f" - {bad[0]}"[:300] if bad else ""))
    if checked.get("skipped"):
        lines.append("- 5. Repository check: next pass (another offload of this server is writing there)")
    elif checked:
        lines.append(f"✅ 5. Repository check: clean · {checked.get('files', 0)} file(s)" if checked.get("clean")
                     else f"❌ 5. Repository check: {checked.get('message', '')}"[:400])
    elif entry.offload.repository_check:
        lines.append(idle("5. Repository check"))
    if error:
        lines.append(f"❌ {error}"[:400])
    title = f"{entry.backup_id} · 2-5. Repository {'done' if status == 'done' else 'ERROR'}"
    return {"flow_title": title, "flow_lines": lines}


def _run_one(entry: _Entry, *, rows, releasable, expirable=(), store, catalog, app_config, logger, data_dir, key,
             key_base64, sleep, summary, refresh_release=None) -> dict[str, Any]:
    job_code = schedule.offload_job_code(entry.backup_id)
    metadata = {"backup_id": entry.backup_id, "server_id": entry.server_id,
                "repository_server_id": entry.offload.server_id, "to_copy": len(rows), "to_release": len(releasable),
                "to_expire": len(expirable)}
    try:
        log_id, started_at = schedule.start_run(
            store=store, job_code=job_code, message=f"Offload {entry.backup_id} started.", metadata=metadata)
    except RunAlreadyClaimed:
        return {"backup_id": entry.backup_id, "status": "already_running"}
    summary["ran"] += 1
    started = time.monotonic()
    result: dict[str, Any] = {}
    error = ""
    hosts = None
    try:
        why = refusal(entry)
        if why:
            raise OffloadRefused(f"{entry.backup_id}: {why}")
        source = resolve_backup_target(entry.first, data_dir=data_dir)
        repository = resolve_ssh_target(entry.offload.server_id, label=f"{entry.backup_id} repository",
                                        data_dir=data_dir, require_container=False)
        if repository.access != "ssh" or repository.platform == "windows":
            raise OffloadRefused(f"{entry.backup_id}: the repository {entry.offload.server_id} is reached over "
                                 f"{repository.access} on {repository.platform}; only an SSH (Linux) repository is "
                                 "built - a Windows share as the repository is a later item.")
        if source.platform == "windows" and not entry.offload.source_share and source.access != "ssh":
            raise OffloadRefused(f"{entry.backup_id}: {entry.server_id} is a Windows server reached over "
                                 f"{source.access} - read it over its share (offload_source_share, _username, "
                                 "_password_ref) or over SSH.")
        if source.platform == "windows" and not entry.offload.source_share and entry.offload.transfer == "cat":
            # A Windows server over SSH answers in PowerShell, where "cat" is Get-Content - text, not
            # the file's bytes. Its OpenSSH serves SFTP, which reads the bytes as they are.
            raise OffloadRefused(f"{entry.backup_id}: {entry.server_id} is a Windows server read over SSH - "
                                 "state offload_transfer: sftp.")
        secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64)
        hosts = _Hosts(entry, source=source, repository=repository, secrets=secrets, data_dir=data_dir)
        if rows and is_long_copy(rows):
            # A long step a start and a done (1.306): a FULL's copy and encryption take minutes to hours.
            size = sum(int(r.get("size_bytes") or 0) for r in rows)
            emit_backup_restore_event(
                app_config=app_config, command="offload", phase="START", level="logging",
                message=f"Offload {entry.backup_id} started: {len(rows)} file(s).", logger=logger,
                started_at=started_at, notify=entry.first.notify,
                metadata={**metadata, "flow_title": f"{entry.backup_id} · 2-3. Copy + encrypt started",
                          "flow_lines": [f"{len(rows)} file(s), {size / 1024 ** 3:.1f} GB -> {entry.offload.server_id}"
                                         + (" (a base backup directory: its size is read as it is copied)"
                                            if any(I.HASHED_MEMBER.get(str(r.get('file_type')).upper()) for r in rows)
                                            else "")]})
        sealing = None
        if rows and entry.offload.encrypt:
            # 1.330: encrypted on the way - the key is taken before the copy. A key that cannot be had
            # here (the vault not set up) leaves the copy plain, and the after-copy encryption below
            # says why, as it did before.
            from dbabrain.backup_restore.repository_encryption import sealing_for
            from dbabrain.lib.secret_text import resolve_cli_key, resolve_key

            try:
                sealing = sealing_for(entry, app_config=app_config, secrets=secrets,
                                      passphrase=resolve_cli_key(key, key_base64) or resolve_key())
            except Exception:  # noqa: BLE001 - named by encrypt_rows below, which meets the same refusal
                sealing = None
        if rows:
            result["copy"] = offload_entry(entry, catalog=catalog, hosts=hosts, rows=rows, sleep=sleep,
                                           sealing=sealing)
        encrypted: set[int] = set()
        sealed: dict[int, list[dict[str, Any]]] = {}
        if entry.offload.encrypt:
            # 0.37.0: each proven copy encrypted at the repository, proven, its plain file gone there.
            from dbabrain.backup_restore.repository_encryption import encrypt_rows
            from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore
            from dbabrain.lib.secret_text import resolve_cli_key, resolve_key

            enc_store = RepositoryEncryptionStore.from_config(app_config)
            after = encrypt_rows(
                entry, app_config=app_config, store=enc_store, hosts=hosts,
                rows=catalog.repository_copies(backup_id=entry.backup_id), secrets=secrets,
                passphrase=resolve_cli_key(key, key_base64) or resolve_key())
            # One step-3 line for the pass: what was sealed on the way, and what the after-copy
            # encryption did for copies made plain (before 1.330, or with no key at copy time).
            on_the_way = (result.get("copy") or {}).pop("sealed", None)
            result["encrypt"] = _merge_encrypt(on_the_way, after)
        if refresh_release is not None and (rows or result.get("encrypt")):
            # This pass's own copies, verified (and encrypted) just now, are released in this pass too:
            # the flow reaches its end in one run and one message (1.306) - before, the delete at the
            # source waited for the next pass and came as a message of its own.
            releasable = refresh_release()
        if entry.offload.encrypt or releasable or expirable:
            # Read whether or not this entry encrypts now: one that did and was switched off still has
            # .age copies, and a plain release or delete would take them for missing plain files.
            from dbabrain.backup_restore.repository_encryption import is_encrypted, sealed_rows
            from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore

            enc_store = RepositoryEncryptionStore.from_config(app_config)
            encrypted = is_encrypted(enc_store, backup_id=entry.backup_id)
            sealed = sealed_rows(enc_store, backup_id=entry.backup_id)
        if releasable:
            result["release"] = release_sources(entry, catalog=catalog, hosts=hosts, rows=releasable,
                                                encrypted=encrypted, sealed=sealed)
        if expirable:
            result["expire"] = prune_repository(entry, catalog=catalog, hosts=hosts, encrypted=encrypted,
                                                sealed=sealed)
        busy = offloads_running_beside(store, entry) if entry.offload.repository_check else []
        if busy:
            # Another entry of this server is copying or encrypting into the same folder right now: its
            # partial and not-yet-recorded files would read as strays (2026-11-11, a LOG pass beside its
            # server's FULL failed on them). Checked by the next pass, never judged mid-copy.
            result["repository_check"] = {"clean": True, "skipped": True,
                                          "message": f"repository folder not checked this pass - {', '.join(busy)} "
                                                     "of this server is offloading into it now"}
        elif entry.offload.repository_check:
            # Last, after this pass's own copies, encryptions and expiries: what the folder holds now.
            result["repository_check"] = check_repository(entry, catalog=catalog, app_config=app_config,
                                                          hosts=hosts)
    except Exception as exc:  # noqa: BLE001 - one entry's failure is its own run's, never the pass's
        error = f"{type(exc).__name__}: {exc}"[:2000]
    copied = result.get("copy") or {}
    released = result.get("release") or {}
    expired = result.get("expire") or {}
    encrypted_now = result.get("encrypt") or {}
    checked = result.get("repository_check") or {}
    problems = (list(copied.get("failed") or []) + list(released.get("refused") or [])
                + list(expired.get("refused") or []) + list(encrypted_now.get("failed") or [])
                + ([checked["message"]] if checked and not checked.get("clean") else []))
    status = "done" if not error and not problems else "error"
    message = (f"Offload {entry.backup_id} -> {entry.offload.server_id}: {status}"
               + (f" - {copied.get('verified', 0)}/{copied.get('files', 0)} file(s) copied and verified" if copied else "")
               + (f", {encrypted_now.get('encrypted', 0)}/{encrypted_now.get('rows', 0)} encrypted on the "
                  f"repository (key {encrypted_now.get('key_id')})" if encrypted_now.get("rows") else "")
               + (f", {released.get('deleted', 0)} released on {entry.server_id}" if released else "")
               + (f", {expired.get('expired', 0)} expired on the repository" if expired else "")
               + (f", {checked['message'].split(' - ')[0]} (another offload of this server is running)"
                  if checked.get("skipped") else
                  f", repository folder checked ({checked.get('files', 0)} file(s))" if checked.get("clean") else "")
               + (f" - {error or '; '.join(problems[:3])}"[:600] if status != "done" else ""))
    finished_at = schedule.finish_run(
        store=store, log_id=log_id, status=status, message=message,
        duration_ms=int((time.monotonic() - started) * 1000), error_text=error or "; ".join(problems)[:2000] or None,
        metadata={**metadata, **result})
    emit_backup_restore_event(
        app_config=app_config, command="offload", phase="END" if status == "done" else "ERROR",
        level="logging" if status == "done" else "error", message=message, logger=logger,
        started_at=started_at, finished_at=finished_at, error_text=error or None,
        metadata={**metadata, **result, **offload_flow_lines(entry, result, error=error, status=status,
                                                             key_until=_key_until(app_config, result))},
        notify=entry.first.notify)
    summary["succeeded" if status == "done" else "failed"] += 1
    return {"backup_id": entry.backup_id, "status": status, **result, **({"error": error} if error else {})}
