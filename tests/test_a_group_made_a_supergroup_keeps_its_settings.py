"""A group Telegram makes a supergroup keeps what it was for (0.34.0).

Telegram gives a group a new id when it becomes a supergroup - an admin change or a history setting
is enough - and every message after it arrives under that id. Discovered as a new chat, the row was
inert: no notify level, no command level. So an alert group that moved lost its alerts in silence,
and on 2026-10-31 a question in *System - AI assists* waited, unread, under its new id. The operator:
*when a group becomes a supergroup and its id changes, add a row and set the old one active false.*
"""

from __future__ import annotations

import json

from dbabrain.lib.config import _load_telegram_groups_file
from dbabrain.telegram import updates

OLD = "-4012345678"
NEW = "-1001234567890"


def _groups(**old_fields):
    old = {"group_id": OLD, "title": "System - AI assists", "group_type": "group", "notify_level": "ai_assist",
           "source_notify_level": "", "allow_command": 10, "active": True, "note": "Loaded from Telegram getUpdates."}
    old.update(old_fields)
    return {OLD: old}


def test_the_move_carries_every_setting_and_retires_the_old_row():
    groups = _groups(custom_flag="kept")
    assert updates.migrate_group(groups, old_id=OLD, new_id=NEW,
                                 chat={"id": int(NEW), "title": "System - AI assists", "type": "supergroup"},
                                 when="2026-10-31T07:53:15Z")
    new, old = groups[NEW], groups[OLD]
    assert (new["notify_level"], new["allow_command"], new["custom_flag"], new["active"]) == ("ai_assist", 10, "kept", True)
    assert new["group_type"] == "supergroup" and OLD in new["note"]
    assert old["active"] is False and NEW in old["note"]
    assert old["notify_level"] == "ai_assist", "the retired row is history, left as it was"


def test_a_second_pass_changes_nothing_and_never_undoes_a_later_edit():
    groups = _groups()
    updates.migrate_group(groups, old_id=OLD, new_id=NEW)
    groups[NEW]["allow_command"] = 50
    assert updates.migrate_group(groups, old_id=OLD, new_id=NEW) is False
    assert groups[NEW]["allow_command"] == 50


def test_both_messages_of_the_move_are_recognised():
    assert updates.migration_of({"chat": {"id": int(OLD)}, "migrate_to_chat_id": int(NEW)}) == (OLD, NEW)
    assert updates.migration_of({"chat": {"id": int(NEW)}, "migrate_from_chat_id": int(OLD)}) == (OLD, NEW)
    assert updates.migration_of({"chat": {"id": int(NEW)}, "text": "hi"}) is None


def test_the_intake_moves_the_group_and_the_level_follows_it(tmp_path):
    groups_path = tmp_path / "telegram_groups.json"
    users_path = tmp_path / "telegram_users.json"
    groups_path.write_text(json.dumps({"telegram_groups": list(_groups().values())}), encoding="utf-8")
    users_path.write_text(json.dumps({"telegram_users": []}), encoding="utf-8")

    class Store:
        def upsert_telegram_messages(self, messages):
            return len(messages)

    old_chat = {"id": int(OLD), "type": "group", "title": "System - AI assists"}
    new_chat = {"id": int(NEW), "type": "supergroup", "title": "System - AI assists"}
    batch = [{"update_id": 1, "message": {"message_id": 9, "date": 1, "chat": old_chat, "from": {"id": 1},
                                          "migrate_to_chat_id": int(NEW)}},
             {"update_id": 2, "message": {"message_id": 1, "date": 1, "chat": new_chat, "from": {"id": 1},
                                          "migrate_from_chat_id": int(OLD)}}]
    updates.save_updates(batch, paths=updates.TelegramUpdatePaths(groups_path=groups_path, users_path=users_path),
                         store=Store())
    rows = {row["group_id"]: row for row in json.loads(groups_path.read_text(encoding="utf-8"))["telegram_groups"]}
    assert rows[NEW]["notify_level"] == "ai_assist" and rows[NEW]["active"] is True
    assert rows[OLD]["active"] is False
    # Every app's resolver now sends the level to the new id - nothing names an id anywhere else.
    assert _load_telegram_groups_file(groups_path)["ai_assist"] == NEW


def test_group_migrate_does_it_by_hand_for_a_move_the_intake_did_not_see(tmp_path):
    path = tmp_path / "telegram_groups.json"
    rows = list(_groups().values()) + [{"group_id": NEW, "title": "System - AI assists", "group_type": "supergroup",
                                        "notify_level": "", "allow_command": 0, "active": True, "note": "Loaded"}]
    path.write_text(json.dumps({"telegram_groups": rows}), encoding="utf-8")
    answer = updates.migrate_group_record(from_id=OLD, to_id=NEW, groups_path=path)
    assert answer["changed"] is True
    saved = {row["group_id"]: row for row in json.loads(path.read_text(encoding="utf-8"))["telegram_groups"]}
    assert saved[NEW]["notify_level"] == "ai_assist" and saved[OLD]["active"] is False
    assert updates.migrate_group_record(from_id=OLD, to_id=NEW, groups_path=path)["changed"] is False


def test_group_level_by_title_means_the_live_row_not_its_retired_twin(tmp_path):
    path = tmp_path / "telegram_groups.json"
    groups = _groups()
    updates.migrate_group(groups, old_id=OLD, new_id=NEW)
    path.write_text(json.dumps({"telegram_groups": list(groups.values())}), encoding="utf-8")
    answer = updates.set_group_level(group="System - AI assists", level="ai_assist", allow_command=20,
                                     groups_path=path)
    assert answer["group_id"] == NEW


def test_upgrade_config_turns_the_assistants_chat_ids_into_the_groups_level(tmp_path):
    """2.87.58 - 2.87.61 listed the assistant's chats by id in ai_assist.json; the list becomes the
    level on each listed group with none, and a group with another level is named, not overwritten."""
    from dbabrain.common.config_upgrade import _stated_ai_assist
    from dbabrain.lib import ai_assist as A

    document = A.shipped_document()
    document.pop("telegram_chat")
    document["chat_ids"] = [OLD, NEW, "-77"]
    (tmp_path / "ai_assist.json").write_text(json.dumps(document), encoding="utf-8")
    rows = [{"group_id": OLD, "notify_level": "", "active": True},
            {"group_id": NEW, "notify_level": "", "active": True},
            {"group_id": "-77", "notify_level": "sql", "active": True}]
    (tmp_path / "telegram_groups.json").write_text(json.dumps({"telegram_groups": rows}), encoding="utf-8")
    answer = _stated_ai_assist(tmp_path, dry_run=False, before_write=lambda path: None)
    saved = json.loads((tmp_path / "ai_assist.json").read_text(encoding="utf-8"))
    assert "chat_ids" not in saved and saved["telegram_chat"] == "ai_assist"
    A.parse_ai_assist(saved)
    levels = {row["group_id"]: row["notify_level"]
              for row in json.loads((tmp_path / "telegram_groups.json").read_text(encoding="utf-8"))["telegram_groups"]}
    assert levels == {OLD: "ai_assist", NEW: "ai_assist", "-77": "sql"}
    assert answer["files"][0]["conflicts"][0]["where"] == "telegram_groups.json[-77]"
