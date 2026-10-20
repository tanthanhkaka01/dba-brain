-- JOB_FAILED (Oracle 10g+): scheduler jobs that failed in the last 24 hours, graded by fail rate.
--
-- SQL_AGENT_JOB_INVENTORY lists the Oracle scheduler's jobs at OK, by design - an inventory - so a
-- DBMS_SCHEDULER job that failed every run (an ETL load, a materialized view refresh, a partition
-- maintenance job) paged nobody: JOB_FAILED was SQL Server's only. Item 1.149's first Oracle port
-- (2026-10-15). This is SQL Server's rule, ported as it is, so the two engines mean the same thing:
-- a fail rate over the 24 h of 70 % or more CRITICAL, 50 % WARNING, any failure LOGGING.
--
-- Read from dba_scheduler_job_run_details, one row per run with its outcome. FAILED is a failure;
-- STOPPED (a run killed or stopped by hand) is counted in the message and not graded, as SQL
-- Server's canceled runs are not. The owners left out are the ones SQL_AGENT_JOB_INVENTORY's Oracle
-- variant leaves out - jobs Oracle itself runs (statistics, advisors, purges), which no DBA scheduled.
-- Inside a PDB the view shows that container's jobs.
--
-- A job the DBA has since disabled is left out, as SQL Server leaves out a disabled Agent job: its
-- failures are no longer anyone's to act on (review of 1.149, 2026-10-16 - the port had missed it).
-- Only a job that still exists and is disabled: a one-off job created with auto_drop is gone from
-- dba_scheduler_jobs once it has run, and its failure is exactly the one-off load somebody needs to
-- hear about.
WITH params AS (
    SELECT 24 AS window_hours, 70 AS critical_fail_pct, 50 AS warning_fail_pct FROM dual
),
runs AS (
    SELECT
        r.owner || '.' || r.job_name AS job_name,
        COUNT(*) AS total_runs,
        SUM(CASE WHEN r.status = 'FAILED' THEN 1 ELSE 0 END) AS failed_runs,
        SUM(CASE WHEN r.status = 'STOPPED' THEN 1 ELSE 0 END) AS stopped_runs,
        MAX(CASE WHEN r.status = 'FAILED' THEN r.log_date END) AS last_failed,
        MAX(CASE WHEN r.status = 'FAILED' THEN r.error# END) KEEP (DENSE_RANK LAST ORDER BY
            CASE WHEN r.status = 'FAILED' THEN r.log_date END NULLS FIRST) AS last_error
    FROM dba_scheduler_job_run_details r
    CROSS JOIN params p
    WHERE r.log_date >= SYSTIMESTAMP - NUMTODSINTERVAL(p.window_hours, 'HOUR')
      AND r.owner NOT IN ('SYS', 'SYSTEM', 'ORACLE_OCM', 'EXFSYS', 'WMSYS', 'DBSNMP', 'APPQOSSYS', 'CTXSYS')
      AND NOT EXISTS (
          SELECT 1 FROM dba_scheduler_jobs j
          WHERE j.owner = r.owner AND j.job_name = r.job_name AND j.enabled = 'FALSE'
      )
    GROUP BY r.owner || '.' || r.job_name
),
failed AS (
    SELECT
        job_name, total_runs, failed_runs, stopped_runs, last_failed, last_error,
        ROUND(failed_runs * 100 / NULLIF(total_runs, 0), 2) AS fail_rate_percent
    FROM runs
    WHERE failed_runs > 0
)
SELECT
    CAST(f.job_name AS varchar2(256)) AS metric_item,
    TO_CHAR(f.fail_rate_percent) AS metric_value,
    CAST('fail_rate_percent_24h' AS varchar2(64)) AS metric_unit,
    CASE
        WHEN f.fail_rate_percent >= p.critical_fail_pct THEN 'CRITICAL'
        WHEN f.fail_rate_percent >= p.warning_fail_pct THEN 'WARNING'
        ELSE 'LOGGING'
    END AS status,
    'job=' || f.job_name
        || ', failed_runs_24h=' || f.failed_runs
        || ', total_runs_24h=' || f.total_runs
        || ', fail_rate_percent=' || TO_CHAR(f.fail_rate_percent)
        || ', stopped_runs_24h=' || f.stopped_runs
        || ', last_failed_datetime=' || NVL(TO_CHAR(f.last_failed, 'YYYY-MM-DD HH24:MI:SS'), 'NULL')
        || ', last_error=ORA-' || NVL(TO_CHAR(f.last_error), 'n/a') AS message
FROM failed f
CROSS JOIN params p
UNION ALL
SELECT
    CAST('scheduler' AS varchar2(256)),
    '0',
    CAST('fail_rate_percent_24h' AS varchar2(64)),
    'OK',
    'No scheduler job failures in the last 24 hours (owners Oracle maintains left out).'
FROM dual
WHERE NOT EXISTS (SELECT 1 FROM failed)
