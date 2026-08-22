"""This app's client for the Telegram app's routing commands.

**One copy, in ``lib``.** It was the same file in five apps, byte for byte. It lived app-side
because an app may not import another app and ``common`` may not start a process — but neither
rule forces a copy per app: this module imports only ``db_ops.lib.notify_route`` (pure) and, as a
last-resort fallback, ``db_ops.config``, which is a root module. So it sits where every component
may import it and there is one of it.

Routing is owned by :mod:`db_ops.telegram.routing` and reached through ``db_ops.telegram.cli``.
An app cannot import another app, so the call crosses a process boundary - the sanctioned way apps
talk here (``tests/test_import_boundaries.py``). It cannot live in ``common`` either: ``common``
sits below every app and must neither depend on one nor run a CLI.

Only the *transport* is per-app. The shape of the answer and the rule for applying it are shared
and pure: :func:`db_ops.lib.notify_route.parse_route` / ``parse_groups`` / ``resolve_chat_id``.

Cached per level for :data:`CACHE_TTL_SECONDS`, because a run emitting a burst of events would
otherwise pay the subprocess for each one; a routing change is still picked up within the TTL.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

from db_ops.lib.notify_route import NO_ROUTE, parse_groups, parse_route

CACHE_TTL_SECONDS = 60
_TIMEOUT_SECONDS = 30

_cache: dict[str, tuple[float, Any]] = {}
# Not a level: levels come from config keys and are lowercased words, so this cannot collide.
_GROUPS_KEY = "__groups__"


def _run(args: list[str], *, what: str) -> Any:
    """Run one telegram CLI command and parse its JSON, or say why it could not be used.

    Failures are printed, never swallowed: a lookup that quietly returned "do not alert" would
    suppress exactly the error somebody is waiting for.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "db_ops.telegram.cli", *args],
            capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"telegram {what} failed: {exc}", file=sys.stderr)
        return None
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:300]
        print(f"telegram {what} exited {completed.returncode}: {detail}", file=sys.stderr)
        return None
    payload = completed.stdout.strip()
    if not payload:
        # Exit 0 with nothing on stdout is not an answer. Parsing it as {} would produce a
        # perfectly valid "must not alert" and cache it, muting the level for the whole TTL.
        print(f"telegram {what} returned no output", file=sys.stderr)
        return None
    try:
        return json.loads(payload)
    except ValueError as exc:
        print(f"telegram {what} returned invalid JSON: {exc}", file=sys.stderr)
        return None


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
    parsed = _run(["route", key], what=f"route for level={key}")
    if parsed is None:
        return dict(NO_ROUTE)
    route = parse_route(parsed)
    _cache[key] = (now + CACHE_TTL_SECONDS, route)
    return dict(route)


def telegram_groups(*, use_cache: bool = True) -> dict[str, str]:
    """The whole ``level -> chat_id`` map, for callers routing by rule rather than by severity."""
    now = time.monotonic()
    if use_cache:
        cached = _cache.get(_GROUPS_KEY)
        if cached and cached[0] > now:
            return dict(cached[1])
    parsed = _run(["groups"], what="groups")
    if parsed is None:
        return {}
    groups = parse_groups(parsed)
    _cache[_GROUPS_KEY] = (now + CACHE_TTL_SECONDS, groups)
    return dict(groups)


def chat_id_for_level(groups: dict[str, str], level: str) -> str:
    """The chat a level maps to, given a map already in hand."""
    from db_ops.config import chat_id_for_level as _resolve

    return _resolve(groups or {}, level)


def clear_cache() -> None:
    """Drop cached answers (tests, and after a deliberate config change)."""
    _cache.clear()
