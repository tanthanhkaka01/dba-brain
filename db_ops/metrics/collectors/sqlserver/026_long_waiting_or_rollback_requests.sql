-- QUERY_LONG_WAITING_OR_ROLLBACK_REQUESTS: per database, the requests that are rolling back,
-- blocked, waiting a long time, or running long with no wait at all.
--
-- The counts are an aggregate over a database, so on their own they name no session: the alert
-- said "SALESDB: 1 long_waiting_or_rollback_requests, max_elapsed_seconds=1315" and gave the DBA
-- nothing to look at. Whoever reads this has to open the instance and find the request again by
-- hand, and by then it may be gone. So the row that DECIDED the status is carried alongside the
-- counts as `top_*`, the same way QUERY_LONG_RUNNING carries its own worst request.
--
-- The ranking order below mirrors the status CASE deliberately. If it did not, the message could
-- name a merely-slow request while the CRITICAL was raised by a rollback on another session —
-- an alert pointing at the wrong spid is worse than one pointing at none.
--
-- Written to run on SQL Server 2008 R2 as well (the legacy_2008r2 variant is kept identical):
-- string building uses `+` and not CONCAT, and every optional value is wrapped in ISNULL,
-- because with `+` a single NULL silently collapses the whole message to NULL.
;WITH flagged AS
(
    SELECT
        r.session_id,
        ISNULL(DB_NAME(r.database_id), 'UNKNOWN') COLLATE DATABASE_DEFAULT AS database_name,
        r.status AS request_status,
        r.command,
        r.wait_type,
        r.wait_time,
        r.blocking_session_id,
        r.total_elapsed_time,
        -- "is genuinely waiting on something worth reporting", computed once instead of repeating
        -- the exclusion list in four aggregates. The IS NOT NULL half is not decoration: the
        -- original wrote `wait_type NOT IN (list)` inside the CASEs, which for a NULL wait_type
        -- evaluates to UNKNOWN and so falls to ELSE — a request with no wait at all must not be
        -- counted as a suspended waiter, and dropping the check would silently start counting it.
        CASE
            WHEN r.wait_type IS NOT NULL
             AND r.wait_type NOT IN (
                'SP_SERVER_DIAGNOSTICS_SLEEP',
                'WAITFOR',
                'BROKER_RECEIVE_WAITFOR',
                'BROKER_TASK_STOP',
                'BROKER_TO_FLUSH',
                'BROKER_EVENTHANDLER',
                'SQLTRACE_BUFFER_FLUSH',
                'XE_TIMER_EVENT',
                'XE_DISPATCHER_WAIT',
                'REQUEST_FOR_DEADLOCK_SEARCH',
                'LAZYWRITER_SLEEP',
                'LOGMGR_QUEUE',
                'CHECKPOINT_QUEUE',
                'FT_IFTS_SCHEDULER_IDLE_WAIT'
             )
            THEN 1 ELSE 0
        END AS real_wait,
        s.login_name,
        s.host_name,
        s.program_name
    FROM sys.dm_exec_requests r
    JOIN sys.dm_exec_sessions s
        ON r.session_id = s.session_id
    WHERE
        s.is_user_process = 1
        AND r.session_id <> @@SPID

        -- exclude known harmless/system-like long waits
        AND ISNULL(r.wait_type, '') NOT IN (
            'SP_SERVER_DIAGNOSTICS_SLEEP',
            'WAITFOR',
            'BROKER_RECEIVE_WAITFOR',
            'BROKER_TASK_STOP',
            'BROKER_TO_FLUSH',
            'BROKER_EVENTHANDLER',
            'SQLTRACE_BUFFER_FLUSH',
            'XE_TIMER_EVENT',
            'XE_DISPATCHER_WAIT',
            'REQUEST_FOR_DEADLOCK_SEARCH',
            'LAZYWRITER_SLEEP',
            'LOGMGR_QUEUE',
            'CHECKPOINT_QUEUE',
            'FT_IFTS_SCHEDULER_IDLE_WAIT'
        )

        AND (
            r.command LIKE '%ROLLBACK%'
            OR r.blocking_session_id <> 0
            OR r.wait_time >= 1000 * 60 * 5
            OR (
                r.total_elapsed_time >= 1000 * 60 * 10
                AND r.wait_type IS NULL
            )
        )
),
ranked AS
(
    SELECT
        *,
        ROW_NUMBER() OVER
        (
            PARTITION BY database_name
            -- Same precedence as the status CASE: a rollback outranks a blocked request, which
            -- outranks the longest waiter, which outranks the longest runner. The session named
            -- in the message is then always the one the severity was raised for.
            ORDER BY
                CASE
                    WHEN command LIKE '%ROLLBACK%' THEN 0
                    WHEN blocking_session_id <> 0 THEN 1
                    ELSE 2
                END,
                wait_time DESC,
                total_elapsed_time DESC,
                session_id
        ) AS rn
    FROM flagged
)
SELECT
    CAST(database_name AS varchar(256)) AS metric_item,
    CAST(COUNT(*) AS varchar(32)) AS metric_value,
    CAST('long_waiting_or_rollback_requests' AS varchar(64)) AS metric_unit,

    CASE
        WHEN SUM(CASE WHEN command LIKE '%ROLLBACK%' THEN 1 ELSE 0 END) > 0
            THEN 'CRITICAL'

        WHEN SUM(CASE WHEN blocking_session_id <> 0
                       AND wait_time >= 1000 * 60 * 5
                      THEN 1 ELSE 0 END) > 0
            THEN 'CRITICAL'

        WHEN MAX(CASE WHEN real_wait = 1 THEN wait_time ELSE 0 END) >= 1000 * 60 * 15
            THEN 'CRITICAL'

        WHEN COUNT(*) >= 5
             AND SUM(CASE WHEN request_status = 'suspended'
                            AND wait_time >= 1000 * 60 * 5
                            AND real_wait = 1
                          THEN 1 ELSE 0 END) >= 3
            THEN 'CRITICAL'

        WHEN MAX(CASE
                    WHEN request_status <> 'sleeping'
                     AND wait_type IS NULL
                    THEN total_elapsed_time
                    ELSE 0
                 END) >= 1000 * 60 * 30
            THEN 'CRITICAL'

        WHEN SUM(CASE WHEN request_status = 'suspended'
                       AND wait_time >= 1000 * 60 * 5
                       AND real_wait = 1
                      THEN 1 ELSE 0 END) > 0
            THEN 'WARNING'

        WHEN MAX(CASE
                    WHEN request_status <> 'sleeping'
                     AND wait_type IS NULL
                    THEN total_elapsed_time
                    ELSE 0
                 END) >= 1000 * 60 * 10
            THEN 'WARNING'

        ELSE 'OK'
    END AS status,

    CAST(
        'db=' + database_name +
        ', total_requests=' + CAST(COUNT(*) AS varchar(20)) +
        ', long_running_count=' +
            CAST(SUM(CASE
                        WHEN total_elapsed_time >= 1000 * 60 * 10
                         AND wait_type IS NULL
                     THEN 1 ELSE 0 END) AS varchar(20)) +
        ', suspended_waiting_count=' +
            CAST(SUM(CASE
                        WHEN request_status = 'suspended'
                         AND wait_time >= 1000 * 60 * 5
                         AND real_wait = 1
                     THEN 1 ELSE 0 END) AS varchar(20)) +
        ', blocked_waiting_count=' +
            CAST(SUM(CASE WHEN blocking_session_id <> 0 THEN 1 ELSE 0 END) AS varchar(20)) +
        ', rollback_count=' +
            CAST(SUM(CASE WHEN command LIKE '%ROLLBACK%' THEN 1 ELSE 0 END) AS varchar(20)) +
        ', max_elapsed_seconds=' + CAST(MAX(total_elapsed_time) / 1000 AS varchar(20)) +
        ', max_wait_seconds=' + CAST(MAX(wait_time) / 1000 AS varchar(20)) +
        -- The request the counts are about. `top_session_id` is the spid to look at first; it is
        -- named `top_*` rather than `session_id` so it cannot be mistaken for a per-row identity
        -- on what is still a per-database aggregate.
        ', top_session_id=' +
            ISNULL(CAST(MAX(CASE WHEN rn = 1 THEN session_id END) AS varchar(20)), '') +
        ', top_status=' +
            ISNULL(MAX(CASE WHEN rn = 1 THEN request_status END), '') +
        ', top_command=' +
            ISNULL(MAX(CASE WHEN rn = 1 THEN command END), '') +
        ', top_wait_type=' +
            ISNULL(MAX(CASE WHEN rn = 1 THEN wait_type END), '') +
        ', top_blocking_session_id=' +
            ISNULL(CAST(MAX(CASE WHEN rn = 1 THEN blocking_session_id END) AS varchar(20)), '') +
        ', top_wait_seconds=' +
            ISNULL(CAST(MAX(CASE WHEN rn = 1 THEN wait_time END) / 1000 AS varchar(20)), '') +
        ', top_elapsed_seconds=' +
            ISNULL(CAST(MAX(CASE WHEN rn = 1 THEN total_elapsed_time END) / 1000 AS varchar(20)), '') +
        ', login=' + ISNULL(MAX(CASE WHEN rn = 1 THEN login_name END), '') +
        ', host=' + ISNULL(MAX(CASE WHEN rn = 1 THEN host_name END), '') +
        ', app=' + ISNULL(MAX(CASE WHEN rn = 1 THEN program_name END), '')
    AS varchar(max)) AS message
