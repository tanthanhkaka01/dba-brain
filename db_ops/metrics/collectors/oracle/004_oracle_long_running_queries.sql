SELECT
    CAST(TO_CHAR(s.sid) || ',' || TO_CHAR(s.serial#) AS varchar2(256)) AS metric_item,
    TO_CHAR(s.last_call_et) AS metric_value,
    CAST('seconds' AS varchar2(32)) AS metric_unit,
    CASE WHEN s.last_call_et >= 1800 THEN 'CRITICAL' ELSE 'WARNING' END AS status,
    'sid=' || TO_CHAR(s.sid) ||
        ', serial=' || TO_CHAR(s.serial#) ||
        ', username=' || NVL(s.username, '') ||
        ', status=' || s.status ||
        ', seconds=' || TO_CHAR(s.last_call_et) AS message
FROM v$session s
WHERE s.type = 'USER'
  AND s.status = 'ACTIVE'
  AND s.last_call_et >= 300
ORDER BY s.last_call_et DESC;
