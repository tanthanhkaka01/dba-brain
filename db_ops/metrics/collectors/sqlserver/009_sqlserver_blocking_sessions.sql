-- Blocking, reported by the session at the HEAD of the chain - the one worth killing.
--
-- This used to group by database and emit "database=SALESDB, blocked_sessions=89": a correct alarm
-- carrying nothing to act on. On 2026-08-07 09:16 that is exactly what fired, and the operator
-- still had to find by hand that 89 sessions waited on 403, which waited on 677, which was asleep.
-- Naming the database tells you where it hurts; naming the head blocker tells you what to do.
--
-- The chain is walked recursively because the head is rarely the direct blocker. 677 blocked one
-- session. Counting direct victims would have scored it 1 and ranked it below every mid-chain
-- session - the least interesting number in the incident.

;WITH blocked AS
(
    SELECT
        r.session_id,
        r.blocking_session_id,
        CAST(r.wait_time AS bigint) / 1000 AS wait_seconds,
        r.database_id
    FROM sys.dm_exec_requests AS r
    JOIN sys.dm_exec_sessions AS s
        ON s.session_id = r.session_id
    WHERE r.blocking_session_id <> 0
      AND r.blocking_session_id <> r.session_id   -- self-blocking is a parallelism artefact
      AND s.is_user_process = 1
),
chain AS
(
    SELECT
        b.session_id AS victim,
        b.blocking_session_id AS blocker,
        b.wait_seconds,
        b.database_id,
        1 AS depth
    FROM blocked AS b

    UNION ALL

    -- Walk up: the blocker of my blocker is also, transitively, blocking me.
    SELECT
        c.victim,
        up.blocking_session_id,
        c.wait_seconds,
        c.database_id,
        c.depth + 1
    FROM chain AS c
    JOIN blocked AS up
        ON up.session_id = c.blocker
    WHERE c.depth < 50
),
heads AS
(
    SELECT
        c.blocker AS head_session_id,
        COUNT(DISTINCT c.victim) AS blocked_sessions,
        MAX(c.wait_seconds) AS max_wait_seconds,
        MAX(c.depth) AS chain_depth,
        MIN(c.database_id) AS any_database_id
    FROM chain AS c
    -- A head is a blocker that is not itself blocked. Everything else is a link, and reporting
    -- links is what buried the real one in 28 message parts.
    WHERE NOT EXISTS (SELECT 1 FROM blocked AS b2 WHERE b2.session_id = c.blocker)
    GROUP BY c.blocker
)
SELECT
    CAST('SPID=' + CAST(h.head_session_id AS varchar(20)) AS varchar(256)) AS metric_item,
    CAST(h.blocked_sessions AS varchar(32)) AS metric_value,
    CAST('blocked_sessions' AS varchar(32)) AS metric_unit,

    CASE
        WHEN h.blocked_sessions >= 10 THEN 'CRITICAL'
        WHEN h.max_wait_seconds >= 300 THEN 'CRITICAL'
        WHEN h.blocked_sessions > 0 THEN 'WARNING'
        ELSE 'OK'
    END AS status,

    CAST(
        'Head blocker. '
        + 'spid=' + CAST(h.head_session_id AS varchar(20))
        + ', blocked_sessions=' + CAST(h.blocked_sessions AS varchar(20))
        + ', max_wait_seconds=' + CAST(h.max_wait_seconds AS varchar(20))
        + ', chain_depth=' + CAST(h.chain_depth AS varchar(20))
        + ', database=' + ISNULL(DB_NAME(h.any_database_id), 'server')
        -- session_status is the field that mattered: a head blocker that is 'sleeping' has
        -- finished its work and walked away holding a transaction, and no query text will
        -- explain it. That is what 677 was.
        + ', session_status=' + ISNULL(s.status, '')
        + ', open_tran=' + ISNULL(CAST(s.open_transaction_count AS varchar(20)), '')
        + ', login=' + ISNULL(s.login_name, '')
        + ', host=' + ISNULL(s.host_name, '')
        + ', program=' + ISNULL(s.program_name, '')
        + ', last_request_end=' + ISNULL(CONVERT(varchar(19), s.last_request_end_time, 120), '')
        + ', idle_seconds=' + ISNULL(CAST(DATEDIFF(SECOND, s.last_request_end_time, GETDATE()) AS varchar(20)), '')
        + ', blocked_session_ids='
        + ISNULL(STUFF((
            SELECT TOP (50) ',' + CAST(v.victim AS varchar(20))
            FROM (SELECT DISTINCT c2.victim FROM chain AS c2 WHERE c2.blocker = h.head_session_id) AS v
            ORDER BY v.victim
            FOR XML PATH(''), TYPE
          ).value('.', 'nvarchar(max)'), 1, 1, ''), '')
        -- Head and tail: an AX SELECT's first 600 characters are its column list, and the
        -- table plus its lock hint - the part that names the contention - is at the end.
        + ', last_or_running_sql=' + CASE WHEN LEN(f.flat) > 700
               THEN LEFT(f.flat, 250) + ' ... ' + RIGHT(f.flat, 450)
               ELSE f.flat END
    AS varchar(max)) AS message

FROM heads AS h
LEFT JOIN sys.dm_exec_sessions AS s
    ON s.session_id = h.head_session_id
LEFT JOIN sys.dm_exec_connections AS c
    ON c.session_id = h.head_session_id
LEFT JOIN sys.dm_exec_requests AS r
    ON r.session_id = h.head_session_id
OUTER APPLY sys.dm_exec_sql_text(ISNULL(r.sql_handle, c.most_recent_sql_handle)) AS t
    CROSS APPLY (SELECT flat = REPLACE(REPLACE(REPLACE(ISNULL(t.text, ''),
                                CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' ')) AS f

UNION ALL

SELECT
    CAST('server' AS varchar(256)) AS metric_item,
    CAST('0' AS varchar(32)) AS metric_value,
    CAST('blocked_sessions' AS varchar(32)) AS metric_unit,
    'OK' AS status,
    CAST('No blocking sessions found.' AS varchar(max)) AS message
WHERE NOT EXISTS
(
    SELECT 1
    FROM sys.dm_exec_requests AS r2
    JOIN sys.dm_exec_sessions AS s2
        ON s2.session_id = r2.session_id
    WHERE r2.blocking_session_id <> 0
      AND r2.blocking_session_id <> r2.session_id
      AND s2.is_user_process = 1
)
OPTION (MAXRECURSION 100);
