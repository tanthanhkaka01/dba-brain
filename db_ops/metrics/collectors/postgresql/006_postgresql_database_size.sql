SELECT
    datname AS metric_item,
    pg_database_size(datname)::text AS metric_value,
    'bytes' AS metric_unit,
    'OK' AS status,
    'database=' || datname || ', size=' || pg_size_pretty(pg_database_size(datname)) AS message
FROM pg_database
WHERE datistemplate = false
  AND datallowconn = true
ORDER BY pg_database_size(datname) DESC;
