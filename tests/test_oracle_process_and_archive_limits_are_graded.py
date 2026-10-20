"""Oracle process limits, archive destination health, and tablespace capacity are graded.

Item 1.138 introduces modern Oracle variants for process and session limit exhaustion
(PROCESS_LIMIT), archive destination health (LOG_FILE_SPACE), and ports multi-file tablespace
capacity evaluation (STORAGE_DATA_FILE_SPACE) from legacy 8i into modern Oracle collectors.

1. PROCESS_LIMIT (`oracle/044_oracle_process_limit.sql`):
   Queries v$resource_limit for 'processes' and 'sessions', comparing current_utilization
   against limit_value. Handles 'UNLIMITED' and whitespace-padded limit values gracefully.
   Grades >= 90 % CRITICAL, >= 80 % WARNING. Resources without limits are not graded (OK).

2. Archive Destination Health (`oracle/110_oracle_archive_dest.sql` and `oracle/007_oracle_archive_log_space.sql`):
   Queries v$archive_dest for configured destinations (destination IS NOT NULL). A destination
   whose status is not VALID (e.g. ERROR, BAD PARAM) halts archiving once redo groups fill and is
   graded CRITICAL, reporting the error text in the message. INACTIVE and DEFERRED destinations are
   unused or administratively paused and are graded OK. Inside a PDB, v$archive_dest may show CDB
   rows or nothing; an empty result is expected and not a failure.

3. Tablespace Capacity Roll-Up (`oracle/006_oracle_data_file_space.sql`):
   Multi-file tablespaces must be evaluated as ONE whole rather than file-by-file. In Oracle, extents
   can allocate in any file of a tablespace that has free space or autoextend headroom. The collector
   aggregates dba_data_files and dba_free_space per tablespace, computing effective capacity including
   autoextend up to maxbytes, grading >= 95 % CRITICAL, >= 85 % WARNING.
"""

from __future__ import annotations

from pathlib import Path
import re
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "oracle"

PROCESS_LIMIT_SQL = COLLECTORS / "044_oracle_process_limit.sql"
ARCHIVE_DEST_SQL = COLLECTORS / "110_oracle_archive_dest.sql"
ARCHIVE_LOG_SPACE_SQL = COLLECTORS / "007_oracle_archive_log_space.sql"
DATA_FILE_SPACE_SQL = COLLECTORS / "006_oracle_data_file_space.sql"


def _sql_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _sql_code(path: Path) -> str:
    """Return SQL without comments and with collapsed whitespace."""
    lines = [
        line.split("--", 1)[0]
        for line in _sql_text(path).splitlines()
        if not line.lstrip().startswith("--")
    ]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# PROCESS_LIMIT (044_oracle_process_limit.sql)
# --------------------------------------------------------------------------- #

def test_process_limit_collector_encapsulates_thresholds_in_params_cte():
    """Thresholds must be declared in a top-level WITH params AS (SELECT ... FROM dual) CTE."""
    sql = _sql_text(PROCESS_LIMIT_SQL)
    match = re.search(r"WITH\s+params\s+AS\s*\(\s*SELECT\s+.*?\s+FROM\s+dual\s*\)", sql, re.IGNORECASE | re.DOTALL)
    assert match, "044_oracle_process_limit.sql must declare thresholds in a top-level params CTE"
    assert "90 AS critical_pct" in match.group(0) or "90 AS CRITICAL_PCT" in match.group(0).upper()
    assert "80 AS warning_pct" in match.group(0) or "80 AS WARNING_PCT" in match.group(0).upper()


def test_process_limit_collector_queries_v_resource_limit_for_processes_and_sessions():
    """Collector must read current and max utilization from v$resource_limit for processes and sessions."""
    code = _sql_code(PROCESS_LIMIT_SQL).upper()
    assert "FROM V$RESOURCE_LIMIT" in code
    assert "'PROCESSES'" in code
    assert "'SESSIONS'" in code


def test_process_limit_collector_handles_unlimited_and_whitespace_padded_limits():
    """Collector must parse or handle 'UNLIMITED' and whitespace-padded limits without throwing errors."""
    code = _sql_code(PROCESS_LIMIT_SQL).upper()
    assert "TRIM(" in code
    assert "UNLIMITED" in code


