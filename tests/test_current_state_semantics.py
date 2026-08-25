"""Regressions for the five P0 defects found in one estate-wide metric health audit.

Every case here is built from what the two production targets actually produced on 2026-08-01,
because each defect was invisible to the tests that existed: they all asserted on data shaped the
way the code expected it, and the code's expectation was the bug.

The five, in the audit's numbering:

* **P0-1** a metric's condition clears with a ``metric_item = NULL`` row, so the CRITICAL item it
  cleared stayed "newest" — and therefore "current" — for the whole report window.
* **P0-2** health areas were whole metric codes and the tile showed the largest number in them,
  so CPU showed SQL memory and Disk space showed disk throughput in KB/s.
* **P0-3** backup posture was the newest evidence anywhere on the instance, so one healthy
  database answered for every stale one.
* **P0-4** freshness was the newest metric on the server, which hid every late metric behind it,
  and NULL-item collector errors were dropped from the per-server query entirely.
* **P0-5** only hand-coded conditions became Priority Attention cards; everything else was
  concatenated into one ``info`` paragraph, severity discarded.
"""

from __future__ import annotations

import datetime

from db_ops.lib import backup_policy
from db_ops.lib import health_model
from db_ops.db.metric_store import MetricStore
from db_ops.db.metric_results import MetricResult
from db_ops.reports.inventory_health import (
    build_metric_problems,
    build_performance_health,
    index_by_server,
)
from db_ops.reports.inventory_report import _build_backup, _metric_problem_cards, _monitoring_gap_cards
from db_ops.reports.server_report import build_areas, build_freshness

from db_ops.common import data_sources
from conftest import shipped_config

# `evaluate_backup_policy` no longer reads data/backup_policy.json itself (2026-08-15):
# reading is data_sources' job, judging is lib's. These tests exercised the shipped policy
# through that default, so they pass the same document in explicitly.
#
# Named through `shipped_config` rather than left to the default, because the default is the
# operator's data directory: a clone has only `backup_policy.example.json`, and the load then
# returned an empty document. That did not error — it made every judgement come back "No policy
# match", so the tests failed saying OK where they expected CRITICAL, which reads as a bug in the
# grading rather than as a missing file.
_SHIPPED_POLICY = data_sources.load_backup_policy(shipped_config("backup_policy.json"))


SERVER = "ACME-192-0-2-115"


def _result(**over) -> MetricResult:
    fields = dict(
        target_id="T", server_id=SERVER, ip="192.0.2.115", db_type="sqlserver",
        db_name="SALESDB-PROD", metric_code="LOCK_BLOCKING_SESSIONS", metric_item="SALESDB",
        metric_value="42", metric_unit="blocked_sessions", status="CRITICAL", message="",
        collected_at="2026-08-01T07:00:00Z", importance=4,
    )
    fields.update(over)
    return MetricResult(**fields)


def _store(tmp_path, results):
    store = MetricStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    run = store.start_run(started_at="2026-08-01T00:00:00Z")
    store.insert_results(run_id=run, results=results)
    return store


def _row(code, item, value, *, status="OK", unit="", message="", when="2026-08-01T07:00:00Z"):
    return {"metric_code": code, "metric_item": item, "metric_value": value, "metric_unit": unit,
            "status": status, "message": message, "collected_at": when, "server_id": SERVER,
            "ip": "192.0.2.115", "db_name": "SALESDB-PROD"}


def _series(code, item, last, status, *, unit="", message="", label=None):
    return {"code": code, "item": item, "label": label or code, "unit": unit, "status": status,
            "numeric": last is not None, "static": False, "last": last,
            "lastText": "" if last is None else str(last), "message": message,
            "lastAt": 1754031600, "points": [[1754031600, last, status]]}


