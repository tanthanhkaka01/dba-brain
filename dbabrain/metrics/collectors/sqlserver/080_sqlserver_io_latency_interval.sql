-- PERFORMANCE_IO_LATENCY_INTERVAL (SQL Server 2012+): I/O latency graded on a live interval.
--
-- `sys.dm_io_virtual_file_stats` counters are cumulative totals since the engine started.
-- An average computed across the entire uptime cannot catch a spike: on 192.0.2.115 the cumulative
-- average sat at 12.84 ms while single 15-minute intervals reached 269 ms and 1736 ms during a
-- log-full outage (quoted in 019). A bad afternoon last month keeps a cumulative average elevated,
-- while acute disk stalls happening right now are diluted to invisibility by the millions of fast
-- historical I/Os.
--
-- The house answer for measuring intervals without per-metric machinery in the collector is the
-- two-read pattern from 014_sqlserver_wait_stats.sql: sample the DMV, wait, sample again, and
-- difference the two reads inside one execution. Nothing is stored on the target or in the runtime
-- store, so the collector needs no store lookups or custom policies (following docs/04_metrics_engine.md).
--
-- Each read is materialised into a table variable before aggregating. Aggregating directly from the
-- DMV can fail with duplicate-key or split-group errors on some builds (as documented in 014).
-- Negative deltas (counters reset by restart, failover, or clear between reads) are dropped.
--
-- Each kind of file is graded on the I/O a session actually waits for. A log is graded on its
-- writes, tightly (@log_write_warning_ms = 20 ms, @log_write_critical_ms = 50 ms): every commit
-- waits for its log flush (WRITELOG). Its reads are backups and replication readers, not a user.
-- A data file is graded on its reads (@data_warning_ms = 50 ms, @data_critical_ms = 200 ms): a
-- query that misses the buffer pool waits for each one (PAGEIOLATCH). Its writes are in the
-- message and not graded - the checkpoint and the lazy writer issue them in bursts of thousands of
-- pages, nobody waits for them, and the stall of a write at the back of that queue measures how
-- deep the queue was. Graded at 50 ms, every busy checkpoint would page (review of 1.147,
-- 2026-10-15). Storage that is slow to write shows on the log, which shares it.
--
-- A gate of @min_ios (default 20) ensures that an idle database with only a handful of I/Os in the
-- sample is not graded on noisy statistical outliers.

SET NOCOUNT ON;

DECLARE @sample_delay char(8) = '00:00:10';
DECLARE @log_write_warning_ms decimal(19,2) = 20;
DECLARE @log_write_critical_ms decimal(19,2) = 50;
DECLARE @data_warning_ms decimal(19,2) = 50;
DECLARE @data_critical_ms decimal(19,2) = 200;
DECLARE @min_ios int = 20;

DECLARE @raw_before TABLE (
    database_id int NOT NULL,
    file_id int NOT NULL,
    type_desc varchar(60) NOT NULL,
    num_of_reads bigint NOT NULL,
    num_of_writes bigint NOT NULL,
    io_stall_read_ms bigint NOT NULL,
    io_stall_write_ms bigint NOT NULL,
    num_of_bytes_read bigint NOT NULL,
    num_of_bytes_written bigint NOT NULL
);

DECLARE @raw_after TABLE (
    database_id int NOT NULL,
    file_id int NOT NULL,
    type_desc varchar(60) NOT NULL,
    num_of_reads bigint NOT NULL,
    num_of_writes bigint NOT NULL,
    io_stall_read_ms bigint NOT NULL,
    io_stall_write_ms bigint NOT NULL,
    num_of_bytes_read bigint NOT NULL,
    num_of_bytes_written bigint NOT NULL
);

INSERT INTO @raw_before (
    database_id,
    file_id,
    type_desc,
    num_of_reads,
    num_of_writes,
    io_stall_read_ms,
    io_stall_write_ms,
    num_of_bytes_read,
    num_of_bytes_written
)
SELECT
    vfs.database_id,
    vfs.file_id,
    mf.type_desc,
    vfs.num_of_reads,
    vfs.num_of_writes,
    vfs.io_stall_read_ms,
    vfs.io_stall_write_ms,
    vfs.num_of_bytes_read,
    vfs.num_of_bytes_written
FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
INNER JOIN sys.master_files AS mf
    ON vfs.database_id = mf.database_id
   AND vfs.file_id = mf.file_id
WHERE mf.type_desc IN ('ROWS', 'LOG');

DECLARE @started datetime2(3) = SYSUTCDATETIME();
WAITFOR DELAY @sample_delay;
-- Measured rather than assumed: scheduler delay or system load can extend the wait,
-- so rates and diagnostics must report the true elapsed sample interval.
DECLARE @elapsed_ms bigint = DATEDIFF(millisecond, @started, SYSUTCDATETIME());

INSERT INTO @raw_after (
    database_id,
    file_id,
    type_desc,
    num_of_reads,
    num_of_writes,
    io_stall_read_ms,
    io_stall_write_ms,
    num_of_bytes_read,
    num_of_bytes_written
)
SELECT
    vfs.database_id,
    vfs.file_id,
    mf.type_desc,
    vfs.num_of_reads,
    vfs.num_of_writes,
    vfs.io_stall_read_ms,
    vfs.io_stall_write_ms,
    vfs.num_of_bytes_read,
    vfs.num_of_bytes_written
FROM sys.dm_io_virtual_file_stats(NULL, NULL) AS vfs
INNER JOIN sys.master_files AS mf
    ON vfs.database_id = mf.database_id
   AND vfs.file_id = mf.file_id
WHERE mf.type_desc IN ('ROWS', 'LOG');

DECLARE @deltas TABLE (
    database_name varchar(256) NOT NULL,
    file_type varchar(10) NOT NULL,
    reads bigint NOT NULL,
    writes bigint NOT NULL,
    io_stall_read_ms bigint NOT NULL,
    io_stall_write_ms bigint NOT NULL,
    bytes_read bigint NOT NULL,
    bytes_written bigint NOT NULL
);

INSERT INTO @deltas (
    database_name,
    file_type,
    reads,
    writes,
    io_stall_read_ms,
    io_stall_write_ms,
    bytes_read,
    bytes_written
)
SELECT
    CAST(DB_NAME(a.database_id) COLLATE DATABASE_DEFAULT AS varchar(256)),
    CASE WHEN a.type_desc = 'LOG' THEN 'log' ELSE 'data' END,
    SUM(a.num_of_reads - b.num_of_reads),
    SUM(a.num_of_writes - b.num_of_writes),
    SUM(a.io_stall_read_ms - b.io_stall_read_ms),
    SUM(a.io_stall_write_ms - b.io_stall_write_ms),
    SUM(a.num_of_bytes_read - b.num_of_bytes_read),
    SUM(a.num_of_bytes_written - b.num_of_bytes_written)
FROM @raw_after AS a
INNER JOIN @raw_before AS b
    ON a.database_id = b.database_id
   AND a.file_id = b.file_id
WHERE DB_NAME(a.database_id) IS NOT NULL
  -- Negative differences mean counters reset (failover, detach/attach, clear). Dropping them is right.
  AND a.num_of_reads >= b.num_of_reads
  AND a.num_of_writes >= b.num_of_writes
  AND a.io_stall_read_ms >= b.io_stall_read_ms
  AND a.io_stall_write_ms >= b.io_stall_write_ms
  AND a.num_of_bytes_read >= b.num_of_bytes_read
  AND a.num_of_bytes_written >= b.num_of_bytes_written
GROUP BY
    CAST(DB_NAME(a.database_id) COLLATE DATABASE_DEFAULT AS varchar(256)),
    CASE WHEN a.type_desc = 'LOG' THEN 'log' ELSE 'data' END
-- Only (database, type) pairs with at least one I/O in the sample.
HAVING SUM(a.num_of_reads - b.num_of_reads) + SUM(a.num_of_writes - b.num_of_writes) > 0;

