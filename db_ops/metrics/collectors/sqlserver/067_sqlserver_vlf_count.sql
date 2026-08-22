-- DATABASE_VLF_COUNT (SQL Server): virtual log files per database transaction log.
--
-- This is what "database-level fragmentation" actually means on SQL Server. The log is divided
-- into VLFs, and every autogrowth adds more. A log that grew from 1 MB in small increments over
-- years ends up with tens of thousands of them, and the cost is paid where it hurts most:
--
--   * **recovery and startup** walk every VLF — a database with 20 000 VLFs can take minutes to
--     come online after a failover or restart, while one with 200 takes seconds;
--   * log backups, replication and CDC readers all scan the VLF chain.
--
-- Nothing in this catalog measured it. STORAGE_DATA_FILE_SPACE and LOG_FILE_SPACE report space,
-- which is a different question and stays healthy while the VLF count quietly climbs.
--
-- sys.dm_db_log_info is SQL Server 2016 SP2+ / 2017+; the definition pins min_major_version 13
-- so older instances select the unsupported variant instead of failing. The per-database
-- TRY/CATCH still covers a 2016 build below SP2, where the DMV is absent.
--
-- Thresholds follow the usual guidance: WARNING at 1000 VLFs, CRITICAL at 10000. The message
-- carries the growth setting, because a log in percent-growth or tiny fixed-MB growth is the one
-- that will be back here again after the next rebuild.
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#vlf') IS NOT NULL DROP TABLE #vlf;
CREATE TABLE #vlf (db_name sysname, vlf_count int, log_mb decimal(18, 1),
                   growth_mb decimal(18, 1), is_percent_growth bit);

DECLARE @db sysname, @sql nvarchar(max);
DECLARE db_cursor CURSOR LOCAL FAST_FORWARD FOR
    SELECT d.name FROM sys.databases AS d
    WHERE d.state = 0 AND d.source_database_id IS NULL AND HAS_DBACCESS(d.name) = 1
    ORDER BY d.name;
OPEN db_cursor; FETCH NEXT FROM db_cursor INTO @db;
WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'
        INSERT INTO #vlf (db_name, vlf_count, log_mb, growth_mb, is_percent_growth)
        SELECT @dbname,
               (SELECT COUNT(*) FROM sys.dm_db_log_info(DB_ID(@dbname))),
               CAST(SUM(mf.size) * 8.0 / 1024.0 AS decimal(18, 1)),
               CAST(MAX(CASE WHEN mf.is_percent_growth = 1 THEN mf.growth
                             ELSE mf.growth * 8.0 / 1024.0 END) AS decimal(18, 1)),
               MAX(CAST(mf.is_percent_growth AS int))
        FROM sys.master_files AS mf
        WHERE mf.database_id = DB_ID(@dbname) AND mf.type_desc = ''LOG'';';
    BEGIN TRY EXEC sys.sp_executesql @sql, N'@dbname sysname', @dbname = @db; END TRY BEGIN CATCH END CATCH;
    FETCH NEXT FROM db_cursor INTO @db;
END
CLOSE db_cursor; DEALLOCATE db_cursor;

SELECT
    CAST(v.db_name AS varchar(400)) AS metric_item,
    CAST(CAST(v.vlf_count AS varchar(16)) AS varchar(64)) AS metric_value,
    CAST('count' AS varchar(32)) AS metric_unit,
    CAST(CASE WHEN v.vlf_count >= 10000 THEN 'CRITICAL'
              WHEN v.vlf_count >= 1000 THEN 'WARNING'
              ELSE 'OK' END AS varchar(16)) AS status,
    CAST(N'vlf_count=' + CAST(v.vlf_count AS nvarchar(16))
         + N', log_mb=' + CAST(v.log_mb AS nvarchar(24))
         + N', growth=' + CAST(v.growth_mb AS nvarchar(24))
         + CASE WHEN v.is_percent_growth = 1 THEN N'%' ELSE N' MB' END
         + CASE WHEN v.vlf_count >= 1000
                THEN N' | action=back up the log, shrink it, then regrow in one step with a fixed growth'
                ELSE N'' END AS varchar(1000)) AS message
FROM #vlf AS v
ORDER BY v.vlf_count DESC, v.db_name;

IF OBJECT_ID('tempdb..#vlf') IS NOT NULL DROP TABLE #vlf;