# --------------------------------------------------------------------------- #
# P0-1 — an empty result clears the items it was about
# --------------------------------------------------------------------------- #
def test_an_empty_result_clears_the_item_it_was_about(tmp_path):
    """The exact 192.0.2.250 case: blocking rows keyed by database, then a snapshot with none.

    ``009_sqlserver_blocking_sessions.sql`` keys its rows by database while something is blocked,
    and the collector records "condition cleared" as one row with ``metric_item = NULL``. Those
    are different partitions, so keeping the newest row per item kept SALESDB CRITICAL on the fleet
    page for the whole window while the live snapshot said zero.
    """
    store = _store(tmp_path, [
        _result(collected_at="2026-08-01T05:00:00Z"),
        _result(metric_item=None, metric_value=None, status="OK",
                message="SQL returned no rows.", collected_at="2026-08-01T07:00:00Z"),
    ])

    severity = store.fetch_severity_by_server(days=7, as_of=AS_OF)

    assert severity[SERVER]["worst"] == "OK"
    assert severity[SERVER]["critical_codes"] == []


def test_a_still_blocked_database_stays_critical(tmp_path):
    """The clearing rule must not clear something that has not cleared."""
    store = _store(tmp_path, [
        _result(collected_at="2026-08-01T05:00:00Z"),
        _result(collected_at="2026-08-01T07:00:00Z"),
    ])

    severity = store.fetch_severity_by_server(days=7, as_of=AS_OF)

    assert severity[SERVER]["worst"] == "CRITICAL"
    assert severity[SERVER]["critical_codes"] == ["LOCK_BLOCKING_SESSIONS"]
    assert severity[SERVER]["critical_rows"] == 1     # one condition, not one per cycle


def test_the_overlay_drops_items_the_latest_collection_did_not_produce():
    """A database that vanished from DATABASE_STATUS is not still on the page for a week."""
    servers = index_by_server([
        _row("DATABASE_STATUS", "SALESDB", "ONLINE", when="2026-07-30T07:00:00Z"),
        _row("DATABASE_STATUS", "Dropped", "ONLINE", when="2026-07-30T07:00:00Z"),
        _row("DATABASE_STATUS", "SALESDB", "ONLINE", when="2026-08-01T07:00:00Z"),
    ])
    _ip, code_map = servers[SERVER]

    assert sorted(item for (_code, item) in code_map) == ["SALESDB"]


def test_a_metric_keeps_its_own_snapshot_and_does_not_follow_a_faster_one():
    """Snapshots are per metric code. A daily size metric is not stale because a 5-minute one ran."""
    servers = index_by_server([
        _row("DATABASE_DATA_SIZE", "SALESDB", "1664.31", unit="GB", when="2026-08-01T02:00:00Z"),
        _row("LOG_FILE_SPACE", "SALESDB", "99.77", unit="pct", when="2026-08-01T07:00:00Z"),
    ])
    _ip, code_map = servers[SERVER]

    assert ("DATABASE_DATA_SIZE", "SALESDB") in code_map
    assert ("LOG_FILE_SPACE", "SALESDB") in code_map


def test_blocking_is_counted_across_databases_not_only_the_server_item():
    """``blocking_count = 0`` next to 42 blocked sessions was a literal item-name mismatch.

    The blocking SQL emits ``server = 0`` **only when nothing is blocked**; the moment something
    is, its rows are keyed by database. Reading the ``server`` item alone therefore returned zero
    exactly when the answer was not zero.
    """
    _ip, code_map = index_by_server([
        _row("LOCK_BLOCKING_SESSIONS", "SALESDB", "42", status="CRITICAL",
             message="database=SALESDB, blocked_sessions=42, max_wait_seconds=2"),
    ])[SERVER]

    health = build_performance_health(code_map)

    assert health["blocking_count"] == 42
    assert health["blocking_status"] == "CRITICAL"
    assert health["blocking_by_database"] == {"SALESDB": 42}
    assert health["blocking_max_wait_seconds"] == 2


