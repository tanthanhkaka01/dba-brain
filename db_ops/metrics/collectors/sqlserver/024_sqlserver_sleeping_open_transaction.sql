-- A session asleep holding a transaction: harmless most of the time, an incident when it blocks.
--
-- The thresholds this replaces were WARNING at 1 day idle and CRITICAL at 365 days, so every row
-- it ever produced read OK - 209 of them on 2026-08-07 while 89 sessions were stuck behind one.
-- A metric that cannot reach its own warning state inside a year is not a check.
--
-- The other half of the fix is what it measures. Idle time alone is the wrong question: a
-- sleeping session with an open transaction that blocks nobody is normal application behaviour
-- and there are hundreds of them on a busy AX instance, while one that blocks 89 sessions is an
-- emergency at thirty seconds. So blocking decides severity, and idle time sharpens it - a
-- blocker that is still working may finish on its own, one that has walked away will not.
--
-- Making blocking sufficient on its own was too far, though. `status = 'sleeping'` is an
-- INSTANT, not a duration: SQL Server sets it the moment a statement finishes and the connection
-- waits for the client's next command. D365 F&O opens a TTS block and then issues many small
-- statements with application think-time between them, so at any sampled moment most sessions
-- inside a transaction read as sleeping with idle_seconds = 0. On 2026-08-10 that raised a
-- CRITICAL for SPID 723 - idle_seconds=0, last_request_end four seconds in the future of the
-- report header, mid-INSERT into a tempdb table - describing a session that was demonstrably
-- working as one that had fallen asleep. Worse, it was a duplicate: LOCK_BLOCKING_SESSIONS had
-- already reported the same SPID correctly, because a live session blocking someone is exactly
-- what that metric is for.
--
-- So idle time is now a REQUIRED condition here, not a modifier. Below @idle_abandoned_seconds
-- the session is still working and blocking belongs to LOCK_BLOCKING_SESSIONS; above it, this
-- metric owns the finding.
--
-- The one case neither metric could see before is a session that is busy - idle_seconds pinned at
-- 0 because it never stops issuing statements - while holding ONE transaction open for half an
-- hour. Every idle-based rule reads healthy and the locks are held the whole time. That needs the
-- age of the transaction, which is why transaction_begin_time is now carried and graded.

DECLARE @idle_warning_seconds      int = 300;    -- 5 minutes asleep holding a transaction, blocking nobody
DECLARE @idle_critical_seconds     int = 1800;   -- 30 minutes asleep and still holding: nobody is coming back
-- Below this the session is between two statements of its own transaction, not abandoned. One
-- minute is far longer than any AOS round trip and far shorter than a human walking away.
DECLARE @idle_abandoned_seconds    int = 60;
-- A transaction open this long is a problem even if the session is busy every second of it.
DECLARE @tran_age_critical_seconds int = 1800;

