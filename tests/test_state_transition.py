"""A recurring check should speak when something changes, not when the clock ticks.

The SLA app sent 78 Telegram messages in 76 hours, averaging 3,852 characters, and the content
barely moved between them. A channel that says the same thing every hour is a channel nobody
reads, so the failure this pins is not "the message was long" — it is that a genuinely new
outage arrived looking exactly like the 77 messages before it.

Two edges carry most of the risk and have their own tests below. A key that vanished from the run
must never be reported as recovered: retiring a policy would otherwise announce a fix that never
happened. And the reminder clock must run from the last message actually sent, because a clock
reset by a silent run is a reminder that never fires.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db_ops.lib.state_transition import decide_notification, diff_states

SEVERITY = ("FAILED", "AT_RISK", "NO_DATA", "PASSED")
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _diff(previous, current):
    return diff_states(previous, current, severity_order=SEVERITY, healthy=("PASSED",))


def test_a_newly_broken_policy_is_a_transition():
    diff = _diff({"A @ srv": "PASSED"}, {"A @ srv": "FAILED"})
    assert diff.new_bad == ("A @ srv",)
    assert diff.has_transition


def test_a_recovery_is_a_transition_worth_saying_out_loud():
    diff = _diff({"A @ srv": "FAILED"}, {"A @ srv": "PASSED"})
    assert diff.recovered == ("A @ srv",)
    assert diff.has_transition


def test_a_finding_that_has_not_moved_is_not_a_transition():
    """The 78-message case: the same failure, hour after hour, is not news."""
    diff = _diff({"A @ srv": "FAILED"}, {"A @ srv": "FAILED"})
    assert diff.unchanged_bad == ("A @ srv",)
    assert not diff.has_transition


def test_getting_worse_counts_as_a_transition_but_staying_bad_does_not():
    diff = _diff({"A @ srv": "AT_RISK"}, {"A @ srv": "FAILED"})
    assert diff.worsened == (("A @ srv", "AT_RISK", "FAILED"),)
    assert not diff.new_bad, "it was already bad; calling it new would double-report the same problem"
    assert diff.has_transition


def test_getting_better_without_recovering_is_reported_as_improvement():
    diff = _diff({"A @ srv": "FAILED"}, {"A @ srv": "AT_RISK"})
    assert diff.improved == (("A @ srv", "FAILED", "AT_RISK"),)
    assert not diff.recovered, "still failing its objective — announcing a recovery would be false"


def test_a_policy_that_stopped_being_evaluated_is_not_a_recovery():
    """Retiring a policy or removing a target must not announce a fix that never happened.

    Same class of mistake as reading "we could not connect" as "the service is healthy": both turn
    an absence of measurement into good news.
    """
    diff = _diff({"A @ srv": "FAILED", "B @ srv": "PASSED"}, {"B @ srv": "PASSED"})
    assert diff.vanished_bad == ("A @ srv",)
    assert diff.recovered == ()
    assert not diff.has_transition, "a monitoring change must not page anyone as if it were a service event"


def test_an_unranked_status_cannot_masquerade_as_an_escalation():
    """A status nobody listed ranks below every listed one, so a typo cannot manufacture a page."""
    diff = _diff({"A @ srv": "FAILED"}, {"A @ srv": "WEIRD"})
    assert diff.improved and not diff.worsened


def test_the_first_run_ever_reports_a_baseline_rather_than_a_fleet_wide_outage():
    """With nothing to compare against, every current failure would look brand new. Saying "37 new
    failures" on first run is how a reader learns the alert means nothing."""
    diff = diff_states(None, {"A @ srv": "FAILED"}, severity_order=SEVERITY, healthy=("PASSED",))
    assert diff.baseline
    assert diff.new_bad == ()
    assert diff.unchanged_bad == ("A @ srv",)

    decision = decide_notification(diff, last_sent_at=None, now=NOW)
    assert decision.send and decision.kind == "baseline"


def test_a_clean_first_run_says_nothing():
    diff = diff_states(None, {"A @ srv": "PASSED"}, severity_order=SEVERITY, healthy=("PASSED",))
    assert not decide_notification(diff, last_sent_at=None, now=NOW).send


def test_an_unchanged_backlog_is_silent_until_the_daily_reminder_is_due():
    diff = _diff({"A @ srv": "FAILED"}, {"A @ srv": "FAILED"})

    fresh = decide_notification(diff, last_sent_at=NOW - timedelta(hours=3), now=NOW)
    assert not fresh.send
    assert "next reminder" in fresh.reason, "a suppressed message must be explainable afterwards"

    due = decide_notification(diff, last_sent_at=NOW - timedelta(hours=25), now=NOW)
    assert due.send and due.kind == "reminder"


def test_the_reminder_clock_runs_from_the_last_message_not_the_last_run():
    """If a silent run reset the clock, the daily reminder would be postponed forever by its own
    suppression — something is always suppressing it."""
    diff = _diff({"A @ srv": "FAILED"}, {"A @ srv": "FAILED"})
    last_sent = NOW - timedelta(hours=25)
    for hours_since_run in (1, 2, 3):  # runs happened in between; none of them sent
        decision = decide_notification(diff, last_sent_at=last_sent, now=NOW + timedelta(hours=hours_since_run))
        assert decision.send and decision.kind == "reminder"


def test_a_transition_interrupts_regardless_of_how_recently_we_spoke():
    """Rate limiting a backlog is restraint; rate limiting a new outage is a missed incident."""
    diff = _diff({"A @ srv": "PASSED"}, {"A @ srv": "FAILED"})
    decision = decide_notification(diff, last_sent_at=NOW - timedelta(minutes=1), now=NOW)
    assert decision.send and decision.kind == "transition"


def test_an_unreadable_timestamp_sends_rather_than_swallows():
    """Erring toward a duplicate reminder costs noise; erring the other way loses the only notice
    anyone gets that a failure is still open."""
    diff = _diff({"A @ srv": "FAILED"}, {"A @ srv": "FAILED"})
    assert decide_notification(diff, last_sent_at="not-a-timestamp", now=NOW).send


@pytest.mark.parametrize("stamp", ["2026-08-05T11:00:00Z", "2026-08-05 11:00:00", "2026-08-05T11:00:00+00:00"])
def test_the_stored_timestamp_formats_all_parse(stamp):
    """row_ins_date and send_date are written by different layers in different shapes; a format
    this function cannot read would silently become "never sent" and remind every hour."""
    diff = _diff({"A @ srv": "FAILED"}, {"A @ srv": "FAILED"})
    assert not decide_notification(diff, last_sent_at=stamp, now=NOW).send


def test_notify_always_overrides_every_suppression():
    diff = _diff({"A @ srv": "FAILED"}, {"A @ srv": "FAILED"})
    assert decide_notification(diff, last_sent_at=NOW, now=NOW, always=True).send


def test_the_counts_are_the_ones_the_reader_was_promised():
    diff = _diff(
        {"A @ s": "PASSED", "B @ s": "FAILED", "C @ s": "FAILED", "D @ s": "AT_RISK"},
        {"A @ s": "FAILED", "B @ s": "PASSED", "C @ s": "FAILED", "D @ s": "FAILED"},
    )
    assert diff.counts == {
        "new_failed": 1, "recovered": 1, "worsened": 1, "improved": 0, "unchanged": 1, "vanished": 0,
    }
