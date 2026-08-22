-- Oracle 8i legacy variant - INSTANCE_CONNECTIONS. Session and process counts.
-- The 8.1.7 Forms-freeze incident showed inactive sessions piling up, so report both
-- the live counts and the inactive total.
SELECT metric_item, metric_value, metric_unit, status, message FROM (
    SELECT 'session_count' AS metric_item,
           TO_CHAR(COUNT(*)) AS metric_value,
           'sessions' AS metric_unit,
           'OK' AS status,
           'total sessions=' || COUNT(*) AS message
    FROM v$session
    UNION ALL
    SELECT 'process_count',
           TO_CHAR(COUNT(*)),
           'processes',
           'OK',
           'total processes=' || COUNT(*)
    FROM v$process
    UNION ALL
    SELECT 'inactive_session_count',
           TO_CHAR(COUNT(*)),
           'sessions',
           'OK',
           'inactive user sessions=' || COUNT(*)
    FROM v$session
    WHERE username IS NOT NULL AND status = 'INACTIVE'
);
