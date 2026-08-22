SELECT
    CAST(table_schema AS CHAR(256)) AS metric_item,
    CAST(ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) AS CHAR(32)) AS metric_value,
    CAST('MB' AS CHAR(32)) AS metric_unit,
    'OK' AS status,
    CONCAT(
        'schema=', table_schema,
        ', used_mb=', ROUND(SUM(data_length + index_length) / 1024 / 1024, 2),
        ', free_mb=', ROUND(SUM(data_free) / 1024 / 1024, 2)
    ) AS message
FROM information_schema.tables
WHERE table_schema NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
GROUP BY table_schema
ORDER BY SUM(data_length + index_length) DESC;
