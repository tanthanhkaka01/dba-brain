"""A file answered to the bot can be capped per parameter.

An attachment is read whole into memory and, as base64, carried inside a JSON request a third larger
than the file. Telegram's own bot limit (20 MB) was the only bound: no command could say "a SQL
body is never more than a megabyte" (review 0.25.0, F8.3). `api.get_file_bytes` already took a
`max_bytes`; nothing passed one. A parameter's `max_file_bytes` now does, and its absence keeps the
old behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from db_ops.telegram import command_processor


@pytest.fixture
def downloads(monkeypatch):
    seen: list[object] = []
    import db_ops.lib.config as config_module
    from db_ops.telegram import api

    monkeypatch.setattr(config_module, "load_config", lambda _path: SimpleNamespace(
        telegram=SimpleNamespace(resolved_bot_token="t", api_url="https://api.telegram.org")))
    monkeypatch.setattr(api, "get_file_bytes", lambda **kw: seen.append(kw.get("max_bytes")) or b"SELECT 1")
    return seen


def test_the_cap_reaches_the_download(downloads):
    command_processor._download_document_text({"file_id": "f"}, config_path="c", max_bytes=1048576)
    command_processor._download_document_base64({"file_id": "f"}, config_path="c", max_bytes=2048)

    assert downloads == [1048576, 2048]


def test_no_cap_keeps_telegram_s_own_limit(downloads):
    command_processor._download_document_text({"file_id": "f"}, config_path="c")

    assert downloads == [None]


@pytest.mark.parametrize(("raw", "expected"), [(None, None), ("", None), (1024, 1024), ("2048", 2048)])
def test_the_parameter_s_value_is_read(raw, expected):
    assert command_processor._max_file_bytes({"max_file_bytes": raw}) == expected


@pytest.mark.parametrize("raw", [0, -1, "big"])
def test_a_value_that_is_not_a_positive_size_is_refused_not_read_as_no_limit(raw):
    with pytest.raises(RuntimeError, match="max_file_bytes"):
        command_processor._max_file_bytes({"max_file_bytes": raw})
