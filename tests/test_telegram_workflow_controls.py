"""Driving a real conversation through Back, Skip, Cancel — and the trail it must leave behind.

`tests/test_telegram_workflow_steps.py` holds the rules as pure functions. This file holds the
part those cannot: that the conversation loop actually applies them, and that the store ends up
able to answer "what was this person asked, what did they say, and which question is live" — which
before 2026-09-09 it could not, because the answers lived in a positional list inside one JSON
column and the prompt that produced them was never kept at all.

The workflow under test is a small one written here rather than a shipped command, so that a
change to the estate's real commands cannot silently turn these into tests of nothing.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from db_ops.db import DbOpsStore
from db_ops.telegram.command_processor import (
    process_one_command_message,
    process_pending_conversation_messages,
)

COMMAND_TEXT = "spbot_demo_flow"
USER = "100"

#: name -> auth -> (ref | password) -> note (optional) -> confirm. Two things are deliberate: the
#: branch, so Back has positions that were never asked to step over, and the optional `note`
#: followed by a required `confirm`, so a Skip cannot finish the workflow and start executing
#: something inside a unit test.
COMMAND = {
    "command_id": 900,
    "command_text": COMMAND_TEXT,
    "command_type": 1,
    "is_group": 1,
    "is_private": 1,
    "need_file": 0,
    "reply_default": 0,
    "reply_text": "",
    "action_type": "cli_execute",
    "action_config": {
        "command_argv": ["{python}", "-c", "print('never reached in these tests')"],
        "parameters": [
            {"name": "name", "source": "arg", "position": 1, "required": True,
             "prompt_text": "Instance name?"},
            {"name": "auth", "source": "arg", "position": 2, "required": True,
             "input_type": "choice", "allow_text_input": False,
             "options": [{"label": "Secret ref", "value": "secret_ref"},
                         {"label": "Password", "value": "password"}],
             "prompt_text": "How should it authenticate?"},
            {"name": "password_ref", "source": "arg", "position": 3, "required": True,
             "ask_when": {"parameter": "auth", "equals": "secret_ref"},
             "prompt_text": "Which secret ref?"},
            {"name": "password_text", "source": "arg", "position": 4, "required": True,
             "secret": True, "ask_when": {"parameter": "auth", "equals": "password"},
             "prompt_text": "The password?"},
            {"name": "note", "source": "arg", "position": 5, "required": False,
             # allow_skip is what asks an optional step at all; see is_asked_when_optional.
             "allow_skip": True, "prompt_text": "A note for the record?"},
            {"name": "confirm", "source": "arg", "position": 6, "required": True,
             "input_type": "choice",
             "options": [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}],
             "validator": "regex", "pattern": "yes|no",
             "validation_error": "answer yes or no",
             "prompt_text": "Go ahead?"},
        ],
    },
}


def _write(path, root_key, rows):
    path.write_text(json.dumps({root_key: rows}, ensure_ascii=False), encoding="utf-8")


class Chat:
    """One operator, one conversation, driven message by message."""

    def __init__(self, tmp_path):
        self.sqlite_path = tmp_path / "runtime.sqlite"
        self.commands_path = tmp_path / "telegram_support_commands.json"
        _write(self.commands_path, "telegram_support_commands", [COMMAND])
        _write(tmp_path / "telegram_users.json", "telegram_users",
               [{"user_id": USER, "user_type": 2, "status": "active"}])
        _write(tmp_path / "telegram_groups.json", "telegram_groups", [])
        self.store = DbOpsStore(self.sqlite_path)
        self.next_id = 10

    def _post(self, text):
        self.next_id += 1
        self.store.upsert_telegram_messages([{
            "update_id": self.next_id, "message_id": self.next_id,
            "message_date": 1_779_478_400 + self.next_id, "chat_id": USER, "chat_type": "private",
            "user_id": USER, "text": text, "raw": {"from": {"id": int(USER), "username": "op"}},
        }])
        return self.next_id

    def start(self):
        self._post(f"/{COMMAND_TEXT}")
        self.store.sync_telegram_command_messages(command_prefix="/spbot")
        with sqlite3.connect(self.sqlite_path) as connection:
            row = connection.execute(
                "SELECT telegram_command_message_id FROM telegram_command_messages "
                "ORDER BY telegram_command_message_id DESC LIMIT 1").fetchone()
        self.run_key = f"tcm:{int(row[0])}"
        return process_one_command_message(
            sqlite_path=self.sqlite_path, telegram_command_message_id=int(row[0]),
            commands_path=self.commands_path)

    def say(self, text):
        self._post(text)
        return process_pending_conversation_messages(
            sqlite_path=self.sqlite_path, commands_path=self.commands_path)

    def last_message(self):
        with sqlite3.connect(self.sqlite_path) as connection:
            connection.row_factory = sqlite3.Row
            return dict(connection.execute(
                "SELECT message_text, metadata_json FROM telegram_send_messages "
                "ORDER BY send_tlgmsg_id DESC LIMIT 1").fetchone())

    def messages_text(self):
        """Everything queued so far. A refusal is followed by the re-asked question, so the
        message that explains it is not the last one — asserting on the last would test the
        ordering of two messages rather than the refusal."""
        with sqlite3.connect(self.sqlite_path) as connection:
            return (chr(10)).join(str(row[0]) for row in connection.execute(
                "SELECT message_text FROM telegram_send_messages ORDER BY send_tlgmsg_id"))

    def keyboard(self):
        markup = json.loads(self.last_message()["metadata_json"]).get("reply_markup") or {}
        return [[button["text"] for button in row] for row in markup.get("keyboard", [])]

    def trail(self):
        return [
            (int(row["step_no"]), str(row["parameter_name"]), str(row["status"]),
             None if row["answer_text"] is None else str(row["answer_text"]),
             None if row["answer_kind"] is None else str(row["answer_kind"]))
            for row in self.store.fetch_telegram_workflow_steps(run_key=self.run_key)
        ]

    def active_step(self):
        row = self.store.fetch_active_telegram_workflow_step(run_key=self.run_key)
        return None if row is None else str(row["parameter_name"])


@pytest.fixture()
def chat(tmp_path):
    conversation = Chat(tmp_path)
    conversation.start()
    return conversation


# --------------------------------------------------------------------------- #
# The trail
# --------------------------------------------------------------------------- #
def test_the_first_question_is_recorded_as_the_live_step(chat) -> None:
    assert chat.active_step() == "name"
    assert chat.trail() == [(1, "name", "active", None, None)]


def test_an_answer_closes_its_step_and_opens_the_next(chat) -> None:
    chat.say("mssql_lab_01")

    assert chat.trail() == [
        (1, "name", "answered", "mssql_lab_01", "text"),
        (2, "auth", "active", None, None),
    ]
    assert chat.active_step() == "auth"


def test_exactly_one_step_of_a_run_is_ever_live(chat) -> None:
    chat.say("mssql_lab_01")
    chat.say("Secret ref")
    chat.say("REMOTE_192_0_2_115_DEV")

    live = [row for row in chat.trail() if row[2] == "active"]
    assert len(live) == 1 and live[0][1] == "note"


def test_a_button_label_is_stored_as_the_value_it_stands_for(chat) -> None:
    """The keyboard says `Secret ref`; the CLI's pattern wants `secret_ref`."""
    chat.say("mssql_lab_01")
    chat.say("Secret ref")

    answered = [row for row in chat.trail() if row[1] == "auth"][0]
    assert answered[3] == "secret_ref" and answered[4] == "option"


