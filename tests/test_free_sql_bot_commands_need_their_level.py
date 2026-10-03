"""Bot actions that run free SQL ship at the level of what that SQL can do (review 0.25.0, F8.1).

`/spbot_add_sql` registers SQL the scheduler runs with commit on the target's own credential, and
`/spbot_sql_to_xlsx` runs any text (KILL, RECONFIGURE, xp_cmdshell are not undone by its rollback).
Both were level 10. The shipped catalogue now says 100 and 50; a node file set lower is used as
written - the file is the truth (owner, 2026-10-01) - and a warning names the fix, once.
"""

from __future__ import annotations

import json
from pathlib import Path

from db_ops.telegram import command_processor
from db_ops.telegram.command_processor import load_support_commands, warn_low_level

from conftest import patch_telegram


def test_the_warning_names_the_fix():
    assert "set command_type to 100" in warn_low_level("spbot_add_sql", "add_sql_task", 10)
    assert warn_low_level("spbot_add_sql", "add_sql_task", 100) == ""
    assert warn_low_level("spbot_sql_to_xlsx", "sql_to_xlsx", -1) == "", "disabled is fine"
    assert warn_low_level("spbot_x", "cli", 1) == ""


def test_a_node_file_left_at_10_is_used_as_written_and_warned_about_once(tmp_path, capsys, monkeypatch):
    patch_telegram(monkeypatch, "_WARNED_LEVELS", set())
    path = tmp_path / "telegram_support_commands.json"
    path.write_text(json.dumps({"telegram_support_commands": [
        {"command_id": 12, "command_text": "spbot_add_sql", "command_type": 10, "action_type": "add_sql_task"},
    ]}), encoding="utf-8")

    first = load_support_commands(path)
    load_support_commands(path)

    assert first[0].command_type == 10
    assert capsys.readouterr().err.count("spbot_add_sql is level 10") == 1


def test_the_shipped_catalogue_states_the_levels_itself():
    shipped = Path(__file__).resolve().parents[1] / "db_ops" / "telegram" / "catalogue" / "telegram_support_commands.json"
    rows = json.loads(shipped.read_text(encoding="utf-8"))["telegram_support_commands"]
    levels = {r["command_text"]: r["command_type"] for r in rows
              if r.get("action_type") in ("add_sql_task", "sql_to_xlsx")}
    assert levels["spbot_add_sql"] == 100 and levels["spbot_sql_to_xlsx"] == 50
