-- PERFORMANCE_TOP_QUERIES: which statements the plan cache attributes the work to.
--
-- PERFORMANCE_QUERY_STATS_TOTALS says how much work the cached plans did in total -- on one instance
-- on 2026-09-11, 2.8M executions, 241M logical reads and 988 s of CPU in 15 minutes. It cannot say
-- which statements did it, which is the question that total is always followed by. This metric is
-- that answer: workload *attribution* next to the workload *shape*.
--
-- The same contract as every cumulative collector: raw totals since each plan was cached, one row
-- per query, no grading. The report subtracts two collections per query_hash to get the interval.
--
-- **Grouped by query_hash, not by plan.** One statement run with different literals, or recompiled
-- into several plans, is still one statement to tune. Summed over its plans it ranks as what it is,
-- instead of as a dozen small entries none of which reaches the top.
--
-- **A union of six rankings, 25 deep.** CPU, elapsed time, logical reads, physical reads, logical
-- writes and executions each pick their own top 25, and the row set is the union -- usually 50 to 80
-- rows, at most 150. The page shows 20 per ranking; the extra depth is what lets the next collection
-- find last collection's rows to subtract from. A statement that was 21st an hour ago and 3rd now is
-- exactly the one worth seeing, and it has no baseline at all if the previous sample stopped at 20.
--
-- **Everything below is plan-cache-resident only**, with the caveats PERFORMANCE_QUERY_STATS_TOTALS
-- spells out: an evicted plan takes its totals with it, and a batch that was never cached was never
-- counted. first_cached_utc is carried so the report can tell "new since the last collection" (the
-- whole total happened inside the window) from "was there, just not ranked" (no baseline, not
-- ranked).
--
-- **The latency figures are a spread, not percentiles.** min/max elapsed per execution are the
-- extremes since the plan was cached. No DMV keeps a distribution, so P95 and P99 are not derivable
-- from this and the report does not label anything as one.
--
-- **The statement text is a 200-character snippet**, decided with the operator on 2026-09-11: it
-- is shown on a served page and an ad hoc statement carries its literal values. Every '=' in it is
-- spaced out so the message parser (interval_rates.message_fields, which keeps the LAST value of a
-- key) cannot read `WHERE cpu_ms=5` inside the text as this row's cpu_ms; and it is the last field,
-- read by the report as everything after "text=".
--
-- **Cost.** One scan of the plan cache, the same as 073, and it runs at the same 1800-second
-- cadence and 60-second timeout for the reason given there. The text and the database are looked
-- up for the chosen rows only.

SET NOCOUNT ON;

DECLARE @counters_since varchar(19) =
    CONVERT(varchar(19), (SELECT sqlserver_start_time FROM sys.dm_os_sys_info), 120);

-- creation_time and last_execution_time are server-local; the store's collected_at is UTC. Shifted
-- here so the report compares like with like.
DECLARE @utc_offset_seconds int = DATEDIFF(second, SYSDATETIME(), SYSUTCDATETIME());

DECLARE @depth int = 25;

IF OBJECT_ID('tempdb..#q') IS NOT NULL DROP TABLE #q;

-- One pass over the cache. The representative plan of each query_hash -- the one whose statement
-- text and database are shown -- is its most CPU-expensive plan, picked in the same pass.
;WITH plan_rows AS (
    SELECT qs.query_hash, qs.sql_handle, qs.plan_handle,
           qs.statement_start_offset, qs.statement_end_offset,
           qs.execution_count, qs.total_worker_time, qs.total_elapsed_time,
           qs.total_logical_reads, qs.total_logical_writes, qs.total_physical_reads,
           qs.min_elapsed_time, qs.max_elapsed_time, qs.creation_time, qs.last_execution_time,
           ROW_NUMBER() OVER (PARTITION BY qs.query_hash ORDER BY qs.total_worker_time DESC) AS rn
    FROM sys.dm_exec_query_stats AS qs
)
SELECT query_hash,
       SUM(execution_count)              AS executions,
       SUM(total_worker_time) / 1000     AS cpu_ms,
       SUM(total_elapsed_time) / 1000    AS elapsed_ms,
       SUM(total_logical_reads)          AS logical_reads,
       SUM(total_logical_writes)         AS logical_writes,
       SUM(total_physical_reads)         AS physical_reads,
       MIN(min_elapsed_time)             AS min_elapsed_us,
       MAX(max_elapsed_time)             AS max_elapsed_us,
       MIN(creation_time)                AS first_cached,
       MAX(last_execution_time)          AS last_execution,
       COUNT(*)                          AS plans,
       MAX(CASE WHEN rn = 1 THEN sql_handle END)             AS sql_handle,
       MAX(CASE WHEN rn = 1 THEN plan_handle END)            AS plan_handle,
       MAX(CASE WHEN rn = 1 THEN statement_start_offset END) AS stmt_start,
       MAX(CASE WHEN rn = 1 THEN statement_end_offset END)   AS stmt_end
INTO #q
FROM plan_rows
GROUP BY query_hash;

