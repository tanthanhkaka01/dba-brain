"""What an instance runs on a schedule, on the page that is supposed to say so.

`server-metrics.html` could report a server perfectly healthy while the nightly job that produces
its backups had been failing for a week: a job is not a time series, so the chart grid drops the
metric entirely, and the only surviving trace was a `disabled_jobs` list on the fleet inventory.
The section this file covers answers the question an operator actually arrives with — what runs
here, what does it execute, when, and has it been working.

The parsing is the part that is easy to get wrong, and none of it is incidental. A job's command
is full of commas and equals signs (`EXEC dbo.p @a = 1, @b = 2`), and the shared message parser
ends a value at the first comma — which would render a job as running something it does not run.
So the fields are read up to the next *known key* instead, and `command` is written last by every
engine's variant precisely so that nothing follows it to be lost.

The engines also disagree about what a run counter means: SQL Server's msdb history gives a
7-day window, Oracle's scheduler keeps lifetime counters. Both are shown, each labelled with the
window it covers, because averaging them into one number would invent a comparison the data does
not support.
"""

from db_ops.reports.server_report import build_jobs


def _job(name, message, value="ENABLED"):
    return {"metric_code": "SQL_AGENT_JOB_INVENTORY", "metric_item": name,
            "metric_value": value, "status": "OK", "message": message}


def test_a_command_containing_commas_is_not_cut_at_the_first_one():
    section = build_jobs([_job(
        "Nightly backup",
        "enabled=1, last_outcome=SUCCEEDED, steps=2, schedule=daily at 02:00:00, "
        "next_run=20260814 02:00:00, last_run=20260813 02:00:00, runs_7d=7, succeeded_7d=7, "
        "failed_7d=0, command=EXEC dbo.backup_all @full = 1, @verify = 1, @retention_days = 14",
    )])

    job = section["jobs"][0]
    assert job["command"] == "EXEC dbo.backup_all @full = 1, @verify = 1, @retention_days = 14"
    assert job["schedule"] == "daily at 02:00:00"
    assert job["runs"] == 7 and job["failed"] == 0


def test_a_disabled_job_is_counted_but_not_listed_as_something_that_runs():
    section = build_jobs([
        _job("Live", "enabled=1, last_outcome=SUCCEEDED, runs_7d=3, failed_7d=0, command=x"),
        _job("Old", "enabled=0, last_outcome=none, command=x", value="DISABLED"),
    ])

    assert [job["name"] for job in section["jobs"]] == ["Live"]
    assert section["summary"] == {**section["summary"], "enabled": 1, "disabled": 1}


def test_the_failing_jobs_come_first_because_that_is_why_the_table_is_read():
    section = build_jobs([
        _job("Aaa healthy", "enabled=1, last_outcome=SUCCEEDED, runs_7d=7, failed_7d=0, command=x"),
        _job("Zzz broken", "enabled=1, last_outcome=FAILED, runs_7d=7, failed_7d=4, command=x"),
    ])

    assert [job["name"] for job in section["jobs"]] == ["Zzz broken", "Aaa healthy"]
    assert section["summary"]["failing"] == 1


def test_oracles_lifetime_counters_are_labelled_as_lifetime_not_as_a_week():
    """dba_scheduler_jobs keeps run_count/failure_count for the life of the job. Reporting them
    under the SQL Server column heading would claim a 7-day failure rate that was never measured."""
    section = build_jobs([_job(
        "LTR.REBUILD_STATS",
        "enabled=1, last_outcome=SCHEDULED, owner=LTR, schedule=FREQ=DAILY;BYHOUR=1, "
        "runs_total=412, failed_total=3, command=BEGIN ltr.rebuild_stats; END;",
    )])

    job = section["jobs"][0]
    assert (job["window"], job["runs"], job["failed"]) == ("total", 412, 3)
    assert job["succeeded"] == 409


def test_an_8i_job_carries_its_consecutive_failure_count_because_oracle_breaks_it_at_16():
    section = build_jobs([_job(
        "job_21",
        "enabled=1, last_outcome=FAILED, owner=LTR, schedule=SYSDATE + 1/24, "
        "consecutive_failures=9, broken=N, command=BEGIN ltr.pp_process_date; END;",
    )])

    assert section["jobs"][0]["consecutiveFailures"] == 9


