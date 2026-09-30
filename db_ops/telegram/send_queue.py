from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from db_ops.lib.rows import row_value
from db_ops.db import DbOpsStore
from db_ops.logging_ops import log_event
from db_ops.telegram.api import (
    DEFAULT_RATE_LIMIT_WAIT_SECONDS,
    MAX_RATE_LIMIT_WAIT_SECONDS,
    TelegramRateLimited,
    send_document,
    send_message,
)


#: How many of each chat's oldest unsent rows one pass takes (0.25.0, the operator's number). The
#: pass runs every second and Telegram takes about 20 messages a minute into one group, so five a
#: pass is a group's whole minute in four passes - and the next chat never waits for it. Until
#: 0.25.0 a pass took the oldest 50 rows of the whole queue: on 2026-09-28 one lab chat's ~46,000
#: alerts held every other chat behind them, the bot's replies to its own commands included.
#: `send_per_chat` in `telegram_config.json` overrides it.
SEND_PER_CHAT = 5

#: How many chats one pass sends to at once (0.25.0, the operator's number). A chat's own rows still
#: go one after another, in order - only chats run side by side. One at a time, a pass that met a
#: backlog in six chats sent 30 rows at ~1.3 s each (a fresh HTTPS call to Telegram, three store
#: round trips) and took ~40 s, and the bot read its commands once per pass: a `/spbot_self_status`
#: reply took a minute (2026-09-29). There is no time budget of its own: the pass is bounded by the
#: Telegram workflow's `time_window.timeout` in `app_commands.json`, like every other app.
#: `send_threads` in `telegram_config.json` overrides it.
SEND_THREADS = 10

#: Where a chat's Telegram-imposed pause is kept between passes - each pass is a process of its
#: own, and a pause that died with the process would be spent on another refused call every
#: second. Seconds of state: losing the file costs one 429 per chat, nothing else.
PAUSES_FILE_NAME = "telegram_chat_pauses.json"


