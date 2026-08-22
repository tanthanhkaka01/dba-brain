-- DATABASE_USER_PERMISSIONS (MySQL/MariaDB): account and privilege inventory.
-- One row per account with its aggregated GLOBAL privileges (from
-- information_schema.USER_PRIVILEGES), plus one row per (schema, account) database-level grant
-- (from information_schema.SCHEMA_PRIVILEGES). Status WARNING when a global grant is powerful
-- (SUPER, GRANT OPTION, FILE, SHUTDOWN, RELOAD, PROCESS, CREATE USER) - the MySQL equivalent of
-- the SQL Server high-privilege flag. Requires the monitor user to see the grant catalog
-- (default for its own grants; SELECT on mysql.* or the grant tables for the full picture).
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        CAST(up.GRANTEE AS CHAR(400)) AS metric_item,
        CAST('GLOBAL' AS CHAR(64)) AS metric_value,
        CAST(NULL AS CHAR(32)) AS metric_unit,
        CAST(CASE WHEN SUM(up.PRIVILEGE_TYPE IN
                 ('SUPER','GRANT OPTION','FILE','SHUTDOWN','RELOAD','PROCESS','CREATE USER')) > 0
             THEN 'WARNING' ELSE 'OK' END AS CHAR(16)) AS status,
        CAST(CONCAT('global_privs=[',
             GROUP_CONCAT(up.PRIVILEGE_TYPE ORDER BY up.PRIVILEGE_TYPE SEPARATOR ','), ']') AS CHAR(1000)) AS message,
        CASE WHEN SUM(up.PRIVILEGE_TYPE IN
                 ('SUPER','GRANT OPTION','FILE','SHUTDOWN','RELOAD','PROCESS','CREATE USER')) > 0
             THEN 0 ELSE 1 END AS sort_rank
    FROM information_schema.USER_PRIVILEGES up
    GROUP BY up.GRANTEE

    UNION ALL

    SELECT
        CAST(CONCAT(sp.TABLE_SCHEMA, ' / ', sp.GRANTEE) AS CHAR(400)) AS metric_item,
        CAST('DB_GRANT' AS CHAR(64)) AS metric_value,
        CAST(NULL AS CHAR(32)) AS metric_unit,
        CAST('OK' AS CHAR(16)) AS status,
        CAST(CONCAT('privs=[',
             GROUP_CONCAT(sp.PRIVILEGE_TYPE ORDER BY sp.PRIVILEGE_TYPE SEPARATOR ','), ']') AS CHAR(1000)) AS message,
        2 AS sort_rank
    FROM information_schema.SCHEMA_PRIVILEGES sp
    WHERE sp.TABLE_SCHEMA NOT IN ('mysql', 'sys', 'performance_schema', 'information_schema')
    GROUP BY sp.TABLE_SCHEMA, sp.GRANTEE
) AS q
ORDER BY q.sort_rank, q.metric_item
LIMIT 200