# --------------------------------------------------------------------------- #
# P0-2 — an area shows the item that answers its own question
# --------------------------------------------------------------------------- #
def test_the_cpu_tile_shows_cpu_not_sql_memory():
    """192.0.2.250 read ``WARNING 75.63 pct`` on CPU while CPU was 5-8%: the value came from
    ``SYSTEM_CPU_MEMORY/sql_memory``, which is in the same metric code and is a bigger number."""
    areas = {area["key"]: area for area in build_areas([
        _series("OS_CPU_USAGE", "cpu_usage", 5.17, "OK", unit="percent"),
        _series("SYSTEM_CPU_MEMORY", "cpu", 8.0, "OK", unit="pct"),
        _series("SYSTEM_CPU_MEMORY", "sql_memory", 75.63, "WARNING", unit="pct"),
    ])}

    assert areas["cpu"]["sourceItem"] in ("cpu_usage", "cpu")
    assert areas["cpu"]["status"] == "OK"
    assert areas["memory"]["sourceCode"] == "SYSTEM_CPU_MEMORY"
    assert areas["memory"]["status"] == "WARNING"


def test_disk_space_never_shows_a_throughput_rate():
    """``53083 KB/s`` was the Disk space value on 192.0.2.115: disk read throughput is a row of
    OS_DISK_USAGE, and it is a far bigger number than any percentage."""
    areas = {area["key"]: area for area in build_areas([
        _series("OS_DISK_USAGE", "L:", 87.3, "WARNING", unit="percent"),
        _series("OS_DISK_USAGE", "disk_read_kbps", 53083.0, "OK", unit="KB/s"),
        _series("OS_DISK_USAGE", "disk_write_kbps", 12000.0, "OK", unit="KB/s"),
    ])}

    assert areas["disk_space"]["sourceItem"] == "L:"
    assert areas["disk_space"]["value"] == "87.3%"
    assert areas["storage_activity"]["sourceItem"] in ("disk_read_kbps", "disk_write_kbps")


def test_memory_never_shows_an_absolute_mb_row_against_a_percent_threshold():
    """``255314 MB`` sat next to a threshold stated in percent."""
    areas = {area["key"]: area for area in build_areas([
        _series("OS_MEMORY_USAGE", "memory_usage", 62.33, "OK", unit="percent"),
        _series("OS_MEMORY_USAGE", "memory_used_mb", 255314.0, "OK", unit="MB"),
    ])}

    assert areas["memory"]["sourceItem"] == "memory_usage"
    assert "MB" not in areas["memory"]["value"]


def test_a_failing_metric_makes_its_area_unknown_rather_than_ok():
    """An area whose own metric is failing does not get to report OK from the last sample that
    worked — that is how a two-day-old value passed for the present."""
    freshness = {"metrics": [{"code": "OS_CPU_USAGE", "state": "FAILED"}]}
    areas = {area["key"]: area for area in build_areas(
        [_series("OS_CPU_USAGE", "cpu_usage", 5.17, "OK", unit="percent")], freshness=freshness)}

    assert areas["cpu"]["status"] == "UNKNOWN"
    assert areas["cpu"]["stale"] is True
    assert "OS_CPU_USAGE" in areas["cpu"]["detail"]


# --------------------------------------------------------------------------- #
# P0-3 — backup is judged per database against a policy
# --------------------------------------------------------------------------- #
def _erp_backup_rows():
    """The ERP FCI as collected: FULL and DIFF current, LOG 124 days behind on every database."""
    rows = []
    for database in ("SALESDB", "SALESDW", "ReportingDB"):
        rows += [
            _row("BACKUP_LAST_RESULT", f"{database} / FULL", "7",
                 message=f"database={database}, recovery_model=FULL, backup_type=D, "
                         "backup_finish_date=2026-07-31 22:38:34"),
            _row("BACKUP_LAST_RESULT", f"{database} / DIFF", "18",
                 message=f"database={database}, recovery_model=FULL, backup_type=I, "
                         "backup_finish_date=2026-07-31 11:33:17"),
            _row("BACKUP_LAST_RESULT", f"{database} / LOG", "2979",
                 message=f"database={database}, recovery_model=FULL, backup_type=L, "
                         "backup_finish_date=2026-03-30 12:00:02"),
        ]
    return rows


def test_a_stale_log_backup_is_critical_and_names_every_affected_database():
    result = backup_policy.evaluate_backup_policy(_erp_backup_rows(), server_id=SERVER, policy=_SHIPPED_POLICY)

    summary = result["summary"]
    assert summary["status"] == "CRITICAL"
    assert (summary["compliant"], summary["eligible"]) == (0, 3)
    # Alphabetical: every database here is equally stale, so the tie is broken by name.
    assert summary["worstDatabases"] == ["ReportingDB", "SALESDB", "SALESDW"]
    assert summary["byType"]["LOG"]["state"] == "VIOLATED"
    assert summary["byType"]["FULL"]["state"] == "OK"


