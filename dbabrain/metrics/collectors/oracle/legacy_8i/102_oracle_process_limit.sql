-- Oracle 8i incident metric - PROCESS_LIMIT. processes/sessions utilization vs configured
-- limit (v$resource_limit). Directly guards the 2026-05-04 ORA-12500 failure (connections
-- climbing toward the processes limit; the 1.5GB/600-process file failed above ~217).
SELECT
    resource_name AS metric_item,
    TO_CHAR(current_utilization) AS metric_value,
    'count' AS metric_unit,
    CASE
        WHEN limit_value = 'UNLIMITED' THEN 'OK'
        WHEN TO_NUMBER(limit_value) <= 0 THEN 'OK'
        WHEN current_utilization * 100 / TO_NUMBER(limit_value) >= 90 THEN 'CRITICAL'
        WHEN current_utilization * 100 / TO_NUMBER(limit_value) >= 80 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'resource=' || resource_name || ', current=' || current_utilization ||
        ', max_seen=' || max_utilization || ', limit=' || limit_value AS message
FROM v$resource_limit
WHERE resource_name IN ('processes', 'sessions');
