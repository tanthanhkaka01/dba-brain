"""Turning capacity history into a date, without inventing one.

The store already held the answer nobody was computing: 192.0.2.115's ``L:`` volume fell from
618 GB free to 163 GB in three days while ``SALESDB``'s log grew 21 -> 428 GB. Every report showed the
current number, so a volume two days from full and one flat for a month rendered identically.

A forecast is only worth having if it is trustworthy in the two cases that break naive ones — a
shrink/regrow spike, and not enough history — so those are what these tests are about.
"""

from datetime import datetime, timedelta, timezone

from db_ops.lib import capacity_forecast as cf

START = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)


def _series(values, *, hours_apart=1.0):
    return [((START + timedelta(hours=i * hours_apart)).strftime("%Y-%m-%dT%H:%M:%SZ"), v)
            for i, v in enumerate(values)]


def test_a_steadily_draining_volume_gets_a_date():
    # 500 GB free, losing 24 GB/day (1 GB/hour) over two days.
    result = cf.forecast(_series([500 - i for i in range(48)]))

    assert result["status"] == "ok"
    assert round(result["per_day"]) == -24
    # 452 GB left at 24 GB/day is a bit under 19 days.
    assert 18 < result["days_to_threshold"] < 20


def test_a_flat_volume_has_no_exhaustion_date():
    """"Never full" must be expressed as no date, not as a huge number that sorts oddly."""
    result = cf.forecast(_series([300.0] * 24))

    assert result["status"] == "ok"
    assert result["per_day"] == 0
    assert result["days_to_threshold"] is None


def test_a_growing_volume_has_no_exhaustion_date():
    result = cf.forecast(_series([100 + i for i in range(24)]))

    assert result["days_to_threshold"] is None


def test_a_shrink_does_not_reverse_the_trend():
    """The case a least-squares fit gets backwards. The volume is draining hard; halfway through,
    someone frees 300 GB. Fitting the whole series reports GROWTH on a volume that is emptying —
    the median of pairwise slopes steps over the jump instead."""
    draining = [500 - i * 5 for i in range(24)]          # -120 GB/day
    after_shrink = [700 - i * 5 for i in range(24)]      # freed, then draining again
    result = cf.forecast(_series(draining + after_shrink))

    assert result["status"] == "ok"
    assert result["per_day"] < 0, "a drained volume must not read as growing"
    assert result["resets"] >= 1
    # Only the post-shrink segment is used, so the reported history is the shorter one.
    assert result["points"] == len(after_shrink)


def test_ordinary_jitter_is_not_mistaken_for_a_shrink():
    """Reset detection keyed on the series' RANGE trimmed an 8-day history down to
    "12 resets seen" and insufficient_history on volumes nobody had touched: a nearly-flat series
    has a tiny range, so normal noise clears any fraction of it. The rule is "many times the
    typical step" instead."""
    values = [300.0 + (0.05 if i % 2 else -0.05) for i in range(60)]

    result = cf.forecast(_series(values))

    assert result["status"] == "ok"
    assert result["resets"] == 0
    assert result["points"] == 60


def test_too_few_samples_refuses_rather_than_reporting_no_growth():
    """The most dangerous output this module could produce is a confident "0 GB/day, never full"
    from two samples."""
    result = cf.forecast(_series([500, 499]))

    assert result["status"] == "insufficient_history"
    assert result["days_to_threshold"] is None if "days_to_threshold" in result else True
    assert "sample" in result["reason"]


def test_samples_crammed_into_a_few_minutes_say_nothing_about_tomorrow():
    result = cf.forecast(_series([500 - i for i in range(20)], hours_apart=0.05))

    assert result["status"] == "insufficient_history"


def test_a_reserve_counts_as_full():
    """"Full" is reaching the reserve, not zero: a volume at its last 50 GB is already an
    incident."""
    with_reserve = cf.forecast(_series([500 - i for i in range(48)]), floor=50.0)
    without = cf.forecast(_series([500 - i for i in range(48)]), floor=0.0)

    assert with_reserve["days_to_threshold"] < without["days_to_threshold"]


def test_a_growing_database_file_projects_to_its_ceiling():
    result = cf.forecast(_series([100 + i for i in range(48)]), direction="up", ceiling=200.0)

    assert result["status"] == "ok"
    assert result["per_day"] > 0
    assert result["days_to_threshold"] is not None


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
def test_horizons_come_from_config_not_from_code():
    policy = {"defaults": {"critical_days_to_full": 10, "warning_days_to_full": 20}}

    assert cf.horizons(policy) == (10, 20)
    assert cf.severity_for(5, policy) == "CRITICAL"
    assert cf.severity_for(15, policy) == "WARNING"
    assert cf.severity_for(500, policy) == "OK"


def test_a_volume_can_override_the_fleet_horizons():
    """A 4 TB data volume and a 60 GB system volume are not the same problem."""
    policy = {
        "defaults": {"critical_days_to_full": 30, "warning_days_to_full": 90, "reserve_gb": 0},
        "overrides": [{"server_id": "ACME-1", "item": "L:\\", "critical_days_to_full": 45,
                       "reserve_gb": 50}],
    }

    assert cf.horizons(policy, server_id="ACME-1", item="L:\\")[0] == 45
    assert cf.reserve_gb(policy, server_id="ACME-1", item="L:\\") == 50
    # A different volume on the same server keeps the fleet default.
    assert cf.horizons(policy, server_id="ACME-1", item="C:\\")[0] == 30


def test_no_date_is_never_a_severity():
    assert cf.severity_for(None) == "OK"


def test_the_refusal_reads_as_a_refusal():
    text = cf.describe(cf.forecast(_series([500, 499])))

    assert "insufficient history" in text
