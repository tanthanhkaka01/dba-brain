-- Oracle 8i (8.1.7) legacy variant - INSTANCE_STATUS. Validated view: v$instance.
SELECT
    instance_name AS metric_item,
    status AS metric_value,
    NULL AS metric_unit,
    CASE WHEN status = 'OPEN' THEN 'OK' ELSE 'CRITICAL' END AS status,
    'instance=' || instance_name || ', status=' || status ||
        ', version=' || version || ', host=' || host_name AS message
FROM v$instance;
