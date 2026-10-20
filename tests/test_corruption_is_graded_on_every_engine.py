"""A corrupt page pages someone on every engine, not only on SQL Server (1.159, 1.160).

`DATABASE_SUSPECT_PAGES` read `msdb.dbo.suspect_pages` and was `supported: false` everywhere else,
so a checksum failure on PostgreSQL or a corrupt block RMAN had found on Oracle was seen by nothing
dbabrain runs. Both engines keep the answer in a view a monitoring login can read:

- PostgreSQL 12+ counts checksum failures per database in `pg_stat_database`. The counter only
  grows, so it is graded by *when* the last failure happened - within the SQL Server variant's own
  30 days CRITICAL, older recorded - or a page repaired long ago would page for ever. A cluster
  built without data checksums cannot detect one at all: that is said once, at LOGGING.
- Oracle lists what RMAN found corrupt in `v$database_block_corruption`, and clears a block when a
  later pass finds it repaired. A NOLOGGING block is unrecoverable from an older backup, not
  damaged - WARNING; every other type is CRITICAL.

Both were run on the `.251` labs on 2026-10-15 (no corruption, no rows) and on copies of the SQL
fed synthetic rows, which graded as below.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
POSTGRES = COLLECTORS / "postgresql" / "021_postgresql_checksum_failures.sql"
ORACLE = COLLECTORS / "oracle" / "021_oracle_block_corruption.sql"
CATALOGUES = (
    "data/metric_definitions.json",
    "data/metric_definitions.example.json",
    "dbabrain/metrics/catalogue/metric_definitions.json",
)


def _code(path: Path) -> str:
    lines = [line.split("--", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("--")]
    return re.sub(r"\s+", " ", " ".join(lines))


def test_a_recent_checksum_failure_is_critical_and_an_old_one_is_recorded():
    code = _code(POSTGRES)
    assert "WITH params AS ( SELECT 30 AS recent_days )" in code
    assert ("WHEN f.checksum_last_failure >= now() - make_interval(days => p.recent_days) THEN 'CRITICAL' "
            "ELSE 'LOGGING'") in code


def test_only_a_database_with_a_failure_is_a_row():
    assert "AND COALESCE(d.checksum_failures, 0) > 0" in _code(POSTGRES)


def test_the_shared_catalogs_are_named_rather_than_dropped():
    """pg_stat_database's row with no datname is pg_database, pg_authid and the like."""
    assert "COALESCE(d.datname, '(shared catalogs)')" in _code(POSTGRES)


def test_a_cluster_without_data_checksums_says_so_once_at_logging():
    code = _code(POSTGRES)
    assert "WHERE current_setting('data_checksums') = 'on'" in code
    assert re.search(r"SELECT 'data_checksums', 'off', NULL, 'LOGGING', .*WHERE current_setting\('data_checksums'\) <> 'on'", code)


def test_an_oracle_block_rman_found_corrupt_is_critical_and_nologging_a_warning():
    code = _code(ORACLE)
    assert "FROM v$database_block_corruption c" in code
    assert "CASE WHEN c.corruption_type = 'NOLOGGING' THEN 'WARNING' ELSE 'CRITICAL' END AS status" in code
    assert "GROUP BY c.file#, c.corruption_type" in code


@pytest.mark.parametrize("path", (POSTGRES, ORACLE))
def test_neither_collector_writes_anything(path: Path):
    code = _code(path)
    for keyword in ("DROP", "DELETE", "TRUNCATE", "INSERT", "UPDATE", "ALTER", "CREATE"):
        assert not re.search(rf"\b{keyword}\b", code, re.IGNORECASE), keyword


@pytest.mark.parametrize("relative", CATALOGUES)
def test_each_engine_runs_the_variant_its_version_can_read(relative):
    data = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
    metric = next(m for m in data["metrics"] if m["metric_code"] == "DATABASE_SUSPECT_PAGES")
    assert metric["empty_result_is_ok"] is True
    variants = {v["name"]: v for v in metric["variants"]}
    assert variants["postgresql_12_plus"]["file"] == "postgresql/021_postgresql_checksum_failures.sql"
    assert variants["postgresql_12_plus"]["min_major_version"] == 12
    assert variants["postgresql_pre_12_unsupported"]["supported"] is False
    assert variants["postgresql_pre_12_unsupported"]["max_major_version"] == 11
    assert variants["oracle_10g_plus"]["file"] == "oracle/021_oracle_block_corruption.sql"
    assert variants["oracle_10g_plus"]["min_major_version"] == 10
    assert variants["oracle_pre_10g_unsupported"]["supported"] is False
    assert variants["oracle_pre_10g_unsupported"]["max_major_version"] == 9
