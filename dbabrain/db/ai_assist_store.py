"""The AI assistant's tables: every question, every message sent to and from the model, every call
to the API and every tool it ran (0.34.0, 1.257).

The operator, 2026-10-31: *keep it carefully - what the user wrote, what the AI answered, which
dbabrain tools it used while answering - and how many tokens were used and what they cost.* petweb
keeps the same four things (``ai_usage``, ``ai_messages``, ``ai_tool_runs``, ``ai_conversations``);
this adds the fifth petweb only logs: one row per call to the API, with Anthropic's ``request-id``.

Five tables:

* ``ai_conversations``  one per conversation: the chat, the user, when it was opened and closed.
* ``ai_requests``       one per message the assistant read in its chat: a question, a group word
  (``/new``, ``/usage``, ``/help``) or a refusal - **a refused question is kept**, with why. The
  ``UNIQUE (chat_id, message_id)`` is the claim: a message is answered once.
* ``ai_messages``       the conversation exactly as the model takes it - each question, each answer
  (thinking blocks included, which must go back as they came) and each set of tool results.
* ``ai_api_calls``      one per call to the model: the model asked and the one that served, the
  four token counts, the cost, the whole response.
* ``ai_tool_runs``      one per tool call: its input, what it returned (redacted), how long.

**Keys, in full** (the operator's rule for every store table): a primary key each, a foreign key to
every parent inside the five, a unique key on each natural key. **No foreign key to
``telegram_messages`` or ``telegram_send_messages``**: those age out, and these rows are the record.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dbabrain.lib import errors
from dbabrain.lib.config import StoreConfig
from dbabrain.db import backend as backend_mod
from dbabrain.db.backend import StoreTarget
from dbabrain.db.store import utc_now_text

#: Bumped when these tables change.
AI_SCHEMA_VERSION = 1

TABLES = ("ai_conversations", "ai_requests", "ai_messages", "ai_api_calls", "ai_tool_runs")

REQUEST_KINDS = ("question", "command")
REQUEST_STATUSES = ("running", "done", "error", "blocked", "stopped")
CLOSE_REASONS = ("new", "idle", "length")
ROLES = ("user", "assistant")

# The column default every catalog table uses - one definition (test_no_duplicate_definitions).
from dbabrain.db.backup_catalog import _NOW  # noqa: E402


def _in(column: str, allowed: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in allowed)})"


SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS ai_conversations
(
    ai_conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT NULL,
    closed_reason TEXT NULL CHECK (closed_reason IS NULL OR {_in('closed_reason', CLOSE_REASONS)}),
    CONSTRAINT uq_ai_conversations UNIQUE (chat_id, user_id, created_at)
);

CREATE INDEX IF NOT EXISTS ix_ai_conversations_user ON ai_conversations (chat_id, user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS ai_requests
(
    ai_request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    telegram_message_id INTEGER NULL,
    reply_to_message_id INTEGER NULL,
    ai_conversation_id INTEGER NULL REFERENCES ai_conversations (ai_conversation_id),
    user_id TEXT NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK ({_in('kind', REQUEST_KINDS)}),
    question TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    context_text TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK ({_in('status', REQUEST_STATUSES)}),
    reason TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    effort TEXT NOT NULL DEFAULT '',
    tools_mode TEXT NOT NULL DEFAULT '',
    est_tokens INTEGER NOT NULL DEFAULT 0 CHECK (est_tokens >= 0),
    est_cost_usd REAL NOT NULL DEFAULT 0 CHECK (est_cost_usd >= 0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_read_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    cost_usd REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    turns INTEGER NOT NULL DEFAULT 0 CHECK (turns >= 0),
    tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls >= 0),
    stop_reason TEXT NOT NULL DEFAULT '',
    answer_text TEXT NOT NULL DEFAULT '',
    reply_send_ids TEXT NOT NULL DEFAULT '[]',
    node_host TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NULL,
    CONSTRAINT uq_ai_requests UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS ix_ai_requests_started ON ai_requests (started_at);
CREATE INDEX IF NOT EXISTS ix_ai_requests_user ON ai_requests (user_id, started_at);
CREATE INDEX IF NOT EXISTS ix_ai_requests_conversation ON ai_requests (ai_conversation_id);

CREATE TABLE IF NOT EXISTS ai_messages
(
    ai_message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ai_conversation_id INTEGER NOT NULL REFERENCES ai_conversations (ai_conversation_id),
    ai_request_id INTEGER NOT NULL REFERENCES ai_requests (ai_request_id),
    seq INTEGER NOT NULL CHECK (seq >= 0),
    role TEXT NOT NULL CHECK ({_in('role', ROLES)}),
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT {_NOW},
    CONSTRAINT uq_ai_messages UNIQUE (ai_conversation_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_ai_messages_request ON ai_messages (ai_request_id);

CREATE TABLE IF NOT EXISTS ai_api_calls
(
    ai_api_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ai_request_id INTEGER NOT NULL REFERENCES ai_requests (ai_request_id),
    turn_no INTEGER NOT NULL CHECK (turn_no >= 1),
    model_requested TEXT NOT NULL,
    model_served TEXT NOT NULL DEFAULT '',
    anthropic_request_id TEXT NOT NULL DEFAULT '',
    stop_reason TEXT NOT NULL DEFAULT '',
    stop_details_json TEXT NOT NULL DEFAULT '{{}}',
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_read_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cost_usd REAL NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    price_known INTEGER NOT NULL DEFAULT 1 CHECK (price_known IN (0, 1)),
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    request_meta_json TEXT NOT NULL DEFAULT '{{}}',
    response_json TEXT NOT NULL DEFAULT '{{}}',
    error_text TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NULL,
    CONSTRAINT uq_ai_api_calls UNIQUE (ai_request_id, turn_no)
);

CREATE TABLE IF NOT EXISTS ai_tool_runs
(
    ai_tool_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ai_request_id INTEGER NOT NULL REFERENCES ai_requests (ai_request_id),
    ai_api_call_id INTEGER NULL REFERENCES ai_api_calls (ai_api_call_id),
    tool_use_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{{}}',
    output_text TEXT NOT NULL DEFAULT '',
    output_chars INTEGER NOT NULL DEFAULT 0 CHECK (output_chars >= 0),
    sent_chars INTEGER NOT NULL DEFAULT 0 CHECK (sent_chars >= 0),
    is_error INTEGER NOT NULL DEFAULT 0 CHECK (is_error IN (0, 1)),
    row_count INTEGER NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
    created_at TEXT NOT NULL DEFAULT {_NOW},
    CONSTRAINT uq_ai_tool_runs UNIQUE (ai_request_id, tool_use_id)
);
"""


