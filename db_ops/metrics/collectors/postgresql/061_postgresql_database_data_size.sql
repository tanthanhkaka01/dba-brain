-- DATABASE_DATA_SIZE (PostgreSQL): on-disk size of each database, in GB.
--
-- Same contract as the SQL Server variant so the inventory report reads one shape on every
-- engine: metric_item is the database name, metric_value is a plain number of GB.
--
-- pg_database_size() covers everything in the database's directory — heap, indexes, TOAST and
-- the free space maps — which is the number that matches what the filesystem shows. Template
-- databases are excluded: they are fixed-size scaffolding, and listing them pushes the real
-- databases down the report for no reason.
--
-- A database the connecting role cannot access raises rather than returning NULL, so the size
-- call is guarded: an unreadable database reports 0 with the reason in the message instead of
-- failing the whole metric and leaving every database blank.
SELECT datname AS metric_item,
       to_char(round((size_bytes / 1073741824.0)::numeric, 2), 'FM999999990.00') AS metric_value,
       'GB' AS metric_unit,
       'OK' AS status,
       'bytes=' || size_bytes
         || ', pretty=' || pg_size_pretty(size_bytes)
         || CASE WHEN size_bytes = 0 THEN ' (size not readable by this role)' ELSE '' END AS message
FROM (
    SELECT d.datname,
           CASE WHEN has_database_privilege(d.datname, 'CONNECT')
                THEN pg_database_size(d.datname) ELSE 0 END AS size_bytes
    FROM pg_database AS d
    WHERE NOT d.datistemplate
) AS s
ORDER BY datname;