def test_a_diff_the_policy_does_not_require_is_not_missing():
    """The report used to imply DIFF coverage from any DIFF row on the instance, and to call its
    absence a gap. Under a daily FULL plus frequent LOG plan it is neither."""
    rows = [
        _row("BACKUP_LAST_RESULT", "OrchestratorData / FULL", "7",
             message="database=OrchestratorData, recovery_model=FULL, backup_type=D, "
                     "backup_finish_date=2026-08-01 00:00:01"),
        _row("BACKUP_LAST_RESULT", "OrchestratorData / LOG", "1",
             message="database=OrchestratorData, recovery_model=FULL, backup_type=L, "
                     "backup_finish_date=2026-08-01 06:00:00"),
    ]

    result = backup_policy.evaluate_backup_policy(rows, server_id=SERVER, policy=_SHIPPED_POLICY)

    record = result["databases"][0]
    assert record["status"] == "OK"
    assert record["types"]["DIFF"]["required"] is False
    assert record["types"]["DIFF"]["state"] == "NOT_REQUIRED"
    assert backup_policy.coverage_text(result["summary"]) == "Full+Log · DIFF not required"


def test_a_simple_recovery_database_is_not_held_to_a_log_rpo():
    rows = [_row("BACKUP_LAST_RESULT", "ReportServerTempDB / FULL", "7",
                 message="database=ReportServerTempDB, recovery_model=SIMPLE, backup_type=D, "
                         "backup_finish_date=2026-08-01 00:00:01")]

    record = backup_policy.evaluate_backup_policy(rows, server_id=SERVER, policy=_SHIPPED_POLICY)["databases"][0]

    assert record["types"]["LOG"]["required"] is False
    assert record["status"] == "OK"


def test_the_fleet_backup_column_never_claims_the_chain_is_broken():
    """"Broken chain" is an instruction to take a fresh FULL. Nothing collected proves it — that
    needs backup LSNs and recovery-fork ids — and acting on it when the chain was intact discards
    a working restore path."""
    result = backup_policy.evaluate_backup_policy(_erp_backup_rows(), server_id=SERVER, policy=_SHIPPED_POLICY)
    model = _build_backup({"databases": [{"db_type": "sqlserver"}],
                           "backup_evidence": {"LOG": {"latest_finish": "2026-03-30 12:00:02",
                                                       "latest_age_hours": 2979}},
                           "backup_policy": result})

    assert model["logStale"] is True
    assert model["status"] == "CRITICAL"
    assert "chain" not in model["note"].lower()
    assert "SALESDB" in model["note"] or "SALESDB" in model["worstDatabases"]


# --------------------------------------------------------------------------- #
# P0-4 — every metric reports its own freshness, and a failure stays visible
# --------------------------------------------------------------------------- #
NOW = int(datetime.datetime(2026, 8, 1, 7, 40, tzinfo=datetime.timezone.utc).timestamp())

#: The same instant as ``NOW``, for the store queries that take ``as_of``.
#:
#: These fixtures are the rows the two production targets produced on 2026-08-01, and a ``days``
#: window is measured back from *now* unless a ceiling is given. Without one the tests quietly
#: expired: on 2026-08-07 the 7-day cutoff moved past the 2026-07-30 row and
#: ``test_a_warning_from_a_broken_credential_is_not_counted_as_a_success`` began failing on a
#: clock, not on a defect. Pinning the ceiling is what ``as_of`` is for — the same mechanism that
#: lets a report be rebuilt for a past date.
AS_OF = "2026-08-01T07:40:00Z"


def _fresh_row(code, *, attempt, success=None, status="OK", message="", collector="sql"):
    return {"metric_code": code, "db_type": "sqlserver", "collector_type": collector,
            "last_attempt": attempt, "last_success": success if success is not None else attempt,
            "rows_in_window": 5, "status": status, "message": message}


