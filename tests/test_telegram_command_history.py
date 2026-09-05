"""Reading your own commands back — the ones the chat cannot show you.

A command answered one prompt at a time leaves a message saying `/spbot_run_sql_task` and nothing
else; the `18` and the `0 30` are separate messages further down, indistinguishable from
conversation. Scrolling for them is what this replaces, so the whole value of the feature is in
the rebuilt line being the line that runs — these tests are mostly about that line.
"""

from __future__ import annotations

import json

from db_ops.common import telegram_command_history as history
from db_ops.lib.telegram_command_text import render_command_line


def _row(**over):
    row = {"telegram_command_message_id": 100, "chat_id": "123456789", "chat_type": "private",
           "user_id": "123456789", "message_date": 1788502581, "text": "/spbot_self_status",
           "command_prefix": "/spbot", "command_payload": "_self_status",
           "created_at": "2026-09-04T06:16:22Z", "conversation_status": None,
           "conversation_command_text": None, "conversation_state_json": None,
           "conversation_updated_at": None}
    row.update(over)
    return row


def _conversation(command_text, args, *, status="done", **over):
    return _row(
        text=f"/{command_text}",
        command_payload="_" + command_text.removeprefix("spbot_"),
        conversation_status=status,
        conversation_command_text=command_text,
        conversation_state_json=json.dumps({"args": args, "parameter_name": "x",
                                            "parameter_position": len(args)}),
        **over,
    )


class _Store:
    def __init__(self, rows):
        self.rows = rows
        self.asked = []

    def fetch_recent_telegram_command_messages(self, *, user_id, limit):
        self.asked.append((user_id, limit))
        return [row for row in self.rows if row["user_id"] == user_id][:limit]


# --------------------------------------------------------------------------- #
# The rebuilt line
# --------------------------------------------------------------------------- #
def test_four_messages_become_the_one_line_that_runs_them():
    """The reason the command exists. `/spbot_run_sql_task` + `18` + `0 30` is a command nobody
    can copy out of a chat, because the chat holds it as three unrelated-looking messages."""
    entry = history.rebuild_command(_conversation("spbot_run_sql_task", ["18", "0 30"]))

    assert entry["line"] == "/spbot_run_sql_task 18 0 30"


def test_a_command_that_needed_no_prompt_keeps_the_arguments_it_was_typed_with():
    entry = history.rebuild_command(
        _row(text="/spbot_run_sql_task 28", command_payload="_run_sql_task 28"))

    assert entry["line"] == "/spbot_run_sql_task 28"


def test_the_bot_username_a_group_appends_is_not_part_of_the_command():
    """Telegram sends `/spbot_status@it_dev_code_sp_bot` in a group. Written back with the
    username attached, the line is one nobody would type."""
    entry = history.rebuild_command(_row(
        text="/spbot_status@it_dev_code_sp_bot", command_payload="_status@it_dev_code_sp_bot"))

    assert entry["line"] == "/spbot_status"


def test_an_answer_holding_a_space_stays_one_argument_when_others_follow_it():
    """Joined plainly it would parse back as two, shifting every later argument by one - a
    different command wearing the same words."""
    line = render_command_line("spbot_run_sql_task", ["18", "0 30", "yes"])

    assert line == "/spbot_run_sql_task 18 '0 30' yes"
    assert line.endswith("yes")


def test_the_last_argument_is_never_quoted_because_it_is_usually_the_rest_of_the_message():
    """`task_params` is `consume_rest`: the dispatcher hands it everything after its position,
    spaces included. Quoting it would show the reader punctuation they never typed."""
    assert render_command_line("spbot_run_sql_task", ["18", "0 30"]) == "/spbot_run_sql_task 18 0 30"


def test_an_optional_answer_nobody_gave_is_not_part_of_the_line():
    assert render_command_line("spbot_kill_spid", ["server", "440", ""]) == "/spbot_kill_spid server 440"


# --------------------------------------------------------------------------- #
# What counts as "ran"
# --------------------------------------------------------------------------- #
def test_a_prompt_that_was_never_answered_is_not_offered_as_a_command():
    """Its arguments are incomplete. Offering half a command invites someone to run it and find
    out which half is missing."""
    assert history.rebuild_command(_conversation("spbot_kill_spid", ["A1A"], status="waiting")) is None
    assert history.rebuild_command(_conversation("spbot_kill_spid", ["A1A"], status="replaced")) is None


def test_the_skipped_ones_are_counted_rather_than_quietly_dropped():
    """Hiding without accounting is indistinguishable from losing - the rule every /spbot_list_*
    reply follows."""
    store = _Store([_conversation("spbot_kill_spid", ["A1A"], status="waiting"),
                    _row()])

    result = history.collect(store, user_id="123456789")

    assert result["unfinished"] == 1
    assert "1 unanswered prompt(s) skipped" in history.render(result)


def test_a_command_that_ran_and_failed_is_listed_and_says_so():
    """It is still the command that was typed, and after a failure it is the one most likely to
    be typed again. Listing it silently would offer a refused argument as if it had worked."""
    store = _Store([_conversation("spbot_run_sql_task", ["28", "aksdhf"], status="error")])

    text = history.render(history.collect(store, user_id="123456789"))

    assert "/spbot_run_sql_task 28 aksdhf" in text
    assert "last one failed" in text


