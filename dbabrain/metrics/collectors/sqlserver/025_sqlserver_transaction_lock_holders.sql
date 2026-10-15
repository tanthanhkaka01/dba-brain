-- Sessions holding locks: the few worth naming, plus one line counting the rest.
--
-- This used to emit one row per holder. On 192.0.2.115 that is 100+ rows every run - the
-- collector's row cap, hit every time - and on 2026-08-07 it produced a 28-part Telegram report
-- of 76 critical rows in which the session that actually caused the outage was part 18. An alert
-- stream that reports normal work at that volume is one nobody reads on the day it matters.
--
-- So the output is now two shapes:
--   * a DETAIL row per holder that is genuinely a finding - an old transaction that is blocking
--     somebody, or asleep - which is a handful even during an incident;
--   * one SUMMARY row counting everything else and listing its session ids, so nothing is hidden,
--     it is just not each given a line of its own.
--
-- sys.dm_tran_locks is read into a temp table first because it is read more than once below. On
-- 192.0.2.115 (1127 sessions, one holding 22,137 locks) reading it per output row meant
-- materialising the whole lock manager 62 times for 61 rows, and the metric took 103 seconds
-- against a 60-second timeout: it had never once succeeded on the one server that needed it.
--
-- It runs every 300s while the two emergency metrics beside it run every 150s, and that gap is
-- deliberate. Measured on 192.0.2.115: 009 (head blocker) 0.14s, 024 (sleeping holder) 0.13s,
-- this one 2.88s - of which ~1.2s is scanning 260,000 rows of sys.dm_tran_locks. Narrowing that
-- scan was tried and is slower (filtering to sessions with an open transaction: 1.45s; to
-- blocking-or-old only: 1.60s), because the lock manager is materialised whole before any
-- predicate applies. The cost is structural, and the concern is not the wall clock against a
-- 60s timeout - it is that scanning the lock manager takes latches ON the lock manager, which
-- is the last thing to do every 150 seconds to an instance that is already blocking. 009 now
-- names the head blocker, so this metric is no longer the detection path; it is the detail
-- behind one, and four times an hour is enough for that.

