"""Severe errors page for a short window, then downgrade to warning for the rest of the day.

Today LOG_RECENT_CRITICAL counts errors across a 24-hour lookback and pages CRITICAL whenever any
critical-class error is found. Because the metric runs every hour (3600 s), a single corruption,
I/O error, or stack dump that occurred hours ago continues paging on every run for a full 24 hours,
causing severe on-call alert fatigue long after DBAs have acknowledged or mitigated the incident.

In addition, severities 19 and 20 (fatal errors in resource or current process) previously alerted
only as WARNING, despite terminating user sessions and indicating severe server-side distress.

Both variants now include severities 19 and 20 in the critical error class. To prevent 24-hour paging
fatigue, a short window (@critical_recent_minutes, default 120) determines CRITICAL status:
- CRITICAL when a critical-class error occurred within the last @critical_recent_minutes.
- WARNING when a critical-class error is older than @critical_recent_minutes but within 24 hours,
  or when any other matched error (e.g. severities 17, 18, 17886) occurred within 24 hours.
- OK when no matched errors occurred.

The message retains every existing key in order and adds critical_errors_recent and
recent_window_minutes immediately after critical_errors_24h.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
MODERN = "sqlserver/017_sqlserver_errorlog_recent_critical.sql"
LEGACY = "sqlserver/legacy_2008r2/017_sqlserver_errorlog_recent_critical.sql"
VARIANTS = (MODERN, LEGACY)

THRESHOLDS = {
    "critical_recent_minutes": 120,
}


def _text(relative: str) -> str:
    return (COLLECTORS / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _code(sql: str) -> str:
    """The SQL without its comment lines, whitespace collapsed: the contract, not its layout."""
    lines = [line.split("--", 1)[0] for line in sql.splitlines()
             if not line.lstrip().startswith("--")]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# Thresholds and configuration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_declare_the_recent_window_threshold_with_the_agreed_default(relative):
    text = _text(relative)
    for name, value in THRESHOLDS.items():
        assert re.search(rf"^DECLARE @{name} int = {value};", text, re.MULTILINE), name


@pytest.mark.parametrize("relative", VARIANTS)
def test_each_threshold_is_in_the_shape_a_configured_number_is_written_into(relative):
    """A node may state any of them in sql_settings; the file must take it, once."""
    configured = apply_sql_settings(_text(relative), {name: value + 10
                                                      for name, value in THRESHOLDS.items()})
    for name, value in THRESHOLDS.items():
        assert f"DECLARE @{name} int = {value + 10};" in configured


def test_no_catalogue_states_them_before_the_code_that_declares_them_has_shipped():
    """Defaults belong in code until released; catalogues should not state unreleased settings."""
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
# Critical class includes Severities 19 and 20
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_severities_19_and_20_join_the_critical_class(relative):
    """Severities 19 and 20 must be included in the critical class for 24h and recent counts."""
    code = _code(_text(relative))
    # The class is declared once; the classifier further down names every severity from 17
    # anyway, so finding the word in the file would prove nothing.
    declared = code[code.index("INSERT INTO @critical_types (error_type) VALUES"):]
    declared = declared[:declared.index(";")]
    for sev in ("SEVERITY_19", "SEVERITY_20", "SEVERITY_21", "SEVERITY_25", "ERROR_824_LOGICAL_IO"):
        assert f"('{sev}')" in declared
    for sev in ("SEVERITY_17", "SEVERITY_18"):
        assert f"('{sev}')" not in declared

    # Both counts read that one list, through the join.
    assert "LEFT JOIN @critical_types AS c ON c.error_type = m.error_type" in code
    assert "SUM(CASE WHEN c.error_type IS NOT NULL THEN 1 ELSE 0 END) AS critical_count_24h" in code
    assert re.search(r"WHEN c\.error_type IS NOT NULL AND m\.LogDate >= DATEADD\(minute, -@critical_recent_minutes, GETDATE\(\)\)", code)


# --------------------------------------------------------------------------- #
# Grading: short window pages CRITICAL, older critical becomes WARNING
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_critical_status_requires_a_critical_error_within_the_recent_window(relative):
    """CRITICAL is emitted only when critical errors exist within the recent window."""
    code = _code(_text(relative))
    critical_matches = re.findall(r"WHEN\s+(.*?)\s+THEN\s+''?CRITICAL''?", code, re.IGNORECASE)
    assert critical_matches, f"expected CRITICAL branch in {relative}"
    for branch in critical_matches:
        assert "critical_count_recent" in branch


@pytest.mark.parametrize("relative", VARIANTS)
def test_an_older_critical_error_inside_24h_is_graded_as_warning(relative):
    """A critical error older than the short window but within 24h downgrades to WARNING."""
    code = _code(_text(relative))
    warning_matches = re.findall(r"WHEN\s+(.*?)\s+THEN\s+''?WARNING''?", code, re.IGNORECASE)
    assert len(warning_matches) >= 2, f"expected at least two WARNING branches in {relative}"

    has_critical_24h = any("critical_count_24h" in branch for branch in warning_matches)
    has_error_24h = any("error_count_24h" in branch for branch in warning_matches)
    assert has_critical_24h, f"expected critical_count_24h in WARNING branches of {relative}"
    assert has_error_24h, f"expected error_count_24h in WARNING branches of {relative}"


# --------------------------------------------------------------------------- #
# Message contract and bounded read
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_the_message_carries_recent_critical_count_and_window_in_order(relative):
    """critical_errors_recent and recent_window_minutes follow critical_errors_24h."""
    code = _code(_text(relative))
    assert re.search(
        r"errors_24h=.*?critical_errors_24h=.*?critical_errors_recent=.*?recent_window_minutes=.*?last_error_time=",
        code,
    ), f"message keys in {relative} must maintain the required order"


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_24_hour_bounded_read_through_xp_readerrorlog_is_preserved(relative):
    """Reading xp_readerrorlog with @since prevents timing out on uncycled large logs."""
    text = _text(relative)
    assert "xp_readerrorlog" in text
    assert "@since" in text
    assert "@since_hours" in text


# --------------------------------------------------------------------------- #
# Version boundaries: 2008 R2 must not use modern syntax
# --------------------------------------------------------------------------- #
def test_the_2008_r2_variant_uses_none_of_the_features_introduced_after_2008_r2():
    """SQL Server 2008 R2 lacks CONCAT, IIF, TRY_CONVERT, TRY_CAST, FORMAT, and STRING_AGG."""
    code = _code(_text(LEGACY))
    forbidden = ["CONCAT", "IIF", "TRY_CONVERT", "TRY_CAST", "FORMAT", "STRING_AGG"]
    for keyword in forbidden:
        # As a call: the file matches log text such as "informational message", which holds FORMAT.
        assert not re.search(rf"\b{keyword}\s*\(", code, re.IGNORECASE), f"{keyword} is not supported in 2008 R2"
