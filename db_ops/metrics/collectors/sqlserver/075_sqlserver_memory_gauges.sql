-- PERFORMANCE_MEMORY_GAUGES: what the instance's memory looked like, reported raw and ungraded.
--
-- The sibling of 072_sqlserver_workload_counters.sql, and the opposite kind of number. Everything
-- 072 records is a total since the engine started and only means something once two collections
-- are subtracted. Everything here is a **gauge**: a reading of the moment. Differencing a gauge
-- produces a figure that looks like a rate and is not one, so nothing here is ever subtracted --
-- the report summarises several readings into latest / min / max over a window instead.
--
-- SYSTEM_CPU_MEMORY and PAGE_LIFE_EXPECTANCY already read two of these values, and this metric is
-- not a duplicate of either, for the same reason PERFORMANCE_WAIT_TOTALS is not a duplicate of
-- PERFORMANCE_WAIT_STATS: those two grade and alert on the moment they ran, this one records the
-- series so a report can say what the last 24 hours looked like. An alert has no use for a series
-- and a report cannot be built from an alert.
--
-- WHY THESE ITEMS, asked on 2026-09-11 after an instance was called short of memory on the
-- strength of one PLE reading:
--
--   * PLE alone cannot carry that conclusion. It is a gauge that dips whenever a large scan
--     lands, and a single sample of it is the weakest evidence in this list. Measured on
--     192.0.2.250 while writing this: the instance figure read 141 with the five NUMA nodes at
--     298 / 265 / 170 / 60 / 168. Readings taken minutes apart had already ranged over a factor
--     of three.
--   * **Memory Grants Pending is the question PLE is usually asked in place of** -- is anything
--     actually waiting for memory to run? Zero means no query was made to wait, however low PLE
--     looks, and it is zero on a healthy instance almost always.
--   * **Total vs Target Server Memory** is the other half: equal means the pool has as much as it
--     wants, so a low PLE is the workload's doing and not the operating system's. Total below
--     Target means SQL Server is being held back and has not reached what it asked for.
--   * **Database cache / stolen / free** splits the pool. A pool that is mostly stolen is a
--     different problem from one that is mostly data, and PLE describes only the data half.
--   * **Available physical memory** and the low-memory signal are what Windows thinks, which is
--     the only thing that makes SQL Server give memory back.
--
-- **Per-NUMA-node PLE is carried separately because the instance figure hides it.** On a NUMA box
-- Buffer Manager's Page life expectancy is a combination of the nodes, not the worst of them, so
-- one node under real pressure reads as a mild instance number. The minimum across Buffer Node is
-- the one worth alarming at, and the node count is carried so a reader can tell whether the two
-- figures are even allowed to differ.
--
-- **Buffer cache hit ratio is deliberately absent.** It counts a read-ahead page as a hit, so a
-- scan-heavy instance reports 99.99% while it reads hundreds of pages a second off disk --
-- measured here at 8,787,105 / 8,787,950 on the same instance doing 262 physical reads/sec. A
-- number that says "fine" under every condition this section exists to detect is worse than no
-- number.
--
-- Lazy writes are NOT here. They are a cumulative counter, 072 already records them, and the
-- report reads them from there -- a second copy under a different collector would be the same
-- counter with two baselines.

SET NOCOUNT ON;

-- Carried so a window that spans a restart can say so. A gauge survives a restart in a way a
-- counter does not -- it still reads the current state -- but PLE in particular begins near zero
-- and climbs, so the minimum over a window containing a restart is the restart, not pressure.
DECLARE @counters_since varchar(19) =
    CONVERT(varchar(19), (SELECT sqlserver_start_time FROM sys.dm_os_sys_info), 120);

DECLARE @cpu_count varchar(12) =
    CAST((SELECT cpu_count FROM sys.dm_os_sys_info) AS varchar(12));

DECLARE @rows TABLE (
    metric_item  varchar(64)  NOT NULL PRIMARY KEY,
    metric_value varchar(32)  NOT NULL,
    metric_unit  varchar(32)  NOT NULL,
    source       varchar(64)  NOT NULL
);

