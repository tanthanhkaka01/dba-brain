SELECT
    CAST(COALESCE(DB_NAME(r.database_id), 'server') COLLATE DATABASE_DEFAULT AS varchar(256)) AS metric_item,
    CAST(COUNT(*) AS varchar(32)) AS metric_value,
    CAST('blocked_sessions' AS varchar(32)) AS metric_unit,
    CASE
        WHEN COUNT(*) >= 10 THEN 'CRITICAL'
        WHEN MAX(CAST(r.wait_time AS bigint) / 1000) >= 300 THEN 'CRITICAL'
        WHEN COUNT(*) > 0 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'database=' + COALESCE(DB_NAME(r.database_id), 'server') COLLATE DATABASE_DEFAULT
        + ', blocked_sessions=' + CAST(COUNT(*) AS varchar(32))
        + ', max_wait_seconds=' + CAST(MAX(CAST(r.wait_time AS bigint) / 1000) AS varchar(32)) AS message
FROM sys.dm_exec_requests AS r
JOIN sys.dm_exec_sessions AS s
    ON r.session_id = s.session_id
WHERE r.blocking_session_id <> 0
  AND s.is_user_process = 1
GROUP BY COALESCE(DB_NAME(r.database_id), 'server') COLLATE DATABASE_DEFAULT

UNION ALL

SELECT
    CAST('server' AS varchar(256)) AS metric_item,
    CAST('0' AS varchar(32)) AS metric_value,
    CAST('blocked_sessions' AS varchar(32)) AS metric_unit,
    'OK' AS status,
    'No blocking sessions found.' AS message
WHERE NOT EXISTS
(
    SELECT 1
    FROM sys.dm_exec_requests AS r
    JOIN sys.dm_exec_sessions AS s
        ON r.session_id = s.session_id
    WHERE r.blocking_session_id <> 0
      AND s.is_user_process = 1
);
