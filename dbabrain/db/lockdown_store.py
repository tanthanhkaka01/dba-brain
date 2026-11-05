"""Lockdown - when an attack shows, dbabrain stops deleting (0.35.0, 1.220 - the plan of 2026-10-29, A3.5).

While a lockdown is on, nothing that destroys or overwrites runs: no prune, no delete of a backup
file, no restore over a database that is there, no SQL that writes. Backups, the scrub, the
metrics and the alerts keep running, so the evidence keeps accumulating while somebody looks.

It is turned on by hand (``control.cli lockdown on``) or by the scrub itself: a backup run that
finds :data:`AUTO_ENGAGE_FILES` or more stored backups changed or gone engages it (decision D6, the
plan's proposal: an attack is not waited out). **It is turned off by hand only** - nothing that
was attacked decides that the attack is over.

One table, ``lockdowns``: one row per lockdown, open while ``ended_at`` is NULL. A partial unique
index allows one open row; a row is never deleted, so the record of each one stays. It lives in the
runtime store because every node must see it: the worker that prunes and the master an operator
types on are different processes on different machines.
"""

from __future__ import annotations

from typing import Any

from dbabrain.db import backend as backend_mod
from dbabrain.db.backend import StoreTarget
from dbabrain.db.store import utc_now_text as utc_text
from dbabrain.lib import errors

#: Bumped when this table changes.
LOCKDOWN_SCHEMA_VERSION = 1
#: Files changed or gone in one scrub that engage a lockdown by themselves (D6).
AUTO_ENGAGE_FILES = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lockdowns
(
    lockdown_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    started_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    ended_at TEXT NULL,
    ended_by TEXT NULL,
    end_note TEXT NULL,
    CONSTRAINT uq_lockdowns_started UNIQUE (started_at, started_by)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_lockdowns_open ON lockdowns ((ended_at IS NULL)) WHERE ended_at IS NULL;
"""


class Locked(errors.Refused):
    """The action is refused because a lockdown is on."""


class LockdownStore:
    def __init__(self, source: Any, *, key: str | None = None, password: str | None = None) -> None:
        self.target = StoreTarget.coerce(source, key=key, password=password)

    @classmethod
    def from_config(cls, config: Any, *, key: str | None = None, password: str | None = None) -> "LockdownStore":
        return cls(StoreTarget.from_config(config, key=key, password=password))

    def connect(self):
        return self.target.connect()

    def initialize(self) -> None:
        if backend_mod.schema_is_ready("LockdownStore", self.target):
            return
        if backend_mod.remote_schema_is_current(self.target, "LockdownStore", LOCKDOWN_SCHEMA_VERSION):
            backend_mod.mark_schema_ready("LockdownStore", self.target)
            return
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            backend_mod.record_schema_version(conn, "LockdownStore", LOCKDOWN_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("LockdownStore", self.target)

    def active(self) -> dict[str, Any] | None:
        """The open lockdown, or ``None``."""
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM lockdowns WHERE ended_at IS NULL "
                               "ORDER BY lockdown_id DESC LIMIT 1").fetchone()
        return dict(row) if row is not None else None

    def engage(self, *, reason: str, by: str) -> dict[str, Any]:
        """Turn it on - or answer the one already on, unchanged: a second alarm is not a second lockdown."""
        if not str(reason or "").strip():
            raise errors.RequestError("a lockdown states why it is on.")
        current = self.active()
        if current is not None:
            return {**current, "already_on": True}
        with self.connect() as conn:
            conn.execute("INSERT INTO lockdowns (started_at, started_by, reason) VALUES (?, ?, ?)",
                         (utc_text(), str(by or "unknown"), str(reason)[:1000]))
        return {**(self.active() or {}), "already_on": False}

    def release(self, *, by: str, note: str) -> dict[str, Any] | None:
        """Turn it off, saying who and why. ``None`` when none was on."""
        if not str(note or "").strip():
            raise errors.RequestError("turning a lockdown off states why it is safe to.")
        current = self.active()
        if current is None:
            return None
        ended = {"ended_at": utc_text(), "ended_by": str(by or "unknown"), "end_note": str(note)[:1000]}
        with self.connect() as conn:
            conn.execute("UPDATE lockdowns SET ended_at = ?, ended_by = ?, end_note = ? WHERE lockdown_id = ?",
                         (ended["ended_at"], ended["ended_by"], ended["end_note"], int(current["lockdown_id"])))
        # The row as it now stands: answering the row read before the update printed `ended_at: null`
        # under "released", which reads as a lockdown that did not end (2026-11-03).
        return {**current, **ended}

    def refuse_if_on(self, action: str) -> None:
        """Raise :class:`Locked` naming the lockdown when one is on."""
        current = self.active()
        if current is not None:
            raise Locked(f"{action} is refused: a lockdown is on since {current['started_at']} "
                         f"({current['reason']}). Backups, the scrub and alerts still run; turn it off "
                         "with `control.cli lockdown off --note ...` once the estate is checked.")


def lockdown_on(app_config: Any = None) -> bool:
    """Is a lockdown on? A store that cannot be read answers yes: an unknown state never allows a
    destructive step. ``app_config`` defaults to this tool root's ``config.json``."""
    try:
        if app_config is None:
            from dbabrain.lib.config import load_config

            app_config = load_config()
        return LockdownStore.from_config(app_config).active() is not None
    except Exception:  # noqa: BLE001 - see docstring
        return True


def refuse_if_locked(app_config: Any, action: str) -> None:
    """The guard every destructive path calls first. A store that cannot be read refuses too: a
    lockdown nobody can see is not a reason to delete."""
    try:
        store = LockdownStore.from_config(app_config)
    except Exception as exc:  # noqa: BLE001 - see docstring
        raise Locked(f"{action} is refused: the lockdown state cannot be read ({type(exc).__name__}: {exc}).") from exc
    store.refuse_if_on(action)
