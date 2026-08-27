-- PERFORMANCE_WAIT_STATS (SQL Server 2008 R2 and older): what the instance is waiting on now.
--
-- Same correction as the modern variant, kept deliberately plainer because these builds are out
-- of support and cannot be exercised before a deploy. `sys.dm_os_wait_stats` is cumulative since
-- the engine started, so a single read grades an average over weeks and never moves; both samples
-- are therefore taken inside this one execution and the difference is what is reported. Nothing
-- is stored on the target or in the runtime store — the metric still grades itself in its own SQL.
--
-- The benign list gained `SOS_WORK_DISPATCHER` and friends for the same reason as the modern
-- variant: on an interval, an idle scheduler's counter *is* the whole interval, so anything a
-- sleeping worker accrues wins every sample unless it is excluded.

SET NOCOUNT ON;

DECLARE @sample_delay char(8) = '00:00:05';
DECLARE @idle_floor_seconds decimal(19,4) = 1.0;
DECLARE @busy_share_pct decimal(10,2) = 50.0;
DECLARE @signal_pct_warn decimal(10,2) = 25.0;

-- The gate that keeps this quiet on a lightly loaded instance: total wait divided by the sample
-- length is the average number of tasks waiting at any instant, and that only means anything
-- against the number of schedulers. See the modern variant for the measurement behind it.
DECLARE @cpu_count int = (SELECT cpu_count FROM sys.dm_os_sys_info);
DECLARE @busy_floor_tasks decimal(19,4) =
    CASE WHEN @cpu_count * 0.10 < 2.0 THEN 2.0 ELSE @cpu_count * 0.10 END;

DECLARE @benign TABLE (wait_type nvarchar(60) PRIMARY KEY);
INSERT INTO @benign (wait_type) VALUES
    ('BROKER_EVENTHANDLER'), ('BROKER_RECEIVE_WAITFOR'), ('BROKER_TASK_STOP'),
    ('BROKER_TO_FLUSH'), ('BROKER_TRANSMITTER'), ('CHECKPOINT_QUEUE'),
    ('CHKPT'), ('CLR_AUTO_EVENT'), ('CLR_MANUAL_EVENT'), ('CLR_SEMAPHORE'),
    ('DBMIRROR_DBM_EVENT'), ('DBMIRROR_EVENTS_QUEUE'), ('DBMIRROR_WORKER_QUEUE'),
    ('DBMIRRORING_CMD'), ('DIRTY_PAGE_POLL'), ('DISPATCHER_QUEUE_SEMAPHORE'),
    ('EXECSYNC'), ('FSAGENT'), ('FT_IFTS_SCHEDULER_IDLE_WAIT'), ('FT_IFTSHC_MUTEX'),
    ('HADR_CLUSAPI_CALL'), ('HADR_FILESTREAM_IOMGR_IOCOMPLETION'), ('HADR_LOGCAPTURE_WAIT'),
    ('HADR_NOTIFICATION_DEQUEUE'), ('HADR_TIMER_TASK'), ('HADR_WORK_QUEUE'),
    ('LAZYWRITER_SLEEP'), ('LOGMGR_QUEUE'), ('MEMORY_ALLOCATION_EXT'),
    ('ONDEMAND_TASK_QUEUE'), ('PARALLEL_REDO_DRAIN_WORKER'), ('PARALLEL_REDO_LOG_CACHE'),
    ('PARALLEL_REDO_TRAN_LIST'), ('PARALLEL_REDO_WORKER_SYNC'), ('PARALLEL_REDO_WORKER_WAIT_WORK'),
    ('PREEMPTIVE_XE_GETTARGETSTATE'), ('PVS_PREALLOC'), ('PWAIT_ALL_COMPONENTS_INITIALIZED'),
    ('PWAIT_DIRECTLOGCONSUMER_GETNEXT'), ('QDS_ASYNC_QUEUE'), ('QDS_CLEANUP_STALE_QUERIES_TASK_MAIN_LOOP_SLEEP'),
    ('QDS_PERSIST_TASK_MAIN_LOOP_SLEEP'), ('QDS_SHUTDOWN_QUEUE'),
    ('REDO_THREAD_PENDING_WORK'), ('REQUEST_FOR_DEADLOCK_SEARCH'), ('RESOURCE_QUEUE'),
    ('SERVER_IDLE_CHECK'), ('SLEEP_BPOOL_FLUSH'), ('SLEEP_DBSTARTUP'), ('SLEEP_DCOMSTARTUP'),
    ('SLEEP_MASTERDBREADY'), ('SLEEP_MASTERMDREADY'), ('SLEEP_MASTERUPGRADED'),
    ('SLEEP_MSDBSTARTUP'), ('SLEEP_SYSTEMTASK'), ('SLEEP_TASK'), ('SLEEP_TEMPDBSTARTUP'),
    ('SNI_HTTP_ACCEPT'), ('SOS_WORK_DISPATCHER'), ('SP_SERVER_DIAGNOSTICS_SLEEP'),
    ('SQLTRACE_BUFFER_FLUSH'), ('SQLTRACE_INCREMENTAL_FLUSH_SLEEP'), ('SQLTRACE_WAIT_ENTRIES'),
    ('VDI_CLIENT_OTHER'), ('WAIT_FOR_RESULTS'), ('WAITFOR'), ('WAITFOR_TASKSHUTDOWN'),
    ('XE_DISPATCHER_JOIN'), ('XE_DISPATCHER_WAIT'), ('XE_LIVE_TARGET_TVF'), ('XE_TIMER_EVENT');

