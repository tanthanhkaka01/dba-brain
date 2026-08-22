SELECT
    CAST(s.schema_name AS CHAR(256)) AS metric_item,
    CAST('ONLINE' AS CHAR(32)) AS metric_value,
    CAST(NULL AS CHAR(32)) AS metric_unit,
    'OK' AS status,
    CONCAT('database=', s.schema_name, ', default_charset=', s.default_character_set_name) AS message
FROM information_schema.schemata AS s
WHERE s.schema_name NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
ORDER BY s.schema_name;
