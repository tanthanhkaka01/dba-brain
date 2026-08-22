SELECT
    CAST(instance_name AS varchar2(256)) AS metric_item,
    CAST(status AS varchar2(32)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    CASE WHEN status = 'OPEN' THEN 'OK' ELSE 'CRITICAL' END AS status,
    'Oracle instance status=' || status || ', instance=' || instance_name AS message
FROM v$instance;
