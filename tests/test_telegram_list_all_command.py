"""`/spbot_list_all_command` must describe the bot from the bot's own config, never from prose.

The point of the command is that adding a command to `telegram_support_commands.json` is the only
step: the listing picks it up with its real arguments and clearance, and nobody has to remember to
write a sentence somewhere else. Two hand-maintained listings already exist for these commands
(the Markdown table and the BotFather block) and `tests/test_listing.py` records that they drifted
twice before anyone noticed — this one cannot drift, and these tests are what holds that.

It also must not advertise what the caller will be refused. Offering an id that cannot work is the
failure `db_ops.lib.listing` exists to prevent, and it is worse here than in the other listings:
this is the command someone types precisely because they do not yet know what they may run.
"""

from __future__ import annotations

import json

import pytest

from db_ops.telegram import command_processor
from db_ops.telegram.command_processor import (
    SupportCommand,
    _command_parameter_summary,
    execute_list_all_command_command,
)


class _Row(dict):
    def keys(self):
        return dict.keys(self)


class _Store:
    def __init__(self):
        self.messages = []

    def insert_telegram_send_message(self, **kwargs):
        self.messages.append(kwargs)


def _command():
    return SupportCommand(
        command_id=22, command_text="spbot_list_all_command", command_type=0, reply_default=1,
        reply_text="{result_listing}", is_group=1, is_private=1, need_file=0,
        action_type="list_all_command", action_config={},
    )


def _write_config(tmp_path, entries, *, users=None, groups=None):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "telegram_support_commands.json").write_text(
        json.dumps({"telegram_support_commands": entries}), encoding="utf-8")
    (data_dir / "telegram_users.json").write_text(
        json.dumps({"telegram_users": users if users is not None
                    else [{"user_id": "7", "user_type": 100}]}), encoding="utf-8")
    (data_dir / "telegram_groups.json").write_text(
        json.dumps({"telegram_groups": groups or []}), encoding="utf-8")
    return data_dir


def _entry(text, **overrides):
    entry = {
        "command_text": text, "command_type": 0, "is_private": 1, "is_group": 1,
        "action_type": "noop", "action_config": {},
    }
    entry.update(overrides)
    return entry


def _run(tmp_path, monkeypatch, entries, *, user_id="7", chat_type="private", users=None):
    _write_config(tmp_path, entries, users=users)
    monkeypatch.setattr(command_processor, "TOOL_ROOT", tmp_path)
    row = _Row(chat_id="7", message_id=1, chat_type=chat_type, user_id=user_id)
    return execute_list_all_command_command(
        store=_Store(), row=row, command=_command(), source_id="1")


def test_a_command_added_to_the_config_appears_with_no_other_edit(tmp_path, monkeypatch):
    """The whole reason the command exists."""
    out = _run(tmp_path, monkeypatch, [_entry("spbot_status"), _entry("spbot_brand_new")])
    assert "/spbot_brand_new" in out["listing"]
    assert out["command_count"] == 2


def test_the_arguments_come_from_the_commands_own_parameters(tmp_path, monkeypatch):
    entries = [_entry("spbot_x", action_config={"parameters": [
        {"name": "target", "position": 1, "required": True},
        {"name": "format", "position": 2, "required": True},
        {"name": "sql_text", "position": 3, "required": True, "consume_rest": True},
    ]})]
    out = _run(tmp_path, monkeypatch, entries)
    assert "/spbot_x <target> <format> <sql_text...>" in out["listing"]


def test_an_optional_argument_reads_as_optional(tmp_path, monkeypatch):
    entries = [_entry("spbot_x", action_config={"parameters": [
        {"name": "windowed", "position": 1, "required": False},
        {"name": "target", "position": 2, "required": True, "consume_rest": True},
    ]})]
    assert "/spbot_x [windowed] <target...>" in _run(tmp_path, monkeypatch, entries)["listing"]


def test_arguments_are_listed_in_the_order_they_are_typed_not_config_order(tmp_path, monkeypatch):
    """`consume_rest` swallows the rest of the message, so an argument printed after it would be
    one the operator can never supply."""
    entries = [_entry("spbot_x", action_config={"parameters": [
        {"name": "sql_text", "position": 3, "required": True, "consume_rest": True},
        {"name": "target", "position": 1, "required": True},
        {"name": "format", "position": 2, "required": True},
    ]})]
    assert "<target> <format> <sql_text...>" in _run(tmp_path, monkeypatch, entries)["listing"]