DECLARE @pressure TABLE (wait_type nvarchar(60) PRIMARY KEY);
INSERT INTO @pressure (wait_type) VALUES
    ('PAGEIOLATCH_SH'), ('PAGEIOLATCH_EX'), ('PAGEIOLATCH_UP'), ('WRITELOG'),
    ('IO_COMPLETION'), ('ASYNC_IO_COMPLETION'), ('BACKUPIO'),
    ('RESOURCE_SEMAPHORE'), ('RESOURCE_SEMAPHORE_QUERY_COMPILE'),
    ('SOS_SCHEDULER_YIELD'), ('CXPACKET'), ('PAGELATCH_UP'), ('PAGELATCH_EX');

-- Each sample is materialised before it is aggregated, and that is not tidiness. Aggregating
-- straight out of `sys.dm_os_wait_stats` returns split groups on some builds: measured on a 2008
-- instance (major version 10) at 2026-08-25, where the DMV holds 490 rows and 490 distinct
-- wait_types under either collation, and `INSERT ... SELECT ... GROUP BY wait_type` still failed
-- with "Violation of PRIMARY KEY constraint ... Cannot insert duplicate key in object '@first'".
-- The DMV reports an order the aggregate trusts and does not actually have. Copying the rows into
-- a table variable first gives the aggregate a well-behaved input, and it works on every build in
-- the estate (major versions 10, 11, 15, 16, 17).
DECLARE @raw_before TABLE (wait_type nvarchar(60), wait_time_ms bigint,
                           signal_wait_time_ms bigint, waiting_tasks_count bigint);
DECLARE @raw_after TABLE (wait_type nvarchar(60), wait_time_ms bigint,
                          signal_wait_time_ms bigint, waiting_tasks_count bigint);

INSERT INTO @raw_before (wait_type, wait_time_ms, signal_wait_time_ms, waiting_tasks_count)
SELECT wait_type, wait_time_ms, signal_wait_time_ms, waiting_tasks_count FROM sys.dm_os_wait_stats;

DECLARE @started datetime = GETUTCDATE();
WAITFOR DELAY @sample_delay;
-- Measured rather than assumed: a busy scheduler can return from WAITFOR late, and a rate
-- computed against the requested delay would then overstate every wait on the busiest instances.
DECLARE @elapsed_ms bigint = DATEDIFF(millisecond, @started, GETUTCDATE());

INSERT INTO @raw_after (wait_type, wait_time_ms, signal_wait_time_ms, waiting_tasks_count)
SELECT wait_type, wait_time_ms, signal_wait_time_ms, waiting_tasks_count FROM sys.dm_os_wait_stats;

DECLARE @first TABLE (wait_type nvarchar(60) PRIMARY KEY, wait_time_ms bigint NOT NULL,
                      signal_wait_time_ms bigint NOT NULL, waiting_tasks_count bigint NOT NULL);

