"""The bot's menu is set from the node's own commands file, not pasted by hand (0.39.0, 1.348).

Read on 2026-11-13: the menu Telegram showed under "/" had 29 commands while the node ran 31 -
`/spbot_copy_backup` (0.34.0) and `/spbot_lockdown` (0.35.0) had never been pasted into BotFather,
so nobody was offered them. `telegram set-menu` reads the live menu and sets the node's.

Its source was first the block of `data/telegram_support_commands.md` limited to the node's
commands - a second file to keep in step by hand. The operator, 2026-11-14 (translated): *set-menu
should take it straight from telegram_support_commands.json, not from the md file.* Each command now
states its own line (`menu_description`); the menu is those lines in `sort_order`, for the commands
the node may run. The md block stays as the shipped commands' description, held to the json here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import shipped_config

from dbabrain.common import config_upgrade
from dbabrain.lib import bot_menu, errors
from dbabrain.telegram import menu as M

REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDS = [{"command_text": "spbot_status", "command_type": 0, "sort_order": 1, "menu_description": "Get bot status"},
           {"command_text": "spbot_offload", "command_type": 10, "sort_order": 3,
            "menu_description": "Run one entry's repository pass"},
           {"command_text": "spbot_backup", "command_type": 10, "sort_order": 2, "menu_description": "Run one backup"},
           {"command_text": "spbot_secret_tool", "command_type": 50, "sort_order": 4}]


def _shipped() -> list[dict]:
    return json.loads((REPO_ROOT / "dbabrain" / "telegram" / "catalogue" / "telegram_support_commands.json")
                      .read_text(encoding="utf-8"))["telegram_support_commands"]


# --------------------------------------------------------------------------- #
# The menu the commands make
# --------------------------------------------------------------------------- #
def test_the_menu_is_each_commands_own_line_in_sort_order():
    node = bot_menu.menu_from_commands(RECORDS)

    assert node["menu"] == [{"command": "spbot_status", "description": "Get bot status"},
                            {"command": "spbot_backup", "description": "Run one backup"},
                            {"command": "spbot_offload", "description": "Run one entry's repository pass"}]
    assert node["not_described"] == ["spbot_secret_tool"], "named, never given an invented line"


def test_a_command_switched_off_on_this_node_is_not_offered():
    records = [dict(r) for r in RECORDS]
    records[2]["command_type"] = -1  # spbot_backup

    node = bot_menu.menu_from_commands(records)

    assert "spbot_backup" not in [e["command"] for e in node["menu"]]
    assert node["switched_off_here"] == ["spbot_backup"]


@pytest.mark.parametrize("record, why", [
    ({"command_text": "SPBOT_UPPER", "menu_description": "shouted"}, "1-32 of a-z"),
    ({"command_text": "spbot-dash", "menu_description": "a dash"}, "1-32 of a-z"),
    ({"command_text": "spbot_long", "menu_description": "x" * 257}, "1-256 characters"),
])
def test_a_line_telegram_would_refuse_is_named_by_why(record, why):
    assert why in bot_menu.menu_from_commands([record])["refused"][0]


def test_every_shipped_command_states_its_line_and_the_md_block_says_the_same():
    """The shipped catalogue offers every command it ships; the md document's block, kept for a reader
    and for BotFather by hand, is held to the json - the json is the source."""
    shipped = _shipped()
    node = bot_menu.menu_from_commands(shipped)
    assert node["not_described"] == [] and node["refused"] == []
    block = {e["command"]: e["description"] for e in M.menu_block(
        (REPO_ROOT / "data" / "telegram_support_commands.md").read_text(encoding="utf-8"))}
    assert {e["command"]: e["description"] for e in node["menu"]} == {
        name: line for name, line in block.items() if name in {e["command"] for e in node["menu"]}}


def test_the_nodes_own_commands_file_states_every_line_too():
    # The node's own file where this checkout has one, else the example a fresh install starts from.
    records = json.loads(shipped_config("telegram_support_commands.json")
                         .read_text(encoding="utf-8"))["telegram_support_commands"]
    assert bot_menu.menu_from_commands(records)["not_described"] == []


# --------------------------------------------------------------------------- #
# set-menu
# --------------------------------------------------------------------------- #
def _commands(tmp_path: Path, records=None) -> Path:
    path = tmp_path / "telegram_support_commands.json"
    path.write_text(json.dumps({"telegram_support_commands": RECORDS if records is None else records}),
                    encoding="utf-8")
    return path


def _api(monkeypatch, live: list[dict]) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def fake(*, bot_token, method_name, payload, api_url, timeout_seconds):
        calls.append((method_name, dict(payload)))
        return {"ok": True, "result": live if method_name == "getMyCommands" else True}

    monkeypatch.setattr(M, "call_telegram_api", fake)
    return calls


def test_a_menu_that_differs_is_set_from_the_json_and_says_what_changed(tmp_path, monkeypatch):
    calls = _api(monkeypatch, [{"command": "spbot_status", "description": "Get bot status"}])

    result = M.set_menu(bot_token="t", commands_path=_commands(tmp_path))

    assert result["set"] is True and result["added"] == ["spbot_backup", "spbot_offload"]
    assert result["not_described"] == ["spbot_secret_tool"]
    method, payload = calls[-1]
    assert method == "setMyCommands"
    assert [c["command"] for c in json.loads(payload["commands"])] == ["spbot_status", "spbot_backup", "spbot_offload"]


def test_no_md_document_is_read(tmp_path, monkeypatch):
    """A node with no `telegram_support_commands.md` (the worker, 2026-11-14) sets its menu all the same."""
    _api(monkeypatch, [])

    assert M.set_menu(bot_token="t", commands_path=_commands(tmp_path), dry_run=True)["menu_commands"] == 3
    assert not (tmp_path / "telegram_support_commands.md").exists()


def test_a_dry_run_reads_and_sets_nothing(tmp_path, monkeypatch):
    calls = _api(monkeypatch, [])

    result = M.set_menu(bot_token="t", commands_path=_commands(tmp_path), dry_run=True)

    assert [m for m, _ in calls] == ["getMyCommands"] and result["set"] is False
    assert result["message"].startswith("dry run: the menu would be set")


def test_a_menu_already_the_nodes_is_left_alone(tmp_path, monkeypatch):
    calls = _api(monkeypatch, bot_menu.menu_from_commands(RECORDS)["menu"])

    result = M.set_menu(bot_token="t", commands_path=_commands(tmp_path))

    assert [m for m, _ in calls] == ["getMyCommands"] and result["set"] is False
    assert result["message"] == "the menu is already this node's"


def test_a_file_whose_commands_state_no_line_is_refused_never_set_to_an_empty_menu(tmp_path, monkeypatch):
    calls = _api(monkeypatch, [{"command": "spbot_status", "description": "Get bot status"}])
    bare = [{k: v for k, v in r.items() if k != "menu_description"} for r in RECORDS]

    with pytest.raises(errors.NotConfigured, match="upgrade-config \\(step menu-descriptions\\)"):
        M.set_menu(bot_token="t", commands_path=_commands(tmp_path, bare))
    assert calls == []


def test_a_line_telegram_would_refuse_stops_the_set(tmp_path, monkeypatch):
    _api(monkeypatch, [])

    with pytest.raises(errors.InvalidConfig, match="menu lines Telegram would refuse"):
        M.set_menu(bot_token="t", commands_path=_commands(
            tmp_path, RECORDS + [{"command_text": "BAD-NAME", "command_type": 0, "menu_description": "x"}]))


# --------------------------------------------------------------------------- #
# The upgrade writes the shipped lines into a node's file
# --------------------------------------------------------------------------- #
def test_upgrade_config_gives_a_command_with_no_line_the_shipped_one_and_leaves_what_is_stated(tmp_path):
    shipped = _shipped()
    status = next(r for r in shipped if r["command_text"] == "spbot_status")
    node = [{k: v for k, v in status.items() if k != "menu_description"},
            {"command_text": "spbot_backup", "command_type": 10, "menu_description": "Our own words"},
            {"command_text": "spbot_local_only", "command_type": 0}]
    (tmp_path / "telegram_support_commands.json").write_text(
        json.dumps({"telegram_support_commands": node}, indent=4), encoding="utf-8")

    _, plan = config_upgrade.upgrade({"data_dir": str(tmp_path), "steps": ["menu-descriptions"]})
    assert plan["records_changed"] == 1
    _, done = config_upgrade.upgrade({"data_dir": str(tmp_path), "steps": ["menu-descriptions"], "dry_run": False})

    written = json.loads((tmp_path / "telegram_support_commands.json").read_text(encoding="utf-8"))
    lines = [r.get("menu_description") for r in written["telegram_support_commands"]]
    assert lines == [status["menu_description"], "Our own words", None], \
        "the shipped line where none was stated; a node's own words kept; a command the catalogue lacks left"
    _, again = config_upgrade.upgrade({"data_dir": str(tmp_path), "steps": ["menu-descriptions"]})
    assert again["records_changed"] == 0, "run twice, it changes nothing"
