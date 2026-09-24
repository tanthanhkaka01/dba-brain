"""One name per concept - 0.22.0 section 1.4, stages A, B and C.

The reference named one concept several ways: a record's switch was ``active`` on eight entries and
``enabled`` on the inventory, a sort position was ``app_ord``, ``ord`` and ``menu_order``, and ``env``
meant a prod/dev label on one record and a map of environment variables on another. Two names also
carried two meanings each: ``db_name`` was the database to ``instance-add`` and a service label to
three readers, and a restore's ``databases`` held mappings where every restore request's held names.

Stage A declares the standard name in the reference and reports the old one as ``deprecated``.
Stage B makes every reader accept both. Stage C moves the files and makes every writer write the
standard name - taken on the master on 2026-09-23 ahead of the worker, on the operator's word. So
these tests hold the other direction now: nothing in this tree writes, or ships, an old spelling.
"""

from __future__ import annotations

import json
from pathlib import Path

from db_ops.common import app_command_admin, field_migration
from db_ops.common import data_sources
from db_ops.lib import field_names, shared_objects
from db_ops.lib.target_flags import is_metrics_enabled, is_target_enabled
from db_ops.telegram.command_processor import menu_order_of

DATA = Path(__file__).resolve().parents[1] / "data"


def test_the_reference_and_the_rename_table_are_one_table():
    """Two lists of renames would disagree the first time one was edited."""
    for object_name, renames in field_names.RENAMES.items():
        entry = shared_objects.describe(object_name, data_dir=DATA)
        declared = {f["field"] for f in entry["fields"]}
        legacy = entry.get("legacy_fields") or {}
        for old, new in renames.items():
            assert new in declared, f"{object_name}.{new} is the standard and must be a field"
            assert old not in declared, f"{object_name}.{old} is legacy and must not be a field"
            assert legacy.get(old) == new, f"{object_name}: legacy_fields[{old}] must be {new}"


def test_the_standard_name_wins_when_a_record_carries_both():
    record = {"enabled": True, "active": False}
    assert field_names.read(record, "db_instance", "active") is False


def test_an_instance_switched_off_under_either_name_is_off():
    assert not is_target_enabled({"enabled": False})
    assert not is_target_enabled({"active": False})
    assert not is_metrics_enabled({"active": False, "metrics": {"enabled": True}})
    assert is_target_enabled({})


def test_the_inventory_loader_hands_every_reader_both_spellings(tmp_path):
    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "A", "enabled": False, "env": "prod", "ord": 3, "database": "sales"},
        {"server_id": "B", "active": True, "environment": "dev", "sort_order": 1,
         "database_name": "hr"},
    ]}), encoding="utf-8")

    old, new = data_sources.load_db_instances(tmp_path)

    assert (old["active"], old["environment"], old["sort_order"], old["database_name"]) == (
        False, "prod", 3, "sales")
    assert (new["enabled"], new["env"], new["ord"], new["database"]) == (True, "dev", 1, "hr")


def test_the_loader_never_changes_the_file(tmp_path):
    path = tmp_path / "db_instances.json"
    path.write_text(json.dumps({"db_instances": [{"server_id": "A", "enabled": True}]}),
                    encoding="utf-8")
    before = path.read_bytes()
    data_sources.load_db_instances(tmp_path)
    assert path.read_bytes() == before


def test_a_command_menu_is_ordered_under_either_name():
    assert menu_order_of({"menu_order": 2.5}) == 2.5
    assert menu_order_of({"sort_order": 1}) == 1.0
    assert menu_order_of({}) == float("inf")


def test_a_writer_writes_the_standard_name_and_takes_the_old_one_off(tmp_path):
    """An edit through the standard name on a record still carrying the old one leaves ONE field."""
    (tmp_path / "app_commands.json").write_text(json.dumps({"app_commands": [
        {"app_command_id": "1", "app_ord": 1, "app_code": "APP-X", "log_scope": "x",
         "active": True},
    ]}), encoding="utf-8")

    outcome = app_command_admin.set_app_command({"app_code": "APP-X", "sort_order": 7},
                                                data_dir=tmp_path)

    row = json.loads((tmp_path / "app_commands.json").read_text())["app_commands"][0]
    assert row["sort_order"] == 7 and "app_ord" not in row
    assert list(row)[1] == "sort_order", "the field keeps its place, so the diff is one line"
    assert outcome["changes"] == [{"field": "sort_order", "from": 1, "to": 7}]