INSERT INTO @first (wait_type, wait_time_ms, signal_wait_time_ms, waiting_tasks_count)
SELECT r.wait_type, SUM(r.wait_time_ms), SUM(r.signal_wait_time_ms), SUM(r.waiting_tasks_count)
FROM @raw_before AS r
WHERE NOT EXISTS (SELECT 1 FROM @benign AS b WHERE b.wait_type = r.wait_type)
GROUP BY r.wait_type;

DECLARE @delta TABLE (wait_type nvarchar(60) PRIMARY KEY, wait_ms bigint NOT NULL,
                      signal_ms bigint NOT NULL, tasks bigint NOT NULL);

INSERT INTO @delta (wait_type, wait_ms, signal_ms, tasks)
SELECT r.wait_type,
       SUM(r.wait_time_ms) - MIN(f.wait_time_ms),
       SUM(r.signal_wait_time_ms) - MIN(f.signal_wait_time_ms),
       SUM(r.waiting_tasks_count) - MIN(f.waiting_tasks_count)
FROM @raw_after AS r
INNER JOIN @first AS f ON f.wait_type = r.wait_type
GROUP BY r.wait_type
-- Negative means the counters were cleared between the reads; there is no interval to report.
HAVING SUM(r.wait_time_ms) - MIN(f.wait_time_ms) > 0;

DECLARE @total_ms bigint = ISNULL((SELECT SUM(wait_ms) FROM @delta), 0);
DECLARE @signal_pct decimal(10,2) =
    ISNULL((SELECT SUM(signal_ms) * 100.0 / NULLIF(SUM(wait_ms), 0) FROM @delta), 0);
DECLARE @avg_waiting_tasks decimal(19,2) = @total_ms * 1.0 / NULLIF(@elapsed_ms, 0);

DECLARE @ranked TABLE (place int PRIMARY KEY, wait_type nvarchar(60), wait_ms bigint, tasks bigint);
INSERT INTO @ranked (place, wait_type, wait_ms, tasks)
SELECT ROW_NUMBER() OVER (ORDER BY wait_ms DESC), wait_type, wait_ms, tasks FROM @delta;

SELECT
    CAST(d.wait_type AS varchar(256)) AS metric_item,
    CAST(CAST(d.wait_ms / 1000.0 AS decimal(19,2)) AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    CAST(
        CASE
            WHEN d.wait_type = 'THREADPOOL' THEN 'CRITICAL'
            WHEN d.wait_ms / 1000.0 < @idle_floor_seconds THEN 'OK'
            WHEN @avg_waiting_tasks < @busy_floor_tasks THEN 'OK'
            WHEN EXISTS (SELECT 1 FROM @pressure AS p WHERE p.wait_type = d.wait_type)
                 AND (d.wait_ms * 100.0 / @total_ms) >= @busy_share_pct THEN 'WARNING'
            WHEN @signal_pct >= @signal_pct_warn THEN 'WARNING'
            ELSE 'OK'
        END AS varchar(16)) AS status,
    CAST(
        'top_wait_type=' + d.wait_type
        + ', wait_seconds=' + CAST(CAST(d.wait_ms / 1000.0 AS decimal(19,2)) AS varchar(32))
        + ', share_pct=' + CAST(CAST(d.wait_ms * 100.0 / @total_ms AS decimal(10,2)) AS varchar(32))
        + ', waiting_tasks=' + CAST(d.tasks AS varchar(32))
        + ', signal_wait_pct=' + CAST(@signal_pct AS varchar(32))
        + ', all_waits_seconds=' + CAST(CAST(@total_ms / 1000.0 AS decimal(19,2)) AS varchar(32))
        + ', avg_waiting_tasks=' + CAST(@avg_waiting_tasks AS varchar(32))
        + '/' + CAST(@cpu_count AS varchar(12)) + 'cpu'
        + ', sample_ms=' + CAST(@elapsed_ms AS varchar(32))
        + ', note=interval_sample_not_cumulative'
        AS varchar(1000)) AS message
FROM @ranked AS d
WHERE d.place = 1

UNION ALL

SELECT
    CAST('wait_stats' AS varchar(256)) AS metric_item,
    CAST('0' AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST('No non-benign wait accumulated during the ' + CAST(@elapsed_ms AS varchar(32))
         + ' ms sample. The instance was idle, not healthy.' AS varchar(1000)) AS message
WHERE NOT EXISTS (SELECT 1 FROM @delta);
