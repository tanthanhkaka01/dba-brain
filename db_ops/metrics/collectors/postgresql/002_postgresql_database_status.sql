SELECT
    datname AS metric_item,
    'ONLINE' AS metric_value,
    NULL::text AS metric_unit,
    'OK' AS status,
    'database=' || datname
        || ', connections_allowed=' || datallowconn
        || ', size=' || pg_size_pretty(pg_database_size(datname))
        -- PostgreSQL's answer to SQL Server's database_id, under the same key so the reports can
        -- order every engine's database list the same way: catalog order, which is creation
        -- order, rather than alphabetical.
        || ', database_id=' || oid AS message
FROM pg_database
WHERE datistemplate = false
  AND datallowconn = true
ORDER BY oid;