def test_a_multi_line_plsql_block_survives_as_the_jobs_command():
    """An Oracle DBMS_JOB stores `what` as the PL/SQL block it was submitted with, newlines and
    all. A pattern that stops at the end of a line does not merely truncate the value — the match
    fails and the field goes missing, which is how the first real 8i job rendered with no command
    at all."""
    section = build_jobs([_job(
        "job_21",
        "enabled=1, last_outcome=SUCCEEDED, owner=SYS, schedule=SYSDATE + 5/(24*60), "
        "command=\nDECLARE\n  vcount NUMBER(5) := 0;\nBEGIN\n  ltr.kill_idle(vcount);\nEND;",
    )])

    assert section["jobs"][0]["command"] == (
        "DECLARE vcount NUMBER(5) := 0; BEGIN ltr.kill_idle(vcount); END;"
    )


def test_each_engines_duration_number_is_carried_under_its_own_name():
    """Three engines, three incompatible answers to "how long does this take": SQL Server's worst
    run of the last 7 days in seconds, Oracle 10g+'s last run as an INTERVAL, 8i's lifetime total.
    Folding them into one field would put a lifetime total in a column headed "longest, 7d"."""
    section = build_jobs([
        _job("Backup_Full", "enabled=1, last_outcome=SUCCEEDED, max_duration_seconds_7d=734, "
                            "runs_7d=7, failed_7d=0, command=x"),
        _job("LTR.STATS", "enabled=1, last_outcome=SCHEDULED, "
                          "last_duration=+000000000 00:05:12.000000, runs_total=9, "
                          "failed_total=0, command=x"),
        _job("job_21", "enabled=1, last_outcome=SUCCEEDED, total_runtime_seconds=8140, command=x"),
    ])
    by_name = {job["name"]: job for job in section["jobs"]}
    sqlserver, scheduler, legacy = by_name["Backup_Full"], by_name["LTR.STATS"], by_name["job_21"]

    assert sqlserver["maxDurationSeconds"] == 734
    assert scheduler["lastDuration"] == "+000000000 00:05:12.000000"
    assert legacy["totalRuntimeSeconds"] == 8140
    # A job that reported no duration at all must stay empty rather than borrow another's field.
    assert (sqlserver["lastDuration"], sqlserver["totalRuntimeSeconds"]) == ("", None)


def test_a_job_reporting_no_duration_at_all_says_nothing_rather_than_zero():
    """SQL Server writes `max_duration_seconds_7d=n/a` for a job with no history in the window.
    Reading that as 0 would show the estate's never-run jobs as its fastest ones."""
    job = build_jobs([_job(
        "OptimizeIndex_Weekly",
        "enabled=1, last_outcome=FAILED, max_duration_seconds_7d=n/a, runs_7d=0, failed_7d=0, "
        "command=EXEC dbo.IndexOptimize @Databases = 'USER_DATABASES'",
    )])["jobs"][0]

    assert job["maxDurationSeconds"] is None


def test_the_schema_a_job_runs_as_is_kept_apart_from_the_user_that_submitted_it():
    """8i's dba_jobs has both: log_user submitted the job, schema_user is what its PL/SQL resolves
    against. When they disagree, that disagreement is the reason a job stops finding its tables —
    so the two are separate fields, not one "owner"."""
    job = build_jobs([_job(
        "job_21",
        "enabled=1, last_outcome=SUCCEEDED, owner=OPS, schema=LTR, restartable=TRUE, "
        "command=BEGIN ltr.pp_process_date; END;",
    )])["jobs"][0]

    assert (job["owner"], job["schema"]) == ("OPS", "LTR")
    assert job["restartable"] is True


def test_a_job_that_is_not_restartable_is_not_flagged_as_one():
    """`restartable` only means something when it is on: a retried job's failure count does not
    mean what the same number means on a job that is not retried. FALSE is the ordinary case and
    must not put a badge on every Oracle row."""
    job = build_jobs([_job("j", "enabled=1, last_outcome=SUCCEEDED, restartable=FALSE, command=x")])

    assert job["jobs"][0]["restartable"] is False


def test_a_collection_that_failed_outright_contributes_no_job_rows():
    """A failed collector is stored with no metric_item. Reading it as a job would put a nameless
    row in the table and count it as something the instance runs."""
    section = build_jobs([_job("", "Oracle bridge did not answer", value="")])

    assert section["jobs"] == []
    assert section["summary"]["enabled"] == 0
