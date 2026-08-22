import json
import sqlite3
import subprocess

#: Captured before any stub replaces it. Queueing a reply goes through the common CLI now
#: and shares this module's subprocess.run, so the stubs below let those calls through to
#: the real thing - the row has to actually land in the test's store for the assertions
#: about the reply text to mean anything.
_REAL_RUN = subprocess.run
import sys
from pathlib import Path

import pytest

from db_ops.db import DbOpsStore
from db_ops.telegram import command_processor
from db_ops.telegram.command_processor import process_one_command_message
from conftest import shipped_config


PRODUCTION_COMMANDS_PATH = shipped_config("telegram_support_commands.json")
COMMAND_TEXT = "spbot_report_metric_history"


def _production_command():
    data = json.loads(PRODUCTION_COMMANDS_PATH.read_text(encoding="utf-8"))
    return next(
        command
        for command in data["telegram_support_commands"]
        if command["command_text"] == COMMAND_TEXT
    )


def _write_json(path, root_key, rows):
    path.write_text(
        json.dumps({root_key: rows}, ensure_ascii=False),
        encoding="utf-8",
    )


def _insert_command_message(sqlite_path, text, *, user_id="100"):
    store = DbOpsStore(sqlite_path)
    store.upsert_telegram_messages(
        [
            {
                "update_id": 10,
                "message_id": 10,
                "message_date": 1_779_478_400,
                "chat_id": user_id,
                "chat_type": "private",
                "user_id": user_id,
                "text": text,
                "raw": {"from": {"id": int(user_id), "username": "admin"}},
            }
        ]
    )
    store.sync_telegram_command_messages(command_prefix="/spbot")
    with sqlite3.connect(sqlite_path) as connection:
        row = connection.execute(
            """
            SELECT telegram_command_message_id
            FROM telegram_command_messages
            WHERE chat_id = ? AND message_id = 10
            """,
            (user_id,),
        ).fetchone()
    return int(row[0])


def _prepare(tmp_path, *, text, user_type=2):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    _write_json(commands_path, "telegram_support_commands", [_production_command()])
    _write_json(
        tmp_path / "telegram_users.json",
        "telegram_users",
        [{"user_id": "100", "user_type": user_type, "status": "active"}],
    )
    _write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])
    command_message_id = _insert_command_message(sqlite_path, text)
    return sqlite_path, commands_path, command_message_id


def _messages(sqlite_path):
    with sqlite3.connect(sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        return list(
            connection.execute(
                """
                SELECT message_text, metadata_json
                FROM telegram_send_messages
                ORDER BY send_tlgmsg_id ASC
                """
            )
        )


def test_production_metric_history_command_contract():
    command = _production_command()
    config = command["action_config"]

    assert command["command_id"] == 9
    assert command["node_role"] == "worker"
    assert command["command_type"] == 2
    assert command["action_type"] == "cli_execute"
    assert config["working_dir"] == "tools/db_ops"
    assert not config.get("background", False)
    assert not config.get("detached", False)
    assert config["defaults"] == {"summary_limit": 150, "dedupe_seconds": 0}
    assert [parameter["name"] for parameter in config["parameters"]] == [
        "server_id",
        "metric_code",
        "hours",
    ]
    assert [parameter["position"] for parameter in config["parameters"]] == [1, 2, 3]
    assert all(parameter["validator"] == "regex" for parameter in config["parameters"])
    assert config["command_argv"] == [
        "{python}",
        "-m",
        "db_ops.reports.cli",
        "--config",
        "{config_path}",
        "metric-history-report",
        "--server-id",
        "{server_id}",
        "--metric-code",
        "{metric_code}",
        "--hours",
        "{hours}",
        "--summary-limit",
        "{summary_limit}",
        "--dedupe-seconds",
        "{dedupe_seconds}",
    ]


@pytest.mark.parametrize(
    ("text", "expected_prompt"),
    [
        (f"/{COMMAND_TEXT}", "Please input server_id"),
        (f"/{COMMAND_TEXT} ACME-192-0-2-115", "Please input metric_code"),
        (
            f"/{COMMAND_TEXT} ACME-192-0-2-115 INSTANCE_STATUS",
            "Please input a positive number of hours",
        ),
    ],
)
def test_metric_history_missing_argument_prompts_for_next_value(
    tmp_path, text, expected_prompt
):
    sqlite_path, commands_path, command_message_id = _prepare(tmp_path, text=text)

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )

    assert result["status"] == "waiting_for_input"
    assert _messages(sqlite_path)[-1]["message_text"] == expected_prompt


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        (
            "../server INSTANCE_STATUS 24",
            "server_id may contain only letters, numbers, dot, underscore, colon, and hyphen",
        ),
        (
            "ACME-192-0-2-115 INSTANCE/STATUS 24",
            "metric_code may contain only letters, numbers, dot, underscore, colon, and hyphen",
        ),
        ("ACME-192-0-2-115 INSTANCE_STATUS 0", "hours must be a positive integer"),
        ("ACME-192-0-2-115 INSTANCE_STATUS -1", "hours must be a positive integer"),
        ("ACME-192-0-2-115 INSTANCE_STATUS 1.5", "hours must be a positive integer"),
    ],
)
def test_metric_history_rejects_unsafe_or_non_positive_arguments(
    tmp_path, arguments, expected_error
):
    sqlite_path, commands_path, command_message_id = _prepare(
        tmp_path,
        text=f"/{COMMAND_TEXT} {arguments}",
    )

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )

    assert result["status"] == "action_failed"
    assert expected_error in _messages(sqlite_path)[-1]["message_text"]


