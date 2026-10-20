"""Availability Group send and redo queues are graded in AVAILABILITY_DATABASE_HEALTH.

A replica whose `synchronization_health_desc` reports HEALTHY by its own flag can still be
gigabytes behind the primary. On failover, a secondary replica that has not received the log loses
all uncommitted log records on the primary (potential data loss, RPO), while a bloated redo queue
is exactly what a failover waits for: the engine must finish redoing the transaction log before the
database becomes queryable and operational (RTO).

In 011_sqlserver_ag_database_health.sql (SQL Server 2012+ only; 2008 R2 has no AG):
- Thresholds follow Redgate SQL Monitor's default "Availability group - replication falling behind"
  alert: Medium (WARNING) when the combined log send queue and redo queue size reaches 100 MB
  (@queue_warning_kb = 102400 KB). Redgate requires 120 seconds of sustained backlog; because a
  collector execution is a single instantaneous reading without cross-run state, the metric's
  configured repeat interval is what repeats the observation.
- A replica row whose health is HEALTHY but whose queues reach @queue_warning_kb is WARNING (not
  CRITICAL: CRITICAL remains reserved for NOT_HEALTHY / broken replicas).
- AG metric_item incorporates the replica server name ('<database> :: <replica_server_name>') from
  sys.availability_replicas joined on replica_id: on a primary replica, sys.dm_hadr_database_replica_states
  exposes rows for all replicas (local and remote), which otherwise collided under the bare database name.
- Message fields carry replica role, locality (is_local), queue sizes, send/redo rates, estimated
  failover redo seconds (Microsoft's redo_queue_size / NULLIF(redo_rate, 0)), estimated data loss
  seconds (DATEDIFF in seconds between secondary and primary last_commit_time for secondary replicas
  read on the primary), and commit timestamps. NULLs print as 'n/a'.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "sqlserver"
COLLECTOR_FILE = "011_sqlserver_ag_database_health.sql"


def _text() -> str:
    return (COLLECTORS / COLLECTOR_FILE).read_text(encoding="utf-8").replace("\r\n", "\n")


def _code(sql: str) -> str:
    """SQL with comment lines removed and whitespace collapsed to single spaces."""
    lines = [
        line.split("--", 1)[0]
        for line in sql.splitlines()
        if not line.lstrip().startswith("--")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# Threshold declaration and sql_settings rewriting
# --------------------------------------------------------------------------- #
def test_queue_warning_threshold_is_declared_on_its_own_line():
    """Redgate SQL Monitor's default 100 MB queue threshold must be declared as a 102400 KB bigint."""
    text = _text()
    assert re.search(r"^DECLARE @queue_warning_kb bigint = 102400;", text, re.MULTILINE), \
        "011_sqlserver_ag_database_health.sql must declare @queue_warning_kb bigint = 102400; on its own line"


def test_queue_warning_threshold_can_be_rewritten_by_apply_sql_settings():
    """The apply_sql_settings helper must successfully rewrite @queue_warning_kb without syntax failure."""
    text = _text()
    rewritten = apply_sql_settings(text, {"queue_warning_kb": 204800})
    assert "DECLARE @queue_warning_kb bigint = 204800;" in rewritten


# --------------------------------------------------------------------------- #
# Replica qualification in metric_item
# --------------------------------------------------------------------------- #
def test_metric_item_joins_replica_server_name_to_avoid_item_collisions_on_primary():
    """A primary replica monitors all replicas, so metric_item must be '<database> :: <replica_server_name>'."""
    code = _code(_text())
    assert "sys.availability_replicas" in code, "Must join sys.availability_replicas"
    assert "drs.replica_id = ar.replica_id" in code or "drs.replica_id=ar.replica_id" in code.replace(" ", "")
    assert "COALESCE(ar.replica_server_name, 'unknown')" in code
    assert "' :: '" in code


