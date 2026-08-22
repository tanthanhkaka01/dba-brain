IF OBJECT_ID('tempdb..#logspace') IS NOT NULL
    DROP TABLE #logspace;

CREATE TABLE #logspace
(
    database_name sysname,
    log_size_mb decimal(19,2),
    log_used_pct decimal(10,2),
    status_code int
);

INSERT INTO #logspace
EXEC('DBCC SQLPERF(LOGSPACE)');

SELECT
    CAST(database_name COLLATE DATABASE_DEFAULT AS varchar(256)) AS metric_item,
    CAST(log_used_pct AS varchar(32)) AS metric_value,
    CAST('pct' AS varchar(32)) AS metric_unit,
    CASE
        WHEN log_used_pct >= 95 AND log_size_mb >= 1024 THEN 'LOGGING'
        WHEN log_used_pct >= 85 AND log_size_mb >= 512 THEN 'LOGGING'
        WHEN log_used_pct >= 95 THEN 'LOGGING'
        ELSE 'OK'
    END AS status,
    'database=' + database_name COLLATE DATABASE_DEFAULT
        + ', log_used_pct=' + CAST(log_used_pct AS varchar(32))
        + ', log_size_mb=' + CAST(log_size_mb AS varchar(32)) AS message
FROM #logspace
WHERE database_name IN
(
    SELECT name
    FROM sys.databases
    WHERE database_id > 4
      AND state = 0
      AND is_read_only = 0
)
ORDER BY log_used_pct DESC;
