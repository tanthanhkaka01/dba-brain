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


def build_drills(runs: list[dict], *, now) -> list[dict]:
    """One row per restore entry from its runs (newest first): its tier, last run, last success, the
    7- and 30-day success rates, and its state against the tier's deadline (``lib.drill_tiers``)."""
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
                                        "offloaded": 0, "newest_full": None, "last_restored": None})
        level = integrity.LEVEL_OF_FILE_TYPE.get(str(row.get("file_type") or "").upper())
        if level:
            item["levels"].add(level)
        item["files"] += int(row.get("files") or 0)
        item["offloaded"] += int(row.get("offloaded") or 0)
        if level == "full":
            item["newest_full"] = max(filter(None, [item["newest_full"], row.get("newest")]), default=None)
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
