"""Web UI accounts and sessions, in the runtime store.

The web UI needs somewhere to keep who may log in and who currently is. Both go in the same
``db_ops`` schema as everything else — one store, one backup, one migration — because a login
table in a second place is a second thing to remember to move.

Three tables, keyed the way :mod:`db_ops.db.config_store` is:

* ``web_users``   PK ``web_user_id``; **partial** unique on ``username WHERE is_active = 1``.
* ``web_sessions``  PK ``web_session_id``; UNIQUE ``token_fingerprint``; FK to ``web_users``.
* ``web_login_attempts``  PK ``web_login_attempt_id``; FK to ``web_users`` (nullable — an attempt
  on a username that does not exist still has to be recorded).

**An account is disabled, never deleted**, same rule as config: ``is_active = 0`` keeps the row,
its level and its history, and frees the username so it can be issued again to somebody else
without the old row's audit trail moving to the new person.

Two things this deliberately does *not* store:

* **the password** — only a PBKDF2 encoding of it, from :mod:`db_ops.lib.web_auth`;
* **the session token** — only its SHA-256 fingerprint. Reading this table, from a backup or a
  replica or a psql prompt, does not let anyone log in as anybody.

**A recoverable copy lives elsewhere, on purpose.** ``password_ref`` names an entry in
``data/encrypted_secret_text.json`` holding the password itself, written by
:func:`remember_password` — the same place every database credential in this estate already lives,
encrypted with ``DB_OPS_SECRET_KEY``. It is there so an operator can look a console password up
instead of resetting it, which is what actually happens otherwise.

Be clear about the trade: with that copy, the hash is no longer the only one, and anyone holding
the passphrase can read the password. That is a real reduction — and a small one *here*, because
the same file already holds the postgres superuser and the SQL Server DBA logins, whose blast
radius is far larger than a read-only console account's. The login path never consults it: it is a
note for a person, not a credential the code falls back to.

Sessions last three months by default and expire on their own clock, so closing the browser does
not end one — see ``docs/12_webhost_app.md``. Expiry is applied on read (``resolve_session``
retires a session it finds past its ``expires_at``), which means the truth is the timestamp and
no sweeper job has to be running for a stale session to stop working.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from db_ops.config import StoreConfig
from db_ops.db import backend as backend_mod
from db_ops.db.backend import StoreTarget
from db_ops.db.store import ensure_sqlite_column
from db_ops.lib import web_auth

#: 2 — added web_users.password_ref, the secret-store entry holding a recoverable copy.
WEB_AUTH_SCHEMA_VERSION = 2

#: Why a login was refused. Recorded per attempt, and deliberately *not* what the user is told —
#: see :meth:`WebAuthStore.authenticate`.
REASON_OK = "ok"
REASON_NO_USER = "no_such_user"
REASON_DISABLED = "account_disabled"
REASON_BAD_PASSWORD = "bad_password"
REASON_LOCKED = "locked_out"

#: Failed attempts before an account is locked, and for how long. Modest numbers: this is an
#: internal tool behind the office network, so the lockout is there to stop an online guessing
#: run, not to survive a determined offline attack — the KDF does that.
DEFAULT_MAX_FAILED = 8
DEFAULT_LOCKOUT_MINUTES = 15


class WebAuthError(RuntimeError):
    """An account operation cannot be carried out as asked."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(moment: datetime) -> str:
    """The store's timestamp format, identical to what the SQL DEFAULTs emit."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: Any) -> datetime | None:
    """Read a stored timestamp back. ``None`` when it is absent or unreadable.

    PostgreSQL hands these back as ``str`` exactly as written; the parse is tolerant of a missing
    ``Z`` because a row written by hand during an incident should still be readable.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class WebAuthStore:
    """Accounts and sessions for the web UI (SQLite or PostgreSQL, per the store declaration)."""

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
    def from_config(cls, config, *, key: str | None = None, password: str | None = None) -> "WebAuthStore":
        return cls(StoreTarget.from_config(config, key=key, password=password))

    @property
    def backend(self) -> str:
        return self.target.store.backend

    def connect(self):
        return self.target.connect()

    def initialize(self, *, force: bool = False) -> None:
        if not force and backend_mod.schema_is_ready("WebAuthStore", self.target):
            return
        if not force and backend_mod.remote_schema_is_current(
                self.target, "WebAuthStore", WEB_AUTH_SCHEMA_VERSION):
            backend_mod.mark_schema_ready("WebAuthStore", self.target)
            return
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            # Additive, for a store created before the secret copy existed. Nullable-with-default
            # rather than backfilled: an empty ref is the truthful answer for an account whose
            # password was never written to the secret store.
            ensure_sqlite_column(
                conn,
                table_name="web_users",
                column_name="password_ref",
                column_sql="password_ref TEXT NOT NULL DEFAULT ''",
            )
            backend_mod.record_schema_version(conn, "WebAuthStore", WEB_AUTH_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("WebAuthStore", self.target)

    # ------------------------------------------------------------------ #
    # Accounts
    # ------------------------------------------------------------------ #
    def create_user(self, *, username: str, password: str, level: int,
                    display_name: str = "", email: str = "", actor: str = "",
                    note: str = "", password_ref: str = "") -> int:
        """Register an account. Refuses a username that is already active."""
        self.initialize()
        name = web_auth.normalize_username(username)
        user_level = web_auth.coerce_level(level)
        encoded = web_auth.hash_password(password)
        if self.get_user(name) is not None:
            raise WebAuthError(
                f"User '{name}' already exists and is active. Change its password or level "
                "instead, or disable it first if the name is being reissued to someone else.")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO web_users
                    (username, display_name, email, password_hash, password_ref, user_level,
                     is_active, failed_login_count, created_at, updated_at, created_by, note)
                VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, ?)
                """,
                (name, str(display_name or ""), str(email or ""), encoded, str(password_ref),
                 user_level, utc_text(utc_now()), utc_text(utc_now()), str(actor), str(note)),
            )
            return int(cursor.lastrowid)

    def set_password(self, *, username: str, password: str, actor: str = "",
                     revoke_sessions: bool = True, password_ref: str | None = None) -> int:
        """Replace an account's password.

        Existing sessions are revoked by default, and that default is the point: a password is
        changed either because it leaked or because somebody is leaving, and both cases are made
        pointless by a three-month cookie that keeps working.
        """
        self.initialize()
        name = web_auth.normalize_username(username)
        encoded = web_auth.hash_password(password)
        user = self._require_user(name)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE web_users
                SET password_hash = ?, password_ref = ?, failed_login_count = 0,
                    locked_until = NULL, updated_at = ?, updated_by = ?
                WHERE web_user_id = ?
                """,
                (encoded,
                 str(password_ref) if password_ref is not None else str(user["password_ref"] or ""),
                 utc_text(utc_now()), str(actor), int(user["web_user_id"])),
            )
        if revoke_sessions:
            self.revoke_user_sessions(int(user["web_user_id"]), reason="password changed")
        return int(user["web_user_id"])

    def set_level(self, *, username: str, level: int, actor: str = "") -> int:
        self.initialize()
        name = web_auth.normalize_username(username)
        user_level = web_auth.coerce_level(level)
        user = self._require_user(name)
        with self.connect() as conn:
            conn.execute(
                "UPDATE web_users SET user_level = ?, updated_at = ?, updated_by = ? "
                "WHERE web_user_id = ?",
                (user_level, utc_text(utc_now()), str(actor), int(user["web_user_id"])),
            )
        return int(user["web_user_id"])

    def deactivate_user(self, *, username: str, actor: str = "", note: str = "") -> int:
        """Disable an account and revoke its sessions. The row and its history stay."""
        self.initialize()
        name = web_auth.normalize_username(username)
        user = self._require_user(name)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE web_users
                SET is_active = 0, deactivated_at = ?, updated_at = ?, updated_by = ?, note = ?
                WHERE web_user_id = ?
                """,
                (utc_text(utc_now()), utc_text(utc_now()), str(actor), str(note),
                 int(user["web_user_id"])),
            )
        self.revoke_user_sessions(int(user["web_user_id"]), reason="account disabled")
        return int(user["web_user_id"])

    def get_user(self, username: str) -> Any | None:
        """The **active** account for a username, or ``None``."""
        self.initialize()
        name = web_auth.normalize_username(username)
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM web_users WHERE username = ? AND is_active = 1", (name,)
            ).fetchone()

    def get_user_by_id(self, web_user_id: int) -> Any | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM web_users WHERE web_user_id = ?", (int(web_user_id),)
            ).fetchone()

    def list_users(self, *, include_inactive: bool = False) -> list[Any]:
        self.initialize()
        clause = "" if include_inactive else "WHERE is_active = 1"
        with self.connect() as conn:
            return list(conn.execute(
                f"SELECT * FROM web_users {clause} ORDER BY is_active DESC, user_level DESC, username"
            ))

    def has_any_user(self) -> bool:
        """Is there an active account at all?

        The login page asks, so a brand-new deployment says "no accounts yet, create one with the
        CLI" instead of silently rejecting every password an operator tries.
        """
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM web_users WHERE is_active = 1").fetchone()
        return int(row["n"]) > 0

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    def authenticate(self, *, username: str, password: str, client_ip: str = "",
                     user_agent: str = "", max_failed: int = DEFAULT_MAX_FAILED,
                     lockout_minutes: int = DEFAULT_LOCKOUT_MINUTES) -> tuple[Any | None, str]:
        """Check a credential. Returns ``(user_row_or_None, reason)``.

        The **reason is for the log, not for the browser.** Every failure path returns a distinct
        reason so an operator reading ``web_login_attempts`` can tell a typo from a disabled
        account from a lockout; the caller renders one message for all of them, because telling a
        stranger "no such user" versus "wrong password" is how a login form becomes a way to
        enumerate who works here.

        A password check runs even when the username is unknown, against a throwaway hash. Without
        it the response time alone answers "does this account exist" — the same question the
        message above refuses to answer.
        """
        self.initialize()
        try:
            name = web_auth.normalize_username(username)
        except web_auth.WebAuthError:
            return None, REASON_NO_USER

        user = self.get_user(name)
        if user is None:
            # Equalise timing against the real path. The cost is one PBKDF2 run on a failed login.
            web_auth.verify_password(str(password or ""), _dummy_hash())
            self._record_attempt(None, name, False, REASON_NO_USER, client_ip, user_agent)
            return None, REASON_NO_USER

        locked_until = parse_utc(user["locked_until"])
        if locked_until is not None and locked_until > utc_now():
            self._record_attempt(int(user["web_user_id"]), name, False, REASON_LOCKED,
                                 client_ip, user_agent)
            return None, REASON_LOCKED

        if not web_auth.verify_password(str(password or ""), str(user["password_hash"])):
            self._register_failure(user, max_failed=max_failed, lockout_minutes=lockout_minutes)
            self._record_attempt(int(user["web_user_id"]), name, False, REASON_BAD_PASSWORD,
                                 client_ip, user_agent)
            return None, REASON_BAD_PASSWORD

        # Success. This is the only moment the plaintext is in hand, so it is the only moment the
        # stored hash can be brought up to the current cost.
        updates: list[tuple[str, Any]] = []
        if web_auth.needs_rehash(str(user["password_hash"])):
            updates.append(("password_hash", web_auth.hash_password(password)))
        with self.connect() as conn:
            assignments = "".join(f"{column} = ?, " for column, _ in updates)
            conn.execute(
                f"UPDATE web_users SET {assignments}failed_login_count = 0, locked_until = NULL, "
                "last_login_at = ? WHERE web_user_id = ?",
                (*[value for _, value in updates], utc_text(utc_now()), int(user["web_user_id"])),
            )
        self._record_attempt(int(user["web_user_id"]), name, True, REASON_OK, client_ip, user_agent)
        return self.get_user_by_id(int(user["web_user_id"])), REASON_OK

    def _register_failure(self, user: Any, *, max_failed: int, lockout_minutes: int) -> None:
        failed = int(user["failed_login_count"] or 0) + 1
        locked_until = None
        if max_failed > 0 and failed >= max_failed:
            locked_until = utc_text(utc_now() + timedelta(minutes=max(1, int(lockout_minutes))))
        with self.connect() as conn:
            conn.execute(
                "UPDATE web_users SET failed_login_count = ?, locked_until = ?, updated_at = ? "
                "WHERE web_user_id = ?",
                (failed, locked_until, utc_text(utc_now()), int(user["web_user_id"])),
            )

    def _record_attempt(self, web_user_id: int | None, username: str, succeeded: bool,
                        reason: str, client_ip: str, user_agent: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO web_login_attempts
                    (web_user_id, username_tried, succeeded, reason, client_ip, user_agent,
                     attempted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (web_user_id, str(username)[:64], 1 if succeeded else 0, str(reason),
                 str(client_ip)[:64], str(user_agent)[:256], utc_text(utc_now())),
            )

    def recent_attempts(self, *, limit: int = 50, username: str | None = None) -> list[Any]:
        self.initialize()
        params: list[Any] = []
        clause = ""
        if username:
            clause = "WHERE username_tried = ?"
            params.append(web_auth.normalize_username(username))
        with self.connect() as conn:
            return list(conn.execute(
                f"SELECT * FROM web_login_attempts {clause} "
                f"ORDER BY web_login_attempt_id DESC LIMIT {int(limit)}",
                tuple(params),
            ))

    # ------------------------------------------------------------------ #
    # Sessions
    # ------------------------------------------------------------------ #
    def issue_session(self, *, web_user_id: int, session_days: int = web_auth.DEFAULT_SESSION_DAYS,
                      client_ip: str = "", user_agent: str = "") -> dict[str, Any]:
        """Start a session. Returns the token, the CSRF token and the expiry.

        **The token is returned and not kept**: only its fingerprint is written. This is the one
        moment the secret exists outside the browser, so the caller must put it straight into a
        cookie and forget it.
        """
        self.initialize()
        token = web_auth.new_session_token()
        csrf = web_auth.new_csrf_token()
        issued = utc_now()
        expires = issued + timedelta(days=max(1, int(session_days)))
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO web_sessions
                    (web_user_id, token_fingerprint, csrf_token, issued_at, expires_at,
                     last_seen_at, client_ip, user_agent, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (int(web_user_id), web_auth.token_fingerprint(token), csrf, utc_text(issued),
                 utc_text(expires), utc_text(issued), str(client_ip)[:64], str(user_agent)[:256]),
            )
            session_id = int(cursor.lastrowid)
        return {"token": token, "csrf_token": csrf, "expires_at": utc_text(expires),
                "web_session_id": session_id, "max_age_seconds": int((expires - issued).total_seconds())}

    def resolve_session(self, token: str, *, touch: bool = True) -> dict[str, Any] | None:
        """The account behind a cookie, or ``None``.

        A session found past its ``expires_at`` is retired here rather than merely ignored, so the
        table stops accumulating rows that look live, and so an operator reading it sees the same
        answer the server gives. That makes expiry a property of the data, not of a sweeper job
        that might not be running.
        """
        self.initialize()
        fingerprint = web_auth.token_fingerprint(token)
        if not str(token or "").strip():
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, u.username, u.display_name, u.user_level, u.is_active AS user_is_active
                FROM web_sessions s
                JOIN web_users u ON u.web_user_id = s.web_user_id
                WHERE s.token_fingerprint = ? AND s.is_active = 1
                """,
                (fingerprint,),
            ).fetchone()
        if row is None:
            return None

        expires = parse_utc(row["expires_at"])
        if expires is None or expires <= utc_now():
            self._revoke_session_id(int(row["web_session_id"]), reason="expired")
            return None
        if not int(row["user_is_active"] or 0):
            # The account was disabled after the session was issued. deactivate_user already
            # revokes, so this is the belt to that braces — a session must never outlive its user.
            self._revoke_session_id(int(row["web_session_id"]), reason="account disabled")
            return None

        if touch:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE web_sessions SET last_seen_at = ? WHERE web_session_id = ?",
                    (utc_text(utc_now()), int(row["web_session_id"])),
                )
        return {
            "web_session_id": int(row["web_session_id"]),
            "web_user_id": int(row["web_user_id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"] or ""),
            "user_level": int(row["user_level"]),
            "csrf_token": str(row["csrf_token"]),
            "issued_at": str(row["issued_at"]),
            "expires_at": str(row["expires_at"]),
        }

    def revoke_session(self, token: str, *, reason: str = "logout") -> bool:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT web_session_id FROM web_sessions "
                "WHERE token_fingerprint = ? AND is_active = 1",
                (web_auth.token_fingerprint(token),),
            ).fetchone()
        if row is None:
            return False
        self._revoke_session_id(int(row["web_session_id"]), reason=reason)
        return True

    def revoke_user_sessions(self, web_user_id: int, *, reason: str = "revoked") -> int:
        self.initialize()
        with self.connect() as conn:
            rows = list(conn.execute(
                "SELECT web_session_id FROM web_sessions WHERE web_user_id = ? AND is_active = 1",
                (int(web_user_id),),
            ))
            for row in rows:
                conn.execute(
                    "UPDATE web_sessions SET is_active = 0, revoked_at = ?, revoked_reason = ? "
                    "WHERE web_session_id = ?",
                    (utc_text(utc_now()), str(reason), int(row["web_session_id"])),
                )
        return len(rows)

    def list_sessions(self, *, web_user_id: int | None = None,
                      include_inactive: bool = False, limit: int = 100) -> list[Any]:
        self.initialize()
        where = [] if include_inactive else ["s.is_active = 1"]
        params: list[Any] = []
        if web_user_id is not None:
            where.append("s.web_user_id = ?")
            params.append(int(web_user_id))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as conn:
            return list(conn.execute(
                f"""
                SELECT s.*, u.username FROM web_sessions s
                JOIN web_users u ON u.web_user_id = s.web_user_id
                {clause}
                ORDER BY s.web_session_id DESC LIMIT {int(limit)}
                """,
                tuple(params),
            ))

    def _revoke_session_id(self, web_session_id: int, *, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE web_sessions SET is_active = 0, revoked_at = ?, revoked_reason = ? "
                "WHERE web_session_id = ?",
                (utc_text(utc_now()), str(reason), int(web_session_id)),
            )

    def _require_user(self, username: str) -> Any:
        user = self.get_user(username)
        if user is None:
            raise WebAuthError(f"No active user named '{username}'.")
        return user


