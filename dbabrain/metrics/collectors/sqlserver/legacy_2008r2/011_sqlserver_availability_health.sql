-- AVAILABILITY_DATABASE_HEALTH (SQL Server 2008 / 2008 R2): database mirroring and log shipping.
--
-- Why this variant exists:
-- Always On Availability Groups require SQL Server 2012 or newer. On SQL Server 2008 R2 and earlier,
-- high availability and disaster recovery rely entirely on database mirroring and log shipping.
-- Marking the metric unsupported on 2008 R2 left those production databases completely unmonitored:
-- an unhealthy mirror or a secondary database falling behind on log restores paged nobody.
--
-- What is graded:
--   * Database mirroring: sys.database_mirroring evaluated per mirrored database (mirroring_guid IS NOT NULL).
--     SYNCHRONIZED is OK, and SYNCHRONIZING on an asynchronous mirror (safety OFF); SYNCHRONIZING
--     on a synchronous one is WARNING; SUSPENDED, DISCONNECTED, PENDING_FAILOVER, or
--     UNSYNCHRONIZED are CRITICAL.
--   * Log shipping: msdb.dbo.log_shipping_monitor_primary and log_shipping_monitor_secondary.
--     Log shipping is graded against the thresholds it was configured with (backup_threshold and
--     restore_threshold minutes), not our own numbers: exceeding the configured threshold or having
--     never run (NULL date) is CRITICAL (the engine's own alert condition), otherwise OK.
--
-- 2008 R2 compatibility:
-- sys.dm_hadr_* DMVs do not exist before SQL Server 2012 and must not be referenced. Modern T-SQL
-- functions (CONCAT, IIF, TRY_CONVERT, TRY_CAST, FORMAT, STRING_AGG) are not supported on 2008 R2;
-- string concatenation uses standard + with COALESCE / CAST.
--
-- Least-privilege guarding:
-- A login lacking permissions on msdb must not lose mirroring rows. The msdb read is guarded
-- via HAS_PERMS_BY_NAME and TRY/CATCH; when unreadable, one LOGGING row 'log_shipping' is emitted.
--
-- When neither mirroring nor log shipping is configured, a single 'availability' NOT_CONFIGURED row
-- is emitted at status OK.

SET NOCOUNT ON;

DECLARE @results TABLE (
    metric_item varchar(256) NOT NULL,
    metric_value varchar(64) NULL,
    metric_unit varchar(32) NULL,
    status varchar(16) NOT NULL,
    message varchar(1000) NOT NULL
);

-- 1. Database Mirroring (2005+)
INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
SELECT
    CAST(COALESCE(DB_NAME(m.database_id), 'unknown') + ' :: mirroring' AS varchar(256)) AS metric_item,
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
    'database=' + COALESCE(DB_NAME(m.database_id), 'unknown') COLLATE DATABASE_DEFAULT
    + ', role=' + COALESCE(m.mirroring_role_desc, 'UNKNOWN') COLLATE DATABASE_DEFAULT
    + ', state=' + COALESCE(m.mirroring_state_desc, 'UNKNOWN') COLLATE DATABASE_DEFAULT
    + ', safety=' + COALESCE(m.mirroring_safety_level_desc, 'UNKNOWN') COLLATE DATABASE_DEFAULT
    + ', partner=' + COALESCE(m.mirroring_partner_instance, 'NONE') COLLATE DATABASE_DEFAULT
    + ', witness_state=' + COALESCE(m.mirroring_witness_state_desc, 'NONE') COLLATE DATABASE_DEFAULT AS message
FROM sys.database_mirroring AS m
WHERE m.mirroring_guid IS NOT NULL;

-- 2. Log Shipping (msdb monitor tables, guarded)
IF HAS_PERMS_BY_NAME('msdb.dbo.log_shipping_monitor_primary', 'OBJECT', 'SELECT') = 1
   AND HAS_PERMS_BY_NAME('msdb.dbo.log_shipping_monitor_secondary', 'OBJECT', 'SELECT') = 1
BEGIN
    BEGIN TRY
        INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
        SELECT
            CAST(p.primary_database COLLATE DATABASE_DEFAULT + ' :: log_shipping_primary' AS varchar(256)) AS metric_item,
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
            'database=' + p.primary_database COLLATE DATABASE_DEFAULT
            + ', role=PRIMARY'
            + ', last_backup=' + COALESCE(CONVERT(varchar(19), p.last_backup_date, 120), 'NEVER')
            + ', age_minutes=' + COALESCE(CAST(DATEDIFF(minute, p.last_backup_date, GETDATE()) AS varchar(20)), 'none')
            + ', threshold_minutes=' + CAST(p.backup_threshold AS varchar(20))
            + ', threshold_alert_enabled=' + CAST(p.threshold_alert_enabled AS varchar(5)) AS message
        FROM msdb.dbo.log_shipping_monitor_primary AS p;

        INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
        SELECT
            CAST(s.secondary_database COLLATE DATABASE_DEFAULT + ' :: log_shipping_secondary' AS varchar(256)) AS metric_item,
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
            'database=' + s.secondary_database COLLATE DATABASE_DEFAULT
            + ', role=SECONDARY'
            + ', last_copied=' + COALESCE(CONVERT(varchar(19), s.last_copied_date, 120), 'NEVER')
            + ', last_restored=' + COALESCE(CONVERT(varchar(19), s.last_restored_date, 120), 'NEVER')
            + ', age_minutes=' + COALESCE(CAST(DATEDIFF(minute, s.last_restored_date, GETDATE()) AS varchar(20)), 'none')
            + ', threshold_minutes=' + CAST(s.restore_threshold AS varchar(20))
            + ', threshold_alert_enabled=' + CAST(s.threshold_alert_enabled AS varchar(5))
            + ', primary_server=' + COALESCE(s.primary_server COLLATE DATABASE_DEFAULT, 'unknown')
            + ', primary_database=' + COALESCE(s.primary_database COLLATE DATABASE_DEFAULT, 'unknown')
            + ', last_restored_latency_minutes=' + COALESCE(CAST(s.last_restored_latency AS varchar(20)), 'none') AS message
        FROM msdb.dbo.log_shipping_monitor_secondary AS s;
    END TRY
    BEGIN CATCH
        INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
        VALUES (
            'log_shipping',
            'UNREADABLE',
            NULL,
            'LOGGING',
            'msdb log shipping monitor tables could not be read: ' + ERROR_MESSAGE()
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

-- 3. Not configured row (emitted only when none of mirroring or log shipping is configured)
IF NOT EXISTS (SELECT 1 FROM @results)
BEGIN
    INSERT INTO @results (metric_item, metric_value, metric_unit, status, message)
    VALUES (
        'availability',
        'NOT_CONFIGURED',
        NULL,
        'OK',
        'Database mirroring and log shipping are not configured on this instance.'
    );
END

SELECT
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
FROM @results;
