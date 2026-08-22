"""Registering a SQL task from a Telegram conversation, without the bot owning any of the rules.

Two boundaries are stubbed here and they are stubbed at different places on purpose, because
they are different kinds of thing:

* the **write** goes out through ``db_ops.common.cli add-sql`` (``lib.common_cli.run``) — an app
  does not import ``common``, it hands it a JSON object. Since 2026-08-15 the assertions are
  therefore about *the request*, not about keyword arguments;
* the **read** that fills in db_type, instance, service and credential is
  ``data_sources.resolve_sql_target_fields`` — the one reader of the data folder, which an app may
  call in-process. The operator is never asked for those four, so they have to be looked up
  before the request exists.
"""

import pytest

from db_ops.telegram import command_processor as cp
from db_ops.common import data_sources
from db_ops.lib import common_cli


def _cmd():
    cmds = cp.load_support_commands()
    return [c for c in cmds if c.command_text == "spbot_add_sql"][0]


def test_add_sql_command_is_registered_multistep_admin():
    cmd = _cmd()
    # Admin-tier command (>= 2); the exact required level may be raised in the data file.
    assert cmd.command_type >= 2 and cmd.action_type == "add_sql_task"
    assert cmd.is_private == 1 and cmd.node_role == "worker"
    params = cmd.action_config["parameters"]
    # db_type / instance_name / target database are deliberately NOT asked for: db_instances.json
    # already records them against the server, and asking gave the operator three more chances to
    # enter something that does not resolve (SQLSERVER-017 got a null instance that way and could
    # never find its database). server_id is the one identifier the operator actually knows.
    assert [p["name"] for p in params] == [
        "server_id", "sql_name", "schedule", "output", "sql_text"]
    assert all(p.get("prompt_text") for p in params)  # every step prompts


def test_parse_time_window():
    assert cp.parse_add_sql_time_window("default") is None
    assert cp.parse_add_sql_time_window("") is None
    assert cp.parse_add_sql_time_window("20 23 3600 600") == {
        "from_hour": 20, "to_hour": 23, "repeat_interval": 3600, "timeout": 600}
    assert cp.parse_add_sql_time_window("8,17") == {"from_hour": 8, "to_hour": 17}
    with pytest.raises(ValueError):
        cp.parse_add_sql_time_window("8 notanint")


def _fake_resolve(monkeypatch, **overrides):
    """Stand in for the db_instances.json lookup the command does on the operator's behalf."""
    resolved = {"db_type": "sqlserver", "server_id": "ACME-192-0-2-250",
                "service_name": "APPDB-PROD", "instance_name": "APPDB",
                "credential_name": "cred_appdb", **overrides}
    monkeypatch.setattr(data_sources, "resolve_sql_target_fields",
                        lambda server_id, **_kw: {**resolved, "server_id": server_id})
    return resolved


def _capture_add_sql(monkeypatch, data: dict) -> dict:
    """Answer ``common.cli add-sql`` with ``data`` and hand back the request it was sent."""
    captured: dict = {}

    def fake_run(command, request):
        assert command == "add-sql"
        captured.update(request)
        return {**data, "server_id": request["server_id"]}

    monkeypatch.setattr(common_cli, "run", fake_run)
    return captured


def test_execute_add_sql_fills_in_what_it_no_longer_asks_for(monkeypatch):
    """The operator gives a server_id; db_type, instance, service and credential come from
    db_instances.json. Those four still have to reach the command, or the target is written
    with the nulls that made SQLSERVER-017 unrunnable."""
    _fake_resolve(monkeypatch)
    captured = _capture_add_sql(monkeypatch, {
        "sql_id": 42, "sql_code": "SQLSERVER-042-X", "target_no": 1,
        "script_path": "sql/tasks/sqlserver/srv/042_x.sql",
        "active": True, "manual_only": False, "output": "xlsx", "ok": True})
    cmd = _cmd()
    args = ["ACME-192-0-2-250", "Nightly cleanup", "20 23 3600 600", "xlsx",
            "DELETE FROM staging;"]
    result = cp.execute_add_sql_task_command(command=cmd, args=args)

    assert result["status"] == "OK" and result["sql_id"] == 42
    assert captured["db_type"] == "sqlserver"          # derived, never typed
    assert captured["instance_name"] == "APPDB"         # derived, never typed
    assert captured["service_name"] == "APPDB-PROD"
    assert captured["credential_name"] == "cred_appdb"
    assert captured["server_id"] == "ACME-192-0-2-250"
    assert captured["sql_name"] == "Nightly cleanup"
    assert captured["sql_text"] == "DELETE FROM staging;"
    assert captured["output"] == "xlsx"
    # The window travels as the command's own four fields, not as a nested object: `add-sql` has
    # one parser and these are the flags it declares.
    assert captured["from_hour"] == 20 and captured["to_hour"] == 23
    assert captured["repeat_interval"] == 3600 and captured["timeout"] == 600
    # `active` is not sent — the command already defaults to registering an enabled task, and a
    # request that restates a default is a second place for it to disagree.
    assert "active" not in captured


