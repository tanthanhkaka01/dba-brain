"""Why the confirmation prompt has a deadline, and why that is a Windows fact rather than a taste.

`--force` on the SQL task runner costs one typed `yes`. Started in the background or with its
output piped, the run selected the task and then waited on an answer that could never arrive:
no `sql_runs` row, no output, nothing in the store — found later as "the task never ran". It is
named as a known limitation in the v0.17.0 release notes.

The cause is that **neither test this module can make distinguishes a human from a scheduler on
Windows.** Measured 2026-09-16 in a fully detached process with its stdin on the null device:
`sys.stdin.isatty()` answers **true**, and `open("CON")` **succeeds**. `/dev/tty` fails honestly
on POSIX; `CON` does not. So "is anyone there" cannot be answered by asking the operating system,
only by asking the question and seeing whether anything answers it.

A person reading the banner is not fast, so the wait is generous. It is finite because silence is
also an answer, and because a run that hangs is the one failure that leaves nothing behind to read.
"""

import io
import time

from db_ops.common import confirm


def test_an_answer_that_never_comes_is_not_waited_on_for_ever(monkeypatch):
    """The whole defect in one line: the terminal opens, and nothing is ever read from it."""
    monkeypatch.setattr(confirm.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(confirm, "open_terminal", lambda: _SilentTerminal())

    started = time.monotonic()
    answer = confirm.read_answer("proceed? ", stream=io.StringIO(), deadline_seconds=0.3)

    assert answer == ""
    assert time.monotonic() - started < 5      # it returned on the deadline, not on the read


def test_an_answer_that_does_come_is_read_exactly_as_before(monkeypatch):
    monkeypatch.setattr(confirm.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(confirm, "open_terminal", lambda: io.StringIO("yes\n"))

    assert confirm.read_answer("proceed? ", stream=io.StringIO()).strip() == "yes"


def test_the_deadline_can_be_waived_for_a_caller_that_means_to_block(monkeypatch):
    """Zero means "no deadline" so the behaviour is still reachable, and reachable deliberately
    rather than by default."""
    monkeypatch.setattr(confirm.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(confirm, "open_terminal", lambda: io.StringIO("yes\n"))

    assert confirm.read_answer("proceed? ", stream=io.StringIO(), deadline_seconds=0).strip() == "yes"


def test_silence_is_refused_and_says_what_to_do_instead(monkeypatch):
    """"expected 'yes', got ''" reads like a typo and sends the operator back to the keyboard.
    The one thing they need to know is that the keyboard is not connected to this run."""
    report = _report()

    allowed = confirm.require_confirmation(
        report, {"confirm": True}, operation="run-sql-task", target="sql_id 9",
        interactive=True, stream=io.StringIO(), input_fn=lambda prompt: "",
    )

    assert allowed is False
    detail = report.to_dict()["gates"][0]["detail"]
    assert "no answer" in detail and "assume_yes" in detail
    assert "Nothing was executed." in detail


def test_a_wrong_answer_is_still_a_wrong_answer(monkeypatch):
    """Somebody typed something. That is a different fact from nobody being there, and the two
    must not read the same afterwards — the evidence file is what says who allowed a run."""
    report = _report()

    confirm.require_confirmation(
        report, {"confirm": True}, operation="run-sql-task", target="sql_id 9",
        interactive=True, stream=io.StringIO(), input_fn=lambda prompt: "no",
    )

    assert report.to_dict()["facts"]["authorization"]["answer"] == "no"


def test_the_forced_run_gate_refuses_when_nothing_answers(monkeypatch):
    """End to end through the gate `sql_tasks` actually calls: intent alone, a terminal that
    opens and never answers, and the operation is refused rather than held open."""
    monkeypatch.setattr(confirm.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(confirm, "open_terminal", lambda: _SilentTerminal())
    monkeypatch.setattr(confirm, "open_terminal_write", lambda: None)
    monkeypatch.setattr(confirm, "ANSWER_DEADLINE_SECONDS", 0.3)

    started = time.monotonic()
    report = confirm.authorize_request({
        "operation": "run-sql-task", "target_id": "9", "target_label": "sql_id 9",
        "confirm": True,
    })

    assert report["ok"] is False
    assert time.monotonic() - started < 5


# ---------------------------------------------------------------------------
class _SilentTerminal(io.StringIO):
    """A terminal that opens and never answers — `CON` in a detached Windows process."""

    def readline(self, *args):
        time.sleep(30)
        return ""


def _report():
    from db_ops.common.evidence import GateReport

    return GateReport(operation="run-sql-task", target="sql_id 9")
