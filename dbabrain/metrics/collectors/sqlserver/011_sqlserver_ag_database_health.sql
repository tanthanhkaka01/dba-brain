-- AVAILABILITY_DATABASE_HEALTH (SQL Server 2012+): Availability Groups, mirroring, and log shipping.
--
-- Why mirroring and log shipping are included alongside Availability Groups:
-- Previously this collector only evaluated Always On Availability Groups. On instances where
-- High Availability or Disaster Recovery is provided by database mirroring or log shipping
-- (including hybrid environments migrating to or from AGs), the collector answered NOT_CONFIGURED.
-- A broken mirror or an out-of-sync log-shipped secondary was completely invisible and paged nobody.
--
-- Why Availability Group queues are graded:
-- A replica reported as HEALTHY by its own flag can still be gigabytes behind. A secondary replica
-- falling far behind on log send will lose that uncommitted log on failover (potential data loss, RPO),
-- while a bloated redo queue is exactly what a failover waits for: the engine must finish redoing the
-- log before the database becomes queryable and operational (RTO).
--
-- Grading follows Redgate SQL Monitor's default "Availability group - replication falling behind"
-- alert: Medium (WARNING) when the combined log send queue and redo queue size reaches 100 MB
-- (@queue_warning_kb = 102400 KB). Redgate requires 120 seconds of sustained backlog; because a
-- collector execution is a single instantaneous reading without cross-run state, the metric's
-- configured repeat interval is what repeats the observation.
--
-- All three availability technologies are evaluated in one pass:
--   * Availability Groups: sys.dm_hadr_database_replica_states evaluated per database replica.
--     AG metric_item is '<database> :: <replica_server_name>' (sys.availability_replicas) because on
--     a primary replica the DMV exposes rows for all replicas, which otherwise collided under the bare
--     database name. HEALTHY is OK, but escalates to WARNING when combined queue size reaches
--     @queue_warning_kb; PARTIALLY_HEALTHY is WARNING; other states (NOT_HEALTHY) remain CRITICAL.
--   * Database mirroring: sys.database_mirroring evaluated per mirrored database (mirroring_guid IS NOT NULL).
--     SYNCHRONIZED is OK, and SYNCHRONIZING on an asynchronous mirror (safety OFF); SYNCHRONIZING
--     on a synchronous one is WARNING; SUSPENDED, DISCONNECTED, PENDING_FAILOVER, or
--     UNSYNCHRONIZED are CRITICAL.
--   * Log shipping: msdb.dbo.log_shipping_monitor_primary and log_shipping_monitor_secondary.
--     Log shipping is graded against the thresholds it was configured with (backup_threshold and
--     restore_threshold minutes), not arbitrary hardcoded intervals: exceeding the configured threshold
--     or having never run (NULL date) is CRITICAL (the engine's own alert condition), otherwise OK.
--
-- Least-privilege guarding:
-- A monitoring login lacking permissions on msdb must not lose AG or mirroring health rows.
-- Access to msdb log shipping tables is guarded (HAS_PERMS_BY_NAME and TRY/CATCH into a table variable);
-- when unreadable, a single LOGGING row 'log_shipping' is emitted explaining the condition without failing
-- the metric or dropping visible AG / mirroring status.
--
-- The NOT_CONFIGURED row is emitted as item 'availability' with status OK only when none of Availability
-- Groups, database mirroring, or log shipping is configured on the instance.

SET NOCOUNT ON;

DECLARE @queue_warning_kb bigint = 102400;

DECLARE @results TABLE (
    metric_item varchar(256) NOT NULL,
    metric_value varchar(64) NULL,
    metric_unit varchar(32) NULL,
    status varchar(16) NOT NULL,
    message varchar(1000) NOT NULL
);

-- 1. Always On Availability Groups (2012+)
INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
SELECT
    CAST(CONCAT(COALESCE(DB_NAME(drs.database_id), 'unknown') COLLATE DATABASE_DEFAULT, ' :: ', COALESCE(ar.replica_server_name, 'unknown') COLLATE DATABASE_DEFAULT) AS varchar(256)) AS metric_item,
    CAST(drs.synchronization_state_desc COLLATE DATABASE_DEFAULT AS varchar(64)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CASE
        WHEN drs.synchronization_health_desc = 'HEALTHY'
             AND (ISNULL(drs.log_send_queue_size, 0) + ISNULL(drs.redo_queue_size, 0)) >= @queue_warning_kb
        THEN 'WARNING'
        WHEN drs.synchronization_health_desc = 'HEALTHY' THEN 'OK'
        WHEN drs.synchronization_health_desc = 'PARTIALLY_HEALTHY' THEN 'WARNING'
        ELSE 'CRITICAL'
    END AS status,
    CONCAT(
        'database=', COALESCE(DB_NAME(drs.database_id), 'unknown') COLLATE DATABASE_DEFAULT,
        ', replica_server_name=', COALESCE(ar.replica_server_name, 'unknown') COLLATE DATABASE_DEFAULT,
        ', is_local=', drs.is_local,
        ', role=', COALESCE(ars.role_desc, 'UNKNOWN') COLLATE DATABASE_DEFAULT,
        ', synchronization_state=', COALESCE(drs.synchronization_state_desc, 'UNKNOWN') COLLATE DATABASE_DEFAULT,
        ', synchronization_health=', COALESCE(drs.synchronization_health_desc, 'UNKNOWN') COLLATE DATABASE_DEFAULT,
        ', log_send_queue_kb=', COALESCE(CAST(drs.log_send_queue_size AS varchar(32)), 'n/a'),
        ', redo_queue_kb=', COALESCE(CAST(drs.redo_queue_size AS varchar(32)), 'n/a'),
        ', log_send_rate_kb_s=', COALESCE(CAST(drs.log_send_rate AS varchar(32)), 'n/a'),
        ', redo_rate_kb_s=', COALESCE(CAST(drs.redo_rate AS varchar(32)), 'n/a'),
        ', est_redo_seconds=', COALESCE(CAST(drs.redo_queue_size / NULLIF(drs.redo_rate, 0) AS varchar(32)), 'n/a'),
        ', est_data_loss_seconds=', COALESCE(CAST(CASE WHEN ars.role_desc = 'SECONDARY' AND drs.last_commit_time IS NOT NULL AND pri.last_commit_time IS NOT NULL THEN DATEDIFF(second, drs.last_commit_time, pri.last_commit_time) ELSE NULL END AS varchar(32)), 'n/a'),
        ', last_commit_time=', COALESCE(CONVERT(varchar(19), drs.last_commit_time, 120), 'n/a')
    ) AS message
FROM sys.dm_hadr_database_replica_states AS drs
LEFT JOIN sys.availability_replicas AS ar
    ON drs.replica_id = ar.replica_id
LEFT JOIN sys.dm_hadr_availability_replica_states AS ars
    ON drs.replica_id = ars.replica_id
LEFT JOIN (
    SELECT
        drs_pri.database_id,
        drs_pri.group_id,
        drs_pri.last_commit_time
    FROM sys.dm_hadr_database_replica_states AS drs_pri
    INNER JOIN sys.dm_hadr_availability_replica_states AS ars_pri
        ON drs_pri.replica_id = ars_pri.replica_id
    WHERE ars_pri.role_desc = 'PRIMARY'
) AS pri
    ON drs.database_id = pri.database_id
   AND drs.group_id = pri.group_id;

-- 2. Database Mirroring (2005+)
INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
SELECT
    CAST(CONCAT(COALESCE(DB_NAME(m.database_id), 'unknown') COLLATE DATABASE_DEFAULT, ' :: mirroring') AS varchar(256)) AS metric_item,
    CAST(m.mirroring_state_desc COLLATE DATABASE_DEFAULT AS varchar(64)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CASE
        WHEN m.mirroring_state_desc = 'SYNCHRONIZED' THEN 'OK'
        -- An asynchronous mirror (safety OFF, high performance) reads SYNCHRONIZING for as long as it
        -- runs: it never reaches SYNCHRONIZED, by design. Only a synchronous one is catching up.
        WHEN m.mirroring_state_desc = 'SYNCHRONIZING' AND m.mirroring_safety_level_desc = 'OFF' THEN 'OK'
        WHEN m.mirroring_state_desc = 'SYNCHRONIZING' THEN 'WARNING'
        WHEN m.mirroring_state_desc IN ('SUSPENDED', 'DISCONNECTED', 'PENDING_FAILOVER', 'UNSYNCHRONIZED') THEN 'CRITICAL'
        ELSE 'CRITICAL'
    END AS status,
    CONCAT(
        'database=', COALESCE(DB_NAME(m.database_id), 'unknown') COLLATE DATABASE_DEFAULT,
        ', role=', COALESCE(m.mirroring_role_desc, 'UNKNOWN') COLLATE DATABASE_DEFAULT,
        ', state=', COALESCE(m.mirroring_state_desc, 'UNKNOWN') COLLATE DATABASE_DEFAULT,
        ', safety=', COALESCE(m.mirroring_safety_level_desc, 'UNKNOWN') COLLATE DATABASE_DEFAULT,
        ', partner=', COALESCE(m.mirroring_partner_instance, 'NONE') COLLATE DATABASE_DEFAULT,
        ', witness_state=', COALESCE(m.mirroring_witness_state_desc, 'NONE') COLLATE DATABASE_DEFAULT
    ) AS message
FROM sys.database_mirroring AS m
WHERE m.mirroring_guid IS NOT NULL;

-- 3. Log Shipping (msdb monitor tables, guarded)
IF HAS_PERMS_BY_NAME('msdb.dbo.log_shipping_monitor_primary', 'OBJECT', 'SELECT') = 1
   AND HAS_PERMS_BY_NAME('msdb.dbo.log_shipping_monitor_secondary', 'OBJECT', 'SELECT') = 1
BEGIN
    BEGIN TRY
        INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
        SELECT
            CAST(CONCAT(p.primary_database COLLATE DATABASE_DEFAULT, ' :: log_shipping_primary') AS varchar(256)) AS metric_item,
            CAST(CASE
                WHEN p.last_backup_date IS NULL THEN 'NEVER_RUN'
                WHEN DATEDIFF(minute, p.last_backup_date, GETDATE()) > p.backup_threshold THEN 'THRESHOLD_EXCEEDED'
                ELSE 'OK'
            END AS varchar(64)) AS metric_value,
            CAST(NULL AS varchar(32)) AS metric_unit,
            CASE
                WHEN p.last_backup_date IS NULL THEN 'CRITICAL'
                WHEN DATEDIFF(minute, p.last_backup_date, GETDATE()) > p.backup_threshold THEN 'CRITICAL'
                ELSE 'OK'
            END AS status,
            CONCAT(
                'database=', p.primary_database COLLATE DATABASE_DEFAULT,
                ', role=PRIMARY',
                ', last_backup=', COALESCE(CONVERT(varchar(19), p.last_backup_date, 120), 'NEVER'),
                ', age_minutes=', COALESCE(CAST(DATEDIFF(minute, p.last_backup_date, GETDATE()) AS varchar(20)), 'none'),
                ', threshold_minutes=', p.backup_threshold,
                ', threshold_alert_enabled=', p.threshold_alert_enabled
            ) AS message
        FROM msdb.dbo.log_shipping_monitor_primary AS p;

        INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
        SELECT
            CAST(CONCAT(s.secondary_database COLLATE DATABASE_DEFAULT, ' :: log_shipping_secondary') AS varchar(256)) AS metric_item,
            CAST(CASE
                WHEN s.last_restored_date IS NULL THEN 'NEVER_RUN'
                WHEN DATEDIFF(minute, s.last_restored_date, GETDATE()) > s.restore_threshold THEN 'THRESHOLD_EXCEEDED'
                ELSE 'OK'
            END AS varchar(64)) AS metric_value,
            CAST(NULL AS varchar(32)) AS metric_unit,
            CASE
                WHEN s.last_restored_date IS NULL THEN 'CRITICAL'
                WHEN DATEDIFF(minute, s.last_restored_date, GETDATE()) > s.restore_threshold THEN 'CRITICAL'
                ELSE 'OK'
            END AS status,
            CONCAT(
                'database=', s.secondary_database COLLATE DATABASE_DEFAULT,
                ', role=SECONDARY',
                ', last_copied=', COALESCE(CONVERT(varchar(19), s.last_copied_date, 120), 'NEVER'),
                ', last_restored=', COALESCE(CONVERT(varchar(19), s.last_restored_date, 120), 'NEVER'),
                ', age_minutes=', COALESCE(CAST(DATEDIFF(minute, s.last_restored_date, GETDATE()) AS varchar(20)), 'none'),
                ', threshold_minutes=', s.restore_threshold,
                ', threshold_alert_enabled=', s.threshold_alert_enabled,
                ', primary_server=', COALESCE(s.primary_server COLLATE DATABASE_DEFAULT, 'unknown'),
                ', primary_database=', COALESCE(s.primary_database COLLATE DATABASE_DEFAULT, 'unknown'),
                ', last_restored_latency_minutes=', COALESCE(CAST(s.last_restored_latency AS varchar(20)), 'none')
            ) AS message
        FROM msdb.dbo.log_shipping_monitor_secondary AS s;
    END TRY
    BEGIN CATCH
        INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
        VALUES (
            'log_shipping',
            'UNREADABLE',
            NULL,
            'LOGGING',
            CONCAT('msdb log shipping monitor tables could not be read: ', ERROR_MESSAGE())
        );
    END CATCH
END
ELSE
BEGIN
    INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
    VALUES (
        'log_shipping',
        'UNREADABLE',
        NULL,
        'LOGGING',
        'msdb log shipping monitor tables are unreadable by this login (lacks SELECT permission).'
    );
END

-- 4. Not configured row (emitted only when none of AG, mirroring or log shipping is configured)
IF NOT EXISTS (SELECT 1 FROM @results)
BEGIN
    INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
    VALUES (
        'availability',
        'NOT_CONFIGURED',
        NULL,
        'OK',
        'Availability Groups, database mirroring, and log shipping are not configured on this instance.'
    );
END

SELECT
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
FROM @results;