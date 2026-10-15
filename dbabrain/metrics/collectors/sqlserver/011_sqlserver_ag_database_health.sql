SELECT
    CAST(COALESCE(DB_NAME(drs.database_id), 'unknown') COLLATE DATABASE_DEFAULT AS varchar(256)) AS metric_item,
    CAST(drs.synchronization_state_desc COLLATE DATABASE_DEFAULT AS varchar(64)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CASE
        WHEN drs.synchronization_health_desc = 'HEALTHY' THEN 'OK'
        WHEN drs.synchronization_health_desc = 'PARTIALLY_HEALTHY' THEN 'WARNING'
        ELSE 'CRITICAL'
    END AS status,
    CONCAT(
        'database=', COALESCE(DB_NAME(drs.database_id), 'unknown') COLLATE DATABASE_DEFAULT,
        ', synchronization_state=', drs.synchronization_state_desc COLLATE DATABASE_DEFAULT,
        ', synchronization_health=', drs.synchronization_health_desc COLLATE DATABASE_DEFAULT
    ) AS message
FROM sys.dm_hadr_database_replica_states AS drs

UNION ALL

SELECT
    CAST('availability_group' AS varchar(256)) AS metric_item,
    CAST('NOT_CONFIGURED' AS varchar(64)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    'OK' AS status,
    'Availability Groups are not configured or no AG database state is visible.' AS message
WHERE NOT EXISTS
(
    SELECT 1
    FROM sys.dm_hadr_database_replica_states
);