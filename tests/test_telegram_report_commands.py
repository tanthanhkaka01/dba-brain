import json
import sqlite3
import subprocess
from conftest import shipped_config

#: Captured before any stub replaces it. Queueing a reply goes through the common CLI now
#: and shares this module's subprocess.run, so the stubs below let those calls through to
#: the real thing - the row has to actually land in the test's store for the assertions
#: about the reply text to mean anything.
_REAL_RUN = subprocess.run
#: Same reason: subprocess.run builds a Popen, so a Popen stub catches the reply queue
#: too. The stubs below hand those calls back to the real one.
_REAL_POPEN = subprocess.Popen
from pathlib import Path

import pytest

from db_ops.db import DbOpsStore
from db_ops.telegram import command_processor
from db_ops.telegram.command_processor import (
    build_cli_argv,
    check_cli_background_tasks,
    process_one_command_message,
)


def write_json(path, root_key, rows):
    path.write_text(json.dumps({root_key: rows}, ensure_ascii=False), encoding="utf-8")


def write_report_command(path):
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
                    "command_template": "python -m db_ops.reports.cli --config {config_path} force-hourly-report --target-ip {target_ip} --summary-limit {summary_limit} --dedupe-seconds {dedupe_seconds}",
                    "start_text": "Force hourly metrics report started for {target_ip}",
                    "success_text": "Force hourly metrics report completed for {target_ip}",
                    "failure_text": "Force hourly metrics report failed for {target_ip}\nExit code: {exit_code}\nError: {error_summary}",
                    "defaults": {
                        "summary_limit": 150,
                        "dedupe_seconds": 0,
                    },
                    "parameters": [
                        {
                            "name": "target_ip",
                            "source": "arg",
                            "position": 1,
                            "required": True,
                            "validator": "target_ip",
                            "prompt_text": "Usage: /spbot_report_hourly_metrics <target_ip>",
                        }
                    ],
                },
            }
        ],
    )


def insert_command_message(sqlite_path, text, *, user_id="100", username="admin"):
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
                "raw": {"from": {"id": int(user_id), "username": username}},
            }
        ]
    )
    store.sync_telegram_command_messages(command_prefix="/spbot")
    with sqlite3.connect(sqlite_path) as conn:
        row = conn.execute(
            "SELECT telegram_command_message_id FROM telegram_command_messages WHERE chat_id = ? AND message_id = 10",
            (user_id,),
        ).fetchone()
    return int(row[0])


def fetch_send_messages(sqlite_path):
    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        return list(
            conn.execute(
                """
                SELECT message_text, source_type, metadata_json
                FROM telegram_send_messages
                ORDER BY send_tlgmsg_id ASC
                """
            )
        )


def prepare(tmp_path, *, user_type=2, text="/spbot_report_hourly_metrics 192.0.2.115"):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_report_command(commands_path)
    write_json(tmp_path / "telegram_users.json", "telegram_users", [{"user_id": "100", "user_type": user_type, "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])
    command_message_id = insert_command_message(sqlite_path, text)
    return sqlite_path, commands_path, command_message_id


def test_report_force_hourly_missing_ip_returns_usage_message(tmp_path):
    sqlite_path, commands_path, command_message_id = prepare(tmp_path, text="/spbot_report_hourly_metrics")

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )

    messages = fetch_send_messages(sqlite_path)
    assert result["status"] == "waiting_for_input"
    assert messages[-1]["message_text"] == "Usage: /spbot_report_hourly_metrics <target_ip>"


def test_report_force_hourly_invalid_ip_returns_validation_error(tmp_path):
    sqlite_path, commands_path, command_message_id = prepare(tmp_path, text="/spbot_report_hourly_metrics not-an-ip")

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
    )

    messages = fetch_send_messages(sqlite_path)
    assert result["status"] == "action_failed"
    assert "Invalid target_ip: not-an-ip." in messages[-1]["message_text"]


def test_report_force_hourly_cli_command_is_built_from_config_defaults(tmp_path, monkeypatch):
    sqlite_path, commands_path, command_message_id = prepare(tmp_path)
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
            stdout=json.dumps({"target_id": "server/sqlserver/db", "created": {"report_ids": [10]}}),
            stderr="",
        )

    monkeypatch.setattr(command_processor.subprocess, "run", fake_run)

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
        config_path=tmp_path / "config.json",
    )

    messages = fetch_send_messages(sqlite_path)
    argv, kwargs = calls[0]
    assert result["status"] == "processed"
    assert argv == [
        "python",
        "-m",
        "db_ops.reports.cli",
        "--config",
        str(tmp_path / "config.json"),
        "force-hourly-report",
        "--target-ip",
        "192.0.2.115",
        "--summary-limit",
        "150",
        "--dedupe-seconds",
        "0",
    ]
    assert kwargs["shell"] is False if "shell" in kwargs else True
    assert messages[0]["message_text"] == "Force hourly metrics report started for 192.0.2.115"
    assert messages[-1]["message_text"] == "Force hourly metrics report completed for 192.0.2.115"
    metadata = json.loads(messages[-1]["metadata_json"])
    assert metadata["telegram_user_id"] == "100"
    assert metadata["telegram_username"] == "admin"
    assert metadata["target_id"] == "server/sqlserver/db"
    assert metadata["status"] == "success"