def test_a_late_metric_is_late_even_when_another_metric_is_three_minutes_old():
    """The whole of P0-4 in one assertion. 192.0.2.250 reported an overall data age near three
    minutes while LOG_RECENT_CRITICAL was 48.1 hours old on a five-minute cadence."""
    freshness = build_freshness([
        _fresh_row("OS_CPU_USAGE", attempt="2026-08-01T07:37:00Z"),
        _fresh_row("LOG_RECENT_CRITICAL", attempt="2026-07-30T07:34:00Z"),
    ], now=NOW, days=7)

    by_code = {entry["code"]: entry for entry in freshness["metrics"]}
    assert by_code["OS_CPU_USAGE"]["state"] == "OK"
    assert by_code["LOG_RECENT_CRITICAL"]["state"] == "LATE"
    assert freshness["late"] == ["LOG_RECENT_CRITICAL"]


def test_a_metric_whose_newest_run_failed_reports_failed_and_carries_the_error():
    freshness = build_freshness([
        _fresh_row("DATABASE_STATUS", attempt="2026-08-01T07:37:00Z",
                   success="2026-08-01T05:37:00Z", status="ERROR",
                   message="Login failed for user 'db_ops'."),
    ], now=NOW, days=7)

    entry = freshness["metrics"][0]
    assert entry["state"] == "FAILED"
    assert "Login failed" in entry["error"]
    assert freshness["failed"] == ["DATABASE_STATUS"]


def test_an_active_catalog_metric_with_no_rows_is_reported_as_a_gap(shipped_metric_catalog):
    """The gap the audit found: MAINTENANCE_HEAP_FRAGMENTATION and MAINTENANCE_INDEX_USAGE had no
    evidence on either target, and nothing on either page said so.

    The code is taken *from* the catalog rather than named, because which metrics an estate
    catalogues is that estate's business. Naming one made this a check on the operator's
    `metric_definitions.json` — it failed on a checkout whose example catalog is four metrics long,
    reporting an empty set as if the coverage section had stopped working.
    """
    catalogued = [
        entry["code"] for entry in shipped_metric_catalog
        if entry["active"] and entry["collector_type"] == "sql" and "sqlserver" in entry["db_types"]
    ]
    assert catalogued, "the shipped catalog has no active SQL Server metric to be missing"
    collected, expected_missing = catalogued[0], catalogued[1:]
    assert expected_missing, "one catalogued metric cannot demonstrate a gap"

    freshness = build_freshness(
        [_fresh_row(collected, attempt="2026-08-01T07:37:00Z")], now=NOW, days=7)

    missing = {entry["code"] for entry in freshness["notCollected"]}
    assert set(expected_missing) <= missing
    assert freshness["seen"] == 1
    assert freshness["expected"] > 1


def test_a_warning_from_a_broken_credential_is_not_counted_as_a_success(tmp_path):
    """A target's severity map downgrades auth and connect failures to WARNING, so status alone
    cannot tell "the check failed" from "the collector never ran". Four metrics on
    192.0.2.250 had been returning AUTH_FAILED for a day and read as ordinary warnings, while
    the page went on showing the values from before the credential broke."""
    store = _store(tmp_path, [
        _result(metric_code="DATABASE_CONFIG", metric_item="APPDB_Prod", metric_value="FULL",
                status="OK", collected_at="2026-07-30T18:05:00Z"),
        # error_type is what the collector writes for this row (event_policy.normalize_error_type
        # reads "login failed" out of the driver message); the freshness query reads it rather
        # than re-deriving the classification from message text in SQL.
        _result(metric_code="DATABASE_CONFIG", metric_item=None, metric_value=None,
                status="WARNING", message="sqlserver connect to 192.0.2.250:1433 failed: "
                                          "Login failed for user 'db_ops'.",
                error_type="AUTH_FAILED", collected_at="2026-07-31T18:05:00Z"),
    ])

    entry = store.fetch_metric_freshness(days=7, as_of=AS_OF)[SERVER][0]

    assert entry["last_success"] == "2026-07-30T18:05:00Z"
    assert entry["last_attempt"] == "2026-07-31T18:05:00Z"


