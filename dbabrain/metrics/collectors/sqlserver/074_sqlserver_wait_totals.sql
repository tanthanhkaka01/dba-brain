-- PERFORMANCE_WAIT_TOTALS: the cumulative wait counters, reported raw so an interval can be taken.
--
-- PERFORMANCE_WAIT_STATS already reads this DMV, and this metric is not a duplicate of it. The two
-- answer different questions and neither can answer the other's:
--
--   * PERFORMANCE_WAIT_STATS watches for five seconds inside one execution and grades what it saw.
--     It is the alert, it runs every 150 seconds, and it stores nothing -- so there is no way to
--     ask it what the instance waited on between 08:00 and 09:00. Its five-second window sees 3%
--     of the interval; a lock storm that started and ended between two runs never happened.
--   * This one stores the totals and grades nothing. Two collections subtracted give the real
--     wait profile of the whole hour, with no sampling gap, which is what a report needs after
--     the fact and what an alert has no use for.
--
-- **The item set is fixed, not a top-N.** A "top waits" collector picks different rows every run,
-- and two collections that do not share an item cannot be subtracted -- the report would silently
-- have nothing to difference for exactly the wait that just became interesting. So every watched
-- type is emitted every time, zero included, and a type that vanishes from the DMV keeps its row.
--
-- The watch list is the pressure list from 014_sqlserver_wait_stats.sql. It is duplicated here
-- rather than shared because a collector is a single file executed on the target with nothing to
-- import; 014 is the one to change first, and this file follows it.
--
-- Lock waits are aggregated into one row rather than listed per mode. LCK_M_U, LCK_M_IX and the
-- rest are the same condition seen through different lock types, and eleven rows of it would
-- crowd out everything else in a fixed-size item set.

SET NOCOUNT ON;

-- The restart marker. Wait counters also reset on DBCC SQLPERF('sys.dm_os_wait_stats', CLEAR),
-- which this cannot see -- but a cleared counter goes backwards, and the report drops a pair
-- whose difference is negative for that reason.
DECLARE @counters_since varchar(19) =
    CONVERT(varchar(19), (SELECT sqlserver_start_time FROM sys.dm_os_sys_info), 120);

DECLARE @cpu_count varchar(12) =
    CAST((SELECT cpu_count FROM sys.dm_os_sys_info) AS varchar(12));

-- Idle and background waits, excluded from the all_waits total. On a cumulative reading an idle
-- wait is merely a big number; in a total it is most of the number. SOS_WORK_DISPATCHER alone
-- accrued 361,631,036 seconds over 22 days on 192.0.2.115 -- more than every real wait on the
-- instance put together. Kept in step with the same list in 014_sqlserver_wait_stats.sql.
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
    ('PWAIT_DIRECTLOGCONSUMER_GETNEXT'), ('QDS_ASYNC_QUEUE'),
    ('QDS_CLEANUP_STALE_QUERIES_TASK_MAIN_LOOP_SLEEP'),
    ('QDS_PERSIST_TASK_MAIN_LOOP_SLEEP'), ('QDS_SHUTDOWN_QUEUE'),
    ('REDO_THREAD_PENDING_WORK'), ('REQUEST_FOR_DEADLOCK_SEARCH'), ('RESOURCE_QUEUE'),
    ('SERVER_IDLE_CHECK'), ('SLEEP_BPOOL_FLUSH'), ('SLEEP_DBSTARTUP'), ('SLEEP_DCOMSTARTUP'),
    ('SLEEP_MASTERDBREADY'), ('SLEEP_MASTERMDREADY'), ('SLEEP_MASTERUPGRADED'),
    ('SLEEP_MSDBSTARTUP'), ('SLEEP_SYSTEMTASK'), ('SLEEP_TASK'), ('SLEEP_TEMPDBSTARTUP'),
    ('SNI_HTTP_ACCEPT'), ('SOS_WORK_DISPATCHER'), ('SP_SERVER_DIAGNOSTICS_SLEEP'),
    ('SQLTRACE_BUFFER_FLUSH'), ('SQLTRACE_INCREMENTAL_FLUSH_SLEEP'), ('SQLTRACE_WAIT_ENTRIES'),
    ('VDI_CLIENT_OTHER'), ('WAIT_FOR_RESULTS'), ('WAITFOR'), ('WAITFOR_TASKSHUTDOWN'),
    ('XE_DISPATCHER_JOIN'), ('XE_DISPATCHER_WAIT'), ('XE_LIVE_TARGET_TVF'), ('XE_TIMER_EVENT');

-- The waits worth carrying an interval for, each with the sentence a reader needs to act on it.
DECLARE @watched TABLE (metric_item varchar(64) PRIMARY KEY, wait_type nvarchar(60) NOT NULL,
                        reason varchar(80) NOT NULL);
