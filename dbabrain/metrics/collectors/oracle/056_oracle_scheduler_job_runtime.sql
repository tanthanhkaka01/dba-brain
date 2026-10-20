-- SQL_AGENT_JOB_RUNTIME (Oracle 10g+): scheduler jobs running now, and each enabled job's next run
-- (1.149's port, 2026-10-16).
--
-- SQL Server's variant (056) is an inventory of the moment and grades nothing: a running job with its
-- elapsed seconds, an enabled job with its next run, OK. Ported as it is, so the two engines mean the
-- same thing: dba_scheduler_running_jobs for what runs, dba_scheduler_jobs for what is next.
--
-- The one thing graded is Oracle's own limit, not a number of ours: a job may declare
-- max_run_duration, and Oracle raises JOB_OVER_MAX_DUR when a run passes it - a running job past its
-- own declared limit is WARNING. A job with no limit stays OK however long it runs; a statement in it
-- that runs long is QUERY_LONG_RUNNING's to grade, on Oracle as on SQL Server.
--
-- The owners Oracle maintains are left out, as SQL_AGENT_JOB_INVENTORY and JOB_FAILED leave them out.
WITH running AS (
    SELECT r.owner || '.' || r.job_name AS job_name,
           EXTRACT(DAY FROM r.elapsed_time) * 86400
             + EXTRACT(HOUR FROM r.elapsed_time) * 3600
             + EXTRACT(MINUTE FROM r.elapsed_time) * 60
             + FLOOR(EXTRACT(SECOND FROM r.elapsed_time)) AS elapsed_seconds,
           EXTRACT(DAY FROM j.max_run_duration) * 86400
             + EXTRACT(HOUR FROM j.max_run_duration) * 3600
             + EXTRACT(MINUTE FROM j.max_run_duration) * 60
             + FLOOR(EXTRACT(SECOND FROM j.max_run_duration)) AS max_run_seconds,
           CASE WHEN j.max_run_duration IS NOT NULL AND r.elapsed_time > j.max_run_duration
                THEN 1 ELSE 0 END AS over_limit
    FROM dba_scheduler_running_jobs r
    LEFT JOIN dba_scheduler_jobs j
           ON j.owner = r.owner AND j.job_name = r.job_name
    WHERE r.owner NOT IN ('SYS', 'SYSTEM', 'ORACLE_OCM', 'EXFSYS', 'WMSYS', 'DBSNMP', 'APPQOSSYS', 'CTXSYS')
),
next_runs AS (
    SELECT j.owner || '.' || j.job_name AS job_name, j.next_run_date
    FROM dba_scheduler_jobs j
    WHERE j.enabled = 'TRUE'
      AND j.next_run_date IS NOT NULL
      AND j.owner NOT IN ('SYS', 'SYSTEM', 'ORACLE_OCM', 'EXFSYS', 'WMSYS', 'DBSNMP', 'APPQOSSYS', 'CTXSYS')
),
metric_data AS (
    SELECT CAST(r.job_name AS varchar2(256)) AS metric_item,
           CAST('RUNNING ' || TO_CHAR(r.elapsed_seconds) || 's' AS varchar2(64)) AS metric_value,
           CAST('seconds' AS varchar2(32)) AS metric_unit,
           CASE WHEN r.over_limit = 1 THEN 'WARNING' ELSE 'OK' END AS status,
           'Currently executing; elapsed_seconds=' || TO_CHAR(r.elapsed_seconds)
               || ', max_run_seconds=' || NVL(TO_CHAR(r.max_run_seconds), 'none')
               || CASE WHEN r.over_limit = 1 THEN ', past the limit the job itself declares' ELSE '' END
               AS message
    FROM running r
    UNION ALL
    SELECT CAST(n.job_name AS varchar2(256)),
           CAST('NEXT ' || TO_CHAR(n.next_run_date, 'YYYY-MM-DD HH24:MI:SS') AS varchar2(64)),
           CAST(NULL AS varchar2(32)),
           'OK',
           'Next scheduled run for an enabled job'
    FROM next_runs n
)
SELECT metric_item, metric_value, metric_unit, status, message
FROM metric_data
UNION ALL
SELECT CAST('Scheduler Jobs' AS varchar2(256)),
       CAST('NO RUNNING OR SCHEDULED JOBS' AS varchar2(64)),
       CAST(NULL AS varchar2(32)),
       'OK',
       'No currently running jobs and no enabled jobs with a next scheduled run (owners Oracle maintains left out)'
FROM dual
WHERE NOT EXISTS (SELECT 1 FROM metric_data)
