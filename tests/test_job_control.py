"""Switching a scheduled job off, and the four ways that must not go wrong.

`disable-job` exists because of `OptimizeIndex_Weekly`: five collapses of 192.0.2.115 in nine
weeks (`audits/20260814_audit_optimizeindex_weekly_server_collapse.md`), and every time the only
way to stop the next run was to reach a workstation and untick a box.

What these tests hold:

* **The right statement reaches the right engine.** A job is one of the few dangerous objects all
  three engines have, and they disagree about what it is — Agent, DBMS_SCHEDULER, the older
  DBMS_JOB, pg_cron. Sending SQL Server's `sp_update_job` to Oracle fails loudly, but sending a
  DBMS_SCHEDULER call for a name that DBMS_JOB actually owns fails *quietly*: `ORA-27475` looks
  like a typo rather than a wrong mechanism.
* **The gate holds.** Nothing runs without `confirm`, and `dry_run` runs nothing at all.
* **An already-disabled job is OK, not FAIL.** The desired state is the actual state; a retry
  after a Telegram timeout must not read as an error.
* **A missing job changes nothing** and says what is actually there.
"""

from __future__ import annotations

import pytest

from db_ops.common import db_catalog, job_control


def _jobs(*rows):
    return {"server_id": "ACME-192-0-2-115", "db_type": "sqlserver", "ip": "192.0.2.115",
            "credential_name": "", "jobs": list(rows), "count": len(rows),
            "enabled_count": sum(1 for r in rows if r["enabled"]),
            "disabled_hidden": 0, "note": ""}


def _job(name="OptimizeIndex_Weekly", *, enabled=True, source="agent", owner="sa"):
    return {"name": name, "enabled": enabled, "category": "Maintenance", "owner": owner,
            "description": "", "source": source}


@pytest.fixture
def executed(monkeypatch):
    """Record the SQL `disable_job` would run, and pretend it succeeded."""
    seen: dict = {}

    def fake_run_sql(request):
        seen.clear()
        seen.update(request)
        return {"columns": [], "rows": [], "row_count": 0}

    monkeypatch.setattr(job_control.sql_run, "run_sql", fake_run_sql)
    return seen


def _listing(monkeypatch, data):
    monkeypatch.setattr(db_catalog, "list_jobs", lambda request: data)


def _request(**overrides):
    base = {"target": "ACME-192-0-2-115", "job_name": "OptimizeIndex_Weekly",
            "confirm": True, "assume_yes": True, "reason": "test"}
    base.update(overrides)
    return base


# --- the statement per engine ------------------------------------------------------------

def test_sqlserver_disables_through_the_agent(monkeypatch, executed):
    _listing(monkeypatch, _jobs(_job()))

    job_control.disable_job(_request())

    assert "sp_update_job" in executed["sql"]
    assert "@enabled = 0" in executed["sql"]
    assert executed["database"] == "msdb"       # sysjobs lives in msdb, not the user database
    assert executed["commit"] is True


def test_oracle_scheduler_job_uses_dbms_scheduler(monkeypatch, executed):
    data = _jobs(_job("NIGHTLY_STATS", source="scheduler", owner="APPUSER"))
    data["db_type"] = "oracle"
    _listing(monkeypatch, data)

    job_control.disable_job(_request(job_name="NIGHTLY_STATS"))

    assert executed["sql"] == "BEGIN DBMS_SCHEDULER.DISABLE('NIGHTLY_STATS'); END;"


def test_an_old_dbms_job_uses_broken_not_the_scheduler(monkeypatch, executed):
    """The quiet failure this guards: DBMS_SCHEDULER.DISABLE on a DBMS_JOB name raises ORA-27475,
    which reads like a typo rather than the wrong mechanism. This estate still runs 8i/9i hosts."""
    data = _jobs(_job("DBMS_JOB:42", source="dbms_job", owner="LTR"))
    data["db_type"] = "oracle"
    _listing(monkeypatch, data)

    job_control.disable_job(_request(job_name="DBMS_JOB:42"))

    assert executed["sql"] == "BEGIN DBMS_JOB.BROKEN(42, TRUE); END;"


def test_postgresql_disables_the_pg_cron_row(monkeypatch, executed):
    data = _jobs(_job("vacuum_nightly", source="pg_cron", owner="postgres"))
    data["db_type"] = "postgresql"
    _listing(monkeypatch, data)

    job_control.disable_job(_request(job_name="vacuum_nightly"))

    assert executed["sql"] == (
        "UPDATE cron.job SET active = false WHERE jobname = 'vacuum_nightly';")


# --- the gate ----------------------------------------------------------------------------

def test_dry_run_executes_nothing(monkeypatch, executed):
    _listing(monkeypatch, _jobs(_job()))

    report = job_control.disable_job(_request(dry_run=True, confirm=False))

    assert executed == {}
    assert report["status"] == "OK"
    assert any(gate["name"] == "dry-run" for gate in report["gates"])


def test_without_confirmation_nothing_runs(monkeypatch, executed):
    _listing(monkeypatch, _jobs(_job()))

    report = job_control.disable_job(
        {"target": "ACME-192-0-2-115", "job_name": "OptimizeIndex_Weekly"})

    assert executed == {}
    assert report["status"] == "FAIL"


# --- states that are not failures ---------------------------------------------------------

def test_an_already_disabled_job_is_ok_and_changes_nothing(monkeypatch, executed):
    """A retry after a Telegram timeout must not look like an error."""
    _listing(monkeypatch, _jobs(_job(enabled=False)))

    report = job_control.disable_job(_request())

    assert executed == {}
    assert report["status"] == "OK"
    assert "already disabled" in report["gates"][0]["detail"]


def test_a_missing_job_changes_nothing_and_says_what_is_there(monkeypatch, executed):
    _listing(monkeypatch, _jobs(_job("Backup_Log"), _job("CommandLog Cleanup")))

    report = job_control.disable_job(_request(job_name="Typo_Job"))

    assert executed == {}
    assert report["status"] == "FAIL"
    assert "2 job(s)" in report["gates"][0]["detail"]


def test_the_reply_warns_that_a_running_job_is_not_stopped(monkeypatch, executed):
    """Disabling affects the schedule, not the execution in flight — the difference an operator
    watching a volume fill needs stated, not implied."""
    _listing(monkeypatch, _jobs(_job()))

    report = job_control.disable_job(_request())

    running = next(g for g in report["gates"] if g["name"] == "running")
    assert "NOT stopped" in running["detail"]


# --- input refusal -------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "  ", "job'; DROP TABLE x--", "a" * 200])
def test_a_name_that_is_not_a_plain_job_name_is_refused(bad):
    with pytest.raises(job_control.JobControlError):
        job_control.disable_job(_request(job_name=bad))


def test_an_unknown_engine_is_refused_by_name():
    with pytest.raises(job_control.JobControlError, match="does not know engine"):
        job_control._disable_statement("mysql", "whatever", "")
