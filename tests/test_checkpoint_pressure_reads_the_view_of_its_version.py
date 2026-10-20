"""Checkpoint pressure reads the view of its PostgreSQL major version and records cumulative counters.

Item 1.145 introduces POSTGRES_CHECKPOINT_PRESSURE to track the share of requested checkpoints
among all checkpoints. Checkpoints triggered by WAL exhaustion (max_wal_size) cause I/O bursts and
stalls compared to smoothly scheduled checkpoints (checkpoint_timeout).

PostgreSQL 17 reorganized checkpoint metrics:
- PostgreSQL 17+: pg_stat_checkpointer (num_timed, num_requested, stats_reset)
- PostgreSQL 10-16: pg_stat_bgwriter (checkpoints_timed, checkpoints_req, stats_reset)

Two variants by major version are authored under dbabrain/metrics/collectors/postgresql/ without
runtime version branches inside the SQL:
- 073_postgresql_checkpoint_pressure_17_plus.sql
- 074_postgresql_checkpoint_pressure_pre_17.sql

House rule (docs/04_metrics_engine.md - "Cumulative counters are not current performance"):
Both views record cumulative counters since stats_reset. As established by the metrics engine contract,
collectors record cumulative counters and do not grade them. Dividing since-boot or since-reset
cumulative totals produces a smoothed average that dilutes active incidents and preserves ancient spikes.
Both collectors emit status LOGGING and declare report_policy.collect_only: true with counters_since,
leaving differential rate calculation and grading against approved thresholds (>= 40% WARNING,
>= 70% CRITICAL) to downstream report generation in dbabrain/lib/interval_rates.py.

These offline tests read the collector SQL text directly and hold the contracts without a database.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "postgresql"

MODERN_17_PLUS = COLLECTORS / "073_postgresql_checkpoint_pressure_17_plus.sql"
LEGACY_PRE_17 = COLLECTORS / "074_postgresql_checkpoint_pressure_pre_17.sql"
VARIANTS = (MODERN_17_PLUS, LEGACY_PRE_17)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _code(sql: str) -> str:
    """The SQL without comment lines and with collapsed whitespace: the contract, not layout."""
    lines = [
        line.split("--", 1)[0]
        for line in sql.splitlines()
        if not line.lstrip().startswith("--")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# Version-specific view and column contracts
# --------------------------------------------------------------------------- #

def test_modern_variant_reads_pg_stat_checkpointer_and_its_columns():
    """PostgreSQL 17+ reads pg_stat_checkpointer with num_timed, num_requested, and stats_reset."""
    code = _code(_text(MODERN_17_PLUS))
    assert "pg_stat_checkpointer" in code
    assert "num_timed" in code
    assert "num_requested" in code
    assert "stats_reset" in code
    assert "pg_stat_bgwriter" not in code


def test_pre_17_variant_reads_pg_stat_bgwriter_and_its_columns():
    """PostgreSQL 10-16 reads pg_stat_bgwriter with checkpoints_timed, checkpoints_req, and stats_reset."""
    code = _code(_text(LEGACY_PRE_17))
    assert "pg_stat_bgwriter" in code
    assert "checkpoints_timed" in code
    assert "checkpoints_req" in code
    assert "stats_reset" in code
    assert "pg_stat_checkpointer" not in code


# --------------------------------------------------------------------------- #
# Metric contract, status LOGGING, and house rule adherence
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", VARIANTS)
def test_both_variants_record_cumulative_counters_with_status_logging(path: Path):
    """Cumulative counters since stats_reset are recorded with status LOGGING under the house rule."""
    code = _code(_text(path))
    assert "'LOGGING'" in code
    # Status must not be hardcoded to WARNING or CRITICAL
    assert "'WARNING' AS status" not in code
    assert "'CRITICAL' AS status" not in code


@pytest.mark.parametrize("path", VARIANTS)
def test_both_variants_declare_approved_thresholds_in_cte(path: Path):
    """Approved thresholds (40% WARNING, 70% CRITICAL) are declared once in the thresholds CTE."""
    code = _code(_text(path))
    assert re.search(r"WITH\s+thresholds\s+AS\s*\(", code, re.IGNORECASE)
    assert "70" in code
    assert "40" in code


@pytest.mark.parametrize("path", VARIANTS)
def test_both_variants_use_checkpoint_pressure_item_and_percent_unit(path: Path):
    """The metric item is checkpoint_pressure and unit is percent."""
    code = _code(_text(path))
    assert "'checkpoint_pressure'" in code
    assert "'percent'" in code


@pytest.mark.parametrize("path", VARIANTS)
def test_both_variants_carry_counters_since_and_raw_counts_in_message(path: Path):
    """Message contract carries timed, requested, total, requested_pct, and counters_since."""
    code = _code(_text(path))
    for key in ("checkpoints_timed=", "checkpoints_req=", "total_checkpoints=", "req_checkpoint_pct=", "counters_since="):
        assert key in code, f"message in {path.name} must include {key}"


@pytest.mark.parametrize("path", VARIANTS)
def test_both_variants_guard_against_division_by_zero(path: Path):
    """When a cluster has zero checkpoints recorded, division by zero is safely guarded."""
    code = _code(_text(path))
    assert "> 0" in code or "NULLIF" in code


# --------------------------------------------------------------------------- #
# Destructive statement safety
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", VARIANTS)
def test_neither_collector_contains_destructive_statements(path: Path):
    """Collectors are strictly read-only and must never perform DDL, DML, or session alteration."""
    code = _code(_text(path))
    forbidden = ["DROP", "DELETE", "TRUNCATE", "KILL", "ALTER", "CREATE TABLE", "INSERT INTO"]
    for keyword in forbidden:
        assert not re.search(rf"\b{keyword}\b", code, re.IGNORECASE), f"{keyword} forbidden in {path.name}"
