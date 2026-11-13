"""The assistant's answer is the asker's level: a command that changes something is handed only to
a user whose Telegram level may run it (0.38.0, 1.328).

The operator's rule for Phanes' runbook: *a command that changes something - a forced backup on
production by day, a restore, a delete - is given with what it touches and the confirmation it asks;
a user whose Telegram level may not run it is told so, not handed the line.* The bot already holds
its commands to ``telegram_users.json`` levels (``commands.can_run_command``); an assistant that
dictates the same command's CLI line to anyone who asks would be a second door around that rule.

So the level is held where the line is made, not in the prompt:

* the level a step takes is the ``command_type`` of the bot command that does the same thing **on
  this node** - found by what it runs, so a node that raised, renamed or switched off its command
  is held to its own value;
* an asker below it gets the step **without** its lines - the CLI line, the master's form and the
  Telegram line - with why, what it would touch, and the level it takes;
* a dry run, a check and a listing change nothing and stay everyone's;
* the node's operator at its own command line (``telegram ai-ask``) holds the passphrase already
  and has no Telegram level: every line;
* a node with no bot command for a step has no level to hold it to, and gives it at the command
  line only - the strictest reading, as for an operation its configuration forgot.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.test_the_ai_assistant_answers_in_its_chat_and_keeps_everything import (
    BOT, BOT_ID, CHAT, NOW as ASKED_AT, OPERATOR, _message, _rows)
from tests.test_the_ai_assistant_says_how_to_run_a_backup_or_a_restore_by_hand import (
    ENVIRONMENTS, ESTATE, SHIPPED_COMMANDS, STORE, WORKER, _context, _step)

from dbabrain.db.store import DbaBrainStore
from dbabrain.lib import ai_assist as A
from dbabrain.lib import backup_runbook as R
from dbabrain.telegram import ai_assist as job
from dbabrain.telegram import ai_tools as T
from dbabrain.telegram.commands import can_run_command

#: Every key that hands a step over. A withheld step carries none of them.
LINES = ("cli", "from_master")


def _answer(tmp_path, *, level: int = 100, operator: bool = False, **asked) -> dict:
    return json.loads(T.run_tool(_context(tmp_path, level=level, operator=operator), "backup_runbook", asked).content)


def _changing(runbook: dict) -> list[dict]:
    return [step for step in runbook["steps"] if step["changes"]]


def _reading(runbook: dict) -> list[dict]:
    return [step for step in runbook["steps"] if not step["changes"]]


@pytest.mark.parametrize("asked", [{"backup_id": "ACME_STORE_PG"}, {"restore_id": "ACME_STORE_TO_LAB"},
                                   {"restore_id": "ACME_APPDB_TO_DRILL"}])
def test_a_user_below_the_commands_level_is_told_so_and_not_handed_the_line(tmp_path, asked):
    """The shipped backup and restore commands take level 10; this asker is level 1."""
    answer = _answer(tmp_path, level=1, **asked)
    runbook = (answer["backups"] or answer["restores"])[0]
    assert answer["asker"]["telegram_level"] == 1 and answer["asker"]["withheld_steps"] == len(_changing(runbook))
    for step in _changing(runbook):
        assert not any(key in step for key in LINES), step["step"]
        assert "takes Telegram level 10" in step["withheld"] and "the asker's level is 1" in step["withheld"]
        assert step["touches"], "what it would change is still said"
        assert step["telegram_level_needed"] == 10
    for step in _reading(runbook):
        assert "cli" in step or "cli_not_written" in step, f"{step['step']} changes nothing: it stays"
    blocks = [runbook["telegram"]] if runbook["kind"] == "backup" else list(runbook["telegram"].values())
    for block in blocks:
        assert block["asker_may_run"] is False and "line" not in block and "runs" not in block
        assert block["level"] == 10 and block["command"].startswith("/spbot_"), "which command, and its level"
    # Not one changing command line is anywhere in what the model was given.
    text = json.dumps(answer)
    assert " --force\"" not in text and "--copy-only" not in text and "backup-stop --config" not in text
    assert "LATEST" not in text


@pytest.mark.parametrize("asked", [{"backup_id": "ACME_STORE_PG"}, {"restore_id": "ACME_STORE_TO_LAB"}])
def test_a_user_at_the_commands_level_is_handed_it_with_what_it_touches(tmp_path, asked):
    answer = _answer(tmp_path, level=10, **asked)
    runbook = (answer["backups"] or answer["restores"])[0]
    assert "withheld_steps" not in answer["asker"]
    for step in _changing(runbook):
        assert "withheld" not in step and ("cli" in step or "cli_not_written" in step)
        assert step["touches"] and step["confirmation"].startswith("none"), "given with what it touches"
        assert step["telegram_level_needed"] == 10
    blocks = [runbook["telegram"]] if runbook["kind"] == "backup" else list(runbook["telegram"].values())
    assert all(block["asker_may_run"] is True and block["line"].startswith("/spbot_") for block in blocks)


def test_the_level_compared_is_the_bots_own_rule():
    """One comparison for the bot and the assistant: what `can_run_command` refuses, `for_asker`
    withholds - a command switched off (-1) from everyone, a public one (0) from nobody."""
    for needed, user in ((10, 9), (10, 10), (50, 10), (100, 100), (0, 0), (-1, 100)):
        commands = json.loads(json.dumps(SHIPPED_COMMANDS))
        for record in commands["telegram_support_commands"]:
            if record["command_text"] == "spbot_backup":
                record["command_type"] = needed
        answer = R.build(ESTATE, backup_id="ACME_APPDB_FULL", node=WORKER, twins=R.bot_twins(commands),
                         environments=ENVIRONMENTS)
        R.for_asker(answer, asker_level=user, may_run=lambda level, user=user: can_run_command(
            allow_command=user, user_type=user, command_type=level))
        allowed = can_run_command(allow_command=user, user_type=user, command_type=needed)
        step = _step(answer["backups"][0], "Run job full now")
        assert ("cli" in step) is allowed and ("withheld" in step) is (not allowed), (needed, user)


def test_a_command_switched_off_on_this_node_is_given_in_no_chat(tmp_path):
    context = _context(tmp_path, level=100)
    commands = json.loads(json.dumps(SHIPPED_COMMANDS))
    for record in commands["telegram_support_commands"]:
        if record["command_text"] == "spbot_restore":
            record["command_type"] = -1
    context.commands_path.write_text(json.dumps(commands), encoding="utf-8")
    runbook = json.loads(T.run_tool(context, "backup_runbook", {"restore_id": "ACME_STORE_TO_LAB"}).content)["restores"][0]
    step = _step(runbook, "Restore to the newest backup")
    assert "cli" not in step and "switched off on this node" in step["withheld"]
    assert "ai-ask" in step["withheld"], "where it is still given: the node's own command line"
    assert runbook["telegram"]["restore"]["switched_off"]
    # Copy only is its own command with its own level, still 10 here: a level-100 user keeps it.
    assert "cli" in _step(runbook, "Copy only - nothing restored")


def test_a_node_with_no_bot_command_for_it_gives_it_at_the_command_line_only(tmp_path):
    context = _context(tmp_path, level=100)
    context.commands_path.write_text(json.dumps({"telegram_support_commands": []}), encoding="utf-8")
    runbook = json.loads(T.run_tool(context, "backup_runbook", {"backup_id": "ACME_APPDB_FULL"}).content)["backups"][0]
    assert runbook["telegram"] == {"available": False,
                                   "why": "this node's telegram_support_commands.json has no command that runs it"}
    step = _step(runbook, "Run job full now")
    assert "cli" not in step and "no Telegram level says who may run it" in step["withheld"]
    assert "cli" in _step(runbook, "Dry run of job full")
    operator = _context(tmp_path, operator=True)
    operator.commands_path.write_text(json.dumps({"telegram_support_commands": []}), encoding="utf-8")
    at_the_node = json.loads(T.run_tool(operator, "backup_runbook", {"backup_id": "ACME_APPDB_FULL"}).content)
    assert "cli" in _step(at_the_node["backups"][0], "Run job full now")


def test_the_nodes_operator_at_its_command_line_is_handed_every_line(tmp_path):
    """`telegram ai-ask`: whoever runs the node's CLI holds its passphrase already."""
    answer = _answer(tmp_path, level=0, operator=True, backup_id="ACME_STORE_PG")
    assert answer["asker"] == {"is": "the node's operator, at its command line", "handed": "every line"}
    runbook = answer["backups"][0]
    assert all("cli" in step and "withheld" not in step for step in runbook["steps"])
    assert runbook["telegram"]["line"] == "/spbot_backup ACME_STORE_PG -"
    assert _step(runbook, "Run job database_full now")["telegram_level_needed"] == 10, "the level is still said"


