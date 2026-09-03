-- PERFORMANCE_WORKLOAD_COUNTERS: how much work the instance has done, reported raw.
--
-- Every value here is a total since the engine started, and that is deliberate. The question a
-- DBA actually asks is "how much did this instance do in the last hour", and no single query on
-- the target can answer it: a counter read once is a total, and a total divided by anything is an
-- average over the whole uptime. On one production instance that uptime was nine months, which is how a
-- 95.97 ms "current" write latency turned out to be last winter -- see the note in
-- 019_sqlserver_io_latency.sql.
--
-- So this metric does not grade and does not divide. It records the counters, the moment it read
-- them, and the baseline they are counted from; the report subtracts two collections and gets the
-- hour. That split is the point:
--
--   * the target does one cheap read every 15 minutes and holds no state,
--   * the arithmetic is in one place (db_ops.lib.interval_rates) instead of being re-derived by
--     every page that wants a rate,
--   * and nothing here can raise an alert, so a counter that ticks is never a finding.
--     report_policy.collect_only says the same thing to the report side.
--
-- PERFORMANCE_WAIT_STATS takes the opposite route -- two reads five seconds apart inside one
-- execution -- because it must grade itself in its own SQL. Both are right for what they do; what
-- would be wrong is a collector that reports a since-boot total as if it were a rate.
--
-- **counters_since is what makes the subtraction honest.** After a restart the totals begin at
-- zero, so the difference across a restart is the new absolute value and would read as a burst of
-- work that never happened. A busy instance can pass its own previous totals within the hour, so
-- nothing about the numbers themselves says the baseline moved -- the marker has to be carried.
--
-- Only cntr_type = 272696576 counters are read. That is the cumulative "per second" family, and
-- filtering on it is not tidiness: Log Flush Wait Time and Processes blocked sit in the same DMV
-- as instantaneous gauges, and differencing a gauge produces a number with no meaning at all. A
-- counter missing from a build is simply absent from the result, which is the honest outcome.

SET NOCOUNT ON;

-- The baseline every value below is counted from. A collection that disagrees with the previous
-- one about this is a collection the report must not subtract across.
DECLARE @counters_since varchar(19) =
    CONVERT(varchar(19), (SELECT sqlserver_start_time FROM sys.dm_os_sys_info), 120);

-- Carried so the reader can turn CPU milliseconds into a percentage without a second query:
-- 30,000,000 ms of CPU in an hour is 100% of eight cores and 8% of a hundred.
DECLARE @cpu_count varchar(12) =
    CAST((SELECT cpu_count FROM sys.dm_os_sys_info) AS varchar(12));

DECLARE @rows TABLE (
    metric_item  varchar(64)  NOT NULL PRIMARY KEY,
    metric_value varchar(32)  NOT NULL,
    metric_unit  varchar(32)  NOT NULL,
    source       varchar(64)  NOT NULL
);

-- ------------------------------------------------------------------------------------------
-- CPU, from the resource pools rather than from sys.dm_exec_query_stats.
--
-- Summing the plan cache is the obvious way to total CPU and it undercounts by an unknown amount:
-- an evicted plan takes its worker time with it, and an ad-hoc batch that was never cached was
-- never in the sum. The resource pools carry every millisecond the engine spent, cached or not.
-- The pools are disjoint (internal, default, and any user pools), so the sum is the instance.
-- ------------------------------------------------------------------------------------------
DECLARE @cpu_ms bigint =
    (SELECT SUM(total_cpu_usage_ms) FROM sys.dm_resource_governor_resource_pools);

INSERT INTO @rows (metric_item, metric_value, metric_unit, source)
SELECT 'cpu_usage_ms', CAST(@cpu_ms AS varchar(32)), 'ms', 'dm_resource_governor_resource_pools'
WHERE @cpu_ms IS NOT NULL;

-- ------------------------------------------------------------------------------------------
-- Throughput, from sys.dm_os_performance_counters.
--
-- object_name is prefixed with the instance ("SQLServer:" on a default instance, "MSSQL$NAME:"
-- on a named one), so it is matched on its tail. Both object_name and instance_name are
-- blank-padded char columns, which is why every comparison goes through RTRIM.
-- ------------------------------------------------------------------------------------------
DECLARE @wanted TABLE (
    metric_item   varchar(64)  NOT NULL PRIMARY KEY,
    object_suffix varchar(64)  NOT NULL,
    counter_name  varchar(128) NOT NULL,
    instance_name varchar(128) NOT NULL,
    metric_unit   varchar(32)  NOT NULL
);
INSERT INTO @wanted (metric_item, object_suffix, counter_name, instance_name, metric_unit) VALUES
    -- What the instance was asked to do.
    ('batch_requests',     'SQL Statistics',     'Batch Requests/sec',      '',       'count'),
    ('sql_compilations',   'SQL Statistics',     'SQL Compilations/sec',    '',       'count'),
    ('sql_recompilations', 'SQL Statistics',     'SQL Re-Compilations/sec', '',       'count'),
    ('logins',             'General Statistics', 'Logins/sec',              '',       'count'),
    ('transactions',       'Databases',          'Transactions/sec',        '_Total', 'count'),
    -- Logical I/O. Page lookups/sec is the instance-wide equivalent of the per-query
    -- total_logical_reads, and unlike the plan cache it misses nothing.
    ('page_lookups',       'Buffer Manager',     'Page lookups/sec',        '',       'pages'),
    -- Physical I/O as the buffer pool sees it. The file-level view of the same work is below.
    ('page_reads',         'Buffer Manager',     'Page reads/sec',          '',       'pages'),
    ('page_writes',        'Buffer Manager',     'Page writes/sec',         '',       'pages'),
    ('readahead_pages',    'Buffer Manager',     'Readahead pages/sec',     '',       'pages'),
    ('checkpoint_pages',   'Buffer Manager',     'Checkpoint pages/sec',    '',       'pages'),
    ('lazy_writes',        'Buffer Manager',     'Lazy writes/sec',         '',       'pages'),
    -- How the work was done. Full scans and page splits are what turn a modest request rate into
    -- a large logical read rate, which is the pair worth reading together.
    ('full_scans',         'Access Methods',     'Full Scans/sec',          '',       'count'),
    ('page_splits',        'Access Methods',     'Page Splits/sec',         '',       'count'),
    ('workfiles_created',  'Access Methods',     'Workfiles Created/sec',   '',       'count'),
    ('worktables_created', 'Access Methods',     'Worktables Created/sec',  '',       'count'),
    -- Contention, as totals. LOCK_BLOCKING_SESSIONS alerts on blocking as it happens; these say
    -- how much of it there was over the interval, which is the question after the fact.
    ('lock_waits',         'Locks',              'Lock Waits/sec',          '_Total', 'count'),
    ('lock_wait_ms',       'Locks',              'Lock Wait Time (ms)',     '_Total', 'ms'),
    ('deadlocks',          'Locks',              'Number of Deadlocks/sec', '_Total', 'count'),
    -- Write path. Log bytes flushed is the one number that says how much change the instance
    -- actually generated, and it is what sizes log backups and replication.
    ('log_flushes',        'Databases',          'Log Flushes/sec',         '_Total', 'count'),
    ('log_bytes_flushed',  'Databases',          'Log Bytes Flushed/sec',   '_Total', 'bytes');

