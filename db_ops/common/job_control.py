"""Switching a scheduled job off, on whichever engine owns it.

The operation the 2026-08-10 log-full outage needed and did not have. `OptimizeIndex_Weekly` has
taken 192.0.2.115 down five times in nine weeks (`audits/20260814_audit_optimizeindex_weekly_server_collapse.md`),
and every time the only way to stop the next run was to reach a workstation, open SSMS and
untick a box. Disabling is the *safe* half of that emergency: it changes no data, it interrupts
nothing that is already running, and it is trivially reversible — which is exactly why it should
be reachable from a phone when the alternative is watching a volume fill.

**Why this is not in `sqlserver_emergency`.** That module is SQL Server by name and by every line
in it. A job is one of the few dangerous objects all three engines in this estate have, and they
disagree about what it *is*:

* **SQL Server** — one Agent, `msdb.dbo.sp_update_job @enabled = 0`.
* **Oracle** — two schedulers. `DBMS_SCHEDULER.DISABLE` for a modern job, `DBMS_JOB.BROKEN` for
  the older `DBMS_JOB` entries this estate still runs on its 8i/9i hosts. The `source` reported by
  ``list-jobs`` says which one owns a given name, and this module dispatches on it rather than
  guessing.
* **PostgreSQL** — nothing built in. `pg_cron` is the usual add-on (`UPDATE cron.job SET active`),
  and when it is absent that is reported as "there is no scheduler here", never as "no jobs".

**Disable, not delete.** There is no `drop-job` here and there should not be: a dropped Agent job
takes its schedule, steps and history with it and no confirmation prompt makes that recoverable.
Disabling is the reversible action, so it is the one a phone gets.

**Running jobs are left alone, and the report says so.** Disabling does not stop the execution
already in flight — Agent and DBMS_SCHEDULER both let the current run finish — so a report that
said only "disabled" would let an operator believe they had stopped the thing filling their disk.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from db_ops.common import confirm, db_catalog, sql_run
from db_ops.common.evidence import FAIL, OK, WARN, GateReport

__all__ = ["JobControlError", "disable_job"]

#: Deliberately narrow, and for the same reason as the one in ``sqlserver_emergency``: these names
#: reach SQL after quoting, and refusing anything that has no business being in a job name is
#: cheaper to be sure of than the quoting is. Oracle's ``DBMS_JOB:123`` synthetic id is allowed
#: because ``list-jobs`` is what produced it.
_SAFE_JOB_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .$#:-]{0,127}$")

_DBMS_JOB_PREFIX = "DBMS_JOB:"


class JobControlError(RuntimeError):
    """A user-facing failure: unknown target, unsupported engine, no such job."""


def _literal(value: str) -> str:
    """A SQL string literal. Doubling the quote is the whole escape in every engine here."""
    return "'" + str(value).replace("'", "''") + "'"


def disable_job(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Disable one scheduled job by name, after confirming what it is.

    Looks before it asks, like every operation in this layer: the confirmation banner names the
    job's current enabled state, its owner and which scheduler owns it, because a prompt that says
    "disable X?" when X is already disabled teaches the operator that the prompt is noise.

    An already-disabled job is reported **OK, not FAIL** — the desired state is the actual state,
    and a retry after a Telegram timeout should not look like an error.
    """
    if not isinstance(request, dict):
        raise JobControlError("request must be a JSON object.")
    job = str(request.get("job_name") or request.get("job") or "").strip()
    if not job:
        raise JobControlError("job_name is required.")
    if not _SAFE_JOB_NAME.match(job):
        raise JobControlError(
            f"job_name={job!r} is not a plain job name. Letters, digits, space and "
            "_ . $ # : - only, up to 128 characters."
        )

    lookup = dict(request)
    lookup["enabled_only"] = False
    try:
        listed = db_catalog.list_jobs(lookup)
    except db_catalog.DbCatalogError as exc:
        raise JobControlError(str(exc)) from exc

    target_id = str(listed["server_id"])
    db_type = str(listed["db_type"])
    report = GateReport("disable-job", target=f"{target_id}/{job}", echo=echo)
    report.note("target", {"server_id": target_id, "ip": listed.get("ip"),
                           "db_type": db_type, "job_name": job})

    match = next((row for row in listed["jobs"] if str(row.get("name") or "") == job), None)
    if match is None:
        report.add("job", FAIL,
                   f"no job named {job!r} on {target_id} ({db_type}). Nothing was changed. "
                   f"Run list-jobs to see the {listed['count']} job(s) that are there.")
        return report.to_dict(request.get("override") or ())

    source = str(match.get("source") or "")
    report.note("job", {"enabled": bool(match.get("enabled")), "owner": match.get("owner"),
                        "category": match.get("category"), "source": source})
    if not match.get("enabled"):
        # Not a failure: the desired state is already the actual state. Reported as OK and
        # non-blocking so a retry after a timeout does not look like an error.
        report.add("job", OK, f"{job!r} is already disabled on {target_id}. Nothing to do.",
                   blocking=False)
        return report.to_dict(request.get("override") or ())

    report.add("job", OK, f"{job!r} found, enabled, owner={match.get('owner') or 'unknown'}"
                          f"{', source=' + source if source else ''}")

    if request.get("dry_run"):
        report.add("dry-run", OK,
                   f"would disable {job!r} on {target_id} — nothing executed.")
        return report.to_dict(request.get("override") or ())

    if not confirm.authorize_operation(
            report, request, operation="disable-job", target_id=target_id,
            target_label=f"{target_id} — {db_type} job {job!r}",
            extra_effects=[f"{job!r} will not start on its schedule until it is re-enabled"]):
        return report.to_dict(request.get("override") or ())

    statement = _disable_statement(db_type, job, source)
    exec_request: dict[str, Any] = {
        "target": request.get("target") or target_id,
        "sql": statement,
        "commit": True,
        "credential_name": request.get("credential_name") or request.get("user_ref") or "",
        "data_dir": data_dir if data_dir is not None else request.get("data_dir"),
        "sql_access": request.get("sql_access"),
    }
    if db_type == "sqlserver":
        exec_request["database"] = "msdb"
    elif request.get("database"):
        exec_request["database"] = request["database"]
    if request.get("timeout_seconds"):
        exec_request["timeout_seconds"] = request["timeout_seconds"]

    try:
        sql_run.run_sql(exec_request)
    except sql_run.SqlRunError as exc:
        report.add("disable", FAIL, f"failed: {exc}")
        return report.to_dict(request.get("override") or ())

    report.add("disable", OK,
               f"{job!r} is disabled on {target_id}. It will not start on its schedule until it "
               "is re-enabled.", blocking=False)
    # Stated every time rather than only when a run is in flight: `list-jobs` reads the definition,
    # not the activity tables, so this module does not actually know whether one is running. An
    # unconditional, accurate sentence beats a conditional one built on a fact it does not have.
    report.add("running", WARN,
               "A run already in progress is NOT stopped by this — disabling only affects future "
               "schedules. Check the job's activity if something is running now.", blocking=False)
    return report.to_dict(request.get("override") or ())


def _disable_statement(db_type: str, job: str, source: str) -> str:
    """The one statement that switches this job off, for the engine that owns it."""
    if db_type == "sqlserver":
        return f"EXEC msdb.dbo.sp_update_job @job_name = {_literal(job)}, @enabled = 0;"
    if db_type == "oracle":
        if source == "dbms_job" or job.startswith(_DBMS_JOB_PREFIX):
            job_number = job[len(_DBMS_JOB_PREFIX):].strip()
            if not job_number.isdigit():
                raise JobControlError(
                    f"{job!r} looks like a DBMS_JOB entry but carries no numeric id.")
            # BROKEN is DBMS_JOB's only "do not run this" switch; there is no enabled flag.
            return f"BEGIN DBMS_JOB.BROKEN({job_number}, TRUE); END;"
        return f"BEGIN DBMS_SCHEDULER.DISABLE({_literal(job)}); END;"
    if db_type == "postgresql":
        return f"UPDATE cron.job SET active = false WHERE jobname = {_literal(job)};"
    raise JobControlError(
        f"disable-job does not know engine {db_type!r}; supported: oracle, postgresql, sqlserver.")