def test_a_secret_answer_is_never_written_to_the_trail(chat) -> None:
    chat.say("mssql_lab_01")
    chat.say("Password")
    chat.say("hunter2-hunter2")

    stored = [row for row in chat.trail() if row[1] == "password_text"][0]
    assert stored[3] == "*** (15 chars)"
    assert "hunter2" not in json.dumps(chat.trail())


# --------------------------------------------------------------------------- #
# Branching
# --------------------------------------------------------------------------- #
def test_choosing_a_secret_ref_never_asks_for_a_password(chat) -> None:
    """The report this work came from: having given the ref, the operator was asked for a
    password and then for a key file, and had to answer `-` to both."""
    chat.say("mssql_lab_01")
    chat.say("Secret ref")
    chat.say("REMOTE_192_0_2_115_DEV")

    assert [row[1] for row in chat.trail()] == ["name", "auth", "password_ref", "note"]


# --------------------------------------------------------------------------- #
# Back
# --------------------------------------------------------------------------- #
def test_back_re_asks_the_previous_question(chat) -> None:
    chat.say("mssql_lab_01")
    chat.say("Secret ref")
    assert chat.active_step() == "password_ref"

    chat.say("back")

    assert chat.active_step() == "auth"
    assert chat.last_message()["message_text"].startswith("How should it authenticate?")
    assert [row[2] for row in chat.trail()][:3] == ["answered", "answered", "back"]


