"""Query Store: a live condition and a configuration fact are two different metrics.

`QUERY_STORE_QUERY_ISSUES` runs every 15 minutes because a query misbehaving now is worth
knowing about now. Whether Query Store is switched ON is not that — it changes when somebody
changes it. Reporting both from one SQL file meant every database with Query Store off produced
a WARNING every 15 minutes: 96 identical alerts a day, per database, on an estate where whole
instances have it off. The alert stream became mostly that one line, which is how a real finding
gets missed.

So the coverage half moved to `QUERY_STORE_COVERAGE`, once a day in the morning. These tests pin
the split itself — that neither metric grew the other's job back.
"""

import json
from pathlib import Path

import pytest
from db_ops.lib.paths import resolve_tool_path
from conftest import shipped_config

DEFINITIONS = shipped_config("metric_definitions.json")
ISSUES_SQL = resolve_tool_path("assets/metrics/sqlserver/023_sqlserver_query_store_query_issues.sql")
COVERAGE_SQL = resolve_tool_path("assets/metrics/sqlserver/070_sqlserver_query_store_coverage.sql")


@pytest.fixture(scope="module")
def metrics():
    doc = json.loads(DEFINITIONS.read_bytes().decode("utf-8-sig"))
    return {m["metric_code"]: m for m in doc["metrics"]}


def test_the_issues_metric_no_longer_reports_coverage():
    """The noisy half. Its absence here is the whole fix."""
    sql = ISSUES_SQL.read_text(encoding="utf-8")

    # No coverage rows, and no second result set to carry them.
    assert "query_store_coverage" not in sql
    assert "UNION ALL" not in sql
    assert "no query history is being captured" not in sql
    # QUERY_STORE_OFF itself stays: it is still a reason to skip a database, just not a finding.
    assert "WHEN d.is_query_store_on = 0 THEN 'QUERY_STORE_OFF'" in sql


def test_the_issues_metric_still_decides_which_databases_it_may_scan():
    """#qs_cov stays even though nothing is reported from it: it is what keeps the scan off an AG
    secondary, which rejects query_store reads with error 976."""
    sql = ISSUES_SQL.read_text(encoding="utf-8")

    assert "#qs_cov" in sql
    assert "AG_SECONDARY" in sql
    assert "WHERE skip_reason IS NULL" in sql


def test_the_coverage_metric_reports_every_database_not_only_the_broken_ones():
    """It used to emit rows only where Query Store was off, which is enough to raise an alert and
    not enough to answer anything: a healthy database produced no row, so "is Query Store on for
    APPDB" was indistinguishable from "APPDB was never collected", and no report could show a state.
    Both report pages are built from these rows, so the OK ones are the point."""
    sql = COVERAGE_SQL.read_text(encoding="utf-8")

    assert "QUERY_STORE_OFF" in sql and "DATABASE_NOT_ONLINE" in sql
    # An AG secondary is correct behaviour, not a fault: classified, reported, and never a warning.
    assert "AG_SECONDARY" in sql
    # No WHERE filter on the final SELECT — every database in #qs_cov reaches the result set.
    assert "WHERE c.skip_reason IN" not in sql
    assert "FROM #qs_cov AS c" in sql and "LEFT JOIN #qs_opt AS o" in sql
    # The database is the item now, so a report can key on it; it used to be a constant.
    assert "CAST(c.database_name AS varchar(256)) AS metric_item" in sql


def test_the_coverage_metric_reads_the_actual_state_not_just_the_configured_flag():
    """A Query Store that reaches max_storage_size flips to READ_ONLY and stops capturing while
    sys.databases.is_query_store_on still reads 1. Reporting only the configured flag calls that
    database covered when it has captured nothing since the day it filled up."""
    sql = COVERAGE_SQL.read_text(encoding="utf-8")

    assert "sys.database_query_store_options" in sql
    assert "actual_state_desc" in sql and "desired_state_desc" in sql
    assert "readonly_reason" in sql
    assert "o.desired_state_desc = 'READ_WRITE' AND o.actual_state_desc <> 'READ_WRITE'" in sql


def test_the_settings_column_added_in_2017_is_only_selected_where_it_exists():
    """wait_stats_capture_mode arrived in SQL Server 2017; naming it on 2016 is a compile error
    that would lose the whole collection rather than one column."""
    sql = COVERAGE_SQL.read_text(encoding="utf-8")

    assert "@major >= 14" in sql and "wait_stats_capture_mode_desc" in sql


