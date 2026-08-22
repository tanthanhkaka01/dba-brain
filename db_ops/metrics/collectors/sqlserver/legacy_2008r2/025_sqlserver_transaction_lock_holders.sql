DECLARE @warning_tran_seconds  int = 300;    -- 5 min with a transaction open: worth a look
DECLARE @critical_tran_seconds int = 900;   -- 15 min, and blocking or asleep: an incident

-- sys.dm_tran_locks is read into a temp table first because the query below reads it twice:
-- once for the per-session aggregate, and once more INSIDE the message column - a correlated
-- subquery that runs per output row. On 192.0.2.115 (1127 sessions, one holding 22,137
-- locks) that meant materialising the whole lock manager 62 times for 61 rows of output, and
-- the metric took 103 seconds against a 60-second timeout: it had never once succeeded there,
-- on the one server whose locking actually needed watching.
--
-- Reading it once is also more correct. The aggregate and the per-type breakdown used to be
-- taken at two different instants, so a session's lock_count and its lock_by_type could
-- disagree in the same message.
IF OBJECT_ID('tempdb..#locks') IS NOT NULL
    DROP TABLE #locks;

SELECT
    tl.request_session_id,
    tl.resource_database_id,
    tl.resource_type,
    tl.request_mode
INTO #locks
FROM sys.dm_tran_locks tl
WHERE tl.request_status = 'GRANT'
  AND tl.request_mode IN ('X','IX','IU','U','RangeX-X','Sch-M')
  -- Never report the collector's own session. Snapshotting the lock manager into #locks is
  -- itself a temp-table create, which takes X locks on tempdb OBJECT, so this metric started
  -- alerting on itself the hour that optimisation shipped: "SPID=79, 24 lock(s), program=
  -- python3.12, open_tran=0". A monitoring query is not a finding about the server.
  AND tl.request_session_id <> @@SPID;

