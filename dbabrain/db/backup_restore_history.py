from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from dbabrain.db import backend as backend_mod
from dbabrain.db.backend import StoreTarget
from dbabrain.db.store import DbaBrainStore, utc_now_text


#: Bumped when backup_restore_history's shape changes, so the schema check can skip the DDL.
HISTORY_SCHEMA_VERSION = 1


def drill_rows(conn: Any) -> list[dict[str, Any]]:
    """Every drill this table holds, as :func:`dbabrain.db.restore_drill.evaluate` reads them.

    The whole history, not a window: "never drilled" and "drilled, but too long ago" are different
    verdicts, and a window would turn the second into the first. Read by ``db.cli
    restore-drill-status`` and by the SLA app's ``restore_drill`` policy - one query for both.
    """
    return [dict(row) for row in conn.execute(
        "SELECT database_name, status, restore_start, restore_end, duration_seconds "
        "FROM backup_restore_history")]


def catalog_drill_rows(conn: Any) -> list[dict[str, Any]]:
    """The drills the backup catalog recorded, in :func:`drill_rows`' shape - one row per finished
    ``restore`` run and database it restored.

    ``backup_restore_history`` is written only by the share-driven SQL Server restore; every
    script-driven drill (PostgreSQL, Oracle, the container drills) is a ``restore_runs`` row, which
    the backup & restore page reads - so the SLA said "never drilled" for drills the page showed OK
    (audit 2026-11-05, D3). A run's databases are those of the catalogued files it restored; a run
    with none named is left out. ``[]`` where the catalog has never been created.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT rr.restore_run_id, rr.started_at, rr.ended_at, rr.phase, rr.databases_failed, "
            "bf.server_id, bf.database_name FROM restore_runs rr "
            "JOIN restore_run_files rf ON rf.restore_run_id = rr.restore_run_id "
            "JOIN backup_files bf ON bf.file_id = rf.file_id "
            "WHERE rr.action = 'restore' AND rr.phase IN ('done', 'error', 'refused') "
            "AND COALESCE(bf.database_name, '') <> ''").fetchall()
    except Exception as exc:  # noqa: BLE001 - only a catalog that does not exist yet is "no rows"
        if "restore_runs" in str(exc) or "backup_files" in str(exc) or "no such table" in str(exc).lower():
            return []
        raise
    out = []
    for row in rows:
        row = dict(row)
        ok = row["phase"] == "done" and not int(row.get("databases_failed") or 0)
        duration = None
        try:
            from datetime import datetime
            start = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(row["ended_at"]).replace("Z", "+00:00")) if row.get("ended_at") else None
            duration = (end - start).total_seconds() if end else None
        except (TypeError, ValueError):
            pass
        out.append({"database_name": row["database_name"], "server_id": row.get("server_id") or "",
                    "status": "SUCCESS" if ok else "FAILED",
                    "restore_start": row["started_at"], "restore_end": row.get("ended_at"),
                    "duration_seconds": duration})
    return out


class BackupRestoreHistory:
    def __init__(self, source, *, key: str | None = None, password: str | None = None) -> None:
        self.store = DbaBrainStore(source, key=key, password=password)

    @classmethod
    def from_config(cls, config, *, key: str | None = None, password: str | None = None) -> "BackupRestoreHistory":
        """Open the backup/restore history on the backend the config declares."""
        return cls(StoreTarget.from_config(config, key=key, password=password))

    @property
    def sqlite_path(self) -> Path:
        return self.store.sqlite_path

    @property
    def backend(self) -> str:
        return self.store.backend

    def initialize(self, *, force: bool = False) -> None:
        # Guarded exactly like the other three stores: this method owns backup_restore_history and
        # runs its own DDL, so leaving it unprotected meant eight app-command processes still raced
        # on the catalogs and died with "tuple concurrently updated" even after the others were fixed.
        if not force and backend_mod.schema_is_ready("BackupRestoreHistory", self.store.target):
            return
        if not force and backend_mod.remote_schema_is_current(
            self.store.target, "BackupRestoreHistory", HISTORY_SCHEMA_VERSION
        ):
            backend_mod.mark_schema_ready("BackupRestoreHistory", self.store.target)
            return
        self.store.initialize(force=force)
        with self.store.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS backup_restore_history
                (
                    restore_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    database_name TEXT NOT NULL,
                    backup_file TEXT NOT NULL,
                    restore_start TEXT NOT NULL,
                    restore_end TEXT NULL,
                    duration_seconds REAL NULL,
                    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'FAILED')),
                    error_message TEXT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
                );

                CREATE INDEX IF NOT EXISTS ix_backup_restore_history_db_created
                    ON backup_restore_history (database_name, created_at DESC);

                CREATE INDEX IF NOT EXISTS ix_backup_restore_history_status_created
                    ON backup_restore_history (status, created_at DESC);
                """
            )
            backend_mod.record_schema_version(conn, "BackupRestoreHistory", HISTORY_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("BackupRestoreHistory", self.store.target)

    def start_restore(self, *, database_name: str, backup_file: str, restore_start: str) -> int:
        self.initialize()
        with self.store.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO backup_restore_history
                (
                    database_name,
                    backup_file,
                    restore_start,
                    status
                )
                VALUES (?, ?, ?, 'RUNNING');
                """,
                (database_name, backup_file, restore_start),
            )
            return int(cursor.lastrowid)

    def finish_restore(
        self,
        *,
        restore_id: int,
        status: str,
        restore_end: str | None = None,
        duration_seconds: float | None = None,
        error_message: str | None = None,
    ) -> None:
        self.initialize()
        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE backup_restore_history
                SET
                    restore_end = ?,
                    duration_seconds = ?,
                    status = ?,
                    error_message = ?
                WHERE restore_id = ?;
                """,
                (
                    restore_end or utc_now_text(),
                    duration_seconds,
                    status,
                    error_message,
                    restore_id,
                ),
            )

    def fetch_recent(self, limit: int = 20) -> list[sqlite3.Row]:
        self.initialize()
        with self.store.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        restore_id,
                        database_name,
                        backup_file,
                        restore_start,
                        restore_end,
                        duration_seconds,
                        status,
                        error_message
                    FROM backup_restore_history
                    ORDER BY restore_id DESC
                    LIMIT ?;
                    """,
                    (limit,),
                )
            )

