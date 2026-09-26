"""`common.cli ask` - one question on the terminal, for an app that may not import `confirm`.

The deploy's config-drift gate asks *adopt / keep / abort*. It used to call `confirm.read_answer`
itself, which made `control` an app importing `common` (rules R03). Asking is an operation with a
deadline and a terminal behind a pipe - the parts that went wrong when a prompt was written twice -
so the question moved to `common` and the gate runs it. These pin what the gate relies on: nobody to
ask is said as such, an empty or late answer is never turned into a choice, and an answer already
collected is held to the same list as a typed one.
"""

from __future__ import annotations

import io

import pytest

from db_ops.common import confirm
from db_ops.control import config_gate


def _no_terminal(monkeypatch):
    monkeypatch.setattr(confirm.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(confirm, "open_terminal", lambda: None)


def _typed(monkeypatch, *lines):
    remaining = list(lines)
    monkeypatch.setattr(confirm.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(confirm, "open_terminal", lambda: io.StringIO(""))
    monkeypatch.setattr(confirm, "open_terminal_write", lambda: io.StringIO())
    monkeypatch.setattr(confirm, "read_answer",
                        lambda prompt, stream=None, deadline_seconds=None: remaining.pop(0) if remaining else "")


def test_with_nobody_to_ask_the_answer_says_so_and_chooses_nothing(monkeypatch):
    _no_terminal(monkeypatch)
    assert confirm.ask_request({"prompt": "adopt / keep / abort ? "}) == {
        "interactive": False, "answer": "", "source": "none"}


def test_a_typed_answer_is_held_to_the_choices_and_asked_again(monkeypatch):
    _typed(monkeypatch, "maybe", "KEEP\n")
    answer = confirm.ask_request({"prompt": "? ", "choices": ["adopt", "keep", "abort"], "tries": 3})
    assert answer == {"interactive": True, "answer": "keep", "source": "terminal"}


def test_an_empty_or_late_answer_is_no_answer_not_a_choice(monkeypatch):
    _typed(monkeypatch, "")
    answer = confirm.ask_request({"prompt": "? ", "choices": ["adopt", "keep", "abort"], "tries": 3})
    assert answer == {"interactive": True, "answer": "", "source": "terminal"}


def test_an_answer_carried_in_the_request_meets_the_same_list(monkeypatch):
    _no_terminal(monkeypatch)
    assert confirm.ask_request({"prompt": "? ", "choices": ["yes", "no"], "answer": "No"})["answer"] == "no"
    with pytest.raises(ValueError, match="not one of"):
        confirm.ask_request({"prompt": "? ", "choices": ["yes", "no"], "answer": "sure"})


def test_a_question_needs_its_prompt():
    with pytest.raises(ValueError, match="prompt"):
        confirm.ask_request({})


def test_the_drift_gate_stops_when_ask_finds_nobody(monkeypatch):
    """The gate's own default reader: `ask` answering interactive false is the no-terminal abort."""
    calls: list[tuple[str, dict]] = []

    def run(command, request, **_kwargs):
        calls.append((command, request))
        return {"interactive": False, "answer": "", "source": "none"}

    monkeypatch.setattr("db_ops.transport.common_cli.run", run)
    assert config_gate._read_choice(config_gate._ask_on_the_terminal) is None
    assert calls == [("ask", {"prompt": "  adopt / keep / abort ? "})]


def test_the_drift_gate_reads_a_typed_choice_through_ask(monkeypatch):
    monkeypatch.setattr("db_ops.transport.common_cli.run",
                        lambda command, request, **_kwargs: {"interactive": True, "answer": "Adopt\n"})
    assert config_gate._read_choice(config_gate._ask_on_the_terminal) == "adopt"
