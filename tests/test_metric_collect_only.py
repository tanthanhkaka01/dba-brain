"""Why some metrics are collected but kept out of the scheduled reports.

Most metrics are a signal: a row means something needs attention. A few are an **inventory** —
MAINTENANCE_INDEX_USAGE emits one row per index, roughly 29,000 on a single large database, and by
design almost all of them are status OK. Those rows belong in the store, where the inventory report
renders them and an operator can query them. Putting them through the hourly warning/critical report
would bury every real alert under thousands of lines that were never alerts.

The dangerous failure here is not "the filter did nothing" — it is "the filter dropped everything",
which would empty the reports silently. Both directions are pinned below.
"""

import json

import pytest

from db_ops.reports import metrics_reports as mr


class _Row(dict):
    """Mapping-like, matching the sqlite3.Row shape the real code receives."""


def _rows():
    return [
        _Row(metric_code="MAINTENANCE_INDEX_USAGE", target_id="T1"),
        _Row(metric_code="BACKUP_AGE", target_id="T1"),
        _Row(metric_code="JOB_FAILED", target_id="T1"),
    ]


def test_the_flag_is_read_from_metric_definitions_not_hardcoded():
    """A hard-coded list here would mean the next inventory metric needs a code change. The flag
    lives in data/metric_definitions.json as report_policy.collect_only, like every other policy."""
    assert "MAINTENANCE_INDEX_USAGE" in mr._collect_only_metric_codes()


def test_an_inventory_metric_does_not_reach_the_scheduled_reports():
    kept = {r["metric_code"] for r in mr._filter_rows_for_reports(_rows())}

    assert "MAINTENANCE_INDEX_USAGE" not in kept


def test_every_other_metric_still_reaches_the_reports():
    """The filter must remove one metric, not thin out the report."""
    kept = {r["metric_code"] for r in mr._filter_rows_for_reports(_rows())}

    assert {"BACKUP_AGE", "JOB_FAILED"} <= kept


def test_a_row_without_a_metric_code_is_kept_rather_than_dropped():
    """Dropping data because of a shape surprise hides metrics instead of filtering them."""
    kept = mr._filter_rows_for_reports([_Row(target_id="T1")])

    assert len(kept) == 1


def test_a_broken_definitions_file_does_not_empty_every_report(tmp_path, monkeypatch):
    """If the config cannot be parsed the safe answer is "exclude nothing" — a report missing its
    rows is far worse than an inventory metric appearing in it for one cycle."""
    broken = tmp_path / "metric_definitions.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mr, "DEFAULT_METRIC_DEFINITIONS_PATH", broken)

    assert mr._collect_only_metric_codes() == set()


def test_a_missing_definitions_file_excludes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "DEFAULT_METRIC_DEFINITIONS_PATH", tmp_path / "absent.json")

    assert mr._collect_only_metric_codes() == set()


def test_the_shipped_index_metric_is_collect_only_and_daily():
    """The two settings that make a 29k-row inventory metric safe to ship: it is excluded from the
    scheduled reports, and it runs once a day rather than on the default short interval."""
    data = json.loads(mr.DEFAULT_METRIC_DEFINITIONS_PATH.read_text(encoding="utf-8-sig"))
    metric = next(m for m in data["metrics"] if m["metric_code"] == "MAINTENANCE_INDEX_USAGE")

    assert metric["report_policy"]["collect_only"] is True
    # A range, not an exact number: the point is that this runs on a daily-ish cadence instead of
    # the short default, and the operator stays free to tune it (it was moved to 72000 = 20h after
    # the first deploy). Pinning the literal would turn a legitimate tuning change into a red build.
    assert metric["time_window"]["repeat_interval"] >= 43200
    # Without a raised cap the shared MAX_RESULT_ROWS (100) would truncate the inventory to a
    # sample, which is worse than not collecting it: it looks complete and is not.
    assert metric["max_rows"] >= 100000


# ---------------------------------------------------------------------------
# Two flags, because they answer two different questions
# ---------------------------------------------------------------------------
def test_maintenance_metrics_are_kept_out_of_the_alert_reports():
    """Fragmentation rows say `action=REBUILD`. That is scheduled work, not an incident, and it
    was filling the hourly warning report with dozens of lines nobody pages on."""
    codes = mr._collect_only_metric_codes()

    assert {"MAINTENANCE_INDEX_USAGE",
            "MAINTENANCE_INDEX_FRAGMENTATION",
            "MAINTENANCE_HEAP_FRAGMENTATION"} <= codes


def test_only_the_huge_metric_loses_its_detail_in_a_chart_series():
    """The distinction that one flag could not carry: fragmentation is maintenance work AND small
    enough to chart per index, so it belongs in the per-server report. Index usage is maintenance
    work AND ~29k rows, so only its aggregate may travel. Using collect_only for both dropped
    fragmentation out of the server report entirely."""
    chart_only = mr._chart_summary_only_metric_codes()

    assert "MAINTENANCE_INDEX_USAGE" in chart_only
    assert "MAINTENANCE_INDEX_FRAGMENTATION" not in chart_only
    assert "MAINTENANCE_HEAP_FRAGMENTATION" not in chart_only


def test_the_server_chart_series_keeps_fragmentation_but_drops_index_detail():
    from db_ops.reports.server_report import _drop_collect_only

    rows = [
        {"metric_code": "MAINTENANCE_INDEX_USAGE", "metric_unit": "user_updates"},   # detail
        {"metric_code": "MAINTENANCE_INDEX_USAGE", "metric_unit": "summary"},        # aggregate
        {"metric_code": "MAINTENANCE_INDEX_FRAGMENTATION", "metric_unit": "percent"},
        {"metric_code": "BACKUP_AGE", "metric_unit": "hours"},
    ]
    kept = [(r["metric_code"], r["metric_unit"]) for r in _drop_collect_only(rows)]

    assert ("MAINTENANCE_INDEX_USAGE", "user_updates") not in kept
    assert ("MAINTENANCE_INDEX_USAGE", "summary") in kept
    assert ("MAINTENANCE_INDEX_FRAGMENTATION", "percent") in kept
    assert ("BACKUP_AGE", "hours") in kept
