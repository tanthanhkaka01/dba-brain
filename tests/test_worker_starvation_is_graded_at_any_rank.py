"""Worker starvation is graded at any rank, with scheduler work queues.

Previously, PERFORMANCE_WAIT_STATS graded THREADPOOL as CRITICAL only when it was the single
top wait of the 5-second in-run sample (WHERE d.place = 1). When another non-benign wait accumulated
more time during the sample (for instance CXPACKET or PAGEIOLATCH under parallel query storms),
an active THREADPOOL starvation sat at rank 2 or 3 and was completely ignored by monitoring.
Worker starvation means sessions are queueing for worker threads, and incoming connections are
about to be refused; it must never be hidden behind a harmless parallelism wait.

Both variants (modern and SQL Server 2008 R2) now separate worker starvation from the top-wait row:
1. Readings around WAITFOR capture active THREADPOOL waiters (sys.dm_os_waiting_tasks), scheduler
   work queues (sys.dm_os_schedulers), max workers (sys.dm_os_sys_info), and active workers at both
   the start and end of the sample.
2. A dedicated metric_item 'threadpool' row is ALWAYS emitted:
   - CRITICAL when sustained: waiters at both reads, work queue > 0 at both reads, or THREADPOOL
     wait time in the sample >= @threadpool_sustained_pct (default 100.0) % of the measured sample.
   - WARNING when any THREADPOOL wait occurred in the sample (delta wait_ms > 0 or delta tasks > 0),
     or waiters/queue > 0 at either read.
   - OK when clear.
3. The top-wait row no longer forces CRITICAL for THREADPOOL; when THREADPOOL is top wait, it
   follows ordinary wait gates and notes graded_on=threadpool so one condition produces one alert.
4. The existing idle row ('wait_stats') is preserved, and the threadpool row emits alongside it.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
MODERN = "sqlserver/014_sqlserver_wait_stats.sql"
LEGACY = "sqlserver/legacy_2008r2/014_sqlserver_wait_stats.sql"
VARIANTS = (MODERN, LEGACY)

THRESHOLDS = {
    "threadpool_sustained_pct": 100.0,
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
def test_both_variants_declare_the_threadpool_sustained_threshold_with_agreed_default(relative):
    text = _text(relative)
    assert re.search(r"^DECLARE @threadpool_sustained_pct decimal\(10,2\) = 100\.0;", text, re.MULTILINE)


@pytest.mark.parametrize("relative", VARIANTS)
def test_each_threshold_is_in_the_shape_a_configured_number_is_written_into(relative):
    """A node may state any of them in sql_settings; the file must take it, once."""
    configured = apply_sql_settings(_text(relative), {"threadpool_sustained_pct": 80.0})
    assert "DECLARE @threadpool_sustained_pct decimal(10,2) = 80;" in configured


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
# DMV readings around WAITFOR
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_sample_threadpool_waiters_and_scheduler_work_queues_around_waitfor(relative):
    """Waiters, work queue, active and max workers are read at both sides of the sample delay."""
    code = _code(_text(relative))
    assert "sys.dm_os_waiting_tasks" in code
    assert "sys.dm_os_schedulers" in code
    assert "sys.dm_os_sys_info" in code

    assert "@waiting_first" in code
    assert "@waiting_second" in code
    assert "@work_queue_first" in code
    assert "@work_queue_second" in code
    assert "@active_workers" in code
    assert "@max_workers" in code


# --------------------------------------------------------------------------- #
# Dedicated threadpool row contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_a_dedicated_threadpool_row_is_always_emitted(relative):
    """The threadpool row is emitted unconditionally via UNION ALL so resolved issues clear."""
    code = _code(_text(relative))
    assert re.search(r"UNION\s+ALL\s+SELECT\s+CAST\('threadpool'", code, re.IGNORECASE)


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_threadpool_row_grades_sustained_starvation_as_critical(relative):
    """Sustained starvation (waiters at both reads, queue at both reads, or >= threshold %) is CRITICAL."""
    code = _code(_text(relative))
    threadpool_select = code.split("CAST('threadpool'")[1]
    critical_matches = re.findall(r"WHEN\s+(.*?)\s+THEN\s+''?CRITICAL''?", threadpool_select, re.IGNORECASE)
    assert critical_matches, f"expected CRITICAL branch in threadpool row of {relative}"

    branch = critical_matches[0]
    assert "@waiting_first > 0 AND @waiting_second > 0" in branch or \
           "@waiting_first>0 AND @waiting_second>0" in branch.replace(" ", "")
    assert "@work_queue_first > 0 AND @work_queue_second > 0" in branch or \
           "@work_queue_first>0 AND @work_queue_second>0" in branch.replace(" ", "")
    assert "@threadpool_sustained_pct" in branch


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_threadpool_row_grades_transient_waits_or_any_queue_as_warning(relative):
    """Any THREADPOOL wait in the sample or positive waiters/queue at either read is WARNING."""
    code = _code(_text(relative))
    threadpool_select = code.split("CAST('threadpool'")[1]
    warning_matches = re.findall(r"WHEN\s+(.*?)\s+THEN\s+''?WARNING''?", threadpool_select, re.IGNORECASE)
    assert warning_matches, f"expected WARNING branch in threadpool row of {relative}"

    branch = warning_matches[0]
    assert "@tp_wait_ms > 0" in branch
    assert "@tp_tasks > 0" in branch
    assert "@waiting_first > 0" in branch
    assert "@waiting_second > 0" in branch
    assert "@work_queue_first > 0" in branch
    assert "@work_queue_second > 0" in branch


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_threadpool_message_contains_all_required_keys_in_order(relative):
    """Message must report wait ms, delta tasks, waiters, queue, worker counts and sample ms."""
    code = _code(_text(relative))
    assert re.search(
        r"threadpool_wait_ms=.*?threadpool_tasks=.*?waiting_first=.*?waiting_second=.*?"
        r"work_queue_first=.*?work_queue_second=.*?active_workers=.*?max_workers=.*?sample_ms=",
        code,
    ), f"threadpool message keys in {relative} must maintain the required contract"


# --------------------------------------------------------------------------- #
# Top-wait row no longer forces CRITICAL for THREADPOOL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_the_top_wait_row_no_longer_grades_threadpool_as_critical(relative):
    """Top wait delegates starvation grading to the threadpool row and follows normal gates."""
    code = _code(_text(relative))
    top_wait_select = code.split("WHERE d.place = 1")[0]
    assert "WHEN d.wait_type = 'THREADPOOL' THEN 'CRITICAL'" not in top_wait_select
    assert "graded_on=threadpool" in top_wait_select


@pytest.mark.parametrize("relative", VARIANTS)
def test_the_idle_row_is_preserved_alongside_the_threadpool_row(relative):
    """When no non-benign wait occurs, the wait_stats idle row is still emitted."""
    code = _code(_text(relative))
    assert "CAST('wait_stats' AS varchar(256))" in code
    assert "NOT EXISTS (SELECT 1 FROM @delta)" in code


# --------------------------------------------------------------------------- #
# Version boundaries: 2008 R2 must not use modern syntax
# --------------------------------------------------------------------------- #
def test_the_2008_r2_variant_uses_none_of_the_features_introduced_after_2008_r2():
    """SQL Server 2008 R2 lacks CONCAT, IIF, TRY_CONVERT, TRY_CAST, FORMAT, and STRING_AGG."""
    code = _code(_text(LEGACY))
    forbidden = ["CONCAT", "IIF", "TRY_CONVERT", "TRY_CAST", "FORMAT", "STRING_AGG", "datetime2", "SYSUTCDATETIME"]
    for keyword in forbidden:
        assert keyword.lower() not in code.lower(), f"{keyword} is not supported in 2008 R2"