-- ------------------------------------------------------------------------------------------
-- The memory manager's own gauges.
--
-- object_name carries the instance prefix ("SQLServer:" on a default instance, "MSSQL$NAME:" on a
-- named one -- this estate has both), so it is matched on its tail through RTRIM, exactly as
-- 072 does. cntr_type 65792 is the plain-gauge family; filtering on it is what keeps a cumulative
-- counter from being read here by mistake.
-- ------------------------------------------------------------------------------------------
DECLARE @wanted TABLE (
    metric_item   varchar(64)  NOT NULL PRIMARY KEY,
    object_suffix varchar(64)  NOT NULL,
    counter_name  varchar(128) NOT NULL,
    metric_unit   varchar(32)  NOT NULL
);
INSERT INTO @wanted (metric_item, object_suffix, counter_name, metric_unit) VALUES
    -- What the pool has, and what it wants. The pair, not either alone.
    ('total_server_memory_kb',    'Memory Manager', 'Total Server Memory (KB)',   'kb'),
    ('target_server_memory_kb',   'Memory Manager', 'Target Server Memory (KB)',  'kb'),
    -- How the pool is spent. PLE describes the first of these three and nothing else.
    ('database_cache_memory_kb',  'Memory Manager', 'Database Cache Memory (KB)', 'kb'),
    ('stolen_server_memory_kb',   'Memory Manager', 'Stolen Server Memory (KB)',  'kb'),
    ('free_memory_kb',            'Memory Manager', 'Free Memory (KB)',           'kb'),
    -- Whether anything actually had to wait for memory. The row this section exists for.
    ('memory_grants_pending',     'Memory Manager', 'Memory Grants Pending',      'count'),
    ('memory_grants_outstanding', 'Memory Manager', 'Memory Grants Outstanding',  'count'),
    -- The instance-wide reading. The per-node minimum is taken separately below.
    ('page_life_expectancy',      'Buffer Manager', 'Page life expectancy',       'seconds');

INSERT INTO @rows (metric_item, metric_value, metric_unit, source)
SELECT w.metric_item,
       CAST(MAX(p.cntr_value) AS varchar(32)),
       w.metric_unit,
       'dm_os_performance_counters'
FROM @wanted AS w
INNER JOIN sys.dm_os_performance_counters AS p
        ON RTRIM(p.object_name) LIKE '%:' + w.object_suffix
       AND RTRIM(p.counter_name) = w.counter_name
       AND RTRIM(p.instance_name) = ''
       AND p.cntr_type = 65792
GROUP BY w.metric_item, w.metric_unit;

-- ------------------------------------------------------------------------------------------
-- Per-NUMA-node page life expectancy: the worst node, and how many there are.
--
-- Buffer Node exists only on a NUMA instance; on a single-node box this yields nothing and both
-- items are simply absent, which is the honest outcome -- an absent item says "this build has no
-- such thing", a zero would say "the worst node is at zero seconds".
-- ------------------------------------------------------------------------------------------
INSERT INTO @rows (metric_item, metric_value, metric_unit, source)
SELECT x.metric_item, x.metric_value, x.metric_unit, 'dm_os_performance_counters'
FROM (
    SELECT 'ple_node_min' AS metric_item,
           CAST(MIN(p.cntr_value) AS varchar(32)) AS metric_value,
           'seconds' AS metric_unit,
           COUNT(*) AS nodes
    FROM sys.dm_os_performance_counters AS p
    WHERE RTRIM(p.object_name) LIKE '%:Buffer Node'
      AND RTRIM(p.counter_name) = 'Page life expectancy'
      AND p.cntr_type = 65792
    UNION ALL
    SELECT 'ple_node_count',
           CAST(COUNT(*) AS varchar(32)),
           'count',
           COUNT(*)
    FROM sys.dm_os_performance_counters AS p
    WHERE RTRIM(p.object_name) LIKE '%:Buffer Node'
      AND RTRIM(p.counter_name) = 'Page life expectancy'
      AND p.cntr_type = 65792
) AS x
WHERE x.nodes > 0;