;WITH blocked AS
(
    SELECT
        r.session_id,
        r.blocking_session_id,
        CAST(r.wait_time AS bigint) / 1000 AS wait_seconds
    FROM sys.dm_exec_requests AS r
    JOIN sys.dm_exec_sessions AS bs
        ON bs.session_id = r.session_id
    WHERE r.blocking_session_id <> 0
      AND r.blocking_session_id <> r.session_id
      AND bs.is_user_process = 1
),
chain AS
(
    SELECT b.session_id AS victim, b.blocking_session_id AS blocker, b.wait_seconds, 1 AS depth
    FROM blocked AS b

    UNION ALL

    -- The whole downstream tree, not the direct victims: 677 blocked exactly one session, and
    -- that one blocked 85. Counting direct victims scores the root of an outage at 1.
    SELECT c.victim, up.blocking_session_id, c.wait_seconds, c.depth + 1
    FROM chain AS c
    JOIN blocked AS up
        ON up.session_id = c.blocker
    WHERE c.depth < 50
),
impact AS
(
    SELECT
        c.blocker AS session_id,
        COUNT(DISTINCT c.victim) AS blocked_sessions,
        MAX(c.wait_seconds) AS max_wait_seconds
    FROM chain AS c
    GROUP BY c.blocker
)
SELECT
    CAST('SPID=' + CAST(s.session_id AS varchar(20)) AS varchar(256)) AS metric_item,
    CAST(ISNULL(i.blocked_sessions, 0) AS varchar(32)) AS metric_value,
    CAST('blocked_sessions' AS varchar(32)) AS metric_unit,

    CASE
        -- Blocking while genuinely asleep: the session is not working, so the wait cannot end on
        -- its own. The idle floor is what separates this from a live session that merely happens
        -- to hold a lock between two of its own statements.
        WHEN DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) >= @idle_abandoned_seconds
             AND ISNULL(i.blocked_sessions, 0) >= 5 THEN 'CRITICAL'
        WHEN DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) >= @idle_abandoned_seconds
             AND ISNULL(i.max_wait_seconds, 0) >= 60
             AND ISNULL(i.blocked_sessions, 0) > 0 THEN 'CRITICAL'
        -- Busy but holding: idle_seconds never rises because the session keeps issuing
        -- statements, yet one transaction has been open for half an hour and its locks with it.
        -- No idle-based rule can see this.
        WHEN ISNULL(DATEDIFF(SECOND, tr.transaction_begin_time, GETDATE()), 0) >= @tran_age_critical_seconds
             AND ISNULL(i.blocked_sessions, 0) > 0 THEN 'CRITICAL'
        WHEN DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) >= @idle_abandoned_seconds
             AND ISNULL(i.blocked_sessions, 0) > 0 THEN 'WARNING'
        WHEN DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) >= @idle_critical_seconds THEN 'WARNING'
        WHEN DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) >= @idle_warning_seconds THEN 'LOGGING'
        -- Blocking someone with a young transaction and no idle time is a live blocking chain,
        -- which LOCK_BLOCKING_SESSIONS reports. Recording it OK here keeps the evidence without
        -- raising the same incident twice under a name that misdescribes it.
        ELSE 'OK'
    END AS status,

    CAST(
        'Sleeping session with open transaction. '
        + 'spid=' + CAST(s.session_id AS varchar(20))
        + ', blocked_sessions=' + CAST(ISNULL(i.blocked_sessions, 0) AS varchar(20))
        + ', max_wait_seconds=' + CAST(ISNULL(i.max_wait_seconds, 0) AS varchar(20))
        + ', idle_seconds=' + CAST(DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) AS varchar(20))
        -- The field that says whether a busy session is nonetheless sitting on an old
        -- transaction. Reading a row without it cannot tell "mid-TTS block" from "holding since
        -- lunchtime", because both show the same idle_seconds.
        + ', tran_age_seconds=' + ISNULL(CAST(DATEDIFF(SECOND, tr.transaction_begin_time, GETDATE()) AS varchar(20)), '')
        + ', tran_begin=' + ISNULL(CONVERT(varchar(19), tr.transaction_begin_time, 120), '')
        + ', open_tran=' + CAST(s.open_transaction_count AS varchar(20))
        + ', login=' + ISNULL(s.login_name, '')
        + ', host=' + ISNULL(s.host_name, '')
        + ', program=' + ISNULL(s.program_name, '')
        + ', last_request_end=' + ISNULL(CONVERT(varchar(19), s.last_request_end_time, 120), '')
        + ', blocked_session_ids='
        + ISNULL(STUFF((
            SELECT TOP (50) ',' + CAST(v.victim AS varchar(20))
            FROM (SELECT DISTINCT c2.victim FROM chain AS c2 WHERE c2.blocker = s.session_id) AS v
            ORDER BY v.victim
            FOR XML PATH(''), TYPE
          ).value('.', 'nvarchar(max)'), 1, 1, ''), '')
        -- Head AND tail. The query that caused the 2026-08-07 outage was a 732-character AX
        -- SELECT whose first 600 characters are nothing but its column list: taking the head
        -- alone lost 'FROM NUMBERSEQUENCETABLE T1 WITH (UPDLOCK)', which is the whole diagnosis.
        + ', last_sql=' + CASE WHEN LEN(f.flat) > 700
               THEN LEFT(f.flat, 250) + ' ... ' + RIGHT(f.flat, 450)
               ELSE f.flat END
    AS varchar(max)) AS message

FROM sys.dm_exec_sessions AS s
LEFT JOIN impact AS i
    ON i.session_id = s.session_id
-- APPLY with MIN, not a join: dm_tran_session_transactions returns one row per enlisted
-- transaction, so a session in a nested or multi-database transaction would otherwise be
-- duplicated into several metric rows for the same SPID. The oldest is the one holding locks.
OUTER APPLY (
    SELECT MIN(tat.transaction_begin_time) AS transaction_begin_time
    FROM sys.dm_tran_session_transactions AS tst
    JOIN sys.dm_tran_active_transactions AS tat
        ON tat.transaction_id = tst.transaction_id
    WHERE tst.session_id = s.session_id
) AS tr
LEFT JOIN sys.dm_exec_connections AS c
    ON c.session_id = s.session_id
OUTER APPLY sys.dm_exec_sql_text(c.most_recent_sql_handle) AS t
    CROSS APPLY (SELECT flat = REPLACE(REPLACE(REPLACE(ISNULL(t.text, ''),
                                CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' ')) AS f
WHERE s.is_user_process = 1
  AND s.status = 'sleeping'
  AND s.open_transaction_count > 0
  -- A busy AX instance holds hundreds of these at any moment and they are ordinary. Reporting
  -- every one of them is what made the 100-row cap bite and dropped the one that mattered.
  AND (ISNULL(i.blocked_sessions, 0) > 0
       OR DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) >= @idle_warning_seconds)
ORDER BY
    ISNULL(i.blocked_sessions, 0) DESC,
    DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) DESC,
    ISNULL(DATEDIFF(SECOND, tr.transaction_begin_time, GETDATE()), 0) DESC
OPTION (MAXRECURSION 100);
