SELECT
    CAST(TO_CHAR(s.sid) || ',' || TO_CHAR(s.serial#) AS varchar2(256)) AS metric_item,
    TO_CHAR(s.seconds_in_wait) AS metric_value,
    CAST('seconds' AS varchar2(32)) AS metric_unit,
    CASE WHEN s.seconds_in_wait >= 300 THEN 'CRITICAL' ELSE 'WARNING' END AS status,
    'blocked session sid=' || TO_CHAR(s.sid) ||
        ', blocking_session=' || TO_CHAR(s.blocking_session) ||
        ', wait_class=' || s.wait_class ||
        ', seconds_in_wait=' || TO_CHAR(s.seconds_in_wait) AS message
FROM v$session s
WHERE s.blocking_session IS NOT NULL
ORDER BY s.seconds_in_wait DESC;
