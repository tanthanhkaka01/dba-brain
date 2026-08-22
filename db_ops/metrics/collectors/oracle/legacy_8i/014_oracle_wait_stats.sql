-- Oracle 8i legacy variant - PERFORMANCE_WAIT_STATS. Top non-idle system waits from
-- v$system_event (8i). Surfaces free buffer waits / buffer busy / db file reads that
-- accompanied the incident.
SELECT * FROM (
    SELECT
        event AS metric_item,
        TO_CHAR(time_waited) AS metric_value,
        'centiseconds' AS metric_unit,
        'OK' AS status,
        'event=' || event || ', total_waits=' || total_waits ||
            ', time_waited_cs=' || time_waited AS message
    FROM v$system_event
    WHERE event NOT LIKE 'SQL*Net%'
      AND event NOT LIKE '%timer%'
      AND event NOT LIKE 'rdbms ipc%'
      AND event NOT LIKE 'pmon%'
      AND event NOT LIKE 'smon%'
    ORDER BY time_waited DESC
)
WHERE ROWNUM <= 10;