INSERT INTO @watched (metric_item, wait_type, reason) VALUES
    ('PAGEIOLATCH_SH', 'PAGEIOLATCH_SH', 'reading data pages from disk'),
    ('PAGEIOLATCH_EX', 'PAGEIOLATCH_EX', 'reading data pages from disk'),
    ('PAGEIOLATCH_UP', 'PAGEIOLATCH_UP', 'reading data pages from disk'),
    ('WRITELOG', 'WRITELOG', 'log write latency'),
    ('IO_COMPLETION', 'IO_COMPLETION', 'non-data file I/O'),
    ('ASYNC_IO_COMPLETION', 'ASYNC_IO_COMPLETION', 'non-data file I/O, backup or autogrow'),
    ('BACKUPIO', 'BACKUPIO', 'backup device throughput'),
    ('RESOURCE_SEMAPHORE', 'RESOURCE_SEMAPHORE', 'query memory grants are queueing'),
    ('RESOURCE_SEMAPHORE_QUERY_COMPILE', 'RESOURCE_SEMAPHORE_QUERY_COMPILE',
     'compile memory is queueing'),
    ('SOS_SCHEDULER_YIELD', 'SOS_SCHEDULER_YIELD', 'CPU contention'),
    ('THREADPOOL', 'THREADPOOL', 'worker starvation, logins are next to be refused'),
    ('CXPACKET', 'CXPACKET', 'parallelism skew'),
    ('CXCONSUMER', 'CXCONSUMER', 'parallelism skew'),
    ('PAGELATCH_UP', 'PAGELATCH_UP', 'in-memory page contention, typically tempdb allocation'),
    ('PAGELATCH_EX', 'PAGELATCH_EX', 'in-memory page contention, typically tempdb allocation'),
    ('LATCH_EX', 'LATCH_EX', 'non-page latch contention inside the engine'),
    ('LOGBUFFER', 'LOGBUFFER', 'log buffer is full, the write path is the bottleneck');

-- Materialised before it is aggregated, for the reason spelled out in 014: on some builds
-- sys.dm_os_wait_stats returns split groups for one wait_type, and aggregating straight out of it
-- fails the primary key. Measured on a major-version 10 instance at 2026-08-25.
DECLARE @raw TABLE (wait_type nvarchar(60), wait_time_ms bigint,
                    signal_wait_time_ms bigint, waiting_tasks_count bigint);
INSERT INTO @raw (wait_type, wait_time_ms, signal_wait_time_ms, waiting_tasks_count)
SELECT wait_type, wait_time_ms, signal_wait_time_ms, waiting_tasks_count
FROM sys.dm_os_wait_stats;

DECLARE @rows TABLE (
    metric_item varchar(64) NOT NULL PRIMARY KEY,
    wait_ms     bigint      NOT NULL,
    signal_ms   bigint      NOT NULL,
    tasks       bigint      NOT NULL,
    reason      varchar(80) NOT NULL
);

-- LEFT JOIN, and ISNULL to zero: a watched type absent from this build still gets its row, so the
-- item set is the same on every collection and the report always has something to subtract.
INSERT INTO @rows (metric_item, wait_ms, signal_ms, tasks, reason)
SELECT w.metric_item,
       ISNULL(SUM(r.wait_time_ms), 0),
       ISNULL(SUM(r.signal_wait_time_ms), 0),
       ISNULL(SUM(r.waiting_tasks_count), 0),
       MIN(w.reason)
FROM @watched AS w
LEFT JOIN @raw AS r ON r.wait_type = w.wait_type
GROUP BY w.metric_item;

-- Every lock mode as one row: LCK_M_U and LCK_M_IX are one condition seen twice.
INSERT INTO @rows (metric_item, wait_ms, signal_ms, tasks, reason)
SELECT 'lock_waits',
       ISNULL(SUM(r.wait_time_ms), 0),
       ISNULL(SUM(r.signal_wait_time_ms), 0),
       ISNULL(SUM(r.waiting_tasks_count), 0),
       'blocking: every LCK_M_* mode together'
FROM @raw AS r
WHERE r.wait_type LIKE 'LCK[_]M[_]%';

-- The denominator. Every share and every "what fraction of the hour" reads against this, and it
-- is the non-benign total on purpose: including the idle counters would make every real wait
-- round to zero percent.
INSERT INTO @rows (metric_item, wait_ms, signal_ms, tasks, reason)
SELECT 'all_waits',
       ISNULL(SUM(r.wait_time_ms), 0),
       ISNULL(SUM(r.signal_wait_time_ms), 0),
       ISNULL(SUM(r.waiting_tasks_count), 0),
       'every non-benign wait together, the denominator for a share'
FROM @raw AS r
WHERE NOT EXISTS (SELECT 1 FROM @benign AS b WHERE b.wait_type = r.wait_type);

SELECT
    CAST(r.metric_item AS varchar(256)) AS metric_item,
    CAST(r.wait_ms AS varchar(32)) AS metric_value,
    CAST('ms' AS varchar(32)) AS metric_unit,
    -- Always OK. The alert on these waits is PERFORMANCE_WAIT_STATS; a total is evidence, and
    -- two sirens for one condition is one siren too many.
    CAST('OK' AS varchar(16)) AS status,
    CAST(
        'value=' + CAST(r.wait_ms AS varchar(32))
        + ', unit=ms'
        + ', signal_wait_ms=' + CAST(r.signal_ms AS varchar(32))
        + ', waiting_tasks=' + CAST(r.tasks AS varchar(32))
        + ', counters_since=' + @counters_since
        + ', cpu_count=' + @cpu_count
        + ', meaning=' + r.reason
        + ', source=dm_os_wait_stats'
        + ', note=cumulative_since_start_subtract_two_collections'
        AS varchar(1000)) AS message
FROM @rows AS r
ORDER BY r.metric_item;
