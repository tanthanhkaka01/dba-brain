"""A command whose payload is a JSON template must still send valid JSON when nothing was typed.

`/spbot_list_sql_runs` answered `request is not valid JSON: Expecting value: line 1 column 12`
with no arguments, and column 24 with one. Its `command_argv` carries the request as a template —
`{"sql_id": {sql_id}, "limit": {limit}, "format": "txt"}` — and a skipped optional parameter
rendered as nothing at all, leaving `{"sql_id": , ...}`.

Both parameters declare `"default": "0"`. The resolver read defaults only from
`action_config.defaults`, so a default written beside its own parameter — where the prompt flow
already reads it, and where a person naturally puts it — was accepted by the config and used by
nobody.

The same failure had already happened once, to `/spbot_trace_session` on 2026-08-12, and the fix
then was to move that command's default into the other spelling. Nothing tested the *rendering*,
so the trap stayed set for the next command, and `tests/test_sql_run_history_request_defaults.py`
did not catch it: it checks what the CLI does with `sql_id: 0` and `limit: 0`, which is the right
question about a request that arrives. This one is about the request arriving at all.

So the test is deliberately not about one command: every shipped command whose argv contains a
JSON-looking template is rendered with no arguments and parsed.
"""

from __future__ import annotations

import json
import re

import pytest

from db_ops.telegram.command_processor import SupportCommand, build_cli_argv, cli_action_values

from conftest import shipped_config


def _commands():
    """Every shipped `cli_execute` command, from the catalogue this installation would use."""
    raw = json.loads(shipped_config("telegram_support_commands.json").read_bytes().decode("utf-8-sig"))
    commands = raw.get("telegram_support_commands") or []
    return [item for item in commands if str(item.get("action_type") or "") == "cli_execute"]


def _json_arguments(argv: list[str]) -> list[str]:
    """The argv entries that are meant to be a JSON object, by their own shape."""
    return [part for part in argv if part.strip().startswith("{") and part.strip().endswith("}")]


def _rendered(entry: dict, args: list[str], tmp_path) -> list[str]:
    command = SupportCommand(
        command_id=int(entry.get("command_id") or 0),
        command_text=str(entry.get("command_text") or ""),
        command_type=int(entry.get("command_type") or 0),
        reply_default=0, reply_text="", is_group=1, is_private=1, need_file=0,
        action_type="cli_execute",
        action_config=entry.get("action_config") or {},
    )
    values = cli_action_values(command=command, args=list(args),
                               config_path=tmp_path / "config.json", chat_id="1", user_id="2")
    return build_cli_argv(command.action_config, values)


def _ids():
    return [str(entry.get("command_text") or "?") for entry in _commands()]


@pytest.mark.parametrize("entry", _commands(), ids=_ids())
def test_every_json_payload_is_valid_json_when_no_argument_is_typed(entry, tmp_path):
    """The operator types the command and nothing else, which is how both breakages were found."""
    required = [p for p in (entry.get("action_config", {}).get("parameters") or [])
                if bool(p.get("required", True)) and str(p.get("source") or "arg") == "arg"]
    if required:
        pytest.skip("this command refuses an empty call before it renders anything")

    for payload in _json_arguments(_rendered(entry, [], tmp_path)):
        # A template that still holds an unresolved {name} is the same defect one step earlier.
        assert not re.search(r"\{[a-z_]+\}", payload), f"unresolved placeholder in {payload}"
        json.loads(payload)


def test_a_skipped_second_argument_does_not_break_the_payload(tmp_path):
    """One argument given and one skipped — the shape that answered `column 24`."""
    entry = next(e for e in _commands() if e.get("command_text") == "spbot_list_sql_runs")

    for payload in _json_arguments(_rendered(entry, ["28"], tmp_path)):
        body = json.loads(payload)
        assert body["sql_id"] == 28, "what was typed"
        assert body["limit"] == 0, "and the default for what was not"


def test_a_default_written_beside_its_parameter_is_used(tmp_path):
    """The two spellings mean the same thing, and both are read.

    `action_config.defaults` is the older one; `parameters[].default` is where the prompt flow
    already looks and where a reader writes it without thinking.
    """
    entry = {
        "command_text": "spbot_example",
        "action_type": "cli_execute",
        "action_config": {
            "command_argv": ["{python}", "-c", '{"n": {n}, "m": {m}}'],
            "defaults": {"m": "7"},
            "parameters": [
                {"name": "n", "source": "arg", "position": 1, "required": False, "default": "3"},
                {"name": "m", "source": "arg", "position": 2, "required": False},
            ],
        },
    }
    payload = _json_arguments(_rendered(entry, [], tmp_path))[0]
    assert json.loads(payload) == {"n": 3, "m": 7}
