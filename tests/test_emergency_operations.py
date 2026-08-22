"""What it costs to authorize a destructive action, and that the cost cannot be skipped.

These are the commands that exist so a DBA does not have to reach a workstation during an
incident: restart a host, shrink a log that filled its volume, kill the session holding an estate
hostage, start the job that fixes it. Reachable from a phone is the point, and it is also the
risk — the confirmation is the only thing between a Telegram message and a production restart.

Two properties matter more than the rest and both are silent when wrong:

* a level-100 operation must cost **two** answers, the second one being the target's own id, so a
  payload written for one host cannot be replayed against another;
* an operation nobody remembered to configure must become **harder**, never easier.
"""

import json
from pathlib import Path

import pytest

from db_ops.common import confirm, sqlserver_emergency
from db_ops.common.evidence import GateReport
from conftest import shipped_config

OPERATIONS = shipped_config("emergency_operations.json")
TELEGRAM_COMMANDS = shipped_config("telegram_support_commands.json")
TARGET = "ACME-192-0-2-115"


def _report():
    return GateReport("test", target=TARGET)


def _ask(*answers):
    """A terminal that types ``answers`` in order, then nothing."""
    queue = list(answers)
    return lambda prompt: queue.pop(0) if queue else ""


# ---------------------------------------------------------------------------
# The level table
# ---------------------------------------------------------------------------
def test_taking_a_machine_down_costs_two_answers_and_moving_data_costs_one():
    assert confirm.load_operation("host-restart")["confirmations"] == 2
    for operation in ("shrink-log", "kill-spid", "start-job"):
        assert confirm.load_operation(operation)["confirmations"] == 1, operation


def test_an_operation_missing_from_the_table_gets_the_strictest_answer_not_the_weakest():
    """A command added to the CLI and forgotten in the config must become harder to run.

    The opposite default — unknown means unguarded — is how a destructive command ships with no
    confirmation at all and nobody notices until it is used.
    """
    rules = confirm.load_operation("some-command-added-next-year")
    assert (rules["level"], rules["confirmations"]) == (100, 2)


def test_an_unreadable_table_still_refuses_rather_than_waves_through(tmp_path):
    broken = tmp_path / "emergency_operations.json"
    broken.write_text("{ this is not json", encoding="utf-8")

    assert confirm.load_operation("shrink-log", path=broken)["confirmations"] == 2


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_intent_alone_is_not_authorization():
    """`confirm: true` says the payload meant it. It does not say a human saw this target."""
    report = _report()
    assert confirm.require_confirmation(
        report, {}, operation="shrink-log", target=TARGET, interactive=True,
        input_fn=_ask("yes"),
    ) is False
    assert report.facts["authorization"]["authorized"] is False


def test_one_yes_authorizes_a_level_50_operation():
    report = _report()
    assert confirm.require_confirmation(
        report, {"confirm": True}, operation="shrink-log", target=TARGET,
        interactive=True, input_fn=_ask("yes"), confirmations=1,
    ) is True
    assert report.facts["authorization"]["confirmations"] == 1


def test_a_level_100_operation_is_not_authorized_by_saying_yes_twice():
    """The second answer is the target's id, precisely so it cannot be muscle memory."""
    report = _report()
    assert confirm.require_confirmation(
        report, {"confirm": True}, operation="host-restart", target="a label",
        interactive=True, input_fn=_ask("yes", "yes"),
        confirmations=2, challenge=TARGET,
    ) is False
    assert "aborted at confirmation 2 of 2" in report.gates[-1].detail


def test_a_level_100_operation_is_authorized_by_typing_the_target_id():
    report = _report()
    assert confirm.require_confirmation(
        report, {"confirm": True}, operation="host-restart", target="a label",
        interactive=True, input_fn=_ask("yes", TARGET),
        confirmations=2, challenge=TARGET,
    ) is True
    assert report.facts["authorization"]["confirmations"] == 2


def test_a_payload_written_for_one_host_is_refused_by_another():
    """The replay case this exists for: the same JSON, aimed somewhere else.

    Whoever typed the id typed the id of the host they were looking at. Against a different
    target the answer no longer matches, and nothing runs.
    """
    payload = {"confirm": "yes", "confirm_target": "ACME-192-0-2-115"}
    report = _report()

    assert confirm.require_confirmation(
        report, payload, operation="host-restart", target="the OTHER host",
        interactive=False, confirmations=2, challenge="ACME-192-0-2-253",
    ) is False
    assert report.facts["authorization"]["expected"] == "ACME-192-0-2-253"