def test_renaming_the_old_spelling_to_the_same_value_is_still_written(tmp_path):
    """`sort_order: 1` over `app_ord: 1` changes no value, and must still move the name."""
    (tmp_path / "app_commands.json").write_text(json.dumps({"app_commands": [
        {"app_command_id": "1", "app_ord": 1, "app_code": "APP-X", "log_scope": "x"},
    ]}), encoding="utf-8")
    app_command_admin.set_app_command({"app_code": "APP-X", "sort_order": 1}, data_dir=tmp_path)
    row = json.loads((tmp_path / "app_commands.json").read_text())["app_commands"][0]
    assert row.get("sort_order") == 1 and "app_ord" not in row


def test_the_shipped_files_speak_only_the_standard_names():
    """The examples and the package catalogue are what every new node starts from."""
    root = Path(__file__).resolve().parents[1]
    for folder in ("db_ops/jobs/catalogue", "db_ops/telegram/catalogue", "db_ops/webhost/catalogue",
                   "db_ops/backup_restore/catalogue", "examples/postgres-quickstart/data",
                   "examples/sqlserver-quickstart/data"):
        if not (root / folder).is_dir():
            continue  # a withheld package is absent from the exported tree
        _, data = field_migration.standardize({"data_dir": str(root / folder)})
        assert data["records_changed"] == 0, (folder, data["files"])
    _, data = field_migration.standardize({"data_dir": str(DATA)})
    left = [f for f in data["files"] if f["file"].endswith(".example.json") and f["changes"]]
    assert not left, left


def test_a_record_is_moved_in_place_and_nothing_else_changes():
    record = {"server_id": "A", "ord": 3, "enabled": False, "metrics": {"enabled": True}}
    new, changes, conflicts = field_names.standardize(record, "db_instance")
    assert list(new) == ["server_id", "sort_order", "active", "metrics"]
    assert new["active"] is False and new["metrics"] == {"enabled": True}
    assert not conflicts and [c["field"] for c in changes] == ["ord", "enabled"]


def test_two_spellings_with_one_value_become_one_field():
    new, changes, conflicts = field_names.standardize(
        {"db_name": "db_ops", "database": "db_ops"}, "db_instance")
    assert new == {"database_name": "db_ops"} and not conflicts
    assert [c["action"] for c in changes] == ["renamed", "dropped, same value"]


def test_two_spellings_that_disagree_are_never_guessed_at():
    record = {"enabled": False, "active": True}
    new, changes, conflicts = field_names.standardize(record, "db_instance")
    assert new == record and not changes
    assert conflicts == [{"standard": "active", "values": {"enabled": False, "active": True}}]


def test_the_migration_plans_by_default_and_writes_only_when_told(tmp_path):
    (tmp_path / "db_instances.json").write_text(
        '{"db_instances": [{"server_id": "A", "ord": 1, "enabled": false}]}\n', encoding="utf-8")
    before = (tmp_path / "db_instances.json").read_bytes()
    _, plan = field_migration.standardize({"data_dir": str(tmp_path)})
    assert plan["dry_run"] and plan["records_changed"] == 1
    assert (tmp_path / "db_instances.json").read_bytes() == before

    field_migration.standardize({"data_dir": str(tmp_path), "dry_run": False})
    record = json.loads((tmp_path / "db_instances.json").read_text())["db_instances"][0]
    assert record == {"server_id": "A", "sort_order": 1, "active": False}
    assert shared_objects.check_data_dir(tmp_path)["deprecated"] == []


def test_a_file_with_a_conflict_is_not_written_at_all(tmp_path):
    """Either the file moved or the file as it was - never half of it."""
    text = ('{"db_instances": [{"server_id": "A", "ord": 1},\n'
            '  {"server_id": "B", "enabled": false, "active": true}]}\n')
    (tmp_path / "db_instances.json").write_text(text, encoding="utf-8")
    _, result = field_migration.standardize({"data_dir": str(tmp_path), "dry_run": False})
    assert result["conflicts"] == 1
    assert (tmp_path / "db_instances.json").read_text(encoding="utf-8") == text


def test_a_hand_formatted_file_keeps_its_layout(tmp_path):
    """The diff of a migration is the renames: one-line arrays and blank lines survive it."""
    text = ('{\n  "_note": "kept",\n\n  "app_commands": [\n'
            '    {"app_code": "APP-X", "app_ord": 1, "time_window": {"from_hour": 0, "to_hour": 23}}\n'
            '  ]\n}\n')
    (tmp_path / "app_commands.json").write_text(text, encoding="utf-8")
    field_migration.standardize({"data_dir": str(tmp_path), "dry_run": False})
    assert (tmp_path / "app_commands.json").read_text(encoding="utf-8") == \
        text.replace('"app_ord"', '"sort_order"')