def test_process_limit_collector_grades_critical_at_90_percent_and_warning_at_80_percent():
    """Collector must grade used_pct >= 90 % as CRITICAL, >= 80 % as WARNING, and unlimited as OK."""
    code = _sql_code(PROCESS_LIMIT_SQL)
    assert "CRITICAL" in code
    assert "WARNING" in code
    assert "OK" in code
    assert re.search(r"used_pct\s*>=\s*p\.critical_pct\s+THEN\s+'CRITICAL'", code, re.IGNORECASE)
    assert re.search(r"used_pct\s*>=\s*p\.warning_pct\s+THEN\s+'WARNING'", code, re.IGNORECASE)


def test_process_limit_collector_projects_five_standard_columns():
    """Must project METRIC_ITEM, METRIC_VALUE, METRIC_UNIT, STATUS, and MESSAGE."""
    code = _sql_code(PROCESS_LIMIT_SQL).upper()
    for col in ("METRIC_ITEM", "METRIC_VALUE", "METRIC_UNIT", "STATUS", "MESSAGE"):
        assert f"AS {col}" in code or f"AS \"{col}\"" in code


def test_process_limit_collector_message_contains_expected_keys():
    """Message must report resource, current, max_seen, limit, and used_pct."""
    code = _sql_code(PROCESS_LIMIT_SQL)
    for key in ("resource=", "current=", "max_seen=", "limit=", "used_pct="):
        assert key in code


# --------------------------------------------------------------------------- #
# Archive Destination Health (110_oracle_archive_dest.sql & 007_oracle_archive_log_space.sql)
# --------------------------------------------------------------------------- #

def test_archive_dest_collector_queries_v_archive_dest_for_configured_destinations():
    """Archive destination check must query v$archive_dest where destination is not null."""
    code = _sql_code(ARCHIVE_DEST_SQL).upper()
    assert "FROM V$ARCHIVE_DEST" in code
    assert "DESTINATION IS NOT NULL" in code


def test_archive_dest_collector_grades_non_valid_destinations_as_critical():
    """A destination not VALID is CRITICAL; VALID, INACTIVE, and DEFERRED are OK."""
    code = _sql_code(ARCHIVE_DEST_SQL).upper()
    assert "WHEN STATUS = 'VALID' THEN 'OK'" in code
    assert "IN ('INACTIVE', 'DEFERRED')" in code
    assert "THEN 'OK'" in code
    assert "ELSE 'CRITICAL'" in code


def test_archive_dest_collector_includes_error_in_message():
    """Message must report destination, status, binding, and error details."""
    code = _sql_code(ARCHIVE_DEST_SQL)
    for key in ("destination=", "status=", "binding=", "error="):
        assert key in code


def test_archive_dest_collector_projects_five_standard_columns():
    """Must project METRIC_ITEM, METRIC_VALUE, METRIC_UNIT, STATUS, and MESSAGE."""
    code = _sql_code(ARCHIVE_DEST_SQL).upper()
    for col in ("METRIC_ITEM", "METRIC_VALUE", "METRIC_UNIT", "STATUS", "MESSAGE"):
        assert f"AS {col}" in code or f"AS \"{col}\"" in code


def test_modern_archive_log_space_007_includes_archive_destination_checks():
    """007_oracle_archive_log_space.sql must query v$archive_dest in addition to Fast Recovery Area."""
    code = _sql_code(ARCHIVE_LOG_SPACE_SQL).upper()
    assert "FROM V$ARCHIVE_DEST" in code
    assert "DESTINATION IS NOT NULL" in code
    assert "V$RECOVERY_FILE_DEST" in code


# --------------------------------------------------------------------------- #
# Tablespace Multi-File Roll-Up (006_oracle_data_file_space.sql)
# --------------------------------------------------------------------------- #

def test_tablespace_collector_grades_multi_file_tablespaces_as_one_by_grouping_data_files():
    """Tablespace usage must aggregate across all datafiles per tablespace (porting legacy_8i/101)."""
    code = _sql_code(DATA_FILE_SPACE_SQL).upper()
    assert "FROM DBA_DATA_FILES" in code
    assert "GROUP BY DF.TABLESPACE_NAME" in code or "GROUP BY TABLESPACE_NAME" in code
    assert "GROUP BY FS.TABLESPACE_NAME" in code or "GROUP BY TABLESPACE_NAME" in code


