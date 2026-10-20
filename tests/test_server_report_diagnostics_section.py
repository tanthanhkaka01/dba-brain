"""Per-server report diagnostics section contract and payload builder.

Operators need a single unified view for unexpected engine anomalies — error logs, ADR
incidents, memory dumps, PostgreSQL server logs, and page corruption — without needing to know
which vendor metric captures which event.

Diagnostic readings are current-state snapshots rather than cumulative rates: an incident or error
that was resolved in the newest collection must clear immediately, rather than lingering to page an
operator for yesterday's trouble.

These tests hold the payload shapes, filtering rules, newest-collection snapshot guarantees,
itemless-row pruning, and template contract without database connectivity or I/O.
"""

from __future__ import annotations

from typing import Any
from pathlib import Path

from dbabrain.reports.server_diagnostics import (
    DIAGNOSTICS_CODES,
    DIAGNOSTICS_DAYS,
    LABELS,
    build_diagnostics,
)
from dbabrain.reports.server_report import TEMPLATE_HTML, build_payload


def _make_row(
    metric_code: str,
    metric_item: str | None,
    metric_value: Any = "",
    metric_unit: str = "",
    message: str = "",
    *,
    collected_at: str = "2026-10-15T10:00:00Z",
    target_id: str = "LAB-TARGET",
    server_id: str = "LAB-TARGET",
    status: str = "OK",
) -> dict[str, Any]:
    """Build a metric row matching the exact store envelope produced by collectors."""
    return {
        "run_id": 1,
        "target_id": target_id,
        "server_id": server_id,
        "metric_code": metric_code,
        "metric_item": metric_item,
        "metric_value": metric_value,
        "metric_unit": metric_unit,
        "status": status,
        "importance": 2,
        "message": message,
        "collected_at": collected_at,
    }


def test_empty_input_returns_empty_dict():
    """A server with no diagnostic metrics collected renders no section at all."""
    assert build_diagnostics([]) == {}
    assert build_diagnostics(None) == {}

    # Unrelated metrics must not produce diagnostic sources.
    unrelated = _make_row(
        metric_code="SYSTEM_CPU_MEMORY",
        metric_item="cpu",
        metric_value="45",
        status="OK",
    )
    assert build_diagnostics([unrelated]) == {}


def test_newest_collection_only_cleared_critical_is_not_shown():
    """An incident reported in an older collection must not linger once a newer collection clears it."""
    older_critical = _make_row(
        metric_code="LOG_RECENT_CRITICAL",
        metric_item="Error 823",
        status="CRITICAL",
        message="I/O error reading page 1:123",
        collected_at="2026-10-15T09:00:00Z",
    )
    newer_clean = _make_row(
        metric_code="LOG_RECENT_CRITICAL",
        metric_item="",
        status="OK",
        message="No critical error log entries in the window.",
        collected_at="2026-10-15T10:00:00Z",
    )

    payload = build_diagnostics([older_critical, newer_clean])

    assert payload["worst"] == "OK"
    assert payload["findingCount"] == 0
    assert len(payload["sources"]) == 1

    source = payload["sources"][0]
    assert source["code"] == "LOG_RECENT_CRITICAL"
    assert source["label"] == "Error log"
    assert source["status"] == "OK"
    assert source["collectedAt"] == "2026-10-15T10:00:00Z"
    # The cleared CRITICAL from 09:00 is not retained
    assert source["findings"] == []
    assert source["rowsTotal"] == 1


