-- DATABASE_CONSTRAINT_HEALTH (SQL Server): per-database constraint integrity.
-- Reports the leading indicators of orphaned / invalid data:
--   * FOREIGN KEY constraints that are DISABLED (not enforced) or UNTRUSTED
--     (is_not_trusted = 1 => re-enabled WITH NOCHECK, so orphaned child rows may exist),
--   * CHECK constraints that are DISABLED or UNTRUSTED,
--   * user triggers that are DISABLED.
-- One WARNING detail row per problem constraint/trigger, plus one summary row per database
-- (OK when clean, WARNING when it has any problem). Runs from the instance (master); a cursor
-- walks each ONLINE, accessible user database. Databases the monitor login cannot read are
-- reported as a single ERROR summary row (never failing the whole metric). Actual orphaned-row
-- counting per FK is deliberately NOT done here (unbounded/expensive) — use a targeted SQL task.
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#cons_detail')  IS NOT NULL DROP TABLE #cons_detail;
IF OBJECT_ID('tempdb..#cons_summary') IS NOT NULL DROP TABLE #cons_summary;
CREATE TABLE #cons_detail (
    db_name    sysname,
    kind       nvarchar(16),
    table_name nvarchar(260) NULL,
    cons_name  nvarchar(260) NULL,
    problem    nvarchar(16)               -- DISABLED | UNTRUSTED
);
CREATE TABLE #cons_summary (
    db_name   sysname,
    total     int,
    disabled  int,
    untrusted int,
    readable  bit
);

DECLARE @db sysname, @sql nvarchar(max);

DECLARE db_cursor CURSOR LOCAL FAST_FORWARD FOR
    SELECT d.name
    FROM sys.databases AS d
    WHERE d.database_id > 4
      AND d.state = 0
      AND d.source_database_id IS NULL
      AND HAS_DBACCESS(d.name) = 1
    ORDER BY d.name;

OPEN db_cursor;
FETCH NEXT FROM db_cursor INTO @db;
WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'
        USE ' + QUOTENAME(@db) + N';

        INSERT INTO #cons_detail (db_name, kind, table_name, cons_name, problem)
        SELECT DB_NAME(), N''FK'', OBJECT_NAME(fk.parent_object_id), fk.name,
               CASE WHEN fk.is_disabled = 1 THEN N''DISABLED'' ELSE N''UNTRUSTED'' END
        FROM sys.foreign_keys AS fk
        WHERE fk.is_disabled = 1 OR fk.is_not_trusted = 1
        UNION ALL
        SELECT DB_NAME(), N''CHECK'', OBJECT_NAME(cc.parent_object_id), cc.name,
               CASE WHEN cc.is_disabled = 1 THEN N''DISABLED'' ELSE N''UNTRUSTED'' END
        FROM sys.check_constraints AS cc
        WHERE cc.is_disabled = 1 OR cc.is_not_trusted = 1
        UNION ALL
        SELECT DB_NAME(), N''TRIGGER'', OBJECT_NAME(tr.parent_id), tr.name, N''DISABLED''
        FROM sys.triggers AS tr
        WHERE tr.is_disabled = 1 AND tr.parent_class = 1;   -- object (table) triggers only

        INSERT INTO #cons_summary (db_name, total, disabled, untrusted, readable)
        SELECT DB_NAME(),
               COUNT(*),
               SUM(CASE WHEN problem = N''DISABLED''  THEN 1 ELSE 0 END),
               SUM(CASE WHEN problem = N''UNTRUSTED'' THEN 1 ELSE 0 END),
               1
        FROM #cons_detail
        WHERE db_name = DB_NAME();
    ';

    BEGIN TRY
        EXEC sys.sp_executesql @sql;
    END TRY
    BEGIN CATCH
        INSERT INTO #cons_summary (db_name, total, disabled, untrusted, readable)
        VALUES (@db, 0, 0, 0, 0);
    END CATCH;

    FETCH NEXT FROM db_cursor INTO @db;
END
CLOSE db_cursor;
DEALLOCATE db_cursor;

-- ONE alerting row per database (the summary), and the individual constraints as LOGGING detail
-- behind it.
--
-- The detail rows used to be WARNING each. On a real ERP database that is 222 warnings for one
-- fact — "DtradeProduction has 222 untrusted constraints" — which the summary row already states
-- in full. The estate went from 3 warnings to 293 the day that server was onboarded, and 223 of
-- them were this metric restating one finding. An operator cannot triage that, and the finding is
-- no more visible for having been repeated.
--
-- LOGGING (not dropped): the rows are still collected and still queryable, so "which constraint"
-- is one query away. It is the same split the index metrics already use — counts alert, the
-- thousands of per-index rows are kept without alerting.
--
-- Summaries sort FIRST now. The old comment said details went first "so the integrity problems
-- survive the collector's row cap" — with the alert on the detail row that was right, and with
-- the alert on the summary it is exactly backwards: a database with more constraints than the cap
-- would lose the only row that still warns.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        CAST(s.db_name + N' :: constraints' AS varchar(400)) AS metric_item,
        CAST(CAST(s.total AS varchar(12)) AS varchar(64))    AS metric_value,
        CAST('count' AS varchar(32))                         AS metric_unit,
        CAST(CASE WHEN s.readable = 0 THEN 'ERROR'
                  WHEN s.total > 0 THEN 'WARNING' ELSE 'OK' END AS varchar(16)) AS status,
        CAST(CASE WHEN s.readable = 0 THEN N'constraints not readable by monitor login'
                  ELSE N'disabled=' + CAST(s.disabled AS nvarchar(12))
                       + N' untrusted=' + CAST(s.untrusted AS nvarchar(12))
                       + N' (FK/CHECK/TRIGGER problems in this database)' END AS varchar(1000)) AS message,
        CASE WHEN s.readable = 0 OR s.total > 0 THEN 0 ELSE 2 END AS sort_rank
    FROM #cons_summary AS s

    UNION ALL

    SELECT
        CAST(d.db_name + N'\' + ISNULL(d.table_name, N'?') + N'.' + ISNULL(d.cons_name, N'?') AS varchar(400)) AS metric_item,
        CAST(d.kind AS varchar(64))    AS metric_value,
        CAST('detail' AS varchar(32))  AS metric_unit,
        CAST('LOGGING' AS varchar(16)) AS status,
        CAST(d.kind + N' ' + d.problem
             + CASE WHEN d.problem = N'UNTRUSTED' THEN N' (WITH NOCHECK - orphaned/invalid rows possible)'
                    ELSE N' (not enforced)' END AS varchar(1000)) AS message,
        1 AS sort_rank
    FROM #cons_detail AS d
) AS q
ORDER BY q.sort_rank, q.metric_item;

IF OBJECT_ID('tempdb..#cons_detail')  IS NOT NULL DROP TABLE #cons_detail;
IF OBJECT_ID('tempdb..#cons_summary') IS NOT NULL DROP TABLE #cons_summary;
