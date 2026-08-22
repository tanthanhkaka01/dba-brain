SELECT
    d.name COLLATE DATABASE_DEFAULT AS metric_item,
    d.state_desc COLLATE DATABASE_DEFAULT AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CASE
        WHEN d.state_desc = 'ONLINE' AND d.is_read_only = 0 THEN 'OK'
        WHEN d.state_desc = 'ONLINE' AND d.is_read_only = 1 THEN 'WARNING'
        ELSE 'CRITICAL'
    END AS status,
    CONCAT(
        'database=', d.name COLLATE DATABASE_DEFAULT,
        ', state=', d.state_desc COLLATE DATABASE_DEFAULT,
        ', user_access=', d.user_access_desc COLLATE DATABASE_DEFAULT,
        ', read_only=', d.is_read_only,
        -- Database compatibility level (110=2012 ... 150=2019, 160=2022). Reported per database
        -- because it is a per-database setting: a database restored or attached from an older
        -- instance keeps its old level, so it can sit well below the engine it now runs on and
        -- silently miss optimizer/feature behaviour the version number implies it has.
        ', compatibility_level=', d.compatibility_level
        , ', collation_name=', d.collation_name
        , ', is_auto_shrink_on=', d.is_auto_shrink_on
        , ', is_broker_enabled=', d.is_broker_enabled
        , ', service_broker_guid=', d.service_broker_guid
        -- Ordering key for every database list db_ops renders: master, tempdb, model, msdb,
        -- then user databases in creation order. This metric filters to `database_id > 4`,
        -- so DATABASE_CONFIG carries it for the system four; both are read.
        , ', database_id=', d.database_id
    ) AS message
FROM sys.databases AS d
WHERE d.database_id > 4
ORDER BY d.database_id;