IF OBJECT_ID('tempdb..#top') IS NOT NULL DROP TABLE #top;

SELECT r.*
INTO #top
FROM (
    SELECT q.*,
           ROW_NUMBER() OVER (ORDER BY q.cpu_ms DESC)         AS r_cpu,
           ROW_NUMBER() OVER (ORDER BY q.elapsed_ms DESC)     AS r_elapsed,
           ROW_NUMBER() OVER (ORDER BY q.logical_reads DESC)  AS r_reads,
           ROW_NUMBER() OVER (ORDER BY q.physical_reads DESC) AS r_physical,
           ROW_NUMBER() OVER (ORDER BY q.logical_writes DESC) AS r_writes,
           ROW_NUMBER() OVER (ORDER BY q.executions DESC)     AS r_executions
    FROM #q AS q
) AS r
WHERE r.r_cpu <= @depth OR r.r_elapsed <= @depth OR r.r_reads <= @depth
   OR r.r_physical <= @depth OR r.r_writes <= @depth OR r.r_executions <= @depth;

SELECT
    CAST(CONVERT(varchar(18), t.query_hash, 1) AS varchar(256)) AS metric_item,
    CAST(t.cpu_ms AS varchar(32)) AS metric_value,
    CAST('ms' AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST(
        'value=' + CAST(t.cpu_ms AS varchar(32))
        + ', unit=ms'
        + ', counters_since=' + @counters_since
        + ', executions='     + CAST(t.executions     AS varchar(32))
        + ', cpu_ms='         + CAST(t.cpu_ms         AS varchar(32))
        + ', elapsed_ms='     + CAST(t.elapsed_ms     AS varchar(32))
        + ', logical_reads='  + CAST(t.logical_reads  AS varchar(32))
        + ', logical_writes=' + CAST(t.logical_writes AS varchar(32))
        + ', physical_reads=' + CAST(t.physical_reads AS varchar(32))
        + ', min_elapsed_ms=' + CAST(CAST(t.min_elapsed_us / 1000.0 AS decimal(19, 3)) AS varchar(32))
        + ', max_elapsed_ms=' + CAST(CAST(t.max_elapsed_us / 1000.0 AS decimal(19, 3)) AS varchar(32))
        + ', plans='          + CAST(t.plans          AS varchar(12))
        + ', first_cached_utc=' + CONVERT(varchar(19), DATEADD(second, @utc_offset_seconds, t.first_cached), 120)
        + ', last_execution_utc=' + CONVERT(varchar(19), DATEADD(second, @utc_offset_seconds, t.last_execution), 120)
        -- The database: the object's own for a procedure/function/trigger, the plan's context
        -- database for an ad hoc or prepared statement (sql_text carries no dbid for those).
        -- Commas and '=' are neutralised in every free-text field for the parser's sake.
        + ISNULL(', db_name=' + REPLACE(REPLACE(DB_NAME(COALESCE(st.dbid, pa.dbid)), ',', ';'), '=', '-'), '')
        + ISNULL(', object_name=' + REPLACE(REPLACE(OBJECT_NAME(st.objectid, st.dbid), ',', ';'), '=', '-'), '')
        + ', source=dm_exec_query_stats'
        + ', note=plan_cache_resident_only_subtract_two_collections_per_query_hash'
        -- LAST, and read as everything after "text=".
        + ', text=' + ISNULL(LEFT(sn.flat, 200), '')
        AS nvarchar(1500)) AS message
FROM #top AS t
OUTER APPLY sys.dm_exec_sql_text(t.sql_handle) AS st
-- The statement as one line with single spaces, BEFORE the 200 characters are taken: measured on
-- the lab instance on 2026-09-11, indentation alone used up half of a snippet ("SELECT         CAST").
-- T-SQL has no regex, so runs of spaces are collapsed with a marker character: ' ' -> ' '+BEL, then
-- every BEL+' ' is removed, then the stray BELs. BEL (CHAR(7)) cannot occur in a statement's text.
-- '=' is spaced out first, so the collapse also tidies it and every '=' ends up after a space --
-- which is all the message parser's guard needs (see the header).
OUTER APPLY (
    SELECT LTRIM(REPLACE(REPLACE(REPLACE(
               REPLACE(REPLACE(REPLACE(REPLACE(
                   SUBSTRING(st.text, (t.stmt_start / 2) + 1,
                             ((CASE t.stmt_end WHEN -1 THEN DATALENGTH(st.text) ELSE t.stmt_end END
                               - t.stmt_start) / 2) + 1),
                   CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' '), '=', ' = '),
               ' ', ' ' + CHAR(7)), CHAR(7) + ' ', ''), CHAR(7), '')) AS flat
) AS sn
OUTER APPLY (
    SELECT CONVERT(int, a.value) AS dbid
    FROM sys.dm_exec_plan_attributes(t.plan_handle) AS a
    WHERE a.attribute = 'dbid'
) AS pa
ORDER BY t.cpu_ms DESC;

DROP TABLE #top;
DROP TABLE #q;
