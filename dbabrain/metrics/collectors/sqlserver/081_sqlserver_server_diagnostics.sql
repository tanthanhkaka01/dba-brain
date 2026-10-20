-- SERVER_DIAGNOSTICS (SQL Server 2012+): memory dumps and sp_server_diagnostics component health.
--
-- Why memory dumps are graded by age:
-- Memory dumps are SQL Server's internal incident records. An access violation (AV), an assertion
-- failure, a non-yielding scheduler, or an unresolvable latch timeout triggers an engine minidump
-- and writes a .mdmp file to the log directory. The instance frequently recovers and carries on as
-- if nothing happened, while internal state may have become corrupt or unstable.
--
-- Following the pattern established for the SQL error log (017_sqlserver_errorlog_recent_critical.sql),
-- recent dumps created within @dump_recent_minutes (default 120 minutes / 2 hours) page as CRITICAL.
-- Dumps that are older but still within the retention window @dump_window_days (default 7 days) alert
-- as WARNING so they remain visible on daily inspection dashboards without waking on-call staff for
-- a week. Dumps older than 7 days are not reported per-item. When no dumps exist within the window,
-- a single OK row 'memory_dumps' reports the total count of dumps retained in the DMV and the
-- timestamp of the newest dump.
--
-- Why sp_server_diagnostics is executed with @repeat_interval = 0:
-- sp_server_diagnostics is the internal health detection procedure introduced in SQL Server 2012,
-- used by Failover Cluster Instances (FCI) and Always On Availability Groups to detect unrecoverable
-- engine health failure (failure condition level 1 to 5).
-- With @repeat_interval = 0, the procedure returns exactly one diagnostic result set across all
-- components and terminates immediately, avoiding the infinite background loop used by cluster DLLs.
--
-- Component health grading:
-- - clean: OK.
-- - warning: WARNING.
-- - error: CRITICAL.
-- - unknown: LOGGING.
--
-- Relevant XML attributes from the 'data' column are extracted for system, resource,
-- query_processing, and io_subsystem components to provide immediate diagnostic context for
-- warnings and errors. The 'events' component contains a large XML payload of Extended Events
-- which is omitted to keep the message concise (state only).
--
-- Fault isolation:
-- sp_server_diagnostics requires VIEW SERVER STATE permission. Its execution is wrapped in a
-- TRY/CATCH block so that a permission failure or execution error reports one LOGGING row
-- 'sp_server_diagnostics' and never drops the memory dump rows already collected.
SET NOCOUNT ON;

DECLARE @dump_recent_minutes int = 120;
DECLARE @dump_window_days int = 7;

DECLARE @results TABLE (
    metric_item varchar(256) NOT NULL,
    metric_value varchar(32) NULL,
    metric_unit varchar(32) NULL,
    status varchar(16) NOT NULL,
    message varchar(1000) NOT NULL,
    severity_rank int NOT NULL
);