;WITH TranAge AS
(
    SELECT
        st.session_id,
        -- sys.dm_exec_sessions.open_transaction_count does NOT exist on SQL Server 2008 R2
        -- (added in 2012), so count active transactions from the tran DMVs instead.
        COUNT(*) AS open_tran_count,
        MIN(at.transaction_begin_time) AS oldest_tran_begin_time,
        CASE
            WHEN DATEDIFF(SECOND, MIN(at.transaction_begin_time), GETDATE()) < 0 THEN 0
            ELSE DATEDIFF(SECOND, MIN(at.transaction_begin_time), GETDATE())
        END AS open_tran_seconds
    FROM sys.dm_tran_session_transactions st
    JOIN sys.dm_tran_active_transactions at
        ON at.transaction_id = st.transaction_id
    GROUP BY st.session_id
),
LockAgg AS
(
    SELECT
        tl.request_session_id AS session_id,
        COUNT(*) AS lock_count,
        SUM(CASE WHEN tl.request_mode IN ('X','IX','IU','U','RangeX-X','Sch-M') THEN 1 ELSE 0 END) AS strong_lock_count,
        SUM(CASE WHEN tl.resource_database_id = 2 THEN 1 ELSE 0 END) AS tempdb_lock_count,
        MIN(tl.resource_database_id) AS min_database_id,
        MAX(tl.resource_database_id) AS max_database_id
    FROM #locks tl
    GROUP BY tl.request_session_id
)
SELECT
    CAST('SPID=' + CAST(s.session_id AS varchar(20)) AS varchar(256)) AS metric_item,
    CAST(la.lock_count AS varchar(32)) AS metric_value,
    CAST('lock(s)' AS varchar(32)) AS metric_unit,

    -- Severity is decided by AGE and BLOCKING, not by the mere existence of a strong lock.
    -- Every writing session holds one for as long as its statement runs, so "strong lock => at
    -- least WARNING" made routine work an alert: on 192.0.2.115 the newest sample was 64 rows,
    -- 52 of them CRITICAL, and 33 had lock_seconds=0. An alert stream that flags normal work is
    -- one nobody reads on the day it flags an incident.
    --
    -- A held lock is telemetry (LOGGING). It becomes a finding when the transaction behind it is
    -- OLD, and an incident when it is old AND somebody is waiting on it, or it is sleeping - a
    -- session that opened a transaction and walked away is the classic cause of a stuck log.
    CASE
        WHEN ISNULL(ta.open_tran_seconds, 0) >= @critical_tran_seconds
             AND (ISNULL(r.blocking_session_id, 0) <> 0
                  OR EXISTS (SELECT 1 FROM sys.dm_exec_requests br
                             WHERE br.blocking_session_id = s.session_id))
        THEN 'CRITICAL'
        WHEN ISNULL(ta.open_tran_seconds, 0) >= @critical_tran_seconds
             AND ISNULL(s.status, '') = 'sleeping'
        THEN 'CRITICAL'
        WHEN ISNULL(ta.open_tran_seconds, 0) >= @warning_tran_seconds THEN 'WARNING'
        ELSE 'LOGGING'
    END AS status,

    ISNULL(ta.open_tran_seconds, 0) AS lock_seconds,

    CAST(
        'Session holding locks. '
        + 'spid=' + CAST(s.session_id AS varchar(20))
        + ', login=' + ISNULL(s.login_name, '')
        + ', host=' + ISNULL(s.host_name, '')
        + ', program=' + ISNULL(s.program_name, '')
        + ', session_status=' + ISNULL(s.status, '')
        + ', request_status=' + ISNULL(r.status, '')
        + ', command=' + ISNULL(r.command, '')
        + ', wait_type=' + ISNULL(r.wait_type, '')
        + ', wait_ms=' + ISNULL(CAST(r.wait_time AS varchar(20)), '')
        + ', blocking_session_id=' + ISNULL(CAST(r.blocking_session_id AS varchar(20)), '')
        + ', open_tran=' + CAST(ISNULL(ta.open_tran_count, 0) AS varchar(20))
        + ', lock_seconds=' + CAST(ISNULL(ta.open_tran_seconds, 0) AS varchar(20))
        + ', oldest_tran_begin_time=' + ISNULL(CONVERT(varchar(19), ta.oldest_tran_begin_time, 120), '')
        + ', last_request_start=' + ISNULL(CONVERT(varchar(19), s.last_request_start_time, 120), '')
        + ', last_request_end=' + ISNULL(CONVERT(varchar(19), s.last_request_end_time, 120), '')
        + ', connect_time=' + ISNULL(CONVERT(varchar(19), c.connect_time, 120), '')
        + ', last_read=' + ISNULL(CONVERT(varchar(19), c.last_read, 120), '')
        + ', last_write=' + ISNULL(CONVERT(varchar(19), c.last_write, 120), '')
        + ', lock_count=' + CAST(la.lock_count AS varchar(20))
        + ', strong_lock_count=' + CAST(la.strong_lock_count AS varchar(20))
        + ', tempdb_lock_count=' + CAST(la.tempdb_lock_count AS varchar(20))
        + ', min_database=' + ISNULL(DB_NAME(la.min_database_id), 'dbid=' + CAST(la.min_database_id AS varchar(20)))
        + ', max_database=' + ISNULL(DB_NAME(la.max_database_id), 'dbid=' + CAST(la.max_database_id AS varchar(20)))

        + ', lock_by_type='
        + ISNULL(STUFF((
            SELECT
                '; '
                + ISNULL(DB_NAME(x.resource_database_id), 'dbid=' + CAST(x.resource_database_id AS varchar(20)))
                + '.' + x.resource_type
                + '.' + x.request_mode
                + '=' + CAST(COUNT(*) AS varchar(20))
            FROM #locks x
            WHERE x.request_session_id = s.session_id
            GROUP BY
                x.resource_database_id,
                x.resource_type,
                x.request_mode
            FOR XML PATH(''), TYPE
        ).value('.', 'nvarchar(max)'), 1, 2, ''), '')

        + ', last_or_running_sql='
        + LEFT(
            REPLACE(REPLACE(REPLACE(ISNULL(t.text, ''), CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' '),
            1000
          )
    AS varchar(max)) AS message

FROM LockAgg la
JOIN sys.dm_exec_sessions s
    ON s.session_id = la.session_id
LEFT JOIN TranAge ta
    ON ta.session_id = s.session_id
LEFT JOIN sys.dm_exec_connections c
    ON c.session_id = s.session_id
LEFT JOIN sys.dm_exec_requests r
    ON r.session_id = s.session_id
OUTER APPLY sys.dm_exec_sql_text(ISNULL(r.sql_handle, c.most_recent_sql_handle)) t
WHERE s.is_user_process = 1
ORDER BY
    ISNULL(ta.open_tran_seconds, 0) DESC,
    ISNULL(ta.open_tran_count, 0) DESC,
    la.lock_count DESC;