def test_a_threshold_breach_is_still_a_successful_collection(tmp_path):
    """CHECK_FAILED is the collector working and not liking the answer. Treating it as a broken
    collector would report every warning on the estate as a monitoring gap."""
    store = _store(tmp_path, [
        _result(metric_code="OS_DISK_USAGE", metric_item="D:", metric_value="91",
                status="WARNING", message="Disk usage is 91 percent.",
                collected_at="2026-08-01T07:00:00Z"),
    ])

    entry = store.fetch_metric_freshness(days=7, as_of=AS_OF)[SERVER][0]

    assert entry["last_success"] == "2026-08-01T07:00:00Z"
    assert build_freshness([entry], now=NOW, days=7)["failed"] == []


def test_a_collector_failure_card_says_what_failed():
    """"__collector__ · —" names the shape of the problem, not the problem."""
    problems = build_metric_problems([
        _row("DATABASE_CHECKDB", None, None, status="WARNING",
             message="sqlserver connect to 192.0.2.250:1433 failed: Login failed for user "
                     "'db_ops'. (18456) (SQLDriverConnect)"),
    ])

    assert "Login failed" in problems[0]["headline"] or "connect to" in problems[0]["headline"]
    assert "__collector__ · —" not in problems[0]["headline"]


def test_a_collector_error_row_reaches_the_server_page(tmp_path):
    """``fetch_server_series`` dropped ``metric_item IS NULL`` outright, which is exactly the row
    that says a metric is currently failing. The page went on charting the values from before it
    broke and presented them as the present."""
    store = _store(tmp_path, [
        _result(metric_code="QUERY_LONG_RUNNING", metric_item="server", metric_value="0",
                status="OK", collected_at="2026-08-01T05:00:00Z"),
        _result(metric_code="QUERY_LONG_RUNNING", metric_item=None, metric_value=None,
                status="ERROR", message="SQL execution failed: timeout expired",
                collected_at="2026-08-01T07:00:00Z"),
    ])

    rows = store.fetch_server_series(server_id=SERVER, days=7, as_of=AS_OF)

    items = {(row["metric_code"], row["metric_item"]) for row in rows}
    assert ("QUERY_LONG_RUNNING", health_model.COLLECTOR_ITEM) in items


def test_a_collector_failure_survives_the_minimum_sample_rule(tmp_path):
    """A failure row is not a series and never will be, so the "needs 3 samples to be charted"
    rule must not drop it. A daily metric that fails twice would otherwise vanish, and its health
    area go back to reading "not collected" instead of naming the credential that broke it."""
    from db_ops.reports.server_report import build_areas, load_server_series

    store = _store(tmp_path, [
        _result(metric_code="SECURITY_FAILED_LOGINS", metric_item=None, metric_value=None,
                status="WARNING", error_type="AUTH_FAILED",
                message="sqlserver connect to 192.0.2.115:1433 failed: Login failed.",
                collected_at="2026-07-31T18:05:00Z"),
    ])
    series, _omitted = load_server_series(store.sqlite_path, server_id=SERVER, days=7, as_of=AS_OF)

    assert [entry["item"] for entry in series] == [health_model.COLLECTOR_ITEM]
    security = {area["key"]: area for area in build_areas(series)}["security"]
    assert security["status"] == "WARNING"
    assert security["value"] != "not collected"


def test_an_empty_result_ok_row_is_not_charted_as_a_collector_error(tmp_path):
    """"SQL returned no rows" is the condition clearing, not a failure. It clears its items
    through the snapshot rule and must not also appear as a series of its own."""
    from db_ops.reports.server_report import load_server_series

    store = _store(tmp_path, [
        _result(metric_item=None, metric_value=None, status="OK",
                message="SQL returned no rows.", collected_at="2026-08-01T07:00:00Z"),
    ])

    series, omitted = load_server_series(store.sqlite_path, server_id=SERVER, days=7, as_of=AS_OF)

    assert series == []
    assert omitted == []


