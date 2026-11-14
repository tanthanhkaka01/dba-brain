"""The bot's command menu - what Telegram shows under "/" - set from the commands this node has.

Until 0.39.0 (1.348) the menu was whatever someone last pasted into BotFather's `/setcommands`
from the block at the top of `data/telegram_support_commands.md`. Nothing compared the two: read on
2026-11-13, the live menu had 29 commands while the node ran 31 - `/spbot_copy_backup` (0.34.0) and
`/spbot_lockdown` (0.35.0) had never been pasted, so nobody saw them offered. It was put right that
morning by calling `setMyCommands` by hand; this is that call, built in.

**The menu is the node's own `telegram_support_commands.json`** (the operator, 2026-11-14: *take it
straight from the json, not from the md file*): each command's `menu_description`, in `sort_order`,
for the commands the node may run (``dbabrain.lib.bot_menu``). The md document keeps its block as a
description of the shipped commands, held to the json by a test; nothing reads it to set the menu.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from dbabrain.lib import bot_menu, errors
from dbabrain.telegram.api import DEFAULT_TELEGRAM_API_URL, call_telegram_api

#: Kept here for the readers that named them; the rule is ``lib.bot_menu``'s.
COMMAND_RE, DESCRIPTION_MAX, MENU_MAX = bot_menu.COMMAND_RE, bot_menu.DESCRIPTION_MAX, bot_menu.MENU_MAX


def menu_block(markdown: str) -> list[dict[str, str]]:
    """The ``command - description`` lines of the document's first fenced block, in order - the
    shipped document's description of the menu, held to the json by its test; never the menu's source."""
    parts = markdown.split("```")
    if len(parts) < 3:
        raise errors.InvalidRequest("the menu document has no fenced block of `command - description` lines")
    entries: list[dict[str, str]] = []
    for line in parts[1].splitlines():
        if " - " not in line:
            continue
        name, description = (part.strip() for part in line.split(" - ", 1))
        if not COMMAND_RE.fullmatch(name):
            raise errors.InvalidRequest(f"menu line names {name!r}: Telegram takes 1-32 of a-z, 0-9 and _")
        if not description or len(description) > DESCRIPTION_MAX:
            raise errors.InvalidRequest(f"menu line {name}: the description is 1-{DESCRIPTION_MAX} characters")
        entries.append({"command": name, "description": description})
    return entries


def set_menu(
    *,
    bot_token: str,
    commands_path: str | Path,
    dry_run: bool = False,
    api_url: str = DEFAULT_TELEGRAM_API_URL,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """Read the live menu, and set it to this node's when the two differ (unless ``dry_run``).

    The node's commands file is the whole source: each command's ``menu_description`` in
    ``sort_order``. A file in which no command states a line is refused, never set to an empty menu
    - that is a node whose upgrade has not written them yet (``upgrade-config``, ``menu-descriptions``)."""
    commands_file = Path(commands_path)
    if not commands_file.is_file():
        raise errors.NotConfigured(f"{commands_file} is not there: the menu is the node's own commands.")
    records = json.loads(commands_file.read_text(encoding="utf-8-sig")).get("telegram_support_commands") or []
    wanted = bot_menu.menu_from_commands(records)
    if wanted["refused"]:
        raise errors.InvalidConfig("menu lines Telegram would refuse - " + "; ".join(wanted["refused"]))
    if not wanted["menu"]:
        raise errors.NotConfigured(
            f"no command in {commands_file} states its menu_description - nothing to set. Run "
            "upgrade-config (step menu-descriptions) to write the shipped lines.")
    live = call_telegram_api(bot_token=bot_token, method_name="getMyCommands", payload={},
                             api_url=api_url, timeout_seconds=timeout_seconds).get("result") or []
    live = [{"command": str(c.get("command")), "description": str(c.get("description"))} for c in live]
    names_live = [c["command"] for c in live]
    names_wanted = [c["command"] for c in wanted["menu"]]
    changed = live != wanted["menu"]
    result = {
        "menu_commands": len(wanted["menu"]),
        "live_commands": len(live),
        "added": [n for n in names_wanted if n not in names_live],
        "removed": [n for n in names_live if n not in names_wanted],
        "reworded_or_moved": changed and set(names_live) == set(names_wanted),
        "switched_off_here": wanted["switched_off_here"],
        "not_described": wanted["not_described"],
        "dry_run": dry_run,
        "set": False,
    }
    if changed and not dry_run:
        call_telegram_api(bot_token=bot_token, method_name="setMyCommands",
                          payload={"commands": json.dumps(wanted["menu"], ensure_ascii=False)},
                          api_url=api_url, timeout_seconds=timeout_seconds)
        result["set"] = True
    result["message"] = (
        "the menu is already this node's" if not changed else
        ("dry run: the menu would be set" if dry_run else "the menu was set")
        + f" - {len(names_wanted)} command(s), {len(result['added'])} added, {len(result['removed'])} removed")
    return result