# --------------------------------------------------------------------------- #
# Grading rules
# --------------------------------------------------------------------------- #
def test_healthy_replica_with_queues_above_threshold_is_graded_warning():
    """A HEALTHY replica whose send + redo queue reaches 100 MB escalates to WARNING."""
    code = _code(_text())
    assert "drs.synchronization_health_desc = 'HEALTHY'" in code
    assert "@queue_warning_kb" in code
    assert "THEN 'WARNING'" in code
    assert re.search(
        r"WHEN\s+drs\.synchronization_health_desc\s*=\s*'HEALTHY'\s+AND\s+"
        r"\(\s*ISNULL\s*\(\s*drs\.log_send_queue_size\s*,\s*0\s*\)\s*\+\s*"
        r"ISNULL\s*\(\s*drs\.redo_queue_size\s*,\s*0\s*\)\s*\)\s*>=\s*@queue_warning_kb\s+THEN\s+'WARNING'",
        code,
    )


def test_queue_warning_is_warning_not_critical_and_not_healthy_remains_critical():
    """Queue backlog escalates HEALTHY to WARNING only; CRITICAL is reserved for NOT_HEALTHY."""
    code = _code(_text())
    # The branch matching @queue_warning_kb must specify WARNING, never CRITICAL
    match = re.search(r"@queue_warning_kb\s+THEN\s+'([A-Z]+)'", code)
    assert match, "Queue comparison branch must produce a status"
    assert match.group(1) == "WARNING", "Queue breach on a healthy replica must produce WARNING, not CRITICAL"

    # NOT_HEALTHY / else branch remains CRITICAL
    assert "WHEN drs.synchronization_health_desc = 'PARTIALLY_HEALTHY' THEN 'WARNING'" in code
    assert "ELSE 'CRITICAL'" in code


def test_healthy_replica_within_queue_threshold_is_graded_ok():
    """A HEALTHY replica whose queues are under 100 MB remains OK."""
    code = _code(_text())
    assert "WHEN drs.synchronization_health_desc = 'HEALTHY' THEN 'OK'" in code


# --------------------------------------------------------------------------- #
# Message keys and lag estimates
# --------------------------------------------------------------------------- #
def test_message_reports_locality_and_replica_role():
    """AG row message must report is_local and role from availability replica states."""
    code = _code(_text())
    assert "', is_local=', drs.is_local" in code
    assert "', role=', COALESCE(ars.role_desc, 'UNKNOWN')" in code
    assert "sys.dm_hadr_availability_replica_states" in code


def test_message_reports_queue_sizes_and_transfer_rates():
    """Send and redo queue sizes and rates must be reported in KB and KB/s."""
    code = _code(_text())
    for key in (
        "log_send_queue_kb=",
        "redo_queue_kb=",
        "log_send_rate_kb_s=",
        "redo_rate_kb_s=",
    ):
        assert key in code, f"Message must include {key}"


def test_estimated_redo_seconds_calculates_failover_redo_duration():
    """Microsoft's failover redo estimate divides redo_queue_size by redo_rate."""
    code = _code(_text())
    assert "est_redo_seconds=" in code
    assert "drs.redo_queue_size / NULLIF(drs.redo_rate, 0)" in code


def test_estimated_data_loss_seconds_calculates_datediff_from_primary_to_secondary_commits():
    """Microsoft's data loss estimate takes DATEDIFF(second, secondary, primary last_commit_time)."""
    code = _code(_text())
    assert "est_data_loss_seconds=" in code
    assert "DATEDIFF(second, drs.last_commit_time, pri.last_commit_time)" in code
    assert "ars.role_desc = 'SECONDARY'" in code


def test_missing_estimates_and_commit_times_are_reported_as_na():
    """Uncomputable redo estimates, data loss estimates, and commit timestamps format as 'n/a'."""
    code = _code(_text())
    assert re.search(r"est_redo_seconds=.*?COALESCE\(.*?'n/a'\)", code)
    assert re.search(r"est_data_loss_seconds=.*?COALESCE\(.*?'n/a'\)", code)
    assert re.search(r"last_commit_time=.*?COALESCE\(.*?'n/a'\)", code)


# --------------------------------------------------------------------------- #
# Non-destructive operation
# --------------------------------------------------------------------------- #
def test_collector_contains_no_destructive_statements():
    """Collector must be strictly read-only and contain no DDL, DML, or KILL operations."""
    code = _code(_text()).upper()
    for disallowed in ("DROP ", "DELETE ", "TRUNCATE ", "KILL ", "ALTER "):
        assert disallowed not in code, f"Collector must not contain destructive statement: {disallowed}"
