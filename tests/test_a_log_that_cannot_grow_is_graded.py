"""A transaction log that cannot grow is graded by its used space; one that can grow is not.

Today LOG_FILE_SPACE emits only LOGGING or OK, whatever the percent used. That is a deliberate
operator decision (2026-10-15: "the log can still grow, which is why it was lowered to LOGGING;
disk full is the CRITICAL"): for a log that can grow, expansion spills onto the drive, and drive
exhaustion is paged by STORAGE_DISK_FREE_SPACE rather than here.

A log that CANNOT grow - autogrowth off, or capped at max_size - fills and halts the database with
error 9002 while the underlying volume still has plenty of headroom. STORAGE_DISK_FREE_SPACE cannot
see this failure, so nothing pages unless this metric grades it.

Per database, both variants evaluate log_can_grow across all log files in sys.master_files (type = 1):
a log can grow if ANY of its files has autogrowth enabled (growth > 0) AND the next growth step fits
within max_size (-1 or 268435456 8 KB pages being unlimited).

If the log can grow, it keeps today's grading (LOGGING / OK). If it cannot grow, it pages CRITICAL at
@log_cannot_grow_critical_pct (default 95) and WARNING at @log_cannot_grow_warning_pct (default 85).
Both thresholds are declared in the apply_sql_settings shape, tempdb is included (database_id = 2),
and the message reports log_can_grow, log_max_size_mb, log_growth, and log_reuse_wait.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
MODERN = "sqlserver/007_sqlserver_log_space.sql"
LEGACY = "sqlserver/legacy_2008r2/007_sqlserver_log_space.sql"
VARIANTS = (MODERN, LEGACY)

THRESHOLDS = {
    "log_cannot_grow_critical_pct": 95,
    "log_cannot_grow_warning_pct": 85,
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
def test_both_variants_declare_the_two_thresholds_with_the_agreed_defaults(relative):
    text = _text(relative)
    for name, value in THRESHOLDS.items():
        assert re.search(rf"^DECLARE @{name} int = {value};", text, re.MULTILINE), name


@pytest.mark.parametrize("relative", VARIANTS)
def test_each_threshold_is_in_the_shape_a_configured_number_is_written_into(relative):
    """A node may state any of them in sql_settings; the file must take it, once."""
    configured = apply_sql_settings(_text(relative), {name: value + 1
                                                      for name, value in THRESHOLDS.items()})
    for name, value in THRESHOLDS.items():
        assert f"DECLARE @{name} int = {value + 1};" in configured


def test_no_catalogue_states_them_before_the_code_that_declares_them_has_shipped():
    """The worker takes the master's data files at any time, and a node whose SQL does not declare
    a stated name refuses the setting - the metric would fail on every SQL Server there. The
    defaults are the operator's numbers, so nothing needs stating; when a released version declares
    both, this test is what to delete."""
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
# Grading: only a log that cannot grow reaches WARNING or CRITICAL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_the_warning_and_critical_branches_require_the_cannot_grow_condition(relative):
    """CRITICAL and WARNING require log_can_grow = 0, guarding against paging for a growing log."""
    code = _code(_text(relative))
    critical_matches = re.findall(r"WHEN\s+(.*?)\s+THEN\s+''?CRITICAL''?", code, re.IGNORECASE)
    warning_matches = re.findall(r"WHEN\s+(.*?)\s+THEN\s+''?WARNING''?", code, re.IGNORECASE)

    assert critical_matches, f"expected CRITICAL branch in status CASE for {relative}"
    assert warning_matches, f"expected WARNING branch in status CASE for {relative}"

    for branch in critical_matches:
        assert "log_can_grow = 0" in branch or "log_can_grow=0" in branch.replace(" ", "")
        assert "@log_cannot_grow_critical_pct" in branch

    for branch in warning_matches:
        assert "log_can_grow = 0" in branch or "log_can_grow=0" in branch.replace(" ", "")
        assert "@log_cannot_grow_warning_pct" in branch


@pytest.mark.parametrize("relative", VARIANTS)
def test_a_log_that_can_grow_is_never_paged(relative):
    """A log with log_can_grow = 1 skips CRITICAL/WARNING and retains today's LOGGING/OK grading."""
    code = _code(_text(relative))
    logging_matches = re.findall(r"WHEN\s+(.*?)\s+THEN\s+''?LOGGING''?", code, re.IGNORECASE)
    assert len(logging_matches) >= 3, f"today's LOGGING rules must be preserved in {relative}"
    assert "ELSE ''OK''" in code or "ELSE 'OK'" in code


# --------------------------------------------------------------------------- #
# Scope and message contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_tempdb_is_in_scope_alongside_user_databases(relative):
    """tempdb's log (database_id = 2) is checked in addition to user databases (database_id > 4)."""
    code = _code(_text(relative))
    assert re.search(r"database_id\s*>\s*4\s+OR\s+.*?database_id\s*=\s*2", code, re.IGNORECASE) or \
           re.search(r"database_id\s*=\s*2\s+OR\s+.*?database_id\s*>\s*4", code, re.IGNORECASE)


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_message_names_log_growth_capacity_and_reuse_wait(relative):
    """The message contract requires log_can_grow, log_max_size_mb, log_growth, and log_reuse_wait."""
    code = _code(_text(relative))
    for field in ("log_can_grow", "log_max_size_mb", "log_growth", "log_reuse_wait"):
        assert f"{field}=" in code, f"message in {relative} must include {field}="


# --------------------------------------------------------------------------- #
# Version boundaries: 2008 R2 must not use modern syntax
# --------------------------------------------------------------------------- #
def test_the_2008_r2_variant_uses_none_of_the_features_introduced_after_2008_r2():
    """SQL Server 2008 R2 lacks CONCAT, IIF, TRY_CONVERT, TRY_CAST, and sys.dm_db_log_space_usage."""
    code = _code(_text(LEGACY))
    forbidden = ["CONCAT", "IIF", "TRY_CONVERT", "TRY_CAST", "sys.dm_db_log_space_usage"]
    for keyword in forbidden:
        assert keyword.lower() not in code.lower(), f"{keyword} is not supported in 2008 R2"


# --------------------------------------------------------------------------- #
# A log whose files cannot be seen is never read as one that cannot grow
# --------------------------------------------------------------------------- #
def test_the_2012_variant_reads_the_database_s_own_files_which_any_user_of_it_can_see():
    """sys.master_files shows a row only to a login holding VIEW ANY DEFINITION, CREATE DATABASE or
    ALTER ANY DATABASE. Read by a monitor login without them it shows no log file, "no file can
    grow" would follow, and every log at 95 % would page. The 2012+ file runs inside each database,
    where sys.database_files needs nothing more than using it."""
    code = _code(_text(MODERN))

    assert "sys.database_files" in code
    assert "sys.master_files" not in code


def test_the_2008_r2_variant_grades_a_log_it_cannot_see_as_one_that_can_grow():
    code = _code(_text(LEGACY))

    assert re.search(r"WHEN NOT EXISTS \( SELECT 1 FROM sys\.master_files mv WHERE mv\.database_id = "
                     r"d\.database_id AND mv\.type = 1 \) THEN NULL", code)
    # NULL never equals 0, so an unseen log keeps the grading of a log that can grow.
    assert "cg.log_can_grow = 0 AND" in code
    assert "ISNULL(CAST(cg.log_can_grow AS varchar(1)), 'unknown')" in code
