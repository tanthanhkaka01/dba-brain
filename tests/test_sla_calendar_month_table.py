"""Availability by calendar month table for SLA reporting.

The SLA app measures rolling windows (24h, 30d). For monthly compliance reporting,
availability is also evaluated across calendar months: this month to date and the closed
previous month.

Key behaviors tested:
1. Month boundaries follow the node's display timezone (dbabrain.lib.timezone.display_zone).
2. The closed previous month reads archive rows once, writes a JSON cache file at
   <runtime_dir>/sla/months/<policy_id>_<YYYY-MM>.json, and subsequent calls read the cache
   without scanning the archive again.
3. The published SLA HTML page displays the "Availability by calendar month" section only
   when at least one evaluated policy has calendar_month_report: true.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dbabrain.lib.timezone import display_zone
from dbabrain.metrics.models import MetricResult
from dbabrain.metrics.storage import MetricStore
from dbabrain.sla.compliance import validate_sla_policies
from dbabrain.sla.models import SlaPolicy
from dbabrain.sla.months import (
    get_month_availability,
    month_boundaries,
    previous_month_cache_path,
    render_calendar_month_section,
)
from dbabrain.sla.publish import render_html
from dbabrain.sla.storage import SlaStore


def _make_policy(**overrides) -> SlaPolicy:
    kwargs = {
        "policy_id": "INSTANCE_AVAILABILITY_30D",
        "name": "Instance availability (30-day)",
        "target_ids": ("server/sqlserver/master",),
        "metric_codes": ("INSTANCE_STATUS",),
        "aggregation": "minutes",
        "policy_model": "time_slo",
        "objective_percent": 99.9,
        "window_hours": 720,
        "good_statuses": ("OK", "LOGGING"),
        "calendar_month_report": True,
    }
    kwargs.update(overrides)
    return SlaPolicy(**kwargs)


def test_month_boundaries_follow_display_timezone() -> None:
    """Month boundaries are aligned with 00:00:00 on the 1st of the month in the display timezone."""
    tz = display_zone()

    # Mid-year test date
    ref_dt = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    utc_prev_start, utc_this_start, utc_now = month_boundaries(ref_dt)

    assert utc_now == ref_dt
    # Local representation in display zone
    local_this = utc_this_start.astimezone(tz)
    assert local_this.year == 2026
    assert local_this.month == 5
    assert local_this.day == 1
    assert local_this.hour == 0
    assert local_this.minute == 0
    assert local_this.second == 0

    local_prev = utc_prev_start.astimezone(tz)
    assert local_prev.year == 2026
    assert local_prev.month == 4
    assert local_prev.day == 1
    assert local_prev.hour == 0
    assert local_prev.minute == 0

    # January transition test date (previous month is December of previous year)
    jan_ref = datetime(2026, 1, 10, 8, 0, 0, tzinfo=timezone.utc)
    utc_prev_start_jan, utc_this_start_jan, _ = month_boundaries(jan_ref)
    local_prev_jan = utc_prev_start_jan.astimezone(tz)
    assert local_prev_jan.year == 2025
    assert local_prev_jan.month == 12
    assert local_prev_jan.day == 1


def test_previous_month_reads_archive_and_caches_to_json(tmp_path: Path) -> None:
    """The previous month queries metric_results_archive, writes a cache file, and reuses it."""
    sqlite_path = tmp_path / "months.sqlite"
    metric_store = MetricStore(sqlite_path)
    metric_store.initialize()

    # Fixed reference date: 2026-05-15T12:00:00Z -> previous month is April 2026
    window_end = "2026-05-15T12:00:00Z"
    target_id = "server/sqlserver/master"

    # Insert archived samples into metric_results_archive for April 2026:
    # 5 good minutes, 1 bad minute
    archive_rows = []
    for m in range(5):
        archive_rows.append(
            (1, target_id, "server", "192.0.2.1", "sqlserver", "master", "INSTANCE_STATUS", "master", "1", "", "OK", 5, f"2026-04-10T10:{m:02d}:30Z")
        )
    archive_rows.append(
        (1, target_id, "server", "192.0.2.1", "sqlserver", "master", "INSTANCE_STATUS", "master", "1", "", "ERROR", 5, "2026-04-10T10:05:30Z")
    )

    with metric_store.connect() as conn:
        conn.executemany(
            """INSERT INTO metric_results_archive
               (run_id, target_id, server_id, ip, db_type, db_name, metric_code, metric_item, metric_value, metric_unit, status, importance, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            archive_rows,
        )

    # Insert 2 good minutes for current month into live metric_results
    live_rows = [
        MetricResult(
            target_id=target_id,
            server_id="server",
            ip="192.0.2.1",
            db_type="sqlserver",
            db_name="master",
            metric_code="INSTANCE_STATUS",
            metric_item="master",
            metric_value="1",
            metric_unit="",
            status="OK",
            importance=5,
            message=None,
            collected_at=f"2026-05-02T08:{m:02d}:30Z",
        )
        for m in range(2)
    ]
    metric_store.insert_results(run_id=1, results=live_rows)

    runtime_dir = tmp_path / "runtime"
    sla_store = SlaStore(sqlite_path)
    policy = _make_policy()

    # First run: computes from archive and writes cache file
    month_data = get_month_availability(
        sla_store,
        policy,
        runtime_dir=runtime_dir,
        window_end=window_end,
    )

    stats = month_data["target_stats"][target_id]
    assert stats["prev_month"].observed_minutes == 6
    assert stats["prev_month"].good_minutes == 5
    assert stats["prev_month"].down_minutes == 1
    assert stats["this_month"].observed_minutes == 2
    assert stats["this_month"].good_minutes == 2

    # Verify cache file exists
    cache_file = previous_month_cache_path(runtime_dir, policy.policy_id, "2026-04")
    assert cache_file.is_file(), f"Expected cache file at {cache_file}"

    cache_content = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cache_content["month"] == "2026-04"
    assert target_id in cache_content["targets"]
    assert cache_content["targets"][target_id]["observed"] == 6
    assert cache_content["targets"][target_id]["good"] == 5

    # Second run: clear metric_results_archive entirely from database
    with metric_store.connect() as conn:
        conn.execute("DELETE FROM metric_results_archive")

    # Second call should read from cache without querying the empty archive
    month_data_cached = get_month_availability(
        sla_store,
        policy,
        runtime_dir=runtime_dir,
        window_end=window_end,
    )
    stats_cached = month_data_cached["target_stats"][target_id]
    assert stats_cached["prev_month"].observed_minutes == 6
    assert stats_cached["prev_month"].good_minutes == 5