def test_a_collector_failure_that_has_since_recovered_is_not_current(tmp_path):
    """The metric ran again and returned no rows: nothing is wrong with it any more.

    This is why the OK "SQL returned no rows" rows must reach the report even though they are
    never charted. Filtering them out in SQL looked equivalent and was not — with them gone, the
    cleared failure was the newest thing the metric had, so 192.0.2.250 reported four-hour-old
    auth failures as present long after the metric had recovered.
    """
    from db_ops.reports.server_report import load_server_series

    store = _store(tmp_path, [
        _result(metric_code="LOCK_TRANSACTION_HOLDERS", metric_item=None, metric_value=None,
                status="WARNING", error_type="AUTH_FAILED",
                message="sqlserver connect to 192.0.2.115:1433 failed: Login failed.",
                collected_at="2026-08-01T03:05:00Z"),
        _result(metric_code="LOCK_TRANSACTION_HOLDERS", metric_item=None, metric_value=None,
                status="OK", message="SQL returned no rows.",
                collected_at="2026-08-01T07:09:00Z"),
    ])

    series, _omitted = load_server_series(store.sqlite_path, server_id=SERVER, days=7, as_of=AS_OF)

    assert series == []


# --------------------------------------------------------------------------- #
# P0-5 — the fleet page renders the severity the classifier gave
# --------------------------------------------------------------------------- #
def test_a_current_critical_becomes_a_critical_card_not_an_info_paragraph():
    """42 blocked sessions on 192.0.2.115 had no dedicated card: build_triage only had
    hand-coded cards for selected conditions, and everything else was concatenated into one
    ``Documented inventory findings`` card at severity ``info``."""
    problems = build_metric_problems([
        _row("LOCK_BLOCKING_SESSIONS", "SALESDB", "42", status="CRITICAL",
             unit="blocked_sessions",
             message="database=SALESDB, blocked_sessions=42, max_wait_seconds=2"),
    ])
    cards = _metric_problem_cards([
        {"role": "ACMESQL01", "ip": "192.0.2.115", "problems": problems},
    ])

    assert [card["sev"] for card in cards] == ["crit"]
    assert "Blocking" in cards[0]["title"]
    assert "192.0.2.115" in cards[0]["body"]
    assert "sampled 2026-08-01T07:00:00Z UTC" in cards[0]["body"]
    assert cards[0]["action"]


def test_a_report_judged_latency_finding_reaches_the_fleet_page():
    """PERFORMANCE_IO_LATENCY returns OK on every branch of its SQL, so the fleet page — which
    trusted the collector's status — never saw the 95.46 ms condition its own server page showed."""
    problems = build_metric_problems([
        _row("PERFORMANCE_IO_LATENCY", r"E:\SALESDB.mdf", "95.46", status="OK", unit="ms"),
    ])

    assert [group["severity"] for group in problems] == ["CRITICAL"]
    assert _metric_problem_cards([{"role": "ACMESQL01", "ip": "192.0.2.115",
                                   "problems": problems}])[0]["sev"] == "crit"


def test_monitoring_gaps_get_their_own_cards():
    cards = _monitoring_gap_cards([{
        "role": "ACMESQL01", "ip": "192.0.2.115",
        "freshness_detail": {"failed": ["DATABASE_STATUS"], "late": ["LOG_RECENT_CRITICAL"],
                             "notCollected": [{"code": "MAINTENANCE_INDEX_USAGE"}]},
    }])

    by_sev = {card["sev"]: card for card in cards}
    assert "crit" in by_sev and "DATABASE_STATUS" in by_sev["crit"]["body"]
    assert any("LOG_RECENT_CRITICAL" in card["body"] for card in cards)
    assert any("MAINTENANCE_INDEX_USAGE" in card["body"] for card in cards)


def test_a_condition_priority_attention_already_explains_is_not_duplicated():
    """Disk, backup and failed-login cards already exist with their own wording and action;
    emitting a generic card for the same metric would print the finding twice."""
    problems = build_metric_problems([
        _row("STORAGE_DISK_FREE_SPACE", "D:", "1.2", status="CRITICAL", unit="GB"),
    ])

    assert _metric_problem_cards([{"role": "x", "ip": "1.2.3.4", "problems": problems}]) == []
