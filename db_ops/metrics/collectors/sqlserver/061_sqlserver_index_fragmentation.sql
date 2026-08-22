-- MAINTENANCE_INDEX_FRAGMENTATION (SQL Server): significantly fragmented indexes per database.
-- Uses sys.dm_db_index_physical_stats in LIMITED mode (cheap - reads allocation/parent level
-- only) and reports only indexes with > 1000 pages and >= 30% average fragmentation, so tiny
-- indexes (where fragmentation is irrelevant) are ignored. Status: WARNING at >= 60% (rebuild
-- territory) on the SUMMARY row only; the per-index rows are LOGGING detail. A summary row is always emitted so a clean instance
-- reads OK rather than NO_DATA. Scheduled nightly inside the 01-06 window because it walks every
-- database: on the ERP instance (SALESDB ~1.66 TB) one pass measured 749s, seconds everywhere else.
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#frag') IS NOT NULL DROP TABLE #frag;
CREATE TABLE #frag (db_name sysname, table_name nvarchar(260) NULL, index_name nvarchar(260) NULL,
                    frag float, page_count bigint);

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
        INSERT INTO #frag (db_name, table_name, index_name, frag, page_count)
        SELECT DB_NAME(), OBJECT_NAME(ips.object_id), i.name,
               ips.avg_fragmentation_in_percent, ips.page_count
        FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, ''LIMITED'') AS ips
        JOIN sys.indexes AS i ON i.object_id = ips.object_id AND i.index_id = ips.index_id
        WHERE ips.index_id > 0
          AND ips.page_count > 1000
          AND ips.avg_fragmentation_in_percent >= 30
          AND OBJECTPROPERTY(ips.object_id, ''IsUserTable'') = 1;';
    BEGIN TRY EXEC sys.sp_executesql @sql; END TRY BEGIN CATCH END CATCH;
    FETCH NEXT FROM db_cursor INTO @db;
END
CLOSE db_cursor; DEALLOCATE db_cursor;

SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        CAST(f.db_name + N'\' + ISNULL(f.table_name, N'?') + N'.' + ISNULL(f.index_name, N'?') AS varchar(400)) AS metric_item,
        CAST(CAST(CAST(f.frag AS decimal(5, 1)) AS varchar(16)) AS varchar(64)) AS metric_value,
        CAST('percent' AS varchar(32)) AS metric_unit,
        -- LOGGING, not WARNING: the summary row below already counts the rebuild candidates, and
        -- one warning per index turns a single finding ("31 indexes need a rebuild") into 31 an
        -- operator has to read one by one. 192.0.2.86's neighbour reported 32 of these in a
        -- single pass. The rows stay collected, so "which index" is one query away.
        CAST('LOGGING' AS varchar(16)) AS status,
        CAST(N'page_count=' + CAST(f.page_count AS nvarchar(20))
             + N' | action=' + CASE WHEN f.frag >= 60 THEN N'REBUILD' ELSE N'REORGANIZE' END AS varchar(1000)) AS message,
        CASE WHEN f.frag >= 60 THEN 1 ELSE 2 END AS sort_rank
    FROM #frag AS f

    UNION ALL

    SELECT
        CAST('index_fragmentation :: summary' AS varchar(400)) AS metric_item,
        CAST(CAST((SELECT COUNT(*) FROM #frag) AS varchar(12)) AS varchar(64)) AS metric_value,
        CAST('count' AS varchar(32)) AS metric_unit,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM #frag WHERE frag >= 60) THEN 'WARNING' ELSE 'OK' END AS varchar(16)) AS status,
        CAST(N'fragmented_indexes(>=30%,>1000pg)=' + CAST((SELECT COUNT(*) FROM #frag) AS nvarchar(12))
             + N' rebuild_candidates(>=60%)=' + CAST((SELECT COUNT(*) FROM #frag WHERE frag >= 60) AS nvarchar(12)) AS varchar(1000)) AS message,
        0 AS sort_rank
) AS q
ORDER BY q.sort_rank, q.metric_item;

IF OBJECT_ID('tempdb..#frag') IS NOT NULL DROP TABLE #frag;
