WITH job_history AS
(
    SELECT
        h.job_id,
        h.step_id,
        h.run_status,
        run_datetime =
            CONVERT(datetime,
                STUFF(STUFF(CAST(h.run_date AS char(8)), 5, 0, '-'), 8, 0, '-')
                + ' '
                + STUFF(STUFF(RIGHT('000000' + CAST(h.run_time AS varchar(6)), 6), 3, 0, ':'), 6, 0, ':')
            )
    FROM msdb.dbo.sysjobhistory AS h
    WHERE h.run_date > 0
),
backup_jobs AS
(
    SELECT
        j.job_id,
        j.name COLLATE DATABASE_DEFAULT AS job_name
    FROM msdb.dbo.sysjobs AS j
    WHERE
        (
            j.name LIKE '%backup%'
            OR j.name LIKE '%DatabaseBackup%'
            OR j.name LIKE '%FULL%'
            OR j.name LIKE '%DIFF%'
            OR j.name LIKE '%LOG%'
        )
        AND j.enabled = 1
        AND j.name NOT LIKE '%CommandLog Cleanup%'
        AND j.name NOT LIKE '%sp_delete_backuphistory%'
        AND j.name NOT LIKE '%delete_backuphistory%'
        AND j.name NOT LIKE '%Cleanup%'
        AND j.name NOT LIKE '%Clean up%'
        AND j.name NOT LIKE '%Shrink%'
        AND j.name NOT LIKE '%DBLog%'
        AND j.name NOT LIKE '%Reorganize%'
        AND j.name NOT LIKE '%Rebuild%'
        AND j.name NOT LIKE '%Index%'
        AND j.name NOT LIKE '%Statistics%'
        AND j.name NOT LIKE '%Maintenance Cleanup%'
),
latest_run AS
(
    SELECT
        bj.job_name,
        h.run_status,
        h.run_datetime,
        ROW_NUMBER() OVER
        (
            PARTITION BY bj.job_id
            ORDER BY h.run_datetime DESC
        ) AS rn
    FROM backup_jobs AS bj
    LEFT JOIN job_history AS h
        ON h.job_id = bj.job_id
       AND h.step_id = 0
)
SELECT
    CAST(job_name AS varchar(256)) AS metric_item,
    CAST(
        CASE run_status
            WHEN 0 THEN 'FAILED'
            WHEN 1 THEN 'SUCCEEDED'
            WHEN 2 THEN 'RETRY'
            WHEN 3 THEN 'CANCELED'
            WHEN 4 THEN 'IN_PROGRESS'
            ELSE 'NO_HISTORY'
        END AS varchar(64)
    ) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CASE
        WHEN run_status = 1 THEN 'OK'
        WHEN run_status IS NULL THEN 'LOGGING'
        ELSE 'CRITICAL'
    END AS status,
    'backup_job=' + job_name
        + ', last_status='
        + CASE run_status
            WHEN 0 THEN 'FAILED'
            WHEN 1 THEN 'SUCCEEDED'
            WHEN 2 THEN 'RETRY'
            WHEN 3 THEN 'CANCELED'
            WHEN 4 THEN 'IN_PROGRESS'
            ELSE 'NO_HISTORY'
          END
        + ', last_run_datetime=' + COALESCE(CONVERT(varchar(19), run_datetime, 120), 'NULL') AS message
FROM latest_run
WHERE rn = 1

UNION ALL

SELECT
    CAST('backup_job' AS varchar(256)) AS metric_item,
    CAST('NOT_FOUND' AS varchar(64)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    'LOGGING' AS status,
    'No enabled SQL Agent backup job found by name pattern.' AS message
WHERE NOT EXISTS
(
    SELECT 1 FROM backup_jobs
);