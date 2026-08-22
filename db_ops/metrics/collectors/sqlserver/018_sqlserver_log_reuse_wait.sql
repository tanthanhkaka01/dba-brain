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
            -- Microsoft documents this one as "routine, and typically brief": a database snapshot
            -- is being created, and DBCC CHECKDB creates an internal one. So the reason a routine
            -- consistency check exists is also a reason this fires - including db_ops' own restore
            -- workflow, whose last step is DBCC CHECKDB. Unlisted, it fell to the ELSE below and
            -- was reported at the same severity as a database that is OFFLINE.
            --
            -- It also OUTLIVES the check that caused it. log_reuse_wait_desc is a cached value,
            -- recomputed only when the engine next attempts to truncate; on a FULL-recovery
            -- database with no log backup yet and no write traffic, that attempt may never come.
            -- Measured on 192.0.2.11 after the 192.0.2.248 migration: the five databases
            -- still reporting it were exactly the five whose log was untouched at its initial
            -- 8 MB, while every database that had grown its log read NOTHING again.
            'DATABASE_SNAPSHOT_CREATION'
        ) THEN 'LOGGING'

        -- Deliberately still CRITICAL: an unrecognised reason is worth a look precisely because
        -- nobody has decided what it means yet. DATABASE_MIRRORING and LOG_SCAN are the other two
        -- documented values not listed above; neither has been observed on this estate, so they
        -- are left to this branch rather than pre-approved from a manual.
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