WITH per_pair AS
(
    SELECT
        d.database_name,
        d.file_type,
        d.reads,
        d.writes,
        d.bytes_read,
        d.bytes_written,
        CONVERT(decimal(19,2), d.io_stall_read_ms * 1.0 / NULLIF(d.reads, 0)) AS read_latency_ms,
        CONVERT(decimal(19,2), d.io_stall_write_ms * 1.0 / NULLIF(d.writes, 0)) AS write_latency_ms,
        CASE
            WHEN d.file_type = 'log' THEN @log_write_warning_ms
            ELSE @data_warning_ms
        END AS warning_ms,
        CASE
            WHEN d.file_type = 'log' THEN @log_write_critical_ms
            ELSE @data_critical_ms
        END AS critical_ms,
        CASE
            WHEN d.file_type = 'log' THEN
                -- Log reads are backups / replication readers and not graded; sample check keys on writes.
                CASE WHEN d.writes >= @min_ios THEN 'YES' ELSE 'NO' END
            ELSE
                -- Data is graded on its reads, so it is their count that has to be enough.
                CASE WHEN d.reads >= @min_ios THEN 'YES' ELSE 'NO' END
        END AS sample_enough
    FROM @deltas AS d
),
graded AS
(
    SELECT
        p.database_name,
        p.file_type,
        p.reads,
        p.writes,
        p.bytes_read,
        p.bytes_written,
        p.read_latency_ms,
        p.write_latency_ms,
        p.warning_ms,
        p.critical_ms,
        p.sample_enough,
        CASE
            WHEN p.file_type = 'log' THEN
                -- LOG: write latency only.
                ISNULL(p.write_latency_ms, 0)
            ELSE
                -- ROWS: read latency only.
                ISNULL(p.read_latency_ms, 0)
        END AS graded_latency_ms
    FROM per_pair AS p
)
SELECT
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
FROM
(
    SELECT
        CAST(g.database_name + ' :: ' + g.file_type AS varchar(256)) AS metric_item,
        CAST(CAST(g.graded_latency_ms AS decimal(19,2)) AS varchar(32)) AS metric_value,
        CAST('ms' AS varchar(32)) AS metric_unit,
        CAST(
            CASE
                WHEN g.sample_enough = 'YES' AND g.graded_latency_ms >= g.critical_ms THEN 'CRITICAL'
                WHEN g.sample_enough = 'YES' AND g.graded_latency_ms >= g.warning_ms THEN 'WARNING'
                ELSE 'OK'
            END AS varchar(16)
        ) AS status,
        CAST(
            'database=' + g.database_name
            + ', file_type=' + g.file_type
            + ', read_latency_ms=' + ISNULL(CAST(g.read_latency_ms AS varchar(32)), '0.00')
            + ', write_latency_ms=' + ISNULL(CAST(g.write_latency_ms AS varchar(32)), '0.00')
            + ', reads=' + CAST(g.reads AS varchar(32))
            + ', writes=' + CAST(g.writes AS varchar(32))
            + ', bytes_read=' + CAST(g.bytes_read AS varchar(32))
            + ', bytes_written=' + CAST(g.bytes_written AS varchar(32))
            + ', sample_ms=' + CAST(@elapsed_ms AS varchar(32))
            + ', sample_enough=' + g.sample_enough
            + ', warning_ms=' + CAST(g.warning_ms AS varchar(32))
            + ', critical_ms=' + CAST(g.critical_ms AS varchar(32))
            + ', note=interval_sample_not_cumulative'
            AS varchar(1000)
        ) AS message,
        CASE
            WHEN g.sample_enough = 'YES' AND g.graded_latency_ms >= g.critical_ms THEN 1
            WHEN g.sample_enough = 'YES' AND g.graded_latency_ms >= g.warning_ms THEN 2
            ELSE 3
        END AS severity_rank,
        g.database_name,
        g.file_type
    FROM graded AS g

    UNION ALL

    SELECT
        CAST('io_latency' AS varchar(256)) AS metric_item,
        CAST('0.00' AS varchar(32)) AS metric_value,
        CAST('ms' AS varchar(32)) AS metric_unit,
        CAST('OK' AS varchar(16)) AS status,
        CAST(
            'No database file accumulated I/O during the ' + CAST(@elapsed_ms AS varchar(32))
            + ' ms sample. The instance was idle, not healthy - this is an observation, not a verdict.'
            AS varchar(1000)
        ) AS message,
        3 AS severity_rank,
        CAST('' AS varchar(256)) AS database_name,
        CAST('' AS varchar(10)) AS file_type
    WHERE NOT EXISTS (SELECT 1 FROM @deltas)
) AS r
ORDER BY
    r.severity_rank,
    r.database_name,
    r.file_type;