def test_report_force_hourly_unauthorized_user_cannot_execute(tmp_path, monkeypatch):
    sqlite_path, commands_path, command_message_id = prepare(tmp_path, user_type=0)
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

    messages = fetch_send_messages(sqlite_path)
    assert result["status"] == "permission_denied"
    assert called is False
    assert "Permission denied" in messages[-1]["message_text"]


def test_report_force_hourly_failure_returns_safe_failure_message(tmp_path, monkeypatch):
    sqlite_path, commands_path, command_message_id = prepare(tmp_path)

    def fake_run(argv, **kwargs):
        if "queue-telegram-message" in argv:
            # Not the call under test: queueing the reply goes through the common CLI now, and
            # it shares this module-level subprocess.run. Let it succeed so the argv this test
            # actually asserts on is the action's, not the notification's - and the row still
            # has to be written, because these tests read the reply back out of the store.
            return _REAL_RUN(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 7, stdout="", stderr="database connection failed")

    monkeypatch.setattr(command_processor.subprocess, "run", fake_run)

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
        config_path=tmp_path / "config.json",
    )

    messages = fetch_send_messages(sqlite_path)
    assert result["status"] == "action_failed"
    assert messages[-1]["message_text"] == (
        "Force hourly metrics report failed for 192.0.2.115\n"
        "Exit code: 7\n"
        "Error: database connection failed"
    )


def test_cli_execute_masks_sensitive_values_in_metadata(tmp_path, monkeypatch):
    sqlite_path, commands_path, command_message_id = prepare(tmp_path)

    def fake_run(argv, **kwargs):
        if "queue-telegram-message" in argv:
            # Not the call under test: queueing the reply goes through the common CLI now, and
            # it shares this module-level subprocess.run. Let it succeed so the argv this test
            # actually asserts on is the action's, not the notification's - and the row still
            # has to be written, because these tests read the reply back out of the store.
            return _REAL_RUN(argv, **kwargs)
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="password=plain-secret token=abc123 sqlcmd -P very-secret",
        )

    monkeypatch.setattr(command_processor.subprocess, "run", fake_run)

    process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
        config_path=tmp_path / "config.json",
    )

    metadata = json.loads(fetch_send_messages(sqlite_path)[-1]["metadata_json"])
    metadata_text = json.dumps(metadata)
    assert "plain-secret" not in metadata_text
    assert "abc123" not in metadata_text
    assert "very-secret" not in metadata_text
    assert "password=***" in metadata_text
    assert "token=***" in metadata_text


def test_report_force_hourly_action_type_is_not_hard_coded():
    source = command_processor.execute_command_action.__code__.co_consts
    assert "report_force_hourly_metrics" not in source


def _restore_command_config():
    path = shipped_config("telegram_support_commands.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return next(
        item
        for item in data["telegram_support_commands"]
        if item["command_text"] == "spbot_restore"
    )


def _ticket_detail_export_command_config():
    path = shipped_config("telegram_support_commands.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return next(
        item
        for item in data["telegram_support_commands"]
        if item["command_text"] == "spbot_json_exp_ticket_detail"
    )


class FakeSqlRunResultStore:
    def __init__(self, result_json):
        self.result_json = result_json

    def fetch_latest_sql_run_for_sql_id(self, *, sql_id, status="done"):
        if sql_id != 14 or status != "done":
            return None
        return {
            "sql_run_id": 456,
            "result_json": self.result_json,
            "row_count": 1,
        }


