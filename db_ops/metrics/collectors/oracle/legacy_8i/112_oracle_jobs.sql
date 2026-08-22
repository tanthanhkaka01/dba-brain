-- Oracle 8i legacy variant - SQL_AGENT_JOB_INVENTORY. The instance's scheduled jobs, from
-- dba_jobs.
--
-- The metric code is SQL Server's, the question is not: "what does this instance run on a
-- schedule, and is it working" is the same question on every engine, and the catalog already maps
-- it per engine this way (LOG_FILE_SPACE reads the FRA on Oracle and the transaction log on SQL
-- Server). Sharing the code means the server page renders one Scheduled jobs section rather than
-- one per engine.
--
-- 8i has DBMS_JOB, not DBMS_SCHEDULER, so this is dba_jobs: `what` is the PL/SQL the job runs,
-- `interval` is the expression that computes the next run (there is no schedule object to
-- decode), and `failures` is the count of consecutive failures since the last success - Oracle
-- marks a job broken after 16 of them and then stops running it entirely.
--
-- metric_value is ENABLED/DISABLED to match the SQL Server variant's contract, since
-- reports/inventory_health.py reads that value by name. A broken job is the Oracle equivalent of
-- disabled: it exists, it is scheduled, and it is not running.
--
-- Status stays OK - logging only, like the SQL Server variant. A failing job is JOB_FAILED's
-- alert to raise, and 8i has no equivalent of that metric yet, so a failure count here is
-- deliberately visible in the report rather than silent.
SELECT
    'job_' || TO_CHAR(j.job) AS metric_item,
    DECODE(j.broken, 'Y', 'DISABLED', 'ENABLED') AS metric_value,
    'state' AS metric_unit,
    'OK' AS status,
    'enabled=' || DECODE(j.broken, 'Y', '0', '1') ||
        ', last_outcome=' || DECODE(j.failures, 0, 'SUCCEEDED',
                                    NULL, 'none',
                                    'FAILED') ||
        ', owner=' || j.log_user ||
        ', schema=' || j.schema_user ||
        ', schedule=' || NVL(SUBSTR(j.interval, 1, 100), 'once') ||
        ', next_run=' || NVL(TO_CHAR(j.next_date, 'YYYY-MM-DD HH24:MI:SS'), 'none') ||
        ', last_run=' || NVL(TO_CHAR(j.last_date, 'YYYY-MM-DD HH24:MI:SS'), 'never') ||
        ', consecutive_failures=' || TO_CHAR(NVL(j.failures, 0)) ||
        ', total_runtime_seconds=' || TO_CHAR(ROUND(NVL(j.total_time, 0))) ||
        ', broken=' || j.broken ||
        -- command last: PL/SQL is full of commas, and the report parses the keys
        -- before it by name up to the next known key.
        ', command=' || SUBSTR(j.what, 1, 200) AS message
FROM dba_jobs j
ORDER BY j.job;
