-- MAINTENANCE_HEAP_FRAGMENTATION (SQL Server): heaps that need attention.
--
-- 061 (index fragmentation) filters `index_id > 0`, which excludes heaps entirely — a table with
-- no clustered index was never measured by anything in this catalog. Heaps are where the two
-- worst shapes hide:
--
--   * **forwarded records** — an UPDATE that grows a row past its page leaves a pointer behind,
--     and every read of that row costs a second IO. The count only ever goes up until the heap is
--     rebuilt; there is no automatic cleanup.
--   * **empty space** — a heap never reclaims deleted pages for other objects, so a table that
--     was purged still occupies what it occupied at its peak.
--
-- Why this is its own metric and not a filter change in 061: forwarded_record_count is NULL in
-- LIMITED mode, which is the cheap mode 061 relies on to walk every database nightly. Getting it
-- needs SAMPLED, which reads 1% of pages — much heavier. Bolting that onto 061 would have made
-- the working scan slow for everyone; a separate metric can carry its own schedule.
--
-- Reports heaps over 1000 pages only (below that neither problem is worth an outage window).
-- WARNING when forwarded records exceed 10% of rows, or free space exceeds 30% of a heap larger
-- than 100 MB. A summary row is always emitted so a clean instance reads OK, not NO_DATA.
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#heap') IS NOT NULL DROP TABLE #heap;
CREATE TABLE #heap (db_name sysname, table_name nvarchar(260) NULL, page_count bigint,
                    record_count bigint, forwarded bigint, avg_space_used float);

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
        INSERT INTO #heap (db_name, table_name, page_count, record_count, forwarded, avg_space_used)
        SELECT DB_NAME(), OBJECT_NAME(ips.object_id), ips.page_count, ips.record_count,
               ISNULL(ips.forwarded_record_count, 0), ips.avg_page_space_used_in_percent
        FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, 0, NULL, ''SAMPLED'') AS ips
        WHERE ips.index_id = 0
          AND ips.page_count > 1000
          AND ips.alloc_unit_type_desc = ''IN_ROW_DATA''
          AND OBJECTPROPERTY(ips.object_id, ''IsUserTable'') = 1;';
    BEGIN TRY EXEC sys.sp_executesql @sql; END TRY BEGIN CATCH END CATCH;
    FETCH NEXT FROM db_cursor INTO @db;
END
CLOSE db_cursor; DEALLOCATE db_cursor;

SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        CAST(h.db_name + N'\' + ISNULL(h.table_name, N'?') AS varchar(400)) AS metric_item,
        CAST(CAST(CAST(CASE WHEN h.record_count > 0
                            THEN h.forwarded * 100.0 / h.record_count ELSE 0 END
                       AS decimal(5, 1)) AS varchar(16)) AS varchar(64)) AS metric_value,
        CAST('percent' AS varchar(32)) AS metric_unit,
        CAST(CASE
                WHEN h.record_count > 0 AND h.forwarded * 100.0 / h.record_count >= 10 THEN 'WARNING'
                WHEN h.page_count > 12800 AND h.avg_space_used < 70 THEN 'WARNING'
                ELSE 'OK'
             END AS varchar(16)) AS status,
        CAST(N'forwarded_records=' + CAST(h.forwarded AS nvarchar(20))
             + N', rows=' + CAST(h.record_count AS nvarchar(20))
             + N', pages=' + CAST(h.page_count AS nvarchar(20))
             + N', size_mb=' + CAST(CAST(h.page_count * 8.0 / 1024.0 AS decimal(18, 1)) AS nvarchar(20))
             + N', page_fullness_pct=' + CAST(CAST(h.avg_space_used AS decimal(5, 1)) AS nvarchar(16))
             + N' | action=ALTER TABLE ... REBUILD (or add a clustered index)' AS varchar(1000)) AS message,
        CASE WHEN h.record_count > 0 AND h.forwarded * 100.0 / h.record_count >= 10 THEN 0 ELSE 1 END AS sort_rank
    FROM #heap AS h
    WHERE (h.record_count > 0 AND h.forwarded * 100.0 / h.record_count >= 10)
       OR (h.page_count > 12800 AND h.avg_space_used < 70)

    UNION ALL

    SELECT
        CAST('heap_fragmentation :: summary' AS varchar(400)) AS metric_item,
        CAST(CAST((SELECT COUNT(*) FROM #heap) AS varchar(12)) AS varchar(64)) AS metric_value,
        CAST('count' AS varchar(32)) AS metric_unit,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM #heap
                               WHERE record_count > 0 AND forwarded * 100.0 / record_count >= 10)
                  THEN 'WARNING' ELSE 'OK' END AS varchar(16)) AS status,
        CAST(N'heaps_scanned(>1000pg)=' + CAST((SELECT COUNT(*) FROM #heap) AS nvarchar(12))
             + N' forwarded_over_10pct=' + CAST((SELECT COUNT(*) FROM #heap
                                                 WHERE record_count > 0
                                                   AND forwarded * 100.0 / record_count >= 10) AS nvarchar(12))
             + N' | sampled mode (1% of pages)' AS varchar(1000)) AS message,
        2 AS sort_rank
) AS q
ORDER BY q.sort_rank, q.metric_item;

IF OBJECT_ID('tempdb..#heap') IS NOT NULL DROP TABLE #heap;
