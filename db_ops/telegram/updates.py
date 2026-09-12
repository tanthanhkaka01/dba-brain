from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from db_ops.common import data_sources
from db_ops.db import DbOpsStore
from db_ops.telegram.api import get_updates
from db_ops.lib.paths import DEFAULT_DATA_DIR  # noqa: F401 - one definition, see that module


GROUPS_PATH = DEFAULT_DATA_DIR / "telegram_groups.json"
USERS_PATH = DEFAULT_DATA_DIR / "telegram_users.json"


def fetch_and_save_updates(
    *,
    bot_token: str,
    api_url: str = "https://api.telegram.org",
    timeout_seconds: int = 20,
    offset: int | None = None,
    limit: int | None = None,
    allowed_updates: list[str] | None = None,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    sqlite_path: str | Path | None = None,
) -> dict[str, Any]:
    result = get_updates(
        bot_token=bot_token,
        api_url=api_url,
        timeout_seconds=timeout_seconds,
        offset=offset,
        limit=limit,
        allowed_updates=allowed_updates,
    )
    updates = result.get("result") or []
    next_update_offset = get_next_update_offset(updates)
    paths = TelegramUpdatePaths.from_data_dir(data_dir)
    if sqlite_path is None:
        raise RuntimeError("sqlite_path is required for saving Telegram messages.")
    store = DbOpsStore(sqlite_path)
    saved = save_updates(updates, paths=paths, store=store)
    return {
        "ok": True,
        "updates": len(updates),
        "next_update_offset": next_update_offset,
        "saved": saved,
        "paths": {
            "messages": str(store.sqlite_path),
            "groups": str(paths.groups_path),
            "users": str(paths.users_path),
        },
    }


def get_next_update_offset(updates: list[dict[str, Any]]) -> int | None:
    update_ids = [int(item["update_id"]) for item in updates if item.get("update_id") is not None]
    if not update_ids:
        return None
    return max(update_ids) + 1


class TelegramUpdatePaths:
    def __init__(self, *, groups_path: Path, users_path: Path) -> None:
        self.groups_path = groups_path
        self.users_path = users_path

    @classmethod
    def from_data_dir(cls, data_dir: str | Path) -> "TelegramUpdatePaths":
        base_dir = Path(data_dir)
        return cls(
            groups_path=base_dir / "telegram_groups.json",
            users_path=base_dir / "telegram_users.json",
        )


def save_updates(
    updates: list[dict[str, Any]],
    *,
    paths: TelegramUpdatePaths | None = None,
    store: DbOpsStore,
) -> dict[str, int]:
    target_paths = paths or TelegramUpdatePaths(
        groups_path=GROUPS_PATH,
        users_path=USERS_PATH,
    )
    # One reader for both files (common.data_sources). The writes below stay here:
    # this app owns the file, and owning it is what makes it the only writer.
    groups_data = data_sources.load_telegram_groups(target_paths.groups_path)
    users_data = data_sources.load_telegram_users(target_paths.users_path)

    messages: list[dict[str, Any]] = []
    groups_by_id = {str(item.get("group_id", "")): item for item in groups_data}
    users_by_id = {str(item.get("user_id", "")): item for item in users_data}
    groups_changed = False
    users_changed = False

    for update in updates:
        message = extract_message(update)
        if not message:
            continue

        saved_message = build_message_record(update, message)
        messages.append(saved_message)

        chat = message.get("chat") or {}
        if is_group_chat(chat):
            group = build_group_record(chat)
            existing_group = groups_by_id.get(group["group_id"], {})
            merged_group = merge_group_record(existing_group, group)
            if merged_group != existing_group:
                groups_by_id[group["group_id"]] = merged_group
                groups_changed = True

        for user in iter_message_users(message):
            user_record = build_user_record(user)
            existing_user = users_by_id.get(user_record["user_id"], {})
            merged_user = merge_user_record(existing_user, user_record)
            if merged_user != existing_user:
                users_by_id[user_record["user_id"]] = merged_user
                users_changed = True

    saved_messages = store.upsert_telegram_messages(messages)
    if groups_changed:
        write_json_list(target_paths.groups_path, root_key="telegram_groups", items=list(groups_by_id.values()))
    if users_changed:
        write_json_list(target_paths.users_path, root_key="telegram_users", items=list(users_by_id.values()))

    return {
        "messages": saved_messages,
        "groups": len(groups_by_id),
        "users": len(users_by_id),
        "groups_changed": int(groups_changed),
        "users_changed": int(users_changed),
    }


