"""The server page's *Backup & restore files* section, from the backup catalog (0.34.0, 1.217).

The operator, 2026-10-29: *on the server page, a report of backup and restore - one report, not two -
with every file's information, newest first, filterable by file type, with the size in bytes, the
checksum, the verification, the virus scan; per server_id; a field saying whether the file has been
restored successfully, and the files restore-tested OK.*

One row per file:

* ``backup`` - a file a backup of this server wrote, with its latest restore or copy beside it (where
  it went, whether it arrived intact, whether the restore that used it passed) and how many restores
  proved it;
* ``restored_here`` - a file a restore or a copy carried ONTO this server from another one;
* ``restore_out`` - a file carried from this server that no catalogued backup matches (a copy made
  before its backup was catalogued): kept, so no restore of this server is missing from its page.

Every figure is one the backup or the copy recorded, never recomputed here. A file is *restored OK*
by a proven copy of it (``match`` / ``copied`` / ``recopied``) in a ``restore`` run that ended
``done`` - copied intact AND restored AND the databases verified. A copy-only run proves the copy,
never a restore.
"""

from __future__ import annotations

from typing import Any

from dbabrain.lib import integrity

#: The rows read per server, each kind. The newest are the question; a server with years of 15-minute
#: logs would otherwise put tens of thousands of rows into its page.
ROW_LIMIT = 500

KINDS = ("backup", "restored_here", "restore_out")