class AiAssistStoreError(errors.OperationFailed):
    """An AI assistant row could not be written as asked."""


#: The columns a writer may set on each table - anything else is a misspelling, refused.
_COLUMNS = {
    "ai_conversations": ("chat_id", "user_id", "user_name", "title", "model", "created_at", "updated_at",
                         "closed_at", "closed_reason"),
    "ai_requests": ("chat_id", "message_id", "telegram_message_id", "reply_to_message_id", "ai_conversation_id",
                    "user_id", "user_name", "kind", "question", "fingerprint", "context_text", "attachments_json",
                    "status", "reason", "model", "effort", "tools_mode", "est_tokens", "est_cost_usd",
                    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens",
                    "cost_usd", "turns", "tool_calls", "stop_reason", "answer_text", "reply_send_ids",
                    "node_host", "app_version", "started_at", "finished_at"),
    "ai_api_calls": ("ai_request_id", "turn_no", "model_requested", "model_served", "anthropic_request_id",
                     "stop_reason", "stop_details_json", "input_tokens", "output_tokens", "cache_read_tokens",
                     "cache_write_tokens", "cost_usd", "price_known", "duration_ms", "request_meta_json",
                     "response_json", "error_text", "started_at", "finished_at"),
    "ai_tool_runs": ("ai_request_id", "ai_api_call_id", "tool_use_id", "tool_name", "input_json", "output_text",
                     "output_chars", "sent_chars", "is_error", "row_count", "duration_ms"),
}


