-- AVAILABILITY_DATABASE_HEALTH (Oracle variant) - Data Guard health.
--
-- Oracle counterpart of the SQL Server Always On query (011_sqlserver_ag_database_health.sql)
-- and of the PostgreSQL replication query (050_postgresql_replication.sql), so one engine-neutral
-- metric code answers "is replication healthy?" on all three engines.
--
-- Role-aware, in one query:
--   * always: one row for this database's role / open mode / protection mode.
--   * transport + apply lag from v$dataguard_stats, when Data Guard is configured.
--   * one row per remote redo destination, so a broken or gapped destination is visible.
--   * NOT_CONFIGURED when no standby destination exists, mirroring the SQL Server variant
--     rather than reporting nothing on a standalone instance.
--
-- v$dataguard_stats reports lag as an interval string ('+00 00:00:05'). It is converted only when
-- it matches that shape, so an unexpected value degrades to "unparsed"/OK instead of failing the
-- whole collection with ORA-01867.

SELECT
    CAST('database_role' AS varchar2(256)) AS metric_item,
    CAST(d.database_role AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    CASE
        WHEN d.database_role = 'PRIMARY' AND d.open_mode = 'READ WRITE' THEN 'OK'
        WHEN d.database_role LIKE '%STANDBY%' AND d.open_mode IN ('MOUNTED', 'READ ONLY', 'READ ONLY WITH APPLY') THEN 'OK'
        ELSE 'WARNING'
    END AS status,
    'role=' || d.database_role
        || ', db_unique_name=' || d.db_unique_name
        || ', open_mode=' || d.open_mode
        || ', protection_mode=' || d.protection_mode
        || ', switchover_status=' || d.switchover_status
        || ', force_logging=' || d.force_logging AS message
FROM v$database d

UNION ALL

-- Transport/apply lag. WARNING from 5 minutes, CRITICAL from 15.
SELECT
    CAST(s.name AS varchar2(256)) AS metric_item,
    CAST(NVL(TO_CHAR(s.lag_seconds), s.value) AS varchar2(64)) AS metric_value,
    CAST(CASE WHEN s.lag_seconds IS NULL THEN NULL ELSE 'seconds' END AS varchar2(32)) AS metric_unit,
    CASE
        WHEN s.lag_seconds IS NULL THEN 'OK'
        WHEN s.lag_seconds >= 900 THEN 'CRITICAL'
        WHEN s.lag_seconds >= 300 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'name=' || s.name
        || ', value=' || NVL(s.value, 'NULL')
        || ', lag_seconds=' || NVL(TO_CHAR(s.lag_seconds), 'unparsed')
        || ', unit=' || NVL(s.unit, '')
        || ', time_computed=' || NVL(s.time_computed, '') AS message
FROM
(
    SELECT
        ds.name,
        ds.value,
        ds.unit,
        ds.time_computed,
        CASE
            WHEN REGEXP_LIKE(ds.value, '^[+-]?[0-9]+ [0-9]{1,2}:[0-9]{2}:[0-9]{2}')
                THEN EXTRACT(DAY    FROM TO_DSINTERVAL(ds.value)) * 86400
                   + EXTRACT(HOUR   FROM TO_DSINTERVAL(ds.value)) * 3600
                   + EXTRACT(MINUTE FROM TO_DSINTERVAL(ds.value)) * 60
                   + EXTRACT(SECOND FROM TO_DSINTERVAL(ds.value))
        END AS lag_seconds
    FROM v$dataguard_stats ds
    WHERE ds.name IN ('transport lag', 'apply lag')
) s

UNION ALL

-- Remote redo destinations: a destination in ERROR, or with a resolvable gap, is the signal.
SELECT
    CAST(ads.dest_name AS varchar2(256)) AS metric_item,
    CAST(ads.status AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    CASE
        WHEN ads.status = 'VALID' AND NVL(ads.gap_status, 'NO GAP') IN ('NO GAP', ' ', '') THEN 'OK'
        WHEN ads.status IN ('DEFERRED', 'DISABLED') THEN 'WARNING'
        ELSE 'CRITICAL'
    END AS status,
    'dest=' || ads.dest_name
        || ', status=' || ads.status
        || ', type=' || ads.type
        || ', db_unique_name=' || NVL(ads.db_unique_name, '')
        || ', destination=' || NVL(ads.destination, '')
        || ', database_mode=' || NVL(ads.database_mode, '')
        || ', synchronization_status=' || NVL(ads.synchronization_status, '')
        || ', synchronized=' || NVL(ads.synchronized, '')
        || ', gap_status=' || NVL(ads.gap_status, '')
        || ', recovery_mode=' || NVL(ads.recovery_mode, '')
        || ', archived_seq=' || TO_CHAR(ads."ARCHIVED_SEQ#")
        || ', applied_seq=' || TO_CHAR(ads."APPLIED_SEQ#")
        || ', error=' || NVL(ads.error, 'none') AS message
FROM v$archive_dest_status ads
WHERE ads.type <> 'LOCAL'
  AND ads.status <> 'INACTIVE'

UNION ALL

-- Standalone instance: say so explicitly instead of returning nothing.
SELECT
    CAST('data_guard' AS varchar2(256)) AS metric_item,
    CAST('NOT_CONFIGURED' AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    'OK' AS status,
    'Data Guard is not configured: no remote redo destination is active on this instance.' AS message
FROM dual
WHERE NOT EXISTS
(
    SELECT 1
    FROM v$archive_dest_status
    WHERE type <> 'LOCAL'
      AND status <> 'INACTIVE'
);