def secret_ref_for(username: str) -> str:
    """The secret-store entry that holds one console account's password.

    Derived from the username rather than stored first and looked up later, so the ref is
    predictable from a psql prompt and from a runbook — and so renaming nothing is required when
    an account is created before anyone thinks about the secret store.
    """
    name = web_auth.normalize_username(username)
    safe = "".join(char if char.isalnum() else "_" for char in name).strip("_").upper()
    return f"WEB_CONSOLE_{safe}"


def remember_password(username: str, password: str, *, data_dir: Any = None,
                      key: str | None = None, plaintext_store: Any = None) -> str:
    """Write a console password into the secret store. Returns the ref it was written under.

    Both copies are written — the encrypted store and the plaintext source that regenerates it on
    deploy — because updating only the first is undone by the next deploy. See
    :func:`db_ops.lib.secret_text.set_secret_everywhere`.
    """
    from db_ops.lib.paths import DEFAULT_DATA_DIR, TOOL_ROOT
    from db_ops.lib.secret_text import set_secret_everywhere

    ref = secret_ref_for(username)
    resolved_dir = Path(data_dir) if data_dir else Path(DEFAULT_DATA_DIR)
    source = Path(plaintext_store) if plaintext_store else Path(TOOL_ROOT) / "secrets" / "secret_text.json"
    set_secret_everywhere(resolved_dir, ref, password, key=key, plaintext_store=source,
                          overwrite=True)
    return ref