def test_manual_schedule_becomes_repeat_interval_minus_one(monkeypatch):
    """'manual' is expressed in the target's own time_window as repeat_interval = -1, not as a
    second key beside it. One place says when a task runs, so the config cannot contradict
    itself. It must also not be parsed as hours ('manual' is not '0 0')."""
    _fake_resolve(monkeypatch)
    captured = _capture_add_sql(monkeypatch, {
        "sql_id": 43, "sql_code": "C", "target_no": 1, "script_path": "p",
        "active": True, "manual_only": True, "output": "none", "ok": True})
    result = cp.execute_add_sql_task_command(
        command=_cmd(),
        args=["ACME-192-0-2-250", "Stock export", "manual", "none", "SELECT 1;"])

    assert captured["repeat_interval"] == -1
    assert "manual_only" not in captured      # no second source of truth
    assert result["schedule"] == "manual"


def test_parse_time_window_manual_and_the_listing_that_shows_it():
    assert cp.parse_add_sql_time_window("manual") == {"repeat_interval": -1}
    assert cp.parse_add_sql_time_window("MANUAL") == {"repeat_interval": -1}
    # The listing must not print day/hour bounds a manual entry never consults — that would
    # read as "runs all day, every day" for a task nothing ever starts.
    line = cp._format_time_window_line(
        {"from_day": 1, "to_day": 31, "from_hour": 0, "to_hour": 23,
         "repeat_interval": -1, "timeout": 1800})
    assert line == "manual (run with /spbot_run_sql_task) timeout 1800s"
    assert "day 1..31" not in line


def test_execute_add_sql_reports_failure_without_raising(monkeypatch):
    """A rejected task is an answer, not a crash: the operator has to read *why* in the reply.

    The command reports failure in the response envelope, which `common_cli.run` turns into a
    `CommonCliError` — the reason has to survive that translation and reach the template.
    """
    def boom(command, request):
        raise common_cli.CommonCliError("add-sql failed: bad db_type")

    _fake_resolve(monkeypatch)
    monkeypatch.setattr(common_cli, "run", boom)
    cmd = _cmd()
    result = cp.execute_add_sql_task_command(
        command=cmd, args=["s", "n", "default", "none", "SELECT 1;"])
    assert result["status"] == "FAILED" and "bad db_type" in result["error"]
    # reply template renders cleanly (no leftover placeholders) on failure
    txt = cp.render_reply_text(cmd.reply_text, row={"telegram_command_message_id": "1"},
                               command=cmd, args=["x"], action_result=result, action_error=None)
    assert "{result_" not in txt and "FAILED" in txt


def test_message_document_extraction():
    assert cp._message_document({"raw_json": '{"document":{"file_id":"F1","file_name":"a.sql"}}'})["file_id"] == "F1"
    assert cp._message_document({"raw_json": '{"text":"no file"}'}) is None
    assert cp._message_document({"raw_json": ""}) is None
    assert cp._message_document({}) is None  # missing raw_json key


def test_sql_text_parameter_accepts_file():
    cmd = _cmd()
    sql_text = cp._parameter_at_position(cmd, 5)
    assert sql_text["name"] == "sql_text" and sql_text.get("accept_file") is True


def test_download_document_text(monkeypatch):
    from db_ops.telegram import api

    monkeypatch.setattr(api, "get_file_bytes", lambda **kw: "﻿SELECT 1;\n".encode("utf-8"))

    class _Cfg:
        class telegram:
            resolved_bot_token = "tok"
            api_url = "https://api.telegram.org"
    monkeypatch.setattr("db_ops.config.load_config", lambda *_a, **_k: _Cfg())
    text = cp._download_document_text({"file_id": "F1", "file_name": "x.sql"}, config_path="config.json")
    assert text == "SELECT 1;"  # BOM stripped, trailing newline trimmed


def test_download_document_text_rejects_empty(monkeypatch):
    from db_ops.telegram import api
    monkeypatch.setattr(api, "get_file_bytes", lambda **kw: b"   ")
    class _Cfg:
        class telegram:
            resolved_bot_token = "tok"
            api_url = "https://api.telegram.org"
    monkeypatch.setattr("db_ops.config.load_config", lambda *_a, **_k: _Cfg())
    with pytest.raises(RuntimeError):
        cp._download_document_text({"file_id": "F1"}, config_path="config.json")
