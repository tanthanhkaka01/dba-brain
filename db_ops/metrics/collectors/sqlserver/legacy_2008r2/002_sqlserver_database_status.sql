
SELECT
    d.name COLLATE DATABASE_DEFAULT AS metric_item,
    d.state_desc COLLATE DATABASE_DEFAULT AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CASE
        WHEN d.state_desc = 'ONLINE' AND d.is_read_only = 0 THEN 'OK'
        WHEN d.state_desc = 'ONLINE' AND d.is_read_only = 1 THEN 'WARNING'
        ELSE 'CRITICAL'
    END AS status,
    'database=' + d.name COLLATE DATABASE_DEFAULT
        + ', state=' + d.state_desc COLLATE DATABASE_DEFAULT
        + ', user_access=' + d.user_access_desc COLLATE DATABASE_DEFAULT
        + ', read_only=' + CAST(d.is_read_only AS varchar(8))
        -- Same field as the 2012+ variant so the message reads identically whatever the engine
        -- version. sys.databases.compatibility_level exists from SQL Server 2005 on, so it is
        -- available here too (90=2005, 100=2008/2008R2). Explicit CAST because this file builds
        -- the message with + rather than CONCAT, and + on a tinyint would attempt numeric
        -- addition against the string instead of concatenating.
        + ', compatibility_level=' + CAST(d.compatibility_level AS varchar(8))
        -- Ordering key for every database list db_ops renders: master, tempdb, model, msdb,
        -- then user databases in creation order. This metric filters to `database_id > 4`,
        -- so DATABASE_CONFIG carries it for the system four; both are read.
        + ', database_id=' + CAST(d.database_id AS varchar(10))
        + ', collation_name=' + CAST(d.collation_name AS VARCHAR(100))
        + ', is_auto_shrink_on=' + CAST(d.is_auto_shrink_on AS VARCHAR(100))
        + ', is_broker_enabled=' + CAST(d.is_broker_enabled AS VARCHAR(100))
        + ', service_broker_guid=' + CAST(d.service_broker_guid AS VARCHAR(100))
        AS message
FROM sys.databases AS d
WHERE d.database_id > 4
ORDER BY d.database_id;