def _known(table: str, values: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(values) - set(_COLUMNS[table]))
    if unknown:
        raise AiAssistStoreError(f"{table} has no column(s) {', '.join(unknown)}.")
    return values


def _insert(conn: Any, table: str, values: dict[str, Any]) -> int:
    columns = [name for name, value in values.items() if value is not None]
    cursor = conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                          tuple(values[name] for name in columns))
    return int(cursor.lastrowid)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class AiAssistStore:
    """The five tables (SQLite or PostgreSQL, per the store declaration). One method per event, each
    in its own transaction, so a question the job dies in the middle of keeps every row it reached."""

    def __init__(self, source: "str | Path | StoreTarget | StoreConfig", *,
                 key: str | None = None, password: str | None = None) -> None:
        self.target = StoreTarget.coerce(source, key=key, password=password)

    @classmethod
    def from_config(cls, config: Any, *, key: str | None = None, password: str | None = None) -> "AiAssistStore":
        return cls(StoreTarget.from_config(config, key=key, password=password))

    @property
    def backend(self) -> str:
        return self.target.store.backend

    def connect(self):
        return self.target.connect()

    def initialize(self, *, force: bool = False) -> None:
        if not force and backend_mod.schema_is_ready("AiAssistStore", self.target):
            return
        if not force and backend_mod.remote_schema_is_current(self.target, "AiAssistStore", AI_SCHEMA_VERSION):
            backend_mod.mark_schema_ready("AiAssistStore", self.target)
            return
        self.target.prepare()
        with self.connect() as conn:
            backend_mod.acquire_schema_lock(conn)
            conn.executescript(SCHEMA_SQL)
            backend_mod.record_schema_version(conn, "AiAssistStore", AI_SCHEMA_VERSION)
        backend_mod.mark_schema_ready("AiAssistStore", self.target)

    # ------------------------------------------------------------------ #
    # Requests
    # ------------------------------------------------------------------ #
    def claim_request(self, **values: Any) -> int | None:
        """Write the request row - the claim. ``None`` when the message was claimed already: a
        message is answered once, whatever runs it twice."""
        values = _known("ai_requests", {"started_at": utc_now_text(), **values})
        for name in ("chat_id", "message_id", "user_id", "kind", "status"):
            if values.get(name) in (None, ""):
                raise AiAssistStoreError(f"ai_requests needs {name}.")
        self.initialize()
        with self.connect() as conn:
            taken = conn.execute("SELECT ai_request_id FROM ai_requests WHERE chat_id = ? AND message_id = ?",
                                 (str(values["chat_id"]), int(values["message_id"]))).fetchone()
            if taken is not None:
                return None
            try:
                return _insert(conn, "ai_requests", values)
            except Exception as exc:
                # Another run inserted it between the SELECT and here: the key decided, it was claimed.
                if backend_mod.is_unique_violation(exc):
                    return None
                raise

    def next_message_id(self, chat_id: str) -> int:
        """The next message number of a chat no Telegram numbers - the command line's (``ai-ask``):
        a request is keyed by its chat and message, and each question asked there is a message."""
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(message_id), 0) AS last FROM ai_requests WHERE chat_id = ?",
                               (str(chat_id),)).fetchone()
        return int(row["last"] or 0) + 1

    def set_request(self, ai_request_id: int, **values: Any) -> None:
        values = _known("ai_requests", values)
        if not values:
            return
        self.initialize()
        with self.connect() as conn:
            conn.execute(f"UPDATE ai_requests SET {', '.join(f'{name} = ?' for name in values)} "
                         "WHERE ai_request_id = ?", (*values.values(), int(ai_request_id)))

    def close_stale(self, *, started_before: str, reason: str) -> int:
        """A question still ``running`` from a job that died: closed as an error, so its held place
        in the limits is given back."""
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE ai_requests SET status = 'error', reason = ?, finished_at = ? "
                "WHERE status = 'running' AND started_at < ?", (reason, utc_now_text(), started_before))
            return int(getattr(cursor, "rowcount", 0) or 0)

    def claimed_message_ids(self, *, chat_id: str, message_ids: list[int]) -> set[int]:
        if not message_ids:
            return set()
        self.initialize()
        marks = ", ".join("?" for _ in message_ids)
        with self.connect() as conn:
            rows = conn.execute(f"SELECT message_id FROM ai_requests WHERE chat_id = ? AND message_id IN ({marks})",
                                (str(chat_id), *[int(item) for item in message_ids])).fetchall()
        return {int(row["message_id"]) for row in rows}

    def unclaimed_messages(self, *, chat_ids: list[str], since_epoch: int, limit: int) -> list[dict[str, Any]]:
        """The chats' messages the assistant has not read yet, oldest first (the bot's workflow saved
        them into ``telegram_messages``)."""
        if not chat_ids:
            return []
        self.initialize()
        marks = ", ".join("?" for _ in chat_ids)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT m.telegram_message_id, m.message_id, m.message_date, m.chat_id, m.chat_type, m.user_id, "
                f"m.text, m.raw_json FROM telegram_messages m WHERE m.chat_id IN ({marks}) AND m.message_date >= ? "
                "AND NOT EXISTS (SELECT 1 FROM ai_requests r WHERE r.chat_id = m.chat_id AND r.message_id = m.message_id) "
                "ORDER BY m.telegram_message_id LIMIT ?",
                (*[str(item) for item in chat_ids], int(since_epoch), int(limit))).fetchall()
        return [dict(row) for row in rows]

    def held_by_a_bot_command(self, *, chat_id: str, user_id: str, telegram_message_id: int,
                              message_id: int) -> bool:
        """Is this message the user's answer to a bot command that is asking them something.

        A ``/spbot`` command that asks (a value, a choice, a password) takes the user's next plain
        message in that chat. In a private chat every message goes to the assistant, so without
        this a typed password would be sent to the model as a question (2026-11-04, private chats).
        It is the bot's while a question is open for that user there, and once the bot took it.

        Open means a ``waiting`` conversation state - the only thing the bot reads an answer
        through (``fetch_waiting_telegram_conversation_states``). A workflow step's own ``active``
        is not: a command left half-answered keeps it for ever, and the first private question on
        the worker (2026-11-04) was held by two steps abandoned on 2026-10-06 and 2026-10-08."""
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM telegram_conversation_states WHERE chat_id = ? AND user_id = ? "
                "AND ((status = 'waiting' AND wait_after_message_id < ?) OR consumed_telegram_message_id = ?) "
                "UNION ALL SELECT 1 FROM telegram_workflow_steps WHERE chat_id = ? AND user_id = ? "
                "AND answer_telegram_message_id = ? LIMIT 1",
                (str(chat_id), str(user_id), int(message_id), int(telegram_message_id),
                 str(chat_id), str(user_id), int(telegram_message_id))).fetchone()
        return row is not None

    def request_by_message(self, *, chat_id: str, message_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM ai_requests WHERE chat_id = ? AND message_id = ?",
                               (str(chat_id), int(message_id))).fetchone()
        return dict(row) if row is not None else None

    def request(self, ai_request_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM ai_requests WHERE ai_request_id = ?", (int(ai_request_id),)).fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------ #
    # What was used
    # ------------------------------------------------------------------ #
    #: A running question counts what it holds; a finished one what it used; a refused one nothing.
    _TOKENS = "CASE WHEN status = 'running' THEN est_tokens ELSE total_tokens END"
    _COST = "CASE WHEN status = 'running' THEN est_cost_usd ELSE cost_usd END"

    def used(self, *, day_start: str, month_start: str, user_id: str | None = None,
             exclude_request_id: int | None = None) -> dict[str, Any]:
        """Tokens, money and questions since the day and the month began - the system's, or one
        user's - leaving out the question being decided, which is not counted against itself."""
        where, binds = ("kind = 'question' AND status <> 'blocked'", [])
        if user_id is not None:
            where += " AND user_id = ?"
            binds.append(str(user_id))
        if exclude_request_id is not None:
            where += " AND ai_request_id <> ?"
            binds.append(int(exclude_request_id))
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(SUM(CASE WHEN started_at >= ? THEN {self._TOKENS} ELSE 0 END), 0) AS day_tokens, "
                f"COALESCE(SUM({self._TOKENS}), 0) AS month_tokens, "
                "COALESCE(SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END), 0) AS day_requests, "
                f"COALESCE(SUM(CASE WHEN started_at >= ? THEN {self._COST} ELSE 0 END), 0) AS day_cost, "
                f"COALESCE(SUM({self._COST}), 0) AS month_cost "
                f"FROM ai_requests WHERE {where} AND started_at >= ?",
                (day_start, day_start, day_start, *binds, month_start)).fetchone()
        return {"day_tokens": int(row["day_tokens"] or 0), "month_tokens": int(row["month_tokens"] or 0),
                "day_requests": int(row["day_requests"] or 0), "day_cost": float(row["day_cost"] or 0),
                "month_cost": float(row["month_cost"] or 0)}

    def recent(self, *, user_id: str, window_since: str, duplicate_since: str, fingerprint: str,
               exclude_request_id: int) -> dict[str, Any]:
        """One user's last question, the questions inside the rate window, and an earlier copy of
        this one inside the duplicate window."""
        self.initialize()
        with self.connect() as conn:
            last = conn.execute(
                "SELECT MAX(started_at) AS last_at FROM ai_requests WHERE user_id = ? AND kind = 'question' "
                "AND status <> 'blocked' AND ai_request_id <> ?", (str(user_id), int(exclude_request_id))).fetchone()
            window = conn.execute(
                "SELECT COUNT(*) AS n FROM ai_requests WHERE user_id = ? AND kind = 'question' "
                "AND status <> 'blocked' AND started_at >= ? AND ai_request_id <> ?",
                (str(user_id), window_since, int(exclude_request_id))).fetchone()
            duplicate = conn.execute(
                # A refused copy does not count: asked again after a cooldown, the question runs.
                "SELECT MIN(ai_request_id) AS first_id FROM ai_requests WHERE user_id = ? AND kind = 'question' "
                "AND status <> 'blocked' AND fingerprint = ? AND started_at >= ? AND ai_request_id <> ?",
                (str(user_id), fingerprint, duplicate_since, int(exclude_request_id))).fetchone()
        return {"last_started_at": last["last_at"] if last else None,
                "window_requests": int(window["n"] or 0) if window else 0,
                "duplicate_of": int(duplicate["first_id"]) if duplicate and duplicate["first_id"] else None}

    # ------------------------------------------------------------------ #
    # Conversations
    # ------------------------------------------------------------------ #
    def open_conversation(self, *, chat_id: str, user_id: str, user_name: str, title: str, model: str) -> int:
        now = utc_now_text()
        # To the microsecond: two in one second for one user (a /new, then a question) would
        # otherwise share the natural key.
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.initialize()
        with self.connect() as conn:
            return _insert(conn, "ai_conversations", _known("ai_conversations", {
                "chat_id": str(chat_id), "user_id": str(user_id), "user_name": user_name, "title": title[:200],
                "model": model, "created_at": created, "updated_at": now}))

    def latest_open_conversation(self, *, chat_id: str, user_id: str, updated_since: str) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_conversations WHERE chat_id = ? AND user_id = ? AND closed_at IS NULL "
                "AND updated_at >= ? ORDER BY updated_at DESC, ai_conversation_id DESC LIMIT 1",
                (str(chat_id), str(user_id), updated_since)).fetchone()
        return dict(row) if row is not None else None

    def conversation_of_reply(self, *, chat_id: str, message_id: int) -> int | None:
        """The conversation whose answer this Telegram message is - a reply to it continues it."""
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT r.ai_conversation_id FROM telegram_send_messages s JOIN ai_requests r "
                "ON s.source_type = 'ai_requests' AND s.source_id = CAST(r.ai_request_id AS TEXT) "
                "WHERE s.tlgchat_id = ? AND s.message_id = ? AND r.ai_conversation_id IS NOT NULL",
                (str(chat_id), int(message_id))).fetchone()
        return int(row["ai_conversation_id"]) if row is not None else None

    def conversation(self, ai_conversation_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM ai_conversations WHERE ai_conversation_id = ?",
                               (int(ai_conversation_id),)).fetchone()
        return dict(row) if row is not None else None

    def close_conversations(self, *, chat_id: str, user_id: str, reason: str,
                            ai_conversation_id: int | None = None) -> int:
        if reason not in CLOSE_REASONS:
            raise AiAssistStoreError(f"closed_reason must be one of {', '.join(CLOSE_REASONS)}.")
        self.initialize()
        sql = ("UPDATE ai_conversations SET closed_at = ?, closed_reason = ? WHERE chat_id = ? AND user_id = ? "
               "AND closed_at IS NULL")
        binds: list[Any] = [utc_now_text(), reason, str(chat_id), str(user_id)]
        if ai_conversation_id is not None:
            sql += " AND ai_conversation_id = ?"
            binds.append(int(ai_conversation_id))
        with self.connect() as conn:
            cursor = conn.execute(sql, tuple(binds))
            return int(getattr(cursor, "rowcount", 0) or 0)

    def touch_conversation(self, ai_conversation_id: int) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute("UPDATE ai_conversations SET updated_at = ? WHERE ai_conversation_id = ?",
                         (utc_now_text(), int(ai_conversation_id)))

    def conversation_questions(self, ai_conversation_id: int) -> int:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM ai_requests WHERE ai_conversation_id = ? "
                               "AND kind = 'question' AND status <> 'blocked'", (int(ai_conversation_id),)).fetchone()
        return int(row["n"] or 0)

    def history(self, ai_conversation_id: int) -> list[dict[str, Any]]:
        """The conversation as the model takes it, in order."""
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute("SELECT role, content_json FROM ai_messages WHERE ai_conversation_id = ? ORDER BY seq",
                                (int(ai_conversation_id),)).fetchall()
        return [{"role": row["role"], "content": json.loads(row["content_json"])} for row in rows]

    def add_messages(self, *, ai_conversation_id: int, ai_request_id: int, messages: list[dict[str, Any]]) -> None:
        """Append a question's messages unchanged, after what the conversation holds."""
        if not messages:
            return
        self.initialize()
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), -1) AS last FROM ai_messages WHERE ai_conversation_id = ?",
                               (int(ai_conversation_id),)).fetchone()
            seq = int(row["last"]) + 1
            for message in messages:
                role = str(message.get("role"))
                if role not in ROLES:
                    raise AiAssistStoreError(f"ai_messages.role must be one of {', '.join(ROLES)}; got {role!r}.")
                conn.execute("INSERT INTO ai_messages (ai_conversation_id, ai_request_id, seq, role, content_json) "
                             "VALUES (?, ?, ?, ?, ?)",
                             (int(ai_conversation_id), int(ai_request_id), seq, role, _json(message.get("content"))))
                seq += 1

    # ------------------------------------------------------------------ #
    # Calls and tools
    # ------------------------------------------------------------------ #
    def record_api_call(self, **values: Any) -> int:
        values = _known("ai_api_calls", values)
        for name in ("stop_details_json", "request_meta_json", "response_json"):
            if name in values and not isinstance(values[name], str):
                values[name] = _json(values[name])
        self.initialize()
        with self.connect() as conn:
            return _insert(conn, "ai_api_calls", values)

    def record_tool_run(self, **values: Any) -> int:
        values = _known("ai_tool_runs", values)
        if "input_json" in values and not isinstance(values["input_json"], str):
            values["input_json"] = _json(values["input_json"])
        self.initialize()
        with self.connect() as conn:
            return _insert(conn, "ai_tool_runs", values)

    # ------------------------------------------------------------------ #
    # Reading back
    # ------------------------------------------------------------------ #
    def usage_report(self, *, since: str, until: str, group_by: str) -> list[dict[str, Any]]:
        """Questions, tokens and money between two instants, by ``day``, ``user`` or ``model``."""
        keys = {"day": "SUBSTR(started_at, 1, 10)", "user": "user_id || ' ' || user_name", "model": "model"}
        if group_by not in keys:
            raise AiAssistStoreError(f"group_by must be one of {', '.join(keys)}.")
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {keys[group_by]} AS bucket, "
                "SUM(CASE WHEN status <> 'blocked' THEN 1 ELSE 0 END) AS questions, "
                "SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS refused, "
                "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors, "
                "COALESCE(SUM(total_tokens), 0) AS tokens, COALESCE(SUM(cache_read_tokens), 0) AS cache_read, "
                "COALESCE(SUM(cost_usd), 0) AS cost_usd, COALESCE(SUM(tool_calls), 0) AS tool_calls "
                "FROM ai_requests WHERE kind = 'question' AND started_at >= ? AND started_at < ? "
                f"GROUP BY {keys[group_by]} ORDER BY 1", (since, until)).fetchall()
        return [dict(row) for row in rows]

    def usage_since(self, *, days: int, group_by: str, now: datetime | None = None) -> dict[str, Any]:
        """The last ``days`` days of :meth:`usage_report`, with its totals. ``telegram ai-usage`` and the
        console's *AI assistant* page both read this, so the two cannot add the same rows up differently."""
        stamp = "%Y-%m-%dT%H:%M:%SZ"
        until = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        since = until - timedelta(days=max(int(days), 1))
        rows = self.usage_report(since=since.strftime(stamp),
                                 until=(until + timedelta(seconds=1)).strftime(stamp), group_by=group_by)
        return {"since": since.strftime(stamp), "until": until.strftime(stamp), "by": group_by, "rows": rows,
                "total_cost_usd": round(sum(float(row["cost_usd"] or 0) for row in rows), 6),
                "total_tokens": sum(int(row["tokens"] or 0) for row in rows)}

    def recent_requests(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """The newest requests first, one line each - questions, group words and refusals alike, since a
        refusal is the row an operator most often comes looking for."""
        self.initialize()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT ai_request_id, ai_conversation_id, chat_id, user_id, user_name, kind, status, reason, "
                "model, question, total_tokens, cost_usd, turns, tool_calls, stop_reason, started_at, finished_at "
                "FROM ai_requests ORDER BY ai_request_id DESC LIMIT ? OFFSET ?",
                (max(int(limit), 1), max(int(offset), 0))).fetchall()
        return [dict(row) for row in rows]

    def transcript(self, ai_request_id: int) -> dict[str, Any]:
        """One question in full: its row, its messages, every API call and every tool run."""
        self.initialize()
        with self.connect() as conn:
            request = conn.execute("SELECT * FROM ai_requests WHERE ai_request_id = ?", (int(ai_request_id),)).fetchone()
            if request is None:
                raise AiAssistStoreError(f"no AI request {ai_request_id}.")
            messages = conn.execute("SELECT seq, role, content_json, created_at FROM ai_messages "
                                    "WHERE ai_request_id = ? ORDER BY seq", (int(ai_request_id),)).fetchall()
            calls = conn.execute("SELECT * FROM ai_api_calls WHERE ai_request_id = ? ORDER BY turn_no",
                                 (int(ai_request_id),)).fetchall()
            tools = conn.execute("SELECT * FROM ai_tool_runs WHERE ai_request_id = ? ORDER BY ai_tool_run_id",
                                 (int(ai_request_id),)).fetchall()
        return {"request": dict(request),
                "messages": [{**dict(row), "content": json.loads(row["content_json"])} for row in messages],
                "api_calls": [dict(row) for row in calls], "tool_runs": [dict(row) for row in tools]}
