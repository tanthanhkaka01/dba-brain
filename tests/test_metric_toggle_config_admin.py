"""Tests for the shared metric-toggle engine (common.config_admin.set_metric_toggle)
and the Telegram listing/toggle command handlers built on it."""

import json
import os
import stat
from pathlib import Path

import pytest

from db_ops.common.config_admin import ConfigAdminError, set_metric_toggle


def _seed(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "db_instances.json").write_text(
        json.dumps({"db_instances": [
            {
                "server_id": "ACME-10-0-0-1",
                "db_type": "sqlserver",
                "ip": "10.0.0.1",
                "enabled": True,
                "metrics": {"enabled": True},
                "report_policy": {"disabled_metric_codes": ["OS_DISK_USAGE"]},
            },
            {
                "server_id": "ACME-10-0-0-2",
                "db_type": "postgresql",
                "ip": "10.0.0.2",
                "enabled": True,
            },
        ]}), encoding="utf-8")
    (data / "metric_definitions.json").write_text(
        json.dumps({"metrics": [
            {"metric_code": "INSTANCE_STATUS", "collector_type": "sql",
             "time_window": {"repeat_interval": 60, "timeout": 60}},
            {"metric_code": "OS_DISK_USAGE", "collector_type": "cmd",
             "time_window": {"repeat_interval": 600, "timeout": 120}},
        ]}), encoding="utf-8")
    return data


def _instance(data: Path, server_id: str) -> dict:
    payload = json.loads((data / "db_instances.json").read_text(encoding="utf-8"))
    return next(item for item in payload["db_instances"] if item["server_id"] == server_id)


def _set_override(data: Path, server_id: str, metric_code: str, override: dict) -> None:
    """Plant an override the way a hand-edit or an older toggle would have left it."""
    payload = json.loads((data / "db_instances.json").read_text(encoding="utf-8"))
    instance = next(item for item in payload["db_instances"] if item["server_id"] == server_id)
    instance.setdefault("metrics", {}).setdefault("metric_overrides", {})[metric_code] = override
    (data / "db_instances.json").write_text(json.dumps(payload), encoding="utf-8")


def test_toggle_all_flips_metrics_enabled(tmp_path):
    data = _seed(tmp_path)
    result = set_metric_toggle(server_id="ACME-10-0-0-1", scope="all", enabled=False, data_dir=data)
    assert result["ok"] and result["changed"] and result["scope"] == "all"
    assert _instance(data, "ACME-10-0-0-1")["metrics"]["enabled"] is False
    result = set_metric_toggle(server_id="ACME-10-0-0-1", scope="all", enabled=True, data_dir=data)
    assert result["changed"]
    assert _instance(data, "ACME-10-0-0-1")["metrics"]["enabled"] is True


def test_toggle_collector_class_adds_and_removes(tmp_path):
    data = _seed(tmp_path)
    result = set_metric_toggle(server_id="ACME-10-0-0-2", scope="collector:cmd", enabled=False, data_dir=data)
    assert result["changed"]
    assert _instance(data, "ACME-10-0-0-2")["metrics"]["disabled_collector_types"] == ["cmd"]
    # disabling again is a no-op with a warning, not an error or a duplicate entry
    result = set_metric_toggle(server_id="ACME-10-0-0-2", scope="collector:cmd", enabled=False, data_dir=data)
    assert not result["changed"] and result["warnings"]
    assert _instance(data, "ACME-10-0-0-2")["metrics"]["disabled_collector_types"] == ["cmd"]
    result = set_metric_toggle(server_id="ACME-10-0-0-2", scope="collector:cmd", enabled=True, data_dir=data)
    assert result["changed"]
    assert _instance(data, "ACME-10-0-0-2")["metrics"]["disabled_collector_types"] == []


