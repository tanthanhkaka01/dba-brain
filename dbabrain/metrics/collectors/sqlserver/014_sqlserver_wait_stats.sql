-- PERFORMANCE_WAIT_STATS (SQL Server 2012+): what the instance is waiting on *right now*.
--
-- `sys.dm_os_wait_stats` is cumulative since the engine started, and reading it directly answers
-- "what has this instance waited on since it booted" — a question nobody asks. Measured on
-- 192.0.2.115 on 2026-08-25, 22 days of uptime: the top wait was `SOS_WORK_DISPATCHER` at
-- 361,631,036 seconds on all 350 samples, status OK on every one, while users sat on `LCK_M_U`
-- for up to 30 minutes. An idle-scheduler counter that accrues a second per scheduler per second
-- out-ranks every real wait forever, and no threshold on a since-start total can ever move.
--
-- So both samples are taken **inside one execution**: read the DMV, wait, read it again, report
-- the difference. Nothing is stored anywhere — not on the target, not in the runtime store — so
-- the metric still grades itself in its own SQL. That is the contract in `docs/04_metrics_engine.md`
-- ("Cumulative counters are not current performance"), and it is why the store-backed interval
-- version of `PERFORMANCE_IO_LATENCY` was withdrawn on 2026-08-11: it needed a policy module, a
-- config file, a store lookup and a per-metric branch in the collector. This needs none of them.
--
-- The exclusion list below is the other half of the fix. On a cumulative reading an idle wait is
-- merely a big number; on an interval it is the *whole* interval, so anything a sleeping scheduler
-- accrues would win every sample. `SOS_WORK_DISPATCHER` and the Query Store / HADR / parallel-redo
-- Tempdb allocation contention (Item 1.161):
-- Contention on tempdb allocation tracking pages (PFS, GAM, SGAM) throttles every concurrent query
-- that creates or destroys temporary objects (#tables, table variables, spool worktables). The standard
-- remedy is adding more equally sized tempdb data files (up to the logical core count, typically 8).
-- Note that on SQL Server 2019+, metadata contention on system tables (addressed by Memory-Optimized
-- TempDB Metadata) presents on different pages and is not this allocation contention row.
--
-- Following Netdata's SQL Server guide, PAGELATCH_UP/EX wait time sustained above 5% of non-benign
-- wait time alongside active allocation waiters raises WARNING, as does observing allocation-page
-- waiters across both sample reads (@tempdb_pagelatch_share_pct = 5.0). This row is always emitted
-- (metric_item 'tempdb_allocation', unit 'tasks', status OK when clear) and never raises CRITICAL
-- because it represents a structural engine configuration recommendation rather than an outage.

SET NOCOUNT ON;

-- How long to watch. Long enough that a two-second lock wait registers, short enough to stay far
-- inside the metric's 60-second timeout and to hold one session for 3% of its 150-second interval.
DECLARE @sample_delay char(8) = '00:00:05';

-- Below this the instance is doing nothing worth grading, and the top wait is whichever benign
-- counter ticked. Grading noise as a finding is how a metric teaches its reader to ignore it.
DECLARE @idle_floor_seconds decimal(19,4) = 1.0;

DECLARE @busy_share_pct decimal(10,2) = 50.0;   -- a wait type has to dominate before it is a verdict
DECLARE @signal_pct_warn decimal(10,2) = 25.0;  -- runnable-queue pressure: waiting for a CPU, not for a resource

-- Worker starvation (THREADPOOL) has its own row, graded at any rank (1.142): a task queued for a
-- worker is a session that cannot run, and the next symptom is logins refused. Any THREADPOOL wait
-- in the sample, or a task in a scheduler's work queue at either read, is WARNING; it is CRITICAL
-- when it lasts - waiters or a work queue at both reads, or THREADPOOL time adding up to this share
-- of the sample (100 = on average one task waited for a worker the whole time).
DECLARE @threadpool_sustained_pct decimal(10,2) = 100.0;

DECLARE @tempdb_pagelatch_share_pct decimal(10,2) = 5.0;

-- The second gate, and the one that keeps this metric quiet. Total wait divided by the sample
-- length is the average number of tasks waiting at any instant, and that only means something
-- against the number of schedulers: 0.96 tasks waiting on a 60-core instance is idle, the same
-- number on a 2-core VM is not. Measured on 192.0.2.115 at 2026-08-25, an instance users were
-- complaining about: 4.79 s of total wait over a 5 s sample, signal wait 49% — a ratio that would
-- have raised WARNING on a box doing almost nothing, every 150 seconds, forever.
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

-- Wait types that are a verdict about the instance rather than a description of its workload.
-- Lock waits are deliberately absent: LOCK_BLOCKING_SESSIONS already alerts on blocking with the
-- session, the chain and the SQL text, and two alerts for one condition is one alert too many.
-- This metric's job for locks is to be the evidence, not a second siren.
DECLARE @pressure TABLE (wait_type nvarchar(60) PRIMARY KEY, reason varchar(80));
INSERT INTO @pressure (wait_type, reason) VALUES
    ('PAGEIOLATCH_SH', 'reading data pages from disk'),
    ('PAGEIOLATCH_EX', 'reading data pages from disk'),
    ('PAGEIOLATCH_UP', 'reading data pages from disk'),
    ('WRITELOG', 'log write latency'),
    ('IO_COMPLETION', 'non-data file I/O'),
    ('ASYNC_IO_COMPLETION', 'non-data file I/O, typically backup or autogrow'),
    ('BACKUPIO', 'backup device throughput'),
    ('RESOURCE_SEMAPHORE', 'query memory grants are queueing'),
    ('RESOURCE_SEMAPHORE_QUERY_COMPILE', 'compile memory is queueing'),
    ('SOS_SCHEDULER_YIELD', 'CPU contention'),
    ('CXPACKET', 'parallelism skew'),
    ('CXCONSUMER', 'parallelism skew'),
    ('PAGELATCH_UP', 'in-memory page contention, typically tempdb allocation'),
    ('PAGELATCH_EX', 'in-memory page contention, typically tempdb allocation');

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

DECLARE @waiting_first int = ISNULL((SELECT COUNT(*) FROM sys.dm_os_waiting_tasks WHERE wait_type = 'THREADPOOL'), 0);
DECLARE @work_queue_first int = ISNULL((SELECT SUM(work_queue_count) FROM sys.dm_os_schedulers WHERE status = 'VISIBLE ONLINE'), 0);

DECLARE @alloc_raw_first TABLE (page_id bigint);
INSERT INTO @alloc_raw_first (page_id)
SELECT TRY_CAST(PARSENAME(REPLACE(resource_description, ':', '.'), 1) AS bigint)
FROM sys.dm_os_waiting_tasks
WHERE wait_type LIKE 'PAGELATCH_%'
  AND resource_description IS NOT NULL
  AND resource_description NOT LIKE '%[^0-9:]%'
  AND PARSENAME(REPLACE(resource_description, ':', '.'), 4) IS NULL
  AND TRY_CAST(PARSENAME(REPLACE(resource_description, ':', '.'), 3) AS int) = 2
  AND TRY_CAST(PARSENAME(REPLACE(resource_description, ':', '.'), 2) AS int) IS NOT NULL
  AND TRY_CAST(PARSENAME(REPLACE(resource_description, ':', '.'), 1) AS bigint) IS NOT NULL;

DECLARE @alloc_waiters_first int = ISNULL((
    SELECT COUNT(*)
    FROM @alloc_raw_first
    WHERE page_id IS NOT NULL
      AND (
          page_id = 1 OR page_id % 8088 = 0
          OR page_id = 2 OR page_id % 511232 = 0
          OR page_id = 3 OR (page_id - 1) % 511232 = 0
      )
), 0);

DECLARE @started datetime2(3) = SYSUTCDATETIME();
WAITFOR DELAY @sample_delay;
-- Measured rather than assumed: a busy scheduler can return from WAITFOR late, and a rate
-- computed against the requested delay would then overstate every wait on the busiest instances.
DECLARE @elapsed_ms bigint = DATEDIFF(millisecond, @started, SYSUTCDATETIME());

DECLARE @waiting_second int = ISNULL((SELECT COUNT(*) FROM sys.dm_os_waiting_tasks WHERE wait_type = 'THREADPOOL'), 0);
DECLARE @work_queue_second int = ISNULL((SELECT SUM(work_queue_count) FROM sys.dm_os_schedulers WHERE status = 'VISIBLE ONLINE'), 0);
DECLARE @max_workers int = ISNULL((SELECT max_workers_count FROM sys.dm_os_sys_info), 0);
DECLARE @active_workers int = ISNULL((SELECT SUM(active_workers_count) FROM sys.dm_os_schedulers WHERE status = 'VISIBLE ONLINE'), 0);

DECLARE @alloc_raw_second TABLE (page_id bigint);
INSERT INTO @alloc_raw_second (page_id)
SELECT TRY_CAST(PARSENAME(REPLACE(resource_description, ':', '.'), 1) AS bigint)
FROM sys.dm_os_waiting_tasks
WHERE wait_type LIKE 'PAGELATCH_%'
  AND resource_description IS NOT NULL
  AND resource_description NOT LIKE '%[^0-9:]%'
  AND PARSENAME(REPLACE(resource_description, ':', '.'), 4) IS NULL
  AND TRY_CAST(PARSENAME(REPLACE(resource_description, ':', '.'), 3) AS int) = 2
  AND TRY_CAST(PARSENAME(REPLACE(resource_description, ':', '.'), 2) AS int) IS NOT NULL
  AND TRY_CAST(PARSENAME(REPLACE(resource_description, ':', '.'), 1) AS bigint) IS NOT NULL;

DECLARE @alloc_waiters_second int = 0;
DECLARE @alloc_pfs int = 0;
DECLARE @alloc_gam int = 0;
DECLARE @alloc_sgam int = 0;

SELECT
    @alloc_waiters_second = COUNT(*),
    @alloc_pfs = ISNULL(SUM(CASE WHEN page_id = 1 OR page_id % 8088 = 0 THEN 1 ELSE 0 END), 0),
    @alloc_gam = ISNULL(SUM(CASE WHEN page_id = 2 OR page_id % 511232 = 0 THEN 1 ELSE 0 END), 0),
    @alloc_sgam = ISNULL(SUM(CASE WHEN page_id = 3 OR (page_id - 1) % 511232 = 0 THEN 1 ELSE 0 END), 0)
FROM @alloc_raw_second
WHERE page_id IS NOT NULL
  AND (
      page_id = 1 OR page_id % 8088 = 0
      OR page_id = 2 OR page_id % 511232 = 0
      OR page_id = 3 OR (page_id - 1) % 511232 = 0
  );

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
-- A negative difference means the counters were cleared between the two reads (DBCC SQLPERF, or
-- a failover). Dropping the row is right: there is no interval to report for it.
HAVING SUM(r.wait_time_ms) - MIN(f.wait_time_ms) > 0;

DECLARE @total_ms bigint = ISNULL((SELECT SUM(wait_ms) FROM @delta), 0);
DECLARE @signal_pct decimal(10,2) =
    ISNULL((SELECT SUM(signal_ms) * 100.0 / NULLIF(SUM(wait_ms), 0) FROM @delta), 0);
DECLARE @avg_waiting_tasks decimal(19,2) = @total_ms * 1.0 / NULLIF(@elapsed_ms, 0);

DECLARE @pagelatch_up_ex_ms bigint =
    ISNULL((SELECT SUM(wait_ms) FROM @delta WHERE wait_type IN ('PAGELATCH_UP', 'PAGELATCH_EX')), 0);
DECLARE @pagelatch_share_pct decimal(10,2) =
    ISNULL((SELECT @pagelatch_up_ex_ms * 100.0 / NULLIF(@total_ms, 0)), 0);
DECLARE @tempdb_data_files int =
    ISNULL((SELECT COUNT(*) FROM sys.master_files WHERE database_id = 2 AND type_desc = 'ROWS'), 0);

-- Each read summed on its own side, not joined row to row: the DMV can return a wait type twice
-- on some builds (the 2008 duplicate above), and a join would multiply both sums by that count.
-- Read from the raw reads rather than @delta, which keeps only a wait whose time grew: a task
-- still waiting for a worker at the second read has added to the count and not yet to the time.
DECLARE @tp_wait_ms bigint =
    ISNULL((SELECT SUM(wait_time_ms) FROM @raw_after WHERE wait_type = 'THREADPOOL'), 0)
  - ISNULL((SELECT SUM(wait_time_ms) FROM @raw_before WHERE wait_type = 'THREADPOOL'), 0);
IF @tp_wait_ms < 0 SET @tp_wait_ms = 0;   -- counters cleared between the reads

DECLARE @tp_tasks bigint =
    ISNULL((SELECT SUM(waiting_tasks_count) FROM @raw_after WHERE wait_type = 'THREADPOOL'), 0)
  - ISNULL((SELECT SUM(waiting_tasks_count) FROM @raw_before WHERE wait_type = 'THREADPOOL'), 0);
IF @tp_tasks < 0 SET @tp_tasks = 0;

-- Ranked once, so the top wait and its runners-up are both addressed by rank. A correlated
-- derived table would have been the obvious way to write "the three below the top one" and it
-- does not compile: a derived table cannot see a column of the query it sits in.
DECLARE @ranked TABLE (place int PRIMARY KEY, wait_type nvarchar(60), wait_ms bigint,
                       signal_ms bigint, tasks bigint);
INSERT INTO @ranked (place, wait_type, wait_ms, signal_ms, tasks)
SELECT ROW_NUMBER() OVER (ORDER BY wait_ms DESC), wait_type, wait_ms, signal_ms, tasks
FROM @delta;

DECLARE @runners_up nvarchar(400) =
    ISNULL(STUFF((
        SELECT ' | ' + r.wait_type + ' '
               + CAST(CAST(r.wait_ms / 1000.0 AS decimal(19,2)) AS varchar(32)) + 's'
        FROM @ranked AS r
        WHERE r.place BETWEEN 2 AND 4
        ORDER BY r.place
        FOR XML PATH(''), TYPE).value('.', 'nvarchar(400)'), 1, 3, ''), 'none');

SELECT
    CAST(d.wait_type AS varchar(256)) AS metric_item,
    CAST(CAST(d.wait_ms / 1000.0 AS decimal(19,2)) AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    CAST(
        CASE
            -- Worker starvation is graded by the dedicated threadpool row below rather than by the
            -- top wait, so an instance starving for workers at rank 2 or 3 still alerts, and a top
            -- wait that is THREADPOOL does not page twice for the same condition.
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
        + ', avg_wait_ms=' + ISNULL(CAST(CAST(d.wait_ms * 1.0 / NULLIF(d.tasks, 0) AS decimal(19,2)) AS varchar(32)), 'n/a')
        + ', signal_wait_pct=' + CAST(@signal_pct AS varchar(32))
        + ', all_waits_seconds=' + CAST(CAST(@total_ms / 1000.0 AS decimal(19,2)) AS varchar(32))
        + ', avg_waiting_tasks=' + CAST(@avg_waiting_tasks AS varchar(32))
        + '/' + CAST(@cpu_count AS varchar(12)) + 'cpu'
        + ', sample_ms=' + CAST(@elapsed_ms AS varchar(32))
        + ISNULL(', meaning=' + (SELECT p.reason FROM @pressure AS p WHERE p.wait_type = d.wait_type), '')
        + CASE WHEN d.wait_type = 'THREADPOOL' THEN ', graded_on=threadpool' ELSE '' END
        + ', runners_up=' + @runners_up
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
         + ' ms sample. The instance was idle, not healthy — this is an observation, not a verdict.'
         AS varchar(1000)) AS message
WHERE NOT EXISTS (SELECT 1 FROM @delta)

UNION ALL

SELECT
    CAST('threadpool' AS varchar(256)) AS metric_item,
    CAST(CAST(@tp_wait_ms / 1000.0 AS decimal(19,2)) AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    CAST(
        CASE
            WHEN (@waiting_first > 0 AND @waiting_second > 0)
                 OR (@work_queue_first > 0 AND @work_queue_second > 0)
                 OR (@tp_wait_ms * 100.0 / NULLIF(@elapsed_ms, 0) >= @threadpool_sustained_pct)
            THEN 'CRITICAL'
            WHEN @tp_wait_ms > 0
                 OR @tp_tasks > 0
                 OR @waiting_first > 0
                 OR @waiting_second > 0
                 OR @work_queue_first > 0
                 OR @work_queue_second > 0
            THEN 'WARNING'
            ELSE 'OK'
        END AS varchar(16)) AS status,
    CAST(
        'threadpool_wait_ms=' + CAST(@tp_wait_ms AS varchar(32))
        + ', threadpool_tasks=' + CAST(@tp_tasks AS varchar(32))
        + ', waiting_first=' + CAST(@waiting_first AS varchar(32))
        + ', waiting_second=' + CAST(@waiting_second AS varchar(32))
        + ', work_queue_first=' + CAST(@work_queue_first AS varchar(32))
        + ', work_queue_second=' + CAST(@work_queue_second AS varchar(32))
        + ', active_workers=' + CAST(@active_workers AS varchar(32))
        + ', max_workers=' + CAST(@max_workers AS varchar(32))
        + ', sample_ms=' + CAST(@elapsed_ms AS varchar(32))
        AS varchar(1000)) AS message

UNION ALL

SELECT
    CAST('tempdb_allocation' AS varchar(256)) AS metric_item,
    CAST(@alloc_waiters_second AS varchar(32)) AS metric_value,
    CAST('tasks' AS varchar(32)) AS metric_unit,
    CAST(
        CASE
            WHEN (@alloc_waiters_first > 0 AND @alloc_waiters_second > 0)
                 OR ((@alloc_waiters_first > 0 OR @alloc_waiters_second > 0)
                     AND @pagelatch_share_pct >= @tempdb_pagelatch_share_pct)
            THEN 'WARNING'
            ELSE 'OK'
        END AS varchar(16)) AS status,
    CAST(
        'waiters_first=' + CAST(@alloc_waiters_first AS varchar(32))
        + ', waiters_second=' + CAST(@alloc_waiters_second AS varchar(32))
        + ', pfs=' + CAST(@alloc_pfs AS varchar(32))
        + ', gam=' + CAST(@alloc_gam AS varchar(32))
        + ', sgam=' + CAST(@alloc_sgam AS varchar(32))
        + ', pagelatch_up_ex_ms=' + CAST(@pagelatch_up_ex_ms AS varchar(32))
        + ', pagelatch_share_pct=' + CAST(@pagelatch_share_pct AS varchar(32))
        + ', tempdb_data_files=' + CAST(@tempdb_data_files AS varchar(32))
        + ', cpu_count=' + CAST(@cpu_count AS varchar(12))
        + ', sample_ms=' + CAST(@elapsed_ms AS varchar(32))
        AS varchar(1000)) AS message;
