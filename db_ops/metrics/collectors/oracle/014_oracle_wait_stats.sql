SELECT *
FROM (
    SELECT
        CAST(event AS varchar2(256)) AS metric_item,
        TO_CHAR(ROUND(time_waited_micro / 1000000, 2)) AS metric_value,
        CAST('seconds' AS varchar2(32)) AS metric_unit,
        'OK' AS status,
        'wait_event=' || event ||
            ', total_waits=' || TO_CHAR(total_waits) ||
            ', time_waited_seconds=' || TO_CHAR(ROUND(time_waited_micro / 1000000, 2)) AS message
    FROM v$system_event
    WHERE wait_class <> 'Idle'
    ORDER BY time_waited_micro DESC
)
WHERE ROWNUM <= 20;