def send_pending_messages(
    *,
    sqlite_path: str | Path,
    bot_token: str,
    api_url: str = "https://api.telegram.org",
    timeout_seconds: int = 20,
    limit: int = 50,
    retry_count: int = 3,
    send_per_chat: int = SEND_PER_CHAT,
    send_threads: int = SEND_THREADS,
    pauses_path: str | Path | None = None,
    now: Any = time.time,
) -> dict[str, int]:
    """One send pass: each chat's oldest ``send_per_chat`` rows, up to ``send_threads`` chats at once.

    Within a chat the rows go one after another, oldest first, each marked, sent and updated on its
    own (rules R29) - a chat's messages must arrive in the order they were written. Across chats
    nothing is shared but the store, so they run side by side.

    A 429 pauses **that chat** for the seconds Telegram asked: the row goes back to the queue
    unchanged, the chat's remaining rows wait with it, and a later pass takes them once the pause is
    over. Sleeping on it here is what made a pass take 143 s on average while one chat was flooded
    (2026-09-28) - every other chat, and the bot reading its commands, waited out another chat's
    limit.
    """
    store = DbOpsStore(sqlite_path)
    rows = store.fetch_pending_telegram_send_messages(limit=limit, per_chat=send_per_chat)
    pauses = read_chat_pauses(pauses_path, now=now())
    counts = {
        "read": len(rows),
        "sent": 0,
        "failed": 0,
        "deferred": 0,
        "paused_chats": 0,
    }

    by_chat: dict[str, list[Any]] = {}
    for row in rows:
        by_chat.setdefault(str(row["tlgchat_id"] or ""), []).append(row)

    logger = logging.getLogger("telegram")

    def send_chat(chat_id: str, chat_rows: list[Any]) -> tuple[dict[str, int], float | None]:
        """One chat's rows in order; stops at the first 429 and says how long to pause."""
        chat_counts = {"sent": 0, "failed": 0, "deferred": 0}
        if chat_id in pauses:
            chat_counts["deferred"] = len(chat_rows)
            return chat_counts, None
        for index, row in enumerate(chat_rows):
            result = send_one_message(
                sqlite_path=sqlite_path,
                send_tlgmsg_id=int(row["send_tlgmsg_id"]),
                bot_token=bot_token,
                api_url=api_url,
                timeout_seconds=timeout_seconds,
                retry_count=retry_count,
                wait_on_rate_limit=False,
            )
            if result.get("status") == "rate_limited":
                wait = float(result.get("retry_after") or DEFAULT_RATE_LIMIT_WAIT_SECONDS)
                chat_counts["deferred"] += len(chat_rows) - index
                log_event(logger, level="warning", message=(
                    f"telegram.send_queue chat {chat_id} paused {wait:.0f}s by Telegram's rate limit; "
                    f"its rows stay queued (send_tlgmsg_id={result['send_tlgmsg_id']} first)"))
                return chat_counts, wait
            if result["sent"] == 1:
                chat_counts["sent"] += 1
            elif result["failed"] == 1:
                chat_counts["failed"] += 1
        return chat_counts, None

    if by_chat:
        workers = max(1, min(int(send_threads or 1), len(by_chat)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="telegram-send") as pool:
            futures = {chat_id: pool.submit(send_chat, chat_id, chat_rows)
                       for chat_id, chat_rows in by_chat.items()}
            for chat_id, future in futures.items():
                chat_counts, pause = future.result()
                for key, value in chat_counts.items():
                    counts[key] += value
                if pause is not None:
                    pauses[chat_id] = now() + pause

    counts["paused_chats"] = len(pauses)
    write_chat_pauses(pauses_path, pauses)
    return counts


def read_chat_pauses(path: str | Path | None, *, now: float) -> dict[str, float]:
    """``{chat_id: epoch it may be sent to again}`` for the pauses still running, else ``{}``.

    A missing, empty or unreadable file is no pause at all - the worst that costs is one 429.
    """
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    pauses: dict[str, float] = {}
    for chat_id, until in raw.items():
        try:
            until_epoch = float(until)
        except (TypeError, ValueError):
            continue
        if until_epoch > now:
            pauses[str(chat_id)] = until_epoch
    return pauses


def write_chat_pauses(path: str | Path | None, pauses: dict[str, float]) -> None:
    """Keep the running pauses for the next pass; no file, no pause."""
    if not path:
        return
    target = Path(path)
    try:
        if not pauses:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_suffix(target.suffix + ".tmp")
        staged.write_text(json.dumps(pauses, sort_keys=True), encoding="utf-8")
        staged.replace(target)
    except OSError:
        # A pause that cannot be kept is one extra 429 next pass - never a reason to fail one.
        pass


def send_one_message(
    *,
    sqlite_path: str | Path,
    send_tlgmsg_id: int,
    bot_token: str,
    api_url: str = "https://api.telegram.org",
    timeout_seconds: int = 20,
    retry_count: int = 3,
    wait_on_rate_limit: bool = True,
) -> dict[str, int | str]:
    """Send one queued row. ``wait_on_rate_limit=False`` hands a 429 straight back (the send pass,
    which runs again a second later and pauses only that chat); the default waits it out, as the
    `send-one` command always has."""
    store = DbOpsStore(sqlite_path)
    row = store.fetch_telegram_send_message(send_tlgmsg_id=send_tlgmsg_id)
    if row is None:
        return {"send_tlgmsg_id": send_tlgmsg_id, "sent": 0, "failed": 1, "status": "not_found"}

    if int(row["send_status"]) != 0:
        return {"send_tlgmsg_id": send_tlgmsg_id, "sent": 0, "failed": 0, "status": "not_pending"}

    store.mark_telegram_send_message_processing(send_tlgmsg_id=send_tlgmsg_id)
    # Rows written before the column existed, and any producer that has not adopted it, simply
    # have nothing here — the send layer then falls back to reading the message header.
    message_type = row_value(row, "message_type")
    last_error = ""
    # Which kind of failure the last attempt was decides what happens to the row: a refusal is
    # terminal, a rate limit is a deferral.
    rate_limited = False
    retry_after = 0.0
    for _ in range(max(1, retry_count)):
        try:
            metadata = metadata_from_json(str(row["metadata_json"] or "{}"))
            document_path = str(metadata.get("document_path") or "").strip()
            if document_path:
                result = send_document(
                    bot_token=bot_token,
                    chat_id=str(row["tlgchat_id"] or ""),
                    document_path=document_path,
                    caption=str(row["message_text"] or ""),
                    api_url=api_url,
                    timeout_seconds=max(timeout_seconds, int(metadata.get("timeout_seconds") or 60)),
                    reply_to_message_id=int(row["reply_message_id"]) if row["reply_message_id"] is not None else None,
                )
            else:
                result = send_message(
                    bot_token=bot_token,
                    chat_id=str(row["tlgchat_id"] or ""),
                    text=str(row["message_text"] or ""),
                    api_url=api_url,
                    timeout_seconds=timeout_seconds,
                    reply_to_message_id=int(row["reply_message_id"]) if row["reply_message_id"] is not None else None,
                    reply_markup=reply_markup_from_metadata(metadata),
                    message_type=message_type,
                    wait_before_first_part=wait_on_rate_limit,
                )
            message_id = extract_sent_message_id(result)
            store.mark_telegram_send_message_sent(
                send_tlgmsg_id=send_tlgmsg_id,
                message_id=message_id,
            )
            return {"send_tlgmsg_id": send_tlgmsg_id, "sent": 1, "failed": 0, "status": "sent"}
        except TelegramRateLimited as exc:
            # The one failure that is not a fault. Retrying it the way every other error is
            # retried - immediately, three times - spent all three attempts inside the same
            # second and threw the row away over a limit that had asked for a short pause.
            last_error = str(exc)
            rate_limited = True
            retry_after = float(exc.retry_after)
            if not wait_on_rate_limit:
                break
            time.sleep(min(exc.retry_after, float(MAX_RATE_LIMIT_WAIT_SECONDS)))
        except Exception as exc:  # noqa: BLE001 - retry path.
            last_error = str(exc)
            rate_limited = False

    # Whatever happens next is logged. The store already held the failure and nothing read it
    # back: on 2026-09-09 a report lost parts to a 429 and it appeared in no log file at all, the
    # only record being `metadata_json` on this row. A message nobody received is a message that
    # did not happen, so the loss belongs where an operator reads failures.
    logger = logging.getLogger("telegram")
    attempts = max(1, retry_count)
    if rate_limited:
        # Telegram did not refuse this message, it asked for a pause - so the row goes back to
        # pending rather than to a terminal -1. Marking it failed is what silently dropped parts
        # of a report whose only problem was arriving too fast.
        store.reset_telegram_send_message_pending(
            send_tlgmsg_id=send_tlgmsg_id, fail_text=last_error)
        if wait_on_rate_limit:
            # Without the wait, the send pass logs the chat's pause once instead - one line per
            # pause, not one per row that met it.
            log_event(logger, level="warning", message=(
                f"telegram.send_queue rate limited after {attempts} attempt(s), requeued: "
                f"send_tlgmsg_id={send_tlgmsg_id} chat={row['tlgchat_id']} error={last_error}"))
        return {"send_tlgmsg_id": send_tlgmsg_id, "sent": 0, "failed": 0, "status": "rate_limited",
                "retry_after": retry_after}

    store.mark_telegram_send_message_failed(
        send_tlgmsg_id=send_tlgmsg_id,
        fail_text=last_error,
    )
    log_event(logger, level="error", message=(
        f"telegram.send_queue delivery FAILED after {attempts} attempt(s): "
        f"send_tlgmsg_id={send_tlgmsg_id} chat={row['tlgchat_id']} error={last_error}"))
    return {"send_tlgmsg_id": send_tlgmsg_id, "sent": 0, "failed": 1, "status": "failed"}


# `row_value` is `db_ops.lib.rows.row_value` since 2026-08-16. It was the third near-copy of
# 'read a column the row may not have' and the only one that did not catch TypeError, so a
# tuple row raised here and nowhere else.


def extract_sent_message_id(result: dict[str, Any]) -> int | None:
    message = result.get("result") or {}
    message_id = message.get("message_id")
    return int(message_id) if message_id is not None else None


def metadata_from_json(metadata_json: str) -> dict[str, Any]:
    try:
        metadata = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return metadata if isinstance(metadata, dict) else {}


def reply_markup_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    if metadata.get("force_reply") is True:
        return {"force_reply": True, "selective": True}
    reply_markup = metadata.get("reply_markup")
    return reply_markup if isinstance(reply_markup, dict) else None