def test_itemless_rows_dropped_when_findings_present():
    """Collectors emitting generic summary rows alongside itemized findings must have the summary pruned."""
    collection_time = "2026-10-15T10:00:00Z"
    itemless_summary = _make_row(
        metric_code="DATABASE_SUSPECT_PAGES",
        metric_item=None,
        status="OK",
        message="0 suspect pages",
        collected_at=collection_time,
    )
    actual_finding = _make_row(
        metric_code="DATABASE_SUSPECT_PAGES",
        metric_item="production_db:file1:page44",
        status="CRITICAL",
        message="Page marked suspect due to bad checksum (event 824)",
        collected_at=collection_time,
    )

    payload = build_diagnostics([itemless_summary, actual_finding])

    assert len(payload["sources"]) == 1
    source = payload["sources"][0]
    assert source["status"] == "CRITICAL"
    assert source["rowsTotal"] == 1
    assert len(source["findings"]) == 1
    assert source["findings"][0]["item"] == "production_db:file1:page44"
    assert source["findings"][0]["status"] == "CRITICAL"
    assert payload["findingCount"] == 1


def test_clean_source_preserves_itemless_row_and_reports_ok():
    """A clean source reporting an itemless row confirms that no findings were present."""
    clean_row = _make_row(
        metric_code="DATABASE_SUSPECT_PAGES",
        metric_item=None,
        status="OK",
        message="No suspect pages found on any database.",
        collected_at="2026-10-15T10:00:00Z",
    )

    payload = build_diagnostics([clean_row])

    assert payload["worst"] == "OK"
    assert payload["findingCount"] == 0
    assert len(payload["sources"]) == 1

    source = payload["sources"][0]
    assert source["code"] == "DATABASE_SUSPECT_PAGES"
    assert source["status"] == "OK"
    assert source["rowsTotal"] == 1
    assert source["findings"] == []


def test_worst_status_severity_ordering():
    """Overall status and source status strictly follow CRITICAL > WARNING > OK/LOGGING."""
    rows = [
        _make_row(
            metric_code="LOG_RECENT_CRITICAL",
            metric_item="Error 17806",
            status="WARNING",
            message="SSPI handshake failed",
        ),
        _make_row(
            metric_code="SERVER_DIAGNOSTICS",
            metric_item="core_dump_20261015",
            status="CRITICAL",
            message="Mini-dump recorded on instance",
        ),
        _make_row(
            metric_code="DATABASE_SUSPECT_PAGES",
            metric_item=None,
            status="OK",
            message="No corruption detected",
        ),
    ]

    payload = build_diagnostics(rows)
    assert payload["worst"] == "CRITICAL"
    assert payload["findingCount"] == 2

    # If the CRITICAL dump is removed, the overall verdict drops to WARNING
    without_dump = [r for r in rows if r["metric_code"] != "SERVER_DIAGNOSTICS"]
    payload_warn = build_diagnostics(without_dump)
    assert payload_warn["worst"] == "WARNING"
    assert payload_warn["findingCount"] == 1

    # If only the clean check remains, status drops to OK
    only_clean = [r for r in rows if r["metric_code"] == "DATABASE_SUSPECT_PAGES"]
    payload_clean = build_diagnostics(only_clean)
    assert payload_clean["worst"] == "OK"
    assert payload_clean["findingCount"] == 0


def test_findings_worst_first_and_capped_at_20():
    """Findings must sort with CRITICAL first, then WARNING, and cap at twenty entries."""
    rows = []
    # 10 CRITICAL items
    for i in range(10):
        rows.append(_make_row(
            metric_code="SERVER_DIAGNOSTICS",
            metric_item=f"dump_{i:02d}",
            status="CRITICAL",
            message=f"Crash dump {i}",
        ))
    # 10 WARNING items
    for i in range(10):
        rows.append(_make_row(
            metric_code="SERVER_DIAGNOSTICS",
            metric_item=f"warn_component_{i:02d}",
            status="WARNING",
            message=f"sp_server_diagnostics warning {i}",
        ))
    # 5 LOGGING items (lowest rank)
    for i in range(5):
        rows.append(_make_row(
            metric_code="SERVER_DIAGNOSTICS",
            metric_item=f"log_component_{i:02d}",
            status="LOGGING",
            message=f"Normal logging row {i}",
        ))

    payload = build_diagnostics(rows)
    source = payload["sources"][0]

    assert source["rowsTotal"] == 25
    assert len(source["findings"]) == 20
    # True total of actionable findings (CRITICAL + WARNING)
    assert payload["findingCount"] == 20

    statuses = [f["status"] for f in source["findings"]]
    # All 10 CRITICAL items lead
    assert statuses[:10] == ["CRITICAL"] * 10
    # Next 10 are WARNING items
    assert statuses[10:20] == ["WARNING"] * 10
    # The 5 LOGGING rows were capped out because they are lower priority


