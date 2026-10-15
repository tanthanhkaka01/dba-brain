SELECT
    CAST(d.name COLLATE DATABASE_DEFAULT AS varchar(256)) AS metric_item,
    CAST(d.log_reuse_wait_desc COLLATE DATABASE_DEFAULT AS varchar(256)) AS metric_value,
    CAST('desc' AS varchar(32)) AS metric_unit,
    CASE
        WHEN d.state_desc <> 'ONLINE' THEN 'CRITICAL'

        WHEN d.log_reuse_wait_desc IN (
            'NOTHING',
            'CHECKPOINT'
        ) THEN 'OK'

        WHEN d.log_reuse_wait_desc IN (
            'LOG_BACKUP',
            'ACTIVE_BACKUP_OR_RESTORE',
            'ACTIVE_TRANSACTION',
            'OLDEST_PAGE',
            'REPLICATION',
            'AVAILABILITY_REPLICA',
            -- Kept in step with the modern variant: Microsoft documents this as "routine, and
            -- typically brief", DBCC CHECKDB is what usually causes it, and the value outlives
            -- the check because log_reuse_wait_desc is only recomputed when truncation is next
            -- attempted. See the comment there for the measurement behind it.
            'DATABASE_SNAPSHOT_CREATION'
        ) THEN 'LOGGING'

        -- An unrecognised reason stays CRITICAL: it is worth a look precisely because nobody has
        -- decided what it means yet.
        ELSE 'CRITICAL'
    END AS status,
    CAST(
        'database=' + d.name COLLATE DATABASE_DEFAULT
        + ', state=' + d.state_desc COLLATE DATABASE_DEFAULT
        + ', recovery_model=' + d.recovery_model_desc COLLATE DATABASE_DEFAULT
        + ', log_reuse_wait=' + d.log_reuse_wait_desc COLLATE DATABASE_DEFAULT
        AS varchar(4000)
    ) AS message
FROM sys.databases d
WHERE d.database_id > 4;