-- ---------------------------------------------------------------------------
-- 1. Memory Dumps (sys.dm_server_memory_dumps)
-- ---------------------------------------------------------------------------
;WITH parsed AS
(
    SELECT
        filename,
        CASE
            WHEN CHARINDEX('\', REVERSE(filename)) > 0
                THEN RIGHT(filename, CHARINDEX('\', REVERSE(filename)) - 1)
            WHEN CHARINDEX('/', REVERSE(filename)) > 0
                THEN RIGHT(filename, CHARINDEX('/', REVERSE(filename)) - 1)
            ELSE filename
        END AS file_name_only,
        creation_time,
        size_in_bytes,
        CAST(ROUND(size_in_bytes / 1048576.0, 2) AS decimal(19,2)) AS size_mb,
        CASE
            WHEN DATEDIFF(minute, creation_time, SYSDATETIMEOFFSET()) < 0 THEN 0
            ELSE DATEDIFF(minute, creation_time, SYSDATETIMEOFFSET())
        END AS age_minutes
    FROM sys.dm_server_memory_dumps
),
in_window AS
(
    SELECT
        file_name_only,
        creation_time,
        size_mb,
        age_minutes,
        CASE
            WHEN age_minutes <= @dump_recent_minutes THEN 'CRITICAL'
            ELSE 'WARNING'
        END AS dump_status,
        CASE
            WHEN age_minutes <= @dump_recent_minutes THEN 1
            ELSE 2
        END AS rank_num
    FROM parsed
    WHERE creation_time >= DATEADD(day, -@dump_window_days, SYSDATETIMEOFFSET())
)
INSERT INTO @results (metric_item, metric_value, metric_unit, status, message, severity_rank)
SELECT
    CAST('memory_dump :: ' + file_name_only AS varchar(256)) AS metric_item,
    CAST(size_mb AS varchar(32)) AS metric_value,
    CAST('MB' AS varchar(32)) AS metric_unit,
    CAST(dump_status AS varchar(16)) AS status,
    CAST(
        'file=' + file_name_only
        + ', created=' + CONVERT(varchar(19), creation_time, 120)
        + ', age_minutes=' + CAST(age_minutes AS varchar(32))
        + ', size_mb=' + CAST(size_mb AS varchar(32))
        AS varchar(1000)
    ) AS message,
    rank_num AS severity_rank
FROM in_window

UNION ALL

SELECT
    CAST('memory_dumps' AS varchar(256)) AS metric_item,
    CAST(COUNT(*) AS varchar(32)) AS metric_value,
    CAST('count' AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST(
        'dumps_total=' + CAST(COUNT(*) AS varchar(32))
        + ', newest_dump=' + ISNULL(CONVERT(varchar(19), MAX(creation_time), 120), 'none')
        + ', dump_window_days=' + CAST(@dump_window_days AS varchar(32))
        + ', note=no memory dumps within window'
        AS varchar(1000)
    ) AS message,
    3 AS severity_rank
FROM sys.dm_server_memory_dumps
-- HAVING, not WHERE: an aggregate with no GROUP BY returns its one row even when WHERE
-- removed every input, so this said "0 dumps, none" beside the dumps listed above.
HAVING NOT EXISTS (
    SELECT 1
    FROM sys.dm_server_memory_dumps
    WHERE creation_time >= DATEADD(day, -@dump_window_days, SYSDATETIMEOFFSET())
);

-- ---------------------------------------------------------------------------
-- 2. sp_server_diagnostics Component Health
-- ---------------------------------------------------------------------------
CREATE TABLE #sp_diag (
    create_time datetime,
    component_type sysname,
    component_name sysname,
    state int,
    state_desc varchar(32),
    data xml
);

BEGIN TRY
    INSERT INTO #sp_diag (create_time, component_type, component_name, state, state_desc, data)
    EXEC sys.sp_server_diagnostics @repeat_interval = 0;

    INSERT INTO @results (metric_item, metric_value, metric_unit, status, message, severity_rank)
    SELECT
        CAST('component :: ' + d.component_name AS varchar(256)) AS metric_item,
        CAST(LOWER(d.state_desc) AS varchar(32)) AS metric_value,
        CAST(NULL AS varchar(32)) AS metric_unit,
        CAST(
            CASE
                WHEN LOWER(d.state_desc) = 'clean' THEN 'OK'
                WHEN LOWER(d.state_desc) = 'warning' THEN 'WARNING'
                WHEN LOWER(d.state_desc) = 'error' THEN 'CRITICAL'
                ELSE 'LOGGING'
            END AS varchar(16)
        ) AS status,
        CAST(
            'component=' + d.component_name
            + ', state=' + LOWER(d.state_desc)
            + ', create_time=' + CONVERT(varchar(19), d.create_time, 120)
            + CASE
                WHEN d.component_name = 'system' THEN
                    ', spinlock_backoffs=' + ISNULL(CAST(d.data.value('(/*/@spinlockBackoffs)[1]', 'bigint') AS varchar(32)), '0')
                    + ', sick_spinlock_type=' + ISNULL(NULLIF(d.data.value('(/*/@sickSpinlockType)[1]', 'varchar(64)'), ''), 'none')
                    + ', latch_warnings=' + ISNULL(CAST(d.data.value('(/*/@latchWarnings)[1]', 'bigint') AS varchar(32)), '0')
                    + ', is_access_violation=' + ISNULL(CAST(d.data.value('(/*/@isAccessViolationOccurred)[1]', 'int') AS varchar(8)), '0')
                    + ', non_yielding_tasks=' + ISNULL(CAST(d.data.value('(/*/@nonYieldingTasksReported)[1]', 'bigint') AS varchar(32)), '0')
                    + ', page_faults=' + ISNULL(CAST(d.data.value('(/*/@pageFaults)[1]', 'bigint') AS varchar(32)), '0')
                WHEN d.component_name = 'resource' THEN
                    ', oom_exceptions=' + ISNULL(CAST(d.data.value('(/*/@outOfMemoryExceptions)[1]', 'bigint') AS varchar(32)), '0')
                    + ', any_pool_oom=' + ISNULL(CAST(d.data.value('(/*/@isAnyPoolOutOfMemory)[1]', 'int') AS varchar(8)), '0')
                    + ', process_oom_period=' + ISNULL(CAST(d.data.value('(/*/@processOutOfMemoryPeriod)[1]', 'bigint') AS varchar(32)), '0')
                WHEN d.component_name = 'query_processing' THEN
                    ', max_workers=' + ISNULL(CAST(d.data.value('(/*/@maxWorkers)[1]', 'int') AS varchar(32)), '0')
                    + ', workers_created=' + ISNULL(CAST(d.data.value('(/*/@workersCreated)[1]', 'int') AS varchar(32)), '0')
                    + ', workers_idle=' + ISNULL(CAST(d.data.value('(/*/@workersIdle)[1]', 'int') AS varchar(32)), '0')
                    + ', tasks_completed=' + ISNULL(CAST(d.data.value('(/*/@tasksCompletedWithinInterval)[1]', 'bigint') AS varchar(32)), '0')
                    + ', pending_tasks=' + ISNULL(CAST(d.data.value('(/*/@pendingTasks)[1]', 'int') AS varchar(32)), '0')
                    + ', unresolvable_deadlock=' + ISNULL(CAST(d.data.value('(/*/@hasUnresolvableDeadlockOccurred)[1]', 'int') AS varchar(8)), '0')
                    + ', deadlocked_schedulers=' + ISNULL(CAST(d.data.value('(/*/@hasDeadlockedSchedulersOccurred)[1]', 'int') AS varchar(8)), '0')
                WHEN d.component_name = 'io_subsystem' THEN
                    ', io_latch_timeouts=' + ISNULL(CAST(d.data.value('(/*/@ioLatchTimeouts)[1]', 'bigint') AS varchar(32)), '0')
                    + ', interval_long_ios=' + ISNULL(CAST(d.data.value('(/*/@intervalLongIos)[1]', 'bigint') AS varchar(32)), '0')
                    + ', total_long_ios=' + ISNULL(CAST(d.data.value('(/*/@totalLongIos)[1]', 'bigint') AS varchar(32)), '0')
                ELSE ''
            END AS varchar(1000)
        ) AS message,
        CASE
            WHEN LOWER(d.state_desc) = 'error' THEN 1
            WHEN LOWER(d.state_desc) = 'warning' THEN 2
            WHEN LOWER(d.state_desc) = 'clean' THEN 3
            ELSE 4
        END AS severity_rank
    FROM #sp_diag AS d;
END TRY
BEGIN CATCH
    INSERT INTO @results (metric_item, metric_value, metric_unit, status, message, severity_rank)
    VALUES (
        'sp_server_diagnostics',
        'ERROR',
        NULL,
        'LOGGING',
        'error_number=' + CAST(ERROR_NUMBER() AS varchar(32))
            + ', error_message=' + REPLACE(REPLACE(ERROR_MESSAGE(), CHAR(13), ' '), CHAR(10), ' ')
            + ', note=sp_server_diagnostics failed, memory dumps preserved',
        4
    );
END CATCH;

SELECT
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
FROM @results
ORDER BY
    severity_rank,
    metric_item;
