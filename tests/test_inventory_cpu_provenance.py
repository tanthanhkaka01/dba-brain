"""The vCPU column went stale after a hardware upgrade, and the memory column beside it did not.

A host was rebuilt from 24 vCPU / 64 GB to 32 / 128. The report's host card picked the new
hardware up within the hour — `os_health` comes from a metric. The SQL configuration table went on
saying **24 vCPU**, because `sql_visible_cpu_count` was classed as a "field with no metric" and
came from `sqlserver_resources`, a hand-authored block in `database-inventory.json`. Two numbers a
few rows apart, one live and one frozen, with nothing on the page to say which was which.

The instance knows both counts — `sys.dm_os_sys_info.cpu_count` and the online scheduler count —
and is connected to on every cycle, so this was never a fact the tool could not collect.

The other half of the fix is about honesty rather than freshness: where the counts still cannot be
collected, the stored number is shown *with the time the instance was last asked*. That is what
separates "nobody has collected from this box in a week" from "we asked it a minute ago and it
still could not tell us" — and only the second is a reason to go and edit the inventory file.
"""

from __future__ import annotations

from db_ops.reports.inventory_health import build_sql_governance
from db_ops.reports.inventory_report import _fmt_cpu


def _cpu_row(message: str, collected_at: str = "2026-09-07T00:29:25Z") -> dict:
    return {"metric_code": "SYSTEM_CPU_MEMORY", "metric_item": "cpu", "metric_value": "6.00",
            "message": message, "collected_at": collected_at, "status": "OK"}


def _code_map(rows: list[dict]) -> dict:
    """Keyed by (metric_code, item), the shape inventory_health reads."""
    return {(row["metric_code"], row["metric_item"]): row for row in rows}


LIVE = ("system_cpu_used_pct=6.00, sql_process_cpu_pct=5.00, system_idle_pct=94.00, "
        "sql_visible_cpu_count=32, scheduler_count=32")
OLD_SHAPE = "system_cpu_used_pct=6.00, sql_process_cpu_pct=5.00, system_idle_pct=94.00"


def test_the_counts_are_taken_from_the_metric_when_it_carries_them():
    gov = build_sql_governance(_code_map([_cpu_row(LIVE)]))

    assert gov["sql_cpu"]["sql_visible_cpu_count"] == 32
    assert gov["sql_cpu"]["scheduler_count"] == 32
    assert gov["sql_cpu"]["cpu_count_source"] == "metric"


def test_a_collected_count_is_rendered_as_a_plain_number():
    """Nothing to caveat: it is what the instance said, this cycle."""
    gov = build_sql_governance(_code_map([_cpu_row(LIVE)]))

    assert _fmt_cpu({"cpu": gov["sql_cpu"]["sql_visible_cpu_count"],
                     "cpuLive": True,
                     "cpuSeenAt": gov["sql_cpu"]["cpu_seen_at"]}) == "32"


def test_a_metric_without_the_counts_leaves_the_stored_ones_alone():
    """An older collector, or an engine whose SQL does not report them. The stored number stays;
    what must not happen is a blank or a zero replacing it."""
    gov = build_sql_governance(_code_map([_cpu_row(OLD_SHAPE)]))

    assert "sql_visible_cpu_count" not in gov["sql_cpu"]
    assert "cpu_count_source" not in gov["sql_cpu"]


def test_the_time_the_instance_was_last_asked_is_recorded_either_way():
    """Recorded even when the counts did not come through — that is the case it exists for."""
    with_counts = build_sql_governance(_code_map([_cpu_row(LIVE)]))
    without = build_sql_governance(_code_map([_cpu_row(OLD_SHAPE)]))

    assert with_counts["sql_cpu"]["cpu_seen_at"] == "2026-09-07T00:29:25Z"
    assert without["sql_cpu"]["cpu_seen_at"] == "2026-09-07T00:29:25Z"


def test_a_stored_count_is_marked_and_dated():
    """The rendering that answers "is this number from the box or from a file?" without asking
    the reader to know which columns have metrics behind them."""
    text = _fmt_cpu({"cpu": 24, "cpuLive": False, "cpuSeenAt": "2026-09-07T00:29:25Z"})

    assert text == "24 ⚠ stored, asked 2026-09-07T00:29:25Z"


def test_a_stored_count_with_no_observation_at_all_still_says_it_is_stored():
    """A server the metrics have never reached. The mark is the part that must not be dropped."""
    assert _fmt_cpu({"cpu": 24, "cpuLive": False, "cpuSeenAt": ""}) == "24 ⚠ stored"


def test_no_count_anywhere_renders_as_a_dash_not_a_zero():
    assert _fmt_cpu({"cpu": None, "cpuLive": False, "cpuSeenAt": ""}) == "—"


# --------------------------------------------------------------------------------------------- #
# The collector has to actually emit them
# --------------------------------------------------------------------------------------------- #
def test_the_shipped_collector_reads_both_counts_from_the_instance():
    """A parser for fields nothing emits is the defect this fix was for, one layer down."""
    from db_ops.lib.paths import PACKAGE_DIR

    sql = (PACKAGE_DIR / "metrics" / "collectors" / "sqlserver"
           / "012_sqlserver_cpu_memory.sql").read_text(encoding="utf-8")

    assert "dm_os_sys_info" in sql and "dm_os_schedulers" in sql
    assert "sql_visible_cpu_count=" in sql, "the message must carry it in key=value form"
    assert "scheduler_count=" in sql


# --------------------------------------------------------------------------------------------- #
# The per-server page lost its snapshot stamp
# --------------------------------------------------------------------------------------------- #
def test_the_server_page_keeps_its_snapshot_stamp_after_a_server_is_picked():
    """The stamp was the initial text of the element the host line is written into, so choosing a
    server erased it — and a page with no date on it is one nobody can tell is stale. The
    inventory page beside it kept showing one, which is how the difference was noticed."""
    from db_ops.reports.server_report import TEMPLATE_HTML

    html = TEMPLATE_HTML.read_text(encoding="utf-8")
    after = html.split('document.getElementById("hostMeta").textContent', 1)[1][:400]

    assert "SNAPSHOT_DATE" in after, "the host line is rebuilt without the stamp"
    assert 'const SNAPSHOT_DATE = "__SNAPSHOT_DATE__"' in html