DECLARE @warning_tran_seconds  int = 300;    -- 5 min with a transaction open: worth a look
DECLARE @critical_tran_seconds int = 900;    -- 15 min, and blocking or asleep: an incident

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
--   and it is written into the DECLARE below, whose number is only the default. 500,000 is about
--   twice the largest count this scan has been measured to walk safely (260,000 in 1.2 s) and
--   under half of what stalled a server.
-- * Who holds the locks cannot be counted without the scan, so the holders are named by what
--   can be read without it: the open transactions, largest first by log written, then oldest.
--   That ranking named the 2026-10-07 session. Each row carries the session, its login, host,
--   client address and program. Two are CRITICAL: the largest writer and the oldest open
--   transaction. One of the two is the holder whether it changed a million rows or only read
--   them under an isolation level that keeps its shared locks - a session that wrote nothing
--   has no log to rank by. The rest are WARNING.
-- * No temp table here, unlike below: a server in this state is the last place to create one.
DECLARE @lock_scan_skip_above bigint = 500000;
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
            CAST('CRITICAL' AS varchar(16)) AS status,
            ISNULL((SELECT MAX(h0.open_tran_seconds) FROM Holder h0), 0) AS lock_seconds,
            CAST(
                'Lock scan SKIPPED: the lock manager holds ' + CAST(@locks_in_use AS varchar(32))
                + ' locks, at or above the limit of ' + CAST(@lock_scan_skip_above AS varchar(32))
                + '. Enumerating that many latches the lock manager and stalls every session that '
                + 'takes or releases a lock, so lock counts per session are not known; the holders '
                + 'named are the open transactions, largest first by log written, and the oldest. '
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
            AS varchar(max)) AS message

        UNION ALL

        SELECT
            h.rn AS sort_key,
            CAST('SPID=' + CAST(h.session_id AS varchar(20)) AS varchar(256)) AS metric_item,
            CAST(h.log_records AS varchar(32)) AS metric_value,
            CAST('log record(s)' AS varchar(32)) AS metric_unit,
            -- The largest writer and the oldest transaction are the two to look at first. The
            -- rest are named so that a ranking by log written, which is a proxy, cannot hide the
            -- real holder behind one line.
            CAST(CASE WHEN h.rn = 1 OR h.rn_age = 1 THEN 'CRITICAL' ELSE 'WARNING' END
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
    tl.request_mode
INTO #locks
FROM sys.dm_tran_locks tl
WHERE tl.request_status = 'GRANT'
  AND tl.request_mode IN ('X','IX','IU','U','RangeX-X','Sch-M')
  -- Never report the collector's own session. Snapshotting into #locks is itself a temp-table
  -- create, which takes X locks on tempdb OBJECT, so this metric started alerting on itself the
  -- hour that optimisation shipped. A monitoring query is not a finding about the server.
  AND tl.request_session_id <> @@SPID;

IF OBJECT_ID('tempdb..#holders') IS NOT NULL
    DROP TABLE #holders;

;WITH TranAge AS
(
    SELECT
        st.session_id,
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
        SUM(CASE WHEN tl.resource_database_id = 2 THEN 1 ELSE 0 END) AS tempdb_lock_count,
        MIN(tl.resource_database_id) AS min_database_id
    FROM #locks tl
    GROUP BY tl.request_session_id
)
SELECT
    s.session_id,
    la.lock_count,
    la.tempdb_lock_count,
    la.min_database_id,
    ISNULL(ta.open_tran_seconds, 0) AS open_tran_seconds,
    ta.oldest_tran_begin_time,
    s.status AS session_status,
    s.open_transaction_count,
    s.login_name,
    s.host_name,
    -- host_name is whatever the client says it is, and an application can send any name or
    -- none; client_net_address is the address SQL Server saw the connection come from.
    CAST(cn.client_net_address AS varchar(48)) AS client_ip,
    s.program_name,
    s.last_request_end_time,
    r.status AS request_status,
    r.command,
    r.wait_type,
    r.blocking_session_id,
    CAST(CASE
        WHEN EXISTS (SELECT 1 FROM sys.dm_exec_requests br WHERE br.blocking_session_id = s.session_id)
        THEN 1 ELSE 0 END AS int) AS is_blocking,
    CAST((SELECT COUNT(*) FROM sys.dm_exec_requests br2 WHERE br2.blocking_session_id = s.session_id)
         AS int) AS blocked_sessions,
    -- The last statement the session ran before it went to sleep. Without it a report says a
    -- session is holding locks and stops there; with it, the 2026-08-07 head blocker reads
    -- "FROM NUMBERSEQUENCETABLE T1 WITH (UPDLOCK)" and the cause is on the screen.
    --
    -- Head AND tail, because an AX SELECT's first 600 characters are its column list: that query
    -- is 732 characters once newlines become spaces, and a head-only cut ended at T1.RECVERSION,
    -- losing both the table and its lock hint.
    CAST(CASE WHEN LEN(f.flat) > 700
              THEN LEFT(f.flat, 250) + ' ... ' + RIGHT(f.flat, 450)
              ELSE f.flat END AS varchar(max)) AS last_sql
INTO #holders
FROM LockAgg la
JOIN sys.dm_exec_sessions s
    ON s.session_id = la.session_id
LEFT JOIN TranAge ta
    ON ta.session_id = s.session_id
LEFT JOIN sys.dm_exec_requests r
    ON r.session_id = s.session_id
LEFT JOIN sys.dm_exec_connections cn
    ON cn.session_id = s.session_id
OUTER APPLY sys.dm_exec_sql_text(ISNULL(r.sql_handle, cn.most_recent_sql_handle)) t
CROSS APPLY (SELECT flat = REPLACE(REPLACE(REPLACE(ISNULL(t.text, ''),
                            CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' ')) f
WHERE s.is_user_process = 1;

-- ---------------------------------------------------------------------------
-- 1. The findings, named individually.
-- ---------------------------------------------------------------------------
SELECT
    CAST('SPID=' + CAST(h.session_id AS varchar(20)) AS varchar(256)) AS metric_item,
    CAST(h.lock_count AS varchar(32)) AS metric_value,
    CAST('lock(s)' AS varchar(32)) AS metric_unit,
    -- CRITICAL means somebody is waiting. Nothing else.
    --
    -- This used to add `OR session_status = 'sleeping'`, on the reasoning that a session which
    -- opened a transaction and walked away is the classic cause of a stuck log. The risk is real,
    -- but the rule paged for it on a bare timer: on 192.0.2.115 it produced three CRITICALs
    -- (SPID 439/544/795, transactions 23-33 minutes old) that were blocking nobody at all, and
    -- their blocked_session_ids were empty because there was nothing to list.
    --
    -- It also contradicted the metric beside it: 024 was changed the same day to treat exactly
    -- this - sleeping, holding, blocking nobody - as WARNING. Two metrics disagreeing about one
    -- condition means whichever shouts loudest wins, and the loud one was measuring the wrong
    -- thing. A long transaction that threatens log truncation is LOG_REUSE_WAIT's question, and
    -- that metric answers it by reading log_reuse_wait_desc rather than inferring from a clock.
    --
    -- These sessions still appear, as WARNING, with open_tran_seconds in the message. They are
    -- visible; they just do not wake anyone at 3am before anybody is affected.
    CASE
        WHEN h.open_tran_seconds >= @critical_tran_seconds AND h.is_blocking = 1 THEN 'CRITICAL'
        ELSE 'WARNING'
    END AS status,
    h.open_tran_seconds AS lock_seconds,
    CAST(
        'Session holding locks. '
        + 'spid=' + CAST(h.session_id AS varchar(20))
        + ', blocked_sessions=' + CAST(h.blocked_sessions AS varchar(20))
        + ', open_tran_seconds=' + CAST(h.open_tran_seconds AS varchar(20))
        + ', session_status=' + ISNULL(h.session_status, '')
        + ', request_status=' + ISNULL(h.request_status, '')
        + ', login=' + ISNULL(h.login_name, '')
        + ', host=' + ISNULL(h.host_name, '')
        + ', client_ip=' + ISNULL(h.client_ip, '')
        + ', program=' + ISNULL(h.program_name, '')
        + ', command=' + ISNULL(h.command, '')
        + ', wait_type=' + ISNULL(h.wait_type, '')
        + ', blocking_session_id=' + ISNULL(CAST(h.blocking_session_id AS varchar(20)), '')
        + ', lock_count=' + CAST(h.lock_count AS varchar(20))
        + ', tempdb_lock_count=' + CAST(h.tempdb_lock_count AS varchar(20))
        + ', database=' + ISNULL(DB_NAME(h.min_database_id), 'dbid=' + CAST(h.min_database_id AS varchar(20)))
        + ', oldest_tran_begin_time=' + ISNULL(CONVERT(varchar(19), h.oldest_tran_begin_time, 120), '')
        + ', last_request_end=' + ISNULL(CONVERT(varchar(19), h.last_request_end_time, 120), '')
        + ', blocked_session_ids='
        + ISNULL(STUFF((
            SELECT TOP (50) ',' + CAST(br.session_id AS varchar(20))
            FROM sys.dm_exec_requests br
            WHERE br.blocking_session_id = h.session_id
            ORDER BY br.session_id
            FOR XML PATH(''), TYPE
          ).value('.', 'nvarchar(max)'), 1, 1, ''), '')
        + ', last_sql=' + ISNULL(h.last_sql, '')
    AS varchar(max)) AS message
FROM #holders h
-- A held lock is telemetry. It becomes a finding when the transaction behind it is OLD, and an
-- incident when it is old AND somebody is waiting on it, or it is sleeping - a session that
-- opened a transaction and walked away is the classic cause of a stuck log.
WHERE h.open_tran_seconds >= @warning_tran_seconds
   OR h.is_blocking = 1

UNION ALL

-- ---------------------------------------------------------------------------
-- 2. Everything else: counted, not listed one per row.
-- ---------------------------------------------------------------------------
SELECT
    CAST('holders_summary' AS varchar(256)) AS metric_item,
    CAST(COUNT(*) AS varchar(32)) AS metric_value,
    CAST('session(s)' AS varchar(32)) AS metric_unit,
    'LOGGING' AS status,
    0 AS lock_seconds,
    CAST(
        'Sessions holding locks with nothing wrong yet. '
        + 'sessions=' + CAST(COUNT(*) AS varchar(20))
        + ', total_locks=' + CAST(SUM(CAST(q.lock_count AS bigint)) AS varchar(30))
        + ', max_open_tran_seconds=' + CAST(MAX(q.open_tran_seconds) AS varchar(20))
        + ', session_ids='
        + ISNULL(STUFF((
            SELECT TOP (200) ',' + CAST(q2.session_id AS varchar(20))
            FROM #holders q2
            WHERE q2.open_tran_seconds < @warning_tran_seconds
              AND q2.is_blocking = 0
            ORDER BY q2.session_id
            FOR XML PATH(''), TYPE
          ).value('.', 'nvarchar(max)'), 1, 1, ''), '')
    AS varchar(max)) AS message
FROM #holders q
WHERE q.open_tran_seconds < @warning_tran_seconds
  AND q.is_blocking = 0
HAVING COUNT(*) > 0

ORDER BY 4 DESC, 5 DESC;

DROP TABLE #locks;
DROP TABLE #holders;
