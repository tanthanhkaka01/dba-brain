-- MEMORY_GRANTS_PENDING (SQL Server 2008 / 2008 R2): memory-grant starvation and workspace memory queueing.
--
-- Why pending memory grants are sampled twice within one run:
-- Query workspace memory is allocated from resource semaphores. When large queries consume the
-- workspace memory pool, subsequent queries requiring memory grants for sorts, hashes, or bulk
-- operations are forced to queue behind RESOURCE_SEMAPHORE waits.
-- Stateless collectors cannot remember the previous sample across collections, so two samples are
-- taken inside one execution separated by a 5-second WAITFOR DELAY. This prevents a transient 100 ms
-- grant wait from alerting while ensuring sustained starvation is detected immediately.
--
-- Approved grading rule:
-- - pending > 5 in the second sample: CRITICAL (severe workspace memory exhaustion).
-- - pending > 0 in both samples: WARNING (sustained query queueing across the 5-second interval).
-- - otherwise: OK (transient blip or no waiters).
--
-- Active RESOURCE_SEMAPHORE waits in sys.dm_os_waiting_tasks are recorded in the message.
-- sys.dm_os_wait_stats is cumulative since boot and is deliberately not graded.
DECLARE @pending_grants_critical int = 5;
DECLARE @pending_grants_warning int = 0;
DECLARE @sample_delay_seconds int = 5;

SET NOCOUNT ON;

-- Sample 1: read instantaneous perf counter and active grant waiters
DECLARE @sample1_perf_pending int;
DECLARE @sample1_dmv_waiters int;
SET @sample1_perf_pending = 0;
SET @sample1_dmv_waiters = 0;

SELECT @sample1_perf_pending = ISNULL(cntr_value, 0)
FROM sys.dm_os_performance_counters
WHERE counter_name = 'Memory Grants Pending'
  AND object_name LIKE '%:Memory Manager%';

SELECT @sample1_dmv_waiters = COUNT(*)
FROM sys.dm_exec_query_memory_grants
WHERE grant_time IS NULL;

DECLARE @sample1_pending int;
SET @sample1_pending = CASE
    WHEN @sample1_perf_pending >= @sample1_dmv_waiters THEN @sample1_perf_pending
    ELSE @sample1_dmv_waiters
END;

-- Wait interval between samples
DECLARE @delay_str char(8);
SET @delay_str = CONVERT(char(8), DATEADD(second, @sample_delay_seconds, '00:00:00'), 108);
WAITFOR DELAY @delay_str;

-- Sample 2: read again, along with oldest wait duration and top resource semaphore
DECLARE @sample2_perf_pending int;
DECLARE @sample2_dmv_waiters int;
DECLARE @oldest_wait_ms bigint;
DECLARE @top_resource_semaphore int;
SET @sample2_perf_pending = 0;
SET @sample2_dmv_waiters = 0;
SET @oldest_wait_ms = 0;
SET @top_resource_semaphore = 0;

SELECT @sample2_perf_pending = ISNULL(cntr_value, 0)
FROM sys.dm_os_performance_counters
WHERE counter_name = 'Memory Grants Pending'
  AND object_name LIKE '%:Memory Manager%';

SELECT
    @sample2_dmv_waiters = COUNT(*),
    @oldest_wait_ms = ISNULL(MAX(CASE
        WHEN wait_time_ms IS NOT NULL THEN wait_time_ms
        ELSE DATEDIFF(millisecond, request_time, GETDATE())
    END), 0)
FROM sys.dm_exec_query_memory_grants
WHERE grant_time IS NULL;

SELECT TOP (1)
    @top_resource_semaphore = resource_semaphore_id
FROM sys.dm_exec_query_memory_grants
WHERE grant_time IS NULL
GROUP BY resource_semaphore_id
ORDER BY COUNT(*) DESC;

DECLARE @sample2_pending int;
SET @sample2_pending = CASE
    WHEN @sample2_perf_pending >= @sample2_dmv_waiters THEN @sample2_perf_pending
    ELSE @sample2_dmv_waiters
END;

-- Current waiting tasks on RESOURCE_SEMAPHORE (not cumulative sys.dm_os_wait_stats)
DECLARE @resource_semaphore_tasks int;
DECLARE @resource_semaphore_max_wait_ms bigint;
SET @resource_semaphore_tasks = 0;
SET @resource_semaphore_max_wait_ms = 0;

SELECT
    @resource_semaphore_tasks = COUNT(*),
    @resource_semaphore_max_wait_ms = ISNULL(MAX(wait_duration_ms), 0)
FROM sys.dm_os_waiting_tasks
WHERE wait_type LIKE 'RESOURCE_SEMAPHORE%';

SELECT
    CAST('memory_grants_pending' AS varchar(256)) AS metric_item,
    CAST(@sample2_pending AS varchar(32)) AS metric_value,
    CAST('grants' AS varchar(32)) AS metric_unit,
    CASE
        WHEN @sample2_pending > @pending_grants_critical THEN 'CRITICAL'
        WHEN @sample1_pending > @pending_grants_warning AND @sample2_pending > @pending_grants_warning THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'pending_grants=' + CAST(@sample2_pending AS varchar(16))
    + ', sample1_pending=' + CAST(@sample1_pending AS varchar(16))
    + ', sample2_pending=' + CAST(@sample2_pending AS varchar(16))
    + ', sample_delay_sec=' + CAST(@sample_delay_seconds AS varchar(16))
    + ', memory_grant_waiters=' + CAST(@sample2_dmv_waiters AS varchar(16))
    + ', oldest_wait_ms=' + CAST(@oldest_wait_ms AS varchar(32))
    + ', top_resource_semaphore=' + CAST(@top_resource_semaphore AS varchar(16))
    + ', resource_semaphore_tasks=' + CAST(@resource_semaphore_tasks AS varchar(16))
    + ', resource_semaphore_max_wait_ms=' + CAST(@resource_semaphore_max_wait_ms AS varchar(32)) AS message;
