"""The lockdown is seen, turned on and turned off from Telegram (0.35.0, 1.220 - the plan of 2026-10-29, A3.5).

An attack is noticed on a phone, at night, more often than at a terminal. ``/spbot_lockdown`` runs
``control.cli lockdown``: ``status`` reads it and asks nothing more; ``on`` and ``off`` ask why and
one ``yes``, and the person who typed it is recorded as ``telegram:<user id>`` - turning it off is
what lets the cleanup delete again, so who did it and why must be on the row. Private and
clearance 50, like the other commands that change a live system.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.lib import workflow_steps as ws
from dbabrain.telegram.command_cli import build_cli_argv

ROOT = Path(__file__).resolve().parents[1]
# The packaged catalogue is the reference; the example ships beside it; a node's own file is read
# only where there is one - the public tree carries none (0.35.0 release day).
CATALOGUE = "dbabrain/telegram/catalogue/telegram_support_commands.json"
COMMAND_FILES = tuple(path for path in ("data/telegram_support_commands.json",
                                        "data/telegram_support_commands.example.json", CATALOGUE)
                      if (ROOT / path).is_file())


def _command(path=CATALOGUE):
    commands = json.load(open(ROOT / path, encoding="utf-8"))["telegram_support_commands"]
    return next(c for c in commands if c["command_text"] == "spbot_lockdown")


def _argv(**answers):
    values = {"python": "py", "config_path": "config.json", "user_id": "42", "text": "", "confirm": "", **answers}
    return build_cli_argv(_command()["action_config"], values)


@pytest.mark.parametrize("path", COMMAND_FILES)
def test_the_command_is_private_clearance_50_and_the_same_everywhere(path):
    command = _command(path)
    assert (command["is_private"], command["is_group"], command["command_type"]) == (1, 0, 50)
    assert command == _command()


def test_status_asks_nothing_more_and_reads_only():
    assert _argv(action="status") == ["py", "-m", "dbabrain.control.cli", "lockdown", "status",
                                      "--config", "config.json", "--by", "telegram:42"]
    steps = _command()["action_config"]["parameters"]
    assert [s["name"] for s in steps if ws.ask_when_holds(s, {"action": "status"})] == ["action"]


def test_on_carries_the_reason_the_yes_and_who_typed_it():
    argv = _argv(action="on", text="read-me3.txt seen on the backup share", confirm="yes")
    assert argv[-4:] == ["--reason", "read-me3.txt seen on the backup share", "--confirm", "yes"]
    assert "telegram:42" in argv and "--note" not in argv


def test_off_carries_a_note_not_a_reason():
    argv = _argv(action="off", text="hosts rebuilt and scanned clean", confirm="yes")
    assert argv[-4:] == ["--note", "hosts rebuilt and scanned clean", "--confirm", "yes"]
    assert "--reason" not in argv


def test_on_and_off_ask_why_and_yes():
    steps = _command()["action_config"]["parameters"]
    for action in ("on", "off"):
        assert [s["name"] for s in steps if ws.ask_when_holds(s, {"action": action})] == ["action", "text", "confirm"]
