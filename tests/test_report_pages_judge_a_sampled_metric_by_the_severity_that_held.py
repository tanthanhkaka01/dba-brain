"""The report pages judge CPU, the disk queue and the wait sample by the severity that held an hour.

The collector grades each of these from one sample - a CPU reading, a queue length, five seconds of
waits - and that is right for an alert, which is about the instant. A report page is about the
server (the operator, 2026-10-26: alerts judge the instant and never read the store; the inventory
and the server page read only the store and may judge differently). One sample at 95 % is a
moment; half an hour of them is the server.

The thresholds stay the collector's, per metric and per server, remaps included: the page reads the
severity the collector stored on each sample and keeps the one at least half of the hour reached.
Copying the thresholds into the page would have made a second rule to keep in step with the first.
"""

from dbabrain.lib import report_windows
from dbabrain.reports import inventory_health
from dbabrain.reports.server_health import build_areas, build_problems
from dbabrain.reports.server_series import _apply_report_window


def _cpu_row(minute, value, status, *, item="cpu_usage", code="OS_CPU_USAGE", hour=9):
    return {"server_id": "SRV-1", "metric_code": code, "metric_item": item,
            "metric_value": str(value), "metric_unit": "percent", "status": status,
            "message": f"cpu_usage={value}",
            "collected_at": f"2026-10-26T{hour + minute // 60:02d}:{minute % 60:02d}:00+00:00"}


def _entry(rows):
    newest = rows[-1]
    return {"code": newest["metric_code"], "label": "CPU", "item": newest["metric_item"],
            "unit": newest["metric_unit"], "status": newest["status"], "numeric": True,
            "static": False, "last": float(newest["metric_value"]),
            "lastText": newest["metric_value"], "message": newest["message"], "lastAt": 0,
            "points": [], "tier": "primary"}


def test_one_sample_over_the_line_is_a_moment_not_the_hour():
    rows = [_cpu_row(m, 20, "OK") for m in range(0, 50, 10)] + [_cpu_row(55, 96, "CRITICAL")]
    entry = _entry(rows)
    _apply_report_window(entry, rows, {})

    assert entry["status"] == "OK"
    area = next(a for a in build_areas([entry]) if a["key"] == "cpu")
    assert area["status"] == "OK"
    assert "1 h avg 32.67%, peak 96%, 1/6 not OK" in area["detail"]


def test_half_an_hour_over_the_line_is_the_server_even_when_the_newest_sample_is_calm():
    rows = [_cpu_row(m, 92, "CRITICAL") for m in range(0, 40, 10)] + [_cpu_row(m, 30, "OK")
                                                                       for m in (40, 50)]
    entry = _entry(rows)
    _apply_report_window(entry, rows, {})

    assert entry["status"] == "CRITICAL"
    assert entry["last"] == 71.33
    detail = build_problems([entry], now=0)[0]["items"][0]["detail"]
    assert detail.startswith("Graded on the last hour: the severity the collector gave")


def test_a_remap_that_silenced_the_samples_silences_the_hour():
    """The page must not un-silence what a severity_map decided: the samples' stored status is
    the remapped one, and the hour is read from those."""
    rows = [_cpu_row(m, 95, "LOGGING") for m in range(0, 60, 10)]
    entry = _entry(rows)
    _apply_report_window(entry, rows, {})

    assert entry["status"] == "OK"


def test_the_held_severity_is_the_highest_at_least_half_reached():
    held = report_windows.held_severity

    assert held(["CRITICAL", "WARNING", "OK", "OK"]) == "WARNING"
    assert held(["CRITICAL", "CRITICAL", "OK", "OK"]) == "CRITICAL"
    assert held(["CRITICAL", "OK", "OK", "OK"]) == "OK"
    assert held(["ERROR", "ERROR", "OK"]) == "CRITICAL"
    assert held(["CRITICAL"]) == "CRITICAL"
    assert held([]) == "OK"


def test_one_hourly_reading_of_two_does_not_hold_the_hour():
    """CPU is read once an hour, so its hour holds two readings: 90 % an hour ago and 74 % now was
    a CRITICAL host on the fleet page (2026-11-06). One of several samples is a moment."""
    held = report_windows.held_severity

    assert held(["CRITICAL", "OK"]) == "OK"
    assert held(["OK", "CRITICAL"]) == "OK"
    assert held(["CRITICAL", "CRITICAL"]) == "CRITICAL"


def test_a_wait_finding_the_hour_did_not_sustain_is_lowered_and_never_raised():
    """The wait sample names a different top wait in each collection, so it is read per
    collection; it has no item of its own to raise, and it lowers one the hour did not hold."""
    def wait(minute, item, status):
        return {"server_id": "SRV-1", "metric_code": "PERFORMANCE_WAIT_STATS", "metric_item": item,
                "metric_value": "1", "metric_unit": "seconds", "status": status,
                "message": f"top_wait_type={item}",
                "collected_at": f"2026-10-26T09:{minute:02d}:00+00:00"}
    rows = [wait(m, "CXCONSUMER", "OK") for m in range(0, 50, 10)] + [wait(55, "PAGEIOLATCH_SH",
                                                                           "WARNING")]
    newest = rows[-1]
    entry = {**_entry([newest]), "label": "Wait statistics", "unit": "seconds"}
    _apply_report_window(entry, [newest], {"CXCONSUMER": rows[:-1], "PAGEIOLATCH_SH": [newest]})

    assert entry["status"] == "OK"
    assert "1 of 6 samples not OK - held OK" in entry["windowSummary"]

    sustained = [wait(m, "PAGEIOLATCH_SH", "WARNING") for m in range(0, 60, 10)]
    calm = {**sustained[-1], "status": "OK"}
    judged = report_windows.rejudge("PERFORMANCE_WAIT_STATS", sustained[:-1] + [calm])
    assert report_windows.judged_status("OK", judged) == "OK"


def test_the_inventory_lists_a_cpu_the_hour_held_though_its_newest_sample_was_fine():
    rows = [_cpu_row(m, 92, "CRITICAL") for m in range(0, 40, 10)] + [_cpu_row(m, 30, "OK")
                                                                       for m in (40, 50)]
    history = report_windows.history_by_item(rows)
    windowed = inventory_health._windowed("SRV-1", rows[-1], history)

    assert windowed["status"] == "CRITICAL"
    assert windowed["metric_value"] == "71.33"
    problems = inventory_health.build_metric_problems([windowed], now=0)
    assert problems[0]["severity"] == "CRITICAL"


def test_the_disk_queue_is_judged_and_the_volumes_beside_it_are_not():
    assert report_windows.is_judged("OS_DISK_USAGE", "disk_queue_length")
    assert not report_windows.is_judged("OS_DISK_USAGE", "C:")
    assert not report_windows.is_judged("OS_CPU_USAGE", "load_average")