def recall_password(username: str, *, data_dir: Any = None, key: str | None = None) -> str:
    """Read a console password back out of the secret store, or ``""`` when it was never kept.

    Deliberately **not** used by :meth:`WebAuthStore.authenticate`: the hash is what a login is
    checked against, and a fallback here would make the secret store a second, softer credential
    path. This exists for the operator who has forgotten a password, and for nothing else.
    """
    from db_ops.lib.paths import DEFAULT_DATA_DIR
    from db_ops.lib.secret_text import load_secret_text

    resolved_dir = Path(data_dir) if data_dir else Path(DEFAULT_DATA_DIR)
    secrets = load_secret_text(resolved_dir, key=key)
    return str(secrets.get(secret_ref_for(username), "") or "")


#: A real PBKDF2 encoding of a value nobody knows, used only to spend the same time on an unknown
#: username as on a known one. Memoised per cost rather than built at import: the cost is then paid
#: once per process instead of on every failed login, and importing this module stays free for the
#: many callers that never authenticate anything.
_DUMMY_HASHES: dict[int, str] = {}


def _dummy_hash() -> str:
    cost = web_auth.PBKDF2_ITERATIONS
    if cost not in _DUMMY_HASHES:
        _DUMMY_HASHES[cost] = web_auth.hash_password("db_ops-timing-equaliser")
    return _DUMMY_HASHES[cost]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS web_users
