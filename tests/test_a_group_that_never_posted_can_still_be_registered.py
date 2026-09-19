"""A chat created *for* alerts has nobody posting in it, and that was enough to make it unusable.

`save-updates` learns a group from `getUpdates`, which only ever reports a chat somebody has
**posted in**. Everything downstream depended on that: `group-level` refuses outright with *"no
groups in telegram_groups.json. Run `save-updates` first"*, so a brand-new alert chat could not be
given a level at all.

The only workaround was to post something in every chat, wait for the intake to run, then
configure — or to copy `telegram_groups.json` between nodes instead of registering it.

`group-add` is that command. The one thing it must not do is trust the number: a chat id typed with
a digit wrong routes every alert of that level to a chat that does not exist, or to somebody else's.
So the id is confirmed with Telegram and **the title comes back from `getChat`**, which is what lets
an operator read the name of the chat they actually configured instead of the number they typed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.telegram import updates as telegram_updates


@pytest.fixture
def groups_file(tmp_path: Path) -> Path:
    path = tmp_path / "telegram_groups.json"
    path.write_text(json.dumps({"schema_version": 1, "telegram_groups": []}), encoding="utf-8")
    return path


@pytest.fixture
def telegram(monkeypatch):
    """Telegram answering `getChat`, and a record of what it was asked."""
    asked: list[dict] = []

    def fake_call(*, bot_token, method_name, payload, api_url="", **kwargs):
        asked.append({"method": method_name, "payload": payload, "token": bot_token})
        return {"ok": True, "result": {"id": payload["chat_id"], "type": "supergroup",
                                       "title": "Alerts - critical"}}

    monkeypatch.setattr("db_ops.telegram.api.call_telegram_api", fake_call)
    return asked


def _records(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["telegram_groups"]


def test_a_chat_nobody_has_posted_in_is_registered(groups_file, telegram) -> None:
    result = telegram_updates.add_group(
        group_id="-1001234567890", level="critical", bot_token="t",
        groups_path=groups_file)

    assert result["created"] is True
    record = _records(groups_file)[0]
    assert record["group_id"] == "-1001234567890"
    assert record["notify_level"] == "critical"
    assert record["status"] == "active"


def test_the_title_comes_back_from_telegram_not_from_the_operator(groups_file, telegram) -> None:
    """The point of asking. An operator who typed the wrong number reads back the wrong chat's
    name, which is the only moment the mistake is cheap."""
    telegram_updates.add_group(group_id="-1001234567890", level="critical", title="what I meant",
                               bot_token="t", groups_path=groups_file)

    assert _records(groups_file)[0]["title"] == "Alerts - critical"
    assert telegram[0]["method"] == "getChat"
    assert telegram[0]["payload"]["chat_id"] == "-1001234567890"


def test_registering_a_chat_grants_no_command_permission(groups_file, telegram) -> None:
    """Where alerts go and who may drive the node from a chat are two decisions. `save-updates`
    writes a discovered group with no command permission, and this keeps that default."""
    telegram_updates.add_group(group_id="-1001234567890", level="critical", bot_token="t",
                               groups_path=groups_file)

    assert _records(groups_file)[0]["allow_command"] == 0


def test_registering_twice_updates_rather_than_refuses(groups_file, telegram) -> None:
    """Re-running a registration is how a node is brought back to a known state. Refusing would
    make the command unusable in the script that stands one up."""
    telegram_updates.add_group(group_id="-1001234567890", level="warning", bot_token="t",
                               groups_path=groups_file)
    result = telegram_updates.add_group(group_id="-1001234567890", level="critical",
                                        allow_command=100, bot_token="t", groups_path=groups_file)

    assert result["created"] is False
    records = _records(groups_file)
    assert len(records) == 1, "a second entry for one chat splits its routing in half"
    assert records[0]["notify_level"] == "critical"
    assert records[0]["allow_command"] == 100


def test_without_a_token_it_refuses_rather_than_writing_an_unchecked_id(groups_file) -> None:
    """Silently writing an unverified route is the failure this command would otherwise add."""
    with pytest.raises(RuntimeError, match="use-bot"):
        telegram_updates.add_group(group_id="-1001234567890", level="critical",
                                   groups_path=groups_file)

    assert _records(groups_file) == []


def test_an_unverified_entry_says_so_in_the_file(groups_file) -> None:
    """There is a legitimate case - no token yet - and the file has to carry the caveat, because
    the next reader cannot tell a confirmed id from a typed one by looking at it."""
    result = telegram_updates.add_group(group_id="-1001234567890", level="critical",
                                        title="Alerts - critical", verify=False,
                                        groups_path=groups_file)

    assert result["verified"] is False
    assert "WITHOUT confirming" in _records(groups_file)[0]["note"]


def test_an_empty_id_is_refused(groups_file, telegram) -> None:
    with pytest.raises(RuntimeError, match="group_id is required"):
        telegram_updates.add_group(group_id="  ", bot_token="t", groups_path=groups_file)
