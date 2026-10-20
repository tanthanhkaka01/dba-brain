"""Memory-grant starvation is graded across two in-run samples.

When queries requiring workspace memory (for sorts, hash joins, or bulk operations) consume the
workspace memory pool, subsequent queries queue behind resource semaphores. Stateless collectors
cannot store state between runs, so both samples are taken inside one execution separated by a
5-second WAITFOR DELAY:
- Read 'Memory Grants Pending' from sys.dm_os_performance_counters and query waiters from
  sys.dm_exec_query_memory_grants (grant_time IS NULL).
- Wait 5 seconds (WAITFOR DELAY).
- Read again, recording the oldest wait and top resource semaphore.

Approved grading:
- pending > 5 in the second sample = CRITICAL (severe workspace memory exhaustion).
- pending > 0 in both samples = WARNING (sustained query queueing across the interval).
- otherwise OK.

sys.dm_os_wait_stats is cumulative since boot and is deliberately not graded. Active waiting tasks
in sys.dm_os_waiting_tasks with wait_type RESOURCE_SEMAPHORE are reported in the message.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
MODERN = "sqlserver/079_sqlserver_memory_grants_pending.sql"
LEGACY = "sqlserver/legacy_2008r2/079_sqlserver_memory_grants_pending.sql"
VARIANTS = (MODERN, LEGACY)

THRESHOLDS = {
    "pending_grants_critical": 5,
    "pending_grants_warning": 0,
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
# Two samples with WAITFOR DELAY
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_take_two_samples_separated_by_waitfor_delay(relative):
    """The metric must take two samples with a WAITFOR DELAY between them."""
    code = _code(_text(relative))
    assert "WAITFOR DELAY" in code
    assert "Memory Grants Pending" in code


# --------------------------------------------------------------------------- #
# Approved grading rule: CRITICAL before WARNING, second sample > 5, both > 0
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_grading_order_and_threshold_logic(relative):
    """CRITICAL must evaluate pending > 5 (second sample) before WARNING (both samples > 0)."""
    code = _code(_text(relative))
    crit_match = re.search(r"WHEN\s+(@\w+)\s*>\s*@pending_grants_critical\s+THEN\s+'CRITICAL'", code)
    assert crit_match, f"CRITICAL branch missing in {relative}"

    warn_match = re.search(r"WHEN\s+(@\w+)\s*>\s*@pending_grants_warning\s+AND\s+(@\w+)\s*>\s*@pending_grants_warning\s+THEN\s+'WARNING'", code)
    assert warn_match, f"WARNING branch missing in {relative}"

    # Verify CRITICAL comes before WARNING in text
    crit_pos = code.index("THEN 'CRITICAL'")
    warn_pos = code.index("THEN 'WARNING'")
    assert crit_pos < warn_pos, f"CRITICAL must precede WARNING in {relative}"


# --------------------------------------------------------------------------- #
# dm_exec_query_memory_grants and RESOURCE_SEMAPHORE waits
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_waiters_evaluated_with_grant_time_is_null(relative):
    """Waiting queries must be counted from sys.dm_exec_query_memory_grants with grant_time IS NULL."""
    code = _code(_text(relative))
    assert "sys.dm_exec_query_memory_grants" in code
    assert re.search(r"grant_time\s+IS\s+NULL", code, re.IGNORECASE)


@pytest.mark.parametrize("relative", VARIANTS)
def test_waiting_tasks_resource_semaphore_reported_not_wait_stats(relative):
    """Active RESOURCE_SEMAPHORE tasks from sys.dm_os_waiting_tasks go in the message;
    sys.dm_os_wait_stats is cumulative and must not be graded."""
    code = _code(_text(relative))
    assert "sys.dm_os_waiting_tasks" in code
    assert "RESOURCE_SEMAPHORE" in code
    assert "sys.dm_os_wait_stats" not in code


# --------------------------------------------------------------------------- #
# Message contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_message_contract_names_required_fields(relative):
    """The message contract requires pending counts, delay, waiters, oldest wait, and semaphores."""
    code = _code(_text(relative))
    for field in ("pending_grants=", "sample1_pending=", "sample2_pending=", "oldest_wait_ms=",
                  "top_resource_semaphore=", "resource_semaphore_tasks="):
        assert field in code, f"message in {relative} must include {field}"


# --------------------------------------------------------------------------- #
# Version syntax boundaries
# --------------------------------------------------------------------------- #
def test_the_2008_r2_variant_uses_no_post_2008_r2_syntax():
    """SQL Server 2008 R2 lacks CONCAT, IIF, TRY_CONVERT, and TRY_CAST."""
    code = _code(_text(LEGACY))
    for keyword in ("CONCAT(", "IIF(", "TRY_CONVERT(", "TRY_CAST("):
        assert keyword.lower() not in code.lower(), f"{keyword} is not supported in 2008 R2"
