"""Notification routing: the Telegram settings, owned by the app that owns Telegram.

Routing answers three questions an app must not answer for itself — is Telegram on, does this
level alert, which chat — and they used to be re-implemented per app from ``config.telegram``.
They were then centralised into ``db_ops.common.cli telegram-route``, which fixed the drift but
put the reading in the wrong layer: ``common`` sits *below* every app and must not depend on one,
yet the routing it served is the Telegram app's own configuration. ``common`` also had to shell
out to its own CLI to get it (``common/notify_route.py`` ran a subprocess), which is a process
boundary inside a shared library.

So the settings live here now, in the app they belong to, and reach other apps the way apps are
supposed to reach each other: across a process boundary, through this app's CLI
(``db_ops.telegram.cli route``). ``common`` keeps the *policy* — how an entry's ``notify`` block
narrows a level — as pure functions that are handed the answer rather than fetching it.

``alert`` is the single flag to act on: Telegram is on AND the level has a chat. Having a chat
*is* the permission — a level that must stay quiet has its chat cleared, so there is one place to
look and nothing to keep in sync.
"""

from __future__ import annotations
from db_ops.lib.notify_route import NO_ROUTE  # noqa: F401 - one definition, see that module

from typing import Any

#: The levels always reported, listed even when unmapped so a blank route is visible rather than
#: silently absent.
STANDARD_LEVELS: tuple[str, ...] = ("logging", "warning", "error", "critical")



def telegram_settings() -> tuple[bool, dict[str, str], list[str]]:
    """**The only place db_ops reads the Telegram settings.**

    Returns ``(enabled, level_chat_map, levels_to_report)``. Every routing question is answered
    from this one read, so the settings have a single reader in-process and a single contract out
    of it (this app's ``route`` and ``groups`` commands).

    ``levels_to_report`` is the standard levels first, then whatever the deployment configured —
    a group carrying ``notify_level: "sla"`` defines a level, and a level missing from this list
    is invisible to every app that routes by rule.
    """
    from db_ops.config import load_config

    telegram = load_config().telegram
    level_chat_map = dict(telegram.level_chat_map or {})
    extra = [level for level in level_chat_map if level not in STANDARD_LEVELS]
    return bool(telegram.enabled), level_chat_map, [*STANDARD_LEVELS, *sorted(extra)]


def route_for_level(level: str) -> dict[str, Any]:
    """``{"level", "enabled", "alert", "chat_id"}`` for one notify level.

    No ``--config``: the routing config has one home (the tool root ``config.json``), so every
    app resolves the same answer regardless of which config file it was started with.
    """
    from db_ops.config import chat_id_for_level

    key = str(level or "").strip().lower()
    if not key:
        return {"level": "", **NO_ROUTE}
    enabled, level_chat_map, _levels = telegram_settings()
    chat_id = chat_id_for_level(level_chat_map, key)
    return {
        "level": key,
        "enabled": bool(enabled),
        "alert": bool(enabled and chat_id),
        "chat_id": chat_id or "",
    }


def groups() -> dict[str, str]:
    """The configured ``level -> chat_id`` map, including deployment-defined levels."""
    _enabled, level_chat_map, _levels = telegram_settings()
    return dict(level_chat_map)
