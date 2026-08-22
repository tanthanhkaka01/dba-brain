"""Scheduling: a declared interval has to be one the system can actually deliver.

The catalog is a promise — "this metric runs every 60 seconds" is what the UI shows and what an
alerting SLO is written against. Nothing here runs continuously, though: a metric is only tested
for due-ness when the daemon sweeps, so the arithmetic around that boundary decides whether the
promise is kept or quietly halved.
"""

# --------------------------------------------------------------------------- #
# Due-time grace: a declared interval has to be achievable
# --------------------------------------------------------------------------- #
def test_a_metric_a_hair_short_of_its_interval_is_due_now_not_a_cycle_later():
    """Nothing runs continuously — a metric is only tested for due-ness when the daemon sweeps,
    every 300s. With an exact comparison a 300-second metric checked 299.4 seconds after its last
    run was "not due" and waited for the NEXT sweep, so it ran every 600. Measured on both audited
    targets: DATABASE_STATUS declared 300s ran at a median of 600s.
    """
    from datetime import datetime, timedelta, timezone
    from db_ops.lib.time_window import repeat_due

    now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
    last_run = now - timedelta(seconds=299.4)

    assert repeat_due(last_run, 300, now) is True


def test_the_grace_never_shortens_an_interval_meaningfully():
    """It absorbs sweep drift; it must not turn a 5-minute metric into a 4-minute one."""
    from datetime import datetime, timedelta, timezone
    from db_ops.lib.time_window import repeat_due

    now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
    # 5% of 300s is 15s, so 280s in is still not due.
    assert repeat_due(now - timedelta(seconds=280), 300, now) is False


def test_the_grace_is_capped_for_long_intervals():
    """5% of a 20-hour maintenance interval would be an hour of slack, which is not drift."""
    from datetime import datetime, timedelta, timezone
    from db_ops.lib.time_window import DUE_GRACE_MAX_SECONDS, repeat_due

    now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
    interval = 72000
    assert repeat_due(now - timedelta(seconds=interval - DUE_GRACE_MAX_SECONDS - 1), interval, now) is False
    assert repeat_due(now - timedelta(seconds=interval - DUE_GRACE_MAX_SECONDS), interval, now) is True


def test_run_once_and_manual_are_unaffected_by_the_grace():
    from datetime import datetime, timedelta, timezone
    from db_ops.lib.time_window import MANUAL_ONLY, RUN_ONCE, repeat_due

    now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
    assert repeat_due(now - timedelta(days=9), RUN_ONCE, now) is False
    assert repeat_due(None, MANUAL_ONLY, now) is False
