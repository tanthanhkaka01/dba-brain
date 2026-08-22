"""Backwards-compatible re-export: severity tagging now lives in ``db_ops.lib.telegram_severity``.

It moved because ``db.telegram_queue`` and ``common.cli`` both need the message-type
vocabulary to validate and echo what they store, and reaching up into an app to get it made
the shared layer depend on one of its own consumers. The behaviour is unchanged and the tag
is still applied in the send path (``db_ops/telegram/api.py``).

This module stays so that path, and the tests that import ``db_ops.telegram.severity``, keep
working unchanged. New code should import from ``db_ops.lib.telegram_severity``.
"""

from __future__ import annotations

from db_ops.lib.telegram_severity import (  # noqa: F401 - re-exported for compatibility
    MESSAGE_TYPES,
    PLAIN,
    SEVERITY_EMOJI,
    STATUS_EMOJI_CHARS,
    classify_message,
    decorate_message,
    normalize_message_type,
)

__all__ = [
    "MESSAGE_TYPES",
    "PLAIN",
    "SEVERITY_EMOJI",
    "STATUS_EMOJI_CHARS",
    "classify_message",
    "decorate_message",
    "normalize_message_type",
]
