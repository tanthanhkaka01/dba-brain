"""Latency percentile and workload attribution collectors follow the record-never-grade contract.

Query latency percentiles (SQL Server Query Store), wait event latency histograms (Oracle
v$event_histogram), and query execution totals (PostgreSQL pg_stat_statements) capture the core
timing and workload attribution metrics needed for performance reporting.

House rules require that a collector records raw numbers and never grades them. Any arithmetic
across collections—calculating rates, taking deltas, deriving percentiles from interval deltas—lives
in dbabrain/lib and executes during report generation, rather than inside the collection path or via
stateful database lookups. All three collectors declare status LOGGING, enforce report_policy with
collect_only and chart_summary_only, and preserve query and reset baselines.

These tests hold the catalogue contracts, variant matrices, SQL settings hooks, engine compatibility
guards, and message envelope structures offline without requiring running databases.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

from conftest import shipped_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_PATH = REPO_ROOT / "dbabrain" / "metrics" / "catalogue" / "metric_definitions.json"
DATA_CATALOGUE_PATH = shipped_config("metric_definitions.json")

SQLSERVER_078 = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "sqlserver" / "078_sqlserver_query_latency_percentiles.sql"
ORACLE_072 = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "oracle" / "072_oracle_wait_latency_histogram.sql"
POSTGRESQL_070 = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "postgresql" / "070_postgresql_statement_totals.sql"

ORACLE_WATCHED_EVENTS = [
    "db file sequential read",
    "db file scattered read",
    "direct path read",
    "log file sync",
    "log file parallel write",
]


def _read_catalog(path: Path) -> dict[int, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {m["metric_id"]: m for m in data["metrics"]}


def _clean_sql(path: Path) -> str:
    """Return SQL text with comment lines stripped and whitespace normalized."""
    raw = path.read_text(encoding="utf-8")
    lines = [line.split("--", 1)[0] for line in raw.splitlines() if not line.lstrip().startswith("--")]
    text = re.sub(r"\s+", " ", " ".join(lines))
    # The SQL Server file is dynamic SQL, so its literals are written with doubled quotes; and a
    # literal is usually typed with CAST('X' AS varchar(n)). Both are spelling, not contract.
    text = text.replace("''", "'")
    return re.sub(r"CAST\(('[^']*') AS n?varchar2?\(\d+\)\) AS", r"\1 AS", text)


# --------------------------------------------------------------------------- #
# Catalogue and variant contracts
# --------------------------------------------------------------------------- #

def test_the_three_metrics_exist_identically_in_both_catalogues_with_expected_settings():
    """Both catalogues must remain in lockstep so nodes running from either copy behave identically."""
    cat_app = _read_catalog(CATALOGUE_PATH)
    cat_data = _read_catalog(DATA_CATALOGUE_PATH)

    for metric_id in (99, 100, 101):
        assert metric_id in cat_app, f"Metric {metric_id} missing from app catalogue"
        assert metric_id in cat_data, f"Metric {metric_id} missing from data catalogue"

        app_def = cat_app[metric_id]
        data_def = cat_data[metric_id]
        assert app_def == data_def, f"Metric {metric_id} differs between app and data catalogue"

        assert app_def["category"] == "performance"
        assert app_def["default_importance"] == 2
        assert app_def["active"] is True
        assert app_def["collector_type"] == "sql"
        assert app_def["connection_error_severity"] == "WARNING"
        assert app_def["execution_error_severity"] == "WARNING"


def test_all_three_metrics_declare_report_policy_collect_only_and_chart_summary_only():
    """Cumulative and interval collectors must not trigger warnings or flood chart dashboards."""
    catalogue = _read_catalog(CATALOGUE_PATH)

    for metric_id in (99, 100, 101):
        policy = catalogue[metric_id].get("report_policy")
        assert policy is not None, f"Metric {metric_id} lacks report_policy"
        assert policy.get("collect_only") is True, f"Metric {metric_id} must have collect_only: true"
        assert policy.get("chart_summary_only") is True, f"Metric {metric_id} must have chart_summary_only: true"
        assert "_note" in policy, f"Metric {metric_id} report_policy must explain why in _note"


def test_metric_99_variants_map_sqlserver_13_plus_and_mark_unsupported_engines():
    """Query Store was introduced in SQL Server 2016 (major version 13); older engines are unsupported."""
    metric = _read_catalog(CATALOGUE_PATH)[99]
    assert metric["metric_code"] == "QUERY_LATENCY_PERCENTILES"
    assert metric["empty_result_is_ok"] is True
    assert metric["time_window"]["repeat_interval"] == 3600
    assert metric["time_window"]["timeout"] == 120
    assert metric["max_rows"] == 500

    variants = metric["variants"]
    supported = [v for v in variants if v.get("supported", True)]
    assert len(supported) == 1
    assert supported[0]["db_type"] == "sqlserver"
    assert supported[0]["min_major_version"] == 13
    assert supported[0]["file"] == "sqlserver/078_sqlserver_query_latency_percentiles.sql"

    unsupported = {v["db_type"]: v for v in variants if not v.get("supported", True)}
    assert "sqlserver" in unsupported
    assert unsupported["sqlserver"]["max_major_version"] == 12
    assert "Query Store exists from SQL Server 2016" in unsupported["sqlserver"]["unsupported_reason"]
    assert "oracle" in unsupported
    assert "100" in unsupported["oracle"]["unsupported_reason"]
    assert "postgresql" in unsupported
    assert "101" in unsupported["postgresql"]["unsupported_reason"]
    assert "mysql" in unsupported


def test_metric_100_variants_map_oracle_11_plus_and_mark_unsupported_engines():
    """v$event_histogram buckets were standardized in Oracle 11g; older versions lack wait time buckets."""
    metric = _read_catalog(CATALOGUE_PATH)[100]
    assert metric["metric_code"] == "ORACLE_WAIT_LATENCY_HISTOGRAM"
    assert metric["empty_result_is_ok"] is False
    assert metric["time_window"]["repeat_interval"] == 900
    assert metric["time_window"]["timeout"] == 60
    assert metric["max_rows"] == 50

    variants = metric["variants"]
    supported = [v for v in variants if v.get("supported", True)]
    assert len(supported) == 1
    assert supported[0]["db_type"] == "oracle"
    assert supported[0]["min_major_version"] == 11
    assert supported[0]["file"] == "oracle/072_oracle_wait_latency_histogram.sql"

    unsupported = {v["db_type"]: v for v in variants if not v.get("supported", True)}
    assert "oracle" in unsupported
    assert unsupported["oracle"]["max_major_version"] == 10
    assert "sqlserver" in unsupported
    assert "postgresql" in unsupported
    assert "mysql" in unsupported


def test_metric_101_variants_map_postgresql_13_plus_and_mark_unsupported_engines():
    """pg_stat_statements renamed total_time to total_exec_time in PG 13; older versions lack modern columns."""
    metric = _read_catalog(CATALOGUE_PATH)[101]
    assert metric["metric_code"] == "POSTGRES_STATEMENT_TOTALS"
    assert metric["empty_result_is_ok"] is False
    assert metric["time_window"]["repeat_interval"] == 1800
    assert metric["time_window"]["timeout"] == 60
    assert metric["max_rows"] == 120

    variants = metric["variants"]
    supported = [v for v in variants if v.get("supported", True)]
    assert len(supported) == 1
    assert supported[0]["db_type"] == "postgresql"
    assert supported[0]["min_major_version"] == 13
    assert supported[0]["file"] == "postgresql/070_postgresql_statement_totals.sql"

    unsupported = {v["db_type"]: v for v in variants if not v.get("supported", True)}
    assert "postgresql" in unsupported
    assert unsupported["postgresql"]["max_major_version"] == 12
    assert "sqlserver" in unsupported
    assert "oracle" in unsupported
    assert "mysql" in unsupported


# --------------------------------------------------------------------------- #
# SQL Server 078 Query Store percentiles contract
# --------------------------------------------------------------------------- #

def test_sqlserver_078_declares_lookback_minutes_in_the_sql_settings_shape():
    """@lookback_minutes must be declared on its own line so apply_sql_settings can reconfigure it."""
    text = SQLSERVER_078.read_text(encoding="utf-8")
    assert re.search(r"^DECLARE @lookback_minutes int = 65;", text, re.MULTILINE), \
        "DECLARE @lookback_minutes must appear on its own line with default 65"


def test_sqlserver_078_can_be_configured_by_apply_sql_settings():
    """Overriding sql_settings from node configuration must cleanly replace the declaration."""
    raw = SQLSERVER_078.read_text(encoding="utf-8")
    modified = apply_sql_settings(raw, {"lookback_minutes": 120})
    assert "DECLARE @lookback_minutes int = 120;" in modified
    assert "DECLARE @lookback_minutes int = 65;" not in modified


def test_sqlserver_078_queries_only_read_write_or_read_only_query_store():
    """Databases with Query Store disabled must be skipped cleanly without errors."""
    code = _clean_sql(SQLSERVER_078)
    assert "actual_state_desc IN ('READ_WRITE', 'READ_ONLY')" in code


def test_sqlserver_078_filters_to_completed_intervals_within_lookback_window():
    """Partial in-flight intervals must not be read; only completed intervals within lookback are used."""
    code = _clean_sql(SQLSERVER_078)
    assert "end_time >= DATEADD(minute, -@lookback_minutes, SYSUTCDATETIME())" in code
    assert "end_time <= SYSUTCDATETIME()" in code


def test_sqlserver_078_keys_metric_item_by_database_and_interval_start_utc():
    """The interval key must include database and UTC interval start for deduplication across collections."""
    code = _clean_sql(SQLSERVER_078)
    # The key is the interval's start in UTC, minute precision: "<database> @ yyyy-MM-ddTHH:mmZ".
    assert "' @ ' + REPLACE(CONVERT(varchar(16), SWITCHOFFSET(ti.start_time, '+00:00'), 120), ' ', 'T') + 'Z'" in code
    assert "measured_as=exec_weighted_plan_average" in code
    assert "database=" in code
    assert "interval_start_utc=" in code
    assert "interval_end_utc=" in code
    assert "p50_ms=" in code
    assert "p95_ms=" in code
    assert "p99_ms=" in code


def test_sqlserver_078_never_emits_warning_or_critical_status():
    """Collectors record and never grade; status must strictly be LOGGING."""
    code = _clean_sql(SQLSERVER_078)
    assert "'LOGGING' AS status" in code
    assert "'WARNING'" not in code
    assert "'CRITICAL'" not in code


# --------------------------------------------------------------------------- #
# Oracle 072 Wait Latency Histogram contract
# --------------------------------------------------------------------------- #

def test_oracle_072_watches_the_five_agreed_io_and_log_sync_events():
    """The collector must query v$event_histogram and watch exactly the five core wait events."""
    raw = ORACLE_072.read_text(encoding="utf-8")
    for event in ORACLE_WATCHED_EVENTS:
        assert f"'{event}'" in raw, f"Event '{event}' missing from Oracle collector watch list"


def test_oracle_072_carries_counters_since_and_bucket_rollup_at_logging_status():
    """Rows must be LOGGING, carry startup time as counters_since, and roll up buckets as le_Nms:count."""
    raw = ORACLE_072.read_text(encoding="utf-8")
    code = _clean_sql(ORACLE_072)

    assert "v$instance" in raw
    assert "startup_time" in raw
    assert "v$event_histogram" in raw
    assert "LISTAGG" in raw
    assert "'LOGGING' AS status" in code
    assert "'waits' AS metric_unit" in code
    assert "event=" in code
    assert "counters_since=" in code
    assert "buckets=" in code
    assert "le_" in code


# --------------------------------------------------------------------------- #
# PostgreSQL 070 Statement Totals contract
# --------------------------------------------------------------------------- #

def test_postgresql_070_guards_view_access_with_to_regclass_and_query_to_xml():
    """Missing pg_stat_statements must not cause parse errors; access must be guarded via query_to_xml."""
    raw = POSTGRESQL_070.read_text(encoding="utf-8")
    code = _clean_sql(POSTGRESQL_070)

    assert "to_regclass('pg_stat_statements')" in code
    assert "query_to_xml" in code
    assert "XMLTABLE" in raw


def test_postgresql_070_emits_not_installed_row_when_extension_is_missing():
    """When pg_stat_statements is absent, a single LOGGING row must explain the operational state."""
    code = _clean_sql(POSTGRESQL_070)
    assert "'NOT_INSTALLED' AS metric_value" in code
    assert "not configured is a state, not a failure" in code
    assert "'pg_stat_statements' AS metric_item" in code


def test_postgresql_070_emits_all_statements_and_top_25_union_when_installed():
    """When installed, emits cluster-wide all_statements summary plus the top 25 time / calls union."""
    code = _clean_sql(POSTGRESQL_070)
    assert "'all_statements' AS metric_item" in code
    assert "CAST('queryid:' || rs.queryid AS varchar(256)) AS metric_item" in code
    assert "rs.r_time <= 25 OR rs.r_calls <= 25" in code


def test_postgresql_070_carries_counters_since_from_stats_reset_or_postmaster_start():
    """Reset baseline must come from pg_stat_statements_info.stats_reset or pg_postmaster_start_time."""
    code = _clean_sql(POSTGRESQL_070)
    assert "to_regclass('pg_stat_statements_info')" in code
    assert "pg_postmaster_start_time()" in code
    assert "counters_since=" in code


def test_postgresql_070_places_statement_text_snippet_last_with_spaced_equals():
    """SQL text snippet must be truncated to 200 chars, space out '=', and be positioned strictly last."""
    code = _clean_sql(POSTGRESQL_070)
    assert "text=" in code
    assert "' = '" in code, "Equality operators in statement text must be spaced out"
    assert "200" in code
    assert "ORDER BY sort_order ASC, sort_time DESC" in code
