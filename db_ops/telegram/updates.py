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
    users_by_id = {str(item.get("user_id", "")): item for item in users_data
                   if str(item.get("user_id") or "").strip()}
    pending_users = [item for item in users_data if not str(item.get("user_id") or "").strip()]
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
            if not existing_user:
                # A level set before this person ever messaged waits under their username with no
                # id (see `set_user_level`). Adopt it here, or they arrive at level 0 and the level
                # the operator already set stays orphaned.
                adopted = adopt_pending_user(pending_users, user_record)
                if adopted:
                    user_record = adopted
                    users_changed = True
            merged_user = merge_user_record(existing_user, user_record)
            if merged_user != existing_user:
                users_by_id[user_record["user_id"]] = merged_user
                users_changed = True

    saved_messages = store.upsert_telegram_messages(messages)
    if groups_changed:
        write_json_list(target_paths.groups_path, root_key="telegram_groups", items=list(groups_by_id.values()))
    if users_changed:
        write_json_list(target_paths.users_path, root_key="telegram_users",
                        items=list(users_by_id.values()) + pending_users)

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


def adopt_pending_user(records: list[dict[str, Any]], update_user: dict[str, Any]) -> dict[str, Any]:
    """The pre-authorised record for this username, if one is waiting, with its id filled in.

    `set_user_level` can be run before anybody has messaged the node - it has to be, because the
    intake that would record them only runs under the daemon, which starts later. Such a record
    carries the level and an empty ``user_id``. This is where it stops being pending: the first
    message from that username adopts it, so the level the operator set is the level they have on
    their first command rather than after it is refused.

    Matching is on username, casefolded, and the pending record is removed from ``records`` so the
    two never coexist - a second record under the numeric id would silently shadow the level.
    """
    name = str(update_user.get("username") or "").casefold()
    if not name:
        return {}
    for index, item in enumerate(records):
        if str(item.get("user_id") or "").strip():
            continue
        if str(item.get("username") or "").casefold() != name:
            continue
        pending = records.pop(index)
        adopted = dict(update_user)
        adopted["user_type"] = pending.get("user_type", adopted.get("user_type", 0))
        adopted["note"] = "Pre-authorised by user-level; adopted on first contact."
        return adopted
    return {}


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


