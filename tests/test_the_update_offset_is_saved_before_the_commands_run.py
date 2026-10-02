"""The bot moves its update offset as soon as the updates are stored, not when the workflow ends.

`run-workflow` saved the offset only after commands, conversations and the send pass had all
returned. A later step that raised - or the daemon killing the run at its timeout - kept the old
offset, so every run re-read the same `limit` updates and the ones behind them were never fetched.
"""

from __future__ import annotations

import json

import pytest

from db_ops.telegram import cli as telegram_cli
from db_ops.telegram import workflow


def test_the_offset_is_saved_even_when_a_later_step_fails(monkeypatch):
    saved: list = []
    monkeypatch.setattr(workflow, "fetch_and_save_updates",
                        lambda **kw: {"ok": True, "updates": 3, "next_update_offset": 43})
    monkeypatch.setattr(workflow, "save_command_messages_from_messages", lambda **kw: {})

    def boom(**kwargs):
        raise RuntimeError("a command step failed")

    monkeypatch.setattr(workflow, "process_pending_command_messages", boom)

    with pytest.raises(RuntimeError):
        workflow.run_bot_workflow(sqlite_path="x", bot_token="t", save_offset=saved.append)

    assert saved == [43]


def test_saving_the_offset_keeps_the_rest_of_the_file(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    settings = data / "telegram_config.json"
    settings.write_text(json.dumps({"level_chat_map": {"error": "-100"}, "update_offset": 1}),
                        encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"telegram_config_file": "data/telegram_config.json"}),
                      encoding="utf-8")

    telegram_cli.save_next_update_offset(str(config), 43)

    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "level_chat_map": {"error": "-100"}, "update_offset": 43}
    assert [p.name for p in data.iterdir()] == ["telegram_config.json"]   # no temp file left
