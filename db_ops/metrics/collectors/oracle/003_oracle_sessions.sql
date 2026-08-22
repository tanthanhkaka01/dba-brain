SELECT
    CAST('server' AS varchar2(256)) AS metric_item,
    TO_CHAR(COUNT(*)) AS metric_value,
    CAST('sessions' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN SUM(CASE WHEN blocking_session IS NOT NULL THEN 1 ELSE 0 END) > 0 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'active_sessions=' || TO_CHAR(SUM(CASE WHEN status = 'ACTIVE' THEN 1 ELSE 0 END)) ||
        ', user_sessions=' || TO_CHAR(COUNT(*)) ||
        ', blocked_sessions=' || TO_CHAR(SUM(CASE WHEN blocking_session IS NOT NULL THEN 1 ELSE 0 END)) AS message
FROM v$session
WHERE type = 'USER';