def test_logging_detail_rows_kept():
    """LOGGING lines are kept in findings to preserve context (e.g. PostgreSQL log messages)."""
    rows = [
        _make_row(
            metric_code="POSTGRES_SERVER_LOG",
            metric_item="FATAL",
            status="WARNING",
            message="1 FATAL error in last 60 minutes",
        ),
        _make_row(
            metric_code="POSTGRES_SERVER_LOG",
            metric_item="FATAL_line_1",
            status="LOGGING",
            message="FATAL: password authentication failed for user 'app'",
        ),
    ]

    payload = build_diagnostics(rows)
    source = payload["sources"][0]

    assert source["status"] == "WARNING"
    assert source["rowsTotal"] == 2
    assert len(source["findings"]) == 2
    assert source["findings"][0]["status"] == "WARNING"
    assert source["findings"][1]["status"] == "LOGGING"
    # LOGGING rows carry detail lines but do not inflate the finding count
    assert payload["findingCount"] == 1


def test_sources_ordered_by_diagnostics_codes_and_omits_missing():
    """Sources must follow DIAGNOSTICS_CODES sequence and exclude uncollected codes."""
    rows = [
        _make_row(metric_code="DATABASE_SUSPECT_PAGES", metric_item=None, status="OK"),
        _make_row(metric_code="LOG_RECENT_CRITICAL", metric_item=None, status="OK"),
    ]

    payload = build_diagnostics(rows)
    codes = [s["code"] for s in payload["sources"]]

    # LOG_RECENT_CRITICAL precedes DATABASE_SUSPECT_PAGES in DIAGNOSTICS_CODES
    assert codes == ["LOG_RECENT_CRITICAL", "DATABASE_SUSPECT_PAGES"]
    assert len(payload["sources"]) == 2


def test_build_payload_carries_diagnostics_key():
    """The server report payload exposes the diagnostics block under 'diagnostics'."""
    row = _make_row(
        metric_code="LOG_RECENT_CRITICAL",
        metric_item="Error 823",
        status="CRITICAL",
        message="I/O error",
    )

    payload_with_diag = build_payload([], [], now=1760000000, diagnostics_rows=[row])
    assert "diagnostics" in payload_with_diag
    assert payload_with_diag["diagnostics"]["worst"] == "CRITICAL"
    assert payload_with_diag["diagnostics"]["findingCount"] == 1

    payload_empty = build_payload([], [], now=1760000000)
    assert "diagnostics" in payload_empty
    assert payload_empty["diagnostics"] == {}


def test_template_renders_diagnostics_block_after_latency_and_escapes_message():
    """The shared HTML template must invoke diagnosticsBlock right after latencyBlock with escaped output."""
    template_content = TEMPLATE_HTML.read_text(encoding="utf-8")

    assert "function diagnosticsBlock(d)" in template_content

    # Execution order in select(slug): latency -> diagnostics -> jobs
    idx_latency = template_content.index("+ latencyBlock(d.latency)")
    idx_diag = template_content.index("+ diagnosticsBlock(d.diagnostics)")
    idx_jobs = template_content.index("+ jobsBlock(d.jobs)")

    assert idx_latency < idx_diag < idx_jobs

    # Ensure message escaping is implemented in the block
    assert "esc(f.message" in template_content
    assert "Diagnostics — clean" in template_content   # the em dash every section heading uses
