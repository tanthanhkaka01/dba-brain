WITH waits AS
(
    SELECT TOP (1)
        wait_type,
        wait_time_ms,
        waiting_tasks_count
    FROM sys.dm_os_wait_stats
    WHERE wait_type NOT LIKE 'SLEEP%'
      AND wait_type NOT IN
      (
        'BROKER_EVENTHANDLER', 'BROKER_RECEIVE_WAITFOR', 'BROKER_TASK_STOP',
        'BROKER_TO_FLUSH', 'BROKER_TRANSMITTER', 'CHECKPOINT_QUEUE',
        'CHKPT', 'CLR_AUTO_EVENT', 'CLR_MANUAL_EVENT', 'LAZYWRITER_SLEEP',
        'LOGMGR_QUEUE', 'REQUEST_FOR_DEADLOCK_SEARCH', 'SQLTRACE_BUFFER_FLUSH',
        'XE_DISPATCHER_WAIT', 'XE_TIMER_EVENT'
      )
    ORDER BY wait_time_ms DESC
)
SELECT
    CAST(wait_type AS varchar(256)) AS metric_item,
    CAST(CAST(wait_time_ms / 1000.0 AS decimal(19,2)) AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    'OK' AS status,
    'top_wait=' + wait_type
        + ', wait_seconds=' + CAST(CAST(wait_time_ms / 1000.0 AS decimal(19,2)) AS varchar(32))
        + ', waiting_tasks_count=' + CAST(waiting_tasks_count AS varchar(32))
        + ', note=cumulative_since_sqlserver_start' AS message
FROM waits;
