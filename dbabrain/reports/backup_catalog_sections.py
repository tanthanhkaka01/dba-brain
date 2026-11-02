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


def build_files(backup_rows: list[dict], restore_rows: list[dict], *, server_id: str) -> dict:
    """``backup_rows``: ``BackupCatalogStore.server_backup_files``; ``restore_rows``:
    ``server_restore_files`` - both newest first already."""
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
        })

    files.sort(key=lambda f: str(f.get("at") or ""), reverse=True)
    return summarize(files, total=len(backup_rows) + len(restore_rows))


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
        "missing": sum(1 for f in files if f["onDisk"] is False),
        "used": sum(1 for f in files if f["usedCount"]),
        "restoredOk": sum(1 for f in files if f["restoredOk"]),
        "notProven": sum(1 for f in files if f["lastRestore"] and not f["lastRestore"]["proven"]),
        "infected": sum(1 for f in files if f["virus"] and f["virus"]["status"] == "infected"),
    }


def build_fleet(per_server: dict[str, tuple[list[dict], list[dict]]]) -> dict:
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
        table = build_files(backup_rows, restore_rows, server_id=server_id)
        total += table["total"]
        files += [{**row, "host": server_id} for row in table["files"] if row["kind"] != "restored_here"]
    files.sort(key=lambda f: str(f.get("at") or ""), reverse=True)
    return summarize(files, total=total)

