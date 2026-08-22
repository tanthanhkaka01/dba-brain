-- MAINTENANCE_INDEX_USAGE (SQL Server): a full per-index inventory with its usage counters, plus
-- the indexes the optimizer keeps asking for.
--
-- Fragmentation (061) answers "are the indexes I have healthy?". This answers the question
-- underneath it: **should these indexes exist at all**. An unused index is paid for on every
-- INSERT/UPDATE/DELETE, is rebuilt in every maintenance window, and is backed up forever — and
-- an index nobody reads is the cheapest thing a DBA will ever delete.
--
-- **Every index is reported, not just the suspicious ones**, and the counters are reported
-- SEPARATELY (seeks / scans / lookups / updates) rather than summed into one "reads" number. A
-- sum hides the distinction that decides what to do: an index serving 2 million seeks is a lookup
-- path worth keeping, one serving 2 million scans is usually a missing index somewhere else.
--
-- The join to sys.dm_db_index_usage_stats is a **LEFT** join. An index that has been neither read
-- nor written since the last restart has no row in that DMV at all, so an inner join makes it
-- invisible — and those are the most droppable indexes there are. On one SALESDB, 24,787 of 29,022
-- indexes had no usage row whatsoever. `kind=COLD` is exactly that population.
--
-- **Both counters reset when the instance restarts**, and that is the trap this metric has to
-- state rather than hide: an index looks unused because SQL Server has only been up for two
-- hours, or because the month-end report that uses it has not run yet. Every row carries the
-- uptime the sample covers.
--
-- Two thresholds, not one, because "is there any signal" and "is the signal safe to act on" are
-- different questions:
--
--   * **Below 12 hours** the detail is suppressed outright. A sample that short says nothing
--     about anything; the summary still reports totals so the page is not silently empty.
--   * **Between 12 hours and 7 days** the detail IS reported, and every DROP recommendation is
--     explicitly qualified as drawn from a short sample. This window is for looking, not for
--     acting: a weekly report, a month-end job or a quarterly batch has not run yet, so an index
--     serving one of them still reads as COLD.
--
-- The suppression used to start at 7 days, which meant an instance that had just failed over
-- showed nothing at all for a week. Lowering the gate is what makes the inventory readable
-- sooner; keeping the qualifier is what stops that readability turning into a wrong DROP.
-- Status stays OK throughout: this is an inventory for a review, not an alert.
--
-- Volume: this is deliberately uncapped, so one large database contributes as many rows as it has
-- indexes (~29k for SALESDB). That is the cost of a complete inventory and it was chosen knowingly.
SET NOCOUNT ON;

-- Uptime is carried in hours as well as days: the gates are 12 hours and 7 days, and comparing
-- 12 hours against a decimal(10,1) day count is a rounding argument nobody should have to have.
DECLARE @uptime_hours int =
    (SELECT DATEDIFF(hour, sqlserver_start_time, GETDATE()) FROM sys.dm_os_sys_info);
DECLARE @uptime_days decimal(10, 1) = CAST(@uptime_hours / 24.0 AS decimal(10, 1));

-- The detail gate, and the "do not act on this yet" gate. See the header.
DECLARE @detail_min_hours int = 12;
DECLARE @trusted_min_hours int = 168;   -- 7 days: one full business week
DECLARE @short_sample bit = CASE WHEN @uptime_hours < @trusted_min_hours THEN 1 ELSE 0 END;

-- The instant every usage counter below was last zeroed. Reported explicitly because
-- "user_seeks = 0" means nothing without it: the number is not "never used", it is "not used
-- SINCE THIS MOMENT", and an operator judging a drop candidate needs to see which moment.
DECLARE @restarted_at nvarchar(19) =
    (SELECT CONVERT(nvarchar(19), sqlserver_start_time, 120) FROM sys.dm_os_sys_info);

