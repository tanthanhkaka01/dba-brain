"""``DbOpsStore``'s methods for the Telegram tables: messages, conversations, workflow steps, background tasks, and the send queue.

Split out of ``db/store.py`` on 2026-10-03 (see ``db/store_base.py``). A mixin, not a store:
``DbOpsStore`` composes it, and its methods use the store's own ``connect`` and ``initialize``.
"""

from __future__ import annotations

import json
import sqlite3
import getpass
import socket
from typing import Any
from datetime import datetime, timedelta, timezone

from db_ops.lib.rows import row_value
from db_ops.db.store_base import utc_now_text
from db_ops.db.store_schema import _redacted_raw_json


class TelegramMixin:

    def upsert_telegram_messages(self, messages: list[dict]) -> int:
        self.initialize()
        if not messages:
            return 0

        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO telegram_messages
                (
                    update_id,
                    message_id,
                    message_date,
                    chat_id,
                    chat_type,
                    user_id,
                    text,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO NOTHING;
                """,
                [
                    (
                        item.get("update_id"),
                        item.get("message_id"),
                        item.get("message_date"),
                        item.get("chat_id", ""),
                        item.get("chat_type", ""),
                        item.get("user_id", ""),
                        item.get("text", ""),
                        json.dumps(item.get("raw") or {}, ensure_ascii=False, sort_keys=True),
                    )
                    for item in messages
                ],
            )
        return len(messages)

    def sync_telegram_command_messages(self, *, command_prefix: str = "/spbot") -> int:
        self.initialize()
        prefix = command_prefix.strip()
        if not prefix:
            raise ValueError("command_prefix is required.")

        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO telegram_command_messages
                (
                    telegram_message_id,
                    update_id,
                    message_id,
                    message_date,
                    chat_id,
                    chat_type,
                    user_id,
                    text,
                    command_prefix,
                    command_payload,
                    raw_json
                )
                SELECT
                    telegram_message_id,
                    update_id,
                    message_id,
                    message_date,
                    chat_id,
                    chat_type,
                    user_id,
                    text,
                    ?,
                    trim(substr(text, length(?) + 1)),
                    raw_json
                FROM telegram_messages
                WHERE text LIKE ? || '%'
                ON CONFLICT(chat_id, message_id) DO NOTHING;
                """,
                (prefix, prefix, prefix),
            )
        return int(cursor.rowcount)

    def fetch_pending_telegram_command_messages(self, limit: int = 50) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        telegram_command_message_id,
                        telegram_message_id,
                        update_id,
                        message_id,
                        message_date,
                        chat_id,
                        chat_type,
                        user_id,
                        text,
                        command_prefix,
                        command_payload,
                        command_id,
                        command_status,
                        reply_message_id,
                        processed_at,
                        raw_json,
                        claimed_at
                    FROM telegram_command_messages
                    WHERE command_status = 0
                    ORDER BY message_date ASC, telegram_command_message_id ASC
                    LIMIT ?;
                    """,
                    (limit,),
                )
            )

    def claim_telegram_conversation_state(
        self,
        *,
        state_id: int,
        claimed_at: str,
        stale_before: str,
    ) -> bool:
        """Exclusive ownership of a waiting conversation state, for the same reason as
        :meth:`claim_telegram_command_message`: the state stays 'waiting' until its action
        finishes, so overlapping workflow cycles would each act on the same user reply."""
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_conversation_states
                SET claimed_at = ?
                WHERE state_id = ?
                  AND status = 'waiting'
                  AND (claimed_at IS NULL OR claimed_at < ?);
                """,
                (claimed_at, state_id, stale_before),
            )
            return cursor.rowcount == 1

    def claim_telegram_command_message(
        self,
        *,
        telegram_command_message_id: int,
        claimed_at: str,
        stale_before: str,
    ) -> bool:
        """Take exclusive ownership of a pending command message. True = this caller owns it.

        The claim is a single conditional UPDATE, so of two Telegram workflow cycles racing for
        the same message exactly one wins and the other skips it. ``stale_before`` re-opens a
        message whose owner died before finishing (e.g. the container was restarted mid-run),
        so a crash costs one retry rather than the command disappearing."""
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_command_messages
                SET claimed_at = ?
                WHERE telegram_command_message_id = ?
                  AND command_status = 0
                  AND (claimed_at IS NULL OR claimed_at < ?);
                """,
                (claimed_at, telegram_command_message_id, stale_before),
            )
            return cursor.rowcount == 1

    def fetch_telegram_command_message(
        self,
        *,
        telegram_command_message_id: int,
    ) -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    telegram_command_message_id,
                    telegram_message_id,
                    update_id,
                    message_id,
                    message_date,
                    chat_id,
                    chat_type,
                    user_id,
                    text,
                    command_prefix,
                    command_payload,
                    command_id,
                    command_status,
                    reply_message_id,
                    processed_at,
                    raw_json
                FROM telegram_command_messages
                WHERE telegram_command_message_id = ?;
                """,
                (telegram_command_message_id,),
            ).fetchone()

    def update_telegram_command_message_status(
        self,
        *,
        telegram_command_message_id: int,
        command_status: int,
        process_note: str = "",
        command_id: int | None = None,
        reply_message_id: int | None = None,
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_command_messages
                SET
                    command_status = ?,
                    command_id = COALESCE(?, command_id),
                    reply_message_id = COALESCE(?, reply_message_id),
                    processed_at = ?,
                    process_note = ?
                WHERE telegram_command_message_id = ?;
                """,
                (
                    command_status,
                    command_id,
                    reply_message_id,
                    utc_now_text(),
                    process_note,
                    telegram_command_message_id,
                ),
            )

    def upsert_telegram_conversation_state(
        self,
        *,
        chat_id: str,
        user_id: str,
        command_id: int,
        command_text: str,
        state_key: str,
        wait_after_message_id: int,
        source_telegram_command_message_id: int | None = None,
        state_data: dict | None = None,
    ) -> int:
        self.initialize()
        state_json = json.dumps(state_data or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_conversation_states
                SET status = 'replaced',
                    updated_at = ?
                WHERE chat_id = ?
                  AND user_id = ?
                  AND status = 'waiting';
                """,
                (utc_now_text(), chat_id, user_id),
            )
            cursor = conn.execute(
                """
                INSERT INTO telegram_conversation_states
                (
                    chat_id,
                    user_id,
                    command_id,
                    command_text,
                    state_key,
                    status,
                    wait_after_message_id,
                    source_telegram_command_message_id,
                    state_json
                )
                VALUES (?, ?, ?, ?, ?, 'waiting', ?, ?, ?);
                """,
                (
                    chat_id,
                    user_id,
                    command_id,
                    command_text,
                    state_key,
                    wait_after_message_id,
                    source_telegram_command_message_id,
                    state_json,
                ),
            )
            return int(cursor.lastrowid)

    def fetch_waiting_telegram_conversation_states(self, limit: int = 50) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        state_id,
                        chat_id,
                        user_id,
                        command_id,
                        command_text,
                        state_key,
                        status,
                        wait_after_message_id,
                        source_telegram_command_message_id,
                        state_json,
                        created_at,
                        updated_at
                    FROM telegram_conversation_states
                    WHERE status = 'waiting'
                    ORDER BY created_at ASC, state_id ASC
                    LIMIT ?;
                    """,
                    (limit,),
                )
            )

    def fetch_next_telegram_message_for_state(self, *, chat_id: str, user_id: str, after_message_id: int) -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    telegram_message_id,
                    update_id,
                    message_id,
                    message_date,
                    chat_id,
                    chat_type,
                    user_id,
                    text,
                    raw_json
                FROM telegram_messages
                WHERE chat_id = ?
                  AND user_id = ?
                  AND message_id > ?
                  AND (
                        trim(COALESCE(text, '')) <> ''
                        OR json_extract(raw_json, '$.document.file_id') IS NOT NULL
                      )
                  AND COALESCE(text, '') NOT LIKE '/spbot%'
                ORDER BY message_id ASC
                LIMIT 1;
                """,
                (chat_id, user_id, after_message_id),
            ).fetchone()

    def update_telegram_conversation_state(
        self,
        *,
        state_id: int,
        status: str,
        state_data: dict | None = None,
        consumed_telegram_message_id: int | None = None,
        note: str = "",
    ) -> None:
        self.initialize()
        state_json = json.dumps(state_data or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_conversation_states
                SET
                    status = ?,
                    consumed_telegram_message_id = COALESCE(?, consumed_telegram_message_id),
                    state_json = ?,
                    note = ?,
                    updated_at = ?
                WHERE state_id = ?;
                """,
                (status, consumed_telegram_message_id, state_json, note, utc_now_text(), state_id),
            )

    # ------------------------------------------------------------------ #
    # Workflow step trail — what was asked, what came back, what is live
    # ------------------------------------------------------------------ #
    def start_telegram_workflow_step(
        self,
        *,
        run_key: str,
        chat_id: str,
        user_id: str,
        command_id: int,
        command_text: str,
        parameter_name: str,
        parameter_position: int,
        prompt_text: str = "",
        options: list | None = None,
        controls: list | None = None,
        is_secret: bool = False,
        state_id: int | None = None,
        prompt_send_tlgmsg_id: int | None = None,
    ) -> int:
        """Record that a step has been asked, and make it the live one.

        Any step of this run still marked ``active`` is closed as ``abandoned`` first. Nothing
        should normally leave one behind — every path here resolves its step — but a daemon killed
        between the answer and the next prompt would, and two live steps in one run is a state the
        table must not be able to hold.

        ``step_no`` is assigned here rather than by the caller: it is the order the questions were
        actually asked in, which after a Back is not the order of the step definitions.
        """
        self.initialize()
        asked_at = utc_now_text()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_workflow_steps
                SET status = 'abandoned', answered_at = ?
                WHERE run_key = ? AND status = 'active';
                """,
                (asked_at, str(run_key)),
            )
            row = conn.execute(
                "SELECT MAX(step_no) AS last_step FROM telegram_workflow_steps WHERE run_key = ?;",
                (str(run_key),),
            ).fetchone()
            last_step = 0
            if row is not None:
                last_step = int(row_value(row, "last_step") or 0)
            cursor = conn.execute(
                """
                INSERT INTO telegram_workflow_steps
                (run_key, chat_id, user_id, command_id, command_text, step_no, parameter_name,
                 parameter_position, status, prompt_text, options_json, controls_json,
                 is_secret, state_id, prompt_send_tlgmsg_id, asked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    str(run_key), str(chat_id), str(user_id), int(command_id), str(command_text),
                    last_step + 1, str(parameter_name), int(parameter_position), str(prompt_text),
                    json.dumps(options or [], ensure_ascii=False),
                    json.dumps(controls or [], ensure_ascii=False),
                    1 if is_secret else 0, state_id, prompt_send_tlgmsg_id, asked_at,
                ),
            )
            return int(cursor.lastrowid)

    def redact_telegram_message(self, *, telegram_message_id: int, replacement: str = "***") -> int:
        """Replace one stored message's text - its row and the command copy of it - with ``replacement``.

        A password answered to the bot was masked in the step trail only; ``telegram_messages`` and
        ``telegram_command_messages`` kept it verbatim, in ``text`` and in ``raw_json``, readable
        from the console and any psql prompt (review 0.25.0, F8.4). Returns rows changed.
        """
        self.initialize()
        changed = 0
        with self.connect() as conn:
            for table in ("telegram_messages", "telegram_command_messages"):
                rows = conn.execute(
                    f"SELECT raw_json FROM {table} WHERE telegram_message_id = ?;",
                    (int(telegram_message_id),)).fetchall()
                for row in rows:
                    raw = _redacted_raw_json(row["raw_json"], replacement)
                    cursor = conn.execute(
                        f"UPDATE {table} SET text = ?, raw_json = ? WHERE telegram_message_id = ?;",
                        (replacement, raw, int(telegram_message_id)))
                    changed += int(cursor.rowcount or 0)
        return changed

    def finish_telegram_workflow_step(
        self,
        *,
        run_key: str,
        status: str,
        answer_text: str | None = None,
        answer_kind: str | None = None,
        answer_telegram_message_id: int | None = None,
    ) -> int:
        """Close the live step of a run. Returns how many rows it closed (0 or 1).

        Addressed by ``run_key`` rather than by id because every caller already knows the run and
        would otherwise have to look the id up first — and because "the live step" is precisely
        what this table exists to make unambiguous.
        """
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_workflow_steps
                SET status = ?, answer_text = ?, answer_kind = ?,
                    answer_telegram_message_id = COALESCE(?, answer_telegram_message_id),
                    answered_at = ?
                WHERE run_key = ? AND status = 'active';
                """,
                (str(status), answer_text, answer_kind, answer_telegram_message_id,
                 utc_now_text(), str(run_key)),
            )
            return int(cursor.rowcount or 0)

    def fetch_telegram_workflow_steps(self, *, run_key: str) -> list:
        """Every step of one run, in the order it was asked."""
        self.initialize()
        with self.connect() as conn:
            return list(conn.execute(
                """
                SELECT workflow_step_id, run_key, chat_id, user_id, command_id, command_text,
                       step_no, parameter_name, parameter_position, status, prompt_text,
                       options_json, controls_json, answer_text, answer_kind, is_secret,
                       state_id, prompt_send_tlgmsg_id, answer_telegram_message_id,
                       asked_at, answered_at
                FROM telegram_workflow_steps
                WHERE run_key = ?
                ORDER BY step_no;
                """,
                (str(run_key),),
            ).fetchall())

    def fetch_active_telegram_workflow_step(self, *, run_key: str):
        """The step this run is waiting on, or ``None``."""
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT workflow_step_id, step_no, parameter_name, parameter_position, status,
                       prompt_text, options_json, controls_json, asked_at
                FROM telegram_workflow_steps
                WHERE run_key = ? AND status = 'active'
                ORDER BY step_no DESC;
                """,
                (str(run_key),),
            ).fetchone()

    def insert_telegram_background_task(
        self,
        *,
        chat_id: str,
        message_id: int | None,
        user_id: str,
        command_id: int,
        command_text: str,
        source_id: str,
        pid: int,
        stdout_path: str,
        stderr_path: str,
        task_data: dict | None = None,
    ) -> int:
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO telegram_background_tasks
                (chat_id, message_id, user_id, command_id, command_text, source_id,
                 pid, stdout_path, stderr_path, task_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    chat_id, message_id, user_id, command_id, command_text, source_id,
                    pid, stdout_path, stderr_path,
                    json.dumps(task_data or {}, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    def fetch_running_telegram_background_tasks(self) -> list[sqlite3.Row]:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT task_id, created_at, chat_id, message_id, user_id,
                       command_id, command_text, source_id, pid,
                       stdout_path, stderr_path, status, task_data
                FROM telegram_background_tasks
                WHERE status = 'running'
                ORDER BY task_id ASC;
                """
            ).fetchall()

    def complete_telegram_background_task(
        self,
        *,
        task_id: int,
        status: str,
        result_json: str | None = None,
    ) -> None:
        self.initialize()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE telegram_background_tasks
                SET status = ?, result_json = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE task_id = ?;
                """,
                (status, result_json, task_id),
            )

    def insert_telegram_send_message(
        self,
        *,
        tlgchat_id: str,
        message_text: str,
        reply_message_id: int | None = None,
        entities: str | None = None,
        note: str = "",
        source_type: str | None = None,
        source_id: str | None = None,
        metadata: dict | None = None,
        message_type: str | None = None,
    ) -> int:
        """Queue one outgoing Telegram message.

        Prefer :func:`db_ops.db.telegram_queue.queue_telegram_message` over calling this
        directly — it is the one entry point every app shares, and it is what keeps
        ``message_type`` consistent instead of each app inventing its own vocabulary.
        """
        self.initialize()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO telegram_send_messages
                (
                    tlgchat_id,
                    message_text,
                    entities,
                    note,
                    host,
                    os_user,
                    ip_address,
                    reply_message_id,
                    source_type,
                    source_id,
                    message_type,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    tlgchat_id,
                    message_text,
                    entities,
                    note,
                    socket.gethostname(),
                    getpass.getuser(),
                    "",
                    reply_message_id,
                    source_type,
                    source_id,
                    (str(message_type).strip().lower() or None) if message_type else None,
                    metadata_json,
                ),
            )
            return int(cursor.lastrowid)

    def fetch_pending_telegram_send_messages(
        self, limit: int = 50, *, per_chat: int | None = None,
    ) -> list[sqlite3.Row]:
        """The unsent rows one send pass takes, oldest first.

        ``per_chat`` takes the oldest *n* of **each** chat and interleaves them - every chat's first
        row, then every chat's second - so no chat waits behind another. Without it the pass took
        the oldest rows of the whole queue: on 2026-09-28 one chat's ~46,000 lab alerts held every
        other chat behind them, the bot's replies to its own commands included. ``limit`` still
        caps the pass.
        """
        self.initialize()
        columns = """
                        send_tlgmsg_id,
                        row_ins_date,
                        tlgchat_id,
                        list_tlguser_id,
                        message_text,
                        entities,
                        note,
                        host,
                        os_user,
                        ip_address,
                        send_status,
                        send_date,
                        message_id,
                        reply_message_id,
                        source_type,
                        source_id,
                        metadata_json,
                        message_type"""
        with self.connect() as conn:
            if per_chat is not None:
                return list(
                    conn.execute(
                        f"""
                        SELECT {columns}
                        FROM (
                            SELECT {columns},
                                ROW_NUMBER() OVER (
                                    PARTITION BY tlgchat_id
                                    ORDER BY row_ins_date ASC, send_tlgmsg_id ASC
                                ) AS chat_rank
                            FROM telegram_send_messages
                            WHERE send_status = 0
                        ) pending
                        WHERE chat_rank <= ?
                        ORDER BY chat_rank ASC, row_ins_date ASC, send_tlgmsg_id ASC
                        LIMIT ?;
                        """,
                        (max(1, int(per_chat)), limit),
                    )
                )
            return list(
                conn.execute(
                    f"""
                    SELECT {columns}
                    FROM telegram_send_messages
                    WHERE send_status = 0
                    ORDER BY row_ins_date ASC, send_tlgmsg_id ASC
                    LIMIT ?;
                    """,
                    (limit,),
                )
            )

    def fetch_telegram_send_message(self, *, send_tlgmsg_id: int) -> sqlite3.Row | None:
        self.initialize()
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    send_tlgmsg_id,
                    row_ins_date,
                    tlgchat_id,
                    list_tlguser_id,
                    message_text,
                    entities,
                    note,
                    host,
                    os_user,
                    ip_address,
                    send_status,
                    send_date,
                    message_id,
                    reply_message_id,
                    source_type,
                    source_id,
                    metadata_json,
                    -- The send layer decides the severity emoji from this. Leaving it out of the
                    -- SELECT made every row look like it declared nothing, so the emoji fell back
                    -- to guessing from the header — and a conversation prompt that merely
                    -- contains the word "timeout" went out tagged as a failure.
                    message_type
                FROM telegram_send_messages
                WHERE send_tlgmsg_id = ?;
                """,
                (send_tlgmsg_id,),
            ).fetchone()

    def mark_telegram_send_message_processing(self, *, send_tlgmsg_id: int) -> bool:
        """Take a pending row for sending. True when this caller took it.

        ``send_date`` is stamped here too: it is when the row went in flight, which is what
        :meth:`requeue_stale_telegram_send_messages` measures. The sent/failed updates overwrite it.
        """
        self.initialize()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_send_messages
                SET send_status = 2,
                    send_date = ?
                WHERE send_tlgmsg_id = ?
                  AND send_status = 0;
                """,
                (utc_now_text(), send_tlgmsg_id),
            )
            return cursor.rowcount == 1

    def requeue_stale_telegram_send_messages(self, *, older_than_seconds: int) -> int:
        """Put rows left in flight (``send_status = 2``) back in the queue. Returns how many.

        A row is in flight only while a send pass holds it, and a pass is a process the daemon kills
        at its timeout - with no chance to put the row back. Nothing else ever moves a row out of
        ``2``, so such a message was lost without a word. Past ``older_than_seconds`` no pass can
        still own it; it goes back to ``0``, accepting that Telegram may already have had it: a
        possible second copy beats a message nobody receives. A row with no ``send_date`` was put in
        flight before that was stamped, and is treated as stale.
        """
        self.initialize()
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=int(older_than_seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_send_messages
                SET send_status = 0,
                    send_date = NULL
                WHERE send_status = 2
                  AND (send_date IS NULL OR send_date < ?);
                """,
                (cutoff,),
            )
            return int(cursor.rowcount or 0)

    def mark_telegram_send_message_sent(self, *, send_tlgmsg_id: int, message_id: int | None) -> None:
        self.initialize()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT source_type, source_id
                FROM telegram_send_messages
                WHERE send_tlgmsg_id = ?;
                """,
                (send_tlgmsg_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE telegram_send_messages
                SET
                    send_status = 1,
                    send_date = ?,
                    message_id = ?
                WHERE send_tlgmsg_id = ?;
                """,
                (utc_now_text(), message_id, send_tlgmsg_id),
            )
            if row and row["source_type"] == "telegram_command_messages" and row["source_id"] and message_id:
                conn.execute(
                    """
                    UPDATE telegram_command_messages
                    SET reply_message_id = ?
                    WHERE telegram_command_message_id = ?;
                    """,
                    (message_id, int(row["source_id"])),
                )

    def mark_telegram_send_message_failed(self, *, send_tlgmsg_id: int, fail_text: str) -> None:
        """Mark a row failed, ``fail_text`` merged into its metadata.

        It replaced the whole object with ``{"fail_text": ...}``, dropping `document_path`,
        `reply_markup`, the level and the SQL task id - so a failed report row no longer said which
        file it had carried (review 0.25.0, B3.2), the very loss `reset_..._pending` says it avoids.
        """
        self.initialize()
        with self.connect() as conn:
            metadata = self._send_row_metadata(conn, send_tlgmsg_id)
            metadata["fail_text"] = fail_text
            metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
            conn.execute(
                """
                UPDATE telegram_send_messages
                SET
                    send_status = -1,
                    send_date = ?,
                    metadata_json = ?
                WHERE send_tlgmsg_id = ?;
                """,
                (utc_now_text(), metadata_json, send_tlgmsg_id),
            )

    @staticmethod
    def _send_row_metadata(conn: Any, send_tlgmsg_id: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT metadata_json FROM telegram_send_messages WHERE send_tlgmsg_id = ?;",
            (send_tlgmsg_id,),
        ).fetchone()
        if row is None:
            return {}
        try:
            parsed = json.loads(str(row["metadata_json"] or "{}"))
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def reset_telegram_send_message_pending(self, *, send_tlgmsg_id: int, fail_text: str,
                                            extra: dict[str, Any] | None = None) -> None:
        """Put a row back in the queue, keeping what it is.

        ``last_fail_text`` is **merged** into the existing metadata, not written over it. This
        method had no caller until the rate-limit deferral in `telegram.send_queue`, and it would
        have replaced the whole object: a re-queued row would have lost `document_path` and
        `reply_markup` and gone out the next cycle as a plain message with the caption as its
        body. A row that is coming back has to come back as itself.
        """
        self.initialize()
        with self.connect() as conn:
            metadata = self._send_row_metadata(conn, send_tlgmsg_id)
            metadata["last_fail_text"] = fail_text
            metadata.update(extra or {})
            conn.execute(
                """
                UPDATE telegram_send_messages
                SET
                    send_status = 0,
                    metadata_json = ?
                WHERE send_tlgmsg_id = ?;
                """,
                (json.dumps(metadata, ensure_ascii=False, sort_keys=True), send_tlgmsg_id),
            )

    def fetch_recent_telegram_command_messages(self, *, user_id: str, limit: int = 200) -> list:
        """One person's most recent dispatched commands, newest first, each with the last state
        of its prompt conversation.

        A command answered one question at a time is not one row: the message carries only what
        was typed before the first prompt (``/spbot_run_sql_task``), and the answers live in
        ``telegram_conversation_states``. The chain carries ``source_telegram_command_message_id``
        forward from prompt to prompt, so its *last* state holds every answer — that is the join,
        and without it the history would show the command stripped of everything the person said.

        Ordered by id rather than ``message_date``: the date is nullable, and the two engines
        disagree about where NULLs sort in a DESC order, so the id is the only ordering that
        means the same thing on both.
        """
        self.initialize()
        with self.connect() as conn:
            return list(conn.execute(
                """
                SELECT
                    command_message.telegram_command_message_id,
                    command_message.chat_id,
                    command_message.chat_type,
                    command_message.user_id,
                    command_message.message_date,
                    command_message.text,
                    command_message.command_prefix,
                    command_message.command_payload,
                    command_message.created_at,
                    conversation.status AS conversation_status,
                    conversation.command_text AS conversation_command_text,
                    conversation.state_json AS conversation_state_json,
                    conversation.updated_at AS conversation_updated_at
                FROM telegram_command_messages command_message
                LEFT JOIN telegram_conversation_states conversation
                    ON conversation.state_id = (
                        SELECT MAX(last_state.state_id)
                        FROM telegram_conversation_states last_state
                        WHERE last_state.source_telegram_command_message_id
                              = command_message.telegram_command_message_id
                    )
                WHERE command_message.command_status = 1
                  AND command_message.user_id = ?
                ORDER BY command_message.telegram_command_message_id DESC
                LIMIT ?;
                """,
                (str(user_id), int(limit)),
            ).fetchall())
