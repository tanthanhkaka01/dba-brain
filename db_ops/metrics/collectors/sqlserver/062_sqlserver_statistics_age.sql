-- MAINTENANCE_STATISTICS_AGE (SQL Server): stale statistics per database.
-- Reports statistics on user tables (> 1000 rows) that are either old (last updated > 30 days
-- ago, or never) or heavily modified since the last update (modification_counter > 20% of rows).
-- Stale stats drive bad cardinality estimates and poor plans. Uses sys.dm_db_stats_properties
-- (per-database TRY/CATCH covers very old builds without it).
--
-- Three shapes of row, and the split is the point. Detail rows are LOGGING, one per statistics
-- object, kept because "which object" has to be one query away. A per-database row rolls them up.
-- An instance summary is the one that grades, so a finding is one line rather than thousands.
--
-- Detail used to be WARNING, which is how a real finding became unreadable: 192.0.2.115 returned
-- 4,261 WARNINGs in a single pass on 2026-08-24 — 1,093 objects over 90 days old, the oldest 953
-- days, and 2,894 stale *and* modified since. Nobody reads 4,261 warnings, and the one number
-- that mattered (NUMBERSEQUENCELIST: 330,110 rows, 15,642,437 modifications, 36 days old, and
-- named in that instance's blocking chains) was in the middle of them. This is the same split
-- MAINTENANCE_INDEX_FRAGMENTATION already uses, for the same reason.
--
-- The message carries age bands because "stale" alone hides the difference between a schedule
-- that slipped a week and one that stopped two years ago.
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#st') IS NOT NULL DROP TABLE #st;
IF OBJECT_ID('tempdb..#worst') IS NOT NULL DROP TABLE #worst;
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

-- The worst object per database, resolved up front. Naming it inside the summary query below as
-- MAX(CASE WHEN mods = (SELECT MAX(mods) ...)) is the obvious way to write it and T-SQL refuses
-- it: "Cannot perform an aggregate function on an expression containing an aggregate or a
-- subquery" (error 130). Ties break on the larger table, because a huge modification count
-- against many rows is a bigger cardinality lie than the same count against few.
IF OBJECT_ID('tempdb..#worst') IS NOT NULL DROP TABLE #worst;
CREATE TABLE #worst (db_name sysname PRIMARY KEY, max_mods bigint NULL,
                     top_table nvarchar(260) NULL, top_rows bigint NULL);
INSERT INTO #worst (db_name, max_mods) SELECT db_name, MAX(mods) FROM #st GROUP BY db_name;
UPDATE w SET top_table = x.table_name, top_rows = x.row_count
FROM #worst AS w
CROSS APPLY (SELECT TOP (1) t.table_name, t.row_count FROM #st AS t
             WHERE t.db_name = w.db_name AND t.mods = w.max_mods
             ORDER BY t.row_count DESC) AS x;

SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        CAST(t.db_name + N'\' + ISNULL(t.table_name, N'?') + N'.' + ISNULL(t.stat_name, N'?') AS varchar(400)) AS metric_item,
        CAST(ISNULL(CONVERT(varchar(16), t.last_updated, 120), 'never') AS varchar(64)) AS metric_value,
        CAST('date' AS varchar(32)) AS metric_unit,
        -- LOGGING, not WARNING: the summary rows below carry the count, and one warning per
        -- statistics object turns one finding into thousands. See the header.
        CAST('LOGGING' AS varchar(16)) AS status,
        CAST(N'rows=' + CAST(t.row_count AS nvarchar(20))
             + N' modifications=' + CAST(t.mods AS nvarchar(20))
             + N' age_days=' + ISNULL(CAST(DATEDIFF(day, t.last_updated, GETDATE()) AS nvarchar(12)), N'inf') AS varchar(1000)) AS message,
        2 AS sort_rank
    FROM #st AS t

    UNION ALL

    -- One row per database. Statistics maintenance is scheduled per database, so this is the
    -- grain someone actually acts on: it names the database to run the update against.
    SELECT
        CAST('statistics_age :: ' + d.db_name AS varchar(400)) AS metric_item,
        CAST(CAST(COUNT(*) AS varchar(12)) AS varchar(64)) AS metric_value,
        CAST('count' AS varchar(32)) AS metric_unit,
        CAST('LOGGING' AS varchar(16)) AS status,
        CAST(N'stale=' + CAST(COUNT(*) AS nvarchar(12))
             + N' over_90d=' + CAST(SUM(CASE WHEN d.last_updated IS NULL
                                              OR DATEDIFF(day, d.last_updated, GETDATE()) > 90
                                             THEN 1 ELSE 0 END) AS nvarchar(12))
             + N' stale_and_modified=' + CAST(SUM(CASE WHEN d.mods > 0 AND (d.last_updated IS NULL
                                              OR DATEDIFF(day, d.last_updated, GETDATE()) > 30)
                                             THEN 1 ELSE 0 END) AS nvarchar(12))
             + N' oldest_days=' + ISNULL(CAST(MAX(DATEDIFF(day, d.last_updated, GETDATE())) AS nvarchar(12)), N'inf')
             + N' most_modified=' + ISNULL(MIN(w.top_table), N'?')
             + N'(' + ISNULL(CAST(MIN(w.max_mods) AS nvarchar(20)), N'0') + N' mods/'
             + ISNULL(CAST(MIN(w.top_rows) AS nvarchar(20)), N'0') + N' rows)'
             AS varchar(1000)) AS message,
        1 AS sort_rank
    FROM #st AS d
    INNER JOIN #worst AS w ON w.db_name = d.db_name
    GROUP BY d.db_name

    UNION ALL

    -- The row that grades. Everything above it is evidence for this one line.
    SELECT
        CAST('statistics_age :: summary' AS varchar(400)) AS metric_item,
        CAST(CAST((SELECT COUNT(*) FROM #st) AS varchar(12)) AS varchar(64)) AS metric_value,
        CAST('count' AS varchar(32)) AS metric_unit,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM #st) THEN 'WARNING' ELSE 'OK' END AS varchar(16)) AS status,
        CAST(N'stale_statistics(>1000 rows, >30d old or >20% modified)=' + CAST((SELECT COUNT(*) FROM #st) AS nvarchar(12))
             + N' over_90d=' + CAST((SELECT COUNT(*) FROM #st
                                     WHERE last_updated IS NULL OR DATEDIFF(day, last_updated, GETDATE()) > 90) AS nvarchar(12))
             + N' stale_and_modified=' + CAST((SELECT COUNT(*) FROM #st
                                     WHERE mods > 0 AND (last_updated IS NULL OR DATEDIFF(day, last_updated, GETDATE()) > 30)) AS nvarchar(12))
             + N' oldest_days=' + ISNULL(CAST((SELECT MAX(DATEDIFF(day, last_updated, GETDATE())) FROM #st) AS nvarchar(12)), N'inf')
             + N' databases=' + CAST((SELECT COUNT(DISTINCT db_name) FROM #st) AS nvarchar(12))
             AS varchar(1000)) AS message,
        0 AS sort_rank
) AS q
ORDER BY q.sort_rank, q.metric_item;

IF OBJECT_ID('tempdb..#st') IS NOT NULL DROP TABLE #st;
IF OBJECT_ID('tempdb..#worst') IS NOT NULL DROP TABLE #worst;
