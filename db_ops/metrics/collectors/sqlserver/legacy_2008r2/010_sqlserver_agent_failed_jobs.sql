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
job_runs AS
(
    SELECT
        j.name COLLATE DATABASE_DEFAULT AS job_name,
        COUNT(*) AS total_runs_24h,
        SUM(CASE WHEN h.run_status = 0 THEN 1 ELSE 0 END) AS failed_runs_24h,
        MAX(CASE WHEN h.run_status = 0 THEN h.run_datetime END) AS last_failed_datetime
    FROM job_history AS h
    JOIN msdb.dbo.sysjobs AS j
        ON j.job_id = h.job_id
    WHERE h.step_id = 0
      AND h.run_datetime >= DATEADD(hour, -24, GETDATE())
      AND j.enabled = 1
      AND j.name NOT LIKE '%CommandLog Cleanup%'
      AND j.name NOT LIKE '%sp_delete_backuphistory%'
      AND j.name NOT LIKE '%delete_backuphistory%'
      AND j.name NOT LIKE '%Cleanup%'
      AND j.name NOT LIKE '%Clean up%'
      AND j.name NOT LIKE '%Shrink%'
      AND j.name NOT LIKE '%DBLog%'
    GROUP BY j.name COLLATE DATABASE_DEFAULT
),
failed_jobs AS
(
    SELECT
        job_name,
        total_runs_24h,
        failed_runs_24h,
        CAST(failed_runs_24h * 100.0 / NULLIF(total_runs_24h, 0) AS decimal(10, 2)) AS fail_rate_percent,
        last_failed_datetime
    FROM job_runs
    WHERE failed_runs_24h > 0
)
SELECT
    CAST(job_name AS varchar(256)) AS metric_item,
    CAST(fail_rate_percent AS varchar(32)) AS metric_value,
    CAST('fail_rate_percent_24h' AS varchar(64)) AS metric_unit,
    CASE
        WHEN fail_rate_percent >= 70 THEN 'CRITICAL'
        WHEN fail_rate_percent >= 50 THEN 'WARNING'
        WHEN failed_runs_24h >= 1 THEN 'LOGGING'
        ELSE 'OK'
    END AS status,
    'job=' + CAST(job_name AS varchar(256))
    + ', failed_runs_24h=' + CAST(failed_runs_24h AS varchar(32))
    + ', total_runs_24h=' + CAST(total_runs_24h AS varchar(32))
    + ', fail_rate_percent=' + CAST(fail_rate_percent AS varchar(32))
    + ', last_failed_datetime='
    + ISNULL(CONVERT(varchar(19), last_failed_datetime, 120), 'NULL') AS message
FROM failed_jobs

UNION ALL

SELECT
    CAST('sql_agent' AS varchar(256)) AS metric_item,
    CAST('0' AS varchar(32)) AS metric_value,
    CAST('fail_rate_percent_24h' AS varchar(64)) AS metric_unit,
    'OK' AS status,
    'No enabled SQL Agent job failures in last 24 hours.' AS message
WHERE NOT EXISTS
(
    SELECT 1
    FROM failed_jobs
);