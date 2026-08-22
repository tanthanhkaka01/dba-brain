"""Was a restore actually PROVEN lately — the one question backups cannot answer.

"There is a backup" and "we can restore" are different claims, and only one of them can be
tested. db_ops.backup_restore already runs the drills and records every one; nothing turned that
into a health signal, so the reports carried static restore evidence from June while the table
held APPDB_STG_DOCKER whose newest success was 43 days old and nobody said so.

The arithmetic is trivial. What these tests pin are the distinctions a naive "age of last success"
would flatten.
"""

from datetime import datetime, timedelta, timezone

from db_ops.common import restore_drill as rd

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


def _row(db, status, hours_ago):
    return {"database_name": db, "status": status,
            "restore_start": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")}


def test_the_default_window_is_one_week():
    """24*7. Short enough that a break is found before a month-end close depends on it, long
    enough not to thrash a large database nightly."""
    assert rd.DEFAULT_MAX_AGE_HOURS == 24 * 7
    assert rd.max_age_hours({}) == 168


def test_a_recent_successful_drill_is_compliant():
    result = rd.evaluate([_row("SALESDB_Prod", "SUCCESS", 11)], now=NOW)[0]

    assert result["status"] == "OK"
    assert result["ageHours"] == 11.0


def test_a_drill_older_than_the_window_is_a_warning():
    result = rd.evaluate([_row("SALESDB_Prod", "SUCCESS", 200)], now=NOW)[0]

    assert result["status"] == "WARNING"


def test_evidence_more_than_twice_as_old_as_the_policy_is_no_evidence():
    result = rd.evaluate([_row("APPDB_STG_DOCKER", "SUCCESS", 1031)], now=NOW)[0]

    assert result["status"] == "CRITICAL"


def test_never_drilled_is_not_the_same_as_drilled_and_stale():
    """A database nobody has ever restored has no evidence at all; one drilled six weeks ago has
    evidence that expired. Both need action, and they are different actions."""
    never = rd.evaluate([_row("NEW_DB", "FAILED", 5)], now=NOW)[0]

    assert never["status"] == "CRITICAL"
    assert never["ageHours"] is None
    assert "has ever been recorded" in never["reason"]


def test_a_newer_failure_is_not_covered_by_an_older_success():
    """Exactly when somebody believes they are protected and is not."""
    rows = [_row("SALESDB_Prod", "SUCCESS", 20), _row("SALESDB_Prod", "FAILED", 2)]

    result = rd.evaluate(rows, now=NOW)[0]

    assert result["status"] == "CRITICAL"
    assert "most recent attempt FAILED" in result["reason"]
    assert result["lastAttemptStatus"] == "FAILED"


def test_an_older_failure_does_not_spoil_a_newer_success():
    rows = [_row("SALESDB_Prod", "FAILED", 200), _row("SALESDB_Prod", "SUCCESS", 5)]

    assert rd.evaluate(rows, now=NOW)[0]["status"] == "OK"


def test_the_window_can_be_pushed_to_a_different_number():
    """The request object carries it, so a caller can ask a different question without editing
    config."""
    rows = [_row("SALESDB_Prod", "SUCCESS", 100)]

    assert rd.evaluate(rows, override_hours=168, now=NOW)[0]["status"] == "OK"
    assert rd.evaluate(rows, override_hours=6, now=NOW)[0]["status"] == "CRITICAL"


def test_a_database_can_override_the_fleet_window():
    policy = {"defaults": {"max_age_hours": 168},
              "overrides": [{"database": "APPDB_STG_DOCKER", "max_age_hours": 336}]}

    assert rd.max_age_hours(policy, database="APPDB_STG_DOCKER") == 336
    assert rd.max_age_hours(policy, database="SALESDB_Prod") == 168


def test_the_worst_verdict_is_listed_first():
    rows = [_row("GOOD", "SUCCESS", 5), _row("STALE", "SUCCESS", 200), _row("DEAD", "FAILED", 1)]

    verdicts = [r["status"] for r in rd.evaluate(rows, now=NOW)]

    assert verdicts == ["CRITICAL", "WARNING", "OK"]


def test_the_summary_is_the_worst_of_the_fleet():
    rows = [_row("GOOD", "SUCCESS", 5), _row("STALE", "SUCCESS", 200)]

    summary = rd.summarize(rd.evaluate(rows, now=NOW))

    assert summary == {"databases": 2, "compliant": 1, "warning": 1, "critical": 0,
                       "status": "WARNING"}
