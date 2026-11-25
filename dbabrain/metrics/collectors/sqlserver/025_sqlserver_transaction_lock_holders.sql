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
-- sys.dm_tran_locks is read once, grouped, because it is needed more than once below. On
-- 192.0.2.115 (1127 sessions, one holding 22,137 locks) reading it per output row meant
-- materialising the whole lock manager 62 times for 61 rows, and the metric took 103 seconds
-- against a 60-second timeout: it had never once succeeded on the one server that needed it.
--
-- It runs every 300s while the two emergency metrics beside it run every 150s, and that gap is
-- deliberate. Measured on 192.0.2.115: 009 (head blocker) 0.14s, 024 (sleeping holder) 0.13s,
-- this one 2.88s - of which ~1.2s is scanning 260,000 rows of sys.dm_tran_locks. Narrowing that
-- scan was tried and is slower (filtering to sessions with an open transaction: 1.45s; to
-- blocking-or-old only: 1.60s), because the lock manager is materialised whole before any
-- predicate applies. The cost is structural: by the lock, whatever is asked of it. 009 now
-- names the head blocker, so this metric is no longer the detection path; it is the detail
-- behind one, and four times an hour is enough for that.

DECLARE @warning_tran_seconds  int = 300;    -- 5 min with a transaction open: worth a look
DECLARE @critical_tran_seconds int = 900;    -- 15 min, and blocking or asleep: an incident

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

