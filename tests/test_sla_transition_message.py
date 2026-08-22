"""The Telegram message must be about what changed, and must fit on a phone screen.

Measured on the live queue: 78 SLA messages in 76 hours, mean length 3,852 characters — a hard
scroll past dozens of unchanged rows to find whether anything was different. The web report already
holds the standing detail and is always current, so repeating it in the message bought nothing.

What these pin is that the message leads with the transition counts, names only the findings that
moved, and links out for the rest.
"""

from __future__ import annotations

from db_ops.lib.state_transition import decide_notification, diff_states
from db_ops.sla.models import SlaPolicyResult, SlaValidationSummary
from db_ops.sla.publish import TELEGRAM_MESSAGE_MAX_CHARS, build_transition_message

SEVERITY = ("FAILED", "AT_RISK", "NO_DATA", "PASSED")
REPORT = "http://example.invalid/report_dba/sla.html"


def _result(policy_id: str, target_id: str, status: str) -> SlaPolicyResult:
    return SlaPolicyResult(
        policy_id=policy_id, name=policy_id, target_id=target_id, scope="instance",
        category="availability", status=status, objective_percent=99.0,
        actual_percent=50.0 if status != "PASSED" else 100.0,
        error_budget_percent=1.0, budget_consumed_percent=0.0, budget_remaining_percent=0.0,
        total_count=10, good_count=5, bad_count=5, no_data=False,
        window_hours=24, window_start="2026-08-04T12:00:00Z", window_end="2026-08-05T12:00:00Z",
    )


def _summary(results) -> SlaValidationSummary:
    return SlaValidationSummary(
        status="FAILED", policy_count=len(results), result_count=len(results),
        passed_count=sum(1 for r in results if r.status == "PASSED"),
        at_risk_count=sum(1 for r in results if r.status == "AT_RISK"),
        failed_count=sum(1 for r in results if r.status == "FAILED"),
        no_data_count=sum(1 for r in results if r.status == "NO_DATA"),
        window_end="2026-08-05T12:00:00Z", results=tuple(results),
    )


def _built(previous, results, *, last_sent_at=None):
    summary = _summary(results)
    current = {f"{r.policy_id} @ {r.target_id}": r.status for r in results}
    diff = diff_states(previous, current, severity_order=SEVERITY, healthy=("PASSED",))
    decision = decide_notification(diff, last_sent_at=last_sent_at)
    return build_transition_message(summary, diff, decision, report_url=REPORT), decision


def test_the_message_names_what_changed_and_not_what_did_not():
    results = [_result("BACKUP", "srv-a", "FAILED"), _result("UPTIME", "srv-b", "FAILED")]
    body, _ = _built({"BACKUP @ srv-a": "PASSED", "UPTIME @ srv-b": "FAILED"}, results)

    assert "BACKUP @ srv-a" in body, "the new failure is the reason to send at all"
    assert "UPTIME @ srv-b" not in body, "unchanged findings belong on the page, not in every message"


def test_the_header_carries_the_three_counts_the_audit_asked_for():
    results = [_result("A", "s", "FAILED"), _result("B", "s", "PASSED"), _result("C", "s", "FAILED")]
    body, _ = _built({"A @ s": "PASSED", "B @ s": "FAILED", "C @ s": "FAILED"}, results)

    assert "1 new failed" in body
    assert "1 recovered" in body
    assert "1 unchanged" in body


def test_the_message_links_to_the_report_instead_of_repeating_it():
    results = [_result("A", "s", "FAILED")]
    body, _ = _built({"A @ s": "PASSED"}, results)
    assert REPORT in body


def test_a_change_message_is_a_fraction_of_the_old_wall_of_text():
    """40 standing failures and one new one: the old body listed all 41 every hour."""
    results = [_result(f"P{index}", "srv", "FAILED") for index in range(41)]
    previous = {f"P{index} @ srv": "FAILED" for index in range(1, 41)}
    previous["P0 @ srv"] = "PASSED"

    body, _ = _built(previous, results)

    assert "P0 @ srv" in body
    assert len(body) < 700, f"still {len(body)} chars; the point was to stop sending 3,852"


def test_a_long_list_of_new_failures_is_capped_and_says_how_many_were_left_out():
    results = [_result(f"P{index}", "srv", "FAILED") for index in range(30)]
    previous = {f"P{index} @ srv": "PASSED" for index in range(30)}

    body, _ = _built(previous, results)

    assert "and 24 more" in body
    assert len(body) <= TELEGRAM_MESSAGE_MAX_CHARS


def test_the_reminder_lists_the_standing_backlog_because_that_is_its_whole_job():
    """On a reminder there are no transitions to report; naming nothing would send an empty alarm."""
    results = [_result("A", "s", "FAILED")]
    body, decision = _built({"A @ s": "FAILED"}, results, last_sent_at="2026-08-01T00:00:00Z")

    assert decision.kind == "reminder"
    assert "Still failing" in body and "A @ s" in body


def test_a_finding_that_stopped_being_evaluated_is_labelled_as_such_not_as_recovered():
    results = [_result("B", "s", "PASSED")]
    body, _ = _built({"A @ s": "FAILED", "B @ s": "PASSED"}, results, last_sent_at="2026-08-01T00:00:00Z")

    assert "no longer evaluated" in body
    assert "Recovered" not in body


def test_the_body_never_exceeds_what_telegram_accepts():
    """Telegram rejects over 4096 with HTTP 400, and a rejected alert is a silent one."""
    results = [_result(f"POLICY_WITH_A_LONG_NAME_{index}", f"server-{index}", "FAILED") for index in range(400)]
    body, _ = _built({}, results)
    assert len(body) <= TELEGRAM_MESSAGE_MAX_CHARS


def test_the_headline_reports_bad_news_alongside_the_good():
    """A run said "0 new, 3 recovered" while a policy fell to 0% in the same window. A headline
    that lists only what improved is how a reader learns to trust the wrong summary."""
    results = [_result("A", "s", "FAILED"), _result("B", "s", "PASSED")]
    body, _ = _built({"A @ s": "AT_RISK", "B @ s": "FAILED"}, results)

    assert "1 worse" in body.splitlines()[0]
    assert "1 recovered" in body.splitlines()[0]


def test_the_producer_does_not_stamp_its_own_emoji():
    """Tagging is the send layer's job (db_ops.telegram.severity); one vocabulary for all
    producers is the point, and a second one here would drift from it."""
    results = [_result("A", "s", "FAILED")]
    body, _ = _built({"A @ s": "PASSED"}, results)
    assert body.splitlines()[0][0].isalpha()


def test_the_message_severity_describes_the_change_not_the_backlog():
    """With a standing backlog the run status is FAILED forever. Passing that through would put a
    failure emoji on a message whose entire content is good news."""
    from db_ops.sla.publish import transition_status

    good_news = diff_states({"A @ s": "FAILED"}, {"A @ s": "PASSED"}, severity_order=SEVERITY, healthy=("PASSED",))
    assert transition_status(good_news, decide_notification(good_news, last_sent_at=None)) == "SUCCESS"

    bad_news = diff_states({"A @ s": "PASSED"}, {"A @ s": "FAILED"}, severity_order=SEVERITY, healthy=("PASSED",))
    assert transition_status(bad_news, decide_notification(bad_news, last_sent_at=None)) == "CRITICAL"

    worse = diff_states({"A @ s": "AT_RISK"}, {"A @ s": "FAILED"}, severity_order=SEVERITY, healthy=("PASSED",))
    assert transition_status(worse, decide_notification(worse, last_sent_at=None)) == "WARNING"
