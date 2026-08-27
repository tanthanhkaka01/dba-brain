"""A finding nobody can read is not a finding: `MAINTENANCE_STATISTICS_AGE` grades a summary.

The metric emitted one WARNING per stale statistics object. On 192.0.2.115 that was 4,232 WARNINGs
in a single pass — 1,093 of them over 90 days old, the oldest 953 days, 2,893 stale *and* modified
since. `report_policy.collect_only` kept them out of the scheduled reports, so nothing alerted; it
also meant the finding was never stated anywhere a person reads, and the object that actually
mattered (`NUMBERSEQUENCELIST`, 330,110 rows against 15.6 million modifications, named by hand in
that instance's blocking chains) sat somewhere in the middle of four thousand identical lines.

So the rows now come in three grains, the same split `MAINTENANCE_INDEX_FRAGMENTATION` has always
used: detail at LOGGING, one row per database, and one summary that grades. These tests pin the
split and the bands, because losing either turns the metric back into a wall of warnings.
"""

import json

import pytest
from db_ops.lib.paths import resolve_tool_path
from conftest import shipped_config

STATS_SQL = resolve_tool_path("assets/metrics/sqlserver/062_sqlserver_statistics_age.sql")


@pytest.fixture(scope="module")
def sql():
    return STATS_SQL.read_text(encoding="utf-8")


def test_per_object_detail_is_logging_not_warning(sql):
    """The line that produced four thousand alerts."""
    detail = sql[sql.index("FROM (") : sql.index("-- One row per database")]

    assert "CAST('LOGGING' AS varchar(16)) AS status" in detail
    assert "'WARNING'" not in detail


def test_the_summary_is_the_row_that_grades(sql):
    summary = sql[sql.index("-- The row that grades") :]

    assert "'statistics_age :: summary'" in summary
    assert "THEN 'WARNING' ELSE 'OK' END" in summary


def test_there_is_a_row_per_database_because_maintenance_is_scheduled_per_database(sql):
    assert "'statistics_age :: ' + d.db_name" in sql
    assert "GROUP BY d.db_name" in sql


@pytest.mark.parametrize("band", ["over_90d", "stale_and_modified", "oldest_days"])
def test_the_summary_carries_bands_and_not_only_a_count(sql, band):
    """"Stale" cannot separate a schedule that slipped a week from one that stopped two years ago."""
    summary = sql[sql.index("-- The row that grades") :]

    assert band in summary


def test_the_per_database_row_names_its_worst_object(sql):
    """The audit found `NUMBERSEQUENCELIST` by hand. The metric should hand it over."""
    assert "most_modified=" in sql
    # Resolved into its own table first: T-SQL refuses an aggregate over a subquery (error 130),
    # which is exactly how the obvious spelling of this failed.
    assert "#worst" in sql
    assert "SELECT MAX(x.mods)" not in sql


def test_detail_items_keep_the_db_backslash_table_dot_object_shape(sql):
    """Stored history is queried on this shape; changing it orphans every row already collected."""
    assert "t.db_name + N'\\' + ISNULL(t.table_name, N'?') + N'.' + ISNULL(t.stat_name, N'?')" in sql


def test_every_temp_table_it_creates_is_dropped(sql):
    """The collector reuses one session per target; a leftover #worst fails the next run."""
    for table in ("#st", "#worst"):
        assert sql.count(f"IF OBJECT_ID('tempdb..{table}') IS NOT NULL DROP TABLE {table};") >= 2


def test_the_metric_still_stays_out_of_the_scheduled_reports():
    """The rollup is why the rows are readable; collect_only is why they are not paged."""
    doc = json.loads(shipped_config("metric_definitions.json").read_bytes().decode("utf-8-sig"))
    metric = next(m for m in doc["metrics"] if m["metric_code"] == "MAINTENANCE_STATISTICS_AGE")

    assert metric["report_policy"]["collect_only"] is True
    # The detail rows are still collected in full, so "which object" stays one query away.
    assert metric["max_rows"] >= 20000
