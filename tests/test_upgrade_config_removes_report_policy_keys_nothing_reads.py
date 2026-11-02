"""`upgrade-config` removes the `report_policy` keys of an instance that nothing has ever read.

Found on 2026-10-23 looking for what silences an alert on one server: 25 records carried
`enabled`, `severity_overrides`, `disabled_event_codes` and `disabled_report_events`, every one at
its empty default, and none of them did anything - `severity_overrides` had a reader in the policy
engine that nothing called, the other three had none. A file that shows a switch which does nothing
lets its reader believe an override is in force. `disabled_metric_codes` stays: since 0.31.0 the
alert reports read it.

Only an empty default is removed. A key holding a value is a conflict - what it was meant to do never
happened, and whether it should is the operator's - and its file is left as it was.
"""
from __future__ import annotations

import json
from pathlib import Path

from dbabrain.common import config_upgrade

DEFAULTS = {"enabled": True, "severity_overrides": {}, "disabled_metric_codes": ["OS_UPTIME"],
            "disabled_event_codes": [], "disabled_report_events": []}


def _data(tmp_path: Path, records: list[dict]) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "db_instances.json").write_text(json.dumps({"db_instances": records}, indent=2) + "\n",
                                            encoding="utf-8")
    return data


def _records(data: Path) -> list[dict]:
    return json.loads((data / "db_instances.json").read_text(encoding="utf-8"))["db_instances"]


def _upgrade(data: Path, **request):
    return config_upgrade.upgrade({"data_dir": str(data), "steps": ["unread-report-keys"], **request})[1]


def test_the_unread_keys_go_and_the_one_the_reports_read_stays(tmp_path):
    data = _data(tmp_path, [{"server_id": "ACME-1", "report_policy": dict(DEFAULTS)},
                            {"server_id": "ACME-2"}])

    plan = _upgrade(data)
    assert plan["records_changed"] == 1 and plan["conflicts"] == 0
    assert _records(data)[0]["report_policy"] == DEFAULTS, "a plan writes nothing"

    _upgrade(data, dry_run=False)
    assert _records(data)[0]["report_policy"] == {"disabled_metric_codes": ["OS_UPTIME"]}
    assert _records(data)[1] == {"server_id": "ACME-2"}
    assert _upgrade(data)["records_changed"] == 0, "a second run has nothing to do"


def test_a_key_holding_a_value_is_named_and_its_file_left_alone(tmp_path):
    held = dict(DEFAULTS, severity_overrides={"BACKUP_AGE": "SUPPRESS"})
    data = _data(tmp_path, [{"server_id": "ACME-1", "report_policy": dict(DEFAULTS)},
                            {"server_id": "ACME-2", "report_policy": held}])

    result = _upgrade(data, dry_run=False)

    assert result["conflicts"] == 1
    conflict = result["steps"][0]["files"][0]["conflicts"][0]
    assert "ACME-2" in conflict["where"]
    assert conflict["values"] == {"severity_overrides": {"BACKUP_AGE": "SUPPRESS"}}
    assert _records(data)[0]["report_policy"] == DEFAULTS, "a conflict leaves the whole file as it was"


def test_the_step_runs_in_a_plain_upgrade():
    assert "unread-report-keys" in config_upgrade.STEPS


HAND_FORMATTED = [
    '{',
    '  "db_instances": [',
    '',
    '    {',
    '      "server_id": "ACME-1",',
    '      "database_names": ["A", "B"],',
    '      "report_policy": {',
    '        "enabled": true,',
    '        "severity_overrides": {},',
    '        "disabled_metric_codes": [],',
    '        "disabled_event_codes": [],',
    '        "disabled_report_events": []',
    '      }',
    '    }',
    '  ]',
    '}',
    '',
]


def test_a_hand_formatted_file_keeps_its_layout(tmp_path):
    """The shipped example is written by hand - inline lists, a blank line between blocks. Dumping
    the parsed document again reflowed every one of them (the first run, 2026-10-23)."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "db_instances.json").write_text("\n".join(HAND_FORMATTED), encoding="utf-8")

    _upgrade(data, dry_run=False)

    lines = (data / "db_instances.json").read_text(encoding="utf-8").split("\n")
    assert lines[:7] == HAND_FORMATTED[:7], "everything outside the block is as written"
    assert lines[7:9] == ['        "disabled_metric_codes": []', '      }']
