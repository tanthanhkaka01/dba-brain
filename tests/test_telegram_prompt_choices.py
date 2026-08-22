"""A prompt that lists what the operator can choose, and never blocks the flow to do it.

`/spbot_xlsx_to_table` asks for a server, then a database, then a schema. Two of those three are
names the operator has to remember exactly, and getting one wrong costs a whole round trip through
Telegram — the command already knows how to list both (`list-databases`, `list-schemas` in the
`common` CLI), it just was not asking.

The listing is an **aid, not a gate**, and that is the property these tests exist to hold. Every
way of failing to produce a list — an unreachable instance, a wrong credential, an engine
`list-schemas` does not know, a timeout, a malformed response — must still send the bare prompt,
because a prompt is the only thing that keeps the conversation moving and typing the name has
always worked. A listing feature that can leave the flow with no question asked is worse than no
listing at all.

The other half is the cap: the estate has instances with well over a hundred databases, and a
Telegram message dies at 4096 characters. A list that silently stopped at 40 would tell the
operator their database does not exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.lib import common_cli
from db_ops.lib.listing import MAX_PROMPT_CHOICES, choice_lines
from db_ops.telegram import command_processor
from conftest import shipped_config


PARAMETERS = [
    {"name": "server_id", "position": 1, "required": True, "prompt_text": "Server id?"},
    {
        "name": "database", "position": 2, "required": True, "prompt_text": "Database name?",
        "prompt_choices": {"command": "list-databases", "data_key": "databases",
                           "request": {"target": "{server_id}"}},
    },
    {
        "name": "schema", "position": 3, "required": True, "prompt_text": "Schema?",
        "prompt_choices": {"command": "list-schemas", "data_key": "schemas",
                           "request": {"target": "{server_id}", "database": "{database}"}},
    },
]


def _parameter(name: str) -> dict:
    return next(item for item in PARAMETERS if item["name"] == name)


@pytest.fixture
def answered(monkeypatch):
    """Record the request `run_allowing_failure` was given, and answer with two databases."""
    seen: dict = {}

    def fake_run(command, request, **_kwargs):
        seen["command"] = command
        seen["request"] = request
        return True, {"databases": [{"name": "APPDB_Testing"}, {"name": "APPDB_Prod"}],
                      "schemas": [{"name": "dbo"}, {"name": "guest"}]}, ""

    monkeypatch.setattr(common_cli, "run_allowing_failure", fake_run)
    return seen


def test_the_database_prompt_lists_the_databases(answered):
    text = command_processor.render_prompt_text(
        _parameter("database"), PARAMETERS, ["ACME-192-0-2-111"])

    assert "Database name?" in text
    assert "1. APPDB_Testing" in text
    assert "2. APPDB_Prod" in text


def test_the_server_just_answered_is_what_gets_listed(answered):
    command_processor.render_prompt_text(
        _parameter("database"), PARAMETERS, ["ACME-192-0-2-111"])

    assert answered["command"] == "list-databases"
    assert answered["request"] == {"target": "ACME-192-0-2-111"}


def test_the_schema_prompt_carries_both_earlier_answers(answered):
    """Schemas live inside one database; without it the answer describes the login's default."""
    command_processor.render_prompt_text(
        _parameter("schema"), PARAMETERS, ["ACME-192-0-2-111", "APPDB_Testing"])

    assert answered["command"] == "list-schemas"
    assert answered["request"] == {"target": "ACME-192-0-2-111", "database": "APPDB_Testing"}


def test_a_parameter_without_prompt_choices_is_unchanged(answered):
    text = command_processor.render_prompt_text(_parameter("server_id"), PARAMETERS, [])

    assert text == "Server id?"
    assert answered == {}                     # nothing was run


# --- fail open: every one of these must still ask the question -----------------------------

def test_an_unreachable_instance_still_sends_the_prompt(monkeypatch):
    monkeypatch.setattr(common_cli, "run_allowing_failure",
                        lambda *a, **k: (False, {}, "Login timeout expired."))

    text = command_processor.render_prompt_text(
        _parameter("database"), PARAMETERS, ["ACME-192-0-2-111"])

    assert text == "Database name?"


def test_a_crashing_cli_still_sends_the_prompt(monkeypatch):
    """Including the timeout: the prompt is most needed exactly when the server does not answer."""
    def boom(*_args, **_kwargs):
        raise common_cli.CommonCliError("timed out after 25s")

    monkeypatch.setattr(common_cli, "run_allowing_failure", boom)

    assert command_processor.render_prompt_text(
        _parameter("database"), PARAMETERS, ["ACME-192-0-2-111"]) == "Database name?"


def test_a_response_without_the_data_key_still_sends_the_prompt(monkeypatch):
    monkeypatch.setattr(common_cli, "run_allowing_failure", lambda *a, **k: (True, {"count": 0}, ""))

    assert command_processor.render_prompt_text(
        _parameter("database"), PARAMETERS, ["ACME-192-0-2-111"]) == "Database name?"


def test_an_empty_list_still_sends_the_prompt(monkeypatch):
    monkeypatch.setattr(common_cli, "run_allowing_failure",
                        lambda *a, **k: (True, {"databases": []}, ""))

    assert command_processor.render_prompt_text(
        _parameter("database"), PARAMETERS, ["ACME-192-0-2-111"]) == "Database name?"


def test_an_unanswered_placeholder_does_not_run_anything(answered):
    """Arguments out of order: no server yet, so there is nothing to list schemas on."""
    text = command_processor.render_prompt_text(_parameter("schema"), PARAMETERS, ["", ""])

    assert text == "Schema?"
    assert answered == {}


# --- the cap ---------------------------------------------------------------------------------

def test_a_long_list_is_capped_and_says_so():
    text = choice_lines([f"db_{index}" for index in range(120)])

    assert "1. db_0" in text
    assert f"{MAX_PROMPT_CHOICES}. db_{MAX_PROMPT_CHOICES - 1}" in text
    assert f"... and {120 - MAX_PROMPT_CHOICES} more" in text
    assert "db_119" not in text


def test_a_list_that_fits_gets_no_footnote():
    assert "more" not in choice_lines(["dbo", "guest"])


def test_blank_names_are_dropped_rather_than_numbered():
    assert choice_lines(["dbo", "", "  ", "guest"]) == "  1. dbo\n  2. guest"


def test_no_names_renders_nothing():
    """So a caller can append unconditionally and get the bare prompt back."""
    assert choice_lines([]) == ""


# --- the shipped config ----------------------------------------------------------------------

def test_the_shipped_xlsx_command_lists_both_database_and_schema():
    """The point of the feature: neither name has to be remembered."""
    shipped = json.loads(
        shipped_config("telegram_support_commands.json").read_text(encoding="utf-8")
    )["telegram_support_commands"]
    entry = next(c for c in shipped if c["command_text"] == "spbot_xlsx_to_table")
    by_name = {p["name"]: p for p in entry["action_config"]["parameters"]}

    assert by_name["database"]["prompt_choices"]["command"] == "list-databases"
    assert by_name["schema"]["prompt_choices"]["command"] == "list-schemas"
    assert by_name["schema"]["prompt_choices"]["request"]["database"] == "{database}"
