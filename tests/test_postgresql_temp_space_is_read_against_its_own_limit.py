"""PostgreSQL's temp space is graded against the limit PostgreSQL enforces (1.149's port).

STORAGE_TEMP_SPACE grades SQL Server's tempdb and Oracle's temporary tablespaces by how full they are,
85 % WARNING and 95 % CRITICAL. PostgreSQL has nothing to be full: a spill goes to files on the data
filesystem. What it enforces is temp_file_limit, per process - past it the query fails - so the
approved thresholds are read against that, for the backend with the most temp files in use now
(pg_ls_tmpdir, 12+, the pid in each file's name). No limit, nothing graded. On the 18 lab a staged
spill read 53 MB in one file on the spilling backend, and 0 once it ended; copies fed 84 %, 90 % and
97 % of a 1 GB limit graded OK, WARNING and CRITICAL, 5 GB with no limit OK.
"""

from __future__ import annotations

import re
from pathlib import Path

from dbabrain.metrics.definitions import load_metric_definitions

from conftest import shipped_config

SQL = (Path(__file__).resolve().parents[1] / "dbabrain" / "metrics" / "collectors" / "postgresql"
       / "008_postgresql_temp_space.sql").read_text(encoding="utf-8")
CODE = re.sub(r"\s+", " ", " ".join(line.split("--", 1)[0] for line in SQL.splitlines()))


def test_the_approved_thresholds_are_read_against_temp_file_limit():
    assert "SELECT 95 AS critical_pct, 85 AS warning_pct" in CODE
    assert "SELECT setting::bigint AS kb FROM pg_settings WHERE name = 'temp_file_limit'" in CODE
    assert "WHEN l.kb > 0 AND top.bytes >= l.kb * 1024.0 * p.critical_pct / 100 THEN 'CRITICAL'" in CODE
    assert "WHEN l.kb > 0 AND top.bytes >= l.kb * 1024.0 * p.warning_pct / 100 THEN 'WARNING'" in CODE


def test_the_files_are_read_by_tablespace_and_their_backend_from_the_name():
    assert "CROSS JOIN LATERAL pg_ls_tmpdir(t.oid) AS f" in CODE
    assert "(regexp_match(f.name, '^pgsql_tmp([0-9]+)'))[1]::integer AS pid" in CODE


def test_no_statement_text_is_carried():
    assert "query" not in CODE.lower().replace("query_", "")


def test_the_catalogue_runs_it_from_12():
    metric = next(item for item in load_metric_definitions(shipped_config("metric_definitions.json"))
                  if item.metric_code == "STORAGE_TEMP_SPACE")
    postgres = {variant.name: variant for variant in metric.variants if variant.db_type == "postgresql"}
    assert postgres["postgresql_12_plus"].path.name == "008_postgresql_temp_space.sql"
    assert postgres["postgresql_12_plus"].min_major_version == 12
    assert postgres["postgresql_pre_12_unsupported"].supported is False