def test_toggle_single_metric_sets_override_and_cleans_legacy_list(tmp_path):
    data = _seed(tmp_path)
    result = set_metric_toggle(server_id="ACME-10-0-0-1", scope="os_disk_usage", enabled=False, data_dir=data)
    assert result["changed"] and result["scope"] == "OS_DISK_USAGE"
    instance = _instance(data, "ACME-10-0-0-1")
    assert instance["metrics"]["metric_overrides"]["OS_DISK_USAGE"]["enabled"] is False

    # enabling clears the override entirely AND removes the code from
    # report_policy.disabled_metric_codes. An entry saying only "enabled: true" is the default
    # written out longhand; leaving it behind is how the deploy merge carries a record that
    # means nothing back over a master that no longer has one.
    result = set_metric_toggle(server_id="ACME-10-0-0-1", scope="OS_DISK_USAGE", enabled=True, data_dir=data)
    assert result["changed"]
    instance = _instance(data, "ACME-10-0-0-1")
    assert "OS_DISK_USAGE" not in instance["metrics"]["metric_overrides"]
    assert instance["report_policy"]["disabled_metric_codes"] == []


def test_enabling_a_metric_drops_the_reason_it_was_disabled(tmp_path):
    """A `disabled_reason` is a statement about why the metric is off. Kept after it is switched
    back on it becomes false and stays false: on the worker `metric_overrides` wins the deploy
    merge, so nothing on the master can ever reach it again. It was written with
    `enabled: false`, and it goes with it — while any real config in the same override stays."""
    data = _seed(tmp_path)
    _set_override(data, "ACME-10-0-0-1", "OS_DISK_USAGE", {
        "enabled": False,
        "disabled_reason": "the collector login has no rights on this host",
        "severity_map": {"WARNING": "CRITICAL"},
    })

    result = set_metric_toggle(server_id="ACME-10-0-0-1", scope="OS_DISK_USAGE", enabled=True, data_dir=data)

    assert result["changed"]
    override = _instance(data, "ACME-10-0-0-1")["metrics"]["metric_overrides"]["OS_DISK_USAGE"]
    assert "disabled_reason" not in override
    assert override["severity_map"] == {"WARNING": "CRITICAL"}


def test_clearing_a_stale_reason_is_not_reported_as_nothing_to_change(tmp_path):
    """The metric is already on, so the old code called it a no-op — while writing to the file.
    An operator told nothing happened goes looking for the write that did."""
    data = _seed(tmp_path)
    _set_override(data, "ACME-10-0-0-1", "OS_DISK_USAGE", {
        "enabled": True,
        "disabled_reason": "left over from when this metric had no SQL for this version",
    })

    result = set_metric_toggle(server_id="ACME-10-0-0-1", scope="OS_DISK_USAGE", enabled=True, data_dir=data)

    assert result["changed"] and not result["warnings"]
    assert "OS_DISK_USAGE" not in _instance(data, "ACME-10-0-0-1")["metrics"]["metric_overrides"]


def test_toggle_rejects_unknown_inputs(tmp_path):
    data = _seed(tmp_path)
    with pytest.raises(ConfigAdminError, match="Unknown server_id"):
        set_metric_toggle(server_id="NOPE", scope="all", enabled=True, data_dir=data)
    with pytest.raises(ConfigAdminError, match="Unknown collector type"):
        set_metric_toggle(server_id="ACME-10-0-0-1", scope="collector:command", enabled=False, data_dir=data)
    with pytest.raises(ConfigAdminError, match="Unknown metric_code"):
        set_metric_toggle(server_id="ACME-10-0-0-1", scope="NOT_A_METRIC", enabled=False, data_dir=data)


def test_format_time_window_line_compact():
    from db_ops.telegram.command_processor import _format_time_window_line

    assert _format_time_window_line(None) == "always"
    assert _format_time_window_line({"from_hour": 1, "to_hour": 5, "repeat_interval": 72000, "timeout": 7200}) == \
        "hour 1..5 every 72000s timeout 7200s"
    assert _format_time_window_line({"repeat_interval": 0}) == "run-once"
    assert _format_time_window_line({"from_day": 7, "to_day": 7, "from_minute": 0, "to_minute": 59}) == \
        "day 7..7 minute 0..59"


