"""A node must be able to say which Telegram bot it is, and be right about it.

`data/bot_telegram.json` is catalogued configuration, so it travels inside a config bundle:
`import-data` faithfully points a machine that has never run at the bot the bundle came from — in
practice the estate's own. `store_config.json` has exactly that shape and got `db use-store` in
v0.14.0; the bot file never got the matching command, and the instruction lived in one cycle's
single-use run sheet rather than in the standing procedure.

Measured 2026-09-14 standing a 0.17.0 node up by hand: it came up on the production bot and on all
twelve production groups, and nothing said so. Starting a daemon there is two pollers on one token,
which the 0.16.0 cycle measured at 4,735 calls refused with HTTP 409.

Three more things were wrong underneath, and the third is the dangerous one:

* `init` writes a `telegram_config.json` naming `data/bot_telegram.json` — a file `init` does not
  create;
* it also pre-filled `telegram_bot_token_ref`, and a value **there** wins over the bot file, so a
  fresh node could not change which bot it was by editing the file its own notes point at;
* and the id and username were still read from the bot file, so the node reported a bot it was not
  authenticating as. Every log line, every `bot-info`, every message: confidently wrong.
"""

from __future__ import annotations

import json

import pytest

from db_ops.telegram import use_bot as use_bot_module
from db_ops.telegram.use_bot import UseBotError, use_bot

REF = "TOKEN_TELEGRAM_TEST_BOT"
IDENTITY = {"ok": True, "telegram_bot_id": "8629909482", "telegram_bot_username": "a_test_bot",
            "privacy_mode": "off", "note": "Privacy mode is OFF."}


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A data folder holding one bot token, with Telegram's answer stubbed."""
    monkeypatch.setattr("db_ops.lib.secret_text.load_secret_text",
                        lambda root, **kw: {REF: "123:abc", "OTHER_REF": "456:def"})
    monkeypatch.setattr("db_ops.telegram.api.bot_info", lambda **kw: dict(IDENTITY))
    return tmp_path


def written(node):
    return json.loads((node / "bot_telegram.json").read_text(encoding="utf-8"))


def test_the_identity_is_read_back_from_telegram_never_typed(node):
    """A hand-edit lets the file claim a bot the token does not belong to. This cannot."""
    result = use_bot(REF, data_dir=node)

    assert written(node) == {
        "telegram_bot_token_ref": REF,
        "telegram_bot_id": "8629909482",
        "telegram_bot_username": "a_test_bot",
    }
    assert result["now"] == "a_test_bot"
    assert result["written"] is True


def test_it_says_which_bot_it_replaced(node):
    """`use-store` prints the resolved connection for this reason: the mistake being prevented is
    *believing* the node is on the other one, and an acknowledgement does not disturb that."""
    (node / "bot_telegram.json").write_text(
        json.dumps({"telegram_bot_token_ref": "OTHER_REF",
                    "telegram_bot_username": "the_old_bot"}), encoding="utf-8")

    result = use_bot(REF, data_dir=node)

    assert (result["was"], result["now"]) == ("the_old_bot", "a_test_bot")
    assert result["privacy_mode"] == "off"


def test_a_dry_run_writes_nothing(node):
    result = use_bot(REF, data_dir=node, dry_run=True)

    assert result["written"] is False
    assert result["now"] == "a_test_bot"
    assert not (node / "bot_telegram.json").exists()


def test_a_ref_the_store_does_not_hold_is_refused_and_names_what_is_there(node):
    """A ref with no secret behind it fails later, inside whichever app command reaches the queue
    first — a long way from the config that caused it."""
    with pytest.raises(UseBotError) as caught:
        use_bot("TOKEN_THAT_DOES_NOT_EXIST", data_dir=node)

    message = str(caught.value)
    assert "TOKEN_THAT_DOES_NOT_EXIST" in message
    assert REF in message, "the refusal lists the token-looking refs that ARE there"
    assert not (node / "bot_telegram.json").exists()


def test_a_token_telegram_rejects_is_refused(node, monkeypatch):
    monkeypatch.setattr("db_ops.telegram.api.bot_info",
                        lambda **kw: {"ok": False, "telegram_bot_id": ""})

    with pytest.raises(UseBotError, match="did not accept"):
        use_bot(REF, data_dir=node)

    assert not (node / "bot_telegram.json").exists()


def test_a_pinned_ref_in_the_settings_file_is_refused_not_silently_ignored(node):
    """The defect this command exists to close, in its purest form.

    A `telegram_bot_token_ref` in `telegram_config.json` WINS over the bot file. Writing the bot
    file anyway changes the name the node reports and not the token it authenticates with — so the
    node ends up confidently claiming a bot it is not. Refuse, and name the file to edit.
    """
    with pytest.raises(UseBotError) as caught:
        use_bot(REF, data_dir=node,
                telegram_settings={"telegram_bot_token_ref": "SOMETHING_ELSE"},
                settings_path="data/telegram_config.json")

    message = str(caught.value)
    assert "SOMETHING_ELSE" in message and "WINS" in message
    assert "data/telegram_config.json" in message
    assert not (node / "bot_telegram.json").exists()


def test_a_pin_naming_the_same_ref_is_not_a_conflict(node):
    """Pinning it deliberately to the ref you are setting is consistent, not a mistake."""
    result = use_bot(REF, data_dir=node, telegram_settings={"telegram_bot_token_ref": REF})

    assert result["written"] is True


def test_an_empty_ref_is_refused(node):
    with pytest.raises(UseBotError, match="--ref is required"):
        use_bot("", data_dir=node)


# ------------------------------------------------------- what init leaves behind, and must not

def test_init_no_longer_pins_the_token_ref_but_does_name_the_bot_file():
    """`init` wrote `telegram_bot_token_ref` as the placeholder "TELEGRAM_BOT_TOKEN", and a value
    there overrides the bot file - so a fresh node could not change its bot by editing the file
    its own notes point at. The master does not set that key, which is why the master works."""
    from db_ops import scaffold

    settings = scaffold.TELEGRAM_CONFIG

    assert "telegram_bot_token_ref" not in settings, (
        "a ref here wins over bot_telegram.json; init must not pre-fill it")
    assert settings.get("bot_config_file") == "data/bot_telegram.json", (
        "the file init depends on has to be named in the config a reader opens")


def test_the_identity_only_comes_from_the_file_that_supplied_the_token():
    """Otherwise a settings file naming one ref and a bot file naming another produce a node that
    reports one bot and authenticates as the other."""
    from pathlib import Path

    from db_ops.config import parse_config

    root = Path(__file__).resolve().parents[1]
    parsed = parse_config({"telegram": {"telegram_bot_token_ref": "PINNED_ELSEWHERE",
                                        "telegram_bot_id": "", "telegram_bot_username": ""}},
                          base_dir=root)

    assert parsed.telegram.telegram_bot_token_ref == "PINNED_ELSEWHERE"
    assert parsed.telegram.telegram_bot_username == "", (
        "the bot file did not supply the token, so it may not supply the name")


def test_the_module_carries_no_estate_name():
    """It named this estate's own bot in its docstring and the export gate refused the tree - G-02,
    2 hits in 2 files, 2026-09-14. A shipped file states the shape, never the estate."""
    import inspect

    source = inspect.getsource(use_bot_module).lower()

    assert "dba_brain" not in source
