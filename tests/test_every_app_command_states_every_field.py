"""Every app command states all fourteen of its fields - nothing is left to a default in the code.

Until 0.30.0 six of the eight shipped records left `run_mode` and `max_parallel` out and three left
their `note` out, so a reader of the file could not tell that APP-METRICS ran `sync` - the reason the
nightly index-fragmentation scan held every server's INSTANCE_STATUS for ~18 minutes - without
reading the daemon. The operator, 2026-10-16: *in app_commands every field is required; fill in
whatever an app is missing, and the reference marks every field required.*

A node upgraded from an older version has records with fields left out. `upgrade-config` writes in
each the value the daemon had been giving it (`earlier_defaults.state_app_commands`), so stating the
field changes nothing the node does; a field the daemon never filled is left for the configuration
check to name.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from dbabrain.common import config_upgrade
from dbabrain.lib import earlier_defaults

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_FIELDS = (
    "node_role",
    "sort_order",
    "app_command_id",
    "app_code",
    "app_name",
    "display_name",
    "log_scope",
    "working_dir",
    "command_text",
    "active",
    "run_mode",
    "max_parallel",
    "time_window",
    "note",
)

APP_COMMAND_FILES = (
    REPO_ROOT / "data" / "app_commands.json",
    REPO_ROOT / "data" / "app_commands.example.json",
    REPO_ROOT / "dbabrain" / "jobs" / "catalogue" / "app_commands.json",
    REPO_ROOT / "examples" / "postgres-quickstart" / "data" / "app_commands.json",
    REPO_ROOT / "examples" / "sqlserver-quickstart" / "data" / "app_commands.json",
)

REFERENCE_COPIES = (
    REPO_ROOT / "data" / "shared_config_objects.json",
    REPO_ROOT / "data" / "shared_config_objects.example.json",
    REPO_ROOT / "dbabrain" / "common" / "catalogue" / "shared_config_objects.json",
)


@pytest.mark.parametrize("path", APP_COMMAND_FILES, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_every_shipped_app_command_states_all_fourteen_fields_in_exact_order(path: Path) -> None:
    """Each record in every shipped app_commands.json states all 14 fields in the APP-SQL_TASKS order."""
    assert path.is_file(), f"{path} is missing"
    document = json.loads(path.read_text(encoding="utf-8"))
    commands = document.get("app_commands") or []
    assert len(commands) > 0, f"no commands in {path}"

    for command in commands:
        cmd_id = command.get("app_command_id", "<unnamed>")
        actual_keys = tuple(command.keys())
        assert actual_keys == EXPECTED_FIELDS, (
            f"Record '{cmd_id}' in {path.name} does not match expected 14 keys in order:\n"
            f"  actual:   {actual_keys}\n"
            f"  expected: {EXPECTED_FIELDS}"
        )
        # Ensure none of the values are null
        for field in EXPECTED_FIELDS:
            assert command[field] is not None, f"Field '{field}' in '{cmd_id}' of {path.name} is null"


def test_app_metrics_is_async_with_parallel_brake_and_one_second_repeat() -> None:
    """APP-METRICS is async with max_parallel 4 and repeat_interval 1 in data and catalogue files."""
    for path in (
        REPO_ROOT / "data" / "app_commands.json",
        REPO_ROOT / "data" / "app_commands.example.json",
        REPO_ROOT / "dbabrain" / "jobs" / "catalogue" / "app_commands.json",
    ):
        document = json.loads(path.read_text(encoding="utf-8"))
        metrics_cmd = next(c for c in document["app_commands"] if c["app_command_id"] == "APP-METRICS")
        assert metrics_cmd["run_mode"] == "async", f"{path.name} run_mode not async"
        assert metrics_cmd["max_parallel"] == 4, f"{path.name} max_parallel not 4"
        assert metrics_cmd["time_window"]["repeat_interval"] == 1, f"{path.name} repeat_interval not 1"
        assert "0.30.0" in metrics_cmd["note"], f"{path.name} note does not mention batch processing"


def test_all_three_reference_copies_are_byte_identical() -> None:
    """The master copy, the packaged catalogue copy, and the public example must be identical."""
    payloads = [path.read_bytes() for path in REFERENCE_COPIES]
    assert payloads[0] == payloads[1], "data/shared_config_objects.json != data/shared_config_objects.example.json"
    assert payloads[0] == payloads[2], "data/shared_config_objects.json != dbabrain/common/catalogue/shared_config_objects.json"


def test_the_reference_marks_every_app_command_field_as_required_and_non_nullable() -> None:
    """The shared reference defines app_command with required_field_count 14 and all fields required."""
    ref_path = REPO_ROOT / "data" / "shared_config_objects.json"
    document = json.loads(ref_path.read_text(encoding="utf-8"))
    shared_objects = document.get("shared_config_objects") or []
    app_cmd_entry = next(item for item in shared_objects if item.get("object") == "app_command")

    assert app_cmd_entry["field_count"] == 14
    assert app_cmd_entry["required_field_count"] == 14

    fields = app_cmd_entry.get("fields") or []
    assert sorted(f["field"] for f in fields) == sorted(EXPECTED_FIELDS)

    for f in fields:
        name = f["field"]
        assert f.get("required") is True, f"Field '{name}' is not marked required: true"
        constraint = f.get("constraint") or {}
        assert constraint.get("nullable") is False, f"Field '{name}' constraint nullable is not false"


def test_state_app_commands_fills_missing_fields_with_loader_defaults() -> None:
    """A legacy record missing run_mode, max_parallel, note, and sort_order gets loader defaults.

    Defaults: run_mode='sync', max_parallel=1, note='', sort_order=index+1 (or app_ord).
    The resulting keys must be in the canonical 14-field order.
    """
    incomplete_record = {
        "node_role": "worker",
        "app_command_id": "APP-CUSTOM",
        "app_code": "APP-CUSTOM",
        "app_name": "custom_app",
        "display_name": "Custom scheduled app",
        "log_scope": "custom",
        "working_dir": "tools/dbabrain",
        "command_text": "python -m custom.app",
        "active": True,
        "time_window": {
            "from_hour": 0,
            "to_hour": 23,
            "repeat_interval": 300,
            "retry_interval": 60,
            "timeout": 3600,
        },
    }
    doc = {"app_commands": [copy.deepcopy(incomplete_record)]}

    changes = earlier_defaults.state_app_commands(doc)

    assert len(changes) >= 4, f"expected at least 4 changes recorded, got {changes}"
    record = doc["app_commands"][0]
    assert tuple(record.keys()) == EXPECTED_FIELDS
    assert record["run_mode"] == "sync"
    assert record["max_parallel"] == 1
    assert record["note"] == ""
    assert record["sort_order"] == 1


def test_state_app_commands_leaves_a_complete_fourteen_field_record_unchanged() -> None:
    """A complete 14-field record in canonical order produces zero changes and is left intact."""
    ref_path = REPO_ROOT / "data" / "app_commands.json"
    doc = json.loads(ref_path.read_text(encoding="utf-8"))
    original_copy = copy.deepcopy(doc)

    changes = earlier_defaults.state_app_commands(doc)

    assert changes == []
    assert doc == original_copy


def test_upgrade_config_earlier_defaults_step_upgrades_legacy_app_commands_file(tmp_path: Path) -> None:
    """Running upgrade-config with step 'earlier-defaults' updates legacy app_commands.json idempotently."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy_doc = {
        "app_commands": [
            {
                "app_command_id": "APP-SAMPLE",
                "app_code": "APP-SAMPLE",
                "app_name": "sample_app",
                "display_name": "Sample App",
                "log_scope": "sample",
                "working_dir": "tools/dbabrain",
                "command_text": "python -m sample",
                "active": True,
                "node_role": "worker",
                "time_window": {"repeat_interval": 60, "retry_interval": 30, "timeout": 120},
            }
        ]
    }
    app_cmd_file = data_dir / "app_commands.json"
    app_cmd_file.write_text(json.dumps(legacy_doc, indent=2), encoding="utf-8")

    # Run upgrade step
    _, answer = config_upgrade.upgrade({
        "data_dir": str(data_dir),
        "steps": ["earlier-defaults"],
        "dry_run": False,
    })

    updated_doc = json.loads(app_cmd_file.read_text(encoding="utf-8"))
    record = updated_doc["app_commands"][0]
    assert tuple(record.keys()) == EXPECTED_FIELDS
    assert record["run_mode"] == "sync"
    assert record["max_parallel"] == 1
    assert record["note"] == ""
    assert record["sort_order"] == 1

    # Idempotent second run
    _, second_answer = config_upgrade.upgrade({
        "data_dir": str(data_dir),
        "steps": ["earlier-defaults"],
        "dry_run": False,
    })
    step = next(s for s in second_answer["steps"] if s["step"] == "earlier-defaults")
    assert step["records_changed"] == 0


def test_a_field_the_daemon_never_filled_is_left_for_the_check_to_name() -> None:
    """No log_scope, id or schedule is invented: the daemon refused the first two and had no
    repeat_interval for the third, so writing one would change what the node does."""
    doc = {"app_commands": [{"app_command_id": "APP-X", "command_text": "python -m x"}]}

    earlier_defaults.state_app_commands(doc)

    record = doc["app_commands"][0]
    assert "log_scope" not in record and "time_window" not in record
    assert record["app_code"] == record["app_name"] == record["display_name"] == "APP-X"