FROM ranked
GROUP BY database_name;

-- -- check detail
-- SELECT
--     GETDATE() AS at_time,
--     r.session_id,
--     r.blocking_session_id,
--     r.status,
--     r.command,
--     DB_NAME(r.database_id) AS DBName,
--     r.wait_type,
--     r.wait_time / 1000 AS wait_seconds,
--     r.cpu_time,
--     r.total_elapsed_time / 1000 AS elapsed_seconds,
--     r.percent_complete,
--     r.estimated_completion_time / 1000 AS estimated_remaining_seconds,
--     s.login_name,
--     c.client_net_address AS IP,
--     s.host_name,
--     s.program_name,
--     t.text
-- FROM sys.dm_exec_requests r
-- JOIN sys.dm_exec_sessions s
--     ON r.session_id = s.session_id
-- LEFT JOIN sys.dm_exec_connections c
--     ON r.session_id = c.session_id
-- OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
-- WHERE s.is_user_process = 1
--   AND (
--         r.blocking_session_id <> 0
--         OR r.status IN ('suspended', 'rollback')
--         OR r.command LIKE '%ROLLBACK%'
--         OR r.wait_time >= 300000
--         OR r.total_elapsed_time >= 300000
--       )
-- ORDER BY
--     CASE WHEN r.status = 'rollback' THEN 0 ELSE 1 END,
--     r.total_elapsed_time DESC;
