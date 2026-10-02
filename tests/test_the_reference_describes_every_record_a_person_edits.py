"""The records a person edits are described field by field, and held to what the code reads.

0.22.0 added seven entries to ``shared_config_objects.json``: ``sql_command``, ``sql_target``,
``db_instance``, ``telegram_support_command`` (with ``telegram_cli_execute`` for its commonest
action) and ``metric_definition`` with ``metric_variant``. Until then the reference described the
blocks shared *inside* records and four whole records, and the files an operator edits most were
described nowhere a program could ask.

What these tests hold:

* **each entry against the code that reads the record** - the engines a task can run on, the
  collector types, the action types. A reference that can drift is a lie with a schema;
* **a whole record is checked whole** (``whole_record``): a key the entry does not describe is
  reported, where before the checker dropped it as another schema's and the loader ignored it;
* **the registrars read the reference** - a request key that is neither a field nor an option is
  refused by name, and a field the reference describes is written rather than accepted and dropped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.common import sql_task_admin
from db_ops.lib import shared_objects
from db_ops.lib.sql_access import SQL_TASK_DB_TYPES
from db_ops.metrics import definitions
from db_ops.telegram.command_processor import ACTION_TYPES

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA = REPO_ROOT / "data"


def _entry(name: str) -> dict:
    return shared_objects.describe(name, data_dir=DATA)


def _field(name: str, field: str) -> dict:
    return shared_objects.describe(name, data_dir=DATA, field=field)


# --------------------------------------------------------------------------- #
# Each entry against the code that reads the record
# --------------------------------------------------------------------------- #
def test_a_sql_task_engine_is_one_the_runner_can_run():
    """The wider inventory vocabulary would let the reference certify a postgresql task, which is
    the defect 0.21.0 found on a live node nine hours after registering one."""
    for name in ("sql_command", "sql_target"):
        assert set(_field(name, "db_type")["constraint"]["enum"]) == set(SQL_TASK_DB_TYPES), name


def test_a_metric_is_described_with_the_metric_vocabulary():
    entry = {f["field"]: f for f in _entry("metric_definition")["fields"]}
    assert set(entry["collector_type"]["constraint"]["enum"]) == definitions.SUPPORTED_COLLECTOR_TYPES
    severities = set(entry["connection_error_severity"]["constraint"]["enum"]) - {"WARN"}
    assert severities == definitions.SUPPORTED_ERROR_SEVERITIES
    required = {name for name, spec in entry.items() if spec["required"]}
    assert set(definitions.REQUIRED_FIELDS) <= required
    # "multi" is a metric value and not an instance db_type - the one place the two differ.
    assert "multi" in entry["db_type"]["constraint"]["enum"]
    assert "multi" not in _field("db_instance", "db_type")["constraint"]["enum"]


def test_a_telegram_action_is_one_the_processor_dispatches():
    assert set(_field("telegram_support_command", "action_type")["constraint"]["enum"]) == ACTION_TYPES


def test_the_security_level_of_a_command_is_described_as_one():
    rule = _field("telegram_support_command", "command_type")["rule"]
    assert "SECURITY BOUNDARY" in rule


def test_an_instance_with_no_database_is_a_host_not_an_error():
    assert "host" in _field("db_instance", "db_type")["constraint"]["enum"]


def test_the_legacy_instance_spellings_are_still_read_and_reported_as_deprecated():
    findings = shared_objects.check_record(
        "db_instance",
        {"server_id": "ACME-192-0-2-10", "site": "ACME", "environment": "prod", "ip": "192.0.2.10",
         "db_type": "sqlserver", "active": True, "metrics": {"enabled": True},
         "db_name": "LABEL", "sqlserver_major_version": 16},
        where="test", data_dir=DATA)
    assert sorted((f["kind"], f["field"]) for f in findings) == [
        ("deprecated", "db_name"), ("deprecated", "sqlserver_major_version")]


# --------------------------------------------------------------------------- #
# A whole record is checked whole
# --------------------------------------------------------------------------- #
def test_a_misspelled_key_on_a_whole_record_is_reported(tmp_path):
    """Before whole_record, check_data_dir kept only the findings named after the entry's own
    fields - so `databse_name` was dropped, the loader ignored it, and the task ran against the
    default database while its config looked right."""
    (tmp_path / "sql_targets.json").write_text(json.dumps({"sql_targets": [{
        "sql_id": 1, "target_no": 1, "server_id": "ACME-192-0-2-10",
        "databse_name": "PAYROLL",
        "notify": {"logging_on_run": {"enabled": False}, "alert_on_error": {"enabled": True}},
        "output": {"format": "none"},
    }]}), encoding="utf-8")

    result = shared_objects.check_data_dir(tmp_path)

    unknown = [v for v in result["violations"] if v["kind"] == "unknown"]
    assert [v["field"] for v in unknown] == ["databse_name"]


def test_an_underscore_key_is_still_the_estates_own_comment(tmp_path):
    (tmp_path / "sql_commands.json").write_text(json.dumps({"sql_commands": [{
        "sql_id": 1, "sql_code": "SQLSERVER-001-X", "sql_name": "x", "db_type": "sqlserver",
        "script_type": "single", "script_path": "a.sql", "_note": "why",
    }]}), encoding="utf-8")
    assert shared_objects.check_data_dir(tmp_path)["violations"] == []


def test_a_required_field_under_its_old_spelling_is_not_missing(tmp_path):
    """`sql_name` is the old spelling of the required `display_name`: deprecated, never missing."""
    (tmp_path / "sql_commands.json").write_text(json.dumps({"sql_commands": [{
        "sql_id": 1, "sql_code": "SQLSERVER-001-X", "sql_name": "x", "db_type": "sqlserver",
        "script_type": "single", "script_path": "a.sql",
    }]}), encoding="utf-8")
    result = shared_objects.check_data_dir(tmp_path)
    assert result["violations"] == []
    assert [item["field"] for item in result["deprecated"]] == ["sql_name"]


def test_the_shipped_examples_obey_the_new_entries(tmp_path):
    """A public checkout has only the examples. The sql_commands example shipped a postgresql task
    until 0.22.0 - a task no runner could execute, whose scripts did not exist either."""
    for path in DATA.glob("*.example.json"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    result = shared_objects.check_data_dir(tmp_path)
    assert result["violations"] == [], "\n".join(
        f"{v['where']}.{v['field']}: {v['detail']}" for v in result["violations"])


# --------------------------------------------------------------------------- #
# A restore entry: what it may say for itself is described on it
# --------------------------------------------------------------------------- #
def _smb_restore_example() -> tuple[dict, dict]:
    """The example's `backup_restore` block, and its one engine (not script) restore entry."""
    from db_ops.backup_restore.config import is_script_restore

    block = json.loads((DATA / "restore_config.example.json").read_text(encoding="utf-8"))["backup_restore"]
    entry = next(item for item in block["restores"] if not is_script_restore(item))
    return block, entry


