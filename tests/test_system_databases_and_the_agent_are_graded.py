"""System databases and the SQL Server Agent service are graded for availability.

DATABASE_STATUS previously skipped the four system databases (WHERE d.database_id > 4) under the
assumption that DATABASE_CONFIG listed them. However, DATABASE_CONFIG only records an inventory of
settings without grading availability, meaning an offline, suspect, or recovery-pending msdb, model,
or tempdb generated no alert. System databases are now included and evaluated under the same rule
as user databases: not ONLINE = CRITICAL.

In addition, targets monitored purely over SQL connections without host-level cmd_access lack
OS_SERVICE_STATUS checks. SQL Server Agent is responsible for scheduled backups, maintenance plans,
and replication; if it stops, scheduled jobs silently fail. Both variants now include a service row
querying sys.dm_server_services:
- If Agent status_desc is not 'Running', status is CRITICAL.
- SQL Server Express (EngineEdition = 4) does not include Agent by design; this expected state
  reports LOGGING.
- SQL Server builds prior to 2008 R2 SP1 do not have sys.dm_server_services. The legacy variant
  protects against compile-time bind errors using an existence check and dynamic SQL, reporting
  LOGGING ("not readable on this build").
- SQL Server on Linux starts Agent only when mssql-conf enables it, yet still lists it as 'Stopped'.
  What the OS leaves out is not an outage (the operator, 2026-10-15): the row is OK there - unless
  an enabled job with an enabled schedule is waiting for Agent, which then never runs and stays
  CRITICAL. The first .251 lab run paged CRITICAL on a Docker instance that never asked for Agent.

The job inventory has the same cause: with no Agent there is no job, and an inventory of nothing
read NO_DATA, a finding. It is declared `empty_result_is_ok` in every copy of the catalogue.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = REPO_ROOT / "dbabrain" / "metrics" / "collectors"
MODERN = "sqlserver/002_sqlserver_database_status.sql"
LEGACY = "sqlserver/legacy_2008r2/002_sqlserver_database_status.sql"
VARIANTS = (MODERN, LEGACY)


def _text(relative: str) -> str:
    return (COLLECTORS / relative).read_text(encoding="utf-8").replace("\r\n", "\n")


def _code(sql: str) -> str:
    """The SQL without comments, whitespace collapsed."""
    lines = [line.split("--", 1)[0] for line in sql.splitlines()
             if not line.lstrip().startswith("--")]
    return re.sub(r"\s+", " ", " ".join(lines))


# --------------------------------------------------------------------------- #
# System database grading: master, msdb, model, tempdb
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_system_databases_are_not_excluded_by_database_id_filter(relative):
    """The old WHERE d.database_id > 4 skipped master, tempdb, model, and msdb.
    The filter must not exclude database IDs <= 4."""
    code = _code(_text(relative))
    assert not re.search(r"WHERE\s+.*?\bdatabase_id\s*>\s*4", code, re.IGNORECASE), (
        f"{relative} must not exclude system databases with database_id > 4"
    )


@pytest.mark.parametrize("relative", VARIANTS)
def test_a_database_not_online_is_graded_critical(relative):
    """Both user and system databases must be CRITICAL when state_desc is not ONLINE."""
    code = _code(_text(relative))
    assert re.search(r"WHEN\s+d\.state_desc\s*=\s*'ONLINE'\s+AND\s+d\.is_read_only\s*=\s*0\s+THEN\s+'OK'", code)
    assert re.search(r"WHEN\s+d\.state_desc\s*=\s*'ONLINE'\s+AND\s+d\.is_read_only\s*=\s*1\s+THEN\s+'WARNING'", code)
    assert re.search(r"ELSE\s+'CRITICAL'", code)


# --------------------------------------------------------------------------- #
# SQL Server Agent service grading and guards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_sql_agent_service_is_emitted_as_a_metric_item(relative):
    """A row with metric_item 'sql_agent_service' must be returned for the Agent service."""
    code = _code(_text(relative))
    assert "'sql_agent_service'" in code


@pytest.mark.parametrize("relative", VARIANTS)
def test_agent_not_running_is_graded_critical(relative):
    """When the Agent service is present and status_desc is not 'Running', status must be CRITICAL."""
    code = _code(_text(relative))
    assert re.search(r"WHEN\s+status_desc\s*=\s*''?Running''?\s+THEN\s+''?OK''?\s+ELSE\s+''?CRITICAL''?", code, re.IGNORECASE)


@pytest.mark.parametrize("relative", VARIANTS)
def test_express_edition_with_no_agent_is_graded_logging(relative):
    """SQL Server Express edition (EngineEdition = 4) lacks Agent by design and must report LOGGING."""
    code = _code(_text(relative))
    assert re.search(r"SERVERPROPERTY\s*\(\s*'EngineEdition'\s*\)\s*.*?(?:=\s*4|IN\s*\(\s*4\s*\))", code, re.IGNORECASE)
    assert "'LOGGING'" in code


def _linux_branch(code: str) -> str:
    start = code.index("ELSE IF @@VERSION LIKE '%on Linux%'")
    return code[start:code.index("ELSE BEGIN INSERT INTO @status", start)]


def test_a_linux_instance_is_recognised_before_the_windows_rule_grades_its_agent():
    """@@VERSION names the OS on every build, so the branch needs no 2017-only DMV."""
    code = _code(_text(MODERN))
    assert code.index("@@VERSION LIKE '%on Linux%'") < code.index("WHEN status_desc = 'Running' THEN 'OK' ELSE 'CRITICAL'")
    assert "dm_os_host_info" not in code


def test_a_stopped_agent_on_linux_with_nothing_scheduled_is_ok():
    branch = _linux_branch(_code(_text(MODERN)))
    assert "status_desc = 'Running'" in branch            # only an Agent that is not running
    assert "j.enabled = 1 AND s.enabled = 1" in branch    # counts jobs that would actually run
    assert "CASE WHEN @scheduled_jobs > 0 THEN 'CRITICAL' ELSE 'OK' END" in branch
    assert "'Not enabled'" in branch


def test_a_stopped_agent_on_linux_with_a_scheduled_job_stays_critical():
    """A job waiting for an Agent that never starts is the failure the row exists for."""
    branch = _linux_branch(_code(_text(MODERN)))
    assert "enabled scheduled jobs will not run" in branch


def test_a_login_that_cannot_read_msdb_does_not_fail_the_database_rows():
    """The msdb read is caught: losing every database's state over a job count would be worse."""
    branch = _linux_branch(_code(_text(MODERN)))
    assert re.search(r"BEGIN TRY .*msdb\.dbo\.sysjobs.* END TRY BEGIN CATCH SET @scheduled_jobs = NULL; END CATCH", branch)
    assert "'unreadable'" in branch and "cannot read the msdb jobs" in branch


