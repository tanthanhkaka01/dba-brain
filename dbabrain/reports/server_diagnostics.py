"""Pure builder for the Diagnostics section of server-metrics.html.

Each database engine records its operational incidents and anomalies in a different place:
SQL Server writes to the error log, sys.dm_server_memory_dumps and sp_server_diagnostics;
Oracle maintains ADR incidents, Health Monitor findings and the alert log; PostgreSQL records
to its container or host server log; and corruption surfaces through suspect pages or checksum
failures. A DBA scanning one server's dashboard should not have to know which collector holds
which engine's incident log.

Only the newest collection of each diagnostic metric is shown. Unlike performance trends,
diagnostics represent current operational states: an incident or error that was resolved and
cleared in the subsequent collection must not linger on the dashboard and page an operator for
yesterday's trouble.
"""

from __future__ import annotations

from typing import Any

DIAGNOSTICS_CODES: tuple[str, ...] = (
    "LOG_RECENT_CRITICAL",
    "ORACLE_ADR_INCIDENTS",
    "SERVER_DIAGNOSTICS",
    "POSTGRES_SERVER_LOG",
    "DATABASE_SUSPECT_PAGES",
)

#: Fetch window capped at one day regardless of page view: diagnostics are current states,
#: and an older run's finding that has cleared must not be brought forward.
DIAGNOSTICS_DAYS = 1

LABELS: dict[str, str] = {
    "LOG_RECENT_CRITICAL": "Error log",
    "ORACLE_ADR_INCIDENTS": "ADR incidents and Health Monitor",
    "SERVER_DIAGNOSTICS": "Memory dumps and sp_server_diagnostics",
    "POSTGRES_SERVER_LOG": "Server log",
    "DATABASE_SUSPECT_PAGES": "Corruption",
}

DIAGNOSTICS_LABELS = LABELS


def _status_rank(status: str) -> int:
    """Status hierarchy: CRITICAL > WARNING > OK/LOGGING."""
    s = str(status or "").strip().upper()
    if s in ("CRITICAL", "ERROR"):
        return 3
    if s in ("WARNING", "WARN", "UNKNOWN", "NO_DATA"):
        return 2
    if s == "LOGGING":
        return 1
    return 0


def _worst_status(statuses: list[str]) -> str:
    """Worst status across a collection or set of sources."""
    ranks = [_status_rank(s) for s in statuses]
    max_rank = max(ranks) if ranks else 0
    if max_rank >= 3:
        return "CRITICAL"
    if max_rank == 2:
        return "WARNING"
    return "OK"


def build_diagnostics(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Extract the newest collection findings across the five diagnostic metrics.

    Evaluates current-state error log entries, incidents, dumps, logs and corruption checks.
    Drop itemless "no rows" rows unless they are the sole row representing a clean source.
    """
    if not rows:
        return {}

    by_code: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        code = str(r.get("metric_code") or r.get("code") or "")
        if code in DIAGNOSTICS_CODES:
            by_code.setdefault(code, []).append(r)

    if not by_code:
        return {}

    sources: list[dict[str, Any]] = []
    total_finding_count = 0

    for code in DIAGNOSTICS_CODES:
        code_rows = by_code.get(code)
        if not code_rows:
            continue

        # Keep only the newest collection for this code: past incidents that cleared
        # must never linger as current findings.
        newest_stamp = max(str(r.get("collected_at") or "") for r in code_rows)
        newest_rows = [
            r for r in code_rows
            if str(r.get("collected_at") or "") == newest_stamp
        ]
        if not newest_rows:
            continue

        # Drop itemless "no rows" rows unless it is the only row (which signals a clean source).
        with_items = [
            r for r in newest_rows
            if str(r.get("metric_item") or r.get("item") or "").strip()
        ]
        if with_items:
            source_rows = with_items
        else:
            source_rows = newest_rows

        source_status = _worst_status([r.get("status", "") for r in source_rows])

        # Non-OK rows are candidate findings; LOGGING detail rows are kept because they
        # carry evidence lines (e.g. POSTGRES_SERVER_LOG per-class messages).
        candidates = [
            r for r in source_rows
            if str(r.get("status") or "").strip().upper() not in ("OK", "HEALTHY")
        ]

        # Worst findings lead so the most severe anomalies appear at the top of the table.
        candidates.sort(
            key=lambda r: (-_status_rank(r.get("status", "")), str(r.get("metric_item") or r.get("item") or ""))
        )

        capped_findings = [
            {
                "item": str(r.get("metric_item") or r.get("item") or "").strip(),
                "status": str(r.get("status") or "").strip(),
                "value": r.get("metric_value") if r.get("metric_value") is not None else r.get("value"),
                "unit": str(r.get("metric_unit") or r.get("unit") or ""),
                "message": str(r.get("message") or ""),
            }
            for r in candidates[:20]
        ]

        # findingCount counts genuine issues (non-OK, non-LOGGING findings).
        for r in source_rows:
            st = str(r.get("status") or "").strip().upper()
            if st not in ("OK", "HEALTHY", "LOGGING"):
                total_finding_count += 1

        sources.append({
            "code": code,
            "label": LABELS.get(code, code),
            "status": source_status,
            "collectedAt": newest_stamp,
            "findings": capped_findings,
            "rowsTotal": len(source_rows),
        })

    if not sources:
        return {}

    overall_worst = _worst_status([s["status"] for s in sources])

    return {
        "sources": sources,
        "worst": overall_worst,
        "findingCount": total_finding_count,
    }
