"""A chat runs a backup's repository pass, not only the backup (0.39.0, 1.347).

Asked of the assistant on 2026-11-13 how to run a FULL from start to end, the assistant gave the backup's
Telegram command and then had to say there was none for the rest: the copy to the repository, its
encryption and the release at the source ran only on the scheduled workflow's next pass, or at a
node's command line. The operator, the same day: *add `/spbot_offload`, so Telegram runs the whole
flow.*

What has to hold:

* the command runs exactly `backup_restore offload --backup-id <id>`, with `--dry-run` only when
  asked, and the line it builds is one the CLI's own parser takes;
* it declares **no completion probe** - a scheduled pass of the same entry writes the same
  `offload.end` row, and a probe that answered stops the task, so a hand pass copying a FULL would
  be killed by the next quarter hour's scheduled pass; the exit code is its outcome;
* the chat is answered with one sentence (`said`) - including the two cases that used to read as a
  plain success: an id with no repository, and a pass with nothing to do;
* the assistant's runbook offers it beside the CLI line and holds the repository pass to **its**
  level; a node without it keeps the old rule, the backup command's level.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_the_ai_assistant_says_how_to_run_a_backup_or_a_restore_by_hand import (
    ENVIRONMENTS, ESTATE, NOW, SHIPPED_COMMANDS, WORKER, _step)

from dbabrain.backup_restore import cli as backup_cli
from dbabrain.backup_restore.offload import said
from dbabrain.lib import backup_runbook as R
from dbabrain.telegram.command_cli import build_cli_argv, render_template
from dbabrain.telegram.commands import can_run_command

REPO_ROOT = Path(__file__).resolve().parents[1]
COPIES = ("dbabrain/telegram/catalogue/telegram_support_commands.json",
          "data/telegram_support_commands.example.json")


def _command(document: dict, name: str = "spbot_offload") -> dict:
    return next(r for r in document["telegram_support_commands"] if r["command_text"] == name)


def _argv(mode: str = "") -> list[str]:
    config = _command(SHIPPED_COMMANDS)["action_config"]
    values = {"python": "python", "config_path": "config.json", "backup_id": "ACME_APPDB_FULL", "mode": mode}
    return build_cli_argv(config, values)


@pytest.mark.parametrize("copy", COPIES)
def test_every_shipped_copy_carries_the_command_at_the_backup_commands_level(copy):
    document = json.loads((REPO_ROOT / copy).read_text(encoding="utf-8-sig"))
    offload, backup = _command(document), _command(document, "spbot_backup")
    assert offload["command_type"] == backup["command_type"] == 10
    assert offload["is_group"] == backup["is_group"] and offload["is_private"] == backup["is_private"]
    assert backup["sort_order"] < offload["sort_order"] < _command(document, "spbot_restore")["sort_order"]
    assert offload == _command(SHIPPED_COMMANDS), "the copies say the same"


def test_it_runs_the_offload_of_that_entry_and_the_cli_takes_the_line():
    argv = _argv()
    assert argv[2:] == ["dbabrain.backup_restore.cli", "offload", "--config", "config.json",
                        "--backup-id", "ACME_APPDB_FULL"]
    args = backup_cli.parse_args(argv[3:])
    assert args.command == "offload" and args.backup_id == "ACME_APPDB_FULL" and not args.dry_run
    assert args.limit is None, "a hand pass takes the entry's offload_files_per_pass (1.340)"


@pytest.mark.parametrize("mode, dry", [("dry-run", True), ("DRY-RUN", True), ("run", False), ("", False)])
def test_only_dry_run_adds_the_dry_run_flag(mode, dry):
    assert ("--dry-run" in _argv(mode)) is dry


def test_the_second_word_is_held_to_run_or_dry_run():
    mode = next(p for p in _command(SHIPPED_COMMANDS)["action_config"]["parameters"] if p["name"] == "mode")
    assert not mode["required"] and mode["allow_skip"]
    for word, ok in (("run", True), ("dry-run", True), ("full", False), ("--dry-run", False)):
        assert bool(re.fullmatch(mode["pattern"], word)) is ok, word


def test_no_completion_probe_so_a_scheduled_pass_cannot_stop_a_hand_one():
    config = _command(SHIPPED_COMMANDS)["action_config"]
    assert "completion_probe" not in config and "success_output_contains" not in config
    assert config["background"] is True and config["detached"] is True
    assert config["timeout_seconds"] >= 6 * 3600, "a FULL's copy and encryption take hours"


def test_the_chat_is_answered_in_one_sentence_for_each_way_a_pass_ends():
    config = _command(SHIPPED_COMMANDS)["action_config"]
    cases = {
        "no repository": ({"entries": 0, "jobs": []}, False, "no backup entry ACME_X with offload_enabled"),
        "nothing to do": ({"entries": 1, "jobs": [], "succeeded": 0, "failed": 0}, False, "nothing to do"),
        "dry run": ({"entries": 1, "jobs": [{"backup_id": "ACME_X", "to_copy": 3, "to_encrypt": 3,
                                              "to_release": 2, "to_expire": 0}]}, True,
                    "ACME_X: 3 to copy, 3 to encrypt, 2 to release at the source, 0 to expire - nothing done"),
        "ran": ({"entries": 1, "jobs": [{"backup_id": "ACME_X"}], "succeeded": 1, "failed": 0}, False,
                "1 done, 0 failed"),
    }
    for name, (summary, dry_run, expected) in cases.items():
        sentence = said(summary, backup_id="ACME_X", dry_run=dry_run)
        assert expected in sentence, name
        text = render_template(config["success_text"], {"backup_id": "ACME_X", "said": sentence})
        assert "{" not in text and sentence in text, name
    # A process killed before it printed anything has no `said`: the failure text does not need it.
    assert "{said}" not in config["failure_text"]


def _runbook(commands: dict, backup_id: str = "ACME_APPDB_FULL") -> dict:
    return R.build(ESTATE, backup_id=backup_id, node=WORKER, twins=R.bot_twins(commands),
                   environments=ENVIRONMENTS, now=NOW)["backups"][0]


def test_the_runbook_offers_it_beside_the_cli_line():
    block = _runbook(SHIPPED_COMMANDS)["telegram_offload"]
    assert block["available"] and block["line"] == "/spbot_offload ACME_APPDB_FULL" and block["level"] == 10
    assert block["runs"].startswith("offload --backup-id ACME_APPDB_FULL")
    assert "dry-run" in block["note"]
    step = _step(_runbook(SHIPPED_COMMANDS), "Run the repository pass now")
    assert step["gate"] == R.GATE_OFFLOAD
    assert "offload_files_per_pass (" in step["note"] and "100 files of the entry" not in step["note"]


def test_an_entry_with_no_repository_is_told_there_is_no_pass_to_run():
    entry = next(e for e in ESTATE["backup_restore"]["backups"] if e.get("offload_enabled") is False)
    block = _runbook(SHIPPED_COMMANDS, entry["backup_id"])["telegram_offload"]
    assert block["available"] is False and block["why"]


def test_the_repository_pass_is_held_to_the_offload_commands_own_level():
    raised = json.loads(json.dumps(SHIPPED_COMMANDS))
    _command(raised)["command_type"] = 50
    answer = R.build(ESTATE, backup_id="ACME_APPDB_FULL", node=WORKER, twins=R.bot_twins(raised),
                     environments=ENVIRONMENTS, now=NOW)
    R.for_asker(answer, asker_level=10, may_run=lambda level: can_run_command(
        allow_command=10, user_type=10, command_type=level))
    runbook = answer["backups"][0]
    assert "cli" in _step(runbook, "Run job full now"), "the backup is still level 10's"
    held = _step(runbook, "Run the repository pass now")
    assert "cli" not in held and "takes Telegram level 50" in held["withheld"]
    assert runbook["telegram_offload"]["asker_may_run"] is False and "line" not in runbook["telegram_offload"]
    assert "cli" in _step(runbook, "Dry run of the repository pass"), "a dry run stays everyone's"


def test_a_node_without_the_command_holds_the_pass_to_the_backup_commands_level():
    older = json.loads(json.dumps(SHIPPED_COMMANDS))
    older["telegram_support_commands"] = [r for r in older["telegram_support_commands"]
                                          if r["command_text"] != "spbot_offload"]
    runbook = _runbook(older)
    assert runbook["telegram_offload"]["available"] is False
    assert "scheduled workflow runs this pass" in runbook["telegram_offload"]["why"]
    assert _step(runbook, "Run the repository pass now")["gate"] == R.GATE_BACKUP


@pytest.mark.parametrize("name", ["spbot_restore", "spbot_copy_backup"])
def test_a_restore_from_a_chat_is_not_stopped_at_two_hours(name):
    """0.39.0 (1.357): the bot stopped `/spbot_restore` at 7200 s and killed its process tree - the
    store's 65 GB drill spent 47 minutes in its consistency check alone. A restore is held to its
    entry's own `time_window.timeout`; the bot's ceiling is a day."""
    for copy in COPIES:
        document = json.loads((REPO_ROOT / copy).read_text(encoding="utf-8-sig"))
        assert _command(document, name)["action_config"]["timeout_seconds"] >= 24 * 3600, copy
