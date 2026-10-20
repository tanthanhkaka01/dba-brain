"""An Oracle PDB not open READ WRITE is graded CRITICAL on a primary database.

Item 1.139 addresses multitenant container database (CDB) and pluggable database (PDB)
availability monitoring under DATABASE_STATUS (`oracle/002_oracle_database_status.sql`):

1. PDB Open Mode Grading:
   On an Oracle primary database, any user PDB that is not open READ WRITE (e.g. MOUNTED,
   READ ONLY, or CLOSED) prevents application connections and represents an active outage;
   this condition must be graded CRITICAL.
   PDB$SEED is the system template pluggable database and is expected to remain in READ ONLY;
   it is graded OK.

2. Data Guard Standby Awareness:
   On a Data Guard physical standby or active standby (`v$database.database_role <> 'PRIMARY'`),
   PDBs are normally in MOUNTED state (standard physical standby) or READ ONLY / READ ONLY WITH APPLY
   (Active Data Guard). These states are normal operational states on a standby and are graded OK.

3. Container Context:
   Connected sessions must report the container they are in via `SYS_CONTEXT('USERENV', 'CON_NAME')`
   in the message key `session_container=`.
   When connected directly into a PDB (e.g. `FREEPDB1`), `v$pdbs` exposes only that PDB; the
   collector evaluates what is visible without failing.

4. Oracle 11g Compatibility:
   Oracle 11g and earlier versions lack multitenant architecture and have no `v$pdbs` view.
   A dedicated variant (`oracle/002_oracle_database_status_11g.sql`) queries `v$database` directly
   without referencing multitenant dictionary views, preserving parse and execution compatibility
   on pre-12c databases.
"""

from __future__ import annotations

from pathlib import Path
import re
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "oracle"

MODERN_12C_SQL = COLLECTORS / "002_oracle_database_status.sql"
LEGACY_11G_SQL = COLLECTORS / "002_oracle_database_status_11g.sql"


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
# Modern Multitenant Variant (002_oracle_database_status.sql)
# --------------------------------------------------------------------------- #

def test_pdb_collector_grades_pdb_not_open_read_write_as_critical_on_primary():
    """On a primary database, any PDB not open READ WRITE (except PDB$SEED) must be CRITICAL."""
    code = _sql_code(MODERN_12C_SQL).upper()
    assert "WHEN P.OPEN_MODE = 'READ WRITE' THEN 'OK'" in code
    assert "ELSE 'CRITICAL'" in code


def test_pdb_collector_grades_pdb_seed_read_only_as_ok():
    """PDB$SEED in READ ONLY mode is expected template behaviour and must be graded OK."""
    code = _sql_code(MODERN_12C_SQL).upper()
    assert re.search(r"P\.NAME\s*=\s*'PDB\$SEED'\s+AND\s+P\.OPEN_MODE\s+LIKE\s+'READ ONLY%'\s+THEN\s+'OK'", code)


def test_pdb_collector_grades_standby_mounted_or_read_only_as_ok():
    """On a standby database (role <> 'PRIMARY'), PDBs in MOUNTED or READ ONLY are graded OK."""
    code = _sql_code(MODERN_12C_SQL).upper()
    assert "DATABASE_ROLE <> 'PRIMARY'" in code
    assert "P.OPEN_MODE LIKE 'READ ONLY%'" in code or "P.OPEN_MODE = 'MOUNTED'" in code


def test_pdb_collector_names_session_container_from_sys_context():
    """Every emitted row message must report session_container from SYS_CONTEXT('USERENV', 'CON_NAME')."""
    code = _sql_code(MODERN_12C_SQL).upper()
    assert "SESSION_CONTAINER=" in code
    assert "SYS_CONTEXT('USERENV', 'CON_NAME')" in code or "SYS_CONTEXT('USERENV','CON_NAME')" in code


def test_pdb_collector_projects_five_standard_columns():
    """Must project METRIC_ITEM, METRIC_VALUE, METRIC_UNIT, STATUS, and MESSAGE."""
    code = _sql_code(MODERN_12C_SQL).upper()
    for col in ("METRIC_ITEM", "METRIC_VALUE", "METRIC_UNIT", "STATUS", "MESSAGE"):
        assert f"AS {col}" in code or f"AS \"{col}\"" in code


def test_pdb_collector_handles_visible_pdbs_from_v_pdbs():
    """Collector queries v$pdbs joined to v$database to report visible pluggable databases."""
    code = _sql_code(MODERN_12C_SQL).upper()
    assert "FROM V$PDBS" in code
    assert "V$DATABASE" in code


# --------------------------------------------------------------------------- #
# Oracle 11g Non-CDB Variant (002_oracle_database_status_11g.sql)
# --------------------------------------------------------------------------- #

def test_11g_database_status_variant_queries_v_database_without_v_pdbs():
    """11g variant must query v$database and must not reference v$pdbs so it parses on pre-12c."""
    code = _sql_code(LEGACY_11G_SQL).upper()
    assert "FROM V$DATABASE" in code
    assert "V$PDBS" not in code


def test_11g_database_status_variant_handles_data_guard_standby_roles():
    """11g variant must grade MOUNTED or READ ONLY as OK on a Data Guard standby."""
    code = _sql_code(LEGACY_11G_SQL).upper()
    assert "DATABASE_ROLE <> 'PRIMARY'" in code
    assert "OPEN_MODE = 'READ WRITE' THEN 'OK'" in code
    assert "ELSE 'CRITICAL'" in code


def test_11g_database_status_variant_names_session_container():
    """11g variant must include session_container in message using SYS_CONTEXT safe fallback."""
    code = _sql_code(LEGACY_11G_SQL).upper()
    assert "SESSION_CONTAINER=" in code
    assert "SYS_CONTEXT('USERENV', 'CON_NAME')" in code or "SYS_CONTEXT('USERENV','CON_NAME')" in code


def test_11g_database_status_variant_projects_five_standard_columns():
    """11g variant must project METRIC_ITEM, METRIC_VALUE, METRIC_UNIT, STATUS, and MESSAGE."""
    code = _sql_code(LEGACY_11G_SQL).upper()
    for col in ("METRIC_ITEM", "METRIC_VALUE", "METRIC_UNIT", "STATUS", "MESSAGE"):
        assert f"AS {col}" in code or f"AS \"{col}\"" in code