-- ---------------------------------------------------------------------------
-- 0. Is the lock manager too large to read whole?
-- ---------------------------------------------------------------------------
-- Reading sys.dm_tran_locks costs by the lock: about 11 microseconds each on a busy ERP server
-- (59,000 locks in 0.65 s, 2026-11-23), so 10 million is two minutes and 100 million twenty. The
-- size is read first, from a counter that enumerates nothing, and above the limit the locks are
-- SAMPLED instead of read whole (until 1.1.0 they were not read at all).
--
-- * The counter is 'Lock Owner Blocks' - the locks in use now, which is what a scan would have
--   to walk (measured: 800 on the counter against 798 rows of sys.dm_tran_locks). NOT the size
--   of the lock manager's memory: that store keeps its pages after the locks are gone - 3,700 MB
--   with 800 locks in use in 2026-10, 30 GB with 14,000 on 2026-11-23 - so a limit in megabytes
--   would have stayed CRITICAL for good.
-- * The sample is the first @lock_sample_rows rows. The view gives its rows as it walks the lock
--   hash: TOP stops it early (TOP 1000 of 59,000 took 0 ms where all of them took 650), and what
--   comes first is spread over the holders as the whole is - 10,000 of 62,736 locks gave the two
--   largest holders 59.8 % and 34.9 %, the whole 62.4 % and 35.7 % (2026-11-23). The DATABASE
--   locks, one per connection, come first and are not counted. So the cost no longer depends on
--   how many locks there are, and the holders are named by their locks: until 1.1.0 by the log
--   they wrote, which a session that only reads does not write.
-- * A sample that ends before @lock_sample_rows is every lock there is, whatever the counter says:
--   the counter is refreshed periodically and can hold a past peak (2026-10-15: 15.7 M for eight
--   minutes after the job that built them had ended). It is then LOGGING, nothing to act on.
-- * The size alone is not a finding: jobs on this estate build 2 to 20 million locks and end. What
--   is graded is who holds them - a session with @lock_holder_min_share_pct of the sample whose
--   transaction is old, or that somebody waits on. On 2026-11-17 to 11-23 five sessions held 161
--   million locks in transactions open for six days; a limit on the count alone had paged every
--   five minutes since the first evening, and for every short job before it.
-- * The sample goes into a variable, not a temp table: writing a row per lock into tempdb inside
--   the statement that reads them is what other sessions queued behind (see the scan below).
DECLARE @lock_scan_skip_above bigint = 1000000;
DECLARE @lock_sample_rows bigint = 200000;
DECLARE @lock_holder_min_share_pct int = 10;
DECLARE @lock_holder_warning_seconds int = 1800;     -- 30 min holding that many: worth a look
DECLARE @lock_holder_critical_seconds int = 14400;   -- 4 h: nobody is coming back to it
-- Locks belong to transactions, and a transaction builds them at a bounded rate, so the counter is
-- credible only if the oldest open user transaction is old enough to have built it at this many
-- locks a second. A rate rather than a fixed age: a job's real 8.6 M had transactions 50 s old.
-- 2,000,000 is above the whole server's measured 1.2 M lock requests a second.
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
    DECLARE @sample xml;
    DECLARE @sample_rows bigint;
    DECLARE @sample_locks bigint;
    DECLARE @sample_started datetime;
    DECLARE @sample_ms int;
    DECLARE @said varchar(400);

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

    SET @sample_started = GETDATE();
    SELECT @sample = (
        SELECT x.sid AS [@sid],
               COUNT_BIG(*) AS [@rows],
               SUM(CASE WHEN x.resource_type <> 'DATABASE' AND x.request_status = 'GRANT'
                        THEN CAST(1 AS bigint) ELSE 0 END) AS [@locks]
        FROM (SELECT TOP (@lock_sample_rows)
                     tl.request_session_id AS sid, tl.resource_type, tl.request_status
              FROM sys.dm_tran_locks tl) x
        GROUP BY x.sid
        FOR XML PATH('h'), TYPE)
    OPTION (HASH GROUP, MAXDOP 1);
    SET @sample_ms = DATEDIFF(MILLISECOND, @sample_started, GETDATE());

    SELECT @sample_rows = ISNULL(SUM(q.h.value('@rows', 'bigint')), 0),
           @sample_locks = ISNULL(SUM(q.h.value('@locks', 'bigint')), 0)
    FROM @sample.nodes('/h') q(h);

    SET @count_credible = CASE
        -- The sample ended early: it is every lock there is, and the counter is behind.
        WHEN @sample_rows < @lock_sample_rows THEN 0
        WHEN CAST(@oldest_open_tran_seconds AS bigint) * @lock_count_credible_locks_per_second
             >= @locks_in_use THEN 1
        ELSE 0
    END;

    -- The numbers a reader needs first, in the units a person says them in.
    SET @said = 'locks in use ' + CAST(CAST(@locks_in_use / 1000000.0 AS decimal(12, 2)) AS varchar(20))
        + ' M, read whole up to ' + CAST(CAST(@lock_scan_skip_above / 1000000.0 AS decimal(12, 2)) AS varchar(20))
        + ' M; sampled ' + CAST(@sample_rows AS varchar(20)) + ' of them'
        + CASE WHEN @locks_in_use > 0 AND @sample_rows >= @lock_sample_rows
               THEN ' (' + CAST(CAST(100.0 * @sample_rows / @locks_in_use AS decimal(9, 3)) AS varchar(20)) + ' %)'
               ELSE '' END
        + ' in ' + CAST(CAST(@sample_ms / 1000.0 AS decimal(9, 2)) AS varchar(20)) + ' s'
        + '; lock memory ' + ISNULL(CAST(CAST(@lock_memory_mb / 1024.0 AS decimal(9, 1)) AS varchar(20)), '?') + ' GB. ';

    ;WITH Sampled AS
    (
        SELECT q.h.value('@sid', 'int') AS session_id, q.h.value('@locks', 'bigint') AS locks
        FROM @sample.nodes('/h') q(h)
    ),
    OpenTran AS
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
            s.session_id,
            o.oldest_tran_begin_time,
            ISNULL(o.log_records, 0) AS log_records,
            ISNULL(o.log_bytes, 0) AS log_bytes,
            CASE
                WHEN o.oldest_tran_begin_time IS NULL
                     OR DATEDIFF(SECOND, o.oldest_tran_begin_time, GETDATE()) < 0 THEN 0
                ELSE DATEDIFF(SECOND, o.oldest_tran_begin_time, GETDATE())
            END AS open_tran_seconds,
            ISNULL(p.locks, 0) AS sampled_locks,
            CASE WHEN @sample_locks > 0
                 THEN CAST(100.0 * ISNULL(p.locks, 0) / @sample_locks AS decimal(5, 1))
                 ELSE 0 END AS share_pct,
            -- Every lock was read when the sample ended early; else its share of the counter.
            CASE WHEN @sample_rows < @lock_sample_rows OR @sample_locks = 0 THEN ISNULL(p.locks, 0)
                 ELSE CAST(1.0 * ISNULL(p.locks, 0) / @sample_locks * @locks_in_use AS bigint)
            END AS locks_held,
            CAST((SELECT COUNT(*) FROM sys.dm_exec_requests br
                  WHERE br.blocking_session_id = s.session_id) AS int) AS blocked_sessions,
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
             WHERE st2.session_id = s.session_id
             ORDER BY dt2.database_transaction_log_bytes_used DESC) AS database_name,
            CAST(CASE WHEN LEN(f.flat) > 700
                      THEN LEFT(f.flat, 250) + ' ... ' + RIGHT(f.flat, 450)
                      ELSE f.flat END AS varchar(max)) AS last_sql,
            ROW_NUMBER() OVER (ORDER BY ISNULL(p.locks, 0) DESC, ISNULL(o.log_bytes, 0) DESC,
                                        s.session_id) AS rn,
            ROW_NUMBER() OVER (ORDER BY CASE WHEN o.oldest_tran_begin_time IS NULL THEN 1 ELSE 0 END,
                                        o.oldest_tran_begin_time ASC, s.session_id) AS rn_age
        FROM sys.dm_exec_sessions s
        LEFT JOIN OpenTran o
            ON o.session_id = s.session_id
        LEFT JOIN Sampled p
            ON p.session_id = s.session_id
        -- One connection per session: a MARS session has several rows here, and each would
        -- repeat the session in the ranking.
        OUTER APPLY (SELECT TOP (1) c.client_net_address, c.most_recent_sql_handle
                     FROM sys.dm_exec_connections c
                     WHERE c.session_id = s.session_id
                     ORDER BY c.connect_time) cn
        OUTER APPLY sys.dm_exec_sql_text(cn.most_recent_sql_handle) t
        CROSS APPLY (SELECT flat = REPLACE(REPLACE(REPLACE(ISNULL(t.text, ''),
                                    CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' ')) f
        WHERE s.is_user_process = 1
          AND s.session_id <> @@SPID
          AND (o.session_id IS NOT NULL OR p.locks > 0)
    ),
    Graded AS
    (
        SELECT
            h.session_id, h.oldest_tran_begin_time, h.log_records, h.log_bytes, h.open_tran_seconds,
            h.sampled_locks, h.share_pct, h.locks_held, h.blocked_sessions, h.session_status,
            h.login_name, h.host_name, h.program_name, h.last_request_end_time, h.client_ip,
            h.database_name, h.last_sql, h.rn, h.rn_age,
            CASE
                WHEN @count_credible = 0 OR h.share_pct < @lock_holder_min_share_pct THEN 0
                WHEN h.open_tran_seconds >= @lock_holder_critical_seconds THEN 2
                WHEN h.open_tran_seconds >= @critical_tran_seconds AND h.blocked_sessions > 0 THEN 2
                WHEN h.open_tran_seconds >= @lock_holder_warning_seconds THEN 1
                WHEN h.blocked_sessions > 0 THEN 1
                ELSE 0
            END AS grade
        FROM Holder h
        -- A holder is a session with a share of the sample; the oldest transaction is named beside
        -- them, sampled or not.
        WHERE (h.rn <= 5 AND h.share_pct >= 1) OR (h.rn_age = 1 AND h.open_tran_seconds > 0)
    )
    SELECT out_rows.metric_item, out_rows.metric_value, out_rows.metric_unit, out_rows.status,
           out_rows.lock_seconds, out_rows.message
    FROM
    (
        SELECT
            0 AS sort_key,
            CAST('lock_scan_sampled' AS varchar(256)) AS metric_item,
            CAST(@locks_in_use AS varchar(32)) AS metric_value,
            CAST('lock(s)' AS varchar(32)) AS metric_unit,
            CAST(CASE ISNULL((SELECT MAX(g0.grade) FROM Graded g0), 0)
                      WHEN 2 THEN 'CRITICAL' WHEN 1 THEN 'WARNING' ELSE 'LOGGING' END
                 AS varchar(16)) AS status,
            @oldest_open_tran_seconds AS lock_seconds,
            CAST(
                CASE
                    WHEN @sample_rows < @lock_sample_rows THEN
                        'Locks read whole: ' + @said + 'The sample ended early, so it is every lock there is ('
                        + CAST(@sample_rows AS varchar(32))
                        + ') and the counter (Lock Owner Blocks, refreshed periodically) is showing a past peak. '
                    WHEN @count_credible = 0 THEN
                        'Locks SAMPLED: ' + @said + 'The oldest open transaction is '
                        + CAST(@oldest_open_tran_seconds AS varchar(32))
                        + ' s old: at ' + CAST(@lock_count_credible_locks_per_second AS varchar(32))
                        + ' locks a second none could have built them, so the counter is showing a past peak. '
                    ELSE
                        'Locks SAMPLED: ' + @said
                        + 'Too many to read whole: the locks of each holder are estimated from its share of the sample. '
                END
                + 'locks_in_use=' + CAST(@locks_in_use AS varchar(32))
                + ', limit=' + CAST(@lock_scan_skip_above AS varchar(32))
                + ', sampled=' + CAST(@sample_rows AS varchar(32))
                + ', sample_ms=' + CAST(@sample_ms AS varchar(20))
                + ', lock_memory_mb=' + ISNULL(CAST(@lock_memory_mb AS varchar(32)), '')
                + ', holders='
                + ISNULL(STUFF((
                    SELECT ' | spid=' + CAST(h2.session_id AS varchar(20))
                         + ' share_pct=' + CAST(h2.share_pct AS varchar(20))
                         + ' locks=' + CAST(CAST(h2.locks_held / 1000000.0 AS decimal(12, 2)) AS varchar(20)) + 'M'
                         + ' open_tran_seconds=' + CAST(h2.open_tran_seconds AS varchar(20))
                         + ' blocked_sessions=' + CAST(h2.blocked_sessions AS varchar(20))
                         + ' client_ip=' + ISNULL(h2.client_ip, '')
                         + ' login=' + ISNULL(h2.login_name, '')
                         + ' host=' + ISNULL(h2.host_name, '')
                         + ' database=' + ISNULL(h2.database_name, '')
                         + CASE WHEN h2.rn_age = 1 THEN ' (the oldest)' ELSE '' END
                    FROM Graded h2
                    ORDER BY h2.rn
                    FOR XML PATH(''), TYPE
                  ).value('.', 'nvarchar(max)'), 1, 3, ''), 'none - no session holds a share of them')
                + ', count_credible=' + CAST(@count_credible AS varchar(10))
                + ', oldest_open_tran_seconds=' + CAST(@oldest_open_tran_seconds AS varchar(20))
            AS varchar(max)) AS message

        UNION ALL

        SELECT
            h.rn AS sort_key,
            CAST('SPID=' + CAST(h.session_id AS varchar(20)) AS varchar(256)) AS metric_item,
            CAST(h.locks_held AS varchar(32)) AS metric_value,
            CAST('lock(s)' AS varchar(32)) AS metric_unit,
            CAST(CASE h.grade WHEN 2 THEN 'CRITICAL' WHEN 1 THEN 'WARNING' ELSE 'LOGGING' END
                 AS varchar(16)) AS status,
            h.open_tran_seconds AS lock_seconds,
            CAST(
                'Session holding locks on a server whose locks were sampled'
                + CASE WHEN h.rn_age = 1 THEN ' - the oldest open transaction' ELSE '' END + '. '
                + 'spid=' + CAST(h.session_id AS varchar(20))
                + ', share_pct=' + CAST(h.share_pct AS varchar(20))
                + ', lock_count=' + CAST(h.locks_held AS varchar(30))
                + CASE WHEN @sample_rows < @lock_sample_rows THEN ''
                       ELSE ' (estimated, ' + CAST(CAST(h.locks_held / 1000000.0 AS decimal(12, 2)) AS varchar(20))
                            + ' M of ' + CAST(CAST(@locks_in_use / 1000000.0 AS decimal(12, 2)) AS varchar(20))
                            + ' M, from ' + CAST(h.sampled_locks AS varchar(20)) + ' of '
                            + CAST(@sample_locks AS varchar(20)) + ' sampled)' END
                + ', blocked_sessions=' + CAST(h.blocked_sessions AS varchar(20))
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
        FROM Graded h
    ) out_rows
    ORDER BY out_rows.sort_key;

    RETURN;
END

IF OBJECT_ID('tempdb..#locks') IS NOT NULL
    DROP TABLE #locks;

-- Grouped as it is read, into a table made before the read. Until 1.1.0 it was SELECT ... INTO, a
-- row per lock: on 192.0.2.115 every time more than a million write locks went through it (1.05 to
-- 1.93 million rows, 24 to 54 s, 2026-10-24 to 11-02, and 2026-10-07) it was slow, and six times it
-- was the head blocker of 13 to 68 sessions, waiting on LATCH_EX - tempdb was being written inside
-- the one statement and transaction that read the locks. With as many locks and 15 of them written
-- it took 9 to 18 s and nobody waited. Grouped, 58,742 locks become 8 rows, and the hash aggregate
-- has read them all before the first is written. MAXDOP 1: one reader walks the lock manager.
CREATE TABLE #locks
(
    request_session_id   int,
    resource_database_id int,
    resource_type        nvarchar(60),
    request_mode         nvarchar(60),
    request_owner_type   nvarchar(60),
    applock_name         nvarchar(256),
    lock_count           bigint
);

INSERT INTO #locks (request_session_id, resource_database_id, resource_type, request_mode,
                    request_owner_type, applock_name, lock_count)
SELECT
    tl.request_session_id,
    tl.resource_database_id,
    tl.resource_type,
    tl.request_mode,
    tl.request_owner_type,
    CAST(CASE WHEN tl.resource_type = 'APPLICATION'
              THEN LTRIM(RTRIM(tl.resource_description)) END AS nvarchar(256)) AS applock_name,
    COUNT_BIG(*)
FROM sys.dm_tran_locks tl
WHERE tl.request_status = 'GRANT'
  AND (tl.request_mode IN ('X','IX','IU','U','RangeX-X','Sch-M')
       -- A session-owned application lock in ANY mode: a shared one still stops an exclusive
       -- request for the same name, and it is graded by the session's idle time below.
       OR (tl.resource_type = 'APPLICATION' AND tl.request_owner_type = 'SESSION'))
  -- Never report the collector's own session. Snapshotting into #locks is itself a temp-table
  -- create, which takes X locks on tempdb OBJECT, so this metric started alerting on itself the
  -- hour that optimisation shipped. A monitoring query is not a finding about the server.
  AND tl.request_session_id <> @@SPID
GROUP BY
    tl.request_session_id,
    tl.resource_database_id,
    tl.resource_type,
    tl.request_mode,
    tl.request_owner_type,
    CAST(CASE WHEN tl.resource_type = 'APPLICATION'
              THEN LTRIM(RTRIM(tl.resource_description)) END AS nvarchar(256))
OPTION (HASH GROUP, MAXDOP 1);

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
        SUM(tl.lock_count) AS lock_count,
        SUM(CASE WHEN tl.resource_database_id = 2 THEN tl.lock_count ELSE 0 END) AS tempdb_lock_count,
        SUM(CASE WHEN tl.resource_type = 'APPLICATION' AND tl.request_owner_type = 'SESSION'
                 THEN tl.lock_count ELSE 0 END) AS session_applock_count,
        MIN(tl.resource_database_id) AS min_database_id
    FROM #locks tl
    GROUP BY tl.request_session_id
)
SELECT
    s.session_id,
    la.lock_count,
    la.tempdb_lock_count,
    la.session_applock_count,
    ai.applock_idle_seconds,
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
-- How long the session has been idle holding a session-owned application lock: 0 unless it holds
-- one and is sleeping. last_request_end_time can read ahead of GETDATE(), so floored at 0 - and a
-- NULL compares as unknown, which lands on 0 too.
CROSS APPLY (SELECT applock_idle_seconds =
    CASE WHEN la.session_applock_count > 0 AND s.status = 'sleeping'
              AND DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) > 0
         THEN DATEDIFF(SECOND, s.last_request_end_time, GETDATE())
         ELSE 0 END) ai
