"""`/spbot_sql_to_xlsx` must keep meaning exactly what it meant, while a format becomes askable.

The old command's second argument is `consume_rest` SQL text. Inserting a format argument into it
would have silently re-read every existing invocation — the first word of someone's SELECT would
have become the format. So the format is a *declared parameter*: a command that declares one takes
it from the arguments, and a command that does not keeps taking it from `action_config`. That is
the whole reason `_format_argument_position` reads the config instead of hard-coding an index, and
these tests are what stops someone "simplifying" it back.
"""

from __future__ import annotations

import json
from pathlib import Path

from db_ops.common.config_admin import FILE_OUTPUT_FORMATS
from db_ops.telegram.command_processor import _format_argument_position
from conftest import shipped_config

COMMANDS_PATH = shipped_config("telegram_support_commands.json")


def _commands():
    document = json.loads(COMMANDS_PATH.read_bytes().decode("utf-8-sig"))
    rows = document["telegram_support_commands"] if isinstance(document, dict) else document
    return {str(row.get("command_text")): row for row in rows}


def test_the_old_command_declares_no_format_parameter_so_its_arguments_are_unchanged():
    config = _commands()["spbot_sql_to_xlsx"]["action_config"]
    assert _format_argument_position(config) is None
    positions = {p["name"]: p["position"] for p in config["parameters"]}
    assert positions == {"target": 1, "sql_text": 2}


def test_the_old_command_still_produces_a_workbook_without_saying_so_anywhere_new():
    """It carries no `format`, so the handler's default applies — the file its name promises."""
    config = _commands()["spbot_sql_to_xlsx"]["action_config"]
    assert "format" not in config
    assert str(config.get("format") or "xlsx") == "xlsx"


def test_the_new_command_takes_the_format_before_the_sql_text():
    """sql_text is consume_rest, so anything after it is swallowed — the format has to come first."""
    config = _commands()["spbot_sql_export"]["action_config"]
    assert _format_argument_position(config) == 2
    positions = {p["name"]: p["position"] for p in config["parameters"]}
    assert positions == {"target": 1, "format": 2, "sql_text": 3}
    sql_text = next(p for p in config["parameters"] if p["name"] == "sql_text")
    assert sql_text.get("consume_rest") is True


def test_the_new_command_has_no_default_format_beside_its_format_argument():
    """A default sitting next to the parameter would be a second answer to the same question,
    and nothing in the config would show which one won."""
    assert "format" not in _commands()["spbot_sql_export"]["action_config"]


def test_both_commands_share_one_action_so_there_is_one_export_path():
    commands = _commands()
    assert commands["spbot_sql_to_xlsx"]["action_type"] == "sql_to_xlsx"
    assert commands["spbot_sql_export"]["action_type"] == "sql_to_xlsx"


def test_the_two_commands_do_not_write_into_each_others_output_folder():
    commands = _commands()
    old = commands["spbot_sql_to_xlsx"]["action_config"]
    new = commands["spbot_sql_export"]["action_config"]
    assert old["output_dir"] != new["output_dir"]
    assert old["file_name_template"] != new["file_name_template"]


def test_the_prompt_offers_the_formats_that_actually_produce_a_document():
    """raw and json render fine but are not documents an operator can open, so the handler
    refuses them; the prompt must not advertise what will be rejected."""
    config = _commands()["spbot_sql_export"]["action_config"]
    prompt = next(p for p in config["parameters"] if p["name"] == "format")["prompt_text"].lower()
    for file_format in FILE_OUTPUT_FORMATS:
        assert file_format in prompt
    assert "raw" not in prompt


def test_command_ids_stay_unique():
    document = json.loads(COMMANDS_PATH.read_bytes().decode("utf-8-sig"))
    rows = document["telegram_support_commands"] if isinstance(document, dict) else document
    ids = [row.get("command_id") for row in rows]
    assert len(ids) == len(set(ids))
