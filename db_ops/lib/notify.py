"""Single source of truth for the **notify block** — who gets told, and where.

Every app needs the same two answers about a unit of work: *tell someone when it runs?* and
*tell someone when it fails?* — each answered by an enable flag, a notify level, and an
optional explicit chat. SQL targets have expressed exactly that for a long time
(``logging_on_run`` / ``alert_on_error`` in ``sql_targets.json``), but the shape lived in
``sql_tasks.runner`` with a second copy in ``common.config_admin``, so no other app could
use it without a cross-app import or a third copy.

This module is that shape, owned once, the same way ``common.time_window`` owns scheduling.
An entry in any config file carries one ``notify`` object::

    "notify": {
      "logging_on_run": {"enabled": false, "telegram_chat": "logging", "chat_id": ""},
      "alert_on_error": {"enabled": true,  "telegram_chat": "error",   "chat_id": ""}
    }

* ``enabled``       — notify at all for this kind of event;
* ``telegram_chat`` — the notify level whose group receives it;
* ``chat_id``       — an explicit chat that wins over ``telegram_chat``, for a job that must
  reach one specific chat regardless of the level map.

``"notify": {}`` is valid and means "defaults" — the caller's defaults, so an app that
notifies by default keeps doing so and one that does not, does not.

Two legacy spellings are still read, because the files that use them are in production:
``logging_on_run``/``alert_on_error`` at the top level of an entry (SQL targets), and the
boolean form (``"logging_on_run": true``). Both are normalized to the same
:class:`NotifyConfig`, so an app never sees more than one shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "DEFAULT_ERROR_LEVEL",
    "DEFAULT_RUN_LEVEL",
    "NOTIFY_CHAT_LEVELS",
    "NOTIFY_RULE_NAMES",
    "known_chat_levels",
    "NotifyConfig",
    "NotifyRule",
    "notify_rule_dict",
    "parse_notify_config",
    "parse_notify_rule",
]

# The levels every deployment has. A rule may also route to any *configured* level — a group
# in data/telegram_groups.json carrying a `notify_level` (or a key in telegram_config.json's
# `groups`) defines one, so adding a "sla"/"backup"/"sql" group is a config edit, not a code
# change. See :func:`known_chat_levels`.
NOTIFY_CHAT_LEVELS = ("logging", "warning", "critical", "error", "test", "private")

# The two kinds of event a notify block answers for.
NOTIFY_RULE_NAMES = ("logging_on_run", "alert_on_error")

DEFAULT_RUN_LEVEL = "logging"
DEFAULT_ERROR_LEVEL = "error"


class NotifyConfigError(ValueError):
    """A notify block that cannot be honored as written."""


def known_chat_levels() -> tuple[str, ...]:
    """Levels a ``telegram_chat`` may name: the standard ones plus whatever config defines.

    The vocabulary is **data**, not code: an operator adds a Telegram group with
    ``notify_level: "sla"`` and that level exists. Hardcoding the list here would mean a code
    edit and an image rebuild for every new group, and the config would be rejected in between.

    Read straight from :mod:`db_ops.config` — a shared module, not an app — rather than through
    the routing CLI. This is *validation*, not delivery: it runs while a ``notify`` block is being
    parsed, which happens on every config load, and paying a subprocess there to learn a list of
    words was the wrong trade. Delivery is the path that must go through the Telegram app
    (:mod:`db_ops.telegram.routing`), because that is where a chat_id is decided.

    The lookup is lazy (import inside the call, so importing this module never touches config) and
    **fails open** to the standard levels: settings that cannot be read must not turn every notify
    block into an error.
    """
    try:
        from db_ops.config import load_config

        level_chat_map = load_config().telegram.level_chat_map or {}
        configured = tuple(str(level).strip().lower() for level in level_chat_map)
    except Exception:  # noqa: BLE001 - settings unavailable: fall back to the standard set.
        configured = ()
    return NOTIFY_CHAT_LEVELS + tuple(
        level for level in configured if level and level not in NOTIFY_CHAT_LEVELS
    )


@dataclass(frozen=True)
class NotifyRule:
    """One notification switch: notify or not, at which level, optionally to which chat.

    ``telegram_chat = ""`` means "follow the event's own severity" — the rule says *whether*
    to notify and leaves *where* to the level the event was raised at. That is the default
    for an app that routes by severity, so adding a ``notify`` object to a config changes
    nothing until a rule is actually set.
    """

    enabled: bool = False
    telegram_chat: str = ""
    chat_id: str = ""

    def resolve_chat_id(self, telegram_groups: dict[str, str], *, level: str = "") -> str:
        """The chat this rule sends to, or ``""`` when it is switched off.

        An explicit ``chat_id`` wins over the level map; otherwise the group wired to
        ``telegram_chat``, or — when that is blank — to ``level``, the severity the event was
        raised at. A disabled rule resolves to nothing, so callers can ask this one question
        instead of checking ``enabled`` and then resolving.
        """
        if not self.enabled:
            return ""
        if self.chat_id:
            return self.chat_id
        target = str(self.telegram_chat or level or "").strip().lower()
        return str((telegram_groups or {}).get(target) or "")

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "telegram_chat": self.telegram_chat, "chat_id": self.chat_id}


@dataclass(frozen=True)
class NotifyConfig:
    """The ``notify`` object of one entry: one rule per kind of event."""

    logging_on_run: NotifyRule = field(default_factory=lambda: NotifyRule(enabled=True))
    alert_on_error: NotifyRule = field(default_factory=lambda: NotifyRule(enabled=True))

    def rule_for_level(self, level: str) -> NotifyRule:
        """The rule that governs a message of severity ``level``.

        Apps emit by severity, not by rule name, so the mapping is made once here:
        anything at or above warning is a failure the operator is meant to act on
        (``alert_on_error``); the rest is routine progress (``logging_on_run``).
        """
        normalized = str(level or "").strip().lower()
        if normalized in {"warning", "error", "critical"}:
            return self.alert_on_error
        return self.logging_on_run

    def resolve_chat_id(self, level: str, telegram_groups: dict[str, str]) -> str:
        """The chat a message of severity ``level`` goes to, or ``""`` for none."""
        return self.rule_for_level(level).resolve_chat_id(telegram_groups, level=level)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name).to_dict() for name in NOTIFY_RULE_NAMES}


def parse_notify_rule(value: Any, *, default: NotifyRule, context: str = "notify") -> NotifyRule:
    """Parse one rule, accepting the object form and the legacy boolean form."""
    if value is None:
        return default
    if isinstance(value, bool):
        # Legacy: `"logging_on_run": true` meant "notify at the default level".
        return replace(default, enabled=value)
    if not isinstance(value, dict):
        raise NotifyConfigError(
            f"{context} must be an object {{enabled, telegram_chat, chat_id}} or a boolean."
        )
    # Present-but-empty is meaningful (follow the event's severity), so absence is tested
    # rather than falsiness.
    raw_level = value["telegram_chat"] if "telegram_chat" in value else default.telegram_chat
    level = str(raw_level or "").strip().lower()
    known = known_chat_levels()
    if level and level not in known:
        raise NotifyConfigError(
            f"{context}.telegram_chat must be one of {', '.join(known)}, got {level!r}. "
            "Add a Telegram group with that notify_level to define it."
        )
    return NotifyRule(
        enabled=bool(value.get("enabled", default.enabled)),
        telegram_chat=level,
        chat_id=str(value.get("chat_id") or "").strip(),
    )


def parse_notify_config(
    values: dict[str, Any] | None,
    *,
    context: str = "notify",
    defaults: NotifyConfig | None = None,
    inherit: NotifyConfig | None = None,
) -> NotifyConfig:
    """Read the ``notify`` object out of a config entry.

    Takes the **whole entry** and pulls ``notify`` out of it, mirroring
    :func:`db_ops.lib.time_window.parse_time_window_config` so both shared objects are
    read the same way. Falls back to the legacy top-level ``logging_on_run`` /
    ``alert_on_error`` keys that ``sql_targets.json`` still uses.

    ``defaults`` is what an unspecified rule means for this caller — an app that notifies by
    default passes enabled rules, one that does not passes disabled ones. ``inherit`` is a
    parent entry's block (a backup entry's, for its sub-jobs), applied before ``defaults`` so
    a sub-job overrides its entry rule by rule.
    """
    base = inherit or defaults or NotifyConfig()
    entry = values or {}
    if not isinstance(entry, dict):
        raise NotifyConfigError(f"{context} must be an object.")

    block = entry.get("notify")
    if block is None:
        block = {}
    elif not isinstance(block, dict):
        raise NotifyConfigError(f"{context}.notify must be an object.")

    unknown = [key for key in block if key not in NOTIFY_RULE_NAMES]
    if unknown:
        raise NotifyConfigError(
            f"{context}.notify has unknown key(s) {', '.join(sorted(unknown))}. "
            f"Expected: {', '.join(NOTIFY_RULE_NAMES)}."
        )

    resolved: dict[str, NotifyRule] = {}
    for name in NOTIFY_RULE_NAMES:
        # The nested block wins; the legacy top-level key is read only when it does not.
        raw = block.get(name, entry.get(name))
        resolved[name] = parse_notify_rule(
            raw, default=getattr(base, name), context=f"{context}.notify.{name}"
        )
    return NotifyConfig(**resolved)


def notify_rule_dict(*, enabled: bool, telegram_chat: str, chat_id: str | None) -> dict[str, Any]:
    """Build one rule in its written JSON form, validating the level."""
    level = str(telegram_chat or "").strip().lower()
    known = known_chat_levels()
    if level not in known:
        raise NotifyConfigError(
            f"telegram_chat must be one of {known}, got {telegram_chat!r}."
        )
    return {"enabled": bool(enabled), "telegram_chat": level, "chat_id": str(chat_id or "").strip()}