def test_a_copy_default_the_restore_block_sets_is_also_an_entry_field():
    """2026-09-29: an entry needed its own `copy_recent_hours` (a weekly FULL is older than the 24 h
    default) and the reference had it only on the block. The parser lets any key on an entry override
    the block's (`_merge_source_config`), so every setting the block carries for its entries is an
    entry field too - described the same way."""
    block = {f["field"]: f for f in _entry("backup_restore_config")["fields"]}
    entry = {f["field"]: f for f in _entry("restore_entry")["fields"]}
    for name in set(block) - {"backups", "restores"}:
        assert name in entry, f"restore_entry does not describe {name}, which the block sets for it"
        for key in ("type", "default", "constraint"):
            assert entry[name][key] == block[name][key], (name, key)


def test_an_entry_s_own_copy_settings_are_the_ones_it_restores_with():
    from db_ops.backup_restore.config import RESTORE_PARSER_DEFAULTS, _parse_restore_items

    block, entry = _smb_restore_example()
    own = {**entry, "copy_recent_hours": 192, "copy_file_patterns": ["APPDB_*"],
           "space_check": {"factor": 2.0}}
    section = {**{k: v for k, v in block.items() if k != "restores"}, "copy_recent_hours": 24,
               "restores": [own]}

    [config] = _parse_restore_items(values=dict(RESTORE_PARSER_DEFAULTS), section=section)

    assert config.copy_recent_hours == 192
    assert list(config.copy_file_patterns) == ["APPDB_*"]
    assert config.space_check.factor == 2.0


