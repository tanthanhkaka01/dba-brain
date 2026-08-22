"""Notification routing **policy**: how an entry's ``notify`` block narrows a node's route.

This module used to *fetch* the route too, by running ``db_ops.common.cli telegram-route`` in a
subprocess. That put two things in the wrong place at once: ``common`` sits below every app, yet
it was reading the Telegram app's configuration — and it was starting a process to do it, from
inside a shared library.

The settings moved to their owner, :mod:`db_ops.telegram.routing`, reachable through
``db_ops.telegram.cli route`` / ``groups``. Each app carries a small client for that call
(``<app>/telegram_route.py``), because an app may not import another app but may cross a process
boundary. What is left here is the part that really is app-independent, and there is exactly one
copy of it.

**Everything in this module is a pure function of its arguments.** Nothing reads config, opens a
store, or starts a process. The app fetches, the shared layer decides.
"""

from __future__ import annotations

from typing import Any

#: What a client returns when routing could not be read. Identical to "this level must not alert"
#: on purpose: a routing outage must not be distinguishable from a muted level, or it would turn
#: into a burst of messages addressed nowhere.
NO_ROUTE: dict[str, Any] = {"enabled": False, "alert": False, "chat_id": ""}


def chat_from_route(route: dict[str, Any] | None) -> str:
    """The chat a fetched ``route`` allows, or ``""`` when this level must not alert.

    ``alert`` already means "Telegram is on AND this level has a chat"; callers act on that flag
    rather than re-deriving it from ``enabled`` and ``chat_id`` — re-deriving it is how the daemon
    drifted from every other app.
    """
    if not route:
        return ""
    return str(route.get("chat_id") or "") if route.get("alert") else ""


def parse_route(payload: Any) -> dict[str, Any]:
    """Normalise a ``telegram.cli route`` answer, so every app's client shares one shape.

    Malformed input becomes :data:`NO_ROUTE` rather than a partial dict: a client that returned
    ``{"alert": True}`` with no chat would send nowhere and report success.
    """
    if not isinstance(payload, dict):
        return dict(NO_ROUTE)
    return {
        "enabled": bool(payload.get("enabled")),
        "alert": bool(payload.get("alert")),
        "chat_id": str(payload.get("chat_id") or ""),
    }


def parse_groups(payload: Any) -> dict[str, str]:
    """Normalise a ``telegram.cli groups`` answer into ``{level: chat_id}``."""
    if not isinstance(payload, dict):
        return {}
    return {str(k).strip().lower(): str(v) for k, v in payload.items() if k and v}


def resolve_chat_id(
    level: str,
    notify: Any = None,
    *,
    route: dict[str, Any] | None,
    groups: dict[str, str] | None = None,
) -> str:
    """Where a message of severity ``level`` goes, given the node's ``route`` and an entry's
    ``notify`` block.

    The two layers meet here, and only here:

    * the **node** decides whether a level may alert at all — ``route``, from the Telegram app;
    * the **entry's** :class:`db_ops.lib.notify.NotifyConfig` narrows that: whether this unit
      of work reports this kind of event, and optionally to which chat instead.

    The node always gates. A ``notify`` object can silence a level or redirect it, never switch on
    a level the node has switched off — otherwise a per-entry block would be a way to leak
    messages out of a node that was deliberately muted. ``notify=None`` means the caller has no
    per-entry block and follows the node alone.

    ``groups`` is needed only by a rule that redirects to a *different* level
    (``telegram_chat: "backup"`` on an ``error`` event): resolving that target is a second lookup,
    and a pure function cannot go and fetch it. Callers omit it when no rule redirects, which is
    why the common case costs one subprocess rather than two.
    """
    key = str(level or "").strip().lower()
    node_chat = chat_from_route(route)
    if notify is None:
        return node_chat

    rule = notify.rule_for_level(key)
    if not rule.enabled:
        return ""
    if rule.chat_id:
        # An explicit chat still needs the node to allow this level to alert.
        return rule.chat_id if node_chat else ""
    target = str(rule.telegram_chat or key).strip().lower()
    if target == key:
        return node_chat
    # Redirected to another level: the gate is the node being on, not this level having a chat -
    # the point of the redirect is to send somewhere else, so a `backup` chat must still receive
    # an event whose own level is unmapped.
    if not (route or {}).get("enabled"):
        return ""
    return str((groups or {}).get(target) or "")
