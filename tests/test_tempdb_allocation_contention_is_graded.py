"""Tempdb allocation page latch contention is graded in PERFORMANCE_WAIT_STATS.

Contention on tempdb allocation tracking pages (PFS, GAM, SGAM) throttles every concurrent query
that creates or destroys temporary objects (#tables, table variables, spool worktables). The standard
remedy is adding more equally sized tempdb data files (up to the logical core count, typically 8)
and enabling trace flags 1117/1118 (or SQL Server 2016+ defaults). Note that on SQL Server 2019+,
metadata contention on system tables (addressed by Memory-Optimized TempDB Metadata) presents on
different pages and is distinct from this allocation contention row.

In both modern (014_sqlserver_wait_stats.sql) and legacy (legacy_2008r2/014_sqlserver_wait_stats.sql):
- Allocation pages are identified from sys.dm_os_waiting_tasks (wait_type LIKE 'PAGELATCH_%')
  for tempdb (db_id = 2) via resource_description 'db_id:file_id:page_id':
  * PFS: page_id = 1 or page_id % 8088 = 0
  * GAM: page_id = 2 or page_id % 511232 = 0
  * SGAM: page_id = 3 or (page_id - 1) % 511232 = 0
- Defensive parsing:
  Modern uses TRY_CAST with db_id = 2.
  Legacy uses PARSENAME(REPLACE(..., ':', '.'), n) with ISNUMERIC / LIKE '%[^0-9]%' guards to avoid
  reordering errors on 2008 R2, and contains no 2012+ T-SQL syntax.
- Contention is sampled at both the first and second reads (alongside THREADPOOL @waiting_first /
  @waiting_second). Counts per kind (pfs, gam, sgam) are preserved for the second read.
- Grading follows Netdata's SQL Server recommendation:
  `DECLARE @tempdb_pagelatch_share_pct decimal(10,2) = 5.0;`
  WARNING when allocation-page waiters persist at BOTH reads, OR when an allocation-page waiter is
  present at either read AND PAGELATCH_UP + PAGELATCH_EX wait time is >= that share of all non-benign
  wait time in @delta. Otherwise OK. Never CRITICAL (a configuration finding: add tempdb data files).
- The row is ALWAYS emitted (metric_item 'tempdb_allocation', metric_value waiters at second read,
  unit 'tasks') so that previous warnings clear automatically when the instance stabilizes.
- The message reports all diagnostic context: waiters_first, waiters_second, pfs, gam, sgam,
  pagelatch_up_ex_ms, pagelatch_share_pct, tempdb_data_files, cpu_count, sample_ms.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "sqlserver"
MODERN = "014_sqlserver_wait_stats.sql"
LEGACY = "legacy_2008r2/014_sqlserver_wait_stats.sql"
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
# Threshold declaration and sql_settings rewriting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_declare_tempdb_pagelatch_share_pct_on_its_own_line(relative: str):
    """The 5.0% PAGELATCH share threshold must be declared on its own line in both variants."""
    text = _text(relative)
    assert re.search(r"^DECLARE @tempdb_pagelatch_share_pct decimal\(10,2\) = 5\.0;", text, re.MULTILINE), \
        f"{relative} must declare @tempdb_pagelatch_share_pct decimal(10,2) = 5.0; on its own line"


@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_allow_threshold_rewriting_via_apply_sql_settings(relative: str):
    """apply_sql_settings must successfully rewrite @tempdb_pagelatch_share_pct."""
    text = _text(relative)
    rewritten = apply_sql_settings(text, {"tempdb_pagelatch_share_pct": 10.0})
    assert "DECLARE @tempdb_pagelatch_share_pct decimal(10,2) = 10" in rewritten


# --------------------------------------------------------------------------- #
# Allocation page identification arithmetic
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_identify_pfs_gam_and_sgam_allocation_pages_correctly(relative: str):
    """Allocation page arithmetic must check PFS (1, % 8088), GAM (2, % 511232), and SGAM (3, (page-1) % 511232)."""
    code = _code(_text(relative))
    # PFS: page 1 or page_id % 8088 = 0
    assert "8088 = 0" in code or "8088=0" in code.replace(" ", "")
    # GAM: page 2 or page_id % 511232 = 0
    assert "511232 = 0" in code or "511232=0" in code.replace(" ", "")
    # SGAM: page 3 or (page_id - 1) % 511232 = 0
    assert "- 1) % 511232 = 0" in code or "-1)%511232=0" in code.replace(" ", "")


# --------------------------------------------------------------------------- #
# Defensive parsing of resource_description
# --------------------------------------------------------------------------- #
def test_modern_variant_uses_try_cast_and_checks_tempdb_database_id():
    """Modern variant parses resource_description using TRY_CAST and filters on db_id = 2."""
    code = _code(_text(MODERN))
    assert "TRY_CAST(PARSENAME(REPLACE(resource_description, ':', '.'), 1) AS bigint)" in code
    assert "TRY_CAST(PARSENAME(REPLACE(resource_description, ':', '.'), 3) AS int) = 2" in code
    assert "PARSENAME(REPLACE(resource_description, ':', '.'), 4) IS NULL" in code


def test_legacy_variant_uses_defensive_guards_without_2012_functions():
    """Legacy variant parses resource_description with PARSENAME and ISNUMERIC / LIKE guards."""
    code = _code(_text(LEGACY))
    assert "PARSENAME(REPLACE(resource_description, ':', '.'), 1)" in code
    assert "PARSENAME(REPLACE(resource_description, ':', '.'), 3) = '2'" in code
    assert "ISNUMERIC(PARSENAME(REPLACE(resource_description, ':', '.'), 1)) = 1" in code
    assert "NOT LIKE '%[^0-9]%'" in code


def test_legacy_variant_contains_no_sqlserver_2012_syntax():
    """SQL Server 2008 R2 variant must not contain 2012+ T-SQL syntax."""
    code = _code(_text(LEGACY))
    for func in ("CONCAT(", "IIF(", "TRY_CONVERT(", "TRY_CAST(", "FORMAT(", "STRING_AGG("):
        assert func not in code, f"2008 R2 variant must not contain 2012+ function {func}"


# --------------------------------------------------------------------------- #
# Two reads and counts per kind
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_sample_allocation_waiters_at_both_first_and_second_reads(relative: str):
    """Allocation waiters must be counted before WAITFOR DELAY and after WAITFOR DELAY."""
    sql = _text(relative)
    first_part = sql[:sql.index("WAITFOR DELAY")]
    second_part = sql[sql.index("WAITFOR DELAY"):]

    assert "@alloc_waiters_first" in first_part
    assert "wait_type LIKE 'PAGELATCH_%'" in first_part

    assert "@alloc_waiters_second" in second_part
    assert "wait_type LIKE 'PAGELATCH_%'" in second_part


@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_preserve_counts_per_kind_for_the_second_read(relative: str):
    """Second read must break down waiters into pfs, gam, and sgam counts."""
    code = _code(_text(relative))
    assert "@alloc_pfs" in code
    assert "@alloc_gam" in code
    assert "@alloc_sgam" in code


# --------------------------------------------------------------------------- #
# Always emitted row and non-critical grading
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_always_emit_tempdb_allocation_row(relative: str):
    """tempdb_allocation row must be unconditionally emitted via UNION ALL."""
    code = _code(_text(relative))
    assert "CAST('tempdb_allocation' AS varchar(256)) AS metric_item" in code
    assert "CAST('tasks' AS varchar(32)) AS metric_unit" in code
    # Unconditional projection (no WHERE clause filtering out the SELECT)
    alloc_block = code[code.index("CAST('tempdb_allocation' AS varchar(256))"):]
    assert "WHERE " not in alloc_block


@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_grade_warning_when_waiters_persist_at_both_reads_or_exceed_share_threshold(relative: str):
    """WARNING is triggered by waiters at both reads, or waiters at either read combined with 5% PAGELATCH share."""
    code = _code(_text(relative))
    alloc_block = code[code.index("CAST('tempdb_allocation' AS varchar(256))"):]
    assert "@alloc_waiters_first > 0 AND @alloc_waiters_second > 0" in alloc_block
    assert "@pagelatch_share_pct >= @tempdb_pagelatch_share_pct" in alloc_block
    assert "THEN 'WARNING'" in alloc_block
    assert "ELSE 'OK'" in alloc_block


@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_never_emit_critical_for_tempdb_allocation(relative: str):
    """Allocation contention is a configuration finding (add tempdb files) and must never produce CRITICAL."""
    code = _code(_text(relative))
    alloc_block = code[code.index("CAST('tempdb_allocation' AS varchar(256))"):]
    status_case = alloc_block[alloc_block.index("CASE"):alloc_block.index("END AS varchar(16)) AS status")]
    assert "'CRITICAL'" not in status_case, "tempdb_allocation status CASE must never return CRITICAL"


# --------------------------------------------------------------------------- #
# Message keys verification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_include_all_required_diagnostic_keys_in_message(relative: str):
    """tempdb_allocation message must report all 10 diagnostic key-value pairs."""
    code = _code(_text(relative))
    alloc_block = code[code.index("CAST('tempdb_allocation' AS varchar(256))"):]
    expected_keys = (
        "waiters_first=",
        "waiters_second=",
        "pfs=",
        "gam=",
        "sgam=",
        "pagelatch_up_ex_ms=",
        "pagelatch_share_pct=",
        "tempdb_data_files=",
        "cpu_count=",
        "sample_ms=",
    )
    for key in expected_keys:
        assert key in alloc_block, f"{relative} tempdb_allocation message must include {key}"


# --------------------------------------------------------------------------- #
# Non-destructive operation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_contain_no_destructive_statements(relative: str):
    """Collector files must contain no destructive DDL or DML statements."""
    for relative in VARIANTS:
        code = _code(_text(relative)).upper()
        for disallowed in ("DROP ", "DELETE ", "TRUNCATE ", "KILL ", "ALTER "):
            assert disallowed not in code, f"{relative} must not contain destructive statement: {disallowed}"