def test_space_check_is_described_as_its_parser_reads_it():
    from db_ops.lib import restore_space

    fields = {f["field"]: f for f in _entry("restore_space_check")["fields"]}
    assert set(fields) == {"enabled", "factor", "on_unknown", "measure_restore"}
    # Off unless the entry says so, as the parser reads it (the operator, 2026-10-02).
    assert fields["measure_restore"]["default"] == "false"
    assert restore_space.SpaceCheck().measure_restore is False
    assert fields["factor"]["constraint"]["min"] == restore_space.MINIMUM_SAFETY_FACTOR
    assert float(fields["factor"]["default"]) == restore_space.DEFAULT_SAFETY_FACTOR
    assert tuple(fields["on_unknown"]["constraint"]["enum"]) == restore_space.UNKNOWN_CHOICES
    assert _field("restore_entry", "space_check")["constraint"]["checked_by"] == "restore_space_check"


def test_a_bad_value_in_an_entry_s_own_copy_settings_is_reported(tmp_path):
    block, entry = _smb_restore_example()
    bad = {**entry, "copy_recent_hours": "eight days", "space_check": {"factor": 1.5, "on_unkown": "refuse"}}
    (tmp_path / "restore_config.json").write_text(json.dumps({"backup_restore": {
        **{k: v for k, v in block.items() if k != "restores"}, "restores": [bad]}}), encoding="utf-8")

    violations = shared_objects.check_data_dir(tmp_path)["violations"]

    fields = sorted(v["field"] for v in violations)
    assert "copy_recent_hours" in fields and "on_unkown" in fields, violations


# --------------------------------------------------------------------------- #
# The registrars read the reference
# --------------------------------------------------------------------------- #
@pytest.fixture()
def roots(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "sql_commands.json").write_text(json.dumps({"sql_commands": []}), encoding="utf-8")
    (data / "sql_targets.json").write_text(json.dumps({"sql_targets": []}), encoding="utf-8")
    (data / "db_instances.json").write_text(json.dumps({"db_instances": []}), encoding="utf-8")
    script = tmp_path / "assets" / "tasks" / "sqlserver" / "001_x.sql"
    script.parent.mkdir(parents=True)
    script.write_text("SELECT 1;\n", encoding="utf-8")
    return data, tmp_path


def _command(data, root, **extra):
    request = {"sql_name": "x", "db_type": "sqlserver",
               "script_path": "assets/tasks/sqlserver/001_x.sql", **extra}
    return sql_task_admin.add_sql_command(request, data_dir=data, tool_root=root)


def test_a_request_key_that_is_not_a_field_is_refused_by_name(roots):
    data, root = roots
    with pytest.raises(sql_task_admin.SqlTaskAdminError) as excinfo:
        _command(data, root, sql_nmae="typo")
    assert "sql_nmae: not a field of sql_command" in str(excinfo.value)
    assert json.loads((data / "sql_commands.json").read_text())["sql_commands"] == []


def test_a_target_key_that_is_not_a_field_is_refused_before_anything_is_read(roots):
    data, root = roots
    _command(data, root)
    with pytest.raises(sql_task_admin.SqlTaskAdminError) as excinfo:
        sql_task_admin.add_sql_target({"sql_id": 1, "server_id": "ACME-192-0-2-10",
                                       "databse_name": "PAYROLL"}, data_dir=data)
    assert "databse_name" in str(excinfo.value)


def test_a_described_command_field_is_written_not_accepted_and_dropped(roots):
    data, root = roots
    _command(data, root, progress_per_file=False)
    written = json.loads((data / "sql_commands.json").read_text())["sql_commands"][0]
    assert written["progress_per_file"] is False


def test_a_notify_block_in_the_request_is_the_one_written(roots):
    """A request copied out of sql_targets.json carries the block. It was dropped for the flat
    defaults, which re-routed a moved task's messages without a word."""
    data, root = roots
    _command(data, root)
    sql_task_admin.add_sql_target({
        "sql_id": 1, "server_id": "ACME-192-0-2-10",
        "notify": {"logging_on_run": {"enabled": False, "telegram_chat": "sql", "chat_id": ""},
                   "alert_on_error": {"enabled": True, "telegram_chat": "sql", "chat_id": ""}},
    }, data_dir=data)
    written = json.loads((data / "sql_targets.json").read_text())["sql_targets"][0]
    assert written["notify"]["logging_on_run"]["enabled"] is False
    assert written["notify"]["alert_on_error"]["enabled"] is True


def test_a_target_may_override_the_servers_transport(roots):
    data, root = roots
    _command(data, root)
    sql_task_admin.add_sql_target({"sql_id": 1, "server_id": "ACME-192-0-2-10",
                                   "sql_access": {"method": "direct"}}, data_dir=data)
    written = json.loads((data / "sql_targets.json").read_text())["sql_targets"][0]
    assert written["sql_access"] == {"method": "direct"}
