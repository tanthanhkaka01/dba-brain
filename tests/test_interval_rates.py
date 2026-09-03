"""Pulling a collector's structured fields back out of the message it wrote them into, and
differencing two of those samples into a rate.

A metric row keeps one derived number in ``metric_value``; everything else the collector measured
survives only as ``key=value`` pairs inside ``message``. The report re-reads them from there, so
this parser is shared — a page that split those fields differently from the collector that wrote
them would disagree with the alert about the same sample.

The second half of the module is the subtraction, and every test below it is about one of the ways
that subtraction can lie. A cumulative counter is a total since the engine started, so the only
honest reading of it is a difference — and a difference taken across a restart, across a cleared
counter, or between two samples half a day apart is worse than no reading at all, because it looks
like a measurement. The arithmetic returns nothing in those cases; the caller says "no interval
data" and must never fall back to the since-boot average, which is the mistake the whole mechanism
exists to prevent.
"""

from db_ops.lib import interval_rates as ir


def _sample(stamp, *, since="2026-08-01 03:00:00", **values):
    return {"collected_at": stamp, "counters_since": since, **values}


def test_a_windows_path_does_not_swallow_the_next_field():
    """The value contains backslashes and a colon, and the field after it must still be found. A
    path is the most common value in these messages and the easiest one for a parser to run past."""
    fields = ir.message_fields(
        r"database=SALESDB, file_type=ROWS, file=E:\MSSQL15\DATA\SALESDB.mdf, reads=3590421")

    assert fields["file"] == r"E:\MSSQL15\DATA\SALESDB.mdf"
    assert fields["reads"] == "3590421"
    assert fields["file_type"] == "ROWS"


def test_a_value_containing_an_equals_sign_is_not_read_as_a_new_key():
    """Error texts carry ``=``. The key is anchored on a comma or whitespace precisely so the tail
    of a value cannot be mistaken for the start of the next field."""
    fields = ir.message_fields("status=ERROR, detail=login failed for user=sa, code=18456")

    assert fields["detail"] == "login failed for user=sa"
    assert fields["code"] == "18456"


def test_keys_are_lower_cased_so_a_reader_never_guesses_the_casing():
    assert ir.message_fields("Database=SALESDB, File_Type=LOG")["file_type"] == "LOG"


def test_a_message_with_no_fields_is_an_empty_mapping_not_an_error():
    """Most metrics write prose. Asking one of those for its fields must be answerable."""
    assert ir.message_fields("SQL returned no rows.") == {}
    assert ir.message_fields(None) == {}


# --------------------------------------------------------------------------- #
# Differencing two samples
# --------------------------------------------------------------------------- #


def test_the_newest_pair_is_what_is_happening_now():
    samples = [
        _sample("2026-09-03T08:00:00+00:00", cpu=1_250_000_000),
        _sample("2026-09-03T08:15:00+00:00", cpu=1_260_000_000),
        _sample("2026-09-03T08:30:00+00:00", cpu=1_267_500_000),
    ]

    delta = ir.interval_delta(samples, fields=["cpu"])

    assert delta["deltas"]["cpu"] == 7_500_000
    assert delta["seconds"] == 900
    assert ir.per_second(delta)["cpu"] == 7_500_000 / 900


def test_an_hour_is_the_widest_pair_inside_the_hour_not_the_nearest_one():
    """The failure this prevents is a four-fold understatement of everything on the page: an hour
    asked for and the last 15 minutes answered."""
    samples = [_sample(f"2026-09-03T08:{m:02d}:00+00:00", cpu=1_000_000 + m * 10_000)
               for m in (0, 15, 30, 45)]
    samples.append(_sample("2026-09-03T09:00:00+00:00", cpu=1_600_000))

    hour = ir.window_delta(samples, fields=["cpu"], window_hours=1.0)

    assert hour["seconds"] == 3600
    assert hour["deltas"]["cpu"] == 600_000
    # The same samples read as "now" give a quarter of it, which is the point of having both.
    assert ir.interval_delta(samples, fields=["cpu"])["deltas"]["cpu"] == 150_000


def test_a_restart_between_the_two_samples_is_refused_not_reported():
    """After a restart the totals begin again at zero, so the difference across one is the new
    absolute value — a burst of work that never happened. A busy instance can pass its own previous
    totals within the hour, so only the carried baseline marker can catch this."""
    samples = [
        _sample("2026-09-03T08:00:00+00:00", since="2025-10-27 02:00:00", cpu=1_250_000_000),
        _sample("2026-09-03T08:15:00+00:00", since="2026-09-03 08:05:00", cpu=40_000),
    ]

    assert ir.interval_delta(samples, fields=["cpu"]) is None


def test_a_counter_that_went_backwards_is_refused_even_with_the_same_baseline():
    """DBCC SQLPERF clears the wait counters without restarting the engine, and a failover moves
    the plan cache. The marker cannot see either; the negative difference can."""
    samples = [
        _sample("2026-09-03T08:00:00+00:00", waits=900_000),
        _sample("2026-09-03T08:15:00+00:00", waits=1_200),
    ]

    assert ir.interval_delta(samples, fields=["waits"]) is None


def test_a_pair_wider_than_the_window_is_refused_rather_than_stretched():
    """A delta over 20 hours is not "now"; calling it that is the same averaging mistake in a
    shorter timeframe."""
    samples = [
        _sample("2026-09-02T12:00:00+00:00", cpu=1_000_000),
        _sample("2026-09-03T08:00:00+00:00", cpu=9_000_000),
    ]

    assert ir.interval_delta(samples, fields=["cpu"], max_pair_hours=3.0) is None


