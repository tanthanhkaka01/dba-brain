"""The report pages judge disk latency and page life expectancy on the last hour of stored rows.

The operator's rule (2026-10-26): the collector and the alert reports alert on the instant and
never read the store; the inventory and the server page read only the store, and may judge a
metric differently. Both pages used to grade the newest row of these two metrics. For disk latency
that row is the average since the engine started, so a production data file read CRITICAL 72.95 ms
eight days after the stall that made it, at 2.3 ms over the last hour - while on another server a
file at 1752 ms over the last hour was missing from the list, its since-boot average still low.

So the pages difference the counters the store already holds (``dbabrain.lib.report_windows``):
graded on the last hour, the latest interval and the last 24 hours printed beside it, the
since-boot figure printed and never graded, and no pair - a restart, one collection - means no
grade rather than the since-boot average.
"""

from pathlib import Path

from dbabrain.lib import health_model, interval_rates, report_windows
from dbabrain.reports import inventory_health
from dbabrain.reports.server_health import build_areas, build_problems
from dbabrain.reports.server_series import _apply_report_window

COLLECTORS = Path(__file__).resolve().parents[1] / "dbabrain" / "metrics" / "collectors"
SINCE = "2026-10-18 10:11:44"


def _io_row(minute, *, reads, writes, stall_read, stall_write, since=SINCE, hour=9):
    stamp = f"2026-10-26T{hour + minute // 60:02d}:{minute % 60:02d}:00+00:00"
    avg_read = round(stall_read / reads, 2) if reads else 0
    avg_write = round(stall_write / writes, 2) if writes else 0
    return {
        "server_id": "SRV-1", "metric_code": "PERFORMANCE_IO_LATENCY",
        "metric_item": "SALESDB / ROWS / D:\\DATA\\SALESDB.mdf",
        "metric_value": str(max(avg_read, avg_write)), "metric_unit": "ms", "status": "OK",
        "collected_at": stamp,
        "message": (f"database=SALESDB, file_type=ROWS, avg_read_latency_ms={avg_read}, "
                    f"avg_write_latency_ms={avg_write}, reads={reads}, writes={writes}, "
                    f"io_stall_read_ms={stall_read}, io_stall_write_ms={stall_write}, "
                    f"counters_since={since}, sample_enough=YES, file=D:\\DATA\\SALESDB.mdf"),
    }


def _slow_history_fast_hour():
    """A file that stalled long ago - 70 ms since boot - and has read at 2 ms all this hour."""
    rows, reads, stall = [], 100_000, 7_000_000
    for minute in range(0, 61, 15):
        rows.append(_io_row(minute, reads=reads, writes=1000, stall_read=stall, stall_write=1000))
        reads += 1000
        stall += 2000
    return rows


def test_a_file_slow_since_boot_and_fast_this_hour_is_graded_on_the_hour():
    judged = report_windows.rejudge("PERFORMANCE_IO_LATENCY", _slow_history_fast_hour())

    assert judged["value"] == 2.0
    assert health_model.current_severity(code="PERFORMANCE_IO_LATENCY", status="OK",
                                         value=judged["value"]) == "OK"
    # The since-boot figure is printed, for what it is, and graded nowhere.
    assert "Since the engine started (2026-10-18 10:11:44), not graded: read 6" in judged["summary"]


def test_a_stall_this_hour_is_found_though_the_average_since_boot_is_low():
    """The other half of the same mistake: a long, healthy history dilutes a stall happening now."""
    rows, reads, stall = [], 10_000_000, 20_000_000
    for minute in range(0, 61, 15):
        rows.append(_io_row(minute, reads=reads, writes=1000, stall_read=stall, stall_write=1000))
        reads += 400
        stall += 400 * 300
    judged = report_windows.rejudge("PERFORMANCE_IO_LATENCY", rows)

    assert float(rows[-1]["metric_value"]) < 3
    assert judged["value"] == 300.0
    assert health_model.current_severity(code="PERFORMANCE_IO_LATENCY", status="OK",
                                         value=judged["value"]) == "CRITICAL"


def test_a_direction_with_too_few_ios_is_not_graded():
    """Three writes that waited on a cold disk are three writes, not a write latency."""
    rows = [_io_row(0, reads=1000, writes=10, stall_read=2000, stall_write=10),
            _io_row(30, reads=2000, writes=13, stall_read=4000, stall_write=3010),
            _io_row(60, reads=3000, writes=15, stall_read=6000, stall_write=5010)]
    judged = report_windows.rejudge("PERFORMANCE_IO_LATENCY", rows)

    assert judged["windows"]["hour"]["writeMs"] == 1000.0
    assert judged["value"] == 2.0


def test_an_idle_hour_has_no_grade_and_says_why():
    rows = [_io_row(0, reads=1000, writes=1000, stall_read=90_000, stall_write=90_000),
            _io_row(60, reads=1005, writes=1002, stall_read=90_500, stall_write=90_200)]
    judged = report_windows.rejudge("PERFORMANCE_IO_LATENCY", rows)

    assert judged["value"] is None
    assert judged["text"] == "under 20 I/Os in 60 min"


