-- Oracle 8i legacy variant - QUERY_LONG_RUNNING. 8i has no v$sql_monitor; approximate with
-- long-active sessions (high last_call_et while ACTIVE). Empty result = nothing long-running.
SELECT * FROM (
    SELECT
        TO_CHAR(s.sid) || ',' || TO_CHAR(s.serial#) AS metric_item,
        TO_CHAR(s.last_call_et) AS metric_value,
        'seconds' AS metric_unit,
        CASE WHEN s.last_call_et > 3600 THEN 'WARNING' ELSE 'OK' END AS status,
        'user=' || s.username || ', machine=' || s.machine ||
            ', program=' || s.program || ', active_seconds=' || s.last_call_et AS message
    FROM v$session s
    WHERE s.username IS NOT NULL
      AND s.status = 'ACTIVE'
      AND s.last_call_et > 60
    ORDER BY s.last_call_et DESC
)
WHERE ROWNUM <= 20;
