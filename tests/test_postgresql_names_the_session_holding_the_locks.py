"""PostgreSQL identifies client backends holding transaction and session advisory locks.

LOCK_TRANSACTION_HOLDERS (metric 34) provides the holder side behind LOCK_BLOCKING_SESSIONS
(009_postgresql_blocking_sessions.sql, which grades the waiter by wait duration) and
POSTGRES_LONG_TRANSACTIONS (054_postgresql_transactions.sql, which grades all transactions by age).

Active PostgreSQL clusters routinely have dozens or hundreds of client backends holding routine
locks for active transactions with nothing wrong. Emitting one row per holder at OK or LOGGING would
hit the collector's row cap on busy clusters and drown real incidents. Detail rows are therefore
restricted to genuine findings: transactions open at or past warning_tran_seconds (300 s), sessions
blocking any other backend, or sessions holding a session advisory lock idle at or past
advisory_idle_warning_seconds (300 s).

CRITICAL status requires an old transaction that is actively blocking waiters, or a session-owned
advisory lock held by an idle session (abandoned after 60 s while blocking someone, or idle for 30
minutes regardless of waiters). Non-finding lock-holding sessions are rolled up into a single
'holders_summary' row at status LOGGING.

Unlike SQL Server, where dynamic lock manager allocations can grow to millions of locks and stall
DMV queries, PostgreSQL's shared lock table (LockMethodData) is statically allocated at startup,
bounded by max_locks_per_transaction * (max_connections + max_prepared_transactions), with row locks
stored on tuple page infomasks rather than pg_locks. Thus no dynamic scan skip guard is required.

These tests inspect the SQL text offline to verify its contract, threshold declarations,
compatibility with PostgreSQL 10 (no MATERIALIZED CTE keyword), and read-only safety without
requiring a live database connection.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_FILE = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "postgresql" / "025_postgresql_transaction_lock_holders.sql"

PARAMS_THRESHOLDS = {
    "warning_tran_seconds": 300,
    "critical_tran_seconds": 900,
    "advisory_idle_warning_seconds": 300,
    "advisory_idle_critical_seconds": 1800,
    "advisory_idle_abandoned_seconds": 60,
}


def _raw_text() -> str:
    """Read the collector SQL file as normalized UTF-8 text."""
    return SQL_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")


def _code() -> str:
    """The SQL code without comment lines, whitespace collapsed."""
    lines = [
        line.split("--", 1)[0]
        for line in _raw_text().splitlines()
        if not line.lstrip().startswith("--")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# 1. Thresholds and params CTE
# --------------------------------------------------------------------------- #
def test_params_cte_declares_all_agreed_thresholds():
    """All numeric thresholds must be declared in a params CTE matching SQL Server defaults."""
    code = _code()
    cte_match = re.search(r"WITH\s+params\s+AS\s*\(\s*SELECT\s+(.*?)\s*\)", code, re.IGNORECASE)
    assert cte_match, "025_postgresql_transaction_lock_holders.sql must begin with a params CTE"
    params_body = cte_match.group(1)

    for name, expected_value in PARAMS_THRESHOLDS.items():
        pattern = rf"{expected_value}(?:::bigint)?\s+AS\s+{name}\b"
        assert re.search(pattern, params_body, re.IGNORECASE), (
            f"params CTE must declare {name} with default value {expected_value}"
        )


# --------------------------------------------------------------------------- #
# 2. Filtering: client backends only and not itself
# --------------------------------------------------------------------------- #
def test_collector_filters_for_client_backends_and_excludes_itself():
    """Collector must inspect client backends only and exclude its own pg_backend_pid()."""
    code = _code()
    assert "backend_type = 'client backend'" in code, (
        "Collector must restrict target sessions to backend_type = 'client backend'"
    )
    assert "pg_backend_pid()" in code, (
        "Collector must exclude its own backend via pg_backend_pid()"
    )
    assert re.search(r"\bpid\s*<>\s*pg_backend_pid\(\)", code), (
        "Collector must filter pid <> pg_backend_pid()"
    )


# --------------------------------------------------------------------------- #
# 3. Grading: CRITICAL and WARNING status rules
# --------------------------------------------------------------------------- #
def test_critical_status_requires_old_and_blocking_or_advisory_idle():
    """CRITICAL severity requires an old transaction that is blocking, or abandoned/idle advisory locks."""
    code = _code()
    case_match = re.search(r"CASE\s+(WHEN\s+.*?)\s+ELSE\s+'WARNING'\s+END\s+AS\s+status", code, re.IGNORECASE)
    assert case_match, "Detail status CASE must evaluate conditions and fall back to WARNING"
    case_body = case_match.group(1)

    # 1. Old transaction AND blocking
    assert re.search(
        r"xact_seconds\s*>=\s*p\.critical_tran_seconds\s+AND\s+h\.blocked_sessions\s*>\s*0",
        case_body,
        re.IGNORECASE,
    ), "CRITICAL must require xact_seconds >= critical_tran_seconds AND blocked_sessions > 0"

    # 2. Advisory idle abandoned AND blocking
    assert re.search(
        r"advisory_idle_seconds\s*>=\s*p\.advisory_idle_abandoned_seconds\s+AND\s+h\.blocked_sessions\s*>\s*0",
        case_body,
        re.IGNORECASE,
    ), "CRITICAL must require advisory_idle_seconds >= advisory_idle_abandoned_seconds AND blocked_sessions > 0"

    # 3. Advisory idle critical
    assert re.search(
        r"advisory_idle_seconds\s*>=\s*p\.advisory_idle_critical_seconds",
        case_body,
        re.IGNORECASE,
    ), "CRITICAL must trigger on advisory_idle_seconds >= advisory_idle_critical_seconds"


# --------------------------------------------------------------------------- #
# 4. Detail rows: emitted for findings only
# --------------------------------------------------------------------------- #
def test_detail_rows_are_emitted_only_for_findings():
    """Detail rows are emitted only for findings (old tran, blocking anyone, or idle advisory)."""
    code = _code()
    detail_where = re.search(
        r"FROM\s+holders\s+h\s+CROSS\s+JOIN\s+params\s+p\s+WHERE\s+(.*?)\s+UNION\s+ALL",
        code,
        re.IGNORECASE,
    )
    assert detail_where, "Detail query must filter holders before UNION ALL"
    where_clause = detail_where.group(1)

    assert "xact_seconds >= p.warning_tran_seconds" in where_clause
    assert "blocked_sessions > 0" in where_clause
    assert "advisory_idle_seconds >= p.advisory_idle_warning_seconds" in where_clause

    # Verify detail row metric_item, metric_value, metric_unit
    assert "'pid=' || h.pid" in code
    assert "h.lock_count::text AS metric_value" in code
    assert "'lock(s)'::text AS metric_unit" in code


# --------------------------------------------------------------------------- #
# 5. Summary row: rolls up non-findings into a single LOGGING row
# --------------------------------------------------------------------------- #
def test_summary_row_reports_logging_for_non_findings_with_up_to_200_pids():
    """Non-finding holders must be rolled up into one 'holders_summary' row at status LOGGING."""
    code = _code()
    # The final ORDER BY: the first one is inside ARRAY_AGG, before the summary row.
    summary_part = code[code.index("'holders_summary'"):code.rindex("ORDER BY")]

    assert "COUNT(*)::text AS metric_value" in summary_part
    assert "'session(s)'::text AS metric_unit" in summary_part
    assert "'LOGGING' AS status" in summary_part
    assert "HAVING COUNT(*) > 0" in summary_part, "Summary row must be omitted when no non-findings exist"

    # Complementary filter
    assert "xact_seconds < p.warning_tran_seconds" in summary_part
    assert "blocked_sessions = 0" in summary_part
    assert "advisory_idle_seconds < p.advisory_idle_warning_seconds" in summary_part

    # Message keys and slice of pids up to 200
    assert "'sessions=' || COUNT(*)" in summary_part
    assert "', total_locks='" in summary_part
    assert "', max_xact_seconds='" in summary_part
    assert "', pids='" in summary_part
    assert "[1:200]" in summary_part, "Summary row must cap listed pids to 200"


# --------------------------------------------------------------------------- #
# 6. Message structure and last_query placement
# --------------------------------------------------------------------------- #
def test_detail_message_contains_all_required_keys_with_last_query_strictly_last():
    """Detail row message must contain all 15 required comma-separated keys with last_query at the end."""
    raw = _raw_text()
    code = _code()

    required_keys = [
        "pid=",
        "blocked_sessions=",
        "xact_seconds=",
        "session_advisory_locks=",
        "advisory_idle_seconds=",
        "state=",
        "wait_event_type=",
        "wait_event=",
        "user=",
        "client_addr=",
        "application_name=",
        "database=",
        "lock_count=",
        "blocked_pids=",
        "last_query=",
    ]

    for key in required_keys:
        assert f"'{key}'" in code or f"', {key}'" in code, f"Message must include key {key}"

    # Verify last_query is the very last key in the detail message expression
    detail_select = code[code.index("'pid=' || h.pid"):code.index("AS message")]
    last_key_idx = detail_select.rindex("', last_query='")
    assert last_key_idx > 0, "last_query must be positioned in detail message"
    tail = detail_select[last_key_idx:]
    # No other key should appear after last_query
    for key in required_keys[:-1]:
        assert f", {key}" not in tail

    # Check query truncation to 200 chars and newline/comma replacement
    assert "SUBSTRING(COALESCE(h.query, '') FROM 1 FOR 200)" in code or "LEFT(COALESCE(h.query, ''), 200)" in code
    assert "blocked_pids" in code
    assert "[1:50]" in code, "blocked_pids must be capped at 50"


# --------------------------------------------------------------------------- #
# 7. PostgreSQL 10 compatibility (no MATERIALIZED keyword)
# --------------------------------------------------------------------------- #
def test_collector_is_compatible_with_postgresql_10_without_materialized_ctes():
    """PostgreSQL 10 does not support the MATERIALIZED CTE keyword."""
    code = _code()
    assert not re.search(r"\bMATERIALIZED\b", code, re.IGNORECASE), (
        "PostgreSQL 10 does not support MATERIALIZED; CTEs must omit this keyword"
    )


# --------------------------------------------------------------------------- #
# 8. Read-only safety (no destructive statements)
# --------------------------------------------------------------------------- #
def test_collector_contains_no_destructive_statements():
    """Collector must be read-only with no DDL, DML, or function creation."""
    code = _code().upper()
    destructive_patterns = [
        r"\bDROP\s+",
        r"\bTRUNCATE\s+",
        r"\bDELETE\s+FROM\b",
        r"\bALTER\s+",
        r"\bUPDATE\s+",
        r"\bINSERT\s+INTO\b",
        r"\bKILL\b",
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION|PROCEDURE|INDEX|SCHEMA|DATABASE)\b",
        r"\bGRANT\s+",
        r"\bREVOKE\s+",
    ]
    for pattern in destructive_patterns:
        assert not re.search(pattern, code), f"Collector must not match destructive pattern {pattern}"
