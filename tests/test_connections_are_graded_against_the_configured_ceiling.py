"""Instance connections are graded against the configured user connections ceiling.

INSTANCE_CONNECTIONS previously evaluated only blocked sessions, raising WARNING when blocking
was detected, but never compared active connections against the engine or administrative ceiling.
If an administrator sets 'user connections' (sp_configure) to protect an instance, exceeding that
ceiling rejects incoming logins with error 17809.

Both variants query sys.configurations for 'user connections':
- When value_in_use is 0 (the default), the engine relies on its internal maximum of 32,767. There
  is no configured ceiling, so the ratio is not graded and emits LOGGING ("not configured is a
  state, not a failure").
- When value_in_use > 0, utilization is graded: >= 90 % is CRITICAL and >= 80 % is WARNING.

The collector preserves today's reporting for active user sessions, running requests, and blocking
while adding the connection limit ratio row.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
MODERN = "sqlserver/003_sqlserver_connections.sql"
LEGACY = "sqlserver/legacy_2008r2/003_sqlserver_connections.sql"
VARIANTS = (MODERN, LEGACY)

THRESHOLDS = {
    "connection_limit_critical_pct": 90,
    "connection_limit_warning_pct": 80,
}


def _text(relative: str) -> str:
    return (COLLECTORS / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _code(sql: str) -> str:
    """The SQL without comments, whitespace collapsed."""
    lines = [line.split("--", 1)[0] for line in sql.splitlines()
             if not line.lstrip().startswith("--")]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# Thresholds and configuration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_declare_thresholds_with_agreed_defaults(relative):
    """Thresholds must be declared as DECLARE @name int = <number>; on lines of their own."""
    text = _text(relative)
    for name, value in THRESHOLDS.items():
        assert re.search(rf"^DECLARE @{name} int = {value};", text, re.MULTILINE), name


@pytest.mark.parametrize("relative", VARIANTS)
def test_each_threshold_takes_configured_settings(relative):
    """apply_sql_settings must successfully rewrite each declared threshold."""
    configured = apply_sql_settings(_text(relative), {name: value + 1
                                                      for name, value in THRESHOLDS.items()})
    for name, value in THRESHOLDS.items():
        assert f"DECLARE @{name} int = {value + 1};" in configured


def test_no_catalogue_states_them_before_shipped():
    """No metric_definitions catalogue should declare these settings until released."""
    files = [COLLECTORS.parent / "catalogue" / "metric_definitions.json",
             REPO_ROOT / "data" / "metric_definitions.json",
             REPO_ROOT / "data" / "metric_definitions.example.json"]
    for path in files:
        if not path.exists():
            continue
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        metrics = next(value for value in document.values()
                       if isinstance(value, list) and value and isinstance(value[0], dict))
        for metric in metrics:
            stated = metric.get("sql_settings") or {}
            assert not set(stated) & set(THRESHOLDS), (path.name, metric.get("metric_code"))


# --------------------------------------------------------------------------- #
# Ceiling check from sys.configurations and ratio grading
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_sys_configurations_is_queried_for_user_connections(relative):
    """The collector must inspect value_in_use of 'user connections' from sys.configurations."""
    code = _code(_text(relative))
    assert "sys.configurations" in code
    assert "'user connections'" in code
    assert "value_in_use" in code


@pytest.mark.parametrize("relative", VARIANTS)
def test_an_unconfigured_ceiling_reports_logging_for_the_ratio(relative):
    """When configured limit is 0 (engine default), the ratio must not be graded and reports LOGGING."""
    code = _code(_text(relative))
    assert re.search(r"WHEN\s+.*?(?:configured_limit\s*=\s*0|value_in_use\s*=\s*0)\s+THEN\s+'LOGGING'", code)


@pytest.mark.parametrize("relative", VARIANTS)
def test_a_configured_ceiling_is_graded_at_thresholds(relative):
    """When a limit is configured, >= 90 % is CRITICAL and >= 80 % is WARNING."""
    code = _code(_text(relative))
    assert re.search(r"@connection_limit_critical_pct\s+THEN\s+'CRITICAL'", code)
    assert re.search(r"@connection_limit_warning_pct\s+THEN\s+'WARNING'", code)


@pytest.mark.parametrize("relative", VARIANTS)
def test_today_active_sessions_and_blocking_checks_are_preserved(relative):
    """Active user process sessions, running requests, and blocking sessions are still reported."""
    code = _code(_text(relative))
    assert "active_sessions" in code
    assert "running_requests" in code
    assert "blocked_sessions" in code
    assert "blocking_session_id" in code


# --------------------------------------------------------------------------- #
# Version syntax boundaries
# --------------------------------------------------------------------------- #
def test_the_2008_r2_variant_uses_no_post_2008_r2_syntax():
    """SQL Server 2008 R2 lacks CONCAT, IIF, TRY_CONVERT, and TRY_CAST."""
    code = _code(_text(LEGACY))
    for keyword in ("CONCAT(", "IIF(", "TRY_CONVERT(", "TRY_CAST("):
        assert keyword.lower() not in code.lower(), f"{keyword} is not supported in 2008 R2"


@pytest.mark.parametrize("relative", [MODERN, LEGACY])
def test_the_ceiling_is_graded_on_the_ratio_row_alone(relative):
    """Graded on the `server` row as well, one breach of the ceiling paged twice: that row keeps
    grading blocking only, as it did before the ceiling was read."""
    code = _code(_text(relative))
    server_row = code[:code.index("UNION ALL")]

    assert "@connection_limit_critical_pct" not in server_row.split("DECLARE @connection_limit_warning_pct")[-1]
    assert "@connection_limit_warning_pct THEN 'WARNING'" not in server_row