def test_metric_history_bot_suffix_builds_exact_argv_and_reports_counts(
    tmp_path, monkeypatch
):
    sqlite_path, commands_path, command_message_id = _prepare(
        tmp_path,
        text=(
            f"/{COMMAND_TEXT}@it_dev_code_sp_bot "
            "ACME-192-0-2-115 INSTANCE_STATUS 24"
        ),
    )
    calls = []

    def fake_run(argv, **kwargs):
        if "queue-telegram-message" in argv:
            # Not the call under test: queueing the reply goes through the common CLI now, and
            # it shares this module-level subprocess.run. Let it succeed so the argv this test
            # actually asserts on is the action's, not the notification's - and the row still
            # has to be written, because these tests read the reply back out of the store.
            return _REAL_RUN(argv, **kwargs)
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "server_id": "ACME-192-0-2-115",
                    "metric_code": "INSTANCE_STATUS",
                    "hours": 24,
                    "row_count": 31,
                    "queued": 2,
                    "status": "success",
                    "exit_code": 0,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(command_processor.subprocess, "run", fake_run)
    config_path = tmp_path / "config.json"

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
        config_path=config_path,
    )

    argv, kwargs = calls[0]
    assert argv == [
        sys.executable,
        "-m",
        "db_ops.reports.cli",
        "--config",
        str(config_path),
        "metric-history-report",
        "--server-id",
        "ACME-192-0-2-115",
        "--metric-code",
        "INSTANCE_STATUS",
        "--hours",
        "24",
        "--summary-limit",
        "150",
        "--dedupe-seconds",
        "0",
    ]
    assert kwargs["shell"] is False
    messages = _messages(sqlite_path)
    assert result["status"] == "processed"
    assert messages[0]["message_text"] == (
        "Metric history report started for server_id=ACME-192-0-2-115, "
        "metric_code=INSTANCE_STATUS, hours=24."
    )
    assert messages[-1]["message_text"] == (
        "Metric history report completed for server_id=ACME-192-0-2-115, "
        "metric_code=INSTANCE_STATUS, hours=24. row_count=31, queued=2."
    )
    metadata = json.loads(messages[-1]["metadata_json"])
    assert metadata["status"] == "success"
    assert metadata["cli_result"]["row_count"] == 31
    assert metadata["cli_result"]["queued"] == 2


def test_metric_history_failure_exposes_identifiers_and_safe_error(tmp_path, monkeypatch):
    sqlite_path, commands_path, command_message_id = _prepare(
        tmp_path,
        text=f"/{COMMAND_TEXT} ACME-192-0-2-115 INSTANCE_STATUS 24",
    )

    def fake_run(argv, **kwargs):
        if "queue-telegram-message" in argv:
            # Not the call under test: queueing the reply goes through the common CLI now, and
            # it shares this module-level subprocess.run. Let it succeed so the argv this test
            # actually asserts on is the action's, not the notification's - and the row still
            # has to be written, because these tests read the reply back out of the store.
            return _REAL_RUN(argv, **kwargs)
        return subprocess.CompletedProcess(
            argv,
            7,
            stdout="",
            stderr="database connection failed",
        )

    monkeypatch.setattr(command_processor.subprocess, "run", fake_run)

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
        config_path=tmp_path / "config.json",
    )

    assert result["status"] == "action_failed"
    assert _messages(sqlite_path)[-1]["message_text"] == (
        "Metric history report failed for server_id=ACME-192-0-2-115, "
        "metric_code=INSTANCE_STATUS, hours=24.\n"
        "Exit code: 7\n"
        "Error: database connection failed"
    )


def test_metric_history_requires_admin_permission(tmp_path, monkeypatch):
    sqlite_path, commands_path, command_message_id = _prepare(
        tmp_path,
        text=f"/{COMMAND_TEXT} ACME-192-0-2-115 INSTANCE_STATUS 24",
        user_type=1,
    )
    called = False

    def fail_if_called(*args, **kwargs):
        if args and "queue-telegram-message" in args[0]:
            # Not the call under test: queueing the reply goes through the common CLI now, and
            # it shares this module-level subprocess.run. Let it succeed so the *args this test
            # actually asserts on is the action's, not the notification's - and the row still
            # has to be written, because these tests read the reply back out of the store.
            return _REAL_RUN(*args, **kwargs)
        nonlocal called
        called = True

    monkeypatch.setattr(command_processor.subprocess, "run", fail_if_called)

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )

    assert result["status"] == "permission_denied"
    assert called is False
    assert f"Permission denied for /{COMMAND_TEXT}" in _messages(sqlite_path)[-1][
        "message_text"
    ]