def extract_message(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        value = update.get(key)
        if isinstance(value, dict):
            return value
    return None


def iter_message_users(message: dict[str, Any]) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def add_user(user: Any) -> None:
        if not isinstance(user, dict) or user.get("id") is None:
            return
        user_id = str(user.get("id"))
        if user_id in seen_ids:
            return
        seen_ids.add(user_id)
        users.append(user)

    add_user(message.get("from"))
    for member in message.get("new_chat_members") or []:
        add_user(member)
    return users


def build_message_record(update: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    return {
        "update_id": update.get("update_id"),
        "message_id": message.get("message_id"),
        "message_date": message.get("date"),
        "chat_id": str(chat.get("id", "")),
        "chat_type": str(chat.get("type", "")),
        "user_id": str(user.get("id", "")) if user.get("id") is not None else "",
        "text": str(message.get("text") or message.get("caption") or ""),
        "raw": message,
    }


def build_group_record(chat: dict[str, Any]) -> dict[str, Any]:
    group_id = str(chat.get("id", ""))
    title = str(chat.get("title") or chat.get("username") or chat.get("first_name") or "")
    return {
        "group_id": group_id,
        "title": title,
        "group_type": str(chat.get("type") or "group"),
        "notify_level": "",
        "source_notify_level": "",
        "allow_command": 0,
        "status": "active",
        "note": "Loaded from Telegram getUpdates.",
    }


def merge_group_record(existing_group: dict[str, Any], update_group: dict[str, Any]) -> dict[str, Any]:
    if not existing_group:
        return update_group

    merged = existing_group.copy()
    for key in ("title", "group_type"):
        value = update_group.get(key)
        if value:
            merged[key] = value
    return merged


def build_user_record(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(user.get("id", "")),
        "is_bot": bool(user.get("is_bot", False)),
        "user_type": 0,
        "first_name": str(user.get("first_name") or ""),
        "last_name": str(user.get("last_name") or ""),
        "username": str(user.get("username") or ""),
        "language_code": str(user.get("language_code") or ""),
        "status": "active",
        "note": "Loaded from Telegram getUpdates.",
    }


def merge_user_record(existing_user: dict[str, Any], update_user: dict[str, Any]) -> dict[str, Any]:
    if not existing_user:
        return update_user

    merged = existing_user.copy()
    for key in ("is_bot", "first_name", "last_name", "username", "language_code"):
        merged[key] = update_user.get(key, merged.get(key))
    return merged


def is_group_chat(chat: dict[str, Any]) -> bool:
    try:
        chat_id = int(chat.get("id", 0))
    except (TypeError, ValueError):
        chat_id = 0
    return chat_id < 0 and str(chat.get("type", "")).lower() in ("group", "supergroup", "channel")


def load_json_list(path: Path, *, root_key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)
    return [item for item in data.get(root_key, []) if isinstance(item, dict)]


def write_json_list(path: Path, *, root_key: str, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump({root_key: items}, file, ensure_ascii=False, indent=2)
        file.write("\n")


#: The notify levels this estate routes on. Not a closed set in the config — `notify_level` is a
#: free string that `telegram_config.json`'s level map and each app's `notify` block agree on — but
#: these are the ones the shipped catalogue uses, and offering them beats leaving a reader to guess
#: which spelling the router expects.
KNOWN_NOTIFY_LEVELS: tuple[str, ...] = (
    "logging", "warning", "error", "critical",
    "sla", "backup", "restore", "sql", "control", "test",
)


def set_group_level(
    *,
    group: str,
    level: str,
    allow_command: int | None = None,
    groups_path: Path | None = None,
) -> dict[str, Any]:
    """Give a discovered group its notify level, without opening the file.

    `save-updates` finds every group the bot is in and writes it with **no level and no command
    permission** — correct, because discovering a chat is not the same as deciding what it is for.
    But the step after it had no command at all: standing a node up on 2026-09-10 meant hand-editing
    `telegram_groups.json` eight times, once per group, in a file whose records the toolkit also
    rewrites. A hand-edit that races a writer is a hand-edit that gets lost.

    ``group`` matches the id exactly, or the title case-insensitively; a substring is accepted only
    when it names exactly one group, because "Errors" matching both "Errors" and "SQL Errors" and
    silently taking the first is how the wrong chat gets the alerts.
    """
    path = Path(groups_path) if groups_path else GROUPS_PATH
    records = load_json_list(path, root_key="telegram_groups")
    if not records:
        raise RuntimeError(
            f"no groups in {path}. Run `save-updates` first: a group has to be discovered "
            "before it can be given a level.")

    wanted = str(group or "").strip()
    exact = [item for item in records
             if str(item.get("group_id", "")) == wanted
             or str(item.get("title", "")).casefold() == wanted.casefold()]
    matches = exact or [item for item in records
                        if wanted.casefold() in str(item.get("title", "")).casefold()]
    if not matches:
        titles = ", ".join(sorted(str(item.get("title") or item.get("group_id")) for item in records))
        raise RuntimeError(f"no group matches {wanted!r}. Known: {titles}")
    if len(matches) > 1:
        titles = ", ".join(sorted(str(item.get("title")) for item in matches))
        raise RuntimeError(
            f"{wanted!r} matches {len(matches)} groups ({titles}). Name one exactly, or use its id.")

    target = matches[0]
    before = {"notify_level": target.get("notify_level"),
              "allow_command": target.get("allow_command")}
    target["notify_level"] = str(level or "")
    if allow_command is not None:
        target["allow_command"] = int(allow_command)
    write_json_list(path, root_key="telegram_groups", items=records)
    return {
        "group_id": str(target.get("group_id")),
        "title": str(target.get("title")),
        "before": before,
        "after": {"notify_level": target["notify_level"],
                  "allow_command": target.get("allow_command")},
        "path": str(path),
        "known_levels": list(KNOWN_NOTIFY_LEVELS),
        "unknown_level": str(level or "") not in KNOWN_NOTIFY_LEVELS,
    }


def set_user_level(
    *,
    user: str,
    level: int,
    users_path: Path | None = None,
) -> dict[str, Any]:
    """Give a discovered Telegram user the level that decides which commands they may run.

    The counterpart of :func:`set_group_level`, and missing until 2026-09-11. Intake records every
    sender at ``user_type: 0``, so the first command anyone sends to a new node answers "Permission
    denied (user_type=0)", and the only way past it was editing ``user_type`` in
    ``telegram_users.json`` by hand — a file the intake itself rewrites every second on a running
    node. Found configuring the 0.15.0 dry-run node, where the operator's own ``/spbot_self_status``
    was refused four times.

    ``user`` matches the numeric id exactly, or the username with or without ``@``,
    case-insensitively. There is no substring match: a level is a permission, and granting it to
    the wrong person because a fragment matched is not a mistake worth making easy.

    A command with ``command_type`` N runs in a private chat for a user whose level is at least N
    (``commands.can_run_command``); 0 is the public tier, so level 0 runs public commands only.
    """
    if int(level) < 0:
        raise RuntimeError("a user level is 0 or more; 0 runs public commands only. "
                           "To stop a user, set their status in the file instead.")
    path = Path(users_path) if users_path else USERS_PATH
    records = load_json_list(path, root_key="telegram_users")
    if not records:
        raise RuntimeError(
            f"no users in {path}. A user is recorded when they first message the bot; send it "
            "anything, let the intake run (the daemon does it every second), then run this again.")

    wanted = str(user or "").strip()
    name = wanted.lstrip("@").casefold()
    matches = [item for item in records
               if str(item.get("user_id", "")) == wanted
               or (name and str(item.get("username", "")).casefold() == name)]
    if not matches:
        known = ", ".join(sorted(
            f"{item.get('username') or item.get('first_name') or '?'} ({item.get('user_id')})"
            for item in records))
        raise RuntimeError(f"no user matches {wanted!r}. Known: {known}")
    if len(matches) > 1:
        raise RuntimeError(f"{wanted!r} matches {len(matches)} users. Use the numeric id.")

    target = matches[0]
    before = target.get("user_type")
    target["user_type"] = int(level)
    write_json_list(path, root_key="telegram_users", items=records)
    return {
        "user_id": str(target.get("user_id")),
        "username": str(target.get("username") or ""),
        "before": {"user_type": before},
        "after": {"user_type": target["user_type"]},
        "path": str(path),
    }
