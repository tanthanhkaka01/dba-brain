""""Run this app now" — a request the console writes and the daemon picks up.

The console runs in the **webhost process**; the app commands are run by the **daemon process**,
which owns the working directory, the log scope, the forwarded secret key, the timeout reaper and
the ``job_runs`` row every run writes. A "Run now" button that spawned its own subprocess would be
a second executor with none of that: no reaper, so a hung command runs forever, and a ``job_runs``
row that says ``running`` until somebody notices.

So the button does not run anything. It writes a **request**, and the daemon starts the command on
its next scan exactly the way the schedule would have. One executor, one place a run can come from,
and the run is visible in ``job_runs`` like every other — which is what lets the dashboard, the
control app's summary and the Telegram alert all keep agreeing about what ran.

The shape follows the rest of the store: PK, a real FK, and a **partial** unique index.

* ``ux_app_command_requests_pending`` covers ``app_command_id WHERE status = 'pending'``, so an
  impatient double-click cannot queue the same app twice. The earlier request stands; the second
  press is told it is already queued rather than silently doubling the work.
* ``job_run_id`` is a foreign key to ``job_runs (log_id)`` — the run this request produced. It is
  what turns "I asked for it" into "here is what happened", and it is why
  :meth:`RunRequestStore.initialize` brings the main store's schema up first: the referenced table
  has to exist before this one can point at it.

There is deliberately **no** foreign key on ``app_command_id``. App commands live in
``data/app_commands.json`` (mirrored into ``config_items``, where the key is one column among
many), not in a table whose primary key is that id — a FK would have to invent one.

A request is never deleted: it ends as ``done``, ``cancelled`` or ``expired`` and stays as the
record of who asked for what. ``expired`` is the daemon-was-down case — a request nobody could
have acted on must not fire hours later when the daemon comes back and surprise whoever is on
shift.

**It moves with the run it points at.** ``job_run_id`` is a real foreign key with no
``ON DELETE``, so a request outlived the ``job_runs`` row it referenced and blocked the archive
sweep from ever removing it: one finished request was enough to fail the whole batch, and the
daemon swallows that failure by design (housekeeping must not stop the scheduler), so the busiest
table in the store silently stopped pruning. Found on 2026-09-04 by reading a live daemon's log —
``23503 … Key (log_id)=(1601189) is still referenced``. So ``archive_old_job_runs`` now moves the
referencing requests into ``app_command_requests_history`` in the same transaction, which keeps
both records and lets the delete succeed. Nothing is lost; it is the same trade ``job_runs``
itself makes.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from db_ops.config import StoreConfig
from db_ops.db import backend as backend_mod
from db_ops.db.backend import StoreTarget
# One clock and one timestamp format for the whole store. web_auth_store spelled these first;
# a second copy is two definitions of "now" that agree until one of them is changed.
from db_ops.db.web_auth_store import utc_now, utc_text  # noqa: F401 - one definition

#: Bumped when this table or its additive migrations change.
RUN_REQUEST_SCHEMA_VERSION = 2

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_STARTED = "started"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

#: Statuses a request can still move out of. Everything else is final.
OPEN_STATUSES = (STATUS_PENDING, STATUS_CLAIMED, STATUS_STARTED)

#: How long a pending request stays actionable. Fifteen minutes is chosen against the daemon's
#: scan interval (seconds) rather than against a person's patience: if it has not been picked up in
#: that time the daemon was not running, and firing the request whenever it comes back would run
#: the app at a moment nobody chose.
DEFAULT_REQUEST_TTL_SECONDS = 900


class RunRequestError(RuntimeError):
    """A run request cannot be recorded or advanced as asked."""


class RunRequestStore:
    """The "run now" queue (SQLite or PostgreSQL, per the store declaration)."""

    def __init__(
        self,
        source: "str | Path | StoreTarget | StoreConfig",
        *,
        key: str | None = None,
        password: str | None = None,
    ) -> None:
        self.target = StoreTarget.coerce(source, key=key, password=password)
        self.sqlite_path = self.target.sqlite_path

    @classmethod
    def from_config(cls, config, *, key: str | None = None, password: str | None = None) -> "RunRequestStore":
        return cls(StoreTarget.from_config(config, key=key, password=password))

    @property
    def backend(self) -> str:
        return self.target.store.backend

    def connect(self):
        return self.target.connect()

    def initialize(self, *, force: bool = False) -> None:
        if not force and backend_mod.schema_is_ready("RunRequestStore", self.target):
            return
        if not force and backend_mod.remote_schema_is_current(
                self.target, "RunRequestStore", RUN_REQUEST_SCHEMA_VERSION):
            backend_mod.mark_schema_ready("RunRequestStore", self.target)
            return
        # job_runs has to exist before a foreign key can point at it. This is the only place one
        # store class builds another's schema, and it is because the reference is real.
        from db_ops.db import DbOpsStore

        DbOpsStore(self.target).initialize()
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            backend_mod.record_schema_version(conn, "RunRequestStore", RUN_REQUEST_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("RunRequestStore", self.target)

    # ------------------------------------------------------------------ #
    # Asking
    # ------------------------------------------------------------------ #
    def request_run(self, *, app_command_id: str, requested_by: str = "",
                    source: str = "console", note: str = "") -> dict[str, Any]:
        """Queue a run. Returns the request, or the one already queued.

        A second request for an app that is already waiting is **not** an error and does not
        create a row: the honest answer to "run it now" when it is already about to run is "it is
        already queued", and an operator pressing a button twice should not run an app twice.
        """
        self.initialize()
        code = str(app_command_id or "").strip()
        if not code:
            raise RunRequestError("app_command_id is required.")
        existing = self.pending_for(code)
        if existing is not None:
            return {"request_id": int(existing["request_id"]), "created": False,
                    "status": str(existing["status"]),
                    "requested_at": str(existing["requested_at"]),
                    "requested_by": str(existing["requested_by"])}
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO app_command_requests
                    (app_command_id, status, requested_by, request_source, requested_at, note)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (code, STATUS_PENDING, str(requested_by), str(source), utc_text(utc_now()),
                 str(note)),
            )
            request_id = int(cursor.lastrowid)
        return {"request_id": request_id, "created": True, "status": STATUS_PENDING,
                "requested_at": utc_text(utc_now()), "requested_by": str(requested_by)}

    def cancel_request(self, request_id: int, *, actor: str = "") -> bool:
        """Withdraw a request that has not been claimed yet."""
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM app_command_requests WHERE request_id = ?",
                (int(request_id),),
            ).fetchone()
            if row is None or str(row["status"]) != STATUS_PENDING:
                return False
            conn.execute(
                "UPDATE app_command_requests SET status = ?, finished_at = ?, "
                "note = ? WHERE request_id = ?",
                (STATUS_CANCELLED, utc_text(utc_now()),
                 f"Cancelled by {actor or 'an operator'}.", int(request_id)),
            )
        return True

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def pending_for(self, app_command_id: str) -> Any | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM app_command_requests "
                "WHERE app_command_id = ? AND status = ?",
                (str(app_command_id), STATUS_PENDING),
            ).fetchone()

    def open_requests(self) -> dict[str, Any]:
        """``app_command_id -> the request that is still in flight``, for the dashboard."""
        self.initialize()
        placeholders = ", ".join("?" for _ in OPEN_STATUSES)
        with self.connect() as conn:
            rows = list(conn.execute(
                f"SELECT * FROM app_command_requests WHERE status IN ({placeholders}) "
                "ORDER BY request_id",
                tuple(OPEN_STATUSES),
            ))
        return {str(row["app_command_id"]): row for row in rows}

    def list_requests(self, *, app_command_id: str | None = None, limit: int = 50) -> list[Any]:
        self.initialize()
        params: list[Any] = []
        clause = ""
        if app_command_id:
            clause = "WHERE app_command_id = ?"
            params.append(str(app_command_id))
        with self.connect() as conn:
            return list(conn.execute(
                f"SELECT * FROM app_command_requests {clause} "
                f"ORDER BY request_id DESC LIMIT {int(limit)}",
                tuple(params),
            ))

    # ------------------------------------------------------------------ #
    # The daemon's side
    # ------------------------------------------------------------------ #
    def claim(self, app_command_id: str, *,
              ttl_seconds: int = DEFAULT_REQUEST_TTL_SECONDS) -> Any | None:
        """Take the pending request for one app, or ``None``.

        Expires it instead of returning it when it has been waiting longer than ``ttl_seconds``.
        The claim is a conditional UPDATE — ``WHERE status = 'pending'`` — so two daemons racing
        on the same store cannot both come away with it; the loser updates nothing and reads back
        no row.
        """
        self.initialize()
        row = self.pending_for(app_command_id)
        if row is None:
            return None
        requested_at = str(row["requested_at"])
        cutoff = utc_text(utc_now() - timedelta(seconds=max(1, int(ttl_seconds))))
        if requested_at < cutoff:
            self._finish(int(row["request_id"]), STATUS_EXPIRED,
                         note=f"Not picked up within {int(ttl_seconds)}s; the daemon was not running.")
            return None
        with self.connect() as conn:
            conn.execute(
                "UPDATE app_command_requests SET status = ?, claimed_at = ? "
                "WHERE request_id = ? AND status = ?",
                (STATUS_CLAIMED, utc_text(utc_now()), int(row["request_id"]), STATUS_PENDING),
            )
            return conn.execute(
                "SELECT * FROM app_command_requests WHERE request_id = ? AND status = ?",
                (int(row["request_id"]), STATUS_CLAIMED),
            ).fetchone()

    def mark_started(self, request_id: int, *, job_run_id: int | None = None) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                "UPDATE app_command_requests SET status = ?, started_at = ?, job_run_id = ? "
                "WHERE request_id = ?",
                (STATUS_STARTED, utc_text(utc_now()),
                 int(job_run_id) if job_run_id is not None else None, int(request_id)),
            )

    def mark_done(self, request_id: int, *, note: str = "") -> None:
        self._finish(int(request_id), STATUS_DONE, note=note)

    def release(self, request_id: int, *, note: str = "") -> None:
        """Put a claimed request back to pending — the start failed before anything ran.

        Without this a command that could not be spawned would leave its request claimed forever,
        and the operator would see "queued" with nothing ever happening.
        """
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                "UPDATE app_command_requests SET status = ?, claimed_at = NULL, note = ? "
                "WHERE request_id = ? AND status = ?",
                (STATUS_PENDING, str(note), int(request_id), STATUS_CLAIMED),
            )

    def expire_stale(self, *, ttl_seconds: int = DEFAULT_REQUEST_TTL_SECONDS) -> int:
        """Expire every pending request older than the window. Returns how many."""
        self.initialize()
        cutoff = utc_text(utc_now() - timedelta(seconds=max(1, int(ttl_seconds))))
        with self.connect() as conn:
            rows = list(conn.execute(
                "SELECT request_id FROM app_command_requests WHERE status = ? AND requested_at < ?",
                (STATUS_PENDING, cutoff),
            ))
            for row in rows:
                conn.execute(
                    "UPDATE app_command_requests SET status = ?, finished_at = ?, note = ? "
                    "WHERE request_id = ?",
                    (STATUS_EXPIRED, utc_text(utc_now()),
                     f"Not picked up within {int(ttl_seconds)}s.", int(row["request_id"])),
                )
        return len(rows)

    def close_finished(self) -> int:
        """Close every ``started`` request whose run has already finished. Returns how many.

        The daemon closes a request as it reaps the process it started, which covers the normal
        path. This is the safety net for the one it cannot: a daemon killed between starting a
        command and reaping it leaves the request at ``started`` forever, and the console goes on
        showing a badge for a run that ended hours ago.

        It reads ``job_runs`` directly, which is exactly what the ``job_run_id`` foreign key
        already says this table does — the run is not a loose reference, it is the answer to what
        the request produced.
        """
        self.initialize()
        closed = 0
        with self.connect() as conn:
            rows = list(conn.execute(
                """
                SELECT r.request_id, j.status, j.finished_at
                FROM app_command_requests r
                JOIN job_runs j ON j.log_id = r.job_run_id
                WHERE r.status = ? AND j.finished_at IS NOT NULL
                """,
                (STATUS_STARTED,),
            ))
            for row in rows:
                conn.execute(
                    "UPDATE app_command_requests SET status = ?, finished_at = ?, note = ? "
                    "WHERE request_id = ? AND status = ?",
                    (STATUS_DONE, utc_text(utc_now()),
                     f"Run finished with status {row['status']}.", int(row["request_id"]),
                     STATUS_STARTED),
                )
                closed += 1
        return closed

    def _finish(self, request_id: int, status: str, *, note: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE app_command_requests SET status = ?, finished_at = ?, note = ? "
                "WHERE request_id = ?",
                (str(status), utc_text(utc_now()), str(note), int(request_id)),
            )


#: The history table on its own, because something other than this module has to be able to
#: create it. ``DbOpsStore.archive_old_job_runs`` does, when it finds an ``app_command_requests``
#: table without one — which is **every store upgraded from a build that predates it**, on
#: either backend. Measured 2026-09-04 on the production PostgreSQL store and on a brand-new
#: SQLite one: the requests table existed, the history table did not, so the sweep's "nothing
#: to move" guard read as normal and the foreign key went on failing the delete exactly as it
#: did before the fix. Defined once and appended to :data:`SCHEMA_SQL`, so the two cannot
#: disagree about the table a store ends up with.
APP_COMMAND_REQUESTS_HISTORY_SQL = """
-- Where a request goes when the run it points at ages out of `job_runs`. No foreign key and no
-- constraints: an archive that can refuse a row is an archive that can block the sweep, which is
-- the failure this table exists to end.
CREATE TABLE IF NOT EXISTS app_command_requests_history
(
    request_id INTEGER,
    app_command_id TEXT,
    status TEXT,
    requested_by TEXT,
    request_source TEXT,
    requested_at TEXT,
    claimed_at TEXT NULL,
    started_at TEXT NULL,
    finished_at TEXT NULL,
    job_run_id INTEGER NULL,
    note TEXT,
    archived_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_app_command_requests_history_command
    ON app_command_requests_history (app_command_id, requested_at DESC);
"""


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_command_requests
(
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_command_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'started', 'done', 'cancelled', 'expired')),
    requested_by TEXT NOT NULL DEFAULT '',
    request_source TEXT NOT NULL DEFAULT '',
    requested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    claimed_at TEXT NULL,
    started_at TEXT NULL,
    finished_at TEXT NULL,
    job_run_id INTEGER NULL,
    note TEXT NOT NULL DEFAULT '',
    CONSTRAINT fk_app_command_requests_job_run FOREIGN KEY (job_run_id)
        REFERENCES job_runs (log_id)
);

-- Partial: one *pending* request per app command. A double-click is told "already queued"
-- instead of running the app twice.
CREATE UNIQUE INDEX IF NOT EXISTS ux_app_command_requests_pending
    ON app_command_requests (app_command_id) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS ix_app_command_requests_status
    ON app_command_requests (status, requested_at);
CREATE INDEX IF NOT EXISTS ix_app_command_requests_command
    ON app_command_requests (app_command_id, request_id);
"""

SCHEMA_SQL += APP_COMMAND_REQUESTS_HISTORY_SQL

