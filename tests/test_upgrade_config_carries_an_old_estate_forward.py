"""`upgrade-config` is the one step after `pip install --upgrade dbabrain`.

A release that renames or moves a field teaches every reader both shapes and every writer the new
one, and leaves the operator's own files in the old shape. Before this command the operator had to
know which migrations each release carried and run them by hand - which is how the master's own
restore ids came to be moved with a one-off script. These tests hold the promises the command makes
to someone upgrading an estate they cannot afford to lose: nothing is written without being asked,
what is written is copied first, a second run is a no-op, and a value is never guessed at.
"""

from __future__ import annotations

import json
from pathlib import Path

from db_ops.common import config_upgrade
from db_ops.lib.paths import PACKAGED_CATALOGUE


def _old_estate(root: Path) -> Path:
    """A 0.21.0-shaped data/ - old names, ids inside the restore blocks, a stale reference."""
    data = root / "data"
    data.mkdir()
    (data / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"ord": 1, "server_id": "SRC-1", "env": "prod", "enabled": True},
        {"ord": 2, "server_id": "TGT-1", "enabled": False},
    ]}, indent=2) + "\n", encoding="utf-8")
    (data / "restore_config.json").write_text(json.dumps({"backup_restore": {"restores": [
        {"restore_id": "R", "active": False,
         "source": {"id": "SRC-1", "backup_share": "//s/b"},
         "target": {"id": "TGT-1", "sql_instance": "localhost,1433"},
         "databases": [{"source_database": "APP", "target_database": "APP_DRILL"}]},
    ]}}, indent=4) + "\n", encoding="utf-8")
    (data / "sql_commands.json").write_text(json.dumps({"sql_commands": [
        {"sql_id": 1, "sql_code": "X", "sql_name": "Nightly", "db_type": "sqlserver"},
    ]}, indent=4) + "\n", encoding="utf-8")
    # An older reference: it does not know the names the other steps move to.
    (data / "shared_config_objects.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
    return data


def _read(data: Path, name: str) -> dict:
    return json.loads((data / name).read_text(encoding="utf-8"))


def test_a_plan_writes_nothing(tmp_path):
    data = _old_estate(tmp_path)
    before = {p.name: p.read_bytes() for p in data.iterdir()}

    _, plan = config_upgrade.upgrade({"data_dir": str(data)})

    assert plan["dry_run"] and plan["records_changed"] > 0
    assert {p.name: p.read_bytes() for p in data.iterdir()} == before
    assert not (tmp_path / "runtime").exists()


def test_an_old_estate_is_carried_to_the_new_shapes(tmp_path):
    data = _old_estate(tmp_path)

    _, result = config_upgrade.upgrade({"data_dir": str(data), "dry_run": False})

    assert result["conflicts"] == 0
    assert _read(data, "db_instances.json")["db_instances"][1] == {
        "sort_order": 2, "server_id": "TGT-1", "active": False}
    restore = _read(data, "restore_config.json")["backup_restore"]["restores"][0]
    assert list(restore)[:3] == ["restore_id", "server_id", "target_server_id"]
    assert (restore["server_id"], restore["target_server_id"]) == ("SRC-1", "TGT-1")
    assert "id" not in restore["source"] and "id" not in restore["target"]
    assert "database_mappings" in restore and "databases" not in restore
    assert _read(data, "sql_commands.json")["sql_commands"][0]["display_name"] == "Nightly"
    assert (data / "shared_config_objects.json").read_bytes() == \
        (PACKAGED_CATALOGUE / "shared_config_objects.json").read_bytes()
    assert result["check_objects"]["deprecated"] == 0


def test_every_file_written_is_copied_first_as_it_was(tmp_path):
    data = _old_estate(tmp_path)
    original = (data / "restore_config.json").read_bytes()

    _, result = config_upgrade.upgrade({"data_dir": str(data), "dry_run": False})

    backup = Path(result["backup_dir"])
    assert backup.parent == tmp_path / "runtime" / "config_upgrade"
    # restore_config.json is written by two steps; the copy is the file before EITHER touched it.
    assert (backup / "restore_config.json").read_bytes() == original
    assert set(result["backed_up"]) == {"db_instances.json", "restore_config.json",
                                        "sql_commands.json", "shared_config_objects.json"}


def test_a_root_this_version_initialised_has_nothing_to_move(tmp_path):
    """`init` and `upgrade-config` are two halves of one promise: what this version writes is
    already in this version's shapes. The 0.22.0 soak node's fresh `init` broke it - the store
    template still said `database`, and the reference files `init` had just written were called an
    older version's because they were compared byte for byte with the packaged copies - so a new
    operator was told on their first command to upgrade a root nothing older had touched."""
    from db_ops.common import scaffold

    scaffold.initialise(tmp_path / "root")
    _, plan = config_upgrade.upgrade({"data_dir": str(tmp_path / "root" / "data")})

    moved = [(step["step"], item["file"]) for step in plan["steps"] for item in step["files"]]
    assert plan["records_changed"] == 0, moved
    assert plan["check_objects"]["deprecated"] == 0


def test_a_reference_file_in_another_layout_is_still_this_versions(tmp_path):
    """Same document, different bytes - CRLF and a two-space indent, which is what `init` writes
    on Windows - is not an older reference, and replacing it would be a change every run."""
    data = tmp_path / "data"
    data.mkdir()
    for name in config_upgrade.REFERENCE_FILES:
        document = json.loads((PACKAGED_CATALOGUE / name).read_bytes().decode("utf-8-sig"))
        (data / name).write_bytes(json.dumps(document, indent=2).replace("\n", "\r\n").encode())

    _, plan = config_upgrade.upgrade({"data_dir": str(data), "steps": ["reference-files"]})

    assert plan["records_changed"] == 0


def test_a_second_run_plans_nothing(tmp_path):
    data = _old_estate(tmp_path)
    config_upgrade.upgrade({"data_dir": str(data), "dry_run": False})

    _, again = config_upgrade.upgrade({"data_dir": str(data)})

    assert again["records_changed"] == 0


def test_a_restore_whose_two_ids_disagree_is_left_whole(tmp_path):
    """`target.id` says one machine and `target_server_id` another: which one is meant is not a
    question a file-format migration can answer."""
    data = _old_estate(tmp_path)
    document = _read(data, "restore_config.json")
    document["backup_restore"]["restores"][0]["target_server_id"] = "SOMETHING-ELSE"
    (data / "restore_config.json").write_text(json.dumps(document, indent=4) + "\n",
                                              encoding="utf-8")
    before = (data / "restore_config.json").read_bytes()

    _, result = config_upgrade.upgrade({"data_dir": str(data), "dry_run": False,
                                        "steps": ["restore-machine-ids"]})

    assert result["conflicts"] == 1
    assert (data / "restore_config.json").read_bytes() == before


def test_a_telegram_status_becomes_the_active_switch_every_other_record_has(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "telegram_groups.json").write_text(json.dumps({"telegram_groups": [
        {"group_id": "-1", "status": "active", "note": "x"},
        {"group_id": "-2", "status": "left"},
    ]}, indent=2) + "\n", encoding="utf-8")

    config_upgrade.upgrade({"data_dir": str(data), "dry_run": False, "steps": ["telegram-active"]})

    groups = _read(data, "telegram_groups.json")["telegram_groups"]
    assert groups == [{"group_id": "-1", "active": True, "note": "x"},
                      {"group_id": "-2", "active": False}]


def test_a_telegram_record_whose_two_switches_disagree_is_left_whole(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    text = json.dumps({"telegram_users": [{"user_id": "1", "status": "active", "active": False}]})
    (data / "telegram_users.json").write_text(text, encoding="utf-8")

    _, result = config_upgrade.upgrade({"data_dir": str(data), "dry_run": False,
                                        "steps": ["telegram-active"]})

    assert result["conflicts"] == 1
    assert (data / "telegram_users.json").read_text(encoding="utf-8") == text


def test_a_command_line_naming_a_moved_command_is_pointed_at_its_cli(tmp_path):
    """A worker moved to 0.23.0 had /spbot_self_status pointed at `db.cli`; 0.24.0 keeps one
    self-status and it is `common.cli`'s (rules R43), and one timezone, `db.cli`'s. The step
    rewrites the argv, keeps the file's layout, leaves every command that did not move alone, and
    plans nothing the second time."""
    data = tmp_path / "data"
    data.mkdir()
    status = ["{python}", "-m", "db_ops.db.cli", "self-status", '{"format": "txt"}']
    clock = ["{python}", "-m", "db_ops.common.cli", "timezone", '{"format": "txt"}']
    stays = ["{python}", "-m", "db_ops.common.cli", "kill-spid", "-"]
    path = data / "telegram_support_commands.json"
    path.write_text(json.dumps({"telegram_support_commands": [
        {"command_text": "spbot_self_status", "action_config": {"command_argv": status}},
        {"command_text": "spbot_timezone", "action_config": {"command_argv": clock}},
        {"command_text": "spbot_kill_spid", "action_config": {"command_argv": stays}},
    ]}, indent=4) + "\n", encoding="utf-8")

    _, result = config_upgrade.upgrade({"data_dir": str(data), "dry_run": False,
                                        "steps": ["moved-commands"]})

    commands = _read(data, "telegram_support_commands.json")["telegram_support_commands"]
    assert commands[0]["action_config"]["command_argv"][2:4] == ["db_ops.common.cli", "self-status"]
    assert commands[1]["action_config"]["command_argv"][2:4] == ["db_ops.db.cli", "timezone"]
    assert commands[2]["action_config"]["command_argv"] == stays
    assert result["records_changed"] == 2
    assert '    "telegram_support_commands": [' in path.read_text(encoding="utf-8"), "layout kept"
    _, again = config_upgrade.upgrade({"data_dir": str(data), "steps": ["moved-commands"]})
    assert again["records_changed"] == 0