def test_a_command_above_the_callers_clearance_is_hidden_and_counted(tmp_path, monkeypatch):
    entries = [_entry("spbot_public"), _entry("spbot_admin", command_type=10)]
    out = _run(tmp_path, monkeypatch, entries, users=[{"user_id": "7", "user_type": 1}])
    assert "/spbot_admin" not in out["listing"]
    assert "above your clearance" in out["listing"]
    assert out["hidden_count"] == 1


def test_a_command_that_only_runs_in_a_group_is_hidden_from_a_private_chat(tmp_path, monkeypatch):
    entries = [_entry("spbot_here"), _entry("spbot_group_only", is_private=0, is_group=1)]
    out = _run(tmp_path, monkeypatch, entries, chat_type="private")
    assert "/spbot_group_only" not in out["listing"]
    assert "only run in a group" in out["listing"]


def test_a_disabled_command_is_hidden_and_named_as_disabled(tmp_path, monkeypatch):
    """A negative command_type is the off switch — the same rule the dispatcher applies, so the
    listing and the dispatcher cannot disagree about what runs."""
    out = _run(tmp_path, monkeypatch, [_entry("spbot_on"), _entry("spbot_off", command_type=-1)])
    assert "/spbot_off" not in out["listing"]
    assert "command_type < 0" in out["listing"]


def test_each_reason_for_hiding_is_counted_separately(tmp_path, monkeypatch):
    """"3 hidden" tells an operator nothing; "above your clearance" is something to act on."""
    entries = [
        _entry("spbot_ok"),
        _entry("spbot_admin", command_type=10),
        _entry("spbot_group_only", is_private=0),
        _entry("spbot_off", command_type=-1),
    ]
    out = _run(tmp_path, monkeypatch, entries, users=[{"user_id": "7", "user_type": 1}])
    listing = out["listing"]
    assert "above your clearance" in listing
    assert "only run in a group" in listing
    assert "command_type < 0" in listing
    assert out["hidden_count"] == 3


def test_the_commands_are_sorted_so_the_reply_is_stable(tmp_path, monkeypatch):
    entries = [_entry("spbot_zulu"), _entry("spbot_alpha"), _entry("spbot_mike")]
    listing = _run(tmp_path, monkeypatch, entries)["listing"]
    assert listing.index("spbot_alpha") < listing.index("spbot_mike") < listing.index("spbot_zulu")


def test_a_listing_too_long_for_one_message_is_attached_as_a_document(tmp_path, monkeypatch):
    """Telegram caps a message at 4096 characters; a listing that silently loses its tail is
    worse than one that arrives as a file."""
    entries = [_entry(f"spbot_command_number_{index:03d}") for index in range(400)]
    out = _run(tmp_path, monkeypatch, entries)
    assert out.get("file_path")
    assert "attached" in out["listing"]


def test_a_command_with_no_parameters_prints_no_argument_placeholder(tmp_path, monkeypatch):
    listing = _run(tmp_path, monkeypatch, [_entry("spbot_status")])["listing"]
    assert "/spbot_status\n" in listing + "\n"
    assert "<" not in listing.split("\n")[1]


def test_the_parameter_summary_survives_a_malformed_parameters_block():
    """Config is edited by hand and through the bot; a listing must not be the thing that breaks."""
    assert _command_parameter_summary({}) == ""
    assert _command_parameter_summary({"parameters": None}) == ""
    assert _command_parameter_summary({"parameters": ["not-a-dict"]}) == ""
    assert _command_parameter_summary({"parameters": [{"position": 1, "required": True}]}) == "<arg>"


@pytest.mark.parametrize("chat_type", ["private", "group"])
def test_the_real_config_lists_itself_without_raising(chat_type):
    """Against the shipped config, not a fixture: the command has to survive whatever is actually
    in telegram_support_commands.json."""
    row = _Row(chat_id="100000001", message_id=1, chat_type=chat_type, user_id="100000001")
    out = execute_list_all_command_command(
        store=_Store(), row=row, command=_command(), source_id="1")
    assert out["command_count"] >= 1
    assert "/spbot_list_all_command" in out["listing"]
