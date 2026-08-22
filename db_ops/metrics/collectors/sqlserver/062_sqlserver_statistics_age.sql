-- MAINTENANCE_STATISTICS_AGE (SQL Server): stale statistics per database.
-- Reports statistics on user tables (> 1000 rows) that are either old (last updated > 30 days
-- ago, or never) or heavily modified since the last update (modification_counter > 20% of rows).
-- Stale stats drive bad cardinality estimates and poor plans. Detail rows are WARNING; a summary
-- row is always emitted so a well-maintained instance reads OK rather than NO_DATA. Uses
-- sys.dm_db_stats_properties (per-database TRY/CATCH covers very old builds without it).
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#st') IS NOT NULL DROP TABLE #st;
CREATE TABLE #st (db_name sysname, table_name nvarchar(260) NULL, stat_name nvarchar(260) NULL,
                  last_updated datetime NULL, row_count bigint, mods bigint);

DECLARE @db sysname, @sql nvarchar(max);
DECLARE db_cursor CURSOR LOCAL FAST_FORWARD FOR
    SELECT d.name FROM sys.databases AS d
    WHERE d.database_id > 4 AND d.state = 0 AND d.source_database_id IS NULL
      AND d.is_read_only = 0 AND HAS_DBACCESS(d.name) = 1
    ORDER BY d.name;
OPEN db_cursor; FETCH NEXT FROM db_cursor INTO @db;
WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'USE ' + QUOTENAME(@db) + N';
        INSERT INTO #st (db_name, table_name, stat_name, last_updated, row_count, mods)
        SELECT DB_NAME(), OBJECT_NAME(s.object_id), s.name, sp.last_updated, sp.rows, sp.modification_counter
        FROM sys.stats AS s
        CROSS APPLY sys.dm_db_stats_properties(s.object_id, s.stats_id) AS sp
        WHERE OBJECTPROPERTY(s.object_id, ''IsUserTable'') = 1
          AND sp.rows > 1000
          AND (sp.last_updated IS NULL
               OR sp.last_updated < DATEADD(day, -30, GETDATE())
               OR sp.modification_counter > (sp.rows * 0.20));';
    BEGIN TRY EXEC sys.sp_executesql @sql; END TRY BEGIN CATCH END CATCH;
    FETCH NEXT FROM db_cursor INTO @db;
END
CLOSE db_cursor; DEALLOCATE db_cursor;

SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        CAST(t.db_name + N'\' + ISNULL(t.table_name, N'?') + N'.' + ISNULL(t.stat_name, N'?') AS varchar(400)) AS metric_item,
        CAST(ISNULL(CONVERT(varchar(16), t.last_updated, 120), 'never') AS varchar(64)) AS metric_value,
        CAST('date' AS varchar(32)) AS metric_unit,
        CAST('WARNING' AS varchar(16)) AS status,
        CAST(N'rows=' + CAST(t.row_count AS nvarchar(20))
             + N' modifications=' + CAST(t.mods AS nvarchar(20))
             + N' age_days=' + ISNULL(CAST(DATEDIFF(day, t.last_updated, GETDATE()) AS nvarchar(12)), N'inf') AS varchar(1000)) AS message,
        0 AS sort_rank
    FROM #st AS t

    UNION ALL

    SELECT
        CAST('statistics_age :: summary' AS varchar(400)) AS metric_item,
        CAST(CAST((SELECT COUNT(*) FROM #st) AS varchar(12)) AS varchar(64)) AS metric_value,
        CAST('count' AS varchar(32)) AS metric_unit,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM #st) THEN 'WARNING' ELSE 'OK' END AS varchar(16)) AS status,
        CAST(N'stale_statistics(>1000 rows, >30d old or >20% modified)=' + CAST((SELECT COUNT(*) FROM #st) AS nvarchar(12)) AS varchar(1000)) AS message,
        1 AS sort_rank
) AS q
ORDER BY q.sort_rank, q.metric_item;

IF OBJECT_ID('tempdb..#st') IS NOT NULL DROP TABLE #st;
