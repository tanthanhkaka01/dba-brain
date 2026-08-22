-- DATABASE_CHECKDB — SQL Server 2008 R2 variant.
--
-- Same two findings, the same thresholds and the same instance-level permission probe as the
-- 2012+ file (see sqlserver/054_sqlserver_database_checkdb.sql). This exists because that file
-- uses one thing 2008 R2 does not have, and it fails the WHOLE batch at compile time — which is
-- why the metric was switched off on 192.0.2.8 and 192.0.2.41 instead of reporting:
--
--   * TRY_CONVERT()  - added in 2012. There is no 2008 R2 expression that converts-or-returns-NULL,
--                      and the obvious rewrite is a trap: a set-based
--                      `CASE WHEN ISDATE(x) = 1 THEN CONVERT(datetime, x, 121) END` does not
--                      guarantee the guard runs before the conversion, so one unparseable value
--                      raises 241 and kills every row. The conversion is therefore done one value
--                      at a time inside the cursor, where IF/SET ordering is guaranteed, and
--                      #checkdb carries the already-converted datetime.
--
-- CONVERT is pinned to style 121 rather than left to the session: dbi_dbccLastKnownGood comes back
-- as the string 'yyyy-mm-dd hh:mm:ss.mmm', and for datetime that form is read through the session
-- LANGUAGE/DATEFORMAT — under dmy the collector would silently read month and day swapped and
-- report an age that is wrong by up to eleven months.
--
-- Everything else (DBCC DBINFO WITH TABLERESULTS, the 1900-01-01 sentinel, the per-database
-- CATCH that costs one database instead of the scan) is 2005-era and carries over unchanged.

SET NOCOUNT ON;

DECLARE @stale_warn_days int = 7;    -- a weekly integrity check is the normal baseline
DECLARE @stale_crit_days int = 30;   -- a schedule that used to work has clearly stopped

IF OBJECT_ID('tempdb..#dbinfo') IS NOT NULL DROP TABLE #dbinfo;
IF OBJECT_ID('tempdb..#checkdb') IS NOT NULL DROP TABLE #checkdb;
CREATE TABLE #dbinfo (ParentObject varchar(255), Object varchar(255), Field varchar(255), Value varchar(255));
CREATE TABLE #checkdb (db sysname, last_good varchar(40) NULL, last_good_at datetime NULL);

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
    DECLARE @db sysname, @sql nvarchar(400), @raw varchar(40), @last_good_at datetime;
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

            SET @raw = (SELECT MAX(Value) FROM #dbinfo WHERE Field = 'dbi_dbccLastKnownGood');

            -- The 1900 sentinel and NULL both mean "no known-good has ever been recorded", and so
            -- does a value this build writes in a shape ISDATE does not accept: all three land as
            -- NULL, which grades as CHECKDB_NEVER — the safe reading when integrity is unproven.
            SET @last_good_at = NULL;
            IF @raw IS NOT NULL AND @raw <> '1900-01-01 00:00:00.000' AND ISDATE(@raw) = 1
                SET @last_good_at = CONVERT(datetime, @raw, 121);

            INSERT INTO #checkdb (db, last_good, last_good_at) VALUES (@db, @raw, @last_good_at);
        END TRY
        BEGIN CATCH
            -- One database refusing must not cost the rest; it is reported as NEVER below, which
            -- is the safe reading: we could not prove integrity for it.
            INSERT INTO #checkdb (db, last_good, last_good_at) VALUES (@db, NULL, NULL);
        END CATCH
        FETCH NEXT FROM db_cur INTO @db;
    END
    CLOSE db_cur;
    DEALLOCATE db_cur;

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
    FROM #checkdb
    ORDER BY
        CASE WHEN last_good_at IS NULL THEN 0 ELSE 1 END,
        last_good_at ASC,
        db;
END

IF OBJECT_ID('tempdb..#dbinfo') IS NOT NULL DROP TABLE #dbinfo;
IF OBJECT_ID('tempdb..#checkdb') IS NOT NULL DROP TABLE #checkdb;
