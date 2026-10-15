-- BACKUP_AGE (Oracle variant) - hours since the newest datafile backup (level 0 or level 1).
--
-- Mirrors 005_sqlserver_backup_age.sql on purpose: metric_item is the database name, metric_value
-- is an age in hours, and the thresholds are the same 24h WARNING / 48h CRITICAL, so the
-- backup_health report ("role": "age") reads identically for both engines. SQL Server counts
-- msdb types D and I here; Oracle's equivalent is a datafile backup at any incremental level.
--
-- Reads v$backup_datafile rather than v$rman_backup_job_details for the same reason
-- 022_oracle_last_backup_result.sql does: the job view is per RMAN *run*, so a run that backed up
-- only archivelogs still counts as a recent "backup", and a controlfile autobackup - which fires
-- after every archivelog backup, every 19 minutes on this lab - would make the datafile age look
-- minutes old forever. `file# > 0` excludes the controlfile; what is left is the data itself.
--
-- A database that has never been backed up returns a row, not an empty result: "never backed up"
-- is the most important answer this metric can give.

SELECT
    CAST(d.name AS varchar2(256)) AS metric_item,
    CASE
        WHEN b.last_backup_time IS NULL THEN NULL
        ELSE TO_CHAR(ROUND((SYSDATE - b.last_backup_time) * 24, 1))
    END AS metric_value,
    CAST('hours' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN b.last_backup_time IS NULL THEN 'WARNING'
        WHEN (SYSDATE - b.last_backup_time) * 24 >= 48 THEN 'CRITICAL'
        WHEN (SYSDATE - b.last_backup_time) * 24 >= 24 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    CASE
        WHEN b.last_backup_time IS NULL THEN
            'No datafile backup found for database ' || d.name || '.'
        ELSE
            'Last full/incremental backup '
            || TO_CHAR(ROUND((SYSDATE - b.last_backup_time) * 24, 1))
            || ' hours ago for database ' || d.name
            || ', incremental_level=' || NVL(TO_CHAR(b.incremental_level), 'full')
            || ', finished=' || TO_CHAR(b.last_backup_time, 'YYYY-MM-DD HH24:MI:SS')
            || '.'
    END AS message
FROM v$database d
CROSS JOIN
(
    SELECT
        MAX(completion_time) AS last_backup_time,
        MAX(incremental_level) KEEP (DENSE_RANK LAST ORDER BY completion_time) AS incremental_level
    FROM v$backup_datafile
    WHERE file# > 0
) b;
