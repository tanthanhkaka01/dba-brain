"""A pending command message must be dispatched exactly once.

The Telegram workflow runs every second and only marks a command message done after its
action finishes. Without an exclusive claim the next cycle re-reads the same pending row and
runs the command again — observed in production as five "Force hourly metrics report started"
replies for one /spbot_report_hourly_metrics, with the collect running repeatedly.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from db_ops.db import DbOpsStore
from db_ops.telegram import command_processor
from db_ops.telegram.command_processor import CLAIM_STALE_SECONDS, process_pending_command_messages

from test_telegram_report_commands import insert_command_message, write_json


def write_echo_command(path):
    """A cli_execute command whose CLI is a no-op, so the test exercises dispatch, not the CLI."""
    write_json(
        path,
        "telegram_support_commands",
        [
            {
                "command_id": 4,
                "command_text": "spbot_report_hourly_metrics",
                "reply_default": 0,
                "reply_text": "",
                "command_type": 2,
                "is_group": 1,
                "is_private": 1,
                "need_file": 0,
                "action_type": "cli_execute",
                "action_config": {
                    "working_dir": "tools/db_ops",
                    "command_argv": ["python", "-c", "print('{\"status\": \"success\"}')"],
                    "start_text": "Force hourly metrics report started for {target_ip}",
                    "success_text": "Force hourly metrics report completed for {target_ip}",
                    "failure_text": "Force hourly metrics report failed for {target_ip}",
                    "parameters": [
                        {
                            "name": "target_ip",
                            "source": "arg",
                            "position": 1,
                            "required": True,
                            "validator": "target_ip",
                            "prompt_text": "Please input target_ip",
                        }
                    ],
                },
            }
        ],
    )


def prepare(tmp_path):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_echo_command(commands_path)
    write_json(tmp_path / "telegram_users.json", "telegram_users",
               [{"user_id": "100", "user_type": 2, "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])
    message_id = insert_command_message(sqlite_path, "/spbot_report_hourly_metrics 192.0.2.116")
    return sqlite_path, commands_path, message_id


def claimed_at(sqlite_path, message_id):
    with sqlite3.connect(sqlite_path) as conn:
        return conn.execute(
            "SELECT claimed_at FROM telegram_command_messages WHERE telegram_command_message_id = ?",
            (message_id,),
        ).fetchone()[0]


def test_a_pending_command_is_dispatched_once_even_if_the_workflow_runs_again(tmp_path, monkeypatch):
    sqlite_path, commands_path, message_id = prepare(tmp_path)
    dispatched = []

    def fake_action(*, store, row, command, args, sqlite_path, config_path, source_id):
        # Simulate the real thing: the action runs, and the second workflow cycle fires while
        # it is still in flight (the row is still command_status = 0).
        dispatched.append(args[0])
        second = process_pending_command_messages(sqlite_path=sqlite_path, commands_path=commands_path)
        assert second["already_claimed"] == 1
        assert second["processed"] == 0
        return {"status": "success"}

    monkeypatch.setattr(command_processor, "execute_command_action", fake_action)

    first = process_pending_command_messages(sqlite_path=sqlite_path, commands_path=commands_path)

    assert first["processed"] == 1
    assert dispatched == ["192.0.2.116"]  # one dispatch, not one per workflow cycle


def test_a_claim_left_behind_by_a_killed_worker_is_retried_once_it_goes_stale(tmp_path, monkeypatch):
    """A container restart kills the workflow mid-command: the row stays pending and claimed.
    It must not be lost — after the stale window another cycle may take it."""
    sqlite_path, commands_path, message_id = prepare(tmp_path)
    store = DbOpsStore(sqlite_path)
    now = datetime.now(timezone.utc)

    abandoned = (now - timedelta(seconds=CLAIM_STALE_SECONDS + 60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert store.claim_telegram_command_message(
        telegram_command_message_id=message_id, claimed_at=abandoned, stale_before=abandoned,
    )

    dispatched = []
    monkeypatch.setattr(
        command_processor, "execute_command_action",
        lambda **kwargs: dispatched.append(kwargs["args"][0]) or {"status": "success"},
    )
    result = process_pending_command_messages(sqlite_path=sqlite_path, commands_path=commands_path)

    assert result["processed"] == 1
    assert dispatched == ["192.0.2.116"]
    assert claimed_at(sqlite_path, message_id) > abandoned
