"""Multixact age is monitored alongside transaction-ID age, and primary replication lag is graded.

Item 1.136 addresses two integrity and replication gaps in PostgreSQL monitoring:

1. POSTGRES_XID_WRAPAROUND (056_postgresql_xid_wraparound.sql):
   PostgreSQL tracks transaction IDs (datfrozenxid) and multixact IDs (datminmxid) in separate
   32-bit cyclical spaces. If autovacuum cannot freeze either horizon in time, the postmaster halts
   the cluster in emergency shutdown to avoid wraparound data corruption. Previously, the collector
   evaluated only datfrozenxid, leaving multixact starvation completely unmonitored.
   Now, each connectable database reports two distinct rows (naming ":: xid" and ":: multixact" in
   metric_item), graded against autovacuum_freeze_max_age and autovacuum_multixact_freeze_max_age
   respectively at >= 90% CRITICAL and >= 75% WARNING.

2. POSTGRES_REPLICATION (050_postgresql_replication.sql):
   Previously, the primary view only checked whether state = 'streaming' (emitting WARNING if not)
   and printed sent_lag_bytes in the message without grading it. Additionally, a primary without any
   connected standby raised a WARNING, which violated the fundamental house rule that "not
   configured is a state, not a failure".
   Now, the primary evaluates replay lag in bytes (pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn))
   against approved thresholds (>= 1 GB CRITICAL, >= 100 MB WARNING, matching the standby's scale),
   reports sent_lsn and sent_lag_bytes alongside replay coordinates, safely guards pg_current_wal_lsn()
   against standby execution (where it throws a fatal recovery error), and reports standalone primaries
   as status OK.

These offline tests read the SQL text directly and hold the query contracts without requiring a live database.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
XID_SQL_PATH = COLLECTORS / "postgresql" / "056_postgresql_xid_wraparound.sql"
REPL_SQL_PATH = COLLECTORS / "postgresql" / "050_postgresql_replication.sql"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _code(sql: str) -> str:
    """The SQL without comment lines and with collapsed whitespace: the contract, not layout."""
    lines = [
        line.split("--", 1)[0]
        for line in sql.splitlines()
        if not line.lstrip().startswith("--")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# POSTGRES_XID_WRAPAROUND: 056_postgresql_xid_wraparound.sql
# --------------------------------------------------------------------------- #

def test_xid_wraparound_sql_checks_both_transaction_and_multixact_ages():
    """Both datfrozenxid and datminmxid are evaluated to detect wraparound risk."""
    code = _code(_text(XID_SQL_PATH))
    assert "age(datfrozenxid)" in code
    assert "mxid_age(datminmxid)" in code


def test_xid_wraparound_sql_reads_both_freeze_max_age_settings():
    """Settings autovacuum_freeze_max_age and autovacuum_multixact_freeze_max_age are both read."""
    code = _code(_text(XID_SQL_PATH))
    assert "current_setting('autovacuum_freeze_max_age')" in code
    assert "current_setting('autovacuum_multixact_freeze_max_age')" in code


def test_xid_wraparound_sql_names_age_type_in_metric_item():
    """Metric item names which age is reported so operators can distinguish xid from multixact."""
    code = _code(_text(XID_SQL_PATH))
    assert "::" in code
    assert "'xid'" in code
    assert "'multixact'" in code


def test_xid_wraparound_sql_declares_approved_threshold_ratios_in_cte():
    """Approved thresholds (90% CRITICAL, 75% WARNING) are declared once in the thresholds CTE."""
    code = _code(_text(XID_SQL_PATH))
    assert re.search(r"WITH\s+thresholds\s+AS\s*\(", code, re.IGNORECASE)
    assert "0.90" in code or "0.9" in code
    assert "0.75" in code


def test_xid_wraparound_sql_grades_critical_and_warning_against_max_age():
    """Breaching 90% of freeze max age is CRITICAL; breaching 75% is WARNING."""
    code = _code(_text(XID_SQL_PATH))
    assert re.search(r"WHEN\s+.*?critical_ratio\s+THEN\s+'CRITICAL'", code, re.IGNORECASE)
    assert re.search(r"WHEN\s+.*?warning_ratio\s+THEN\s+'WARNING'", code, re.IGNORECASE)
    assert "ELSE 'OK'" in code


def test_xid_wraparound_sql_message_carries_required_key_value_pairs():
    """The message contract requires age, freeze_max_age, and percent_consumed."""
    code = _code(_text(XID_SQL_PATH))
    for key in ("freeze_max_age=", "percent_consumed="):
        assert key in code, f"message must include {key}"
    # The key is each row's own label (`xid_age` or `mxid_age`) - seen on the .251 lab's 18.6.
    assert "r.age_label || '=' || r.current_age" in code
    assert "'xid_age' AS age_label" in code and "'mxid_age' AS age_label" in code


def test_xid_wraparound_sql_filters_connectable_databases():
    """datallowconn is enforced so template or non-connectable databases are excluded."""
    code = _code(_text(XID_SQL_PATH))
    assert "WHERE datallowconn" in code


# --------------------------------------------------------------------------- #
# POSTGRES_REPLICATION: 050_postgresql_replication.sql
# --------------------------------------------------------------------------- #

def test_primary_replication_grades_replay_lag_with_approved_thresholds():
    """Primary replay lag >= 1 GB is CRITICAL, >= 100 MB is WARNING."""
    code = _code(_text(REPL_SQL_PATH))
    assert "1073741824" in code  # 1 GB in bytes
    assert "104857600" in code   # 100 MB in bytes
    assert "pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)" in code


def test_primary_replication_guards_pg_current_wal_lsn_against_standby_execution():
    """On a standby, pg_current_wal_lsn() raises an error; it must be guarded by NOT pg_is_in_recovery()."""
    code = _code(_text(REPL_SQL_PATH))
    assert "NOT pg_is_in_recovery()" in code
    # Both in WHERE clause and guarded inside CASE
    assert re.search(r"CASE\s+WHEN\s+NOT\s+pg_is_in_recovery\(\)", code, re.IGNORECASE)


def test_primary_replication_message_names_sent_lsn_and_sent_lag_bytes():
    """Message on primary includes sent_lsn, sent_lag_bytes, replay_lsn, and replay_lag_bytes."""
    code = _code(_text(REPL_SQL_PATH))
    for field in ("sent_lsn=", "sent_lag_bytes=", "replay_lsn=", "replay_lag_bytes="):
        assert field in code, f"primary replication message must include {field}"


def test_primary_replication_reports_standalone_primary_as_ok():
    """A primary with no connected standby is a valid configuration, not an alerting failure."""
    code = _code(_text(REPL_SQL_PATH))
    # Must return OK when NOT pg_is_in_recovery() and no streaming replicas exist
    standalone_block = re.search(
        r"'replication'\s+AS\s+metric_item.*?NOT\s+EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+pg_stat_replication\s*\)",
        code,
        re.IGNORECASE,
    )
    assert standalone_block is not None, "standalone primary block must exist"
    assert "'OK' AS status" in standalone_block.group(0), "standalone primary must report status OK"
    assert "'WARNING' AS status" not in standalone_block.group(0)


def test_standby_replication_retains_paused_replay_critical_and_lag_thresholds():
    """Standby checks continue to trigger CRITICAL on paused replay or >= 100 MB lag, WARNING on 10 MB."""
    code = _code(_text(REPL_SQL_PATH))
    assert "pg_is_wal_replay_paused()" in code
    assert "10485760" in code   # 10 MB standby warning
    assert "WHERE pg_is_in_recovery()" in code


# --------------------------------------------------------------------------- #
# Destructive statement safety
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", (XID_SQL_PATH, REPL_SQL_PATH))
def test_neither_collector_contains_destructive_statements(path: Path):
    """Collectors are strictly read-only and must never perform DDL, DML, or session alteration."""
    code = _code(_text(path))
    forbidden = ["DROP", "DELETE", "TRUNCATE", "KILL", "ALTER", "CREATE TABLE", "INSERT INTO"]
    for keyword in forbidden:
        assert not re.search(rf"\b{keyword}\b", code, re.IGNORECASE), f"{keyword} forbidden in {path.name}"
