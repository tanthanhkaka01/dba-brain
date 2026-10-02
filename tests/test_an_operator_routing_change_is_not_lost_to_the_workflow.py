"""An operator's change to the Telegram routing files is not overwritten by the running workflow.

`telegram_groups.json` and `telegram_users.json` have two writers: the workflow's `save_updates`,
which reads both files every second and writes them back when it learned a chat or a user, and the
operator's `group-add`, `group-level` and `user-level`. A change the operator made between the
workflow's read and its write was lost - the routing table silently went back (review 0.25.0, F8.2).

Each of them now holds the file's lock from its read to its write, so one waits for the other.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from db_ops.lib.file_lock import FileLock
from db_ops.telegram import updates


def _files(tmp_path: Path) -> updates.TelegramUpdatePaths:
    groups = tmp_path / "telegram_groups.json"
    users = tmp_path / "telegram_users.json"
    groups.write_text(json.dumps({"telegram_groups": [
        {"group_id": "-100", "title": "DBABRAIN - Errors", "notify_level": "", "allow_command": 0}]}),
        encoding="utf-8")
    users.write_text(json.dumps({"telegram_users": []}), encoding="utf-8")
    return updates.TelegramUpdatePaths(groups_path=groups, users_path=users)


def _runs_only_after_the_lock_is_released(lock_path: Path, work) -> bool:
    done = threading.Event()
    with FileLock(lock_path):
        thread = threading.Thread(target=lambda: (work(), done.set()), daemon=True)
        thread.start()
        time.sleep(0.6)
        waited = not done.is_set()
    thread.join(timeout=30)
    return waited and done.is_set()


def test_group_level_waits_for_the_file_s_lock(tmp_path):
    paths = _files(tmp_path)

    assert _runs_only_after_the_lock_is_released(paths.groups_path, lambda: updates.set_group_level(
        group="-100", level="error", groups_path=paths.groups_path))

    group = json.loads(paths.groups_path.read_text(encoding="utf-8"))["telegram_groups"][0]
    assert group["notify_level"] == "error"


def test_the_workflow_waits_for_the_file_s_lock_too(tmp_path):
    paths = _files(tmp_path)

    class Store:
        @staticmethod
        def upsert_telegram_messages(messages):
            return len(messages)

    assert _runs_only_after_the_lock_is_released(paths.groups_path, lambda: updates.save_updates(
        [], paths=paths, store=Store()))