def test_back_twice_walks_two_questions_back(chat) -> None:
    chat.say("mssql_lab_01")
    chat.say("Secret ref")
    chat.say("back")
    chat.say("back")

    assert chat.active_step() == "name"


def test_back_steps_over_a_branch_that_was_never_asked(chat) -> None:
    """`position - 1` from `note` is `password_text`, which this run never asked. Back must not
    ask it now: the answer would be meaningless and the branch was not taken."""
    chat.say("mssql_lab_01")
    chat.say("Secret ref")
    chat.say("REMOTE_192_0_2_115_DEV")
    assert chat.active_step() == "note"

    chat.say("back")

    assert chat.active_step() == "password_ref"


def test_there_is_nothing_to_go_back_to_from_the_first_question(chat) -> None:
    chat.say("back")

    assert "nothing to go back to" in chat.messages_text()
    assert chat.active_step() == "name"


def test_re_choosing_a_branch_forgets_what_the_abandoned_one_collected(chat) -> None:
    """Give a password, go back, choose the ref instead: the password must not survive."""
    chat.say("mssql_lab_01")
    chat.say("Password")
    chat.say("hunter2-hunter2")
    chat.say("back")
    chat.say("back")
    chat.say("Secret ref")

    assert chat.active_step() == "password_ref"
    with sqlite3.connect(chat.sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        state = connection.execute(
            "SELECT state_json FROM telegram_conversation_states "
            "ORDER BY state_id DESC LIMIT 1").fetchone()
    assert "hunter2" not in str(state["state_json"])


# --------------------------------------------------------------------------- #
# Skip
# --------------------------------------------------------------------------- #
def test_skip_is_offered_on_an_optional_step_and_stores_the_dash(chat) -> None:
    chat.say("mssql_lab_01")
    chat.say("Secret ref")
    chat.say("REMOTE_192_0_2_115_DEV")
    assert ["⬅️ Back", "⏭️ Skip", "🚫 Cancel"] in chat.keyboard()

    chat.say("skip")

    skipped = [row for row in chat.trail() if row[1] == "note"][0]
    assert skipped[2] == "skipped" and skipped[3] == "-"
    assert chat.active_step() == "confirm"


def test_a_required_step_refuses_to_be_skipped_and_asks_again(chat) -> None:
    chat.say("skip")

    assert "cannot be skipped" in chat.messages_text()
    assert chat.active_step() == "name"


def test_a_required_step_never_shows_a_skip_button(chat) -> None:
    assert chat.keyboard() == [["🚫 Cancel"]]


# --------------------------------------------------------------------------- #
# Cancel
# --------------------------------------------------------------------------- #
def test_cancel_ends_the_workflow_and_says_nothing_was_done(chat) -> None:
    chat.say("mssql_lab_01")
    chat.say("cancel")

    message = chat.last_message()
    assert "cancelled" in message["message_text"].lower()
    assert "No changes were made." in message["message_text"]
    assert json.loads(message["metadata_json"])["reply_markup"] == {
        "remove_keyboard": True, "selective": True}
    assert chat.active_step() is None
    assert [row[2] for row in chat.trail()] == ["answered", "cancelled"]


def test_a_cancelled_run_stops_consuming_replies(chat) -> None:
    chat.say("cancel")
    before = chat.trail()

    chat.say("mssql_lab_01")

    assert chat.trail() == before, "a cancelled workflow must not pick the conversation back up"


# --------------------------------------------------------------------------- #
# Validation, at the step rather than at the end
# --------------------------------------------------------------------------- #
def test_an_answer_outside_the_offered_options_is_refused_at_that_step(chat) -> None:
    chat.say("mssql_lab_01")
    chat.say("whatever")

    assert "Please choose one of: Secret ref, Password" in chat.messages_text()
    assert chat.active_step() == "auth"
    assert [row for row in chat.trail() if row[1] == "auth"][0][2] == "rejected"


def test_a_value_failing_the_pattern_is_refused_at_that_step(chat) -> None:
    """Before this, the run reached step 14 and then reported a mistake made at step 2."""
    chat.say("mssql_lab_01")
    chat.say("Secret ref")
    chat.say("REMOTE_192_0_2_115_DEV")
    chat.say("skip")
    chat.say("maybe")

    assert "answer yes or no" in chat.messages_text()
    assert chat.active_step() == "confirm"


# --------------------------------------------------------------------------- #
# Answers that arrive in the command message itself
# --------------------------------------------------------------------------- #
def _start_with(tmp_path, text):
    conversation = Chat(tmp_path)
    conversation._post(text)
    conversation.store.sync_telegram_command_messages(command_prefix="/spbot")
    with sqlite3.connect(conversation.sqlite_path) as connection:
        row = connection.execute(
            "SELECT telegram_command_message_id FROM telegram_command_messages "
            "ORDER BY telegram_command_message_id DESC LIMIT 1").fetchone()
    conversation.run_key = f"tcm:{int(row[0])}"
    process_one_command_message(
        sqlite_path=conversation.sqlite_path, telegram_command_message_id=int(row[0]),
        commands_path=conversation.commands_path)
    return conversation


def test_arguments_given_in_the_command_message_are_not_asked_again(tmp_path) -> None:
    """`/spbot_run_sql_task 18 0 30` is the whole point: the conversation is a fallback, not a
    toll gate. Only what is still missing is prompted for."""
    chat = _start_with(tmp_path, f"/{COMMAND_TEXT} lab01 secret_ref MYREF")

    assert chat.active_step() == "note"


def test_answers_typed_ahead_of_the_question_are_still_recorded(tmp_path) -> None:
    """Otherwise the trail begins at the first *prompted* step and silently drops the rest —
    a run answered entirely in one message would leave no trace of what was answered."""
    chat = _start_with(tmp_path, f"/{COMMAND_TEXT} lab01 secret_ref MYREF")

    assert chat.trail()[:3] == [
        (1, "name", "answered", "lab01", "inline"),
        (2, "auth", "answered", "secret_ref", "inline"),
        (3, "password_ref", "answered", "MYREF", "inline"),
    ]


def test_a_branch_the_inline_answers_did_not_take_is_not_recorded(tmp_path) -> None:
    """Positions are positional: `-` in the password slot belongs to a branch this run excluded,
    so it is neither asked nor written down as an answer."""
    chat = _start_with(tmp_path, f"/{COMMAND_TEXT} lab01 secret_ref MYREF -")

    assert "password_text" not in [row[1] for row in chat.trail()]


def test_an_inline_secret_is_masked_in_the_trail_exactly_as_a_typed_one_is(tmp_path) -> None:
    # position 3 is password_ref, which this branch does not use - but it still occupies a
    # position on the command line, so it needs a placeholder. See the test below.
    chat = _start_with(tmp_path, f"/{COMMAND_TEXT} lab01 password - hunter2-hunter2")

    stored = [row for row in chat.trail() if row[1] == "password_text"][0]
    assert stored[3] == "*** (15 chars)" and stored[4] == "inline"


def test_inline_positions_still_count_the_steps_a_branch_will_not_ask(tmp_path) -> None:
    """The one thing the branch does **not** simplify: typing the command in one message.

    A conversation asks only the questions this run needs, so `password` is the third *question*.
    On the command line it is still the fourth *position*, because positions are fixed by the
    definition and not by the branch. Written down because the failure is quiet: the value lands
    on the step the branch is skipping, is discarded with it, and the bot then asks for the
    password it looks like you already gave.
    """
    chat = _start_with(tmp_path, f"/{COMMAND_TEXT} lab01 password hunter2-hunter2")

    assert chat.active_step() == "password_text", "the password went into the skipped slot"
    assert "hunter2-hunter2" not in json.dumps(chat.trail())
