"""I/O latency is graded per interval with log writes evaluated separately from data files.

sys.dm_io_virtual_file_stats exposes cumulative counters since SQL Server service start.
An average across uptime cannot catch sudden storage degradation: during the 2026-08-10
outage on 192.0.2.115, the cumulative average read 12.84 ms while single 15-minute intervals
spiked to 269 ms and 1736 ms, staying green because historical uptime diluted the spike.

Following 014_sqlserver_wait_stats.sql, both collectors sample sys.dm_io_virtual_file_stats
twice within a single execution across a 10-second WAITFOR window. Raw reads are materialised into
table variables to prevent duplicate-key errors on SQL Server 2008 R2, negative deltas (counters reset)
are dropped, and the deltas are aggregated per database and file type (ROWS vs LOG).

Log writes are graded tightly (@log_write_warning_ms = 20 ms, @log_write_critical_ms = 50 ms)
because transaction commits block synchronously on WRITELOG. Data files are graded at 50 / 200 ms
on their read latency once the reads reached @min_ios (default 20): a query that misses the buffer
pool waits for each read. Data-file writes are not graded - the checkpoint issues them in bursts
that nobody waits for, so a write's stall measures the queue, and grading it would page on every
busy checkpoint (changed in review, 2026-10-15). Log reads are excluded too (backups / replication
readers). If no I/O occurs, a single observation row 'io_latency' is reported at status OK.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from dbabrain.lib.sql_text import apply_sql_settings

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
MODERN = "sqlserver/080_sqlserver_io_latency_interval.sql"
LEGACY = "sqlserver/legacy_2008r2/080_sqlserver_io_latency_interval.sql"
VARIANTS = (MODERN, LEGACY)

NUMERIC_THRESHOLDS = {
    "log_write_warning_ms": 20,
    "log_write_critical_ms": 50,
    "data_warning_ms": 50,
    "data_critical_ms": 200,
    "min_ios": 20,
}


def _text(relative: str) -> str:
    return (COLLECTORS / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _code(sql: str) -> str:
    """The SQL text without comments and normalized to single whitespace."""
    lines = [
        line.split("--", 1)[0]
        for line in sql.splitlines()
        if not line.lstrip().startswith("--")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# Thresholds and configuration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_declare_all_thresholds_and_sample_delay_on_their_own_lines(relative: str):
    text = _text(relative)
    assert re.search(r"^DECLARE @sample_delay char\(8\) = '00:00:10';", text, re.MULTILINE), \
        f"{relative} must declare @sample_delay char(8) = '00:00:10' on its own line"
    for name, value in NUMERIC_THRESHOLDS.items():
        if name == "min_ios":
            pattern = rf"^DECLARE @{name} int = {value};"
        else:
            pattern = rf"^DECLARE @{name} decimal\(19,2\) = {value};"
        assert re.search(pattern, text, re.MULTILINE), \
            f"{relative} must declare @{name} with default {value} on its own line"


@pytest.mark.parametrize("relative", VARIANTS)
def test_numeric_thresholds_match_apply_sql_settings_shape_and_can_be_rewritten(relative: str):
    """The apply_sql_settings helper must successfully rewrite all numeric thresholds."""
    text = _text(relative)
    overrides = {name: value + 10 for name, value in NUMERIC_THRESHOLDS.items()}
    configured = apply_sql_settings(text, overrides)
    for name, value in overrides.items():
        assert f"DECLARE @{name}" in configured
        assert f"= {value};" in configured


# --------------------------------------------------------------------------- #
# Two reads inside one run with measured elapsed ms
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_take_two_samples_around_waitfor_delay(relative: str):
    code = _code(_text(relative))
    # Must query sys.dm_io_virtual_file_stats at least twice
    assert code.count("sys.dm_io_virtual_file_stats") >= 2, \
        f"{relative} must sample sys.dm_io_virtual_file_stats twice"
    assert "WAITFOR DELAY @sample_delay" in code, \
        f"{relative} must wait between samples using WAITFOR DELAY @sample_delay"


@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_measure_elapsed_milliseconds_between_samples(relative: str):
    code = _code(_text(relative))
    assert "DATEDIFF(millisecond," in code.replace(" ", "") or "DATEDIFF(millisecond, @started" in code, \
        f"{relative} must measure elapsed ms using DATEDIFF(millisecond, ...)"


# --------------------------------------------------------------------------- #
# Table variables and delta handling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_materialise_raw_dmv_reads_into_table_variables_before_aggregating(relative: str):
    code = _code(_text(relative))
    assert "@raw_before TABLE" in code, f"{relative} must declare @raw_before table variable"
    assert "@raw_after TABLE" in code, f"{relative} must declare @raw_after table variable"
    assert "@deltas TABLE" in code, f"{relative} must declare @deltas table variable"


@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_drop_negative_deltas_caused_by_counter_resets(relative: str):
    code = _code(_text(relative))
    # Negative deltas must be filtered out when joining the two samples
    assert "num_of_reads >= b.num_of_reads" in code or "num_of_reads>=b.num_of_reads" in code.replace(" ", "")
    assert "num_of_writes >= b.num_of_writes" in code or "num_of_writes>=b.num_of_writes" in code.replace(" ", "")
    assert "io_stall_read_ms >= b.io_stall_read_ms" in code or "io_stall_read_ms>=b.io_stall_read_ms" in code.replace(" ", "")
    assert "io_stall_write_ms >= b.io_stall_write_ms" in code or "io_stall_write_ms>=b.io_stall_write_ms" in code.replace(" ", "")


# --------------------------------------------------------------------------- #
# Grading rules: log writes vs data files, min_ios gate, and idle row
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_log_files_are_graded_on_write_latency_only_ignoring_reads(relative: str):
    """Log reads reflect backups/replication and are excluded; log grading keys only on write latency."""
    code = _code(_text(relative))
    assert "d.io_stall_write_ms * 1.0 / NULLIF(d.writes, 0)" in code
    assert "WHEN p.file_type = 'log' THEN ISNULL(p.write_latency_ms, 0)" in code
    assert "WHEN d.file_type = 'log' THEN CASE WHEN d.writes >= @min_ios THEN 'YES' ELSE 'NO' END" in code


@pytest.mark.parametrize("relative", VARIANTS)
def test_data_files_are_graded_on_their_reads_once_there_are_enough_of_them(relative: str):
    code = _code(_text(relative))
    assert "ELSE CASE WHEN d.reads >= @min_ios THEN 'YES' ELSE 'NO' END END AS sample_enough" in code
    assert "ELSE ISNULL(p.read_latency_ms, 0) END AS graded_latency_ms" in code


@pytest.mark.parametrize("relative", VARIANTS)
def test_a_checkpoint_burst_of_slow_data_writes_does_not_page(relative: str):
    """The write latency of a data file is reported, and no branch of the grade reads it."""
    code = _code(_text(relative))
    graded = code[code.index("graded AS"):code.index("AS graded_latency_ms")]
    data_branch = graded[graded.rindex("ELSE"):]
    assert "write_latency_ms" not in data_branch
    assert "write_latency_ms=" in code


@pytest.mark.parametrize("relative", VARIANTS)
def test_pairs_below_min_ios_are_graded_ok_with_sample_enough_no(relative: str):
    code = _code(_text(relative))
    assert "sample_enough" in code
    assert "'YES'" in code and "'NO'" in code
    # Status CASE requires sample_enough = 'YES' before evaluating CRITICAL or WARNING
    assert re.search(r"sample_enough\s*=\s*'YES'\s+AND\s+.*?critical", code, re.IGNORECASE)
    assert re.search(r"sample_enough\s*=\s*'YES'\s+AND\s+.*?warning", code, re.IGNORECASE)


@pytest.mark.parametrize("relative", VARIANTS)
def test_idle_instance_with_no_file_io_emits_single_ok_row_for_io_latency(relative: str):
    code = _code(_text(relative))
    assert "'io_latency'" in code or "''io_latency''" in code
    assert "NOT EXISTS (SELECT 1 FROM @deltas)" in code or "NOT EXISTS ( SELECT 1 FROM @deltas )" in code


# --------------------------------------------------------------------------- #
# Message contract and ordering
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_emit_all_specified_message_keys_as_key_value_pairs(relative: str):
    code = _code(_text(relative))
    required_keys = (
        "database=",
        "file_type=",
        "read_latency_ms=",
        "write_latency_ms=",
        "reads=",
        "writes=",
        "bytes_read=",
        "bytes_written=",
        "sample_ms=",
        "sample_enough=",
        "warning_ms=",
        "critical_ms=",
        "note=interval_sample_not_cumulative",
    )
    for key in required_keys:
        assert key in code, f"{relative} must include '{key}' in message output"


@pytest.mark.parametrize("relative", VARIANTS)
def test_both_variants_order_rows_by_severity_critical_first_then_database_then_type(relative: str):
    code = _code(_text(relative))
    assert "ORDER BY" in code
    # Severity ordering: CRITICAL (1), WARNING (2), OK (3)
    assert re.search(r"severity_rank.*database_name.*file_type", code, re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Compatibility and safety constraints
# --------------------------------------------------------------------------- #
def test_legacy_2008r2_variant_contains_no_features_introduced_after_2008_r2():
    """SQL Server 2008 R2 rejects CONCAT, IIF, TRY_CONVERT, TRY_CAST, FORMAT, STRING_AGG."""
    code = _code(_text(LEGACY))
    forbidden = (
        "CONCAT",
        "IIF",
        "TRY_CONVERT",
        "TRY_CAST",
        "FORMAT",
        "STRING_AGG",
        "DATEFROMPARTS",
        "TIMEFROMPARTS",
        "EOMONTH",
        "CHOOSE",
        "TRY_PARSE",
    )
    for keyword in forbidden:
        assert keyword.lower() not in code.lower(), f"{keyword} is not supported in SQL Server 2008 R2"


@pytest.mark.parametrize("relative", VARIANTS)
def test_neither_variant_contains_any_destructive_ddl_or_dml_statements(relative: str):
    code = _code(_text(relative)).upper()
    destructive = ("DROP ", "DELETE ", "TRUNCATE ", "ALTER ", "KILL ")
    for verb in destructive:
        assert verb not in code, f"{relative} must be read-only and never include {verb}"