def test_counters_across_a_restart_are_never_graded_and_never_fall_back_to_the_average():
    rows = [_io_row(0, reads=500_000, writes=1000, stall_read=50_000_000, stall_write=1000),
            _io_row(45, reads=600, writes=100, stall_read=600, stall_write=100,
                    since="2026-10-26 09:30:00")]
    judged = report_windows.rejudge("PERFORMANCE_IO_LATENCY", rows)

    assert judged["value"] is None
    assert judged["text"] == "no interval yet"
    assert interval_rates.consecutive_deltas(report_windows.io_samples(rows),
                                             fields=report_windows.IO_FIELDS) == []


def test_the_chart_draws_each_interval_not_the_running_average():
    rows = _slow_history_fast_hour()
    points = report_windows.io_interval_points(rows)

    assert [value for _epoch, value in points] == [2.0, 2.0, 2.0, 2.0]


def test_page_life_expectancy_is_graded_on_the_hours_average_not_one_reading():
    """One scan drops the gauge to near zero for a reading; an hour of readings is the profile."""
    readings = [3000, 2900, 3100, 3050, 120]
    rows = [{"server_id": "SRV-1", "metric_code": "PAGE_LIFE_EXPECTANCY",
             "metric_item": "page_life_expectancy", "metric_value": str(value),
             "metric_unit": "seconds", "status": "OK", "message": "Buffer Manager Page Life Expectancy",
             "collected_at": f"2026-10-26T09:{minute:02d}:00+00:00"}
            for minute, value in zip(range(0, 50, 10), readings)]
    judged = report_windows.rejudge("PAGE_LIFE_EXPECTANCY", rows)

    assert judged["windows"]["now"]["latest"] == 120
    assert judged["value"] == 2434
    assert health_model.current_severity(code="PAGE_LIFE_EXPECTANCY", status="OK",
                                         value=judged["value"]) == "OK"


def test_the_server_page_tile_and_problem_read_the_window():
    rows = [_io_row(0, reads=10_000, writes=1000, stall_read=100_000, stall_write=1000),
            _io_row(15, reads=10_400, writes=1000, stall_read=124_000, stall_write=1000),
            _io_row(30, reads=10_800, writes=1000, stall_read=148_000, stall_write=1000),
            _io_row(45, reads=11_200, writes=1000, stall_read=172_000, stall_write=1000),
            _io_row(60, reads=11_600, writes=1000, stall_read=196_000, stall_write=1000)]
    entry = {"code": "PERFORMANCE_IO_LATENCY", "label": "Disk latency", "item": rows[0]["metric_item"],
             "unit": "ms", "status": "OK", "numeric": True, "static": False,
             "last": float(rows[-1]["metric_value"]), "lastText": rows[-1]["metric_value"],
             "message": rows[-1]["message"], "lastAt": 0, "points": [], "tier": "primary"}
    _apply_report_window(entry, rows, {})

    assert entry["last"] == 60.0
    area = next(a for a in build_areas([entry]) if a["key"] == "disk_latency")
    assert area["status"] == "CRITICAL"
    assert area["value"] == "60 ms"
    # An hour of rows fills no 24-hour window, and the line does not pretend it does.
    assert area["capacity"] == "latest 60.0 / — ms · 1 h 60.0 / — ms (read / write)"
    detail = build_problems([entry], now=0)[0]["items"][0]["detail"]
    assert detail.startswith("Graded on the last hour")
    assert "| database=SALESDB" in detail


def test_the_inventory_judges_its_snapshot_row_on_the_same_window():
    rows = _slow_history_fast_hour()
    history = report_windows.history_by_item(rows)
    windowed = inventory_health._windowed("SRV-1", rows[-1], history)

    assert rows[-1]["metric_value"] == "70.0" or float(rows[-1]["metric_value"]) > 60
    assert windowed["metric_value"] == "2"
    assert windowed["message"].startswith("Graded on the last hour")
    problems = inventory_health.build_metric_problems([windowed], now=0)
    assert problems == []


def test_a_row_of_any_other_metric_is_left_alone():
    row = {"metric_code": "OS_MEMORY_USAGE", "metric_item": "memory_usage", "metric_value": "97"}

    assert inventory_health._windowed("SRV-1", row, {}) is row


def test_both_oracle_variants_carry_the_counters_a_window_needs():
    """Without the raw totals in the message an Oracle file has no window and no grade."""
    for path in ("oracle/019_oracle_io_latency.sql", "oracle/legacy_8i/019_oracle_io_latency.sql"):
        text = (COLLECTORS / path).read_text(encoding="utf-8")
        for field in ("reads=", "writes=", "io_stall_read_ms=", "io_stall_write_ms=",
                      "counters_since="):
            assert f"', {field}'" in text or f", {field}" in text, (path, field)