def test_a_user_the_bot_does_not_know_has_no_level_to_run_anything_by(tmp_path):
    context = _context(tmp_path, level=100)
    context.asker_user_id = "424242"  # active for the assistant, absent from telegram_users.json
    answer = json.loads(T.run_tool(context, "backup_runbook", {"backup_id": "ACME_APPDB_FULL"}).content)
    assert answer["asker"]["telegram_level"] == 0
    assert all("withheld" in step for step in _changing(answer["backups"][0]))


def test_the_listing_of_ids_holds_nothing_back_because_it_hands_nothing_over(tmp_path):
    assert "asker" not in _answer(tmp_path, level=0)
    assert len(_answer(tmp_path, level=0)["backups"]) == 5


# --------------------------------------------------------------------------- #
# End to end: what reaches the model, and what is kept
# --------------------------------------------------------------------------- #
class AsksForTheRunbook:
    """A stand-in model: asks for the store's runbook, then answers."""

    def __init__(self):
        self.calls = []

    def __call__(self, params):
        self.calls.append(params)
        last = params["messages"][-1]["content"]
        if not any(block.get("type") == "tool_result" for block in last):
            return {"model": "claude-sonnet-5-5", "stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "t1", "name": "backup_runbook", "input": {"backup_id": "ACME_STORE_PG"}}],
                "usage": {"input_tokens": 100, "output_tokens": 10}}
        return {"model": "claude-sonnet-5-5", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Here is how."}], "usage": {"input_tokens": 100, "output_tokens": 10}}


def _asked_in_the_chat(tmp_path, *, level: int) -> tuple[dict, AsksForTheRunbook]:
    data = tmp_path / "data"
    data.mkdir()
    document = {**A.shipped_document(), "enabled": True, "api_key_ref": "KEY", "cooldown_seconds": 0,
                "require_mention": False, "max_message_age_seconds": 10 ** 9}
    document["users"] = [A.shipped_user(OPERATOR, "Op", active=True)]
    (data / "ai_assist.json").write_text(json.dumps(document), encoding="utf-8")
    (data / "telegram_users.json").write_text(json.dumps({"telegram_users": [
        {"user_id": OPERATOR, "user_type": level, "active": True}]}), encoding="utf-8")
    (data / "telegram_support_commands.json").write_text(json.dumps(SHIPPED_COMMANDS), encoding="utf-8")
    (data / "restore_config.json").write_text(json.dumps(ESTATE), encoding="utf-8")
    (data / "db_instances.json").write_text(json.dumps({"db_instances": [
        {"server_id": STORE, "environment": "production"}]}), encoding="utf-8")
    DbaBrainStore(str(tmp_path / "store.sqlite")).initialize()
    _message(tmp_path, 1, OPERATOR, "How do I run the store's full backup by hand?")
    settings, missing = job.load_settings(data)
    assert settings is not None, missing
    config = SimpleNamespace(store=str(tmp_path / "store.sqlite"), timezone="+07:00", node_role="worker",
                             telegram=SimpleNamespace(resolved_bot_token="", api_url="", telegram_bot_id=BOT_ID,
                                                      telegram_bot_username=BOT, level_chat_map={"ai_assist": CHAT}))
    model = AsksForTheRunbook()
    job.Runner(config=config, config_path=tmp_path / "config.json", settings=settings, api_key="k",
               secrets={"KEY": "sk-ant-secret-value"}, call=model, now=ASKED_AT).run()
    given = next(block["content"] for block in model.calls[1]["messages"][-1]["content"]
                 if block.get("type") == "tool_result")
    return json.loads(given), model


def test_asked_in_the_chat_the_runbook_reaches_the_model_at_the_askers_level(tmp_path):
    low, model = _asked_in_the_chat(tmp_path, level=1)
    assert "backup_runbook" in [tool["name"] for tool in model.calls[0]["tools"]]
    assert low["asker"]["telegram_level"] == 1
    run = _step(low["backups"][0], "Run job database_full now")
    assert "cli" not in run and "takes Telegram level 10" in run["withheld"]
    assert "cli" in _step(low["backups"][0], "Dry run of job database_full")
    # The job's own clock decides whether the window is open: the node's configured timezone.
    assert low["backups"][0]["jobs"][0]["now"].endswith(" +07")
    # What the tool gave is kept as it was given - the same redacted, level-held text.
    kept = _rows(tmp_path, "SELECT tool_name, output_text, is_error FROM ai_tool_runs")[0]
    assert kept["tool_name"] == "backup_runbook" and kept["is_error"] == 0
    assert json.loads(kept["output_text"])["asker"]["withheld_steps"] >= 1


def test_asked_in_the_chat_a_user_at_the_level_gets_the_lines(tmp_path):
    high, _model = _asked_in_the_chat(tmp_path, level=100)
    run = _step(high["backups"][0], "Run job database_full now")
    # Written for the node the test runs on - its own install - so only the command's own half is held.
    assert run["cli"].endswith("backup --config config.json --backup-id ACME_STORE_PG --job database_full --force")
    assert "is a production instance in the inventory" in run["touches"]
    with sqlite3.connect(tmp_path / "store.sqlite") as conn:
        footer = conn.execute("SELECT message_text FROM telegram_send_messages ORDER BY send_tlgmsg_id DESC").fetchone()[0]
    assert "backup_runbook" in footer, "the cost footer names the tool it used"
