SELECT
    CAST('server' AS varchar(256)) AS metric_item,
    CAST(COUNT(DISTINCT s.session_id) AS varchar(32)) AS metric_value,
    CAST('sessions' AS varchar(32)) AS metric_unit,
    CASE
        WHEN COALESCE(SUM(CASE WHEN r.blocking_session_id <> 0 THEN 1 ELSE 0 END), 0) > 0 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    CONCAT(
        'active_sessions=', COUNT(DISTINCT s.session_id),
        ', running_requests=', COUNT(DISTINCT r.session_id),
        ', blocked_sessions=', COALESCE(SUM(CASE WHEN r.blocking_session_id <> 0 THEN 1 ELSE 0 END), 0)
    ) AS message
FROM sys.dm_exec_sessions AS s
LEFT JOIN sys.dm_exec_requests AS r
    ON r.session_id = s.session_id
WHERE s.is_user_process = 1;