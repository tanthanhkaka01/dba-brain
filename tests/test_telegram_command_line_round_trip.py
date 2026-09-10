"""A command written back as a line must parse into the same answers — or the replay is a lie.

`/spbot_list_my_commands` shows every past command as the line a person would type to repeat it,
and people do repeat them. So the round trip *answers → line → answers* is not cosmetic: when it
does not close, tapping a remembered command runs a different one, wearing the same words.

This file pins where it closes and where it does not, because the boundary is a design constraint
the whole command catalogue is written against — **a step must be answerable in one word** — and a
constraint nobody can see is one the next parameter quietly violates. The operator asked for it to
be written down after answering a `Your name?` prompt with three words; the measurements here are
what that answer actually does.

The unquoted tail is deliberate and tested in `test_telegram_command_history.py`: the last
parameter is usually `consume_rest`. These tests do not argue with that — they record what it
costs, so the cost stays chosen rather than rediscovered.
"""

from __future__ import annotations

from db_ops.lib.telegram_command_text import (
    parse_command_message,
    render_command_line,
    split_with_verbatim_tail,
)

COMMAND = "spbot_demo"


def round_trip(answers: list[str]) -> list[str]:
    """What comes back when a run's answers are written as a line and read again."""
    return parse_command_message(render_command_line(COMMAND, answers))["args"]


def test_single_word_answers_survive() -> None:
    assert round_trip(["a", "b", "c"]) == ["a", "b", "c"]


def test_a_space_in_a_middle_answer_survives_because_it_is_quoted() -> None:
    assert render_command_line(COMMAND, ["my value", "c"]) == f"/{COMMAND} 'my value' c"
    assert round_trip(["my value", "c"]) == ["my value", "c"]


def test_a_space_in_the_last_answer_shifts_the_command() -> None:
    """`Your name?` answered `Trieu Tan Thanh` replays as three arguments, not one.

    This is the constraint, stated as a measurement: the tail is left unquoted for `consume_rest`
    bodies, so an ordinary answer holding a space becomes several arguments when the line is read
    back — and every parameter after it is shifted.
    """
    assert round_trip(["Trieu Tan Thanh"]) == ["Trieu", "Tan", "Thanh"]
    assert round_trip(["a", "my value"]) == ["a", "my", "value"]


def test_it_is_the_answer_just_given_that_is_most_exposed() -> None:
    """Mid-workflow, trailing unanswered arguments are trimmed — so the newest answer is last.

    Which means the value most likely to be mangled by a replay is the one the operator has this
    second finished typing, not some historical tail.
    """
    mid_run = ["mssql_lab_01", "Trieu Tan Thanh", "", "", ""]
    assert render_command_line(COMMAND, mid_run) == f"/{COMMAND} mssql_lab_01 Trieu Tan Thanh"
    assert round_trip(mid_run) == ["mssql_lab_01", "Trieu", "Tan", "Thanh"]


def test_a_button_label_with_spaces_is_safe_because_the_value_is_stored() -> None:
    """Labels may read like English; only the value ever reaches the args list.

    `[Yes - destroy and rebuild]` is a fine button for the value `yes` — the conversation maps the
    label back to the value before anything is stored, so the line holds `yes` and round-trips.
    """
    assert round_trip(["lab01", "yes"]) == ["lab01", "yes"]


def test_an_answer_that_was_never_given_is_not_part_of_the_line() -> None:
    assert round_trip(["server", "440", ""]) == ["server", "440"]


# --------------------------------------------------------------------------- #
# A consume_rest tail is the message, not a list of words
# --------------------------------------------------------------------------- #
def test_a_pasted_sql_body_keeps_its_quotes() -> None:
    """`shlex.split` removes them, and the statement then means something else — or nothing.

    Measured before the fix: `WHERE name = 'Tan Thanh'` reached the CLI as
    `WHERE name = Tan Thanh`.
    """
    text = "/spbot_sql_to_xlsx TGT SELECT id FROM users WHERE name = 'Tan Thanh'"

    args = split_with_verbatim_tail(text, 2)

    assert args == ["TGT", "SELECT id FROM users WHERE name = 'Tan Thanh'"]


def test_a_pasted_sql_body_keeps_its_line_breaks() -> None:
    """Flattened to one line, a `--` comment swallows the rest of the query.

    `SELECT id\n-- only the active ones\nFROM users` became
    `SELECT id -- only the active ones FROM users`, which runs as `SELECT id`. Nothing reported
    it: the query was valid, just not the one anybody wrote.
    """
    text = "/spbot_sql_to_xlsx TGT SELECT id\n-- only the active ones\nFROM users WHERE ok = 1"

    args = split_with_verbatim_tail(text, 2)

    assert args[1] == "SELECT id\n-- only the active ones\nFROM users WHERE ok = 1"
    assert "\n" in args[1], "a comment line must stay a line"


def test_the_arguments_before_the_tail_are_still_split_normally() -> None:
    assert split_with_verbatim_tail("/spbot_add_sql SRV name daily chat SELECT 1", 5) == [
        "SRV", "name", "daily", "chat", "SELECT 1"]


def test_a_quoted_head_argument_still_arrives_unquoted() -> None:
    """`render_command_line` quotes head arguments when it writes a command back, so reading one
    has to accept them — the two directions are the same rule seen from two sides."""
    assert split_with_verbatim_tail("/spbot_add_sql SRV 'my task' daily chat SELECT 1", 5) == [
        "SRV", "my task", "daily", "chat", "SELECT 1"]


def test_a_command_with_no_tail_yet_is_just_its_head() -> None:
    assert split_with_verbatim_tail("/spbot_sql_to_xlsx TGT", 2) == ["TGT"]
