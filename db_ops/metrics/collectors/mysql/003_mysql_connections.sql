SELECT
    CAST('server' AS CHAR(256)) AS metric_item,
    CAST(COUNT(*) AS CHAR(32)) AS metric_value,
    CAST('sessions' AS CHAR(32)) AS metric_unit,
    CASE
        WHEN SUM(CASE WHEN state LIKE '%lock%' THEN 1 ELSE 0 END) > 0 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    CONCAT(
        'active_sessions=', COUNT(*),
        ', running_threads=', SUM(CASE WHEN command <> 'Sleep' THEN 1 ELSE 0 END),
        ', lock_wait_sessions=', SUM(CASE WHEN state LIKE '%lock%' THEN 1 ELSE 0 END)
    ) AS message
FROM information_schema.processlist
WHERE id <> CONNECTION_ID();
