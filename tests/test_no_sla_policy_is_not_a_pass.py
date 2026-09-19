"""An estate with no objective written for it is unjudged, not compliant.

The rule: *a missing config file must never make a page say something false.* It was
written after `backup_policy.json` — absent, every backup type "not required", every database
compliant, `4/4 DB within policy` printed over a transaction-log backup 168 days old. That one file
was fixed; the rule covers every grader that reads a policy.

SLA is the second grader with the same shape, and it reached the same answer by the same route. With
no policies there is nothing to evaluate, so `required_failure` is False, so the summary fell out as
**PASSED** — a green SLA page over an estate nobody has set a single objective for. "No rule
required this" and "no rule exists" must never produce the same sentence.

The two graders that also read a policy file are honest already and stay that way, for a reason
worth writing down: `capacity_policy.json` and `restore_drill_policy.json` both carry **built-in
defaults that still grade** (30/90 days to full; a drill no older than a week). A default that
judges is not a blind spot — the danger is a default that judges *nothing* and calls it a pass.
"""

from __future__ import annotations

import json

import pytest

from db_ops.sla import policies as sla_policies
from db_ops.sla.cli import exit_code_for
from db_ops.sla.compliance import validate_sla_policies
from db_ops.sla.publish import STATUS_DISPLAY


def test_no_policies_is_not_a_pass(tmp_path):
    summary = validate_sla_policies(sqlite_path=tmp_path / "db_ops.sqlite", policies=[])

    assert summary.status == "NOT_CONFIGURED"
    assert summary.passed_count == 0
    assert summary.result_count == 0


def test_the_answer_names_the_file_and_the_word_unjudged(tmp_path):
    """The operator has to know which file to go and write. "NOT_CONFIGURED" alone sends them
    looking through the whole of data/."""
    summary = validate_sla_policies(sqlite_path=tmp_path / "db_ops.sqlite", policies=[])

    assert "data/sla_policies.json" in summary.reason
    assert "unjudged" in summary.reason
    assert "compliant" in summary.reason      # ...as the thing it is explicitly not


def test_it_renders_blank_rather_than_green():
    """The page is where the false statement was made, so this is where it matters. NO_DATA's
    marker, not PASSED's: nothing was measured."""
    assert STATUS_DISPLAY["NOT_CONFIGURED"] == STATUS_DISPLAY["NO_DATA"]
    assert STATUS_DISPLAY["NOT_CONFIGURED"] != STATUS_DISPLAY["PASSED"]


def test_it_does_not_make_the_scheduled_command_fail():
    """The other half of the same rule. `APP-SLA-VALIDATE` is in the shipped schedule, and a
    correct install logging a failing command every cycle teaches its reader to ignore the log —
    which is how the next real failure gets missed. Visible, not noisy."""
    assert exit_code_for("NOT_CONFIGURED") == 0
    assert exit_code_for("FAILED") == 1


def test_a_missing_default_policy_file_is_a_state_and_a_named_one_is_an_error(tmp_path, monkeypatch):
    """Two different facts. A path the caller typed is a file they believe in, and failing to open
    it is the answer they need; the default missing is a node that has not been given objectives,
    which belongs in the verdict rather than in a traceback."""
    monkeypatch.setattr(sla_policies, "DEFAULT_POLICIES_PATH", tmp_path / "nothing-here.json")

    assert sla_policies.load_sla_policies() == []
    with pytest.raises(OSError):
        sla_policies.load_sla_policies(tmp_path / "nothing-here.json")


def test_a_file_whose_policies_are_all_inactive_reads_the_same_way(tmp_path, monkeypatch):
    """Switching every policy off is the same state as having written none, and an operator who
    did it deliberately is not misled by being told so."""
    path = tmp_path / "sla_policies.json"
    path.write_text(json.dumps({"sla_policies": [
        {"policy_id": "P1", "db_types": ["sqlserver"], "metric_codes": ["CPU"], "active": False},
    ]}), encoding="utf-8")
    monkeypatch.setattr(sla_policies, "DEFAULT_POLICIES_PATH", path)

    assert sla_policies.load_sla_policies() == []
    assert validate_sla_policies(
        sqlite_path=tmp_path / "db_ops.sqlite", policies=[]).status == "NOT_CONFIGURED"


# ---------------------------------------------------------------------------
# The same rule one layer down: asking an empty store a question is not an error
# ---------------------------------------------------------------------------
def test_a_store_that_has_never_queued_anything_answers_never(tmp_path):
    """`sla validate --notify` reads when it last sent, to time its reminder. On a root where
    nothing has queued a message the table does not exist yet, and the raw
    `no such table: telegram_send_messages` reached the operator as a failed app command — on a
    correct install, for a queue that is simply empty."""
    from db_ops.db import DbOpsStore
    from db_ops.db.telegram_queue import last_queued_at

    store = DbOpsStore(tmp_path / "fresh.sqlite")      # deliberately not initialised

    assert last_queued_at(store=store, source_type="sla", note="sla_validate") == ""
