"""Oracle temporary space in use now is evaluated from active segments and graded.

Item 1.144 overhauls Oracle STORAGE_TEMP_SPACE (`oracle/009_oracle_temp_usage.sql`):

1. Active Sort Usage (In Use Now):
   The previous collector queried `v$temp_space_header`, which measures OS-level allocated extents.
   In locally managed temporary tablespaces, Oracle retains allocated extents after sorting ends;
   `v$temp_space_header` used_pct remains permanently near 100% after a single sort, causing endless
   false CRITICAL alarms.
   The overhauled query measures space in use NOW from `v$tempseg_usage` (blocks * block_size)
   for live sessions.

2. Capacity with Autoextend:
   Active usage is evaluated against effective capacity from `dba_temp_files`, accounting for
   autoextension up to maxbytes across all temporary files of the tablespace.

3. Idle Tablespace Preservation:
   A `LEFT JOIN` from `dba_temp_files` to `v$tempseg_usage` ensures that when zero queries are
   sorting (the healthy default state), the temporary tablespace is retained in the result set
   with 0% utilization and status OK, avoiding empty-result errors.

4. Threshold Encapsulation:
   Thresholds are declared in a top-level `WITH params AS (SELECT 95 AS critical_pct, 85 AS warning_pct FROM dual)`
   CTE. Utilization >= 95% is CRITICAL, >= 85% is WARNING, otherwise OK.
"""

from __future__ import annotations

from pathlib import Path
import re
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMP_USAGE_SQL = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "oracle" / "009_oracle_temp_usage.sql"


def _sql_text() -> str:
    return TEMP_USAGE_SQL.read_text(encoding="utf-8").replace("\r\n", "\n")


def _sql_code() -> str:
    """Return SQL without comments and with collapsed whitespace."""
    lines = [
        line.split("--", 1)[0]
        for line in _sql_text().splitlines()
        if not line.lstrip().startswith("--")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


def test_temp_space_collector_reads_active_usage_from_v_tempseg_usage_and_rejects_header_view():
    """Collector must read active segments from v$tempseg_usage and must not read v$temp_space_header."""
    code = _sql_code().upper()
    assert "V$TEMPSEG_USAGE" in code
    assert "V$TEMP_SPACE_HEADER" not in code, "v$temp_space_header reports retained extents and causes false alarms"


def test_temp_space_collector_reads_capacity_from_dba_temp_files_with_autoextend():
    """Collector must aggregate dba_temp_files per tablespace accounting for autoextend maxbytes."""
    code = _sql_code().upper()
    assert "FROM DBA_TEMP_FILES" in code
    assert "GROUP BY TF.TABLESPACE_NAME" in code or "GROUP BY TABLESPACE_NAME" in code
    assert "AUTOEXTENSIBLE = 'YES'" in code
    assert "MAXBYTES" in code


def test_temp_space_collector_encapsulates_thresholds_in_params_cte():
    """Thresholds must be declared in a top-level WITH params AS (SELECT ... FROM dual) CTE."""
    sql = _sql_text()
    match = re.search(r"WITH\s+params\s+AS\s*\(\s*SELECT\s+.*?\s+FROM\s+dual\s*\)", sql, re.IGNORECASE | re.DOTALL)
    assert match, "009_oracle_temp_usage.sql must declare thresholds in a top-level params CTE"
    assert "95 AS critical_pct" in match.group(0) or "95 AS CRITICAL_PCT" in match.group(0).upper()
    assert "85 AS warning_pct" in match.group(0) or "85 AS WARNING_PCT" in match.group(0).upper()


def test_temp_space_collector_grades_critical_at_95_and_warning_at_85():
    """Utilization >= 95% is CRITICAL, >= 85% is WARNING, and idle/normal is OK."""
    code = _sql_code()
    assert re.search(r">=\s*p\.critical_pct\s+THEN\s+'CRITICAL'", code, re.IGNORECASE)
    assert re.search(r">=\s*p\.warning_pct\s+THEN\s+'WARNING'", code, re.IGNORECASE)
    assert "ELSE 'OK'" in code.upper()


def test_temp_space_collector_left_joins_sort_usage_to_preserve_idle_tablespaces():
    """Left outer join must preserve temporary tablespaces when zero active sorts are running."""
    code = _sql_code().upper()
    assert "LEFT JOIN" in code
    assert "NVL(" in code


def test_temp_space_collector_projects_five_standard_columns():
    """Must project METRIC_ITEM, METRIC_VALUE, METRIC_UNIT, STATUS, and MESSAGE."""
    code = _sql_code().upper()
    for col in ("METRIC_ITEM", "METRIC_VALUE", "METRIC_UNIT", "STATUS", "MESSAGE"):
        assert f"AS {col}" in code or f"AS \"{col}\"" in code


def test_temp_space_collector_message_contains_expected_keys():
    """Message must report temp_tablespace, used_pct, used_mb, max_mb, allocated_mb, and file counts."""
    code = _sql_code()
    for key in ("temp_tablespace=", "used_pct=", "used_mb=", "max_mb=", "allocated_mb=", "tempfiles="):
        assert key in code
