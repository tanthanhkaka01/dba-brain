import json

from db_ops.db import DbOpsStore
from db_ops.telegram.updates import TelegramUpdatePaths, save_updates


def load_users(path):
    return json.loads(path.read_text(encoding="utf-8"))["telegram_users"]


def test_save_updates_adds_new_chat_members_to_users_json(tmp_path):
    groups_path = tmp_path / "telegram_groups.json"
    users_path = tmp_path / "telegram_users.json"
    sqlite_path = tmp_path / "runtime.sqlite"
    groups_path.write_text('{"telegram_groups":[]}', encoding="utf-8")
    users_path.write_text(
        json.dumps(
            {
                "telegram_users": [
                    {
                        "user_id": "100",
                        "user_type": 2,
                        "status": "active",
                        "first_name": "Admin",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = save_updates(
        [
            {
                "update_id": 9001,
                "message": {
                    "message_id": 12,
                    "date": 1_779_478_400,
                    "chat": {"id": -200, "type": "group", "title": "DBA"},
                    "from": {"id": 100, "is_bot": False, "first_name": "Admin"},
                    "new_chat_members": [
                        {
                            "id": 300,
                            "is_bot": False,
                            "first_name": "New",
                            "last_name": "User",
                            "username": "new_user",
                        }
                    ],
                },
            }
        ],
        paths=TelegramUpdatePaths(groups_path=groups_path, users_path=users_path),
        store=DbOpsStore(sqlite_path),
    )

    users_by_id = {row["user_id"]: row for row in load_users(users_path)}
    assert result["users_changed"] == 1
    assert users_by_id["100"]["user_type"] == 2
    assert users_by_id["300"]["first_name"] == "New"
    assert users_by_id["300"]["last_name"] == "User"
    assert users_by_id["300"]["username"] == "new_user"
    assert users_by_id["300"]["status"] == "active"
