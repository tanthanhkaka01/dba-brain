"""Database mirroring and log shipping are graded in AVAILABILITY_DATABASE_HEALTH.

Always On Availability Groups require SQL Server 2012 or newer. On SQL Server 2008 R2 and earlier,
and in pre-AG or hybrid environments, high availability and disaster recovery depend entirely on
database mirroring and log shipping. An AG-only check left those databases unmonitored: an unhealthy
mirror or an out-of-sync secondary that stopped restoring log backups paged nobody.

In 011_sqlserver_ag_database_health.sql (2012+) and legacy_2008r2/011_sqlserver_availability_health.sql
(2008 R2):
- Database mirroring (sys.database_mirroring) is graded per mirrored database: SYNCHRONIZED is OK,
  SYNCHRONIZING is WARNING, and SUSPENDED, DISCONNECTED, PENDING_FAILOVER, or UNSYNCHRONIZED are
  CRITICAL.
- Log shipping is read from msdb's monitor tables (log_shipping_monitor_primary and
  log_shipping_monitor_secondary) and graded strictly against its own configured thresholds
  (backup_threshold and restore_threshold minutes), not hardcoded intervals. Exceeding the threshold
  or having never run (NULL date) is CRITICAL (matching the engine's alert condition); otherwise OK.
- Access to msdb is guarded (HAS_PERMS_BY_NAME and TRY/CATCH into a table variable) so a monitoring
  login lacking msdb permissions does not lose visible AG and mirroring rows; when unreadable, a single
  LOGGING row 'log_shipping' is emitted.
- The NOT_CONFIGURED row is emitted as item 'availability' with status OK only when none of the
  monitored technologies is configured.
- The 2012+ variant keeps its AG rows unchanged. The 2008 R2 variant excludes AG DMVs and contains
  no 2012-only T-SQL syntax (CONCAT, IIF, TRY_CONVERT, TRY_CAST, FORMAT, STRING_AGG).
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "sqlserver"
MODERN = "011_sqlserver_ag_database_health.sql"
LEGACY = "legacy_2008r2/011_sqlserver_availability_health.sql"
VARIANTS = (MODERN, LEGACY)


def _text(relative: str) -> str:
    return (COLLECTORS / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _code(sql: str) -> str:
    """SQL with comment lines removed and whitespace collapsed to single spaces."""
    lines = [
        line.split("--", 1)[0]
        for line in sql.splitlines()
        if not line.lstrip().startswith("--")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# Mirroring state grading
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_grade_mirroring_states_correctly(relative: str):
    code = _code(_text(relative))
    assert "sys.database_mirroring" in code, f"{relative} must query sys.database_mirroring"
    assert "mirroring_guid IS NOT NULL" in code, f"{relative} must filter for configured mirrors"

    # Mirroring item and state value
    assert ":: mirroring" in code, f"{relative} must emit metric_item '<database> :: mirroring'"
    assert "mirroring_state_desc" in code, f"{relative} must project mirroring_state_desc"

    # State transitions: SYNCHRONIZED -> OK, SYNCHRONIZING -> WARNING, others -> CRITICAL
    assert re.search(r"WHEN\s+m\.mirroring_state_desc\s*=\s*'SYNCHRONIZED'\s+THEN\s+'OK'", code), \
        f"{relative} must grade SYNCHRONIZED as OK"
    assert re.search(r"WHEN\s+m\.mirroring_state_desc\s*=\s*'SYNCHRONIZING'\s+THEN\s+'WARNING'", code), \
        f"{relative} must grade SYNCHRONIZING as WARNING"
    for critical_state in ("SUSPENDED", "DISCONNECTED", "PENDING_FAILOVER", "UNSYNCHRONIZED"):
        assert critical_state in code, f"{relative} must check {critical_state} as CRITICAL"


@pytest.mark.parametrize("relative", VARIANTS)
def test_an_asynchronous_mirror_that_reads_synchronizing_is_ok(relative: str):
    """High-performance mirroring (safety OFF) stays SYNCHRONIZING for as long as it runs, by design:
    graded WARNING it would never clear. Found in review (2026-10-15); the OK branch comes first."""
    code = _code(_text(relative))
    async_ok = ("WHEN m.mirroring_state_desc = 'SYNCHRONIZING' AND m.mirroring_safety_level_desc = 'OFF' "
                "THEN 'OK'")
    assert async_ok in code
    assert code.index(async_ok) < code.index("WHEN m.mirroring_state_desc = 'SYNCHRONIZING' THEN 'WARNING'")


# --------------------------------------------------------------------------- #
# Log shipping grading against configured thresholds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_grade_log_shipping_against_configured_thresholds_without_literal_minutes(relative: str):
    code = _code(_text(relative))

    # Must query msdb monitor tables
    assert "msdb.dbo.log_shipping_monitor_primary" in code
    assert "msdb.dbo.log_shipping_monitor_secondary" in code

    # Metric items
    assert ":: log_shipping_primary" in code
    assert ":: log_shipping_secondary" in code

    # Primary grading: NULL date -> CRITICAL, past backup_threshold -> CRITICAL, else OK
    assert re.search(r"WHEN\s+p\.last_backup_date\s+IS\s+NULL\s+THEN\s+'CRITICAL'", code), \
        f"{relative} must grade unexecuted primary log backup as CRITICAL"
    assert re.search(r"DATEDIFF\s*\(\s*minute\s*,\s*p\.last_backup_date\s*,\s*GETDATE\(\)\s*\)\s*>\s*p\.backup_threshold\s+THEN\s+'CRITICAL'", code), \
        f"{relative} must grade primary log backup exceeding backup_threshold as CRITICAL"

    # Secondary grading: NULL date -> CRITICAL, past restore_threshold -> CRITICAL, else OK
    assert re.search(r"WHEN\s+s\.last_restored_date\s+IS\s+NULL\s+THEN\s+'CRITICAL'", code), \
        f"{relative} must grade unexecuted secondary log restore as CRITICAL"
    assert re.search(r"DATEDIFF\s*\(\s*minute\s*,\s*s\.last_restored_date\s*,\s*GETDATE\(\)\s*\)\s*>\s*s\.restore_threshold\s+THEN\s+'CRITICAL'", code), \
        f"{relative} must grade secondary log restore exceeding restore_threshold as CRITICAL"

    # No hardcoded literal minute comparisons (e.g., > 60 or > 120 minutes)
    assert not re.search(r"DATEDIFF\s*\(\s*minute\s*,[^)]+\)\s*>\s*\d+", code), \
        f"{relative} must not use hardcoded literal minute thresholds for log shipping"


# --------------------------------------------------------------------------- #
# msdb guard and least-privilege handling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_guard_msdb_read_with_permissions_check_and_try_catch(relative: str):
    code = _code(_text(relative))

    # Guarded via HAS_PERMS_BY_NAME
    assert "HAS_PERMS_BY_NAME('msdb.dbo.log_shipping_monitor_primary', 'OBJECT', 'SELECT')" in code
    assert "HAS_PERMS_BY_NAME('msdb.dbo.log_shipping_monitor_secondary', 'OBJECT', 'SELECT')" in code

    # Guarded via TRY/CATCH into table variable
    assert "BEGIN TRY" in code
    assert "BEGIN CATCH" in code
    assert "DECLARE @results TABLE" in code

    # Emits a LOGGING row for unreadable msdb log shipping
    assert re.search(r"'log_shipping'\s*,\s*'UNREADABLE'\s*,\s*NULL\s*,\s*'LOGGING'", code), \
        f"{relative} must emit a LOGGING row for 'log_shipping' when msdb is unreadable"


# --------------------------------------------------------------------------- #
# Single NOT_CONFIGURED row
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_emit_a_single_availability_not_configured_row_when_no_ha_is_present(relative: str):
    code = _code(_text(relative))

    # Item is 'availability' (not 'availability_group')
    assert "'availability'" in code
    assert "'NOT_CONFIGURED'" in code
    assert re.search(r"IF NOT EXISTS\s*\(\s*SELECT 1 FROM @results\s*\)", code), \
        f"{relative} must emit NOT_CONFIGURED only when no HA rows were populated"


def test_modern_variant_names_three_availability_technologies_in_not_configured_message():
    code = _code(_text(MODERN))
    match = re.search(r"INSERT INTO @results.*?VALUES\s*\(\s*'availability',\s*'NOT_CONFIGURED',\s*NULL,\s*'OK',\s*'(.*?)'\s*\)", code)
    assert match, f"Could not find NOT_CONFIGURED insert in {MODERN}"
    msg = match.group(1)
    assert "Availability Groups" in msg
    assert "mirroring" in msg
    assert "log shipping" in msg


def test_legacy_variant_names_two_availability_technologies_in_not_configured_message():
    code = _code(_text(LEGACY))
    match = re.search(r"INSERT INTO @results.*?VALUES\s*\(\s*'availability',\s*'NOT_CONFIGURED',\s*NULL,\s*'OK',\s*'(.*?)'\s*\)", code)
    assert match, f"Could not find NOT_CONFIGURED insert in {LEGACY}"
    msg = match.group(1)
    assert "mirroring" in msg
    assert "log shipping" in msg
    assert "Availability Groups" not in msg


# --------------------------------------------------------------------------- #
# Availability Groups preserved in 2012+ variant
# --------------------------------------------------------------------------- #
def test_modern_2012_variant_preserves_the_exact_availability_group_query():
    code = _code(_text(MODERN))
    assert "sys.dm_hadr_database_replica_states" in code
    assert "drs.synchronization_state_desc" in code
    assert "drs.synchronization_health_desc = 'HEALTHY' THEN 'OK'" in code
    assert "drs.synchronization_health_desc = 'PARTIALLY_HEALTHY' THEN 'WARNING'" in code
    assert "database=" in code
    assert "synchronization_state=" in code
    assert "synchronization_health=" in code


# --------------------------------------------------------------------------- #
# SQL Server 2008 R2 syntax compatibility
# --------------------------------------------------------------------------- #
def test_legacy_2008r2_variant_contains_no_hadr_dmvs_and_no_2012_syntax():
    code = _code(_text(LEGACY))

    # No Always On Availability Groups DMVs
    assert "dm_hadr" not in code, "2008 R2 cannot query sys.dm_hadr_* DMVs"

    # No 2012+ T-SQL built-in functions
    disallowed_functions = (
        "CONCAT(",
        "IIF(",
        "TRY_CONVERT(",
        "TRY_CAST(",
        "FORMAT(",
        "STRING_AGG(",
    )
    for func in disallowed_functions:
        assert func not in code, f"2008 R2 variant must not contain 2012+ function {func}"


# --------------------------------------------------------------------------- #
# Non-destructive operation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_neither_variant_contains_destructive_statements(relative: str):
    code = _code(_text(relative)).upper()
    disallowed = ("DROP ", "DELETE ", "TRUNCATE ", "KILL ", "ALTER ")
    for stmt in disallowed:
        assert stmt not in code, f"{relative} must not contain destructive statement: {stmt}"


# --------------------------------------------------------------------------- #
# Message keys verification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_format_messages_with_all_required_keys(relative: str):
    code = _code(_text(relative))

    # Mirroring message keys
    for key in ("database=", "role=", "state=", "safety=", "partner=", "witness_state="):
        assert key in code, f"{relative} mirroring message must include {key}"

    # Log shipping primary message keys
    for key in ("database=", "role=PRIMARY", "last_backup=", "age_minutes=", "threshold_minutes=", "threshold_alert_enabled="):
        assert key in code, f"{relative} primary log shipping message must include {key}"

    # Log shipping secondary message keys
    for key in (
        "database=", "role=SECONDARY", "last_copied=", "last_restored=", "age_minutes=",
        "threshold_minutes=", "threshold_alert_enabled=", "primary_server=",
        "primary_database=", "last_restored_latency_minutes="
    ):
        assert key in code, f"{relative} secondary log shipping message must include {key}"
