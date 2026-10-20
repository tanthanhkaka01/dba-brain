DECLARE @warning_tran_seconds  int = 300;    -- 5 min with a transaction open: worth a look
DECLARE @critical_tran_seconds int = 900;   -- 15 min, and blocking or asleep: an incident

-- A session-owned application lock (sp_getapplock @LockOwner = 'Session') is held with no
-- transaction open, so open_tran_seconds is 0 for as long as it is held and the two thresholds
-- above never see it. sys.dm_tran_locks keeps no grant time, so the lock has no age of its own;
-- what is measured is how long the session holding it has been idle. A session that is working
-- releases it; an idle one keeps it until sp_releaseapplock or until somebody closes it. Found
-- when a query tool's session held two such locks for hours and three lock metrics gave only
-- WARNINGs: the waiters gave up at their own @LockTimeout and retried, so no wait ever grew.
DECLARE @applock_idle_warning_seconds int = 300;     -- 5 min idle holding one: worth a look
DECLARE @applock_idle_critical_seconds int = 1800;   -- 30 min idle holding one: an incident
DECLARE @applock_idle_abandoned_seconds int = 60;    -- 1 min idle while somebody waits on it

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
-- ---------------------------------------------------------------------------
-- 0. Is the lock manager too large to enumerate at all?
-- ---------------------------------------------------------------------------
-- Everything below starts by reading sys.dm_tran_locks whole, and enumerating the lock manager
-- takes latches ON the lock manager. On 2026-10-07 one session on an ERP server held 1,137,515
-- locks in a transaction 44 hours old; this metric's scan outlived its 60-second timeout and
-- became the head of 68 waiting sessions, every one of them queued behind a monitoring query.
-- Nothing bounded the scan by the size of what it scanned.
--
-- So the size is read first, from a counter that enumerates nothing, and above the limit the
-- scan is NOT run. That is itself the finding, and it is CRITICAL: a lock manager that large is
-- an incident whoever is waiting, and it is the one moment this metric must not add to it.
--
-- * The counter is 'Lock Owner Blocks' - the locks in use now, which is what a scan would have
--   to walk (measured: 800 on the counter against 798 rows of sys.dm_tran_locks). NOT the size
--   of the lock manager's memory: that store keeps its pages after the locks are gone - the same
--   server read 3,700 MB with 800 locks in use, two days after the incident - so a limit in
--   megabytes would have stayed CRITICAL for good.
-- * The limit is configuration: metric_definitions.json states sql_settings.lock_scan_skip_above
--   and it is written into the DECLARE below, whose number is only the default. 2,000,000 since
--   2026-10-15, the operator's: ABOVE the 1.2 M that stalled a server, so a lock manager of 1.2 to
--   2 M is scanned again; the scan was measured at 260,000 in 1.2 s. It was 500,000 before.
-- * Who holds the locks cannot be counted without the scan, so the holders are named by what
--   can be read without it: the open transactions, largest first by log written, then oldest.
--   That ranking named the 2026-10-07 session. Each row carries the session, its login, host,
--   client address and program. Two are CRITICAL: the largest writer and the oldest open
--   transaction. One of the two is the holder whether it changed a million rows or only read
--   them under an isolation level that keeps its shared locks - a session that wrote nothing
--   has no log to rank by. The rest are WARNING.
-- * No temp table here, unlike below: a server in this state is the last place to create one.
DECLARE @lock_scan_skip_above bigint = 2000000;
-- The counter is refreshed periodically and can hold a past peak: on 2026-10-15 it read 15.7 M
-- for eight minutes after an Agent job that built them had ended, with one transaction open on
-- the server, a second old. Locks belong to transactions, and a transaction builds them at a
-- bounded rate, so a count is credible only if the oldest open user transaction is old enough
-- to have built it at this many locks a second. A rate rather than a fixed age: the same job's
-- real 8.6 M at 08:00 had transactions 50 s old, which a 60-second bound would have called a
-- past peak. 2,000,000 is above the whole server's measured 1.2 M lock requests a second.
DECLARE @lock_count_credible_locks_per_second bigint = 2000000;
DECLARE @locks_in_use bigint;
DECLARE @lock_memory_mb bigint;

