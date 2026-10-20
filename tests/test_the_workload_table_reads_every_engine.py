"""The Workload table answers "how much did this instance do" on Oracle and PostgreSQL too (1.149).

PERFORMANCE_WORKLOAD_COUNTERS was SQL Server's alone; an Oracle or PostgreSQL page said "not
collected" where SQL Server's showed an hour of transactions, reads and log. Both engines keep the
totals - Oracle in v$sysstat, PostgreSQL in pg_stat_database - so their collectors write them under
the table's own keys where the meaning is the same (a transaction is a commit or a rollback, a
logical read is a block asked of the cache, log generated is redo or WAL bytes) and under keys of
their own where it is not (Oracle's user calls, PostgreSQL's rows read and written, its temp files).

Two things are easy to get wrong and are held here: a PostgreSQL read latency without
track_io_timing is a flat 0 that the page would print as measured, and a standby generates no WAL.
"""

from __future__ import annotations

import re
from pathlib import Path

from dbabrain.metrics.definitions import load_metric_definitions
from dbabrain.reports import workload

from conftest import shipped_config

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
STAMPS = [f"2026-10-16T{8 + minute // 60:02d}:{minute % 60:02d}:00+00:00" for minute in (0, 15, 30, 45, 60)]


def _rows(values_per_step: dict, source: str, **fields) -> list[dict]:
    carried = ", ".join(f"{key}={value}" for key, value in fields.items())
    return [
        {"metric_code": workload.COUNTER_CODE, "metric_item": item, "metric_value": str(value * step),
         "collected_at": stamp, "message": f"value={value * step}, {carried}, source={source}"}
        for step, stamp in enumerate(STAMPS) for item, value in values_per_step.items()
    ]


def _keys(block: dict) -> dict[str, dict]:
    return {row["key"]: row for group in block["groups"] for row in group["rows"]}


def _code(relative: str) -> str:
    text = (COLLECTORS / relative).read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", " ".join(line.split("--", 1)[0] for line in text.splitlines()))


def test_an_oracle_hour_has_its_transactions_reads_and_redo():
    rows = _rows({"transactions": 1_000, "user_calls": 5_000, "executions": 20_000,
                  "page_lookups": 2_000_000, "page_reads": 10_000, "log_bytes_flushed": 50 * 1024 * 1024},
                 "v$sysstat", counters_since="2026-10-02 09:38:03", cpu_count=4, schedulers_online=2)

    by_key = _keys(workload.build_workload(rows))

    assert by_key["transactions"]["values"]["hour"]["total"] == 4_000
    assert by_key["user_calls"]["values"]["hour"]["total"] == 20_000
    assert by_key["executions"]["values"]["hour"]["total"] == 80_000
    assert by_key["log_mb_flushed"]["values"]["hour"]["total"] == 200.0


def test_a_postgresql_hour_has_rows_and_temp_and_is_known_for_postgresql():
    rows = _rows({"transactions": 300, "rows_read": 90_000, "rows_written": 1_200,
                  "temp_files": 2, "temp_bytes": 8 * 1024 * 1024},
                 "pg_stat_database", counters_since="2026-10-15 08:50:36")

    block = workload.build_workload(rows)
    by_key = _keys(block)

    assert block["cpu"]["engine"] == "PostgreSQL"
    assert by_key["rows_written"]["values"]["hour"]["total"] == 4_800
    assert by_key["temp_files"]["values"]["hour"]["total"] == 8
    assert by_key["temp_mb_written"]["values"]["hour"]["total"] == 32.0


def test_postgresql_read_latency_is_written_only_when_postgresql_times_its_reads():
    code = _code("postgresql/075_postgresql_workload_counters.sql")
    assert "current_setting('track_io_timing') = 'on' AS io_timing" in code
    assert "SELECT 'io_stall_read_ms', d.io_stall_read_ms, 'ms', 'pg_stat_database' FROM d, meta m WHERE m.io_timing" in code


def test_a_standby_writes_no_log_generated_row():
    code = _code("postgresql/075_postgresql_workload_counters.sql")
    assert "CASE WHEN m.standby THEN NULL ELSE pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint END" in code
    assert "WHERE c.metric_value IS NOT NULL" in code


def test_a_column_newer_than_the_version_is_read_without_breaking_the_query():
    """pg_stat_database.sessions is 14+: named directly it fails the whole collection on 10 - 13."""
    code = _code("postgresql/075_postgresql_workload_counters.sql")
    assert "(to_jsonb(s) ->> 'sessions')::bigint" in code


def test_a_reset_of_the_statistics_moves_the_baseline():
    code = _code("postgresql/075_postgresql_workload_counters.sql")
    assert "GREATEST(pg_postmaster_start_time(), d.stats_reset)" in code


def test_oracle_maps_two_statistics_onto_one_transaction_row():
    for relative in ("oracle/081_oracle_workload_counters.sql", "oracle/legacy_8i/081_oracle_workload_counters.sql"):
        code = _code(relative)
        assert "'user commits'" in code and "'user rollbacks'" in code
        assert "SUM(" in code and re.search(r"GROUP BY (m\.)?metric_item", code)


def test_the_catalogue_runs_the_counters_on_every_engine_it_can():
    metric = next(item for item in load_metric_definitions(shipped_config("metric_definitions.json"))
                  if item.metric_code == workload.COUNTER_CODE)
    files = {variant.db_type: variant.path.name for variant in metric.variants if variant.supported and variant.path}
    assert files["postgresql"] == "075_postgresql_workload_counters.sql"
    assert files["oracle"] == "081_oracle_workload_counters.sql"
    assert files["sqlserver"] == "072_sqlserver_workload_counters.sql"
