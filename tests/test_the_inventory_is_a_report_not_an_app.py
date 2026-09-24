"""The inventory is a report of the reports app, not an app of its own (0.22.0, the operator).

Until 0.22.0 it was the app command APP-REPORTS-INVENTORY-WORKFLOW: a second schedule beside the
reports app, for something that is one of its reports. It is now the entry `rp_inventory_health` in
reports_config.json, built by `run-scheduled` like the metrics and backup reports.

Two things are different about it, and both are held here. It builds FILES and sends nothing, so it
must record its run as a run - recorded as a skipped send, like a report with nothing to queue, it
would have no anchor and be rebuilt on every two-minute pass. And an estate carrying the old app
command must be moved by `upgrade-config`, keeping its own schedule - left behind, the node would
build the inventory twice, once per scheduler.
"""

from __future__ import annotations

import json
from pathlib import Path

from db_ops.common import config_upgrade
from db_ops.reports import metrics_reports


class _State:
    def __init__(self):
        self.rows = []

    def upsert_report_send_state(self, **row):
        self.rows.append(row)


def _run(monkeypatch, *, raises=None):
    seen = {}

    def fake_workflow(**kwargs):
        seen.update(kwargs)
        if raises:
            raise raises
        return {"status": "SUCCESS", "stamp": "20260923_000000"}

    import db_ops.reports.inventory_summary as inventory_summary
    monkeypatch.setattr(inventory_summary, "build_inventory_workflow", fake_workflow)
    state = _State()
    result = metrics_reports._run_inventory_report(
        store=state, sqlite_path="x.sqlite",
        report_config={"report_code": "rp_inventory_health", "metric_max_age_seconds": 7 * 86400},
        channel="telegram", started_at="2026-09-23T00:00:00Z", config=None, logger=None)
    return result, state.rows, seen


def test_a_built_inventory_records_its_run_so_the_interval_can_count_from_it(monkeypatch):
    result, rows, seen = _run(monkeypatch)
    assert result["created"] == 1 and result["queued"] == 0
    assert rows == [{"report_code": "rp_inventory_health", "channel": "telegram",
                     "last_run_at": "2026-09-23T00:00:00Z", "last_status": "built",
                     "last_skipped_reason": ""}]
    assert seen["days"] == 7 and seen["beauty"] == 1


def test_a_failed_inventory_is_recorded_and_does_not_raise_through_the_pass(monkeypatch):
    """The other reports of the same pass still run."""
    result, rows, _ = _run(monkeypatch, raises=RuntimeError("store unreachable"))
    assert result["created"] == 0 and "store unreachable" in result["error"]
    assert rows[0]["last_status"] == "failed" and rows[0]["last_run_at"]


def test_the_shipped_reports_include_the_inventory_and_no_app_command_builds_it():
    root = Path(__file__).resolve().parents[1]
    for folder in ("db_ops/reports/catalogue", "data"):
        for name in ("reports_config.json", "reports_config.example.json"):
            path = root / folder / name
            if path.is_file():
                codes = {e["report_code"] for e in json.loads(path.read_text(encoding="utf-8"))["reports"]}
                assert metrics_reports.INVENTORY_CODE in codes, path
    for path in (root / "db_ops/jobs/catalogue/app_commands.json", root / "data/app_commands.example.json"):
        if path.is_file():
            assert config_upgrade.INVENTORY_APP not in path.read_text(encoding="utf-8"), path


def _estate(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "app_commands.json").write_text(json.dumps({"app_commands": [
        {"app_code": "APP-REPORTS-CREATE", "time_window": {"repeat_interval": 120, "timeout": 600}},
        {"app_code": "APP-REPORTS-INVENTORY-WORKFLOW", "active": False,
         "command_text": "python -m db_ops.reports.cli inventory-workflow --days 3 --beauty 1",
         "time_window": {"from_hour": 1, "to_hour": 5, "repeat_interval": 3600, "timeout": 1200}},
    ]}, indent=2) + "\n", encoding="utf-8")
    (data / "reports_config.json").write_text(json.dumps({"reports": [
        {"report_code": "rp_backup_health_daily", "metric_max_age_seconds": 86400}]}, indent=2) + "\n",
        encoding="utf-8")
    (data / "webhost_config.json").write_text(json.dumps({"apps": [
        {"app_code": "reports", "app_command_ids": ["APP-REPORTS-CREATE", "APP-REPORTS-INVENTORY-WORKFLOW"]}]},
        indent=2) + "\n", encoding="utf-8")
    return data


def test_upgrade_config_moves_the_app_command_into_a_report_keeping_the_nodes_own_schedule(tmp_path):
    data = _estate(tmp_path)
    config_upgrade.upgrade({"data_dir": str(data), "dry_run": False, "steps": ["inventory-into-reports"]})

    reports = json.loads((data / "reports_config.json").read_text(encoding="utf-8"))["reports"]
    moved = next(e for e in reports if e["report_code"] == "rp_inventory_health")
    assert moved["metric_max_age_seconds"] == 3 * 86400 and moved["active"] is False
    assert moved["time_window"] == {"from_hour": 1, "to_hour": 5, "repeat_interval": 3600, "timeout": 1200}
    commands = json.loads((data / "app_commands.json").read_text(encoding="utf-8"))["app_commands"]
    assert [c["app_code"] for c in commands] == ["APP-REPORTS-CREATE"]
    assert commands[0]["time_window"]["timeout"] == 600 + 1200, "the pass now builds the inventory too"
    web = json.loads((data / "webhost_config.json").read_text(encoding="utf-8"))
    assert web["apps"][0]["app_command_ids"] == ["APP-REPORTS-CREATE"]


def test_moving_the_inventory_twice_changes_nothing_the_second_time(tmp_path):
    data = _estate(tmp_path)
    config_upgrade.upgrade({"data_dir": str(data), "dry_run": False, "steps": ["inventory-into-reports"]})
    _, again = config_upgrade.upgrade({"data_dir": str(data), "steps": ["inventory-into-reports"]})
    assert again["records_changed"] == 0
