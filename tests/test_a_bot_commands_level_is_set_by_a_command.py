"""A bot command's level is set by a command, not by a hand-edit of the file.

A node's own ``telegram_support_commands.json`` is used as written - the file is the truth (owner,
2026-10-01). So a node filled from another node's bundle keeps that node's levels: the 0.26 soak
node ran ``/spbot_add_sql`` at level 10 where this version ships 100 (the 0.26 sheet, section 6, C1).
``upgrade-config`` leaves the file alone on purpose - a level is the operator's value, and an
estate may keep some of them below the shipped ones - and the log's warning named a hand-edit as
the only fix. ``telegram.cli command-level`` is that edit as a command.
"""

from __future__ import annotations

import json

import pytest

from db_ops.lib import errors
from db_ops.telegram.command_permissions import set_command_level, warn_low_level


def _files(tmp_path, node_levels, shipped_levels):
    def write(name, levels):
        path = tmp_path / name
        path.write_text(json.dumps({"notes": ["kept"], "telegram_support_commands": [
            {"command_id": index, "command_text": text, "command_type": level, "action_type": "cli_execute"}
            for index, (text, level) in enumerate(levels.items(), 1)]}, indent=2) + "\n", encoding="utf-8")
        return path

    return write("node.json", node_levels), write("shipped.json", shipped_levels)


def _levels(path):
    document = json.loads(path.read_text(encoding="utf-8"))
    return {row["command_text"]: row["command_type"] for row in document["telegram_support_commands"]}


def test_one_command_is_given_the_level_that_was_typed(tmp_path):
    node, shipped = _files(tmp_path, {"spbot_add_sql": 10, "spbot_status": 10}, {})

    answer = set_command_level(bot_command="/spbot_add_sql", level=100, commands_path=node, shipped_path=shipped)

    assert answer["changed"] == [{"command_text": "spbot_add_sql", "before": 10, "after": 100}]
    assert answer["written"] is True
    assert _levels(node) == {"spbot_add_sql": 100, "spbot_status": 10}
    assert json.loads(node.read_text(encoding="utf-8"))["notes"] == ["kept"], "the rest of the file stays"


def test_shipped_raises_every_command_below_this_versions_level_and_nothing_else(tmp_path):
    node, shipped = _files(
        tmp_path,
        {"spbot_add_sql": 10, "spbot_sql_export": 10, "spbot_status": 10, "spbot_kill_spid": 100,
         "spbot_off": -1, "spbot_my_own": 5},
        {"spbot_add_sql": 100, "spbot_sql_export": 50, "spbot_status": 10, "spbot_kill_spid": 50,
         "spbot_off": 50})

    answer = set_command_level(shipped=True, commands_path=node, shipped_path=shipped)

    assert [(c["command_text"], c["before"], c["after"]) for c in answer["changed"]] == [
        ("spbot_add_sql", 10, 100), ("spbot_sql_export", 10, 50)]
    levels = _levels(node)
    assert levels["spbot_kill_spid"] == 100, "a level above the shipped one is somebody's decision"
    assert levels["spbot_off"] == -1, "a command switched off stays off"
    assert levels["spbot_my_own"] == 5, "a command this version does not ship has no level to take"


def test_a_dry_run_names_the_changes_and_writes_nothing(tmp_path):
    node, shipped = _files(tmp_path, {"spbot_add_sql": 10}, {"spbot_add_sql": 100})
    before = node.read_bytes()

    answer = set_command_level(shipped=True, dry_run=True, commands_path=node, shipped_path=shipped)

    assert answer["changed"] and answer["written"] is False
    assert node.read_bytes() == before


def test_a_file_already_at_its_levels_is_left_untouched(tmp_path):
    node, shipped = _files(tmp_path, {"spbot_add_sql": 100}, {"spbot_add_sql": 100})
    before = node.read_bytes()

    answer = set_command_level(shipped=True, commands_path=node, shipped_path=shipped)

    assert answer["changed"] == [] and answer["written"] is False and "nothing to change" in answer["note"]
    assert node.read_bytes() == before


def test_minus_one_switches_a_command_off(tmp_path):
    node, shipped = _files(tmp_path, {"spbot_kill_spid": 100}, {})

    set_command_level(bot_command="spbot_kill_spid", level=-1, commands_path=node, shipped_path=shipped)

    assert _levels(node)["spbot_kill_spid"] == -1


def test_a_name_that_is_only_part_of_a_command_is_refused(tmp_path):
    """A level is a permission: `spbot_sql` must not quietly mean `spbot_sql_export`."""
    node, shipped = _files(tmp_path, {"spbot_sql_export": 10}, {})

    with pytest.raises(errors.InvalidRequest, match="spbot_sql_export"):
        set_command_level(bot_command="spbot_sql", level=50, commands_path=node, shipped_path=shipped)


def test_a_request_that_does_not_say_which_level_is_refused(tmp_path):
    node, shipped = _files(tmp_path, {"spbot_add_sql": 10}, {"spbot_add_sql": 100})

    with pytest.raises(errors.InvalidRequest, match="level"):
        set_command_level(bot_command="spbot_add_sql", commands_path=node, shipped_path=shipped)
    with pytest.raises(errors.InvalidRequest, match="not both"):
        set_command_level(bot_command="spbot_add_sql", level=50, shipped=True,
                          commands_path=node, shipped_path=shipped)
    with pytest.raises(errors.InvalidRequest, match="name the command"):
        set_command_level(level=50, commands_path=node, shipped_path=shipped)


def test_the_logs_warning_names_the_command_that_fixes_it():
    warning = warn_low_level("spbot_add_sql", "add_sql_task", 10)

    assert "command-level --bot-command spbot_add_sql --level 100" in warning