WHERE s.is_user_process = 1;

-- ---------------------------------------------------------------------------
-- 1. The findings, named individually.
-- ---------------------------------------------------------------------------
SELECT
    CAST('SPID=' + CAST(h.session_id AS varchar(20)) AS varchar(256)) AS metric_item,
    CAST(h.lock_count AS varchar(32)) AS metric_value,
    CAST('lock(s)' AS varchar(32)) AS metric_unit,
    -- For a transaction, CRITICAL means somebody is waiting. Nothing else.
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
    --
    -- A session-owned application lock is the one case graded on a timer alone, and the reason
    -- above does not reach it. That lock is taken only to keep other workers out of a named piece
    -- of work. Held by a session nobody is driving, it stops that work - and its waiters give up
    -- at their own @LockTimeout and retry, so the wait never grows and "somebody is waiting" is
    -- true only at the instant a retry happens to be in flight. Nothing else here would ever
    -- page for it: there is no transaction, so no log to hold and no transaction age.
    CASE
        WHEN h.open_tran_seconds >= @critical_tran_seconds AND h.is_blocking = 1 THEN 'CRITICAL'
        WHEN h.applock_idle_seconds >= @applock_idle_abandoned_seconds AND h.is_blocking = 1
            THEN 'CRITICAL'
        WHEN h.applock_idle_seconds >= @applock_idle_critical_seconds THEN 'CRITICAL'
        ELSE 'WARNING'
    END AS status,
    CASE WHEN h.applock_idle_seconds > h.open_tran_seconds
         THEN h.applock_idle_seconds ELSE h.open_tran_seconds END AS lock_seconds,
    CAST(
        'Session holding locks. '
        + 'spid=' + CAST(h.session_id AS varchar(20))
        + ', blocked_sessions=' + CAST(h.blocked_sessions AS varchar(20))
        + ', open_tran_seconds=' + CAST(h.open_tran_seconds AS varchar(20))
        + ', session_applocks=' + CAST(h.session_applock_count AS varchar(20))
        + ', applock_idle_seconds=' + CAST(h.applock_idle_seconds AS varchar(20))
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
        + ', applock_names='
        + ISNULL(STUFF((
            SELECT TOP (5) '; '
                + ISNULL(DB_NAME(x.resource_database_id), 'dbid=' + CAST(x.resource_database_id AS varchar(20)))
                + ' ' + x.request_mode + ' ' + ISNULL(x.applock_name, '')
            FROM #locks x
            WHERE x.request_session_id = h.session_id
              AND x.resource_type = 'APPLICATION'
              AND x.request_owner_type = 'SESSION'
            ORDER BY x.resource_database_id, x.applock_name
            FOR XML PATH(''), TYPE
          ).value('.', 'nvarchar(max)'), 1, 2, ''), '')
        + ', last_sql=' + ISNULL(h.last_sql, '')
    AS varchar(max)) AS message
FROM #holders h
-- A held lock is telemetry. It becomes a finding when the transaction behind it is OLD, and an
-- incident when it is old AND somebody is waiting on it, or it is sleeping - a session that
-- opened a transaction and walked away is the classic cause of a stuck log. A session-owned
-- application lock becomes one when the session holding it has sat idle.
WHERE h.open_tran_seconds >= @warning_tran_seconds
   OR h.is_blocking = 1
   OR h.applock_idle_seconds >= @applock_idle_warning_seconds

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
              AND q2.applock_idle_seconds < @applock_idle_warning_seconds
            ORDER BY q2.session_id
            FOR XML PATH(''), TYPE
          ).value('.', 'nvarchar(max)'), 1, 1, ''), '')
    AS varchar(max)) AS message
FROM #holders q
WHERE q.open_tran_seconds < @warning_tran_seconds
  AND q.is_blocking = 0
  AND q.applock_idle_seconds < @applock_idle_warning_seconds
HAVING COUNT(*) > 0

ORDER BY 4 DESC, 5 DESC;

DROP TABLE #locks;
DROP TABLE #holders;