def test_a_nested_block_with_the_same_key_name_is_left_alone(tmp_path):
    """`enabled` is renamed on the instance and must stay `enabled` on its metrics block."""
    text = '{"db_instances": [{"enabled": true, "metrics": {"enabled": false}}]}\n'
    (tmp_path / "db_instances.json").write_text(text, encoding="utf-8")
    field_migration.standardize({"data_dir": str(tmp_path), "dry_run": False})
    assert json.loads((tmp_path / "db_instances.json").read_text()) == {
        "db_instances": [{"active": True, "metrics": {"enabled": False}}]}


def test_a_restore_entry_reads_its_mappings_under_either_name():
    from db_ops.backup_restore.config import _parse_database_mappings

    mapping = [{"source_database": "APP", "target_database": "APP_DRILL"}]
    for name in ("database_mappings", "databases"):
        value = field_names.read({name: mapping}, "restore_entry", "database_mappings")
        parsed = _parse_database_mappings(value)
        assert [(m.source_database, m.target_database) for m in parsed] == [
            ("APP", "APP_DRILL")]


def test_a_restore_names_its_machines_on_the_entry_and_the_old_ids_are_still_read():
    """`server_id` / `target_server_id` on the entry win; `source.id` / `target.id` are legacy."""
    from db_ops.backup_restore.config import parse_restore_config as parse

    base = {"restore_id": "R", "source": {"backup_share": "//s/b"},
            "target": {"vm_import_unc": "//t/i", "credential_target": "192.0.2.9"},
            "vm_import_local": "C:/i", "vm_log_unc": "//t/l", "vm_log_local": "C:/l",
            "restore_data_dir_on_vm": "/d", "cleanup_retention": 86400}
    standard = parse({**base, "server_id": "SRC-1", "target_server_id": "TGT-1"})
    assert (standard.source_id, standard.target_id) == ("SRC-1", "TGT-1")
    legacy = parse({**base, "source": {**base["source"], "id": "SRC-OLD"},
                    "target": {**base["target"], "id": "TGT-OLD"}})
    assert (legacy.source_id, legacy.target_id) == ("SRC-OLD", "TGT-OLD")
    both = parse({**base, "server_id": "SRC-1", "target_server_id": "TGT-1",
                  "source": {**base["source"], "id": "SRC-OLD"},
                  "target": {**base["target"], "id": "TGT-OLD"}})
    assert (both.source_id, both.target_id) == ("SRC-1", "TGT-1")


def test_a_restore_naming_a_machine_no_inventory_has_is_dangling(tmp_path):
    from db_ops.lib import config_references

    (tmp_path / "db_instances.json").write_text(json.dumps(
        {"db_instances": [{"server_id": "SRC-1"}]}), encoding="utf-8")
    (tmp_path / "restore_config.json").write_text(json.dumps({"backup_restore": {"restores": [
        {"restore_id": "R", "server_id": "SRC-1", "target_server_id": "MSSQL-DOCKER-192-0-2-9"},
    ]}}), encoding="utf-8")
    result = config_references.check(tmp_path)
    assert [item["value"] for item in result["dangling"]] == ["MSSQL-DOCKER-192-0-2-9"]


def test_a_switched_off_instance_is_read_as_off_under_either_spelling(tmp_path):
    """The reference rules say `active`; a file not yet migrated says `enabled`."""
    from db_ops.lib import config_references

    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "A", "enabled": False, "default_credential_name": "GONE"},
    ]}), encoding="utf-8")
    (tmp_path / "users.json").write_text(json.dumps({"database_credentials": []}), encoding="utf-8")
    result = config_references.check(tmp_path)
    assert result["dangling"] == [] and [i["value"] for i in result["inactive"]] == ["GONE"]


def test_a_telegram_record_is_switched_on_under_either_spelling_and_absent_means_on():
    """The permission check read a record with no `status` as active and the level routing read it
    as inactive, so one hand-written group could run commands and never be sent an alert."""
    from db_ops.lib.target_flags import is_record_active

    assert is_record_active({"active": True}) and is_record_active({"status": "active"})
    assert not is_record_active({"active": False}) and not is_record_active({"status": "left"})
    assert is_record_active({"group_id": "-1"})


