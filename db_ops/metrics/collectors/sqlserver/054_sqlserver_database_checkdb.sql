-- DATABASE_CHECKDB: last successful DBCC CHECKDB per database (dbi_dbccLastKnownGood).
--
-- This used to return status OK for every row, including when the value was 'unknown', so a
-- server that has never proven its integrity looked exactly like one that proves it nightly.
-- Both audited targets reported 'unknown' for every database while the page said OK.
--
-- 'unknown' was also two different findings wearing one label, which is why it could not be
-- alerted on safely:
--
--   * DBCC DBINFO ran and reported the 1900-01-01 sentinel -> CHECKDB has genuinely never
--     recorded a known-good page. That is a finding about the DATABASE.
--   * DBCC DBINFO could not run at all ("User 'guest' does not have permission to run DBCC
--     DBINFO", observed on 192.0.2.248) -> the monitoring login lacks rights. That is a finding
--     about the MONITORING, and reporting it per database would blame 11 databases for one grant.
--
-- So the permission case is detected once, up front, and emitted as a single instance-level row.
-- Nothing else is collected in that case, because nothing else can be.

SET NOCOUNT ON;

DECLARE @stale_warn_days int = 7;    -- a weekly integrity check is the normal baseline
DECLARE @stale_crit_days int = 30;   -- a schedule that used to work has clearly stopped

IF OBJECT_ID('tempdb..#dbinfo') IS NOT NULL DROP TABLE #dbinfo;
IF OBJECT_ID('tempdb..#checkdb') IS NOT NULL DROP TABLE #checkdb;
CREATE TABLE #dbinfo (ParentObject varchar(255), Object varchar(255), Field varchar(255), Value varchar(255));
CREATE TABLE #checkdb (db sysname, last_good varchar(40) NULL);

-- ---------------------------------------------------------------- can we read DBINFO at all?
-- DBCC DBINFO needs elevated rights and they are granted at the instance, so one probe answers
-- for every database. Probing once also keeps the failure out of the per-database loop, where it
-- would have produced one identical row per database.
DECLARE @dbinfo_error nvarchar(400) = NULL;
BEGIN TRY
    INSERT INTO #dbinfo (ParentObject, Object, Field, Value)
    EXEC (N'DBCC DBINFO(''master'') WITH TABLERESULTS');
END TRY
BEGIN CATCH
    SET @dbinfo_error = LEFT(REPLACE(REPLACE(ERROR_MESSAGE(), CHAR(13), ' '), CHAR(10), ' '), 300);
END CATCH

IF @dbinfo_error IS NOT NULL
BEGIN
    SELECT
        CAST('checkdb_coverage' AS varchar(256)) AS metric_item,
        CAST('UNREADABLE' AS varchar(32)) AS metric_value,
        CAST(NULL AS varchar(32)) AS metric_unit,
        CAST('WARNING' AS varchar(16)) AS status,
        CAST('issue_type=CHECKDB_UNREADABLE, error=' + @dbinfo_error
             + ' - this is a MONITORING permission gap, not a database fault: integrity cannot be'
             + ' confirmed or denied until the collector login can run DBCC DBINFO.'
             AS varchar(4000)) AS message;
END
ELSE
BEGIN
    DECLARE @db sysname, @sql nvarchar(400);
    DECLARE db_cur CURSOR LOCAL FAST_FORWARD FOR
        -- tempdb is excluded because it can never satisfy this metric. It is recreated from model
        -- at every service start, so its DBINFO page — and with it dbi_dbccLastKnownGood — is
        -- new every restart: running DBCC CHECKDB on tempdb does not durably record a known-good,
        -- and the row comes back 'never' forever. It was 11 permanent WARNINGs across the estate,
        -- one per instance, that no action could ever clear. (Ola Hallengren's
        -- DatabaseIntegrityCheck leaves tempdb out for the same reason.)
        -- master/model/msdb stay in: their known-good IS durable, so 'never' there is a real
        -- finding and one somebody can close by running CHECKDB.
        SELECT name FROM sys.databases WHERE state = 0 AND name <> 'tempdb';
    OPEN db_cur;
    FETCH NEXT FROM db_cur INTO @db;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        BEGIN TRY
            DELETE FROM #dbinfo;
            SET @sql = N'DBCC DBINFO(' + QUOTENAME(@db, '''') + N') WITH TABLERESULTS';
            INSERT INTO #dbinfo (ParentObject, Object, Field, Value) EXEC (@sql);
            INSERT INTO #checkdb (db, last_good)
                SELECT @db, MAX(Value) FROM #dbinfo WHERE Field = 'dbi_dbccLastKnownGood';
        END TRY
        BEGIN CATCH
            -- One database refusing must not cost the rest; it is reported as NEVER below, which
            -- is the safe reading: we could not prove integrity for it.
            INSERT INTO #checkdb (db, last_good) VALUES (@db, NULL);
        END CATCH
        FETCH NEXT FROM db_cur INTO @db;
    END
    CLOSE db_cur;
    DEALLOCATE db_cur;

    ;WITH graded AS
    (
        SELECT
            db,
            -- The 1900 sentinel and NULL both mean "no known-good has ever been recorded".
            last_good_at = TRY_CONVERT(datetime, NULLIF(last_good, '1900-01-01 00:00:00.000')),
            raw = last_good
        FROM #checkdb
    )
    SELECT
        CAST(db AS varchar(256)) AS metric_item,
        CAST(ISNULL(CONVERT(varchar(19), last_good_at, 120), 'never') AS varchar(32)) AS metric_value,
        CAST('days_since_checkdb' AS varchar(32)) AS metric_unit,
        CAST(
            CASE
                WHEN last_good_at IS NULL THEN 'WARNING'
                WHEN DATEDIFF(day, last_good_at, GETDATE()) >= @stale_crit_days THEN 'CRITICAL'
                WHEN DATEDIFF(day, last_good_at, GETDATE()) >= @stale_warn_days THEN 'WARNING'
                ELSE 'OK'
            END AS varchar(16)) AS status,
        CAST(
            'db_name=' + db
            + ', last_known_good=' + ISNULL(CONVERT(varchar(19), last_good_at, 120), 'never')
            + ', age_days=' + ISNULL(CAST(DATEDIFF(day, last_good_at, GETDATE()) AS varchar(12)), 'n/a')
            + ', warn_after_days=' + CAST(@stale_warn_days AS varchar(12))
            + ', critical_after_days=' + CAST(@stale_crit_days AS varchar(12))
            + ', issue_type=' + CASE
                WHEN last_good_at IS NULL THEN 'CHECKDB_NEVER'
                WHEN DATEDIFF(day, last_good_at, GETDATE()) >= @stale_warn_days THEN 'CHECKDB_STALE'
                ELSE 'OK' END
            + CASE WHEN last_good_at IS NULL
                   THEN ' - no successful DBCC CHECKDB has ever been recorded for this database,'
                        + ' so corruption would only be found when a query or a restore hits it'
                   ELSE '' END
            AS varchar(4000)) AS message
    FROM graded
    ORDER BY
        CASE WHEN last_good_at IS NULL THEN 0 ELSE 1 END,
        last_good_at ASC,
        db;
END

IF OBJECT_ID('tempdb..#dbinfo') IS NOT NULL DROP TABLE #dbinfo;
IF OBJECT_ID('tempdb..#checkdb') IS NOT NULL DROP TABLE #checkdb;