SET @locks_in_use = (SELECT SUM(CAST(pc.cntr_value AS bigint))
                     FROM sys.dm_os_performance_counters pc
                     WHERE pc.object_name LIKE '%Memory Manager%'
                       AND pc.counter_name = 'Lock Owner Blocks');
SET @lock_memory_mb = (SELECT SUM(CAST(pc.cntr_value AS bigint)) / 1024
                       FROM sys.dm_os_performance_counters pc
                       WHERE pc.object_name LIKE '%Memory Manager%'
                         AND pc.counter_name = 'Lock Memory (KB)');

IF ISNULL(@locks_in_use, 0) >= @lock_scan_skip_above
BEGIN
    DECLARE @oldest_open_tran_seconds int;
    DECLARE @count_credible int;

    -- A lock count at or above the limit is credible only if an open user transaction is old
    -- enough to have built it. The counter (Lock Owner Blocks) is refreshed periodically and can
    -- hold a past peak for minutes after an Agent job finishes.
    SELECT
        @oldest_open_tran_seconds = ISNULL(MAX(CASE
            WHEN DATEDIFF(SECOND, at.transaction_begin_time, GETDATE()) < 0 THEN 0
            ELSE DATEDIFF(SECOND, at.transaction_begin_time, GETDATE())
        END), 0)
    FROM sys.dm_tran_session_transactions st
    JOIN sys.dm_tran_active_transactions at
        ON at.transaction_id = st.transaction_id
    JOIN sys.dm_exec_sessions s
        ON s.session_id = st.session_id
    WHERE s.is_user_process = 1
      AND st.session_id <> @@SPID;

    SET @count_credible = CASE
        WHEN CAST(@oldest_open_tran_seconds AS bigint) * @lock_count_credible_locks_per_second
             >= @locks_in_use THEN 1
        ELSE 0
    END;

    ;WITH OpenTran AS
    (
        SELECT
            st.session_id,
            MIN(at.transaction_begin_time) AS oldest_tran_begin_time,
            SUM(CAST(ISNULL(dt.database_transaction_log_record_count, 0) AS bigint)) AS log_records,
            SUM(CAST(ISNULL(dt.database_transaction_log_bytes_used, 0) AS bigint)) AS log_bytes
        FROM sys.dm_tran_session_transactions st
        JOIN sys.dm_tran_active_transactions at
            ON at.transaction_id = st.transaction_id
        LEFT JOIN sys.dm_tran_database_transactions dt
            ON dt.transaction_id = st.transaction_id
        WHERE st.session_id <> @@SPID
        GROUP BY st.session_id
    ),
    Holder AS
    (
        SELECT
            o.session_id,
            o.oldest_tran_begin_time,
            o.log_records,
            o.log_bytes,
            CASE
                WHEN DATEDIFF(SECOND, o.oldest_tran_begin_time, GETDATE()) < 0 THEN 0
                ELSE DATEDIFF(SECOND, o.oldest_tran_begin_time, GETDATE())
            END AS open_tran_seconds,
            s.status AS session_status,
            s.login_name,
            s.host_name,
            s.program_name,
            s.last_request_end_time,
            -- host_name is whatever the client says it is; client_net_address is the address SQL
            -- Server saw the connection come from.
            CAST(cn.client_net_address AS varchar(48)) AS client_ip,
            -- Where the transaction wrote most. tempdb for the 2026-10-07 session.
            (SELECT TOP (1) DB_NAME(dt2.database_id)
             FROM sys.dm_tran_session_transactions st2
             JOIN sys.dm_tran_database_transactions dt2
                 ON dt2.transaction_id = st2.transaction_id
             WHERE st2.session_id = o.session_id
             ORDER BY dt2.database_transaction_log_bytes_used DESC) AS database_name,
            CAST(CASE WHEN LEN(f.flat) > 700
                      THEN LEFT(f.flat, 250) + ' ... ' + RIGHT(f.flat, 450)
                      ELSE f.flat END AS varchar(max)) AS last_sql,
            ROW_NUMBER() OVER (ORDER BY o.log_bytes DESC, o.oldest_tran_begin_time ASC,
                                        o.session_id) AS rn,
            ROW_NUMBER() OVER (ORDER BY o.oldest_tran_begin_time ASC, o.session_id) AS rn_age
        FROM OpenTran o
        JOIN sys.dm_exec_sessions s
            ON s.session_id = o.session_id
        -- One connection per session: a MARS session has several rows here, and each would
        -- repeat the session in the ranking.
        OUTER APPLY (SELECT TOP (1) c.client_net_address, c.most_recent_sql_handle
                     FROM sys.dm_exec_connections c
                     WHERE c.session_id = o.session_id
                     ORDER BY c.connect_time) cn
        OUTER APPLY sys.dm_exec_sql_text(cn.most_recent_sql_handle) t
        CROSS APPLY (SELECT flat = REPLACE(REPLACE(REPLACE(ISNULL(t.text, ''),
                                    CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' ')) f
        WHERE s.is_user_process = 1
    )
    SELECT out_rows.metric_item, out_rows.metric_value, out_rows.metric_unit, out_rows.status,
           out_rows.lock_seconds, out_rows.message
    FROM
    (
        SELECT
            0 AS sort_key,
            CAST('lock_scan_skipped' AS varchar(256)) AS metric_item,
            CAST(@locks_in_use AS varchar(32)) AS metric_value,
            CAST('lock(s)' AS varchar(32)) AS metric_unit,
            CAST(CASE WHEN @count_credible = 1 THEN 'CRITICAL' ELSE 'WARNING' END AS varchar(16)) AS status,
            @oldest_open_tran_seconds AS lock_seconds,
            CAST(
                CASE
                    WHEN @count_credible = 1 THEN
                        'Lock scan SKIPPED: the lock manager holds ' + CAST(@locks_in_use AS varchar(32))
                        + ' locks, at or above the limit of ' + CAST(@lock_scan_skip_above AS varchar(32))
                        + '. Enumerating that many latches the lock manager and stalls every session that '
                        + 'takes or releases a lock, so lock counts per session are not known; the holders '
                        + 'named are the open transactions, largest first by log written, and the oldest. '
                    ELSE
                        'Lock scan SKIPPED: the counter reads ' + CAST(@locks_in_use AS varchar(32))
                        + ' locks at or above the limit, but the oldest open transaction is '
                        + CAST(@oldest_open_tran_seconds AS varchar(32))
                        + ' s old: at ' + CAST(@lock_count_credible_locks_per_second AS varchar(32))
                        + ' locks a second none could have built them, so the counter (Lock Owner Blocks, '
                        + 'refreshed periodically) is showing a past peak; the scan is skipped all the same. '
                END
                + 'locks_in_use=' + CAST(@locks_in_use AS varchar(32))
                + ', limit=' + CAST(@lock_scan_skip_above AS varchar(32))
                + ', lock_memory_mb=' + ISNULL(CAST(@lock_memory_mb AS varchar(32)), '')
                + ', sessions_with_open_tran=' + CAST((SELECT COUNT(*) FROM Holder hc) AS varchar(20))
                + ', largest_open_transactions='
                + ISNULL(STUFF((
                    SELECT ' | spid=' + CAST(h2.session_id AS varchar(20))
                         + ' client_ip=' + ISNULL(h2.client_ip, '')
                         + ' login=' + ISNULL(h2.login_name, '')
                         + ' host=' + ISNULL(h2.host_name, '')
                         + ' database=' + ISNULL(h2.database_name, '')
                         + ' open_tran_seconds=' + CAST(h2.open_tran_seconds AS varchar(20))
                         + ' log_records=' + CAST(h2.log_records AS varchar(30))
                         + CASE WHEN h2.rn_age = 1 THEN ' (the oldest)' ELSE '' END
                    FROM Holder h2
                    WHERE h2.rn <= 5 OR h2.rn_age = 1
                    ORDER BY h2.rn
                    FOR XML PATH(''), TYPE
                  ).value('.', 'nvarchar(max)'), 1, 3, ''), 'none - no session has a transaction open')
                + ', count_credible=' + CAST(@count_credible AS varchar(10))
                + ', oldest_open_tran_seconds=' + CAST(@oldest_open_tran_seconds AS varchar(20))
            AS varchar(max)) AS message

        UNION ALL

        SELECT
            h.rn AS sort_key,
            CAST('SPID=' + CAST(h.session_id AS varchar(20)) AS varchar(256)) AS metric_item,
            CAST(h.log_records AS varchar(32)) AS metric_value,
            CAST('log record(s)' AS varchar(32)) AS metric_unit,
            -- When the count is credible, the largest writer and the oldest transaction are the
            -- two to look at first (CRITICAL) and the rest WARNING. When not credible, none is
            -- CRITICAL - all WARNING (they are named only as context).
            CAST(CASE WHEN @count_credible = 1 AND (h.rn = 1 OR h.rn_age = 1) THEN 'CRITICAL' ELSE 'WARNING' END
                 AS varchar(16)) AS status,
            h.open_tran_seconds AS lock_seconds,
            CAST(
                'Open transaction on a server whose locks were too many to scan - likely lock '
                + 'holder number ' + CAST(h.rn AS varchar(10)) + ' by log written'
                + CASE WHEN h.rn_age = 1 THEN ', and the oldest open transaction' ELSE '' END
                + '; its lock count is not known. '
                + 'spid=' + CAST(h.session_id AS varchar(20))
                + ', open_tran_seconds=' + CAST(h.open_tran_seconds AS varchar(20))
                + ', log_records=' + CAST(h.log_records AS varchar(30))
                + ', log_mb=' + CAST(h.log_bytes / 1048576 AS varchar(30))
                + ', session_status=' + ISNULL(h.session_status, '')
                + ', login=' + ISNULL(h.login_name, '')
                + ', host=' + ISNULL(h.host_name, '')
                + ', client_ip=' + ISNULL(h.client_ip, '')
                + ', program=' + ISNULL(h.program_name, '')
                + ', database=' + ISNULL(h.database_name, '')
                + ', oldest_tran_begin_time=' + ISNULL(CONVERT(varchar(19), h.oldest_tran_begin_time, 120), '')
                + ', last_request_end=' + ISNULL(CONVERT(varchar(19), h.last_request_end_time, 120), '')
                + ', locks_in_use=' + CAST(@locks_in_use AS varchar(32))
                + ', last_sql=' + ISNULL(h.last_sql, '')
            AS varchar(max)) AS message
        FROM Holder h
        WHERE h.rn <= 5 OR h.rn_age = 1
    ) out_rows
    ORDER BY out_rows.sort_key;

    RETURN;
END

IF OBJECT_ID('tempdb..#locks') IS NOT NULL
    DROP TABLE #locks;

SELECT
    tl.request_session_id,
    tl.resource_database_id,
    tl.resource_type,
    tl.request_mode,
    tl.request_owner_type,
    CAST(CASE WHEN tl.resource_type = 'APPLICATION'
              THEN LTRIM(RTRIM(tl.resource_description)) END AS nvarchar(256)) AS applock_name
INTO #locks
FROM sys.dm_tran_locks tl
WHERE tl.request_status = 'GRANT'
  AND (tl.request_mode IN ('X','IX','IU','U','RangeX-X','Sch-M')
       -- A session-owned application lock in ANY mode: a shared one still stops an exclusive
       -- request for the same name, and it is graded by the session's idle time below.
       OR (tl.resource_type = 'APPLICATION' AND tl.request_owner_type = 'SESSION'))
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
        SUM(CASE WHEN tl.resource_type = 'APPLICATION' AND tl.request_owner_type = 'SESSION'
                 THEN 1 ELSE 0 END) AS session_applock_count,
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
    --
    -- A session-owned application lock is graded by how long its session has sat idle instead.
    -- That lock is taken only to keep other workers out of a named piece of work. Held by a
    -- session nobody is driving, it stops that work - and its waiters give up at their own
    -- @LockTimeout and retry, so the wait never grows and "somebody is waiting" is true only at
    -- the instant a retry happens to be in flight. There is no transaction, so no log to hold and
    -- no transaction age: nothing else here would ever page for it.
    CASE
        WHEN ISNULL(ta.open_tran_seconds, 0) >= @critical_tran_seconds
             AND (ISNULL(r.blocking_session_id, 0) <> 0
                  OR EXISTS (SELECT 1 FROM sys.dm_exec_requests br
                             WHERE br.blocking_session_id = s.session_id))
        THEN 'CRITICAL'
        WHEN ISNULL(ta.open_tran_seconds, 0) >= @critical_tran_seconds
             AND ISNULL(s.status, '') = 'sleeping'
        THEN 'CRITICAL'
        WHEN ai.applock_idle_seconds >= @applock_idle_abandoned_seconds
             AND EXISTS (SELECT 1 FROM sys.dm_exec_requests br
                         WHERE br.blocking_session_id = s.session_id)
        THEN 'CRITICAL'
        WHEN ai.applock_idle_seconds >= @applock_idle_critical_seconds THEN 'CRITICAL'
        WHEN ISNULL(ta.open_tran_seconds, 0) >= @warning_tran_seconds THEN 'WARNING'
        WHEN ai.applock_idle_seconds >= @applock_idle_warning_seconds THEN 'WARNING'
        ELSE 'LOGGING'
    END AS status,

    lk.lock_seconds,

    CAST(
        'Session holding locks. '
        + 'spid=' + CAST(s.session_id AS varchar(20))
        + ', login=' + ISNULL(s.login_name, '')
        + ', host=' + ISNULL(s.host_name, '')
        -- host_name is whatever the client says it is, and an application can send any name or
        -- none; client_net_address is the address SQL Server saw the connection come from.
        + ', client_ip=' + ISNULL(CAST(c.client_net_address AS varchar(48)), '')
        + ', program=' + ISNULL(s.program_name, '')
        + ', session_status=' + ISNULL(s.status, '')
        + ', request_status=' + ISNULL(r.status, '')
        + ', command=' + ISNULL(r.command, '')
        + ', wait_type=' + ISNULL(r.wait_type, '')
        + ', wait_ms=' + ISNULL(CAST(r.wait_time AS varchar(20)), '')
        + ', blocking_session_id=' + ISNULL(CAST(r.blocking_session_id AS varchar(20)), '')
        + ', open_tran=' + CAST(ISNULL(ta.open_tran_count, 0) AS varchar(20))
        + ', lock_seconds=' + CAST(lk.lock_seconds AS varchar(20))
        + ', open_tran_seconds=' + CAST(ISNULL(ta.open_tran_seconds, 0) AS varchar(20))
        + ', session_applocks=' + CAST(la.session_applock_count AS varchar(20))
        + ', applock_idle_seconds=' + CAST(ai.applock_idle_seconds AS varchar(20))
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

        + ', applock_names='
        + ISNULL(STUFF((
            SELECT TOP (5) '; '
                + ISNULL(DB_NAME(x.resource_database_id), 'dbid=' + CAST(x.resource_database_id AS varchar(20)))
                + ' ' + x.request_mode + ' ' + ISNULL(x.applock_name, '')
            FROM #locks x
            WHERE x.request_session_id = s.session_id
              AND x.resource_type = 'APPLICATION'
              AND x.request_owner_type = 'SESSION'
            ORDER BY x.resource_database_id, x.applock_name
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
-- How long the session has been idle holding a session-owned application lock: 0 unless it holds
-- one and is sleeping. last_request_end_time can read ahead of GETDATE(), so floored at 0 - and a
-- NULL compares as unknown, which lands on 0 too.
CROSS APPLY (SELECT applock_idle_seconds =
    CASE WHEN la.session_applock_count > 0 AND s.status = 'sleeping'
              AND DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) > 0
         THEN DATEDIFF(SECOND, s.last_request_end_time, GETDATE())
         ELSE 0 END) ai
-- The larger of the transaction's age and that idle time.
CROSS APPLY (SELECT lock_seconds =
    CASE WHEN ai.applock_idle_seconds > ISNULL(ta.open_tran_seconds, 0)
         THEN ai.applock_idle_seconds ELSE ISNULL(ta.open_tran_seconds, 0) END) lk
WHERE s.is_user_process = 1
ORDER BY
    lk.lock_seconds DESC,
    ISNULL(ta.open_tran_seconds, 0) DESC,
    ISNULL(ta.open_tran_count, 0) DESC,
    la.lock_count DESC;