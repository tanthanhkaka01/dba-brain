"""The shared confirmation control: db_ops.common.confirm.

Restarting a host, stopping a database service and applying a cumulative update are different
operations with the same failure mode — the right command aimed at the wrong machine. They
therefore share one control rather than one each, and these tests pin what it guarantees:

* a payload declaring intent (``confirm``) is not enough on its own at a terminal — a human must
  read the target and type the whole word ``yes``;
* with no terminal to ask on, the operation is **refused** unless the request explicitly says it
  is unattended, so a scheduled job cannot silently reboot production;
* whatever authorized the run is recorded, because a typed confirmation and an unattended one
  are different facts.
"""

import io

from db_ops.common import confirm
from db_ops.common.evidence import FAIL, OK, GateReport


def _report():
    return GateReport("host-restart", target="ACME-192-0-2-250")


def test_intent_alone_is_not_enough_at_a_terminal():
    """A payload can be replayed, copied, or written for another host; a person cannot."""
    report = _report()
    stream = io.StringIO()
    asked: list[str] = []

    allowed = confirm.require_confirmation(
        report, {"confirm": True}, operation="host-restart", target="APPDB-DB (10.0.0.5)",
        effects=["the host will be REBOOTED now"], interactive=True, stream=stream,
        input_fn=lambda prompt: asked.append(prompt) or "yes",
    )

    assert allowed is True
    assert asked, "a terminal run must actually ask"
    assert report.gates[-1].status == OK
    assert report.facts["authorization"]["by"] == "prompt"


def test_the_prompt_names_the_target_and_the_consequence():
    """"Are you sure?" with no content trains people to answer without reading."""
    report = _report()
    stream = io.StringIO()

    confirm.require_confirmation(
        report, {"confirm": True, "reason": "clear pending file renames"},
        operation="host-restart", target="APPDB-DB (192.0.2.250, windows)",
        effects=["the host will be REBOOTED now", "services that must come back: MSSQL$APPDB"],
        interactive=True, stream=stream, input_fn=lambda prompt: "yes",
    )
    shown = stream.getvalue()

    assert "192.0.2.250" in shown
    assert "REBOOTED" in shown
    assert "MSSQL$APPDB" in shown
    assert "clear pending file renames" in shown


def test_only_the_whole_word_yes_proceeds():
    """`y` is what a hand types while reading something else."""
    for answer in ["y", "Y", "no", "", "yes please", "sure"]:
        report = _report()
        allowed = confirm.require_confirmation(
            report, {"confirm": True}, operation="host-restart", target="APPDB-DB",
            interactive=True, stream=io.StringIO(), input_fn=lambda prompt, a=answer: a,
        )
        assert allowed is False, f"{answer!r} must not authorize a restart"
        assert report.gates[-1].status == FAIL

    report = _report()
    assert confirm.require_confirmation(
        report, {"confirm": True}, operation="host-restart", target="APPDB-DB",
        interactive=True, stream=io.StringIO(), input_fn=lambda prompt: "  YES\n",
    ) is True


def test_a_refusal_at_the_prompt_is_recorded_as_loudly_as_an_approval():
    report = _report()
    confirm.require_confirmation(
        report, {"confirm": True}, operation="host-restart", target="APPDB-DB",
        interactive=True, stream=io.StringIO(), input_fn=lambda prompt: "no",
    )

    assert report.gates[-1].name == "confirm"
    assert "aborted at confirmation 1 of 1" in report.gates[-1].detail
    assert report.facts["authorization"]["authorized"] is False
    assert report.facts["authorization"]["answer"] == "no"


def test_without_a_terminal_the_operation_is_refused_rather_than_assumed():
    """A cron job that forgot to say "nobody is watching" must fail, not reboot a host at 03:00."""
    report = _report()

    allowed = confirm.require_confirmation(
        report, {"confirm": True}, operation="host-restart", target="APPDB-DB", interactive=False,
    )

    assert allowed is False
    assert "no terminal to ask on" in report.gates[-1].detail
    assert "assume_yes" in report.gates[-1].detail  # the message says exactly what to add


def test_unattended_automation_must_declare_itself_and_is_recorded_as_such():
    report = _report()
    stream = io.StringIO()

    allowed = confirm.require_confirmation(
        report, {"confirm": True, "assume_yes": True}, operation="host-restart",
        target="APPDB-DB", interactive=False, stream=stream,
    )

    assert allowed is True
    assert report.facts["authorization"]["by"] == "assume_yes"
    assert stream.getvalue() == "", "nothing should be printed when nobody is there to read it"


def test_assume_yes_behaves_the_same_whether_or_not_someone_is_logged_in():
    """A scripted run must not change behaviour because an operator happens to have a terminal."""
    report = _report()
    asked: list[str] = []

    allowed = confirm.require_confirmation(
        report, {"confirm": True, "assume_yes": True}, operation="host-restart", target="APPDB-DB",
        interactive=True, stream=io.StringIO(), input_fn=lambda prompt: asked.append(prompt) or "",
    )

    assert allowed is True
    assert asked == []


def test_piping_the_request_on_stdin_does_not_take_the_question_away(monkeypatch):
    """`... host-restart - < request.json` leaves stdin exhausted. Reading the answer from that
    pipe returns EOF instantly, aborting a legitimate operation and teaching operators that the
    prompt is broken — so the question is asked on the controlling terminal instead."""
    monkeypatch.setattr(confirm.sys, "stdin", io.StringIO(""))   # a consumed pipe, not a tty
    monkeypatch.setattr(confirm, "open_terminal", lambda: io.StringIO("yes\n"))

    assert confirm.is_interactive() is True
    assert confirm.read_answer("proceed? ", stream=io.StringIO()).strip() == "yes"


def test_with_no_terminal_behind_the_pipe_there_is_nobody_to_ask(monkeypatch):
    monkeypatch.setattr(confirm.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(confirm, "open_terminal", lambda: None)

    assert confirm.is_interactive() is False
    assert confirm.read_answer("proceed? ", stream=io.StringIO()) == ""


def test_a_request_that_declares_no_intent_is_refused_before_anything_is_asked():
    report = _report()
    asked: list[str] = []

    allowed = confirm.require_confirmation(
        report, {}, operation="host-restart", target="APPDB-DB", interactive=True,
        stream=io.StringIO(), input_fn=lambda prompt: asked.append(prompt) or "yes",
    )

    assert allowed is False
    assert asked == [], "there is nothing to confirm until the request says what it wants"
    assert '"confirm": true' in report.gates[-1].detail
