"""Two edits of db_instances.json that must leave the rest of the file as it was.

On 2026-10-19, 49 standing alerts were lowered one level by 49 calls of `metric-severity`, fed by
a script, and each call rewrote the whole 2-space file at a 1-space indent: the review saw an
8,290-line diff for 260 lines of change. A list of remaps is now one call that writes once - all
of them or none, so a list refused in the middle leaves no half-changed file - and every writer
keeps the file's own indent.

The same day, three lab instances had to be switched on, and there was no command for an
instance's `active` flag: `instance-add` with `replace` rewrites the record and its credential.
`instance-active` writes that one flag. And the `.251` PostgreSQL lab read *never backed up* while
dbabrain backed it up every hour: its record named no `PG_BACKUP_DIR`, and no command could write
one. `metric-env` sets a collector's inputs on one instance.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dbabrain.common.config_admin import (
    ConfigAdminError,
    set_instance_active,
    set_metric_collector_env,
    set_metric_severity_map,
    set_metric_severity_maps,
)

REPO = Path(__file__).resolve().parents[1]


def _seed(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    instances = {"db_instances": [
        {"server_id": "ACME-192-0-2-10", "db_type": "sqlserver", "ip": "192.0.2.10", "active": True,
         "metrics": {"enabled": True}},
        {"server_id": "ACME-192-0-2-11", "db_type": "sqlserver", "ip": "192.0.2.11", "active": False,
         "metrics": {"enabled": False}},
    ]}
    (data / "db_instances.json").write_text(json.dumps(instances, indent=2) + "\n", encoding="utf-8")
    (data / "metric_definitions.json").write_text(json.dumps({"metrics": [
        {"metric_code": "BACKUP_AGE"}, {"metric_code": "LOG_FILE_SPACE"}]}), encoding="utf-8")
    return data


def _instances(data: Path) -> dict[str, dict]:
    return {r["server_id"]: r for r in json.loads((data / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]}


def test_a_remap_keeps_the_file_in_its_own_indent(tmp_path):
    data = _seed(tmp_path)
    set_metric_severity_map(server_id="ACME-192-0-2-10", metric_code="BACKUP_AGE",
                            severity_map={"CRITICAL": "WARNING"}, data_dir=data)
    text = (data / "db_instances.json").read_text(encoding="utf-8")
    assert text.startswith('{\n  "db_instances": [\n    {\n')


def test_a_list_of_remaps_is_written_once_and_answers_one_result_per_remap(tmp_path):
    data = _seed(tmp_path)
    answer = set_metric_severity_maps([
        {"server_id": "ACME-192-0-2-10", "metric_code": "BACKUP_AGE", "metric_item": "app_db",
         "severity_map": {"CRITICAL": "WARNING"}, "note": "a restored copy"},
        {"server_id": "ACME-192-0-2-11", "metric_code": "LOG_FILE_SPACE", "severity_map": {"WARNING": "LOGGING"}},
    ], data_dir=data)
    assert answer["changed"] is True
    assert [item["server_id"] for item in answer["items"]] == ["ACME-192-0-2-10", "ACME-192-0-2-11"]
    rows = _instances(data)
    item = rows["ACME-192-0-2-10"]["metrics"]["metric_overrides"]["BACKUP_AGE"]["metric_item_overrides"]["app_db"]
    assert item == {"severity_map": {"CRITICAL": "WARNING"}, "_note": "a restored copy"}
    assert rows["ACME-192-0-2-11"]["metrics"]["metric_overrides"]["LOG_FILE_SPACE"]["severity_map"] == {"WARNING": "LOGGING"}


def test_a_list_with_one_refused_remap_writes_nothing_and_names_its_place(tmp_path):
    data = _seed(tmp_path)
    before = (data / "db_instances.json").read_bytes()
    with pytest.raises(ConfigAdminError, match=r"items\[1\]: Unknown metric_code: NOT_A_METRIC"):
        set_metric_severity_maps([
            {"server_id": "ACME-192-0-2-10", "metric_code": "BACKUP_AGE", "severity_map": {"CRITICAL": "WARNING"}},
            {"server_id": "ACME-192-0-2-10", "metric_code": "NOT_A_METRIC", "severity_map": {"CRITICAL": "WARNING"}},
        ], data_dir=data)
    assert (data / "db_instances.json").read_bytes() == before


def test_a_remap_in_a_list_refuses_a_field_it_does_not_take(tmp_path):
    data = _seed(tmp_path)
    with pytest.raises(ConfigAdminError, match="unknown field"):
        set_metric_severity_maps([{"server_id": "ACME-192-0-2-10", "metric_code": "BACKUP_AGE",
                                   "severity_map": {"CRITICAL": "WARNING"}, "data_dir": str(data)}], data_dir=data)


def test_the_command_takes_a_list_and_refuses_a_single_remap_beside_it(tmp_path):
    data = _seed(tmp_path)

    def run(request: dict) -> dict:
        out = subprocess.run([sys.executable, "-m", "dbabrain.common.cli", "metric-severity", json.dumps(request)],
                             capture_output=True, text=True, encoding="utf-8", cwd=REPO)
        return json.loads(out.stdout)

    answer = run({"items": [{"server_id": "ACME-192-0-2-10", "metric_code": "BACKUP_AGE",
                             "severity_map": {"CRITICAL": "WARNING"}}], "data_dir": str(data)})
    assert answer["success"] is True and len(answer["data"]["items"]) == 1
    refused = run({"items": [], "server_id": "ACME-192-0-2-10", "data_dir": str(data)})
    assert refused["success"] is False and "only items and data_dir" in refused["message"]


def test_an_instance_is_switched_on_by_its_active_flag_alone(tmp_path):
    data = _seed(tmp_path)
    before = _instances(data)["ACME-192-0-2-11"]
    answer = set_instance_active(server_id="ACME-192-0-2-11", active=True, data_dir=data)
    after = _instances(data)["ACME-192-0-2-11"]
    assert answer["changed"] is True and after["active"] is True
    assert {k: v for k, v in after.items() if k != "active"} == {k: v for k, v in before.items() if k != "active"}
    # metrics.enabled is metric-toggle's: said, not changed.
    assert any("metric-toggle" in warning for warning in answer["warnings"])
    assert set_instance_active(server_id="ACME-192-0-2-11", active=True, data_dir=data)["changed"] is False


def test_a_record_saying_it_under_the_older_name_is_not_rewritten_to_say_the_same(tmp_path):
    data = _seed(tmp_path)
    rows = json.loads((data / "db_instances.json").read_text(encoding="utf-8"))
    rows["db_instances"][0].pop("active")
    rows["db_instances"][0]["enabled"] = True
    (data / "db_instances.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    before = (data / "db_instances.json").read_bytes()
    answer = set_instance_active(server_id="ACME-192-0-2-10", active=True, data_dir=data)
    assert answer["changed"] is False and answer["changes"] == []
    assert (data / "db_instances.json").read_bytes() == before


@pytest.mark.parametrize("server_id, active, refusal", [
    ("ACME-192-0-2-99", True, "Unknown server_id"),
    ("ACME-192-0-2-10", "yes", "active is required"),
    ("", True, "server_id is required"),
])
def test_an_instance_switch_is_refused_for_an_unknown_instance_or_a_value_that_is_not_true_or_false(
        tmp_path, server_id, active, refusal):
    data = _seed(tmp_path)
    with pytest.raises(ConfigAdminError, match=refusal):
        set_instance_active(server_id=server_id, active=active, data_dir=data)


def test_a_collector_input_is_set_on_the_instance_and_removed_by_null(tmp_path):
    data = _seed(tmp_path)
    answer = set_metric_collector_env(server_id="ACME-192-0-2-10", env={"PG_BACKUP_DIR": "/opt/backup/pg"}, data_dir=data)
    assert answer["changed"] is True
    assert _instances(data)["ACME-192-0-2-10"]["metrics"]["collector_env"] == {"PG_BACKUP_DIR": "/opt/backup/pg"}
    assert set_metric_collector_env(server_id="ACME-192-0-2-10", env={"PG_BACKUP_DIR": "/opt/backup/pg"},
                                    data_dir=data)["changed"] is False
    set_metric_collector_env(server_id="ACME-192-0-2-10", env={"PG_BACKUP_DIR": None}, data_dir=data)
    assert "collector_env" not in _instances(data)["ACME-192-0-2-10"]["metrics"]


def test_a_collector_input_for_one_metric_goes_to_that_metrics_own_block(tmp_path):
    data = _seed(tmp_path)
    set_metric_collector_env(server_id="ACME-192-0-2-10", env={"BACKUP_ROOT": "/b"}, metric_code="backup_age", data_dir=data)
    metrics = _instances(data)["ACME-192-0-2-10"]["metrics"]
    assert metrics["metric_overrides"]["BACKUP_AGE"]["collector_env"] == {"BACKUP_ROOT": "/b"}
    assert "collector_env" not in metrics
    set_metric_collector_env(server_id="ACME-192-0-2-10", env={"BACKUP_ROOT": None}, metric_code="BACKUP_AGE", data_dir=data)
    assert "metric_overrides" not in _instances(data)["ACME-192-0-2-10"]["metrics"]


@pytest.mark.parametrize("env, refusal", [
    ({"BACKUP_PASSWORD": "x"}, "names a secret"),
    ({"pg backup dir": "/x"}, "not a variable name"),
    ({"PG_BACKUP_DIR": ""}, "non-empty text"),
    ({}, "env is required"),
])
def test_a_collector_input_that_is_a_secret_or_not_a_variable_is_refused(tmp_path, env, refusal):
    data = _seed(tmp_path)
    before = (data / "db_instances.json").read_bytes()
    with pytest.raises(ConfigAdminError, match=refusal):
        set_metric_collector_env(server_id="ACME-192-0-2-10", env=env, data_dir=data)
    assert (data / "db_instances.json").read_bytes() == before