def test_coverage_runs_once_a_day_in_the_morning(metrics):
    """72000s is longer than a day's working hours, so the 08:00-10:00 window is what actually
    decides when it runs — one report per morning instead of 96 a day."""
    window = metrics["QUERY_STORE_COVERAGE"]["time_window"]

    assert window["repeat_interval"] == 72000
    assert window["from_hour"] == 8
    assert window["to_hour"] == 10


def test_the_issues_metric_keeps_its_fast_cadence(metrics):
    """Splitting the noise out must not slow down the half that is actually time-sensitive."""
    assert metrics["QUERY_STORE_QUERY_ISSUES"]["time_window"]["repeat_interval"] == 900


def test_coverage_is_not_attempted_below_sql_server_2016(metrics):
    """Query Store arrives in 2016, so there is no coverage to report below it — and a variant
    claiming a version it cannot run on is how LINKED_SERVER_STATUS failed on every 2008 R2 run."""
    variants = [v for v in metrics["QUERY_STORE_COVERAGE"]["variants"]
                if v["db_type"] == "sqlserver"]

    unsupported = [v for v in variants if not v.get("supported", True)]
    supported = [v for v in variants if v.get("supported", True)]
    assert [v["max_major_version"] for v in unsupported] == [12]
    assert [v["min_major_version"] for v in supported] == [13]
    assert supported[0]["file"] == "sqlserver/070_sqlserver_query_store_coverage.sql"


def test_every_variant_file_the_two_metrics_name_exists(metrics):
    for code in ("QUERY_STORE_QUERY_ISSUES", "QUERY_STORE_COVERAGE"):
        for variant in metrics[code]["variants"]:
            if variant.get("file"):
                assert (resolve_tool_path("assets/metrics") / variant["file"]).is_file(), variant["file"]


def test_coverage_groups_by_instance_not_by_database(metrics):
    """"9 databases have Query Store off on this instance" is the finding; the database list is
    detail. Grouping per database would put the noise back one alert at a time."""
    grouping = metrics["QUERY_STORE_COVERAGE"]["report_policy"]["condition_grouping"]

    assert grouping["key_fields"] == ["instance_key", "issue_type"]
    assert grouping["replace_raw_rows"] is True


# --------------------------------------------------------------------------- #
# What the two report pages do with those rows
# --------------------------------------------------------------------------- #
def _coverage_row(name, value, message, status="OK"):
    return {"metric_code": "QUERY_STORE_COVERAGE", "metric_item": name, "metric_value": value,
            "status": status, "collected_at": "2026-08-12T01:00:00Z", "message": message}


def test_a_readonly_reason_is_spelled_out_for_the_reader():
    """A page printing `readonly_reason=65536` has told the reader nothing they can act on, and
    that number is exactly the case they most need to act on."""
    from db_ops.reports import inventory_health

    assert inventory_health.describe_query_store_readonly_reason(65536) == (
        "storage size limit reached")
    assert inventory_health.describe_query_store_readonly_reason(0) == ""
    assert inventory_health.describe_query_store_readonly_reason(None) == ""


def test_a_query_store_that_filled_up_is_reported_as_not_capturing():
    """The failure the configured flag hides: switched on, silently recording nothing."""
    from db_ops.reports import inventory_health

    entry = inventory_health.build_query_store_entry(_coverage_row(
        "APPDB", "READ_ONLY",
        "db_name=APPDB, issue_type=NONE, desired_state=READ_WRITE, actual_state=READ_ONLY, "
        "readonly_reason=65536, current_storage_mb=1000, max_storage_mb=1000, "
        "storage_used_pct=100.0", status="WARNING"))

    assert entry["on"] is True            # sys.databases still says it is on
    assert entry["capturing"] is False    # and it is recording nothing
    assert entry["readonly_reason_desc"] == "storage size limit reached"
    assert entry["storage_used_pct"] == 100.0


def test_a_database_the_collector_never_saw_is_unknown_not_off():
    """Blank and off are different answers; rendering both as off invents a finding."""
    from db_ops.reports import inventory_health

    assert inventory_health.build_query_store_entry(None) == {}


