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
* a base backup (a directory) is never deleted here - the engine script's chain retention owns it;
* the delete is ``common.cli delete-files``, fenced to the entry's backup folder, and the row says
  ``pruned`` with ``source_deleted_at`` - which the store accepts only for a ``verified`` copy.

**The repository keeps each copy by its file type** (``offload_retention_full`` / ``_diff`` /
``_log``, seconds, 0 for good - the operator, 2026-10-31: *each file type's retention in the
repository comes from config*). See :func:`expiry_plan`: a copy expires by its own level's age, never
inside the newest chain of its database on the repository, a full never while a copy that depends
on it is kept, a certificate never.

**Every value the pass runs by is the entry's** (``offload_files_per_pass``, the pauses, the
timeouts, ``offload_stall_seconds``) - nothing here is a number of its own.

**Not built yet, refused by name rather than ignored:** ``offload_scan: clamav`` (1.242 - the
scanner, decision V-3), and a repository that is not an SSH host.
"""

from __future__ import annotations

import posixpath
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
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.db.store import DbaBrainStore, RunAlreadyClaimed, utc_now_text
from dbabrain.lib import errors
from dbabrain.lib import integrity as I
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
    """Where a file's copy lives on the repository: ``<folder>/<server_id>/<path under backup_dir>`` -
    the server's own layout, so a restore from the repository reads what it would read on the server.
    A file outside the backup folder (a WAL archive elsewhere) goes under its database or type."""
    relative = _relative(str(row.get("file_path") or ""), entry.backup_dir)
    if not relative:
        relative = f"{row.get('database_name') or row.get('file_type') or 'files'}/{row.get('file_name')}"
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
        self.copy_source: dict[str, Any] = (
            {"source_share": {k: v for k, v in self.share.items() if k != "subpath"}} if self.share
            else {"source": {k: self.source_host[k] for k in ("host", "port", "username", "password", "key_file")}})
        self.target = {k: self.repository_host[k] for k in ("host", "port", "username", "password", "key_file")}
        self.copy_errors: dict[str, str] = {}

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

    def copy_round(self, pairs: list[tuple[str, str]]) -> None:
        """One ``copy-files`` for a round; each file's own failure is kept for its row."""
        timeout = int(self.entry.offload.copy_timeout_seconds)
        answer = common_cli.run("copy-files", {
            **self.copy_source, "target": self.target, "timeout_seconds": timeout,
            "stall_seconds": int(self.entry.offload.stall_seconds),
            "files": [{"source": source, "target": target} for source, target in pairs]},
            timeout_seconds=timeout + _PROCESS_MARGIN_SECONDS, stream_stderr=True)
        for line in answer.get("files") or []:
            if not line.get("ok"):
                self.copy_errors[str(line.get("target"))] = str(line.get("error") or "not copied")


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
        if hosts.share:
            source_path = str(row.get("file_path") or "")      # hashed on the Windows host itself
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
                  sleep=time.sleep) -> dict[str, Any]:
    """Copy and prove ``rows`` onto the repository; mark each row; answer the counts."""
    items, of_row, copy_source = _items(entry, rows, hosts)
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
    pending: list[tuple[str, str]] = []
    repository_hash = hosts.hasher(hosts.repository_host)

    def copy(item: copy_gate.FileToCopy) -> int:
        pending.append((copy_source[item.target_path], item.target_path))
        return 0

    def hash_targets(paths: list[str]) -> dict[str, tuple[str, int | None]]:
        if pending:
            # The round's copies travel together, then the round is hashed (as restore_script does).
            round_files, pending[:] = list(pending), []
            try:
                hosts.copy_round(round_files)
            except Exception as exc:  # noqa: BLE001 - a round that failed is an attempt spent, per file
                for _source, target in round_files:
                    hosts.copy_errors[target] = f"{type(exc).__name__}: {exc}"[:1000]
        return repository_hash(paths)

    try:
        outcomes = copy_gate.copy_verified_batch(
            items, integrity=integrity, hash_sources=hosts.hasher(hosts.source_host), hash_targets=hash_targets,
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
    return {"offload_run_id": run_id, "files": len(rows), "verified": verified, "failed": failed,
            "source_missing": missing, "recopied": summary["recopied"]}


def release_sources(entry: _Entry, *, catalog: BackupCatalogStore, hosts: _Hosts,
                    rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Delete on the server the files whose repository copy is verified - after proving it again, and
    never the newest chain of a database (1.246)."""
    newest_full: dict[str, str] = {}
    for row in catalog.newest_full_per_database(backup_id=entry.backup_id):
        newest_full[str(row["database_name"] or "")] = str(row["at"] or "")
    keep, candidates = [], []
    for row in rows:
        at = str(row.get("backup_ended_at") or row.get("created_at") or "")
        floor = newest_full.get(str(row.get("database_name") or ""))
        if I.HASHED_MEMBER.get(str(row["file_type"]).upper()):
            keep.append(f"{row['file_name']}: a base backup directory - the script's chain retention owns it")
        elif not floor or at >= floor:
            keep.append(f"{row['file_name']}: in the newest chain of {row.get('database_name') or 'its database'}")
        else:
            candidates.append(row)
    if not candidates:
        return {"deleted": 0, "kept": len(keep), "refused": []}
    # Proven again, now: the copy a delete relies on is the copy as it is at this moment.
    found = hosts.hasher(hosts.repository_host)([str(r["offload_path"]) for r in candidates])
    refused, to_delete = [], []
    for row in candidates:
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
    by_root: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in to_delete:
        by_root[_source_root(row, entry)].append(row)
    for root, group in by_root.items():
        path_of = {str(r["file_path"] if hosts.share else (r.get("host_path") or r["file_path"])): r for r in group}
        try:
            answer = common_cli.run("delete-files", {"host": hosts.source_host, "paths": sorted(path_of),
                                                     "must_be_under": root})
        except Exception as exc:  # noqa: BLE001 - one folder refused is that folder's, the rest go on
            refused += [f"{r['file_name']}: delete refused - {type(exc).__name__}: {exc}"[:500] for r in group]
            continue
        for line in answer.get("files") or []:
            row = path_of.get(str(line.get("path")))
            if row is None:
                continue
            if line.get("status") in ("deleted", "not_found"):
                catalog.set_backup_file(int(row["file_id"]), state="pruned", on_disk=0,
                                        source_deleted_at=utc_now_text())
                deleted += 1
            else:
                refused.append(f"{row['file_name']}: delete {line.get('status')} - {line.get('reason') or ''}")
    return {"deleted": deleted, "kept": len(keep), "refused": refused}


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
# The repository's retention (offload_retention_full / _diff / _log)
# --------------------------------------------------------------------------- #
def _at(row: dict[str, Any]) -> str:
    return str(row.get("backup_ended_at") or row.get("created_at") or "")


def expiry_plan(rows: list[dict[str, Any]], offload: Any, *, now: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """Which repository copies have outlived their retention: ``(expire, kept_reasons)``.

    ``rows`` are an entry's verified copies, each with its ``chain_start`` (for a full: the start of
    the run that wrote it). The rules, per database:

    1. **By its own level's age.** ``offload.retention_of(file_type)`` seconds after the backup ended;
       0 keeps that level for good.
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
        keep_for = int(offload.retention_of(str(row["file_type"])))
        if keep_for <= 0 or not _at(row):
            return False
        ended = datetime.strptime(_at(row)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return (now - ended).total_seconds() > keep_for

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
                     now: datetime | None = None) -> dict[str, Any]:
    """Delete on the repository the copies :func:`expiry_plan` names, and mark them ``expired``."""
    now = now or datetime.now(timezone.utc)
    expire, kept = expiry_plan(catalog.repository_copies(backup_id=entry.backup_id), entry.offload, now=now)
    if not expire:
        return {"expired": 0, "kept": kept, "refused": []}
    fence = posixpath.join(entry.offload.folder.rstrip("/"), entry.server_id)
    paths: dict[str, dict[str, Any]] = {}
    refused: list[str] = []
    for row in expire:
        where = str(row["offload_path"])
        if I.HASHED_MEMBER.get(str(row["file_type"]).upper()):
            # A base backup is a folder on the repository too; delete-files takes files, so its files.
            try:
                listing = _list_folder(hosts, hosts.repository_host, where)
            except Exception as exc:  # noqa: BLE001 - this copy's own failure
                refused.append(f"{row['file_name']}: {type(exc).__name__}: {exc}"[:500])
                continue
            members = [str(f["path"]) for f in listing.get("files") or []]
            for member in members:
                paths[member] = row
            if not members:
                paths.setdefault(f"{where}/", row)    # nothing left inside: the folder is done with
        else:
            paths[where] = row
    # A copy is expired when every one of its files is gone - deleted now, or already not there.
    gone: dict[int, bool] = {int(row["file_id"]): True for path, row in paths.items() if path.endswith("/")}
    files = sorted(p for p in paths if not p.endswith("/"))
    if files:
        try:
            answer = common_cli.run("delete-files", {"host": hosts.repository_host, "paths": files,
                                                     "must_be_under": fence})
        except Exception as exc:  # noqa: BLE001 - nothing was deleted; the next pass tries again
            return {"expired": 0, "kept": kept,
                    "refused": refused + [f"delete refused on the repository - {type(exc).__name__}: {exc}"[:500]]}
        answered = {str(line.get("path")): line for line in answer.get("files") or []}
        for path in files:
            row, line = paths[path], answered.get(path) or {"status": "not answered"}
            ok = line.get("status") in ("deleted", "not_found")
            gone[int(row["file_id"])] = gone.get(int(row["file_id"]), True) and ok
            if not ok:
                refused.append(f"{row['file_name']}: delete {line.get('status')} - {line.get('reason') or ''}")
    expired = 0
    for file_id, ok in gone.items():
        if ok:
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
    for entry in entries:
        retry_before = (datetime.now(timezone.utc) - timedelta(seconds=entry.retry_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        per_pass = int(limit or entry.offload.files_per_pass)
        rows = catalog.files_to_offload(backup_id=entry.backup_id, limit=per_pass, retry_before=retry_before)
        releasable = (catalog.files_to_release(backup_id=entry.backup_id, limit=per_pass)
                      if entry.offload.delete_source else [])
        expirable = _expirable(entry, catalog)
        if not rows and not releasable and not expirable:
            continue
        if dry_run:
            summary["jobs"].append({"backup_id": entry.backup_id, "status": "dry-run", "to_copy": len(rows),
                                    "to_release": len(releasable), "to_expire": len(expirable)})
            continue
        summary["jobs"].append(_run_one(entry, rows=rows, releasable=releasable, expirable=expirable,
                                        store=store, catalog=catalog,
                                        app_config=app_config, logger=logger, data_dir=data_dir, key=key,
                                        key_base64=key_base64, sleep=sleep, summary=summary))
    return summary


def _expirable(entry: _Entry, catalog: BackupCatalogStore) -> list[dict[str, Any]]:
    """The copies the repository's retention would remove now - none when every level keeps for good,
    without reading the catalog: an entry that never prunes costs nothing here."""
    offload = entry.offload
    kept_for = [value for value in (offload.retention_full, offload.retention_diff, offload.retention_log) if value]
    if not kept_for:
        return []
    now = datetime.now(timezone.utc)
    # One cheap question a second before the whole plan: is any copy older than the shortest
    # retention at all? Most passes answer no, and read nothing more.
    before = (now - timedelta(seconds=min(kept_for))).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not catalog.has_repository_copy_before(backup_id=entry.backup_id, before=before):
        return []
    expire, _kept = expiry_plan(catalog.repository_copies(backup_id=entry.backup_id), offload, now=now)
    return expire


def _run_one(entry: _Entry, *, rows, releasable, expirable=(), store, catalog, app_config, logger, data_dir, key,
             key_base64, sleep, summary) -> dict[str, Any]:
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
        if source.platform == "windows" and not entry.offload.source_share:
            raise OffloadRefused(f"{entry.backup_id}: {entry.server_id} is a Windows server, read over its share - "
                                 "state offload_source_share, _username and _password_ref.")
        secrets = _load_secrets(data_dir=data_dir, key=key, key_base64=key_base64)
        hosts = _Hosts(entry, source=source, repository=repository, secrets=secrets, data_dir=data_dir)
        if rows:
            result["copy"] = offload_entry(entry, catalog=catalog, hosts=hosts, rows=rows, sleep=sleep)
        if releasable:
            result["release"] = release_sources(entry, catalog=catalog, hosts=hosts, rows=releasable)
        if expirable:
            result["expire"] = prune_repository(entry, catalog=catalog, hosts=hosts)
    except Exception as exc:  # noqa: BLE001 - one entry's failure is its own run's, never the pass's
        error = f"{type(exc).__name__}: {exc}"[:2000]
    copied = result.get("copy") or {}
    released = result.get("release") or {}
    expired = result.get("expire") or {}
    problems = (list(copied.get("failed") or []) + list(released.get("refused") or [])
                + list(expired.get("refused") or []))
    status = "done" if not error and not problems else "error"
    message = (f"Offload {entry.backup_id} -> {entry.offload.server_id}: {status}"
               + (f" - {copied.get('verified', 0)}/{copied.get('files', 0)} file(s) copied and verified" if copied else "")
               + (f", {released.get('deleted', 0)} released on {entry.server_id}" if released else "")
               + (f", {expired.get('expired', 0)} expired on the repository" if expired else "")
               + (f" - {error or '; '.join(problems[:3])}"[:600] if status != "done" else ""))
    finished_at = schedule.finish_run(
        store=store, log_id=log_id, status=status, message=message,
        duration_ms=int((time.monotonic() - started) * 1000), error_text=error or "; ".join(problems)[:2000] or None,
        metadata={**metadata, **result})
    emit_backup_restore_event(
        app_config=app_config, command="offload", phase="END" if status == "done" else "ERROR",
        level="logging" if status == "done" else "error", message=message, logger=logger,
        started_at=started_at, finished_at=finished_at, error_text=error or None,
        metadata={**metadata, **result}, notify=entry.first.notify)
    summary["succeeded" if status == "done" else "failed"] += 1
    return {"backup_id": entry.backup_id, "status": status, **result, **({"error": error} if error else {})}
