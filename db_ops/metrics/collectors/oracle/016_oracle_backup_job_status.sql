-- BACKUP_JOB_STATUS (Oracle variant) - recent backup job outcomes.
--
-- Counterpart of 016_sqlserver_backup_job_status.sql, which reads SQL Agent job history. Oracle has
-- two places a backup job can be driven from, and both are covered here:
--   * RMAN runs recorded in v$rman_backup_job_details (whatever launched them);
--   * DBMS_SCHEDULER jobs whose name looks backup/RMAN related, so a scheduler job that never
--     reached RMAN (bad credential, missing script) is still reported instead of appearing as
--     simply "no backup ran".
--
-- The metric is declared empty_result_is_ok, so a database with no backup activity in the window
-- returns no rows rather than a synthetic one - BACKUP_AGE and BACKUP_LAST_RESULT are the metrics
-- that assert a backup should exist.

SELECT
    CAST('rman_' || LOWER(REPLACE(j.input_type, ' ', '_')) AS varchar2(256)) AS metric_item,
    CAST(j.status AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    CASE
        WHEN j.status = 'COMPLETED' THEN 'OK'
        WHEN j.status LIKE 'COMPLETED%' THEN 'WARNING'
        WHEN j.status LIKE 'RUNNING%' THEN 'OK'
        ELSE 'CRITICAL'
    END AS status,
    'job_type=rman'
        || ', input_type=' || j.input_type
        || ', status=' || j.status
        || ', started=' || TO_CHAR(j.start_time, 'YYYY-MM-DD HH24:MI:SS')
        || ', finished=' || NVL(TO_CHAR(j.end_time, 'YYYY-MM-DD HH24:MI:SS'), 'running')
        || ', elapsed=' || NVL(j.time_taken_display, '') AS message
FROM v$rman_backup_job_details j
WHERE j.start_time > SYSDATE - 1

UNION ALL

SELECT
    CAST('scheduler_' || SUBSTR(r.job_name, 1, 200) AS varchar2(256)) AS metric_item,
    CAST(r.status AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    CASE WHEN r.status = 'SUCCEEDED' THEN 'OK' ELSE 'CRITICAL' END AS status,
    'job_type=scheduler'
        || ', job=' || r.owner || '.' || r.job_name
        || ', status=' || r.status
        || ', started=' || TO_CHAR(r.actual_start_date, 'YYYY-MM-DD HH24:MI:SS')
        || ', run_duration=' || NVL(TO_CHAR(r.run_duration), '')
        || ', error=' || TO_CHAR(r."ERROR#")
        || ', info=' || SUBSTR(REPLACE(REPLACE(NVL(r.additional_info, ''), CHR(10), ' '), CHR(13), ' '), 1, 200) AS message
FROM dba_scheduler_job_run_details r
WHERE r.actual_start_date > SYSTIMESTAMP - INTERVAL '1' DAY
  AND (UPPER(r.job_name) LIKE '%BACKUP%' OR UPPER(r.job_name) LIKE '%RMAN%');