class CaptureReplyStore:
    def __init__(self):
        self.messages = []

    def insert_telegram_send_message(self, **kwargs):
        self.messages.append(kwargs)
        return len(self.messages)


def test_spbot_json_exp_ticket_detail_maps_to_sql_id_14():
    command = _ticket_detail_export_command_config()
    config = command["action_config"]

    assert command["action_type"] == "cli_execute"
    assert config["command_argv"][-2:] == ["14", "--force"]
    assert config["result_file"]["sql_id"] == 14
    assert config["result_file"]["output_dir"] == "runtime/output/telegram/json_exports"
    assert config["result_file"]["folder_name_template"] == "json_exp_ticket_detail_{timestamp}"


def test_cli_result_file_failure_queues_failure_not_completed(monkeypatch, tmp_path):
    command_config = _ticket_detail_export_command_config()["action_config"]
    command = command_processor.SupportCommand(
        command_id=7,
        command_text="spbot_json_exp_ticket_detail",
        command_type=1,
        reply_default=0,
        reply_text="",
        is_group=1,
        is_private=1,
        need_file=1,
        action_type="cli_execute",
        action_config=command_config,
    )
    store = CaptureReplyStore()
    row = {
        "chat_id": "100",
        "message_id": 10,
        "user_id": "100",
        "raw_json": json.dumps({"from": {"id": 100, "username": "admin"}}),
    }
    monkeypatch.setenv("DB_OPS_SECRET_KEY", "test-passphrase")
    monkeypatch.setattr(command_processor, "run_configured_cli_command", lambda **_kwargs: {"stdout": "ok"})

    def fail_create_file(**_kwargs):
        raise command_processor.TelegramCommandError("Result column not found in SQL run output: ResultJson", exit_code=1)

    monkeypatch.setattr(command_processor, "create_sql_run_result_file", fail_create_file)

    with pytest.raises(command_processor.TelegramCommandError):
        command_processor.execute_configured_cli_command(
            store=store,
            row=row,
            command=command,
            args=[],
            config_path=tmp_path / "config.json",
            source_id="1",
        )

    texts = [message["message_text"] for message in store.messages]
    assert texts[0] == "Ticket detail JSON export started."
    assert texts[-1] == (
        "Ticket detail JSON export failed.\n"
        "Exit code: 1\n"
        "Error: Result column not found in SQL run output: ResultJson"
    )
    assert not any(text == "Ticket detail JSON export completed. Preparing file..." for text in texts)


def test_sql_run_result_export_creates_safe_timestamp_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(command_processor, "TOOL_ROOT", tmp_path)
    result_json = json.dumps(
        {
            "files": [
                {
                    "result_sets": [
                        {
                            "columns": ["ResultJson"],
                            "rows": [[json.dumps({"ok": True})]],
                        }
                    ]
                }
            ]
        }
    )
    store = FakeSqlRunResultStore(result_json)

    result = command_processor.create_sql_run_result_file(
        store=store,
        config={
            "sql_id": 14,
            "status": "done",
            "result_column": "ResultJson",
            "validate_json": True,
            "output_dir": "runtime/output/telegram/json_exports",
            "folder_name_template": "json exp ticket detail {timestamp}",
            "file_name_template": "ticket detail full {timestamp}.json",
        },
        values={},
    )

    file_path = Path(result["file_path"])
    assert file_path.exists()
    assert json.loads(file_path.read_text(encoding="utf-8")) == {"ok": True}
    assert result["folder_name"].startswith("json_exp_ticket_detail_")
    assert result["folder_name"].replace("json_exp_ticket_detail_", "").replace("_", "").isdigit()
    assert result["file_name"].startswith("ticket_detail_full_")
    assert " " not in result["folder_name"]
    assert " " not in result["file_name"]
    assert file_path.parent == tmp_path / "runtime" / "output" / "telegram" / "json_exports" / result["folder_name"]


def test_sql_run_result_export_reports_folder_creation_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(command_processor, "TOOL_ROOT", tmp_path)
    result_json = json.dumps(
        {
            "files": [
                {
                    "result_sets": [
                        {
                            "columns": ["ResultJson"],
                            "rows": [["[]"]],
                        }
                    ]
                }
            ]
        }
    )
    store = FakeSqlRunResultStore(result_json)
    blocking_file = tmp_path / "runtime"
    blocking_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(command_processor.TelegramCommandError, match="Cannot create result folder"):
        command_processor.create_sql_run_result_file(
            store=store,
            config={
                "sql_id": 14,
                "output_dir": "runtime/output/telegram/json_exports",
                "folder_name_template": "json_exp_ticket_detail_{timestamp}",
            },
            values={},
        )


