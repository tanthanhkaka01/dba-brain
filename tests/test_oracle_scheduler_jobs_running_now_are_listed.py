"""Oracle's running scheduler jobs are listed the way SQL Server's Agent jobs are (1.149's port).

SQL_AGENT_JOB_RUNTIME is an inventory of the moment on SQL Server - a running job with its elapsed
seconds, an enabled job with its next run - and grades nothing. The Oracle port keeps that contract
(dba_scheduler_running_jobs, dba_scheduler_jobs) so the two engines mean the same thing, and grades
one thing only: a job past the max_run_duration it declares itself, which is when Oracle raises
JOB_OVER_MAX_DUR. A threshold of ours here would be a number nobody approved; the job's own is not.
Fed synthetic jobs on the 26ai lab: a job 90 minutes into a 60-minute limit WARNING, ten hours with
no limit OK, a SYS job left out, a disabled job with no next run listed.
"""

from __future__ import annotations

import re
from pathlib import Path

from dbabrain.metrics.definitions import load_metric_definitions

from conftest import shipped_config

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"


def _code(relative: str) -> str:
    text = (COLLECTORS / relative).read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", " ".join(line.split("--", 1)[0] for line in text.splitlines()))


def test_only_a_job_past_its_own_declared_limit_warns():
    code = _code("oracle/056_oracle_scheduler_job_runtime.sql")
    assert ("CASE WHEN j.max_run_duration IS NOT NULL AND r.elapsed_time > j.max_run_duration "
            "THEN 1 ELSE 0 END AS over_limit") in code
    assert "CASE WHEN r.over_limit = 1 THEN 'WARNING' ELSE 'OK' END AS status" in code
    assert "CRITICAL" not in code


def test_it_lists_what_sql_servers_lists_in_the_same_words():
    oracle = _code("oracle/056_oracle_scheduler_job_runtime.sql")
    sql_server = _code("sqlserver/056_sqlserver_agent_job_runtime.sql")
    for words in ("'RUNNING '", "'NEXT '", "NO RUNNING OR SCHEDULED JOBS", "Next scheduled run for an enabled job"):
        assert words in oracle and words in sql_server
    assert "j.enabled = 'TRUE'" in oracle


def test_jobs_oracle_runs_for_itself_are_left_out_here_too():
    owners = "('SYS', 'SYSTEM', 'ORACLE_OCM', 'EXFSYS', 'WMSYS', 'DBSNMP', 'APPQOSSYS', 'CTXSYS')"
    code = _code("oracle/056_oracle_scheduler_job_runtime.sql")
    assert code.count(f"owner NOT IN {owners}") == 2


def test_the_catalogue_runs_it_from_10g():
    metric = next(item for item in load_metric_definitions(shipped_config("metric_definitions.json"))
                  if item.metric_code == "SQL_AGENT_JOB_RUNTIME")
    oracle = {variant.name: variant for variant in metric.variants if variant.db_type == "oracle"}
    assert oracle["oracle_10g_plus"].path.name == "056_oracle_scheduler_job_runtime.sql"
    assert oracle["oracle_10g_plus"].min_major_version == 10
    assert oracle["oracle_pre_10g_unsupported"].supported is False