def test_a_group_with_no_status_is_routed_to_as_well_as_obeyed(tmp_path):
    from db_ops.db import cli as db_cli

    (tmp_path / "telegram_groups.json").write_text(json.dumps({"telegram_groups": [
        {"group_id": "-100", "notify_level": "backup"},
        {"group_id": "-200", "notify_level": "sql", "active": False},
    ]}), encoding="utf-8")
    assert db_cli._active_group_levels(tmp_path) == {"backup": "-100"}


def test_a_remote_login_is_written_with_note_not_notes(tmp_path):
    from db_ops.common import remote_credential_admin

    (tmp_path / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": "ACME-HOST", "db_type": "host", "ip": "192.0.2.5"}]}), encoding="utf-8")
    remote_credential_admin.add_remote_credential(
        {"server_id": "ACME-HOST", "host": "192.0.2.5", "username": "ops",
         "password_ref": "REMOTE_X", "notes": "legacy spelling in the request"}, data_dir=tmp_path)
    group = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))["remote_credentials"][0]
    credential = group["credentials"][0]
    assert credential["note"] == "legacy spelling in the request" and "notes" not in credential


def test_a_restore_reads_its_secret_refs_under_either_name_and_the_new_one_wins():
    """`password_env` meant an environment variable in ssh_auth and a store ref here; the ref is
    `password_ref` now, and an entry written before still restores."""
    from db_ops.backup_restore.config import parse_restore_config as parse

    base = {"restore_id": "R", "vm_import_local": "C:/i", "vm_log_unc": "//t/l",
            "vm_log_local": "C:/l", "restore_data_dir_on_vm": "/d", "cleanup_retention": 86400}
    new = parse({**base, "source": {"backup_share": "//s/b", "password_ref": "SRC_NEW"},
                 "target": {"vm_import_unc": "//t/i", "password_ref": "VM_NEW",
                            "sql_password_ref": "SQL_NEW"}})
    old = parse({**base, "source": {"backup_share": "//s/b", "password_env": "SRC_OLD"},
                 "target": {"vm_import_unc": "//t/i", "password_env": "VM_OLD",
                            "sql_password_env": "SQL_OLD"}})
    both = parse({**base, "source": {"backup_share": "//s/b", "password_ref": "SRC_NEW",
                                     "password_env": "SRC_OLD"},
                  "target": {"vm_import_unc": "//t/i", "password_ref": "VM_NEW",
                             "password_env": "VM_OLD"}})
    assert (new.prod_smb_password_env, new.vm_password_env, new.restore_sql_password_env) == (
        "SRC_NEW", "VM_NEW", "SQL_NEW")
    assert (old.prod_smb_password_env, old.vm_password_env, old.restore_sql_password_env) == (
        "SRC_OLD", "VM_OLD", "SQL_OLD")
    assert (both.prod_smb_password_env, both.vm_password_env) == ("SRC_NEW", "VM_NEW")


def test_an_sla_policy_reads_its_title_and_synonyms_under_the_standard_names_first():
    from db_ops.sla.policies import parse_sla_policy

    old = parse_sla_policy({"policy_id": "P", "db_types": ["sqlserver"], "metric_codes": ["M"],
                            "name": "Old title", "slo_target": 98, "operator": "<=",
                            "aggregation_method": "average"})
    new = parse_sla_policy({"policy_id": "P", "db_types": ["sqlserver"], "metric_codes": ["M"],
                            "display_name": "New title", "objective_percent": 97,
                            "comparison_operator": ">", "aggregation": "minimum",
                            "name": "ignored"})
    assert (old.name, old.objective_percent, old.comparison_operator, old.aggregation) == (
        "Old title", 98.0, "<=", "average")
    assert (new.name, new.objective_percent, new.comparison_operator, new.aggregation) == (
        "New title", 97.0, ">", "minimum")


def test_the_store_reads_its_database_under_either_name():
    from db_ops.config import _parse_postgres_store

    for spelling in ("database_name", "database"):
        store = _parse_postgres_store({"host": "h", spelling: "db_ops"}, store_dir=Path("."))
        assert store.database == "db_ops"


def test_a_docker_connection_is_registered_under_the_standard_names():
    import inspect

    from db_ops.sre.docker_db import register_config

    source = inspect.getsource(register_config)
    assert '"db_type": spec.engine' in source and '"password_ref": spec.password_env' in source
    assert '"engine":' not in source and '"password_env":' not in source