IF OBJECT_ID('tempdb..#usage') IS NOT NULL DROP TABLE #usage;
CREATE TABLE #usage
(
    kind         varchar(12),
    db_name      sysname,
    schema_name  sysname NULL,
    table_name   nvarchar(260) NULL,
    index_name   nvarchar(400) NULL,
    index_id     int NULL,
    type_desc    nvarchar(60) NULL,
    is_unique    bit NULL,
    is_primary   bit NULL,
    is_uq_constr bit NULL,
    is_disabled  bit NULL,
    has_filter   bit NULL,
    seeks        bigint NULL,
    scans        bigint NULL,
    lookups      bigint NULL,
    writes       bigint NULL,
    last_read    datetime NULL,
    stats_date   datetime NULL,
    impact       decimal(18, 2) NULL
);

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
        INSERT INTO #usage (kind, db_name, schema_name, table_name, index_name, index_id,
                            type_desc, is_unique, is_primary, is_uq_constr, is_disabled, has_filter,
                            seeks, scans, lookups, writes, last_read, stats_date)
        SELECT
            CASE
                WHEN us.object_id IS NULL THEN ''COLD''
                WHEN COALESCE(us.user_seeks,0) + COALESCE(us.user_scans,0)
                   + COALESCE(us.user_lookups,0) = 0 THEN ''UNUSED''
                ELSE ''USED''
            END,
            DB_NAME(), s.name, o.name, i.name, i.index_id, i.type_desc,
            i.is_unique, i.is_primary_key, i.is_unique_constraint, i.is_disabled, i.has_filter,
            COALESCE(us.user_seeks, 0), COALESCE(us.user_scans, 0),
            COALESCE(us.user_lookups, 0), COALESCE(us.user_updates, 0),
            -- The most recent of the three read timestamps: "when did anything last use this?"
            (SELECT MAX(v) FROM (VALUES (us.last_user_seek), (us.last_user_scan),
                                        (us.last_user_lookup)) AS t(v)),
            -- When the statistics for this index were last refreshed. A DIFFERENT clock from the
            -- usage counters above: those accumulate since the instance restarted, while this is
            -- set by UPDATE STATISTICS / auto-update. An index can be heavily used and still carry
            -- statistics from months ago, which is how a good index still produces a bad plan.
            STATS_DATE(i.object_id, i.index_id)
        FROM sys.indexes AS i
        JOIN sys.objects AS o ON o.object_id = i.object_id
                             AND o.type = ''U'' AND o.is_ms_shipped = 0
        JOIN sys.schemas AS s ON s.schema_id = o.schema_id
        -- LEFT: an index never read and never written has NO row here, and that is the finding.
        LEFT JOIN sys.dm_db_index_usage_stats AS us
               ON us.database_id = DB_ID() AND us.object_id = i.object_id
              AND us.index_id = i.index_id
        WHERE i.index_id > 0;          -- index_id 0 is the heap itself, not an index

        INSERT INTO #usage (kind, db_name, schema_name, table_name, index_name, writes, impact)
        SELECT TOP 20 ''MISSING'', DB_NAME(),
               OBJECT_SCHEMA_NAME(id.object_id), OBJECT_NAME(id.object_id),
               ISNULL(id.equality_columns, N'''')
                 + CASE WHEN id.inequality_columns IS NULL THEN N'''' ELSE N'' | '' + id.inequality_columns END
                 + CASE WHEN id.included_columns IS NULL THEN N'''' ELSE N'' INCLUDE '' + id.included_columns END,
               gs.user_seeks + gs.user_scans,
               CAST(gs.avg_total_user_cost * gs.avg_user_impact * (gs.user_seeks + gs.user_scans) AS decimal(18, 2))
        FROM sys.dm_db_missing_index_groups AS ig
        JOIN sys.dm_db_missing_index_group_stats AS gs ON gs.group_handle = ig.index_group_handle
        JOIN sys.dm_db_missing_index_details AS id ON id.index_handle = ig.index_handle
        WHERE id.database_id = DB_ID()
        ORDER BY 7 DESC;';
    BEGIN TRY EXEC sys.sp_executesql @sql; END TRY BEGIN CATCH END CATCH;
    FETCH NEXT FROM db_cursor INTO @db;
END
CLOSE db_cursor; DEALLOCATE db_cursor;

-- Below the detail gate the DMVs have seen essentially nothing. Keep the summary, drop the
-- detail, so the operator learns the sample is too short instead of acting on it. Above it the
-- detail is reported and every DROP line says how long the sample is (see @short_sample below).
IF @uptime_hours < @detail_min_hours
    DELETE FROM #usage WHERE kind <> 'MISSING';

SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    -- One row per index. metric_item carries the full path so the table an index sits on is
    -- readable without parsing the message: db.schema.table.index
    SELECT
        CAST(u.db_name + N'.' + ISNULL(u.schema_name, N'?') + N'.' + ISNULL(u.table_name, N'?')
             + N'.' + ISNULL(u.index_name, N'(heap)') AS varchar(400)) AS metric_item,
        CAST(CAST(ISNULL(u.writes, 0) AS varchar(20)) AS varchar(64)) AS metric_value,
        CAST(CASE WHEN u.kind = 'MISSING' THEN 'impact' ELSE 'user_updates' END AS varchar(32)) AS metric_unit,
        CAST(CASE
                WHEN u.is_disabled = 1 AND u.type_desc = N'CLUSTERED' THEN 'CRITICAL'
                WHEN u.is_disabled = 1 THEN 'WARNING'
                ELSE 'OK'
             END AS varchar(16)) AS status,
        CAST(CASE u.kind
                WHEN 'MISSING' THEN
                     N'MISSING: table=' + ISNULL(u.schema_name, N'?') + N'.' + ISNULL(u.table_name, N'?')
                     + N', columns=' + ISNULL(u.index_name, N'?')
                     + N', estimated_impact=' + CAST(ISNULL(u.impact, 0) AS nvarchar(24))
                     + N', seeks=' + CAST(ISNULL(u.writes, 0) AS nvarchar(20))
                     + N', uptime_days=' + CAST(@uptime_days AS nvarchar(16))
                     + N' | action=evaluate before creating; the optimizer suggests columns, not indexes'
                ELSE
                     u.kind
                     + N': db=' + u.db_name
                     + N', schema=' + ISNULL(u.schema_name, N'?')
                     + N', table=' + ISNULL(u.table_name, N'?')
                     + N', index_name=' + ISNULL(u.index_name, N'(heap)')
                     + N', index_id=' + CAST(ISNULL(u.index_id, 0) AS nvarchar(12))
                     + N', type_desc=' + ISNULL(u.type_desc, N'?')
                     + N', is_unique=' + CAST(ISNULL(u.is_unique, 0) AS nvarchar(1))
                     + N', is_disabled=' + CAST(ISNULL(u.is_disabled, 0) AS nvarchar(1))
                     + N', has_filter=' + CAST(ISNULL(u.has_filter, 0) AS nvarchar(1))
                     + N', user_seeks=' + CAST(ISNULL(u.seeks, 0) AS nvarchar(20))
                     + N', user_scans=' + CAST(ISNULL(u.scans, 0) AS nvarchar(20))
                     + N', user_lookups=' + CAST(ISNULL(u.lookups, 0) AS nvarchar(20))
                     + N', user_updates=' + CAST(ISNULL(u.writes, 0) AS nvarchar(20))
                     + N', last_read=' + ISNULL(CONVERT(nvarchar(19), u.last_read, 120), N'never')
                     + N', last_stats_update=' + ISNULL(CONVERT(nvarchar(19), u.stats_date, 120), N'never')
                     + N', uptime_days=' + CAST(@uptime_days AS nvarchar(16))
                     + N', is_primary_key=' + CAST(ISNULL(u.is_primary, 0) AS nvarchar(1))
                     + N', is_unique_constraint=' + CAST(ISNULL(u.is_uq_constr, 0) AS nvarchar(1))
                     -- The advice is per category, because "unused" means different things.
                     -- A primary key, a unique constraint and the clustered index are structure:
                     -- they can show zero seeks for years and still be doing their job, and a
                     -- DROP suggestion on one of them is a wrong instruction sitting in a report.
                     + CASE
                         WHEN u.is_disabled = 1 AND u.type_desc = N'CLUSTERED'
                             THEN N' | DISABLED CLUSTERED INDEX: the whole table is inaccessible until this is rebuilt; action=ALTER INDEX ... REBUILD now'
                         WHEN u.is_disabled = 1
                             THEN N' | disabled: the index structure is gone, only the definition remains; action=REBUILD to restore it, or DROP if it is not wanted'
                         WHEN u.is_primary = 1
                             THEN N' | primary key: enforces uniqueness and is usually the clustered structure; action=KEEP (low reads are normal)'
                         WHEN u.is_uq_constr = 1
                             THEN N' | unique constraint: this is a rule, not a lookup path; action=KEEP'
                         WHEN u.type_desc = N'CLUSTERED'
                             THEN N' | clustered index: this IS the table storage; action=KEEP'
                         WHEN u.is_unique = 1
                             THEN N' | unique index: may be relied on for correctness, not speed; action=review carefully before DROP'
                         -- These two are the only lines in this metric that tell someone to
                         -- delete something, so they are the only ones the sample length can
                         -- make dangerous. Below a full business week the qualifier is part of
                         -- the recommendation, not a footnote somewhere else on the page.
                         WHEN u.kind = 'COLD'
                             THEN N' | no row in dm_db_index_usage_stats: never read AND never written since restart; action=review, then DROP'
                                  + CASE WHEN @short_sample = 1 THEN N' — BUT the sample covers only '
                                       + CAST(@uptime_days AS nvarchar(16)) + N' day(s) since ' + @restarted_at
                                       + N': a weekly or month-end query has not necessarily run yet. Do not drop on this sample alone.'
                                    ELSE N'' END
                         WHEN u.kind = 'UNUSED'
                             THEN N' | written but never read; action=review, then DROP'
                                  + CASE WHEN @short_sample = 1 THEN N' — BUT the sample covers only '
                                       + CAST(@uptime_days AS nvarchar(16)) + N' day(s) since ' + @restarted_at
                                       + N': a weekly or month-end query has not necessarily run yet. Do not drop on this sample alone.'
                                    ELSE N'' END
                         ELSE N'' END
             END AS varchar(2000)) AS message,
        CASE u.kind WHEN 'UNUSED' THEN 1 WHEN 'COLD' THEN 2 WHEN 'USED' THEN 3 ELSE 4 END AS sort_rank
    FROM #usage AS u

    UNION ALL

    -- Per-database counts, so the shape of each database is readable without adding up its rows.
    SELECT
        CAST(t.db_name + N' :: index_usage summary' AS varchar(400)) AS metric_item,
        CAST(CAST(COUNT(*) AS varchar(12)) AS varchar(64)) AS metric_value,
        -- 'summary' rather than 'indexes': the unit is what the reports key on to tell an
        -- aggregate row apart from the ~29k per-index detail rows, so it has to be unambiguous.
        CAST('summary' AS varchar(32)) AS metric_unit,
        CAST('OK' AS varchar(16)) AS status,
        CAST(N'db=' + t.db_name
             + N', indexes_total=' + CAST(COUNT(*) AS nvarchar(12))
             + N', used=' + CAST(SUM(CASE WHEN t.kind = 'USED' THEN 1 ELSE 0 END) AS nvarchar(12))
             + N', unused=' + CAST(SUM(CASE WHEN t.kind = 'UNUSED' THEN 1 ELSE 0 END) AS nvarchar(12))
             + N', cold=' + CAST(SUM(CASE WHEN t.kind = 'COLD' THEN 1 ELSE 0 END) AS nvarchar(12))
             + N', disabled=' + CAST(SUM(CASE WHEN t.is_disabled = 1 THEN 1 ELSE 0 END) AS nvarchar(12))
             + N', disabled_clustered=' + CAST(SUM(CASE WHEN t.is_disabled = 1 AND t.type_desc = N'CLUSTERED' THEN 1 ELSE 0 END) AS nvarchar(12))
             -- The real drop candidates: nonclustered, not unique, not enforcing a constraint,
             -- and never read. Everything excluded here is excluded because dropping it would
             -- break something, not because it is being used.
             + N', droppable=' + CAST(SUM(CASE
                    WHEN t.type_desc = N'NONCLUSTERED'
                     AND ISNULL(t.is_unique, 0) = 0
                     AND ISNULL(t.is_primary, 0) = 0
                     AND ISNULL(t.is_uq_constr, 0) = 0
                     AND ISNULL(t.seeks, 0) + ISNULL(t.scans, 0) + ISNULL(t.lookups, 0) = 0
                    THEN 1 ELSE 0 END) AS nvarchar(12))
             + N', tables=' + CAST(COUNT(DISTINCT t.schema_name + N'.' + t.table_name) AS nvarchar(12))
             + N', uptime_days=' + CAST(@uptime_days AS nvarchar(16))
             + N', counters_since=' + @restarted_at
             + N' | cold = no row in dm_db_index_usage_stats (never read AND never written)'
             AS varchar(2000)) AS message,
        0 AS sort_rank
    FROM #usage AS t
    WHERE t.kind <> 'MISSING'
    GROUP BY t.db_name

    UNION ALL

    SELECT
        CAST('index_usage :: summary' AS varchar(400)) AS metric_item,
        CAST(CAST((SELECT COUNT(*) FROM #usage WHERE kind <> 'MISSING') AS varchar(12)) AS varchar(64)) AS metric_value,
        CAST('summary' AS varchar(32)) AS metric_unit,
        CAST('OK' AS varchar(16)) AS status,
        CAST(N'indexes_total=' + CAST((SELECT COUNT(*) FROM #usage WHERE kind <> 'MISSING') AS nvarchar(12))
             + N', used=' + CAST((SELECT COUNT(*) FROM #usage WHERE kind = 'USED') AS nvarchar(12))
             + N', unused=' + CAST((SELECT COUNT(*) FROM #usage WHERE kind = 'UNUSED') AS nvarchar(12))
             + N', cold=' + CAST((SELECT COUNT(*) FROM #usage WHERE kind = 'COLD') AS nvarchar(12))
             + N', disabled=' + CAST((SELECT COUNT(*) FROM #usage WHERE kind <> 'MISSING' AND is_disabled = 1) AS nvarchar(12))
             + N', disabled_clustered=' + CAST((SELECT COUNT(*) FROM #usage WHERE kind <> 'MISSING' AND is_disabled = 1 AND type_desc = N'CLUSTERED') AS nvarchar(12))
             + N', droppable=' + CAST((SELECT COUNT(*) FROM #usage
                    WHERE kind <> 'MISSING' AND type_desc = N'NONCLUSTERED'
                      AND ISNULL(is_unique, 0) = 0 AND ISNULL(is_primary, 0) = 0
                      AND ISNULL(is_uq_constr, 0) = 0
                      AND ISNULL(seeks, 0) + ISNULL(scans, 0) + ISNULL(lookups, 0) = 0) AS nvarchar(12))
             + N', missing_suggestions=' + CAST((SELECT COUNT(*) FROM #usage WHERE kind = 'MISSING') AS nvarchar(12))
             + N', uptime_days=' + CAST(@uptime_days AS nvarchar(16))
             + N', counters_since=' + @restarted_at
             -- Three states, because the reader has to be able to tell "no data" from "data you
             -- must not act on yet" from "data you can act on".
             + CASE
                    WHEN @uptime_hours < @detail_min_hours
                        THEN N' | sample too short: the usage DMVs reset on restart, so per-index detail is withheld below '
                             + CAST(@detail_min_hours AS nvarchar(8)) + N' hours of uptime'
                    WHEN @short_sample = 1
                        THEN N' | SHORT SAMPLE: the usage DMVs reset on restart and this one covers only '
                             + CAST(@uptime_days AS nvarchar(16)) + N' day(s). Read the detail, but treat every '
                             + N'unused/cold/droppable count as provisional until '
                             + CAST(@trusted_min_hours / 24 AS nvarchar(8)) + N' days of uptime'
                    ELSE N'' END AS varchar(2000)) AS message,
        -1 AS sort_rank
) AS q
ORDER BY q.sort_rank, q.metric_item;

IF OBJECT_ID('tempdb..#usage') IS NOT NULL DROP TABLE #usage;