INSERT INTO @rows (metric_item, metric_value, metric_unit, source)
SELECT w.metric_item,
       CAST(MAX(p.cntr_value) AS varchar(32)),
       w.metric_unit,
       'dm_os_performance_counters'
FROM @wanted AS w
INNER JOIN sys.dm_os_performance_counters AS p
        ON RTRIM(p.object_name) LIKE '%:' + w.object_suffix
       AND RTRIM(p.counter_name) = w.counter_name
       AND RTRIM(p.instance_name) = w.instance_name
       -- The cumulative family. Everything else in this DMV is a gauge, and a differenced gauge
       -- is a number that looks like a rate and is not one.
       AND p.cntr_type = 272696576
GROUP BY w.metric_item, w.metric_unit;

-- ------------------------------------------------------------------------------------------
-- Physical I/O at the file layer, summed over every file of every database.
--
-- PERFORMANCE_IO_LATENCY already reports these per file and is the right place to ask "which file
-- is slow". It is the wrong place to ask "how much I/O did this instance do", because that answer
-- is a sum over up to 500 rows per collection and a report would have to load a week of them to
-- subtract two. Six rows here answer it at the instance level for the same cost.
-- ------------------------------------------------------------------------------------------
-- Read once into variables rather than re-scanning the function per row: six correlated reads of
-- the same DMV would also be six different moments, and the six numbers are only comparable to
-- each other if they were taken together.
DECLARE @io_reads bigint, @io_writes bigint, @io_bytes_read bigint,
        @io_bytes_written bigint, @io_stall_read_ms bigint, @io_stall_write_ms bigint;

SELECT @io_reads          = SUM(num_of_reads),
       @io_writes         = SUM(num_of_writes),
       @io_bytes_read     = SUM(num_of_bytes_read),
       @io_bytes_written  = SUM(num_of_bytes_written),
       @io_stall_read_ms  = SUM(io_stall_read_ms),
       @io_stall_write_ms = SUM(io_stall_write_ms)
FROM sys.dm_io_virtual_file_stats(NULL, NULL);

INSERT INTO @rows (metric_item, metric_value, metric_unit, source)
SELECT x.metric_item, x.metric_value, x.metric_unit, 'dm_io_virtual_file_stats'
FROM (
              SELECT 'io_reads' AS metric_item, CAST(@io_reads AS varchar(32)) AS metric_value,
                     'count' AS metric_unit
    UNION ALL SELECT 'io_writes', CAST(@io_writes AS varchar(32)), 'count'
    UNION ALL SELECT 'io_bytes_read', CAST(@io_bytes_read AS varchar(32)), 'bytes'
    UNION ALL SELECT 'io_bytes_written', CAST(@io_bytes_written AS varchar(32)), 'bytes'
    UNION ALL SELECT 'io_stall_read_ms', CAST(@io_stall_read_ms AS varchar(32)), 'ms'
    UNION ALL SELECT 'io_stall_write_ms', CAST(@io_stall_write_ms AS varchar(32)), 'ms'
) AS x
WHERE x.metric_value IS NOT NULL;

SELECT
    CAST(r.metric_item AS varchar(256)) AS metric_item,
    CAST(r.metric_value AS varchar(32)) AS metric_value,
    CAST(r.metric_unit AS varchar(32)) AS metric_unit,
    -- Always OK. This metric records, it does not judge: a counter has no threshold, and the rate
    -- it becomes is graded by whoever compares two of these.
    CAST('OK' AS varchar(16)) AS status,
    CAST(
        'value=' + r.metric_value
        + ', unit=' + r.metric_unit
        + ', counters_since=' + @counters_since
        + ', cpu_count=' + @cpu_count
        + ', source=' + r.source
        + ', note=cumulative_since_start_subtract_two_collections'
        AS varchar(1000)) AS message
FROM @rows AS r
ORDER BY r.metric_item;