def test_the_linux_message_keeps_the_agent_contract_and_names_the_platform():
    branch = _linux_branch(_code(_text(MODERN)))
    for field in ("status_desc=", "startup_type_desc=", "service_account=", "platform=Linux", "scheduled_jobs="):
        assert field in branch


@pytest.mark.parametrize("relative", (
    "data/metric_definitions.json",
    "data/metric_definitions.example.json",
    "dbabrain/metrics/catalogue/metric_definitions.json",
))
def test_an_instance_with_no_scheduled_job_has_an_ok_job_inventory(relative):
    data = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
    inventory = next(m for m in data["metrics"] if m["metric_code"] == "SQL_AGENT_JOB_INVENTORY")
    assert inventory["empty_result_is_ok"] is True


def test_legacy_variant_guards_sys_dm_server_services_with_existence_check_and_dynamic_sql():
    """SQL Server 2008 and 2008 R2 RTM lack sys.dm_server_services (introduced in 2008 R2 SP1).
    The legacy variant must check OBJECT_ID and use dynamic SQL so compilation does not fail."""
    text = _text(LEGACY)
    code = _code(text)
    assert "OBJECT_ID('sys.dm_server_services')" in text or "OBJECT_ID(N'sys.dm_server_services')" in text
    assert "sp_executesql" in code
    assert "not readable on this build" in text


# --------------------------------------------------------------------------- #
# Message contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("relative", VARIANTS)
def test_the_agent_message_names_status_startup_type_and_service_account(relative):
    """The Agent service message contract requires status_desc, startup_type_desc, and service_account."""
    code = _code(_text(relative))
    for field in ("status_desc=", "startup_type_desc=", "service_account="):
        assert field in code, f"message in {relative} must include {field}"


# --------------------------------------------------------------------------- #
# Version syntax boundaries
# --------------------------------------------------------------------------- #
def test_the_2008_r2_variant_uses_no_post_2008_r2_syntax():
    """SQL Server 2008 R2 does not support CONCAT, IIF, TRY_CONVERT, or TRY_CAST."""
    code = _code(_text(LEGACY))
    for keyword in ("CONCAT(", "IIF(", "TRY_CONVERT(", "TRY_CAST("):
        assert keyword.lower() not in code.lower(), f"{keyword} is not supported in 2008 R2"
