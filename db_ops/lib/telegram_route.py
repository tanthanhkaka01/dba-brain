"""Notification routing, answered in-process from the configuration (0.24.0).

Three questions every app asks before it sends anything - is Telegram on, does this level alert,
which chat - and the answer is a function of the configuration alone (``telegram.enabled`` and
``telegram.level_chat_map``). Until 0.24.0 every app asked the Telegram app's CLI (``telegram.cli
route``), a process per level, because the configuration parser was not ``lib``'s; that made this
module start a process from the layer everything imports (rules R07), and one app drive another's
CLI (R42). The parser is ``lib.config`` now, so routing is read here and nothing is started.

The Telegram app still answers ``route`` / ``groups`` for a person at a shell - from these same
functions (``telegram/routing.py`` re-exports them), so there is one answer and one definition.

``alert`` is the single flag to act on: Telegram is on AND the level has a chat. Having a chat *is*
the permission - a level that must stay quiet has its chat cleared. Every lookup **fails closed**
and says why on stderr: a lookup that quietly returned "do not alert" would suppress exactly the
error somebody is waiting for.

Cached per level for :data:`CACHE_TTL_SECONDS`, because a run emitting a burst of events would
otherwise read the configuration for each one; a routing change is still picked up within the TTL.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from db_ops.lib.notify_route import NO_ROUTE, parse_groups, parse_route

CACHE_TTL_SECONDS = 60

#: The levels always reported, listed even when unmapped so a blank route is visible rather than
#: silently absent.
STANDARD_LEVELS: tuple[str, ...] = ("logging", "warning", "error", "critical")

_cache: dict[str, tuple[float, Any]] = {}
# Not a level: levels come from config keys and are lowercased words, so this cannot collide.
_GROUPS_KEY = "__groups__"


def telegram_settings() -> tuple[bool, dict[str, str], list[str]]:
    """**The only place db_ops reads the Telegram settings.** ``(enabled, level_chat_map, levels)``.

    ``levels`` is the standard levels first, then whatever the deployment configured - a group
    carrying ``notify_level: "sla"`` defines a level, and a level missing from this list is
    invisible to every app that routes by rule. No ``--config``: the routing config has one home
    (the tool root ``config.json``), so every app resolves the same answer.
    """
    from db_ops.lib.config import load_config

    telegram = load_config().telegram
    level_chat_map = dict(telegram.level_chat_map or {})
    extra = [level for level in level_chat_map if level not in STANDARD_LEVELS]
    return bool(telegram.enabled), level_chat_map, [*STANDARD_LEVELS, *sorted(extra)]


def route_for_level(level: str) -> dict[str, Any]:
    """``{"level", "enabled", "alert", "chat_id"}`` for one notify level, uncached."""
    from db_ops.lib.config import chat_id_for_level as _resolve

    key = str(level or "").strip().lower()
    if not key:
        return {"level": "", **NO_ROUTE}
    enabled, level_chat_map, _levels = telegram_settings()
    chat_id = _resolve(level_chat_map, key)
    return {"level": key, "enabled": bool(enabled), "alert": bool(enabled and chat_id),
            "chat_id": chat_id or ""}


def groups() -> dict[str, str]:
    """The configured ``level -> chat_id`` map, including deployment-defined levels, uncached."""
    _enabled, level_chat_map, _levels = telegram_settings()
    return dict(level_chat_map)


def telegram_route(level: str, *, use_cache: bool = True) -> dict[str, Any]:
    """``{"enabled", "alert", "chat_id"}`` for one notify level. Fails closed."""
    key = str(level or "").strip().lower()
    if not key:
        return dict(NO_ROUTE)
    now = time.monotonic()
    if use_cache:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return dict(cached[1])
    try:
        route = parse_route(route_for_level(key))
    except Exception as exc:  # noqa: BLE001 - fail closed, and say so.
        print(f"telegram route for level={key} failed: {exc}", file=sys.stderr)
        return dict(NO_ROUTE)
    _cache[key] = (now + CACHE_TTL_SECONDS, route)
    return dict(route)


def telegram_groups(*, use_cache: bool = True) -> dict[str, str]:
    """The whole ``level -> chat_id`` map, for callers routing by rule rather than by severity."""
    now = time.monotonic()
    if use_cache:
        cached = _cache.get(_GROUPS_KEY)
        if cached and cached[0] > now:
            return dict(cached[1])
    try:
        mapping = parse_groups(groups())
    except Exception as exc:  # noqa: BLE001 - fail closed, and say so.
        print(f"telegram groups failed: {exc}", file=sys.stderr)
        return {}
    _cache[_GROUPS_KEY] = (now + CACHE_TTL_SECONDS, mapping)
    return dict(mapping)


def chat_id_for_level(groups_map: dict[str, str], level: str) -> str:
    """The chat a level maps to, given a map already in hand."""
    from db_ops.lib.config import chat_id_for_level as _resolve

    return _resolve(groups_map or {}, level)


def clear_cache() -> None:
    """Drop cached answers (tests, and after a deliberate config change)."""
    _cache.clear()
