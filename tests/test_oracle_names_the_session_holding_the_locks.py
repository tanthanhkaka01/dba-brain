"""Oracle LOCK_TRANSACTION_HOLDERS names root locking sessions and grades by duration and waiters.

009_oracle_blocking_sessions.sql surfaces waiting victim sessions, but diagnosing and resolving
cascading lock contention requires identifying the root holder session, its transaction age,
undo records generated, and the sessions queued behind it.

025_oracle_transaction_lock_holders.sql ports the SQL Server 025 contract to Oracle 10g+:
- Encapsulates numeric thresholds in a top-level params CTE (the Oracle collector convention).
- Inspects user sessions only (v$session.type = 'USER') and excludes the monitoring session itself
  via SYS_CONTEXT('USERENV', 'SID').
- Selects holders having an open transaction (v$transaction via taddr), blocking someone
  (v$session.blocking_session), or holding a user application lock (v$lock type 'UL', DBMS_LOCK).
- Emits detail rows for findings only: tran_seconds >= warning (300s), blocking another session,
  or idle with a user lock >= ul_idle_warning_seconds (300s).
- Grades CRITICAL when an old transaction blocks someone (>= 900s), an abandoned user lock holder
  blocks someone (>= 60s), or an idle user lock holder exceeds ul_idle_critical_seconds (1800s);
  otherwise WARNING.
- Aggregates up to 50 blocked SIDs via XMLAGG/XMLELEMENT (valid across Oracle 10g+ without LISTAGG).
- Emits a single LOGGING summary row counting unproblematic holders, omitted when none exist.
- Orders CRITICAL rows first, then descending by the longer of tran_seconds or ul_idle_seconds.
- Read-only: contains no destructive statements.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR_FILE = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "oracle" / "025_oracle_transaction_lock_holders.sql"

NUMERIC_THRESHOLDS = {
    "warning_tran_seconds": 300,
    "critical_tran_seconds": 900,
    "ul_idle_warning_seconds": 300,
    "ul_idle_critical_seconds": 1800,
    "ul_idle_abandoned_seconds": 60,
}


def _sql_text() -> str:
    return COLLECTOR_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")


def _sql_code() -> str:
    """The SQL text without comments and normalized to single whitespace."""
    lines = [
        line.split("--", 1)[0]
        for line in _sql_text().splitlines()
        if not line.lstrip().startswith("--")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


def test_oracle_lock_holders_collector_file_exists_on_disk():
    assert COLLECTOR_FILE.is_file(), f"Collector file missing: {COLLECTOR_FILE}"


def test_oracle_lock_holders_declares_thresholds_in_top_level_params_cte():
    text = _sql_text()
    match = re.search(r"WITH\s+params\s+AS\s*\(\s*SELECT\s+(.*?)\s+FROM\s+dual\s*\)", text, re.IGNORECASE | re.DOTALL)
    assert match, "Thresholds must be encapsulated in a top-level WITH params AS (SELECT ... FROM dual) CTE"
    params_body = match.group(1)

    for name, expected_val in NUMERIC_THRESHOLDS.items():
        pattern = rf"{expected_val}\s+AS\s+{name}"
        assert re.search(pattern, params_body, re.IGNORECASE), (
            f"params CTE must declare {expected_val} AS {name}"
        )


def test_oracle_lock_holders_filters_user_sessions_and_excludes_collector_session():
    code = _sql_code()
    assert "s.type = 'USER'" in code or "s.type='USER'" in code.replace(" ", "")
    assert "SYS_CONTEXT('USERENV', 'SID')" in code or "SYS_CONTEXT('USERENV','SID')" in code.replace(" ", "")
    assert "s.taddr" in code
    assert "blocking_session" in code
    assert "UL" in code


def test_oracle_lock_holders_critical_severity_strictly_requires_blocking_or_extreme_idle():
    code = _sql_code()

    assert "p.critical_tran_seconds" in code
    assert "p.ul_idle_abandoned_seconds" in code
    assert "p.ul_idle_critical_seconds" in code

    case_match = re.search(r"CASE(.*?)END AS status", code, re.IGNORECASE)
    assert case_match, "Expected status CASE expression"
    case_body = case_match.group(1)

    critical_branches = [b for b in case_body.split("WHEN")[1:] if "'CRITICAL'" in b]
    assert critical_branches, "Expected CRITICAL branch in status CASE"
    crit_text = critical_branches[0]

    assert "critical_tran_seconds" in crit_text and "blocked_sessions" in crit_text
    assert "ul_idle_abandoned_seconds" in crit_text and "blocked_sessions" in crit_text
    assert "ul_idle_critical_seconds" in crit_text


def test_oracle_lock_holders_detail_rows_are_findings_only_with_standard_columns():
    code = _sql_code()

    # Findings filter
    assert "p.warning_tran_seconds" in code
    assert "p.ul_idle_warning_seconds" in code

    # Metric item, value, unit
    assert "'SID='" in code
    assert "'lock(s)'" in code

    # Required message keys
    required_keys = [
        "sid=", "serial=", "blocked_sessions=", "tran_seconds=", "used_undo_records=",
        "user_locks=", "ul_idle_seconds=", "status=", "username=", "osuser=",
        "machine=", "program=", "module=", "event=", "blocking_session=",
        "sql_id=", "prev_sql_id=", "blocked_sids=",
    ]
    for key in required_keys:
        assert key in code, f"Message must include key: {key}"


def test_oracle_lock_holders_emits_logging_summary_row_for_unproblematic_holders():
    code = _sql_code()

    assert "'holders_summary'" in code
    assert "'session(s)'" in code
    assert "'LOGGING'" in code
    assert "HAVING COUNT(*) > 0" in code
    assert "sessions=" in code
    assert "total_locks=" in code
    assert "max_tran_seconds=" in code


def test_oracle_lock_holders_orders_critical_first_then_lock_seconds_descending():
    code = _sql_code()
    assert "ORDER BY sort_priority ASC, lock_seconds DESC" in code or "ORDER BY sort_priority, lock_seconds DESC" in code


def test_oracle_lock_holders_projects_five_standard_metric_columns():
    code = _sql_code().upper()
    for col in ("METRIC_ITEM", "METRIC_VALUE", "METRIC_UNIT", "STATUS", "MESSAGE"):
        assert f"AS {col}" in code or f"AS \"{col}\"" in code, (
            f"Collector missing projected column {col}"
        )


def test_oracle_lock_holders_contains_no_destructive_statements():
    code = _sql_code().upper()
    destructive = ["DROP ", "DELETE ", "TRUNCATE ", "ALTER ", "KILL ", "UPDATE ", "INSERT "]
    for word in destructive:
        assert word not in code, f"Collector must be read-only and not contain {word}"
