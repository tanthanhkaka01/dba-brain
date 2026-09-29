"""How every app queues an outgoing Telegram message: one row in ``telegram_send_messages``.

The app supplies data - the chat, the rendered text, the message type's two halves, and the
**store to write to**, as a live store the caller holds or as a declaration
(:mod:`db_ops.db.declaration`) - and this writes the row through ``db.telegram_queue``.

**One copy, in ``db``.** It was the same file in six apps, byte for byte, held identical by a
test - and even then the six near-copies had drifted into three variants before any of them
shipped, one having quietly lost ``reply_message_id``, the field a command reply needs. Every app
may import ``db``, so ``db`` is where it belongs. Even the Telegram app uses it: exempting the app
that owns ``telegram_send_messages`` would put one writer on a different path from the others.

**In-process, and nothing else (the operator, 2026-09-28: `db` does not import `transport`).**
Until 0.25.0 this started ``db.cli queue-telegram-message`` through ``transport`` and fell back to
the in-process insert when the process could not deliver - a process per message to reach the
function the fallback already called directly, and a shared layer reaching up into the layer that
starts processes. The insert is the same either way; ``db.cli queue-telegram-message`` stays for a
person at a shell.
"""

from __future__ import annotations

import sys
from typing import Any


def store_block(app_config: Any) -> dict[str, Any] | None:
    """This node's store, stated as data. ``None`` when it cannot be described.

    Never raises: describing the store resolves its password, and a secret store that cannot be
    read must not stop the message being queued - without a block the row goes to this node's
    ``config.json`` store.
    """
    try:
        from db_ops.db.declaration import describe

        return describe(app_config)
    except Exception as exc:  # noqa: BLE001 - reported; the message still goes to this node's store.
        print(f"store could not be described for a Telegram message: {exc}", file=sys.stderr)
        return None


def store_block_from(store: Any) -> dict[str, Any] | None:
    """The same, from a live store the caller already holds (no config needed)."""
    try:
        from db_ops.db.declaration import describe_store

        return describe_store(store)
    except Exception as exc:  # noqa: BLE001 - reported; the message still goes to this node's store.
        print(f"store could not be described for a Telegram message: {exc}", file=sys.stderr)
        return None


def queue_message(request: dict[str, Any], *, fallback_store: Any = None) -> int | None:
    """Queue one outgoing message. Returns its id, or ``None`` when it could not be queued.

    The store is ``fallback_store`` when the caller holds one, else the request's ``store`` block,
    else this node's ``config.json`` (:func:`db_ops.db.telegram_queue.queue_from_request`, which
    ``db.cli queue-telegram-message`` runs too). A push never fails the operation it reports on.
    """
    from db_ops.db.telegram_queue import queue_from_request

    try:
        return queue_from_request(request, store=fallback_store)
    except Exception as exc:  # noqa: BLE001 - a push must never fail the operation it reports on.
        print(f"Telegram message not queued: {exc}", file=sys.stderr)
        return None
