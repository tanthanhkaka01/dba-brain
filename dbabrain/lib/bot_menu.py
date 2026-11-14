"""The bot's command menu, read from the commands themselves (0.39.0, 1.348).

What Telegram offers under "/" is set with ``setMyCommands``: a list of ``command - description``
lines. Until 2026-11-14 the lines were the first fenced block of ``data/telegram_support_commands.md``
- a second place beside ``telegram_support_commands.json`` that had to be kept in step by hand, and
the reason the menu once lacked two commands the node ran. The operator, 2026-11-14 (translated):
*set-menu should take it straight from telegram_support_commands.json, not from the md file.*

So each command states its own line, ``menu_description``, on its record, and the menu is the
records: in ``sort_order``, the ones the node may run (``command_type`` 0 and up - below 0 is switched
off here). A command that states no line is named (``not_described``), never given an invented one.
``state_menu_descriptions`` is the upgrade's half: a record that states none is given the shipped
catalogue's line for the same command; what a node states is left as it is.

Values and rules only - no file, no network: ``telegram.menu`` reads the file and calls Telegram,
``common.config_upgrade`` writes the node's file.
"""

from __future__ import annotations

import re
from typing import Any

#: The record field that holds a command's menu line.
FIELD = "menu_description"
#: Telegram's own limits for one menu entry (Bot API, BotCommand).
COMMAND_RE = re.compile(r"[a-z0-9_]{1,32}")
DESCRIPTION_MAX = 256
#: Telegram's limit on the number of entries in one menu.
MENU_MAX = 100


def command_name(record: dict[str, Any]) -> str:
    return str(record.get("command_text") or "").strip().lstrip("/")


def level(record: dict[str, Any]) -> int:
    """The command's level; a value that is not a number reads as switched off - never as open."""
    try:
        return int(record.get("command_type", 0))
    except (TypeError, ValueError):
        return -1


def _order(record: dict[str, Any]) -> float:
    try:
        return float(record.get("sort_order"))
    except (TypeError, ValueError):
        return float("inf")


def line_problem(record: dict[str, Any]) -> str:
    """Why a record's menu line would be refused by Telegram, or ``""``."""
    name = command_name(record)
    if not COMMAND_RE.fullmatch(name):
        return f"{name!r}: Telegram takes a command of 1-32 of a-z, 0-9 and _"
    text = str(record.get(FIELD) or "").strip()
    if not text or len(text) > DESCRIPTION_MAX:
        return f"{name}: {FIELD} is 1-{DESCRIPTION_MAX} characters"
    return ""


def menu_from_commands(records: list[dict[str, Any]]) -> dict[str, Any]:
    """The menu the node's commands make, and what is left out of it and why.

    ``{"menu": [{"command", "description"}...], "not_described": [...], "switched_off_here": [...],
    "refused": ["<why>", ...]}`` - in ``sort_order``, a tie kept in the file's order.
    """
    ordered = sorted((r for r in records if isinstance(r, dict) and command_name(r)), key=_order)
    menu: list[dict[str, str]] = []
    not_described: list[str] = []
    switched_off: list[str] = []
    refused: list[str] = []
    for record in ordered:
        name = command_name(record)
        if level(record) < 0:
            switched_off.append(name)
            continue
        if not str(record.get(FIELD) or "").strip():
            not_described.append(name)
            continue
        problem = line_problem(record)
        if problem:
            refused.append(problem)
            continue
        menu.append({"command": name, "description": str(record[FIELD]).strip()})
    return {"menu": menu[:MENU_MAX], "not_described": not_described, "switched_off_here": switched_off,
            "refused": refused}


def state_menu_descriptions(document: dict[str, Any], shipped: dict[str, Any]) -> list[dict[str, Any]]:
    """Give each command of a node's ``telegram_support_commands.json`` that states no menu line the
    shipped catalogue's line for the same command, in place. Answers one change per record written;
    a command the catalogue does not know is left - its owner writes its line."""
    records = document.get("telegram_support_commands") if isinstance(document, dict) else None
    known = {command_name(r): str(r.get(FIELD) or "").strip()
             for r in (shipped or {}).get("telegram_support_commands") or [] if isinstance(r, dict)}
    changes: list[dict[str, Any]] = []
    for index, record in enumerate(records or []):
        if not isinstance(record, dict) or str(record.get(FIELD) or "").strip():
            continue
        line = known.get(command_name(record))
        if line:
            record[FIELD] = line
            changes.append({"where": f"telegram_support_commands[{index}]", "field": FIELD, "value": line})
    return changes
