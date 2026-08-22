WITH waits AS
(
    SELECT
        wait_type,
        waiting_tasks_count,
        wait_time_ms,
        signal_wait_time_ms,
        wait_time_ms - signal_wait_time_ms AS resource_wait_time_ms
    FROM sys.dm_os_wait_stats
    WHERE wait_type NOT IN
    (
        'BROKER_EVENTHANDLER', 'BROKER_RECEIVE_WAITFOR', 'BROKER_TASK_STOP',
        'BROKER_TO_FLUSH', 'BROKER_TRANSMITTER', 'CHECKPOINT_QUEUE',
        'CHKPT', 'CLR_AUTO_EVENT', 'CLR_MANUAL_EVENT',
        'DISPATCHER_QUEUE_SEMAPHORE', 'FT_IFTS_SCHEDULER_IDLE_WAIT',
        'HADR_FILESTREAM_IOMGR_IOCOMPLETION', 'LAZYWRITER_SLEEP',
        'LOGMGR_QUEUE', 'ONDEMAND_TASK_QUEUE', 'REQUEST_FOR_DEADLOCK_SEARCH',
        'RESOURCE_QUEUE', 'SERVER_IDLE_CHECK', 'SLEEP_BPOOL_FLUSH',
        'SLEEP_DBSTARTUP', 'SLEEP_DCOMSTARTUP', 'SLEEP_MASTERDBREADY',
        'SLEEP_MASTERMDREADY', 'SLEEP_MASTERUPGRADED', 'SLEEP_MSDBSTARTUP',
        'SLEEP_SYSTEMTASK', 'SLEEP_TASK', 'SLEEP_TEMPDBSTARTUP',
        'SP_SERVER_DIAGNOSTICS_SLEEP', 'SQLTRACE_BUFFER_FLUSH',
        'SQLTRACE_INCREMENTAL_FLUSH_SLEEP', 'WAITFOR',
        'XE_DISPATCHER_WAIT', 'XE_TIMER_EVENT'
    )
),
top_wait AS
(
    SELECT TOP (1)
        wait_type,
        waiting_tasks_count,
        wait_time_ms,
        signal_wait_time_ms,
        resource_wait_time_ms,
        CAST(wait_time_ms / 1000.0 AS decimal(19,2)) AS wait_seconds,
        CAST(signal_wait_time_ms * 100.0 / NULLIF(wait_time_ms, 0) AS decimal(10,2)) AS signal_wait_pct
    FROM waits
    WHERE wait_time_ms > 0
    ORDER BY wait_time_ms DESC
)
SELECT
    CAST(wait_type AS varchar(256)) AS metric_item,
    CAST(wait_seconds AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    CASE
        WHEN wait_type IN ('PAGEIOLATCH_SH', 'PAGEIOLATCH_EX', 'WRITELOG', 'RESOURCE_SEMAPHORE')
             AND wait_seconds >= 3600 THEN 'WARNING'
        WHEN signal_wait_pct >= 25 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    CONCAT(
        'top_wait_type=', wait_type,
        ', wait_seconds=', wait_seconds,
        ', waiting_tasks_count=', waiting_tasks_count,
        ', signal_wait_pct=', signal_wait_pct,
        ', note=cumulative_since_sqlserver_start'
    ) AS message
FROM top_wait

UNION ALL

SELECT
    CAST('wait_stats' AS varchar(256)) AS metric_item,
    CAST('0' AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    'OK' AS status,
    'No meaningful wait stats found.' AS message
WHERE NOT EXISTS
(
    SELECT 1 FROM waits WHERE wait_time_ms > 0
);