"""SQL Server diagnostic health is evaluated from memory dumps and sp_server_diagnostics.

Item 1.164 (audits/TEST_VERSION_BEFORE_RELEASE_DETAIL_0_30_0.md section 2.10):
Oracle's ADR (incidents, dumps, Health Monitor) is monitored by ORACLE_ADR_INCIDENTS;
SQL Server's nearest equivalents are sys.dm_server_memory_dumps and sp_server_diagnostics.

Memory dumps represent internal SQL Server incidents: an access violation, a non-yielding
scheduler, an assertion, or an unresolvable latch timeout triggers an engine minidump file
in the log directory while the instance frequently continues running as if nothing happened.
Following the pattern from 017_sqlserver_errorlog_recent_critical.sql, dumps created within
@dump_recent_minutes (default 120 minutes / 2 hours) alert as CRITICAL; older dumps within
@dump_window_days (default 7 days) alert as WARNING so they remain visible on dashboards without
paging on-call staff; dumps older than 7 days are not reported per-dump. When no dumps exist
within the window, one OK row 'memory_dumps' reports the total dump count retained in the DMV
and the newest dump timestamp.

sys.dm_server_memory_dumps was introduced in SQL Server 2008 R2 SP1; earlier builds (2008 RTM/SP,
2008 R2 RTM) do not have this DMV and would fail at compilation time. The legacy variant guards
the view via an OBJECT_ID check and dynamic SQL, reporting one LOGGING row when missing.

sp_server_diagnostics (2012+ only) is the procedure used by failover clustering and availability
groups to detect instance-level unhealthiness. Executing with @repeat_interval = 0 returns a
single diagnostic result set and terminates immediately. Component rows (system, resource,
query_processing, io_subsystem, events, and AG components) are graded: clean -> OK,
warning -> WARNING, error -> CRITICAL, unknown -> LOGGING. Diagnostic XML attributes are extracted
for key components, while the events component's large XML is skipped. Execution is wrapped in
TRY/CATCH so permission or execution failures report one LOGGING row and never drop memory dumps.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
MODERN = "sqlserver/081_sqlserver_server_diagnostics.sql"
LEGACY = "sqlserver/legacy_2008r2/081_sqlserver_server_diagnostics.sql"
VARIANTS = (MODERN, LEGACY)

NUMERIC_THRESHOLDS = {
    "dump_recent_minutes": 120,
    "dump_window_days": 7,
}


def _text(relative: str) -> str:
    return (COLLECTORS / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _code(sql: str) -> str:
    """The SQL text without comments and normalized to single whitespace."""
    lines = [
        line.split("--", 1)[0]
        for line in sql.splitlines()
        if not line.lstrip().startswith("--")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# Thresholds and configuration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_declare_dump_thresholds_on_their_own_lines(relative: str):
    """Every threshold must be a DECLARE @name type = number; on a line of its own."""
    text = _text(relative)
    for name, value in NUMERIC_THRESHOLDS.items():
        pattern = rf"^DECLARE @{name} int = {value};"
        assert re.search(pattern, text, re.MULTILINE), \
            f"{relative} must declare @{name} with default {value} on its own line"


@pytest.mark.parametrize("relative", VARIANTS)
def test_numeric_thresholds_match_apply_sql_settings_shape_and_can_be_rewritten(relative: str):
    """apply_sql_settings must successfully rewrite the declared thresholds."""
    text = _text(relative)
    overrides = {name: value + 10 for name, value in NUMERIC_THRESHOLDS.items()}
    configured = apply_sql_settings(text, overrides)
    for name, value in overrides.items():
        assert f"DECLARE @{name} int = {value};" in configured


# --------------------------------------------------------------------------- #
# Memory dumps grading rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_memory_dumps_within_dump_recent_minutes_are_graded_critical(relative: str):
    """Dumps within @dump_recent_minutes must be graded CRITICAL."""
    code = _code(_text(relative))
    critical_match = re.search(
        r"WHEN\s+age_minutes\s*<=\s*@dump_recent_minutes\s+THEN\s+''?CRITICAL''?",
        code,
        re.IGNORECASE,
    )
    assert critical_match, f"{relative} must grade dumps within @dump_recent_minutes as CRITICAL"


@pytest.mark.parametrize("relative", VARIANTS)
def test_memory_dumps_older_than_recent_but_within_window_are_graded_warning(relative: str):
    """Dumps older than @dump_recent_minutes but within @dump_window_days must be graded WARNING."""
    code = _code(_text(relative))
    warning_match = re.search(
        r"WHEN\s+age_minutes\s*<=\s*@dump_recent_minutes\s+THEN\s+''?CRITICAL''?\s+ELSE\s+''?WARNING''?",
        code,
        re.IGNORECASE,
    )
    assert warning_match, f"{relative} must grade dumps within window but older than recent as WARNING"


@pytest.mark.parametrize("relative", VARIANTS)
def test_memory_dumps_older_than_window_days_are_excluded_from_window_rows(relative: str):
    """Dumps older than @dump_window_days must be excluded from individual dump rows."""
    code = _code(_text(relative))
    window_filter = re.search(
        r"creation_time\s*>=\s*DATEADD\s*\(\s*day\s*,\s*-@dump_window_days\s*,\s*SYSDATETIMEOFFSET\(\)\s*\)",
        code,
        re.IGNORECASE,
    )
    assert window_filter, f"{relative} must filter dumps within @dump_window_days"


@pytest.mark.parametrize("relative", VARIANTS)
def test_when_no_dumps_in_window_one_ok_row_reports_dumps_total_and_newest_date(relative: str):
    """When no dumps exist in the window, one OK row 'memory_dumps' must report total and newest date."""
    code = _code(_text(relative))
    assert "memory_dumps" in code
    assert "dumps_total=" in code
    assert "newest_dump=" in code
    assert "dump_window_days=" in code
    assert "note=no memory dumps within window" in code


@pytest.mark.parametrize("relative", VARIANTS)
def test_memory_dump_rows_format_metric_item_value_unit_and_message_keys(relative: str):
    """Individual memory dump rows must format item, size in MB, unit 'MB', and message keys."""
    code = _code(_text(relative))
    assert "memory_dump :: " in code
    assert "'MB'" in code
    for key in ("file=", "created=", "age_minutes=", "size_mb="):
        assert key in code, f"{relative} must include key '{key}' in memory dump message"


# --------------------------------------------------------------------------- #
# Legacy 2008 R2 DMV guard and omitted sp_server_diagnostics
# --------------------------------------------------------------------------- #
def test_legacy_file_guards_sys_dm_server_memory_dumps_with_object_id_and_dynamic_sql():
    """sys.dm_server_memory_dumps must be guarded by OBJECT_ID and executed via dynamic SQL."""
    code = _code(_text(LEGACY))
    assert "OBJECT_ID('sys.dm_server_memory_dumps') IS NULL" in code
    assert "not readable on this build" in code
    assert "sys.dm_server_memory_dumps requires SQL Server 2008 R2 SP1+" in code
    assert "sp_executesql" in code


def test_legacy_file_omits_sp_server_diagnostics_and_explains_why():
    """The legacy 2008 R2 file must not execute sp_server_diagnostics and must explain why in comments."""
    text = _text(LEGACY)
    code = _code(text)
    assert "sp_server_diagnostics" not in code, \
        "legacy 2008 R2 variant must not execute sp_server_diagnostics"
    assert "sp_server_diagnostics was introduced in SQL Server 2012" in text, \
        "legacy 2008 R2 variant comment must explain why sp_server_diagnostics is omitted"


# --------------------------------------------------------------------------- #
# Modern SQL Server 2012+ sp_server_diagnostics
# --------------------------------------------------------------------------- #
def test_modern_file_executes_sp_server_diagnostics_with_repeat_interval_zero():
    """Modern variant must execute sp_server_diagnostics with @repeat_interval = 0 into a temp table."""
    code = _code(_text(MODERN))
    assert "CREATE TABLE #sp_diag" in code
    assert re.search(r"EXEC\s+(?:sys\.)?sp_server_diagnostics\s+@repeat_interval\s*=\s*0", code, re.IGNORECASE), \
        "Modern variant must execute sp_server_diagnostics with @repeat_interval = 0"


def test_modern_file_grades_components_clean_ok_warning_warning_error_critical_unknown_logging():
    """sp_server_diagnostics component states must be graded according to the specification."""
    code = _code(_text(MODERN))
    assert re.search(r"WHEN\s+LOWER\s*\(\s*d\.state_desc\s*\)\s*=\s*'clean'\s+THEN\s+'OK'", code, re.IGNORECASE)
    assert re.search(r"WHEN\s+LOWER\s*\(\s*d\.state_desc\s*\)\s*=\s*'warning'\s+THEN\s+'WARNING'", code, re.IGNORECASE)
    assert re.search(r"WHEN\s+LOWER\s*\(\s*d\.state_desc\s*\)\s*=\s*'error'\s+THEN\s+'CRITICAL'", code, re.IGNORECASE)
    assert re.search(r"ELSE\s+'LOGGING'", code, re.IGNORECASE)


def test_modern_file_wraps_sp_server_diagnostics_in_try_catch_preserving_dump_rows():
    """sp_server_diagnostics execution must be wrapped in TRY/CATCH reporting one LOGGING row on error."""
    code = _code(_text(MODERN))
    assert "BEGIN TRY" in code
    assert "BEGIN CATCH" in code
    assert "sp_server_diagnostics failed, memory dumps preserved" in code


def test_modern_file_extracts_diagnostic_attributes_for_system_resource_query_and_io():
    """Modern variant must extract diagnostic XML attributes for key components."""
    code = _code(_text(MODERN))
    system_attrs = (
        "spinlockBackoffs",
        "sickSpinlockType",
        "latchWarnings",
        "isAccessViolationOccurred",
        "nonYieldingTasksReported",
        "pageFaults",
    )
    for attr in system_attrs:
        assert attr in code, f"system component must extract XML attribute '{attr}'"

    resource_attrs = (
        "outOfMemoryExceptions",
        "isAnyPoolOutOfMemory",
        "processOutOfMemoryPeriod",
    )
    for attr in resource_attrs:
        assert attr in code, f"resource component must extract XML attribute '{attr}'"

    query_attrs = (
        "maxWorkers",
        "workersCreated",
        "workersIdle",
        "tasksCompletedWithinInterval",
        "pendingTasks",
        "hasUnresolvableDeadlockOccurred",
        "hasDeadlockedSchedulersOccurred",
    )
    for attr in query_attrs:
        assert attr in code, f"query_processing component must extract XML attribute '{attr}'"

    io_attrs = (
        "ioLatchTimeouts",
        "intervalLongIos",
        "totalLongIos",
    )
    for attr in io_attrs:
        assert attr in code, f"io_subsystem component must extract XML attribute '{attr}'"


def test_modern_file_skips_events_component_large_xml():
    """Modern variant must not extract events component XML payload into the message."""
    code = _code(_text(MODERN))
    # events component has no XML attribute extraction branch
    assert "WHEN d.component_name = 'events' THEN" not in code


# --------------------------------------------------------------------------- #
# Compatibility and safety constraints
# --------------------------------------------------------------------------- #
def test_legacy_variant_contains_no_features_introduced_after_2008_r2():
    """SQL Server 2008 R2 rejects CONCAT, IIF, TRY_CONVERT, TRY_CAST, FORMAT, STRING_AGG, etc."""
    code = _code(_text(LEGACY))
    forbidden = (
        "CONCAT",
        "IIF",
        "TRY_CONVERT",
        "TRY_CAST",
        "FORMAT",
        "STRING_AGG",
        "DATEFROMPARTS",
        "TIMEFROMPARTS",
        "EOMONTH",
        "CHOOSE",
        "TRY_PARSE",
    )
    for keyword in forbidden:
        assert keyword.lower() not in code.lower(), f"{keyword} is not supported in SQL Server 2008 R2"


@pytest.mark.parametrize("relative", VARIANTS)
def test_neither_variant_contains_any_destructive_ddl_or_dml_statements(relative: str):
    """Collectors are read-only and must never include destructive statements."""
    code = _code(_text(relative)).upper()
    destructive = ("DROP ", "DELETE ", "TRUNCATE ", "ALTER ", "KILL ")
    for verb in destructive:
        assert verb not in code, f"{relative} must be read-only and never include {verb}"