def test_the_server_section_puts_the_databases_that_capture_nothing_first():
    """The section exists to answer "where can I not investigate"; on a 13-database instance that
    answer must not be somewhere in the middle of the list."""
    from db_ops.reports import server_report

    section = server_report.build_query_store([
        _coverage_row("Globex_Prod", "READ_WRITE",
                      "desired_state=READ_WRITE, actual_state=READ_WRITE, storage_used_pct=19.3"),
        _coverage_row("APPDB", "OFF", "issue_type=QUERY_STORE_OFF", status="WARNING"),
    ])

    assert [db["name"] for db in section["databases"]] == ["APPDB", "Globex_Prod"]
    assert section["summary"] == {"databases": 2, "capturing": 1, "off": 1,
                                  "onButNotCapturing": 0, "stoppedOnItsOwn": 0,
                                  "asOf": "2026-08-12T01:00:00Z"}


def test_the_server_section_counts_on_but_not_capturing_separately():
    """It is neither "on" nor "off" in any way a reader can act on, and it is the interesting one."""
    from db_ops.reports import server_report

    section = server_report.build_query_store([
        _coverage_row("APPDB", "READ_ONLY",
                      "desired_state=READ_WRITE, actual_state=READ_ONLY, readonly_reason=65536",
                      status="WARNING"),
    ])

    assert section["summary"]["onButNotCapturing"] == 1
    assert section["summary"]["off"] == 0
    assert section["databases"][0]["readonlyReasonDesc"] == "storage size limit reached"


def test_a_failed_collection_is_not_rendered_as_a_database():
    """An itemless row is a collection that failed, not a database called ""."""
    from db_ops.reports import server_report

    section = server_report.build_query_store([
        {"metric_code": "QUERY_STORE_COVERAGE", "metric_item": None, "metric_value": None,
         "status": "ERROR", "message": "SQL failed"},
    ])

    assert section["databases"] == []


def test_the_inventory_row_carries_the_state_for_the_database_column():
    from db_ops.reports import inventory_report

    rows = inventory_report._build_databases({
        "database_health": [{
            "database_name": "APPDB", "state": "ONLINE",
            "query_store": {"state": "OFF", "capturing": False, "on": False},
        }],
    })

    assert rows[0]["queryStore"] == "OFF"
    assert rows[0]["queryStoreCapturing"] is False


def test_query_store_coverage_is_loaded_into_the_inventory_overlay():
    """Without it in HEALTH_CODES the overlay never sees the rows, so the column renders blank for
    every database however well the collector runs."""
    from db_ops.reports import inventory_health

    assert "QUERY_STORE_COVERAGE" in inventory_health.HEALTH_CODES


def test_off_by_choice_and_off_after_a_failure_are_told_apart():
    """"off" alone cannot be acted on. Somebody switching Query Store off is a decision; Query
    Store switching itself off after an error is a fault, usually with captured data still in the
    database. SALESDB on 192.0.2.115 is the second kind — desired OFF, actual ERROR, 2128 MB still
    there — and read as the first for as long as the collector skipped the options DMV for
    databases reporting Query Store as off."""
    from db_ops.reports import server_report

    section = server_report.build_query_store([
        _coverage_row("SALESDB", "ERROR",
                      "issue_type=QUERY_STORE_OFF, off_reason=ERROR_STATE, desired_state=OFF, "
                      "actual_state=ERROR, current_storage_mb=2128", status="WARNING"),
        _coverage_row("SALESDW", "OFF",
                      "issue_type=QUERY_STORE_OFF, off_reason=TURNED_OFF, desired_state=OFF, "
                      "actual_state=OFF", status="WARNING"),
    ])

    by_name = {db["name"]: db for db in section["databases"]}
    assert by_name["SALESDB"]["offReasonDesc"] == "stopped after an error"
    assert by_name["SALESDW"]["offReasonDesc"] == "switched off"
    # Only the one that stopped by itself is counted as such; the other is somebody's decision.
    assert section["summary"]["stoppedOnItsOwn"] == 1
    assert section["summary"]["off"] == 1


def test_the_options_dmv_is_read_even_when_the_database_says_query_store_is_off():
    """That read is the only thing that separates the two cases above."""
    sql = COVERAGE_SQL.read_text(encoding="utf-8")

    assert "WHERE skip_reason IS NULL OR skip_reason = 'QUERY_STORE_OFF'" in sql
    assert "off_reason=" in sql
    assert "'TURNED_OFF'" in sql and "'ERROR_STATE'" in sql
