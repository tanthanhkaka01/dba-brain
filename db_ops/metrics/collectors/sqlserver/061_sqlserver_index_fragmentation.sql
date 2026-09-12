-- MAINTENANCE_INDEX_FRAGMENTATION (SQL Server): significantly fragmented indexes per database.
-- Uses sys.dm_db_index_physical_stats in LIMITED mode (cheap - reads allocation/parent level
-- only) and reports only indexes with > 1000 pages and >= 30% average fragmentation, so tiny
-- indexes (where fragmentation is irrelevant) are ignored. Status: WARNING at >= 60% (rebuild
-- territory) on the SUMMARY row only; the per-index rows are LOGGING detail. A summary row is always emitted so a clean instance
-- reads OK rather than NO_DATA. Scheduled nightly inside the 01-06 window because it walks every
-- database: on the ERP instance (SALESDB ~1.66 TB) one pass measured 749s, seconds everywhere else.
--
-- ONE ROW PER PARTITION, and the row says which (2026-09-10). dm_db_index_physical_stats returns
-- a row per partition, and this collector used to key them all on 'db\table.index'. On a
-- date-partitioned timekeeping table that published the same index name fourteen times at
-- fourteen different percentages, which reads as a duplicate and is really the whole finding:
-- twelve partitions were at 99% and the maintenance job had never touched one of them. The
-- partition is now part of the identity, but only for indexes that actually have more than one -
-- an unpartitioned index keeps the name it has always had, so its history does not restart.
--
-- LIMITED mode is a deliberate ceiling on what this row can carry. record_count and
-- avg_page_space_used_in_percent (page density) are NULL in LIMITED and need SAMPLED, which on
-- this estate turns a nightly pass into an hours-long scan - one SAMPLED call against a single
-- 13M-row partitioned table measured over two minutes. They are left out rather than bought at
-- that price; STATS_DATE is free and answers the neighbouring question, so it is here.
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#frag') IS NOT NULL DROP TABLE #frag;
CREATE TABLE #frag (db_name sysname, schema_name sysname NULL, table_name nvarchar(260) NULL,
                    index_name nvarchar(260) NULL, index_type nvarchar(60) NULL,
                    partition_number int NULL, partition_count int NULL,
                    frag float, page_count bigint, stats_updated datetime NULL);

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
        INSERT INTO #frag (db_name, schema_name, table_name, index_name, index_type,
                           partition_number, partition_count, frag, page_count, stats_updated)
        SELECT DB_NAME(), SCHEMA_NAME(o.schema_id), OBJECT_NAME(ips.object_id), i.name,
               ips.index_type_desc, ips.partition_number, pc.n,
               ips.avg_fragmentation_in_percent, ips.page_count,
               STATS_DATE(i.object_id, i.index_id)
        FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, ''LIMITED'') AS ips
        JOIN sys.indexes AS i ON i.object_id = ips.object_id AND i.index_id = ips.index_id
        JOIN sys.objects AS o ON o.object_id = ips.object_id
        CROSS APPLY (SELECT COUNT(*) AS n FROM sys.partitions AS p
                     WHERE p.object_id = i.object_id AND p.index_id = i.index_id) AS pc
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
        -- The partition suffix only appears on an index that has more than one, so the identity
        -- of every unpartitioned index - and its collected history - is unchanged.
        CAST(f.db_name + N'\' + ISNULL(f.schema_name, N'?') + N'.' + ISNULL(f.table_name, N'?')
             + N'.' + ISNULL(f.index_name, N'?')
             + CASE WHEN ISNULL(f.partition_count, 1) > 1
                    THEN N'#p' + CAST(f.partition_number AS nvarchar(12)) ELSE N'' END
             AS varchar(400)) AS metric_item,
        CAST(CAST(CAST(f.frag AS decimal(5, 1)) AS varchar(16)) AS varchar(64)) AS metric_value,
        CAST('percent' AS varchar(32)) AS metric_unit,
        -- LOGGING, not WARNING: the summary row below already counts the rebuild candidates, and
        -- one warning per index turns a single finding ("31 indexes need a rebuild") into 31 an
        -- operator has to read one by one. 192.0.2.86's neighbour reported 32 of these in a
        -- single pass. The rows stay collected, so "which index" is one query away.
        CAST('LOGGING' AS varchar(16)) AS status,
        -- Read back by db_ops.reports.index_report._kv, so every field is a k=v pair. size_mb is
        -- derived rather than measured: page_count * 8 KB is what makes "99% fragmented" mean
        -- something, because the cost of the rebuild is the size and nothing else on this row is.
        CAST(N'db=' + f.db_name
             + N' | schema=' + ISNULL(f.schema_name, N'?')
             + N' | table=' + ISNULL(f.table_name, N'?')
             + N' | index=' + ISNULL(f.index_name, N'?')
             + N' | index_type=' + ISNULL(f.index_type, N'?')
             + N' | partition=' + CAST(f.partition_number AS nvarchar(12))
             + N'/' + CAST(ISNULL(f.partition_count, 1) AS nvarchar(12))
             + N' | page_count=' + CAST(f.page_count AS nvarchar(20))
             + N' | size_mb=' + CAST(CAST(f.page_count * 8.0 / 1024 AS decimal(12, 1)) AS nvarchar(24))
             + N' | stats_updated=' + ISNULL(CONVERT(nvarchar(19), f.stats_updated, 126), N'never')
             + N' | action=' + CASE WHEN f.frag >= 60 THEN N'REBUILD' ELSE N'REORGANIZE' END
             AS varchar(1000)) AS message,
        CASE WHEN f.frag >= 60 THEN 1 ELSE 2 END AS sort_rank
    FROM #frag AS f

    UNION ALL

    SELECT
        CAST('index_fragmentation :: summary' AS varchar(400)) AS metric_item,
        CAST(CAST((SELECT COUNT(*) FROM #frag) AS varchar(12)) AS varchar(64)) AS metric_value,
        CAST('count' AS varchar(32)) AS metric_unit,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM #frag WHERE frag >= 60) THEN 'WARNING' ELSE 'OK' END AS varchar(16)) AS status,
        -- The partitioned count is here because "227 fragmented indexes" and "227 fragmented
        -- partitions of 40 indexes" are different findings and the first one is the wrong one.
        CAST(N'fragmented_indexes(>=30%,>1000pg)=' + CAST((SELECT COUNT(*) FROM #frag) AS nvarchar(12))
             + N' rebuild_candidates(>=60%)=' + CAST((SELECT COUNT(*) FROM #frag WHERE frag >= 60) AS nvarchar(12))
             + N' distinct_indexes=' + CAST((SELECT COUNT(DISTINCT CAST(db_name AS nvarchar(300)) + N'.'
                   + ISNULL(schema_name, N'?') + N'.' + ISNULL(table_name, N'?') + N'.'
                   + ISNULL(index_name, N'?')) FROM #frag) AS nvarchar(12))
             + N' total_mb=' + CAST(CAST((SELECT ISNULL(SUM(page_count), 0) * 8.0 / 1024 FROM #frag) AS decimal(12, 1)) AS nvarchar(24))
             AS varchar(1000)) AS message,
        0 AS sort_rank
) AS q
ORDER BY q.sort_rank, q.metric_item;

IF OBJECT_ID('tempdb..#frag') IS NOT NULL DROP TABLE #frag;