-- ------------------------------------------------------------------------------------------
-- What the operating system and the process see.
--
-- Read once into variables rather than re-querying per row: the figures are only comparable to
-- each other if they were taken at the same instant, which is the same rule 072 applies to
-- dm_io_virtual_file_stats.
-- ------------------------------------------------------------------------------------------
DECLARE @total_physical_kb bigint, @available_physical_kb bigint, @low_memory_signal int,
        @process_memory_kb bigint, @process_memory_low int;

SELECT @total_physical_kb     = sm.total_physical_memory_kb,
       @available_physical_kb = sm.available_physical_memory_kb,
       @low_memory_signal     = CASE WHEN sm.system_low_memory_signal_state = 1 THEN 1 ELSE 0 END,
       @process_memory_kb     = pm.physical_memory_in_use_kb,
       @process_memory_low    = CASE WHEN pm.process_physical_memory_low = 1 THEN 1 ELSE 0 END
FROM sys.dm_os_sys_memory AS sm
CROSS JOIN sys.dm_os_process_memory AS pm;

INSERT INTO @rows (metric_item, metric_value, metric_unit, source)
SELECT x.metric_item, x.metric_value, x.metric_unit, x.source
FROM (
              SELECT 'total_physical_kb' AS metric_item,
                     CAST(@total_physical_kb AS varchar(32)) AS metric_value,
                     'kb' AS metric_unit, 'dm_os_sys_memory' AS source
    UNION ALL SELECT 'available_physical_kb', CAST(@available_physical_kb AS varchar(32)),
                     'kb', 'dm_os_sys_memory'
    UNION ALL SELECT 'system_low_memory_signal', CAST(@low_memory_signal AS varchar(32)),
                     'flag', 'dm_os_sys_memory'
    -- Larger than Total Server Memory: the pool is most of this process but not all of it.
    UNION ALL SELECT 'sql_physical_memory_in_use_kb', CAST(@process_memory_kb AS varchar(32)),
                     'kb', 'dm_os_process_memory'
    UNION ALL SELECT 'process_physical_memory_low', CAST(@process_memory_low AS varchar(32)),
                     'flag', 'dm_os_process_memory'
) AS x
WHERE x.metric_value IS NOT NULL;

-- ------------------------------------------------------------------------------------------
-- Internal pressure, as a count rather than as a number nobody can read.
--
-- sys.dm_os_memory_brokers has no "pressure" column; what it has is last_notification, which is
-- GROW, SHRINK or STABLE per broker. "How many brokers is the engine currently telling to give
-- memory back" is the readable form of the question, and its healthy answer is zero -- measured
-- zero here, with all eleven brokers on GROW, while PLE read 141.
-- ------------------------------------------------------------------------------------------
INSERT INTO @rows (metric_item, metric_value, metric_unit, source)
SELECT 'brokers_shrinking',
       CAST(SUM(CASE WHEN b.last_notification = 'SHRINK' THEN 1 ELSE 0 END) AS varchar(32)),
       'count',
       'dm_os_memory_brokers'
FROM sys.dm_os_memory_brokers AS b;

SELECT
    CAST(r.metric_item AS varchar(256)) AS metric_item,
    CAST(r.metric_value AS varchar(32)) AS metric_value,
    CAST(r.metric_unit AS varchar(32)) AS metric_unit,
    -- Always OK. This metric records, it does not judge: none of these gauges has a threshold
    -- that holds on every instance, and the conclusion operators reach from a single reading of
    -- PLE is the mistake this collector was written to make visible.
    CAST('OK' AS varchar(16)) AS status,
    CAST(
        'value=' + r.metric_value
        + ', unit=' + r.metric_unit
        + ', counters_since=' + @counters_since
        + ', cpu_count=' + @cpu_count
        + ', source=' + r.source
        + ', note=gauge_not_cumulative_never_subtract_two_collections'
        AS varchar(1000)) AS message
FROM @rows AS r
ORDER BY r.metric_item;
