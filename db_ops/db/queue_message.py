"""This app's client for ``db_ops.db.cli queue-telegram-message``.

The app supplies data; ``common`` does the work. Everything the other side needs is in the
request - the chat, the rendered text, the message type's two halves, and the **store to write
to**, stated as connection details rather than as a config file to go and read. ``common`` looks
nothing up.

**One copy, in ``db``.** It was the same file in six apps, byte for byte, held identical by a
test - and even then the six near-copies had drifted into three variants before any of them
shipped, one having quietly lost ``reply_message_id``, the field a command reply needs. It lived
app-side because an app may not import another app and ``common`` may not run a CLI; neither
forces it into six folders now that ORD 01 owns both ends of it. Everything here is either the
``db.cli queue-telegram-message`` call or the ``db.telegram_queue`` fallback, so ``db`` is where
it belongs, and every app may import ``db``.

Even the Telegram app uses it. Exempting the app that owns ``telegram_send_messages`` would put
one writer on a different path from the others, which is the asymmetry the model removes.

The store block is what makes this possible: a subprocess cannot be handed a live store object, so
without a declaration a caller could only ever say "config.json" - and a test writing to a temp
store, or a run against a store that is not this node's own, had no way to say what it meant. See
:mod:`db_ops.db.declaration`.

The payload goes in on **stdin**, never argv: it carries the store password.

Falling back to the in-process call is deliberate. If the subprocess cannot deliver, the message is
still queued rather than lost - a notification that vanishes because a CLI would not launch is a
worse failure than one that took the short path, and the short path calls the very function the CLI
would have called.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from db_ops.lib import common_cli

_TIMEOUT_SECONDS = 60


def store_block(app_config: Any) -> dict[str, Any] | None:
    """This node's store, stated as data. ``None`` when it cannot be described.

    Never raises: describing the store resolves its password, and a secret store that cannot be
    read must not stop the message being queued - the caller falls back.
    """
    try:
        from db_ops.db.declaration import describe

        return describe(app_config)
    except Exception as exc:  # noqa: BLE001 - reported, then the caller takes the short path.
        print(f"store could not be described for queue-telegram-message: {exc}", file=sys.stderr)
        return None


def store_block_from(store: Any) -> dict[str, Any] | None:
    """The same, from a live store the caller already holds (no config needed)."""
    try:
        from db_ops.db.declaration import describe_store

        return describe_store(store)
    except Exception as exc:  # noqa: BLE001 - reported, then the caller takes the short path.
        print(f"store could not be described for queue-telegram-message: {exc}", file=sys.stderr)
        return None


def queue_message(request: dict[str, Any], *, fallback_store: Any = None) -> int | None:
    """Queue one outgoing message through the common CLI. Returns its id, or ``None``."""
    if not request.get("store"):
        # A store that cannot state its own connection cannot be named in a request, so
        # the CLI would write somewhere else entirely. Better to take the short path than
        # to send the row to the wrong database.
        return _queue_in_process(request, fallback_store)
    # The spawn is `lib.common_cli.spawn`, aimed at `db.cli`. What stays here is the only part
    # that is this module's own: **the fallback**. `lib` may not import `db`, so it cannot hold a
    # policy that ends in an in-process insert — and this module had its own copy of the twenty
    # lines around the subprocess until 2026-08-16, which is the same "spawn a db_ops CLI and read
    # JSON back" that already exists once.
    completed, error = common_cli.spawn(
        "queue-telegram-message", request,
        module="db_ops.db.cli", timeout_seconds=_TIMEOUT_SECONDS,
    )
    if completed is None:
        print(f"{error}; queueing in-process instead.", file=sys.stderr)
        return _queue_in_process(request, fallback_store)

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:300]
        print(f"queue-telegram-message exited {completed.returncode}: {detail}", file=sys.stderr)
        return _queue_in_process(request, fallback_store)
    try:
        answer = json.loads(completed.stdout.strip() or "{}")
    except ValueError:
        return None
    # The command answers in the response envelope since 2026-08-16, so the id is in `data`. The
    # older top level is still read: a worker running one deploy behind must not silently stop
    # returning ids, which would look like every message failing to queue.
    data = answer.get("data") if isinstance(answer.get("data"), dict) else answer
    return data.get("send_tlgmsg_id")


def _queue_in_process(request: dict[str, Any], fallback_store: Any = None) -> int | None:
    """The same function the CLI would have called, when the CLI itself could not deliver."""
    from db_ops.db.telegram_queue import queue_telegram_message
    from db_ops.db import DbOpsStore

    try:
        block = request.get("store")
        if fallback_store is not None:
            store = fallback_store
        elif block:
            from db_ops.db.declaration import parse as parse_store

            store = DbOpsStore(parse_store(block))
        else:
            from db_ops.config import load_config

            store = DbOpsStore.from_config(load_config())
        return queue_telegram_message(
            store=store,
            chat_id=str(request.get("chat_id") or ""),
            text=str(request.get("text") or ""),
            message_type=request.get("message_type"),
            level=request.get("level"),
            phase=request.get("phase"),
            status=request.get("status"),
            note=str(request.get("note") or ""),
            source_type=request.get("source_type"),
            source_id=request.get("source_id"),
            reply_message_id=request.get("reply_message_id"),
            metadata=request.get("metadata"),
        )
    except Exception as exc:  # noqa: BLE001 - a push must never fail the operation it reports on.
        print(f"in-process queue failed: {exc}", file=sys.stderr)
        return None
