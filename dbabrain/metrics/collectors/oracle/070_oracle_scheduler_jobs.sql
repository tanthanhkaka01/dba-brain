-- SQL_AGENT_JOB_INVENTORY (Oracle 10g+) - the instance's scheduled jobs, from
-- dba_scheduler_jobs.
--
-- The metric code is SQL Server's and the question is not; see the 8i variant
-- (legacy_8i/112_oracle_jobs.sql) for why one code covers both engines. This is the
-- DBMS_SCHEDULER half: it has a real schedule object, so repeat_interval and the run counters
-- come straight out of the view instead of being derived.
--
-- metric_value is ENABLED/DISABLED to match the SQL Server variant's contract, which
-- reports/inventory_health.py reads by name. run_count/failure_count on this view are the job's
-- own lifetime counters, so they are labelled `_total` rather than `_7d` - unlike msdb's
-- sysjobhistory they are not a window, and calling them one would be a lie a reader cannot see.
--
-- Oracle-maintained jobs are excluded: an instance carries dozens of them (statistics gathering,
-- space advisors, auto-purge), none of which the DBA scheduled or is being asked about here.
--
-- Status stays OK - logging only, like the SQL Server variant.
SELECT
    CAST(owner || '.' || job_name AS varchar2(400)) AS metric_item,
    CAST(DECODE(enabled, 'TRUE', 'ENABLED', 'DISABLED') AS varchar2(32)) AS metric_value,
    CAST('state' AS varchar2(32)) AS metric_unit,
    CAST('OK' AS varchar2(16)) AS status,
    CAST('enabled=' || DECODE(enabled, 'TRUE', '1', '0') ||
        ', last_outcome=' || NVL(state, 'UNKNOWN') ||
        ', owner=' || owner ||
        ', job_class=' || NVL(job_class, 'none') ||
        ', schedule=' || NVL(SUBSTR(NVL(repeat_interval, schedule_name), 1, 150), 'once') ||
        ', next_run=' || NVL(TO_CHAR(next_run_date, 'YYYY-MM-DD HH24:MI:SS'), 'none') ||
        ', last_run=' || NVL(TO_CHAR(last_start_date, 'YYYY-MM-DD HH24:MI:SS'), 'never') ||
        ', last_duration=' || NVL(TO_CHAR(last_run_duration), 'n/a') ||
        ', runs_total=' || TO_CHAR(NVL(run_count, 0)) ||
        ', failed_total=' || TO_CHAR(NVL(failure_count, 0)) ||
        ', restartable=' || NVL(restartable, 'FALSE') ||
        -- command last: a job action is full of commas, and the report parses the
        -- keys before it by name up to the next known key.
        ', command=' || SUBSTR(NVL(job_action, program_name), 1, 200) AS varchar2(4000)) AS message
FROM dba_scheduler_jobs
WHERE owner NOT IN ('SYS', 'SYSTEM', 'ORACLE_OCM', 'EXFSYS', 'WMSYS', 'DBSNMP', 'APPQOSSYS', 'CTXSYS')
ORDER BY owner, job_name;