def _flag(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _virus(row: dict) -> dict | None:
    if not row.get("av_scan_status"):
        return None
    return {"status": row.get("av_scan_status"), "at": row.get("av_scanned_at"),
            "tool": row.get("av_scan_tool"), "version": row.get("av_scan_tool_version"),
            "signatures": row.get("av_scan_signature_version"), "by": row.get("av_scan_server_id"),
            "text": row.get("av_scan_result_text")}


def _restored_ok(row: dict) -> bool:
    return (row.get("verdict") in integrity.PROVEN_VERDICTS and row.get("action") == "restore"
            and row.get("run_phase") == "done")


def _restore(row: dict) -> dict:
    """One restore or copy of a file, as the row carries it."""
    return {
        "at": row.get("run_started_at"),
        "action": row.get("action"),
        "restoreId": row.get("restore_id") or "",
        "phase": row.get("run_phase"),
        "pointInTime": row.get("point_in_time"),
        "source": row.get("source_server_id"),
        "target": row.get("target_server_id"),
        "verdict": row.get("verdict"),
        "proven": row.get("verdict") in integrity.PROVEN_VERDICTS,
        "attempts": row.get("copy_attempts"),
        "maxRetry": row.get("copy_max_retry"),
        "sha256SourceBefore": row.get("checksum_source_before_copy"),
        "sha256TargetAfter": row.get("checksum_target_after_copy"),
        "sourceOnDisk": _flag(row.get("source_on_disk")),
        "sourceMatches": _flag(row.get("source_checksum_matches")),
        "targetOnDisk": _flag(row.get("target_on_disk")),
        "targetMatches": _flag(row.get("target_checksum_matches")),
        "sourcePath": row.get("source_host_path") or row.get("source_path") or "",
        "targetPath": row.get("target_host_path") or row.get("target_path") or "",
        "testedOk": _restored_ok(row),
        "refusedReason": row.get("refused_reason") or "",
    }


def _base(path: str) -> str:
    return str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def load_repository(catalog: Any, backup_rows: list[dict]) -> dict[int, dict]:
    """What the repository holds of each backup row that went there (0.37.0, the operator 2026-11-11:
    *encrypted or not, with which key, until when; the checksum and bytes before and after encryption;
    the three files of one FULL shown as one backup*): ``{file_id: repository facts}``, read in a few
    queries for the whole page. A row no offload took has none."""
    from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore

    ids = [int(r["file_id"]) for r in backup_rows if r.get("offload_state") or r.get("offload_path")]
    if not ids:
        return {}
    members = catalog.offload_members_of(ids)
    store = RepositoryEncryptionStore(catalog.target)
    sealed = store.files_of_many(ids)
    keys = {int(k["key_id"]): k for k in store.keys()}
    by_id = {int(r["file_id"]): r for r in backup_rows}
    return {fid: repository_facts(by_id[fid], members.get(fid, []), sealed.get(fid, []), keys) for fid in ids}


def repository_facts(row: dict, members: list[dict], sealed: list[dict], keys: dict[int, dict]) -> dict:
    """One backup's repository copy, file by file: on the server (bytes, sha256 before the copy) ->
    on the repository (bytes, sha256 after) -> encrypted (the .age file's bytes and sha256, its key and
    the moment that key stops encrypting, proven, the plain file removed there)."""
    by_plain = {str(f.get("plain_path") or ""): f for f in sealed}
    files = []
    for m in members:
        target = str(m.get("target_path") or "")
        age = by_plain.pop(target, None)
        key = keys.get(int(age["key_id"])) if age else None
        files.append({
            "name": _base(target or m.get("source_path") or ""),
            "serverPath": m.get("source_host_path") or m.get("source_path") or "",
            "bytes": m.get("size_source_bytes"), "sha256": m.get("checksum_source_before_copy"),
            "repoPath": target, "repoBytes": m.get("size_target_bytes") if m.get("size_target_bytes") is not None
            else m.get("size_source_bytes"),
            "repoSha256": m.get("checksum_target_after_copy"),
            # The copy's own start and end (its attempts'); null for a copy made before they were read
            # here - never the run's end, which comes after the verify.
            "copyStartedAt": m.get("copy_started_at"), "copiedAt": m.get("copy_ended_at"),
            "age": _age(age, key),
        })
    # An encrypted file whose copy row is not among the newest proven ones: shown, never dropped.
    for age in by_plain.values():
        key = keys.get(int(age["key_id"]))
        files.append({"name": _base(age.get("plain_path")), "serverPath": "", "bytes": age.get("plain_size_bytes"),
                      "sha256": age.get("plain_sha256"), "repoPath": age.get("plain_path"),
                      "repoBytes": age.get("plain_size_bytes"), "repoSha256": age.get("plain_sha256"),
                      "copyStartedAt": None, "copiedAt": None, "age": _age(age, key)})
    proven = [f for f in files if f["age"] and f["age"]["provenAt"]]
    return {
        "server": row.get("offload_server_id") or "", "state": row.get("offload_state") or "",
        "path": row.get("offload_path") or "", "verifiedAt": row.get("offload_verified_at"),
        "expiredAt": row.get("offload_expired_at"), "error": row.get("offload_error") or "",
        "files": files, "encrypted": len(proven), "total": len(files),
        # The moments of the copy's life, for the row's timeline (1.345): when the first file's copy
        # began, and - once EVERY file of the backup has reached it - when the last one was copied,
        # encrypted, proven, and had no plain file left there. Null while one file has not.
        "copyStartedAt": min((f["copyStartedAt"] for f in files if f["copyStartedAt"]), default=None),
        "copiedAt": _when_all(f["copiedAt"] for f in files),
        "encryptedAt": _when_all((f["age"] or {}).get("encryptedAt") for f in files),
        "provenAt": _when_all((f["age"] or {}).get("provenAt") for f in files),
        "plainGoneAt": _when_all((f["age"] or {}).get("plainRemovedAt") for f in files),
        "keys": sorted({f["age"]["keyId"] for f in files if f["age"]}),
        "keyUntil": min((f["age"]["keyUntil"] for f in files if f["age"] and f["age"]["keyUntil"]), default=None),
    }


def _when_all(moments) -> str | None:
    """The latest of ``moments`` when every one is there - the instant the last file got there; else
    ``None``: a backup is copied, encrypted or proven only once all its files are."""
    values = [str(m) if m else "" for m in moments]
    return max(values) if values and all(values) else None


def _age(age: dict | None, key: dict | None) -> dict | None:
    if not age:
        return None
    return {"path": age.get("encrypted_path"), "bytes": age.get("encrypted_size_bytes"),
            "sha256": age.get("encrypted_sha256"), "keyId": int(age["key_id"]),
            "keyUntil": (key or {}).get("encrypt_until"), "algorithm": age.get("algorithm") or "",
            "encryptedAt": age.get("encrypted_at"), "provenAt": age.get("proven_at"),
            "plainRemovedAt": age.get("plain_deleted_at")}


def _on_server(row: dict) -> dict:
    """Where the backup stands on its own server: there, deleted after its repository copy was
    verified (when), only the manifest the next incremental reads, or gone without a release."""
    from dbabrain.db.backup_catalog import MANIFEST_KEPT

    if row.get("source_deleted_at"):
        return {"state": "released", "at": row.get("source_deleted_at")}
    if row.get("pruned_by") == MANIFEST_KEPT:
        return {"state": "manifest_only", "at": None}
    if _flag(row.get("on_disk")) is False:
        return {"state": "pruned" if row.get("state") == "pruned" else "missing", "at": None,
                "by": row.get("pruned_by") or ""}
    return {"state": "present", "at": None}


# --------------------------------------------------------------------------- #
# A file's restore chain (0.38.0, 1.345)
# --------------------------------------------------------------------------- #
# The operator, 2026-11-12, reading one LOG file's row (translated): *and if the file is a diff or a
# log, can the full, diff, log chain be had?* A log alone restores nothing: what a restore to its end
# reads is the newest full before it, the differential or incrementals after that full, and every
# log since. The catalog holds each of them with its moment, so the page can name them and say where
# each is - on the repository, on the server, or nowhere.
#
# By time, not by the row's recorded link (`chain_parent_file_id`): that link was written as "the
# newest full still on the server", and a full released to the repository is no longer that - on the
# estate every log after a release pointed at an older full, or at none (see `chain_rows`).
def _level(row: dict) -> str:
    return integrity.LEVEL_OF_FILE_TYPE.get(str(row.get("file_type") or "").upper(), "")


def _moment(row: dict) -> tuple[str, int]:
    """When a file ended and its id - the order of a chain, the id breaking a tie."""
    return str(row.get("ended_at") or row.get("backup_ended_at") or row.get("created_at") or ""), int(row["file_id"])


def _of(row: dict) -> tuple[str, str]:
    return str(row.get("server_id") or ""), str(row.get("database_name") or "")


def _base_full(row: dict, fulls: list[dict]) -> dict | None:
    """The newest of ``fulls`` (one database's, oldest first) that ended before ``row`` did."""
    at = _moment(row)
    earlier = [f for f in fulls if _moment(f) < at]
    return earlier[-1] if earlier else None


def load_chains(catalog: Any, backup_rows: list[dict]) -> dict[int, dict]:
    """``{file_id: chain}`` for the diff and log rows a page shows (:func:`build_chains`): per server,
    its fulls, then every diff and log since the oldest full one of those rows builds on."""
    wanted = [r for r in backup_rows[:ROW_LIMIT] if _level(r) in ("diff", "log")]
    chains: dict[int, dict] = {}
    for server_id in sorted({_of(r)[0] for r in wanted}):
        rows = [r for r in wanted if _of(r)[0] == server_id]
        fulls = catalog.chain_rows(server_id, levels=("full",))
        by_db: dict[tuple[str, str], list[dict]] = {}
        for full in fulls:
            by_db.setdefault(_of(full), []).append(full)
        bases = [base for base in (_base_full(r, by_db.get(_of(r), [])) for r in rows) if base]
        rest = (catalog.chain_rows(server_id, levels=("diff", "log"), since=min(_moment(b)[0] for b in bases))
                if bases else [])
        chains.update(build_chains(rows, [*fulls, *rest]))
    return chains


def _link(row: dict) -> dict:
    return {"type": row.get("file_type"), "name": row.get("file_name"), "at": _moment(row)[0],
            "repo": row.get("offload_state") or "", "repoServer": row.get("offload_server_id") or "",
            "onServer": _on_server(row)["state"], "deletedAt": row.get("source_deleted_at")}


def _can_be_had(row: dict) -> bool:
    """A restore can read the file: its repository copy is verified, or it is still on its server."""
    return row.get("offload_state") == "verified" or _on_server(row)["state"] == "present"


def build_chains(backup_rows: list[dict], chain_rows: list[dict]) -> dict[int, dict]:
    """For each diff or log among ``backup_rows``: the files a restore to its end reads, and whether
    every one can be had. ``chain_rows``: ``BackupCatalogStore.chain_rows`` - the fulls, diffs and
    logs of the same servers, oldest first.

    The full is the newest of the same database on the same server that ended before the file. A SQL
    Server differential holds everything since its full, so only the newest one after that full
    counts; an incremental of another engine builds on the one before it, so every one since the full
    does. Logs: every one after the last of those (or the full) up to the file itself. By the
    catalog's order of ending - the engine's own check of the chain (LSNs, WAL names) is the
    restore's, before it restores anything."""
    groups: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for row in chain_rows:
        if _level(row) in ("full", "diff", "log"):
            groups.setdefault(_of(row), {"full": [], "diff": [], "log": []})[_level(row)].append(row)
    for group in groups.values():
        for rows in group.values():
            rows.sort(key=_moment)

    chains: dict[int, dict] = {}
    for row in backup_rows:
        level = _level(row)
        if level not in ("diff", "log"):
            continue
        file_id, group = int(row["file_id"]), groups.get(_of(row), {"full": [], "diff": [], "log": []})
        full = _base_full(row, group["full"])
        if full is None:
            chains[file_id] = {"full": None, "diffs": [], "logs": None, "complete": False, "onRepository": False,
                               "missing": [], "missingCount": 0, "selfGone": not _can_be_had(row),
                               "note": "the catalog holds no full backup of this database taken before this file"}
            continue
        at, since = _moment(row), _moment(full)
        cumulative = str(row.get("engine") or full.get("engine") or "") == "sqlserver"
        before = [d for d in group["diff"] if since < _moment(d) < at]
        if level == "diff":
            diffs, logs = ([] if cumulative else before) + [row], []
        else:
            diffs = before[-1:] if cumulative else before
            base = _moment(diffs[-1]) if diffs else since
            logs = [entry for entry in group["log"] if base < _moment(entry) <= at]
            if not any(int(entry["file_id"]) == file_id for entry in logs):
                logs.append(row)
        needed = [full, *diffs, *logs]
        gone = [r for r in needed if not _can_be_had(r)]
        chains[file_id] = {
            "full": _link(full), "diffs": [_link(d) for d in diffs],
            "logs": ({"count": len(logs), "from": _moment(logs[0])[0], "to": _moment(logs[-1])[0],
                      "onRepository": sum(1 for r in logs if r.get("offload_state") == "verified"),
                      "onServer": sum(1 for r in logs if _on_server(r)["state"] == "present")} if logs else None),
            "complete": not gone,
            "onRepository": all(r.get("offload_state") == "verified" for r in needed),
            "missing": [str(r.get("file_name")) for r in gone[:5]], "missingCount": len(gone), "note": "",
            # The file itself is nowhere any more - a chain its retention swept, which is history, not
            # an alarm. The alarm is a file still kept whose chain lacks a piece.
            "selfGone": not _can_be_had(row),
        }
    return chains


def build_files(backup_rows: list[dict], restore_rows: list[dict], *, server_id: str,
                repository: dict[int, dict] | None = None, chains: dict[int, dict] | None = None) -> dict:
    """``backup_rows``: ``BackupCatalogStore.server_backup_files``; ``restore_rows``:
    ``server_restore_files`` - both newest first already; ``repository``: :func:`load_repository`;
    ``chains``: :func:`load_chains`."""
    repository = repository or {}
    chains = chains or {}
    by_id: dict[int, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for row in restore_rows:
        if row.get("source_server_id") != server_id:
            continue
        if row.get("file_id") is not None:
            by_id.setdefault(int(row["file_id"]), []).append(row)
        else:
            by_name.setdefault(str(row.get("file_name") or ""), []).append(row)

    files: list[dict] = []
    attached: set[int] = set()
    for row in backup_rows[:ROW_LIMIT]:
        restores = by_id.get(int(row["file_id"]), []) + by_name.get(str(row.get("file_name") or ""), [])
        restores.sort(key=lambda r: str(r.get("run_started_at") or ""), reverse=True)
        attached.update(int(r["run_file_id"]) for r in restores)
        files.append({
            "kind": "backup",
            "at": row.get("backup_ended_at") or row.get("created_at"),
            # When the engine began writing it - the first moment of the row's timeline (1.345).
            "startedAt": row.get("backup_started_at"),
            "server": row.get("server_id"),
            "database": row.get("database_name") or "",
            "type": row.get("file_type"),
            "name": row.get("file_name"),
            "sizeBytes": row.get("size_bytes"),
            "sha256": row.get("checksum_after_backup"),
            "engineVerify": row.get("engine_verify") or "not_run",
            "engineVerifyText": row.get("engine_verify_text") or "",
            "verdict": row.get("verdict"),
            # What the backup script wrote it with; null for a file it did not say about (written
            # before 0.34.0, or catalogued by catalog-backfill) - shown as such, never as "no".
            "encrypted": _flag(row.get("encrypted")),
            "onDisk": _flag(row.get("on_disk")),
            "checksumMatches": _flag(row.get("checksum_matches")),
            "lastChecked": row.get("last_checked_at"),
            "virus": _virus(row),
            "path": row.get("host_path") or row.get("file_path") or "",
            "container": row.get("container_name") or "",
            # Counted by the store over every restore, not only the rows read here.
            "usedCount": int(row.get("used_count") or 0),
            "restoredOk": int(row.get("restored_ok_count") or 0),
            # The file's own mark, written by every restore that used it: when, onto which target,
            # the run's result, this file's copy verdict.
            "mark": ({"at": row.get("last_restore_at"), "action": row.get("last_restore_action"),
                      "target": row.get("last_restore_target_server_id"),
                      "result": row.get("last_restore_result"), "verdict": row.get("last_restore_verdict")}
                     if row.get("last_restore_run_id") is not None else None),
            "lastRestore": _restore(restores[0]) if restores else None,
            "repo": repository.get(int(row["file_id"])),
            "onServer": _on_server(row),
            # A diff's or a log's restore chain; null on a full, which is its own.
            "chain": chains.get(int(row["file_id"])),
        })

    # Restores of another server's files onto this one, and this server's files that no catalogued
    # backup matches - each its own row, so no copy of or onto this server is missing.
    for row in restore_rows[:ROW_LIMIT]:
        if int(row["run_file_id"]) in attached:
            continue
        incoming = row.get("target_server_id") == server_id and row.get("source_server_id") != server_id
        restore = _restore(row)
        files.append({
            "kind": "restored_here" if incoming else "restore_out",
            "at": row.get("run_started_at"),
            "server": row.get("source_server_id"),
            "database": row.get("database_name") or "",
            "type": row.get("file_type"),
            "name": row.get("file_name"),
            "sizeBytes": row.get("size_source_bytes") if row.get("size_source_bytes") is not None
            else row.get("size_target_bytes"),
            "sha256": row.get("checksum_after_backup") or row.get("checksum_source_before_copy"),
            "engineVerify": None, "engineVerifyText": "", "verdict": None, "encrypted": None,
            "onDisk": restore["targetOnDisk"] if incoming else restore["sourceOnDisk"],
            "checksumMatches": restore["targetMatches"] if incoming else restore["sourceMatches"],
            "lastChecked": row.get("target_checked_at") if incoming else row.get("source_checked_at"),
            "virus": _virus(row),
            "path": restore["targetPath"] if incoming else restore["sourcePath"],
            "container": "",
            "usedCount": 1,
            "restoredOk": 1 if restore["testedOk"] else 0,
            "mark": {"at": restore["at"], "action": restore["action"], "target": restore["target"],
                     "result": restore["phase"], "verdict": restore["verdict"]},
            "lastRestore": restore,
            "repo": None, "onServer": None,
        })

    files.sort(key=lambda f: str(f.get("at") or ""), reverse=True)
    return summarize(files, total=len(backup_rows) + len(restore_rows))


def _missing(f: dict) -> bool:
    state = (f.get("onServer") or {}).get("state")
    return state == "missing" if state else f.get("onDisk") is False


def summarize(files: list[dict], *, total: int) -> dict:
    """The table's answer - its rows and the counts its summary line reads. One function for a
    server's table and for the fleet's, so the two pages count alike."""
    seen = {f["type"] for f in files if f.get("type")}
    return {
        "files": files,
        "types": [t for t in integrity.FILE_TYPES if t in seen],
        "kinds": [k for k in KINDS if any(f["kind"] == k for f in files)],
        "servers": sorted({str(f["host"]) for f in files if f.get("host")}),
        "total": total, "shown": len(files),
        "notOk": sum(1 for f in files if f["kind"] == "backup" and f["verdict"] != "ok"),
        "changed": sum(1 for f in files if f["checksumMatches"] is False),
        # Gone from its server without a release or a retention prune: a deleted-as-meant file is not missing.
        "missing": sum(1 for f in files if _missing(f)),
        "used": sum(1 for f in files if f["usedCount"]),
        "restoredOk": sum(1 for f in files if f["restoredOk"]),
        "notProven": sum(1 for f in files if f["lastRestore"] and not f["lastRestore"]["proven"]),
        "infected": sum(1 for f in files if f["virus"] and f["virus"]["status"] == "infected"),
    }


def build_retention(rows: list[dict]) -> list[dict]:
    """Each backup entry's retention and point-in-time reach, as its newest run recorded them (1.233,
    T3): ``BackupCatalogStore.retention_by_entry``. The reach is the number an operator means by
    "retention" - the shorter of the base and the log window - printed rather than inferred."""
    import json

    from dbabrain.lib import cleanup_retention

    out = []
    for row in rows:
        try:
            stated = json.loads(row.get("retention_json") or "null")
        except ValueError:
            stated = row.get("retention_json")
        reach = row.get("pitr_reach_seconds")
        out.append({"backupId": row.get("backup_id"), "server": row.get("server_id"), "engine": row.get("engine"),
                    "windows": cleanup_retention.describe_config(stated),
                    "reach": "kept, no limit by age" if reach is None else cleanup_retention.describe(int(reach)),
                    "reachSeconds": reach, "since": row.get("started_at")})
    return out


def build_fleet(per_server: dict[str, tuple[list[dict], list[dict]]],
                repository: dict[int, dict] | None = None, chains: dict[int, dict] | None = None) -> dict:
    """Every server's files in one table - the fleet page, ``backup-restore.html``.

    ``per_server``: ``{server_id: (server_backup_files, server_restore_files)}``. Each server's rows
    are its own table's (:func:`build_files`), tagged with the server (``host``), keeping its backups
    and the copies of files no backup recorded. A restore onto a server from another one is left
    out here: it is already on the source's row (the file's mark and its last restore), and listing
    it twice would count one copy as two.
    """
    files: list[dict] = []
    total = 0
    for server_id, (backup_rows, restore_rows) in sorted(per_server.items()):
        table = build_files(backup_rows, restore_rows, server_id=server_id, repository=repository, chains=chains)
        total += table["total"]
        files += [{**row, "host": server_id} for row in table["files"] if row["kind"] != "restored_here"]
    files.sort(key=lambda f: str(f.get("at") or ""), reverse=True)
    return summarize(files, total=total)



# --------------------------------------------------------------------------- #
# Recoverability (0.35.0, 1.248 / 1.252 / 1.253)
# --------------------------------------------------------------------------- #
def _instant(value: Any):
    from datetime import datetime, timezone

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _drill_ok(run: dict) -> bool:
    """A drill succeeded when it ended ``done`` and no database failed its check."""
    return run.get("phase") == "done" and not int(run.get("databases_failed") or 0)


def build_drills(runs: list[dict], *, now, entries: list[dict] | None = None) -> list[dict]:
    """One row per restore entry from its runs (newest first): its tier, last run, last success, the
    7- and 30-day success rates, and its state against the tier's deadline (``lib.drill_tiers``).

    ``entries`` are the configured restore entries: an active one with a tier and no run in the
    window read is listed OVERDUE - the window is twice the longest deadline, so it is late whatever
    it did before - instead of being absent from the table (audit 2026-11-05, C3)."""
    from datetime import timedelta

    from dbabrain.lib import drill_tiers

    by_entry: dict[str, list[dict]] = {}
    for run in runs:
        by_entry.setdefault(str(run.get("restore_id") or "(no id)"), []).append(run)
    rows = []
    for restore_id, entry_runs in sorted(by_entry.items()):
        tier = next((r.get("tier") for r in entry_runs if r.get("tier")), None)
        success = next((r for r in entry_runs if _drill_ok(r)), None)
        last_success = _instant(success.get("ended_at") or success.get("started_at")) if success else None

        def rate(days: int) -> tuple[int, int]:
            window = [r for r in entry_runs if (_instant(r.get("started_at")) or now) >= now - timedelta(days=days)]
            return sum(1 for r in window if _drill_ok(r)), len(window)

        ok_7, runs_7 = rate(7)
        ok_30, runs_30 = rate(30)
        last = entry_runs[0]
        rows.append({
            "restore_id": restore_id, "tier": tier, "source": last.get("source_server_id"),
            "target": last.get("target_server_id"), "last_run": last.get("started_at"),
            "last_phase": last.get("phase"), "last_success": success.get("ended_at") if success else None,
            "ok_7": ok_7, "runs_7": runs_7, "ok_30": ok_30, "runs_30": runs_30,
            "state": drill_tiers.drill_state(tier, last_success, now),
        })
    if entries is not None:
        # A drill whose entry is gone from the config: its runs stay in the catalog, but it is no
        # deadline's any more - listed as such, never as a failing drill (2026-11-11: the store's
        # repository drill read "error" for a week after its entry was removed).
        configured = {str(e.get("restore_id") or "") for e in entries}
        for row in rows:
            if row["restore_id"] not in configured:
                row["state"] = "REMOVED"
    listed = {row["restore_id"] for row in rows}
    for entry in entries or []:
        restore_id = str(entry.get("restore_id") or "")
        tier = str(entry.get("tier") or "").lower() or None
        if not restore_id or restore_id in listed or not tier or entry.get("active") is False:
            continue
        rows.append({
            "restore_id": restore_id, "tier": tier, "source": entry.get("server_id"),
            "target": entry.get("target_server_id"), "last_run": None, "last_phase": "no run in the window",
            "last_success": None, "ok_7": 0, "runs_7": 0, "ok_30": 0, "runs_30": 0,
            "state": drill_tiers.OVERDUE if drill_tiers.deadline(tier) else drill_tiers.UNTIERED,
        })
    rows.sort(key=lambda row: row["restore_id"])
    return rows


def build_coverage(rows: list[dict]) -> list[dict]:
    """Per server and database (1.252): the levels its present backups hold, how many are copied
    off the host, the last restore that used one - and what is missing, named. Complete is: a full,
    log or WAL backups, a copy off the host, a drill. A repository and a virus scan are not counted
    until they exist (1.240 is off until V-1; 1.242)."""
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row.get("server_id") or ""), str(row.get("database_name") or ""))
        item = grouped.setdefault(key, {"server_id": key[0], "database_name": key[1],
                                        "engine": row.get("engine"), "levels": set(), "files": 0,
                                        "offloaded": 0, "newest_full": None, "newest_diff": None,
                                        "newest_log": None, "last_restored": None})
        level = integrity.LEVEL_OF_FILE_TYPE.get(str(row.get("file_type") or "").upper())
        if level:
            item["levels"].add(level)
        item["files"] += int(row.get("files") or 0)
        item["offloaded"] += int(row.get("offloaded") or 0)
        if level == "full":
            item["newest_full"] = max(filter(None, [item["newest_full"], row.get("newest")]), default=None)
        if level == "diff":
            # A differential or an incremental (1.345): the table said when the newest full and the
            # newest log were, and nothing of the level between them.
            item["newest_diff"] = max(filter(None, [item["newest_diff"], row.get("newest")]), default=None)
        if level == "log":
            item["newest_log"] = max(filter(None, [item["newest_log"], row.get("newest")]), default=None)
        item["last_restored"] = max(filter(None, [item["last_restored"], row.get("last_restored")]), default=None)
    out = []
    for item in grouped.values():
        missing = []
        if "full" not in item["levels"]:
            missing.append("a full backup")
        if "log" not in item["levels"]:
            missing.append("log / WAL backups")
        if not item["offloaded"]:
            missing.append("a copy off the host")
        if not item["last_restored"]:
            missing.append("a restore drill")
        out.append({**item, "levels": sorted(item["levels"]), "missing": missing,
                    "status": "complete" if not missing else "incomplete"})
    return sorted(out, key=lambda r: (r["status"] == "complete", r["server_id"], r["database_name"]))