def test_page_omits_calendar_section_when_no_policy_asks_for_it(tmp_path: Path) -> None:
    """The calendar month section appears on the SLA page only when an active policy requests it."""
    sqlite_path = tmp_path / "pub_check.sqlite"
    metric_store = MetricStore(sqlite_path)
    metric_store.initialize()
    sla_store = SlaStore(sqlite_path)

    policy_no_report = _make_policy(
        policy_id="INSTANCE_AVAILABILITY_24H",
        calendar_month_report=False,
    )
    policy_with_report = _make_policy(
        policy_id="INSTANCE_AVAILABILITY_30D",
        calendar_month_report=True,
    )

    runtime_dir = tmp_path / "runtime"

    # 1. render_calendar_month_section directly
    assert render_calendar_month_section([policy_no_report], sla_store, runtime_dir=runtime_dir) == ""
    assert render_calendar_month_section([policy_with_report], sla_store, runtime_dir=runtime_dir) != ""

    # A real summary of the same (empty) store, so the page is rendered from what the run produces.
    summary = validate_sla_policies(sqlite_path=sqlite_path, policies=[policy_no_report],
                                    window_end="2026-05-15T12:00:00Z")

    # 2. render_html without calendar month policy
    page_without = render_html(summary, recent_runs=[], policies=[policy_no_report], store=sla_store,
                               runtime_dir=runtime_dir)
    assert "Availability by calendar month" not in page_without

    # 3. render_html with calendar month policy
    page_with = render_html(summary, recent_runs=[], policies=[policy_with_report], store=sla_store,
                            runtime_dir=runtime_dir)
    assert "Availability by calendar month" in page_with
    # Nothing was measured, so no month was cached.
    assert not (runtime_dir / "sla" / "months").exists()


def test_without_a_runtime_folder_no_month_is_cached_anywhere(tmp_path: Path) -> None:
    """No runtime folder named, no file written - least of all into the repository's own runtime/."""
    assert previous_month_cache_path(None, "INSTANCE_AVAILABILITY_30D", "2026-04") is None