def test_a_window_that_cannot_be_half_filled_reports_nothing():
    """Two samples 20 minutes apart do not describe an hour. Reporting them as one would understate
    every figure by two thirds while looking exactly like a full reading."""
    samples = [
        _sample("2026-09-03T08:40:00+00:00", cpu=1_000_000),
        _sample("2026-09-03T09:00:00+00:00", cpu=1_100_000),
    ]

    assert ir.window_delta(samples, fields=["cpu"], window_hours=1.0) is None
    # The same pair is a perfectly good "right now" reading, and is offered as one.
    assert ir.interval_delta(samples, fields=["cpu"])["seconds"] == 1200


def test_one_unusable_sample_costs_precision_and_not_the_answer():
    """A single failed collection sits between two good ones. Walking back past it keeps the
    reading; refusing outright would blank the page for one missed run."""
    samples = [
        _sample("2026-09-03T08:00:00+00:00", cpu=1_000_000),
        _sample("2026-09-03T08:15:00+00:00", cpu=None),
        _sample("2026-09-03T08:30:00+00:00", cpu=1_050_000),
    ]

    delta = ir.interval_delta(samples, fields=["cpu"])

    assert delta["deltas"]["cpu"] == 50_000
    assert delta["seconds"] == 1800


def test_a_rate_is_computed_against_the_span_that_was_measured():
    """Not against the cadence the metric is configured at. A worker that ran a minute late would
    otherwise inflate every rate on the page by that minute's worth of work."""
    samples = [
        _sample("2026-09-03T08:00:00+00:00", reads=0),
        _sample("2026-09-03T08:16:40+00:00", reads=1_000_000),   # 1000 s, not the configured 900
    ]

    delta = ir.interval_delta(samples, fields=["reads"])

    assert delta["seconds"] == 1000
    assert ir.per_second(delta)["reads"] == 1000.0


def test_no_pair_returns_none_so_a_caller_cannot_mistake_it_for_zero():
    assert ir.interval_delta([], fields=["cpu"]) is None
    assert ir.interval_delta([_sample("2026-09-03T08:00:00+00:00", cpu=1)], fields=["cpu"]) is None
    assert ir.per_second(None) == {}


def test_io_latency_over_an_interval_is_the_stall_the_interval_actually_saw():
    """The collector's own avg_read_latency_ms is the average since the engine started. On
    one production instance that was nine months, so a bad afternoon last winter kept the tile red while a
    problem starting this morning was diluted to invisibility."""
    samples = [
        _sample("2026-09-03T08:00:00+00:00", reads=1_000_000, writes=500_000,
                io_stall_read_ms=10_000_000, io_stall_write_ms=2_000_000),
        _sample("2026-09-03T08:15:00+00:00", reads=1_001_000, writes=500_500,
                io_stall_read_ms=10_180_000, io_stall_write_ms=2_002_500),
    ]

    interval = ir.io_latency_interval(samples)

    assert interval["readLatencyMs"] == 180.0     # 180,000 ms over 1,000 reads
    assert interval["writeLatencyMs"] == 5.0
    assert interval["readIops"] == round(1000 / 900, 1)


def test_a_file_with_no_io_in_the_interval_reports_no_latency_rather_than_zero():
    """"Nothing happened" is not "instant", and a 0 ms tile on an idle file reads as the fastest
    storage in the estate."""
    samples = [
        _sample("2026-09-03T08:00:00+00:00", reads=10, writes=0,
                io_stall_read_ms=100, io_stall_write_ms=0),
        _sample("2026-09-03T08:15:00+00:00", reads=10, writes=0,
                io_stall_read_ms=100, io_stall_write_ms=0),
    ]

    interval = ir.io_latency_interval(samples)

    assert interval["readLatencyMs"] is None
    assert interval["writeLatencyMs"] is None


def test_one_counter_going_backwards_can_be_dropped_without_losing_the_others():
    """The plan cache's exception. sys.dm_exec_query_stats loses a plan's totals the moment that
    plan is evicted, so one field falling while the rest rise is normal there and a reset nowhere
    else. Measured on a production instance on 2026-09-03: physical reads fell by 209 while logical reads
    rose by 220 million, and refusing the pair blanked all six plan-cache figures."""
    samples = [
        _sample("2026-09-03T08:00:00+00:00", cpu=100, physical_reads=603_890),
        _sample("2026-09-03T08:15:00+00:00", cpu=250, physical_reads=603_681),
    ]

    assert ir.interval_delta(samples, fields=["cpu", "physical_reads"]) is None

    partial = ir.interval_delta(samples, fields=["cpu", "physical_reads"], allow_partial=True)
    assert partial["deltas"] == {"cpu": 150}
    assert partial["dropped"] == ["physical_reads"]


def test_every_field_going_backwards_is_a_reset_even_when_partials_are_allowed():
    """One field falling is an eviction; all of them falling is DBCC FREEPROCCACHE or a failover,
    and there is no interval to report across it."""
    samples = [
        _sample("2026-09-03T08:00:00+00:00", cpu=1_000, reads=5_000),
        _sample("2026-09-03T08:15:00+00:00", cpu=12, reads=40),
    ]

    assert ir.interval_delta(samples, fields=["cpu", "reads"], allow_partial=True) is None