def test_listing_commands_render_from_repo_config():
    """Smoke: both listing handlers run against the real repo config (read-only).
    A fake store captures the document queue when the listing overflows one message."""
    from db_ops.telegram import command_processor

    queued = []

    class _FakeStore:
        def insert_telegram_send_message(self, **kwargs):
            queued.append(kwargs)
            return 1

    row = {"chat_id": "-100", "message_id": 5}
    for action, handler in (
        ("list_sql_tasks", command_processor.execute_list_sql_tasks_command),
        ("list_metrics", command_processor.execute_list_metrics_command),
    ):
        command = command_processor.SupportCommand(
            command_id=99, command_text=f"spbot_{action}", command_type=1,
            reply_default=1, reply_text="{result_listing}", is_group=1, is_private=1,
            need_file=1, action_type=action, action_config={"parameters": []},
        )
        result = handler(store=_FakeStore(), row=row, command=command, source_id="test")
        assert result["listing"].strip()
        assert len(result["listing"]) <= command_processor.TELEGRAM_LISTING_TEXT_LIMIT + 200
    for kwargs in queued:
        document_path = Path(kwargs["metadata"]["document_path"])
        assert document_path.exists()
        json.loads(document_path.read_text(encoding="utf-8"))
        document_path.unlink()


def test_execute_metric_toggle_command_maps_args(monkeypatch):
    """/spbot_metric_toggle turns three chat words into the `metric-toggle` request.

    Stubbed at the CLI boundary since 2026-08-15: the write runs in
    `db_ops.common.cli metric-toggle`, so patching the in-process function would patch something
    this path no longer calls. What is asserted is the request — the bot's only job here is to
    say which server, which scope and which direction.
    """
    from db_ops.lib import common_cli
    from db_ops.telegram import command_processor

    calls = {}

    def fake_run(command, request):
        assert command == "metric-toggle"
        calls.update(request)
        return {"ok": True, "server_id": request["server_id"], "scope": request["scope"],
                "enabled": request["state"] == "on", "changed": True,
                "changes": ["metrics.enabled: True -> False"], "warnings": []}

    monkeypatch.setattr(common_cli, "run", fake_run)
    command = command_processor.SupportCommand(
        command_id=17, command_text="spbot_metric_toggle", command_type=2,
        reply_default=1, reply_text="", is_group=0, is_private=1, need_file=0,
        action_type="metric_toggle", action_config={"parameters": []},
    )
    result = command_processor.execute_metric_toggle_command(
        command=command, args=["ACME-10-0-0-1", "off", "all"]
    )
    assert calls == {"server_id": "ACME-10-0-0-1", "scope": "all", "state": "off"}
    assert result["status"] == "changed" and result["state"] == "off"
    assert "metrics.enabled" in result["detail"]

    with pytest.raises(command_processor.TelegramCommandError, match="on' or 'off"):
        command_processor.execute_metric_toggle_command(command=command, args=["ACME-10-0-0-1", "maybe", "all"])


def test_a_toggle_keeps_the_files_mode_and_owner(tmp_path):
    """`data/` is a bind mount shared between the worker container and its host. The container
    runs as root, so an atomic write that lets the replacement inherit `mkstemp`'s metadata hands
    the file to root at 0600 — and the master, which reads the worker over SFTP as `tuser`, stops
    being able to open it. The merge then reports "not on worker" and the deploy overwrites the
    toggle. The write is not what has to survive; the file's identity is.
    """
    data = _seed(tmp_path)
    path = data / "db_instances.json"
    os.chmod(path, 0o664)
    before = path.stat()

    set_metric_toggle(server_id="ACME-10-0-0-1", scope="OS_DISK_USAGE", enabled=False, data_dir=data)

    after = path.stat()
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
