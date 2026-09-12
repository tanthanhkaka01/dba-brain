"""Who a bot token belongs to, and whether the bot can actually read a group.

`data/bot_telegram.json` asks a new install for `telegram_bot_id` and `telegram_bot_username`, and
until 2026-09-10 there was no command that could answer either: standing a node up from the
scaffold meant calling `getMe` by hand with the raw token, which is both a chore and a good way to
paste a token somewhere it should not go.

The second fact this reports is the one that costs a day later. **Telegram bots default to privacy
mode ON**, and a bot in a group then sees only slash-commands aimed at it and replies to its own
messages. That is enough for `/spbot_*` to work, so the group looks correctly configured — and
anything that reads ordinary chat silently gets nothing. Measured the same day: this estate's own
bot has privacy off and the newly created one had it on, which is exactly the pair that makes the
difference invisible when you copy a working setup.
"""

from __future__ import annotations

from db_ops.telegram import api


def _fake_get_me(monkeypatch, result: dict, ok: bool = True):
    def fake_call(*, bot_token, method_name, payload, api_url, timeout_seconds):
        assert method_name == "getMe"
        assert payload == {}
        return {"ok": ok, "result": result}

    monkeypatch.setattr(api, "call_telegram_api", fake_call)


def test_it_answers_the_two_values_the_scaffold_asks_for(monkeypatch):
    _fake_get_me(monkeypatch, {"id": 123456, "username": "some_bot",
                               "can_join_groups": True, "can_read_all_group_messages": True})
    answer = api.bot_info(bot_token="x")
    assert answer["telegram_bot_id"] == "123456"
    assert answer["telegram_bot_username"] == "some_bot"
    # Ready to paste: the same two keys, spelled the way bot_telegram.json spells them.
    assert answer["bot_telegram_json"] == {
        "telegram_bot_id": "123456", "telegram_bot_username": "some_bot"}


def test_privacy_mode_on_is_reported_as_a_limit_rather_than_left_to_be_discovered(monkeypatch):
    _fake_get_me(monkeypatch, {"id": 1, "username": "b", "can_read_all_group_messages": False})
    answer = api.bot_info(bot_token="x")
    assert answer["privacy_mode"] == "on"
    # The note has to say what still works, or a reader concludes the bot is broken when it is not.
    assert "commands addressed to it" in answer["note"]
    assert "setprivacy" in answer["note"]


def test_privacy_mode_off_says_so_plainly(monkeypatch):
    _fake_get_me(monkeypatch, {"id": 1, "username": "b", "can_read_all_group_messages": True})
    answer = api.bot_info(bot_token="x")
    assert answer["privacy_mode"] == "off"
    assert "every message" in answer["note"]


def test_a_missing_field_becomes_an_empty_string_rather_than_the_word_none(monkeypatch):
    # These values get pasted into a JSON file. "None" in `telegram_bot_id` is a config that looks
    # filled in and is not.
    _fake_get_me(monkeypatch, {})
    answer = api.bot_info(bot_token="x")
    assert answer["telegram_bot_id"] == ""
    assert answer["telegram_bot_username"] == ""
    assert answer["privacy_mode"] == "on"


def test_the_id_is_a_string_because_that_is_what_the_config_file_holds(monkeypatch):
    _fake_get_me(monkeypatch, {"id": 8629909482, "username": "b"})
    answer = api.bot_info(bot_token="x")
    assert answer["telegram_bot_id"] == "8629909482"
    assert isinstance(answer["telegram_bot_id"], str)


def test_the_command_is_registered_and_reaches_the_function():
    from db_ops.telegram import cli

    args = cli.parse_args(["bot-info"])
    assert args.command == "bot-info"
    assert args.telegram_function is api.bot_info