# ---------------------------------------------------------------------------
# Answers that arrive from somewhere other than a terminal
# ---------------------------------------------------------------------------
def test_answers_carried_in_the_request_authorize_without_a_terminal():
    """How a human answers from a phone: the Telegram processor asks, and forwards the replies.

    It must not need a tty — there is none — and it must not need `assume_yes`, which would
    record the run as unattended when a person actually answered.
    """
    report = _report()
    assert confirm.require_confirmation(
        report,
        {"confirm": "yes", "confirm_target": TARGET,
         "authorized_by": {"channel": "telegram", "user_id": "42"}},
        operation="host-restart", target="label", interactive=False,
        confirmations=2, challenge=TARGET,
    ) is True

    authorization = report.facts["authorization"]
    assert authorization["answered_by"] == ["request", "request"]
    assert authorization["channel"] == "telegram"


def test_a_bare_true_is_intent_only_so_a_terminal_run_still_asks():
    asked = []
    confirm.require_confirmation(
        _report(), {"confirm": True}, operation="shrink-log", target=TARGET,
        interactive=True, input_fn=lambda prompt: asked.append(prompt) or "yes",
    )
    assert len(asked) == 1, "a boolean must not count as the typed answer"


def test_without_a_terminal_and_without_answers_the_operation_names_what_is_missing():
    report = _report()
    assert confirm.require_confirmation(
        report, {"confirm": True}, operation="host-restart", target=TARGET,
        interactive=False, confirmations=2, challenge=TARGET,
    ) is False
    assert "'confirm'" in report.gates[-1].detail


def test_unattended_automation_is_recorded_as_unattended():
    """`assume_yes` stays available and stays honest: the evidence must not read like a person."""
    report = _report()
    assert confirm.require_confirmation(
        report, {"confirm": True, "assume_yes": True}, operation="host-restart",
        target=TARGET, interactive=False, confirmations=2, challenge=TARGET,
    ) is True
    assert report.facts["authorization"]["by"] == "assume_yes"


# ---------------------------------------------------------------------------
# The operations themselves
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["SALESDB; DROP DATABASE X", "a'b", "[weird]", "", "x" * 200])
def test_object_names_that_are_not_plain_names_are_refused_before_any_connection(name):
    """These values reach T-SQL. Quoting is applied too, but the narrow gate is what makes the
    quoting provably sufficient — and it fails before the target is even resolved."""
    with pytest.raises(sqlserver_emergency.EmergencyError):
        sqlserver_emergency._require_name(name, "database")


def test_shrinking_to_zero_cannot_be_asked_for_by_omission():
    """A missing size must fail, not default. A log shrunk "to 0" grows straight back."""
    with pytest.raises(sqlserver_emergency.EmergencyError, match="size_mb is required"):
        sqlserver_emergency.shrink_log({"target": TARGET, "database": "SALESDB"})


def test_system_sessions_are_not_killable():
    with pytest.raises(sqlserver_emergency.EmergencyError, match="system session"):
        sqlserver_emergency.kill_spid({"target": TARGET, "spid": 16})


# ---------------------------------------------------------------------------
# The two config files have to agree
# ---------------------------------------------------------------------------
def test_the_telegram_clearance_matches_the_operation_level():
    """Two files, two questions: `command_type` is who may ask, the level is how hard to confirm.

    They are allowed to be separate — `common` reads no Telegram settings — but an operation that
    costs two confirmations must not be reachable from a chat cleared for level 50.
    """
    levels = json.loads(OPERATIONS.read_bytes().decode("utf-8-sig"))["operations"]
    commands = json.loads(TELEGRAM_COMMANDS.read_bytes().decode("utf-8-sig"))[
        "telegram_support_commands"
    ]
    by_operation = {
        str((entry.get("action_config") or {}).get("emergency_operation") or ""): entry
        for entry in commands
    }

    for operation, rules in levels.items():
        entry = by_operation.get(operation)
        if entry is None:
            continue  # not exposed to Telegram yet; the CLI still enforces the level
        assert int(entry["command_type"]) == int(rules["level"]), (
            f"{operation}: telegram command_type={entry['command_type']} but the operation is "
            f"level {rules['level']}"
        )