def add_group(
    *,
    group_id: str,
    level: str = "",
    title: str = "",
    allow_command: int | None = None,
    bot_token: str = "",
    groups_path: Path | None = None,
    api_url: str = "",
    verify: bool = True,
) -> dict[str, Any]:
    """Register a chat the bot has never been spoken to in.

    `save-updates` learns a group from `getUpdates`, which only ever reports a chat somebody has
    **posted in**. So standing a node up meant going to nine chats and typing something in each,
    then hoping the intake had run before `group-level` was reached - and `group-level` refuses
    outright until then, with "no groups in telegram_groups.json". A brand-new alert chat that
    nobody has written in yet cannot be configured at all, which is the normal state of a chat
    created *for* alerts.

    **The chat id is verified with Telegram rather than trusted.** A wrong id is the failure this
    command would otherwise introduce: alerts route to a chat that does not exist, or - worse, and
    entirely possible with a mistyped digit - to somebody else's. `getChat` answers with the title,
    which is written down, so the operator reads back the name of the chat they actually configured
    instead of the number they typed. Pass ``verify=False`` only where there is no token to check
    with; the entry is then marked so the file itself says it was never confirmed.
    """
    path = Path(groups_path) if groups_path else GROUPS_PATH
    wanted = str(group_id or "").strip()
    if not wanted:
        raise RuntimeError("group_id is required: the numeric chat id, e.g. -1001234567890.")

    confirmed_title, confirmed_type, verified = str(title or "").strip(), "", False
    if verify:
        if not str(bot_token or "").strip():
            raise RuntimeError(
                "no bot token, so the chat id cannot be confirmed. Store one and run "
                "`telegram use-bot --ref <SECRET_REF>` first, or pass verify=False to write the "
                "entry unconfirmed.")
        from db_ops.telegram.api import DEFAULT_TELEGRAM_API_URL, call_telegram_api

        answer = call_telegram_api(
            bot_token=str(bot_token), method_name="getChat", payload={"chat_id": wanted},
            api_url=api_url or DEFAULT_TELEGRAM_API_URL)
        chat = answer.get("result") or {}
        # Telegram's own title wins over anything typed: the point of asking is to find out.
        confirmed_title = str(chat.get("title") or chat.get("username") or confirmed_title)
        confirmed_type = str(chat.get("type") or "")
        verified = True

    records = load_json_list(path, root_key="telegram_groups")
    existing = next((item for item in records if str(item.get("group_id", "")) == wanted), None)
    if existing is not None:
        # Not an error: re-running a registration is how a node is brought back to a known state,
        # and refusing would make this command unusable in the script that stands a node up.
        before = dict(existing)
        if confirmed_title:
            existing["title"] = confirmed_title
        if level:
            existing["notify_level"] = level
        if allow_command is not None:
            existing["allow_command"] = int(allow_command)
        existing["status"] = "active"
        write_json_list(path, root_key="telegram_groups", items=records)
        return {"group_id": wanted, "title": existing.get("title", ""), "created": False,
                "verified": verified, "before": before, "after": dict(existing), "path": str(path)}

    record = {
        "group_id": wanted,
        "title": confirmed_title,
        "group_type": confirmed_type or "group",
        "notify_level": level,
        "source_notify_level": level,
        # `save-updates` writes a discovered group with no command permission at all, and this
        # keeps that default: registering a chat says where alerts go, not who may drive the node
        # from it.
        "allow_command": int(allow_command) if allow_command is not None else 0,
        "status": "active",
        "note": ("Registered with `telegram group-add`."
                 if verified else
                 "Registered with `telegram group-add` WITHOUT confirming the id with Telegram."),
    }
    records.append(record)
    write_json_list(path, root_key="telegram_groups", items=records)
    return {"group_id": wanted, "title": record["title"], "created": True,
            "verified": verified, "before": None, "after": record, "path": str(path)}


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
    pending: bool = False,
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

    wanted = str(user or "").strip()
    name = wanted.lstrip("@").casefold()
    matches = [item for item in records
               if str(item.get("user_id", "")) == wanted
               or (name and str(item.get("username", "")).casefold() == name)]

    if not matches and not pending:
        # Unchanged, and deliberately so. An unknown name is far more often a typo than somebody
        # who has not arrived yet, and a level is a permission: granting one to a name nobody has
        # claimed is how it reaches the wrong person. Pre-authorising is possible but it is asked
        # for, never inferred - see the `pending` branch below.
        if not records:
            raise RuntimeError(
                f"no users in {path}. A user is recorded when they first message the bot; send it "
                "anything, let the intake run (the daemon does it every second), then run this "
                "again - or pass pending to set the level before they ever message.")
        known = ", ".join(sorted(
            f"{item.get('username') or item.get('first_name') or '?'} ({item.get('user_id')})"
            for item in records))
        raise RuntimeError(
            f"no user matches {wanted!r}. Known: {known}. "
            "Pass pending to set a level for somebody who has not messaged this bot yet.")

    # Nobody has messaged this node yet, or not this person. Until 2026-09-17 both were a refusal,
    # and the second half of that refusal could not be satisfied in the order a node is built:
    # intake only runs under the daemon, and the daemon starts *after* this step. So the operator
    # could not be given a level before the clock started, met "Permission denied (user_type=0)" on
    # their first command, and fixed it by hand afterwards - measured on the 0.17.0 run, which
    # reached hour 15 at user_type 0, and again on 2026-09-17 at forty minutes.
    #
    # `add_group` is the same fix for chats (a chat created *for* alerts has nobody posting in it),
    # and it verifies the id with `getChat`. There is no bot API that resolves a *username* to a
    # user id, so the equivalent here is a record marked pending: it carries the level and no
    # `user_id`, and `merge_user_record` adopts it the moment that username first speaks.
    if not matches:
        if not name or name.isdigit():
            raise RuntimeError(
                f"{wanted!r} cannot be pre-authorised: it must be a @username. The intake matches "
                "an arriving message to a pending record by username - a numeric id it has never "
                "seen matches nothing, so the level would never be adopted.")
        pending = {
            "user_id": "",
            "is_bot": False,
            "user_type": int(level),
            "first_name": "",
            "last_name": "",
            "username": name,
            "language_code": "",
            "status": "active",
            "note": "Pre-authorised by user-level; awaiting first contact.",
        }
        records.append(pending)
        write_json_list(path, root_key="telegram_users", items=records)
        return {
            "user_id": "",
            "username": name,
            "before": {"user_type": None},
            "after": {"user_type": int(level)},
            "pending": True,
            "path": str(path),
            "note": (f"@{name} has not messaged this bot yet, so there is no id to attach the "
                     "level to. The level is recorded against the username and takes effect the "
                     "moment they first message the bot."),
        }
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