def test_tablespace_collector_accounts_for_autoextend_headroom_up_to_maxbytes():
    """Effective capacity must account for autoextensible data files up to maxbytes."""
    code = _sql_code(DATA_FILE_SPACE_SQL).upper()
    assert "AUTOEXTENSIBLE = 'YES'" in code
    assert "MAXBYTES" in code


def test_tablespace_collector_left_joins_free_space_to_preserve_zero_free_tablespaces():
    """Free space must be LEFT JOINed so a tablespace with zero free extents is not silently dropped."""
    code = _sql_code(DATA_FILE_SPACE_SQL).upper()
    assert "LEFT JOIN" in code
    assert "NVL(" in code


def test_tablespace_collector_encapsulates_thresholds_in_params_cte():
    """Thresholds must be declared in a top-level WITH params AS (SELECT ... FROM dual) CTE."""
    sql = _sql_text(DATA_FILE_SPACE_SQL)
    match = re.search(r"WITH\s+params\s+AS\s*\(\s*SELECT\s+.*?\s+FROM\s+dual\s*\)", sql, re.IGNORECASE | re.DOTALL)
    assert match, "006_oracle_data_file_space.sql must declare thresholds in a top-level params CTE"
    assert "95 AS critical_pct" in match.group(0) or "95 AS CRITICAL_PCT" in match.group(0).upper()
    assert "85 AS warning_pct" in match.group(0) or "85 AS WARNING_PCT" in match.group(0).upper()


def test_tablespace_collector_grades_critical_at_95_and_warning_at_85():
    """Used pct >= 95 % is CRITICAL, >= 85 % is WARNING, otherwise OK."""
    code = _sql_code(DATA_FILE_SPACE_SQL)
    assert re.search(r"used_pct\s*>=\s*p\.critical_pct\s+THEN\s+'CRITICAL'", code, re.IGNORECASE)
    assert re.search(r"used_pct\s*>=\s*p\.warning_pct\s+THEN\s+'WARNING'", code, re.IGNORECASE)


def test_tablespace_collector_projects_five_standard_columns():
    """Must project METRIC_ITEM, METRIC_VALUE, METRIC_UNIT, STATUS, and MESSAGE."""
    code = _sql_code(DATA_FILE_SPACE_SQL).upper()
    for col in ("METRIC_ITEM", "METRIC_VALUE", "METRIC_UNIT", "STATUS", "MESSAGE"):
        assert f"AS {col}" in code or f"AS \"{col}\"" in code


def test_the_tablespace_is_graded_and_its_files_are_kept_as_the_reports_read_them():
    """The tablespace is what fills, so only its row is graded; the per-file rows stay exactly as
    the server page (its datafile list) and the fleet page (the database's size) read them - and the
    tablespace row names itself `scope=tablespace, name=`, so neither reader counts it twice."""
    code = _sql_code(REPO_ROOT / "dbabrain/metrics/collectors/oracle/006_oracle_data_file_space.sql")

    assert "'scope=tablespace, name=' || t.tablespace_name" in code
    assert "'tablespace=' || f.tablespace_name" in code and "', file=' || f.file_name" in code
    assert "', current_mb=' || TO_CHAR(ROUND(f.total_bytes / 1024 / 1024, 2))" in code
    assert "CAST('LOGGING' AS varchar2(16)) AS status" in code
    tablespace_row = code[code.index("1 AS sort_group"):code.index("2 AS sort_group")]
    assert "'tablespace='" not in tablespace_row and "current_mb" not in tablespace_row


def test_inside_a_pdb_the_process_limits_are_read_from_v_parameter():
    """v$resource_limit answers no rows inside a PDB (26ai FREEPDB1): the limits come from
    v$parameter and a count of v$process / v$session, and the message names that source."""
    code = _sql_code(REPO_ROOT / "dbabrain/metrics/collectors/oracle/044_oracle_process_limit.sql")

    assert "FROM v$parameter pr" in code
    assert "AND NOT EXISTS (SELECT 1 FROM raw_limits)" in code
    assert "', source=' || g.source" in code