def test_sql_run_result_export_keeps_raw_file_when_json_validation_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(command_processor, "TOOL_ROOT", tmp_path)
    result_json = json.dumps(
        {
            "files": [
                {
                    "result_sets": [
                        {
                            "columns": ["ResultJson"],
                            "rows": [["[{not-json}]"]],
                        }
                    ]
                }
            ]
        }
    )
    store = FakeSqlRunResultStore(result_json)

    with pytest.raises(command_processor.TelegramCommandError, match="raw file kept at"):
        command_processor.create_sql_run_result_file(
            store=store,
            config={
                "sql_id": 14,
                "output_dir": "runtime/output/telegram/json_exports",
                "folder_name_template": "json_exp_ticket_detail_{timestamp}",
                "file_name_template": "ticket_detail_full_{timestamp}.json",
                "validate_json": True,
            },
            values={},
        )

    files = list((tmp_path / "runtime" / "output" / "telegram" / "json_exports").glob("*/ticket_detail_full_*.json"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "[{not-json}]"


def test_cli_failure_summary_ignores_normal_sql_task_logging_lines():
    stdout = "\n".join(
        [
            "[db_ops.config] app=sql_tasks source=cli config=config.json",
            "2026-06-30 13:09:18|LOGGING|sql_tasks|runner|sql_tasks.runner.start|scope=sql_tasks|mode=force|sql_id=14",
        ]
    )
    stderr = "\n".join(
        [
            "[db_ops.config] app=sql_tasks source=cli config=config.json",
            "SQL task scan failed tasks: 1",
        ]
    )

    summary = command_processor._extract_error_from_output(stderr, stdout)

    assert summary == "SQL task scan failed tasks: 1"
    assert "LOGGING" not in summary
    assert "[db_ops.config]" not in summary


def test_spbot_restore_cli_execute_builds_latest_and_pitr_argv():
    command = _restore_command_config()
    config = command["action_config"]
    values = {
        "python": "python",
        "config_path": "config.json",
        "restore_id": "ACME_TO_SQLSERVER_198_51_100_31",
        "point_in_time": "LATEST",
    }

    latest = build_cli_argv(config, values)
    pitr = build_cli_argv(
        config,
        values | {"point_in_time": "2026-06-15 09:30:00 +07:00"},
    )

    assert command["action_type"] == "cli_execute"
    assert "--point-in-time" not in latest
    assert pitr[-2:] == ["--point-in-time", "2026-06-15 09:30:00 +07:00"]


def _create_db_docker_config():
    import json
    from db_ops.telegram.command_processor import DEFAULT_COMMANDS_PATH
    data = json.loads(DEFAULT_COMMANDS_PATH.read_text(encoding="utf-8-sig"))
    cmd = next(c for c in data["telegram_support_commands"] if c["command_text"] == "spbot_create_db_docker")
    return cmd["action_config"]


def test_spbot_restore_uses_generic_background_cli_checker(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_json(commands_path, "telegram_support_commands", [_restore_command_config()])
    write_json(tmp_path / "telegram_users.json", "telegram_users", [{"user_id": "100", "user_type": _restore_command_config()["command_type"], "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])
    command_message_id = insert_command_message(
        sqlite_path,
        "/spbot_restore ACME_TO_SQLSERVER_198_51_100_31 LATEST",
    )
    launches = []
    monkeypatch.setenv("DB_OPS_SECRET_KEY", "test-passphrase")

    class FakePopen:
        pid = 43210

        def __new__(cls, argv, **kwargs):
            # subprocess.run needs a full Popen (context manager, communicate, poll), so the
            # reply queue gets the real class rather than a partial stand-in. Returning a
            # non-instance also skips __init__, which is what keeps the fake's bookkeeping clean.
            if "queue-telegram-message" in argv:
                return _REAL_POPEN(argv, **kwargs)
            return super().__new__(cls)

        def __init__(self, argv, **kwargs):
            launches.append((argv, kwargs))

        def wait(self, timeout=None):
            # Simulate a long-running restore: still running past the startup grace window.
            raise command_processor.subprocess.TimeoutExpired(cmd="restore-workflow", timeout=timeout)

    monkeypatch.setattr(command_processor.subprocess, "Popen", FakePopen)

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
        config_path=tmp_path / "config.json",
    )

    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute(
            "SELECT * FROM telegram_background_tasks ORDER BY task_id DESC LIMIT 1"
        ).fetchone()
    assert result["status"] == "processed"
    assert launches[0][0][3] == "restore-workflow"
    assert "--point-in-time" not in launches[0][0]
    assert task is not None

    Path(task["stdout_path"]).write_text(
        "restore-workflow completed status=SUCCESS",
        encoding="utf-8",
    )
    monkeypatch.setattr(command_processor, "_is_pid_alive", lambda _pid: False)
    monkeypatch.setattr(command_processor.sys, "platform", "linux")

    check_result = check_cli_background_tasks(sqlite_path=sqlite_path)

    messages = fetch_send_messages(sqlite_path)
    assert check_result["completed"] == 1
    assert messages[0]["message_text"].startswith("Restore workflow started")
    assert messages[-1]["message_text"].startswith("Restore workflow completed")


def test_spbot_restore_fails_loudly_when_secret_key_missing(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_json(commands_path, "telegram_support_commands", [_restore_command_config()])
    write_json(tmp_path / "telegram_users.json", "telegram_users", [{"user_id": "100", "user_type": _restore_command_config()["command_type"], "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])
    command_message_id = insert_command_message(
        sqlite_path,
        "/spbot_restore ACME_TO_SQLSERVER_198_51_100_31 LATEST",
    )
    monkeypatch.delenv("DB_OPS_SECRET_KEY", raising=False)

    def fail_popen(*args, **kwargs):
        if args and "queue-telegram-message" in args[0]:
            return _REAL_POPEN(*args, **kwargs)
        raise AssertionError("subprocess must not start when the secret key is missing")

    monkeypatch.setattr(command_processor.subprocess, "Popen", fail_popen)

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
        config_path=tmp_path / "config.json",
    )

    # No background task is created, and the user is told exactly why.
    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute("SELECT * FROM telegram_background_tasks LIMIT 1").fetchone()
    assert task is None
    messages = fetch_send_messages(sqlite_path)
    assert any("secret key is not available" in m["message_text"] for m in messages)


def test_spbot_restore_reports_immediate_crash(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "runtime.sqlite"
    commands_path = tmp_path / "telegram_support_commands.json"
    write_json(commands_path, "telegram_support_commands", [_restore_command_config()])
    write_json(tmp_path / "telegram_users.json", "telegram_users", [{"user_id": "100", "user_type": _restore_command_config()["command_type"], "status": "active"}])
    write_json(tmp_path / "telegram_groups.json", "telegram_groups", [])
    command_message_id = insert_command_message(
        sqlite_path,
        "/spbot_restore ACME_TO_SQLSERVER_198_51_100_31 LATEST",
    )
    monkeypatch.setenv("DB_OPS_SECRET_KEY", "test-passphrase")

    class CrashPopen:
        pid = 51515

        def __new__(cls, argv, **kwargs):
            # subprocess.run needs a full Popen (context manager, communicate, poll), so the
            # reply queue gets the real class rather than a partial stand-in. Returning a
            # non-instance also skips __init__, which is what keeps the fake's bookkeeping clean.
            if "queue-telegram-message" in argv:
                return _REAL_POPEN(argv, **kwargs)
            return super().__new__(cls)

        def __init__(self, argv, **kwargs):
            pass

        def wait(self, timeout=None):
            # Emulate a process that exits non-zero within the startup grace window.
            return 1

    monkeypatch.setattr(command_processor.subprocess, "Popen", CrashPopen)

    result = process_one_command_message(
        sqlite_path=sqlite_path,
        telegram_command_message_id=command_message_id,
        commands_path=commands_path,
        config_path=tmp_path / "config.json",
    )

    # Immediate crash is surfaced as a failure reply; no lingering background task is left.
    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute("SELECT * FROM telegram_background_tasks LIMIT 1").fetchone()
    assert task is None
    messages = fetch_send_messages(sqlite_path)
    assert messages[0]["message_text"].startswith("Restore workflow started")
    assert any("Restore workflow failed" in m["message_text"] for m in messages)
