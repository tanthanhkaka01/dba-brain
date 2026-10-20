"""A failed Oracle scheduler job pages someone, by SQL Server's own rule (1.149).

JOB_FAILED was SQL Server's only, and SQL_AGENT_JOB_INVENTORY lists the Oracle scheduler's jobs at
OK by design, so a DBMS_SCHEDULER job failing every run paged nobody. The Oracle 10g+ variant reads
dba_scheduler_job_run_details over 24 hours and grades the fail rate exactly as SQL Server's does -
70 % CRITICAL, 50 % WARNING, any failure LOGGING - leaving out the owners Oracle maintains, the same
list the job inventory leaves out. Run on the .251 26ai PDB (no user job, OK) and on a copy fed
synthetic runs.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from conftest import present_copies

REPO_ROOT = Path(__file__).resolve().parents[1]
ORACLE = REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "oracle"
CATALOGUES = present_copies("data/metric_definitions.json", "data/metric_definitions.example.json",
                            "dbabrain/metrics/catalogue/metric_definitions.json")


def _code(path: Path) -> str:
    lines = [line.split("--", 1)[0] for line in path.read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("--")]
    return re.sub(r"\s+", " ", " ".join(lines))


def test_the_fail_rate_is_graded_as_sql_servers_is():
    code = _code(ORACLE / "010_oracle_scheduler_failed_jobs.sql")
    assert "SELECT 24 AS window_hours, 70 AS critical_fail_pct, 50 AS warning_fail_pct FROM dual" in code
    assert ("WHEN f.fail_rate_percent >= p.critical_fail_pct THEN 'CRITICAL' "
            "WHEN f.fail_rate_percent >= p.warning_fail_pct THEN 'WARNING' ELSE 'LOGGING'") in code
    sql_server = _code(REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "sqlserver" / "010_sqlserver_agent_failed_jobs.sql")
    assert "WHEN fail_rate_percent >= 70 THEN 'CRITICAL'" in sql_server
    assert "WHEN fail_rate_percent >= 50 THEN 'WARNING'" in sql_server


def test_jobs_oracle_runs_for_itself_are_left_out_as_the_inventory_leaves_them_out():
    failed = _code(ORACLE / "010_oracle_scheduler_failed_jobs.sql")
    inventory = _code(ORACLE / "070_oracle_scheduler_jobs.sql")
    owners = "('SYS', 'SYSTEM', 'ORACLE_OCM', 'EXFSYS', 'WMSYS', 'DBSNMP', 'APPQOSSYS', 'CTXSYS')"
    assert f"r.owner NOT IN {owners}" in failed
    assert f"owner NOT IN {owners}" in inventory


def test_a_disabled_job_is_left_out_as_sql_server_leaves_it_out_but_a_dropped_one_is_not():
    """SQL Server reads enabled Agent jobs only; the port had missed it (review, 2026-10-16). Only a
    job that still exists and is disabled: a one-off auto_drop job is gone once it has run, and its
    failure is the one somebody needs to hear about."""
    code = _code(ORACLE / "010_oracle_scheduler_failed_jobs.sql")
    assert ("AND NOT EXISTS ( SELECT 1 FROM dba_scheduler_jobs j WHERE j.owner = r.owner "
            "AND j.job_name = r.job_name AND j.enabled = 'FALSE' )") in code
    sql_server = _code(REPO_ROOT / "dbabrain" / "metrics" / "collectors" / "sqlserver" / "010_sqlserver_agent_failed_jobs.sql")
    assert "AND j.enabled = 1" in sql_server


def test_a_clean_day_is_one_ok_row():
    code = _code(ORACLE / "010_oracle_scheduler_failed_jobs.sql")
    assert re.search(r"SELECT CAST\('scheduler' AS varchar2\(256\)\), '0', .*'OK'.*WHERE NOT EXISTS \(SELECT 1 FROM failed\)", code)


@pytest.mark.parametrize("relative", CATALOGUES)
def test_oracle_from_10g_runs_the_variant(relative):
    data = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
    metric = next(m for m in data["metrics"] if m["metric_code"] == "JOB_FAILED")
    oracle = {v["name"]: v for v in metric["variants"] if v["db_type"] == "oracle"}
    assert oracle["oracle_10g_plus"]["file"] == "oracle/010_oracle_scheduler_failed_jobs.sql"
    assert oracle["oracle_10g_plus"]["min_major_version"] == 10
    assert oracle["oracle_pre_10g_unsupported"]["supported"] is False