(
    web_user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    password_ref TEXT NOT NULL DEFAULT '',
    user_level INTEGER NOT NULL DEFAULT 1 CHECK (user_level BETWEEN 1 AND 100),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT NULL,
    last_login_at TEXT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    deactivated_at TEXT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);

-- Partial, like ux_config_items_active: one *active* account may hold a username, and disabling
-- one frees the name again without deleting the row that says what it used to be able to do.
CREATE UNIQUE INDEX IF NOT EXISTS ux_web_users_active
    ON web_users (username) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS web_sessions
(
    web_session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    web_user_id INTEGER NOT NULL,
    token_fingerprint TEXT NOT NULL,
    csrf_token TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    client_ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    revoked_at TEXT NULL,
    revoked_reason TEXT NOT NULL DEFAULT '',
    CONSTRAINT uq_web_sessions_token UNIQUE (token_fingerprint),
    CONSTRAINT fk_web_sessions_user FOREIGN KEY (web_user_id)
        REFERENCES web_users (web_user_id)
);

CREATE INDEX IF NOT EXISTS ix_web_sessions_user ON web_sessions (web_user_id, is_active);
CREATE INDEX IF NOT EXISTS ix_web_sessions_expires ON web_sessions (expires_at);

CREATE TABLE IF NOT EXISTS web_login_attempts
(
    web_login_attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    web_user_id INTEGER NULL,
    username_tried TEXT NOT NULL,
    succeeded INTEGER NOT NULL DEFAULT 0 CHECK (succeeded IN (0, 1)),
    reason TEXT NOT NULL DEFAULT '',
    client_ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    attempted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    CONSTRAINT fk_web_login_attempts_user FOREIGN KEY (web_user_id)
        REFERENCES web_users (web_user_id)
);

CREATE INDEX IF NOT EXISTS ix_web_login_attempts_user
    ON web_login_attempts (web_user_id, attempted_at);
CREATE INDEX IF NOT EXISTS ix_web_login_attempts_username
    ON web_login_attempts (username_tried, attempted_at);
"""