# --------------------------------------------------------------------------- #
# Distinct, newest first
# --------------------------------------------------------------------------- #
def test_the_same_command_run_nine_times_spends_one_line_and_says_nine():
    """Someone chasing a problem repeats one command. Ten copies of it is not a history."""
    rows = [_row(created_at="2026-09-04T06:16:2%dZ" % n) for n in range(9)]
    rows.append(_row(text="/spbot_list_metrics", command_payload="_list_metrics",
                     created_at="2026-09-03T01:00:00Z"))

    result = history.collect(_Store(rows), user_id="123456789")

    assert [entry["line"] for entry in result["entries"]] == [
        "/spbot_self_status", "/spbot_list_metrics"]
    assert result["entries"][0]["times"] == 9


def test_a_repeat_still_counts_after_the_answer_is_full():
    """The count is what tells a repeat from a one-off. Stopping the scan at the limit would
    report `x1` for the command someone ran all morning."""
    rows = [_row(text=f"/spbot_c{n}", command_payload=f"_c{n}") for n in range(4)]
    rows.extend(_row(text="/spbot_c0", command_payload="_c0") for _ in range(2))

    result = history.collect(_Store(rows), user_id="123456789", limit=3)

    assert [entry["line"] for entry in result["entries"]] == [
        "/spbot_c0", "/spbot_c1", "/spbot_c2"], "the fourth is past the limit"
    assert result["entries"][0]["times"] == 3, "its two later repeats were seen anyway"


def test_the_newest_run_dates_the_entry():
    """Ordered newest first, so the first sighting is the most recent one. Overwriting it with a
    later repeat would date the command by the oldest time it was run."""
    rows = [_row(created_at="2026-09-04T06:16:22Z"), _row(created_at="2026-08-01T00:00:00Z")]

    result = history.collect(_Store(rows), user_id="123456789")

    assert result["entries"][0]["sent_at"] == "2026-09-04T06:16:22Z"


def test_the_history_command_leaves_itself_out():
    """Its own name is the one command the reader already knows: they just typed it. A listing
    of ten that always spends one on itself is a listing of nine."""
    rows = [_row(text="/spbot_list_my_commands", command_payload="_list_my_commands"), _row()]

    result = history.collect(_Store(rows), user_id="123456789", exclude=["spbot_list_my_commands"])

    assert [entry["line"] for entry in result["entries"]] == ["/spbot_self_status"]


# --------------------------------------------------------------------------- #
# Whose history, and how much of it
# --------------------------------------------------------------------------- #
def test_only_the_person_who_asked_is_read():
    """The caller is the row's user_id, never an argument. A history that took the person as a
    parameter would let anyone read anyone's by typing a number."""
    store = _Store([_row(), _row(user_id="7873858430", text="/spbot_backup")])

    result = history.collect(store, user_id="123456789")

    assert store.asked[0][0] == "123456789"
    assert [entry["line"] for entry in result["entries"]] == ["/spbot_self_status"]


def test_a_chat_with_no_sender_is_told_so_rather_than_shown_an_empty_history():
    """A channel post carries no user. "You have not run any command" would be a lie about the
    person reading it."""
    result = history.collect(_Store([]), user_id="")

    assert result["entries"] == [] and result["scanned"] == 0
    assert "does not identify a sender" in history.render(result)


def test_more_messages_are_read_than_commands_returned():
    """Repeats are the norm, so a window the size of the answer returns three lines on a busy
    morning - the store is asked for a multiple of what the reader gets."""
    store = _Store([])

    history.collect(store, user_id="123456789", limit=10)

    assert store.asked == [("123456789", 10 * history.SCAN_MULTIPLIER)]


def test_the_limit_is_bounded_so_a_typo_cannot_ask_for_the_whole_table():
    store = _Store([])

    history.collect(store, user_id="123456789", limit=10_000)
    history.collect(store, user_id="123456789", limit=0)

    assert [limit for _, limit in store.asked] == [history.MAX_SCAN, history.SCAN_MULTIPLIER]


def test_an_empty_history_says_so_rather_than_printing_a_bare_header():
    assert history.render(history.collect(_Store([]), user_id="123456789")) == (
        "You have not run any bot command yet.")


def test_the_listing_stops_before_telegram_truncates_it_and_says_so():
    """Telegram cuts at 4096 characters. A listing the transport truncates loses its newest rows
    with nothing to say it happened, which is the wrong end and a silent one."""
    long_argument = "A-RATHER-LONG-SERVER-NAME-AS-THIS-ESTATE-WRITES-THEM-%03d" % 0
    rows = [_row(text=f"/spbot_shrink_log {long_argument} database_{n} 2000",
                 command_payload=f"_shrink_log {long_argument} database_{n} 2000",
                 created_at="2026-09-04T06:16:22Z")
            for n in range(200)]

    text = history.render(history.collect(_Store(rows), user_id="123456789", limit=history.MAX_LIMIT))

    assert len(text) < 4096
    assert "more not shown (message size limit)." in text
    assert "database_0 2000\n" in text, "the newest rows are the ones that must survive"
