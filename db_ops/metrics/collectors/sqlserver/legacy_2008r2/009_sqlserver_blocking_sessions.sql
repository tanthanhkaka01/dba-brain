-- The 2008 R2 variant: blocking, reported by the session at the HEAD of the chain, as the current
-- file reports it (its header says why a count per database is an alarm with nothing to act on).
--
-- Until 0.26.0 this file still counted blocked sessions per database and named no session, so on
-- the two 2008 R2 servers a blocking alert said where it hurt and not what to do - no head
-- blocker, no login, no host, no client address (0.26.0 section 1.71).
--
-- What differs from the current file: sys.dm_exec_sessions has no open_transaction_count before
-- SQL Server 2012, so open_tran is counted from sys.dm_tran_session_transactions, as this folder's
-- 024 does; and the connection is joined on parent_connection_id IS NULL, as 024 does, so a MARS
-- session gives one row. Everything else here exists on 10.50: the recursive CTE, FOR XML PATH,
-- sys.dm_exec_connections with its client_net_address and most_recent_sql_handle.

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
    -- A head is a blocker that is not itself blocked. Everything else is a link.
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
        + ', database=' + ISNULL(DB_NAME(h.any_database_id), 'server') COLLATE DATABASE_DEFAULT
        -- A head blocker that is 'sleeping' has finished its work and walked away holding a
        -- transaction, and no query text will explain it.
        + ', session_status=' + ISNULL(s.status, '')
        + ', open_tran=' + CAST((SELECT COUNT(*) FROM sys.dm_tran_session_transactions AS st
                                 WHERE st.session_id = h.head_session_id) AS varchar(20))
        + ', login=' + ISNULL(s.login_name, '')
        + ', host=' + ISNULL(s.host_name, '')
        -- host_name is whatever the client says it is; client_net_address is the address SQL
        -- Server saw the connection come from.
        + ', client_ip=' + ISNULL(CAST(c.client_net_address AS varchar(48)), '')
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
        -- Head and tail: a long SELECT's first 600 characters are its column list, and the table
        -- plus its lock hint - the part that names the contention - is at the end.
        + ', last_or_running_sql=' + CASE WHEN LEN(f.flat) > 700
               THEN LEFT(f.flat, 250) + ' ... ' + RIGHT(f.flat, 450)
               ELSE f.flat END
    AS varchar(max)) AS message

FROM heads AS h
LEFT JOIN sys.dm_exec_sessions AS s
    ON s.session_id = h.head_session_id
LEFT JOIN sys.dm_exec_connections AS c
    ON c.session_id = h.head_session_id
   AND c.parent_connection_id IS NULL   -- one row per session: a MARS session has child connections
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
