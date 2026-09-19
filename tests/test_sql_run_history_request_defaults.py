"""`/spbot_list_sql_runs` takes a sql_id and a count, and 0 means "I did not say".

The command renders its request from a JSON template, so a parameter the operator skipped still
reaches the CLI — as 0. Read literally, both zeros answered a question nobody asks: `sql_id: 0`
filtered on a task numbered zero and returned an empty listing, and `limit: 0` went through
`max(1, ...)` and returned a single row. Both look exactly like "there is no history", which is
the one answer this command must never give by accident.

Asked for on 2026-09-17: sql_id filters when given, 0 or skipped lists every task; the count is 10
when 0 or skipped, otherwise what was asked for, up to 100.
"""

from __future__ import annotations

import pytest

from db_ops.db.cli import REQUEST_LISTING_LIMIT, REQUEST_LISTING_MAX


def _normalise(request: dict) -> tuple[int, int | None]:
    """The two lines `_sql_run_history_command` runs before it touches the store."""
    limit = int(request.get("limit") or 0) or REQUEST_LISTING_LIMIT
    limit = max(1, min(limit, REQUEST_LISTING_MAX))
    sql_id = int(request.get("sql_id") or 0) or None
    return limit, sql_id


@pytest.mark.parametrize("request_body", [{}, {"sql_id": 0}, {"sql_id": "0"}])
def test_no_sql_id_or_zero_means_every_task(request_body):
    _, sql_id = _normalise(request_body)
    assert sql_id is None, "0 must not filter on a task numbered zero"


def test_a_real_sql_id_still_filters():
    _, sql_id = _normalise({"sql_id": 28})
    assert sql_id == 28


@pytest.mark.parametrize("request_body", [{}, {"limit": 0}, {"limit": "0"}, {"limit": 10}])
def test_no_count_or_zero_or_ten_gives_ten(request_body):
    limit, _ = _normalise(request_body)
    assert limit == REQUEST_LISTING_LIMIT == 10


def test_a_count_is_honoured_between_one_and_the_cap():
    assert _normalise({"limit": 1})[0] == 1
    assert _normalise({"limit": 37})[0] == 37
    assert _normalise({"limit": 100})[0] == 100


def test_a_count_above_the_cap_is_clamped_not_refused():
    # Refusing would make the operator retype; the cap is about what fits in a chat message.
    assert _normalise({"limit": 500})[0] == REQUEST_LISTING_MAX == 100


def test_the_chat_cap_is_below_the_api_ceiling():
    """`sql_run_history.MAX_LIMIT` still bounds a direct API caller; the chat's cap is its own."""
    from db_ops.common import sql_run_history

    assert REQUEST_LISTING_MAX < sql_run_history.MAX_LIMIT


def test_the_command_passes_both_parameters_through_to_the_cli():
    import json
    from pathlib import Path

    packaged = (Path(__file__).resolve().parents[1]
                / "db_ops/telegram/catalogue/telegram_support_commands.json")
    doc = json.loads(packaged.read_bytes().decode("utf-8-sig"))
    commands = doc.get("telegram_support_commands", doc)
    entry = next(c for c in commands if c["command_text"] == "spbot_list_sql_runs")

    names = [p["name"] for p in entry["action_config"]["parameters"]]
    assert names == ["sql_id", "limit"]
    assert all(p.get("required") is False for p in entry["action_config"]["parameters"]), \
        "both are optional: skipping either is the common case"
    argv = " ".join(entry["action_config"]["command_argv"])
    assert "{sql_id}" in argv and "{limit}" in argv
