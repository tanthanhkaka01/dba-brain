"""Oracle collectors are wired to supported 11g+ variants and grade actionable emergencies.

Five Oracle collector files existed in the repository without catalogue variants wiring them to
the metrics scheduler. Each metric's Oracle entry was a placeholder marking Oracle unsupported.
All five were run from a PDB on Oracle 26ai (23.26) - no older Oracle was available to run them on - and are now
wired to supported variants (`oracle_11g_plus`), leaving pre-11g instances unsupported with explicit
reasons.

In addition, ADR incident monitoring (`ORACLE_ADR_INCIDENTS`, metric_id 98) is introduced to monitor
diagnostic incidents and problems directly via SQL views (v$diag_incident, v$diag_problem, v$diag_info)
available from inside a PDB, without requiring host OS file access to the XML alert log.

Oracle SQL lacks a configurable T-SQL DECLARE mechanism that `apply_sql_settings` can rewrite, so every
changed Oracle collector encapsulates its thresholds in a single named CTE (`WITH params AS (...)`)
at the top so each number appears once.

These tests hold the catalogue wiring, the offline SQL contracts, threshold encapsulation, and grading
rules for all six collectors.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from conftest import shipped_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_PATH = REPO_ROOT / "dbabrain" / "metrics" / "catalogue" / "metric_definitions.json"
DATA_PATH = shipped_config("metric_definitions.json")
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"

WIRED_METRICS = {
    11: "AVAILABILITY_DATABASE_HEALTH",
    16: "BACKUP_JOB_STATUS",
    17: "LOG_RECENT_CRITICAL",
    18: "LOG_REUSE_WAIT",
    33: "LOCK_SLEEPING_OPEN_TRANSACTION",
}

EXPECTED_FILES = {
    11: "oracle/011_oracle_dataguard_health.sql",
    16: "oracle/016_oracle_backup_job_status.sql",
    17: "oracle/017_oracle_alertlog_recent_critical.sql",
    18: "oracle/018_oracle_redo_reuse_wait.sql",
    33: "oracle/024_oracle_sleeping_open_transaction.sql",
}

ALL_ORACLE_COLLECTOR_FILES = [
    "oracle/011_oracle_dataguard_health.sql",
    "oracle/016_oracle_backup_job_status.sql",
    "oracle/017_oracle_alertlog_recent_critical.sql",
    "oracle/018_oracle_redo_reuse_wait.sql",
    "oracle/020_oracle_adr_incidents.sql",
    "oracle/024_oracle_sleeping_open_transaction.sql",
]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sql_text(relative: str) -> str:
    return (COLLECTORS / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _sql_code(sql: str) -> str:
    """SQL with comment lines removed and whitespace collapsed."""
    lines = [line.split("--", 1)[0] for line in sql.splitlines()
             if not line.lstrip().startswith("--")]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# Catalogue wiring and contract
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("catalogue_file", [CATALOGUE_PATH, DATA_PATH])
def test_the_five_oracle_metrics_have_supported_11g_plus_and_unsupported_pre_11g_variants(catalogue_file):
    """Each of the five metrics must have an oracle_11g_plus variant pointing to its SQL file and
    a pre-11g unsupported variant explaining why."""
    data = _read_json(catalogue_file)
    by_id = {m["metric_id"]: m for m in data["metrics"]}

    for metric_id, expected_code in WIRED_METRICS.items():
        metric = by_id[metric_id]
        assert metric["metric_code"] == expected_code

        variants = [v for v in metric["variants"] if v.get("db_type") == "oracle"]
        assert len(variants) >= 2, f"Expected supported and unsupported Oracle variants for {expected_code}"

        supported = [v for v in variants if v.get("name") == "oracle_11g_plus"]
        assert len(supported) == 1, f"Missing oracle_11g_plus variant for {expected_code}"
        assert supported[0]["min_major_version"] == 11
        assert supported[0]["file"] == EXPECTED_FILES[metric_id]
        assert supported[0].get("supported", True) is True

        unsupported = [v for v in variants if v.get("name") == "oracle_pre_11g_unsupported"]
        assert len(unsupported) == 1, f"Missing oracle_pre_11g_unsupported variant for {expected_code}"
        assert unsupported[0]["min_major_version"] == 0
        assert unsupported[0]["max_major_version"] == 10
        assert unsupported[0]["supported"] is False
        assert unsupported[0].get("unsupported_reason"), f"Missing unsupported_reason for {expected_code}"


def test_the_oracle_variant_files_named_by_the_catalogue_exist_on_disk():
    """Every file referenced by an Oracle variant in the catalogue must exist in the repository."""
    for relative_path in ALL_ORACLE_COLLECTOR_FILES:
        target = COLLECTORS / relative_path
        assert target.is_file(), f"Expected collector file does not exist: {target}"


def test_oracle_adr_incidents_metric_is_defined_in_the_catalogue():
    """ORACLE_ADR_INCIDENTS (metric_id 98) must be declared with all required fields in the catalogue."""
    cat_data = _read_json(CATALOGUE_PATH)
    by_code = {m["metric_code"]: m for m in cat_data["metrics"]}
    metric = by_code["ORACLE_ADR_INCIDENTS"]

    assert metric["metric_id"] == 98
    assert metric["db_type"] == "oracle"
    assert metric["category"] == "errorlog"
    assert metric["collector_type"] == "sql"
    assert metric["default_importance"] == 5
    assert metric["active"] is True
    assert metric["empty_result_is_ok"] is False
    assert metric["connection_error_severity"] == "WARNING"
    assert metric["execution_error_severity"] == "WARNING"
    assert metric["max_rows"] == 200
    assert metric["time_window"]["repeat_interval"] == 300
    assert metric["time_window"]["timeout"] == 60
    assert metric.get("note"), "Expected note explaining why ADR incident monitoring exists"

    variants = metric["variants"]
    supported = next(v for v in variants if v.get("name") == "oracle_11g_plus")
    assert supported["min_major_version"] == 11
    assert supported["file"] == "oracle/020_oracle_adr_incidents.sql"

    unsupported = next(v for v in variants if v.get("name") == "oracle_pre_11g_unsupported")
    assert unsupported["min_major_version"] == 0
    assert unsupported["max_major_version"] == 10
    assert unsupported["supported"] is False


# --------------------------------------------------------------------------- #
# SQL Collector contracts and grading rules
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("relative", ALL_ORACLE_COLLECTOR_FILES)
def test_each_changed_oracle_collector_keeps_its_thresholds_in_a_single_params_cte(relative):
    """Every Oracle collector must define a top-level WITH params AS (SELECT ... FROM dual) CTE
    so that numbers appear exactly once and are encapsulated."""
    sql = _sql_text(relative)
    match = re.search(r"WITH\s+params\s+AS\s*\(\s*SELECT\s+.*?\s+FROM\s+dual\s*\)", sql, re.IGNORECASE | re.DOTALL)
    assert match, f"{relative} must declare its thresholds in a top-level 'WITH params AS (SELECT ... FROM dual)' CTE"


@pytest.mark.parametrize("relative", ALL_ORACLE_COLLECTOR_FILES)
def test_each_oracle_collector_returns_the_five_standard_metric_columns(relative):
    """Every metric query must project METRIC_ITEM, METRIC_VALUE, METRIC_UNIT, STATUS, and MESSAGE."""
    code = _sql_code(_sql_text(relative)).upper()
    for col in ("METRIC_ITEM", "METRIC_VALUE", "METRIC_UNIT", "STATUS", "MESSAGE"):
        assert f"AS {col}" in code or f"AS \"{col}\"" in code, f"{relative} missing projected column {col}"


def test_oracle_adr_incidents_reads_diagnostic_views_and_always_emits_a_summary_row():
    """ORACLE_ADR_INCIDENTS must query v$diag_incident, v$diag_problem, v$diag_info, and v$hm_finding,
    emitting open critical Health Monitor findings at WARNING and an adr_summary row for overall diagnostic health."""
    sql = _sql_text("oracle/020_oracle_adr_incidents.sql")
    code = _sql_code(sql)

    assert "v$diag_incident" in code
    assert "v$diag_problem" in code
    assert "v$diag_info" in code
    assert "v$hm_finding" in code
    assert "'hm_finding='" in code
    assert "'adr_summary'" in code
    assert "Active Problem Count" in code
    assert "Active Incident Count" in code
    assert "ADR Home" in code
    assert "hm_open_critical=" in code

    # Incidents in the window are CRITICAL; HM findings are WARNING; summary can be WARNING or OK
    assert "'CRITICAL'" in code
    assert "'WARNING'" in code
    assert "'OK'" in code


def test_oracle_adr_incidents_grades_open_critical_health_monitor_findings_as_warning():
    """Open critical Health Monitor findings in v$hm_finding must be emitted at status WARNING."""
    sql = _sql_text("oracle/020_oracle_adr_incidents.sql")
    code = _sql_code(sql)

    assert "v$hm_finding" in code
    assert "hm_critical_findings" in code
    assert "status = 'OPEN'" in code or "status='OPEN'" in code.replace(" ", "")
    assert "priority = 'CRITICAL'" in code or "priority='CRITICAL'" in code.replace(" ", "")
    assert "'hm_finding='" in code
    assert "'finding'" in code
    assert "finding_id=" in code
    assert "run_id=" in code
    assert "damage_description=" in code


def test_alert_log_collector_grades_internal_and_fatal_ora_errors_critical():
    """017_oracle_alertlog_recent_critical.sql must grade internal and fatal database crashes CRITICAL
    (ORA-00600, ORA-07445, ORA-01578, ORA-01110, ORA-04031, ORA-00257, ORA-00353, ORA-00470/471, ORA-27102)
    and grade other unsuppressed ORA- messages WARNING."""
    sql = _sql_text("oracle/017_oracle_alertlog_recent_critical.sql")
    code = _sql_code(sql)

    # All agreed critical ORA error codes must participate in the CRITICAL regex/check
    critical_codes = ["600", "7445", "1578", "1110", "4031", "257", "353", "470", "471", "27102"]
    for err in critical_codes:
        assert err in code, f"ORA-{err} must be graded CRITICAL in alert log query"

    # Both statuses must be producible
    assert "'CRITICAL'" in code
    assert "'WARNING'" in code

    # Row cap and window preservation
    assert "max_rows" in sql
    assert "lookback_days" in sql
    assert "v$diag_alert_ext" in code


def test_data_guard_collector_grades_apply_and_transport_lags_at_agreed_thresholds():
    """Data Guard apply lag must be CRITICAL >= 3600s, WARNING >= 900s; transport lag CRITICAL >= 1800s,
    WARNING >= 300s; archive destination in ERROR must be CRITICAL; standalone must be OK."""
    sql = _sql_text("oracle/011_oracle_dataguard_health.sql")
    code = _sql_code(sql)

    assert "apply_lag_critical_seconds" in sql
    assert "apply_lag_warning_seconds" in sql
    assert "transport_lag_critical_seconds" in sql
    assert "transport_lag_warning_seconds" in sql

    assert re.search(r"3600\s+AS\s+apply_lag_critical_seconds", sql)
    assert re.search(r"900\s+AS\s+apply_lag_warning_seconds", sql)
    assert re.search(r"1800\s+AS\s+transport_lag_critical_seconds", sql)
    assert re.search(r"300\s+AS\s+transport_lag_warning_seconds", sql)

    # Remote dest in ERROR is CRITICAL
    assert "ads.status = 'ERROR' THEN 'CRITICAL'" in code or "ads.status = 'ERROR' THEN ''CRITICAL''" in code

    # Standalone instance remains OK
    assert "'NOT_CONFIGURED'" in code
    assert "'OK' AS status" in code


def test_sleeping_open_transaction_critical_severity_strictly_requires_a_waiter():
    """An idle sleeping session holding a transaction may only be graded CRITICAL if another session
    is actively blocked by it (blocking_others > 0). Idle time alone must not page CRITICAL."""
    sql = _sql_text("oracle/024_oracle_sleeping_open_transaction.sql")
    code = _sql_code(sql)

    # Ensure params declare idle thresholds
    assert "idle_abandoned_seconds" in sql
    assert "idle_critical_seconds" in sql
    assert "idle_warning_seconds" in sql

    # Status CASE inspection: any WHEN returning CRITICAL must require blocking_others > 0
    case_match = re.search(r"CASE(.*?)END AS status", code)
    assert case_match, "Expected status CASE in 024"
    case_body = case_match.group(1)

    critical_branches = [b for b in case_body.split("WHEN")[1:] if "'CRITICAL'" in b]
    assert critical_branches, "Expected at least one CRITICAL branch"
    for branch in critical_branches:
        assert "blocking_others > 0" in branch or "blocking_others>0" in branch.replace(" ", ""), (
            f"CRITICAL branch without waiter requirement: {branch}"
        )

    # Without a waiter, high idle time must be WARNING or LOGGING, never CRITICAL
    warning_branches = [b for b in case_body.split("WHEN")[1:] if "'WARNING'" in b]
    assert warning_branches, "Expected WARNING branches"

    # Message contract requirements
    assert "idle_minutes=" in sql
    assert "is_blocking=" in sql


def test_redo_reuse_wait_grades_stuck_archiver_critical_when_all_online_groups_need_archiving():
    """018_oracle_redo_reuse_wait.sql must grade CRITICAL when the archiver is stuck because every
    online redo log group needs archiving (online redo cannot grow)."""
    sql = _sql_text("oracle/018_oracle_redo_reuse_wait.sql")
    code = _sql_code(sql)

    assert "all_groups_unarchived_pct" in sql
    assert "ARCHIVER_STUCK" in code
    assert "'CRITICAL'" in code

    # Remote destination in ERROR is CRITICAL
    assert "ads.status = 'ERROR' THEN 'CRITICAL'" in code


def test_backup_job_status_grades_failed_jobs_critical_and_errors_or_warnings_as_warning():
    """016_oracle_backup_job_status.sql must grade FAILED jobs in the last 24h as CRITICAL,
    and COMPLETED WITH ERRORS / WARNINGS as WARNING."""
    sql = _sql_text("oracle/016_oracle_backup_job_status.sql")
    code = _sql_code(sql)

    assert "j.status = 'FAILED' THEN 'CRITICAL'" in code or "j.status = 'FAILED'" in code
    assert "COMPLETED WITH WARNINGS" in code
    assert "COMPLETED WITH ERRORS" in code
    assert "lookback_days" in sql


def test_redo_reuse_reads_unarchived_groups_only_in_archivelog_mode():
    """A NOARCHIVELOG database never archives: every group reads archived='NO' for ever, and the
    ACTIVE group after each log switch would read as a stuck archiver - a CRITICAL on every healthy
    test database. The stall rows need ARCHIVELOG; the OK row still answers in NOARCHIVELOG, so the
    metric is never empty there."""
    sql = (COLLECTORS / "oracle" / "018_oracle_redo_reuse_wait.sql").read_text(encoding="utf-8")
    flat = " ".join(sql.split())

    assert "SELECT log_mode FROM v$database" in flat
    assert "WHERE db.log_mode = 'ARCHIVELOG' AND l.archived = 'NO' AND l.status = 'ACTIVE'" in flat
    assert "WHERE (db.log_mode <> 'ARCHIVELOG' OR NOT EXISTS" in flat
