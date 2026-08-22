;WITH long_requests AS
(
    SELECT
        r.session_id,
        COALESCE(DB_NAME(r.database_id), 'server') COLLATE DATABASE_DEFAULT AS database_name,
        r.status AS request_status,
        r.command,
        CAST(r.total_elapsed_time AS bigint) / 1000 AS elapsed_seconds,
        CAST(r.cpu_time AS bigint) / 1000 AS cpu_seconds,
        r.reads,
        r.writes,
        r.logical_reads,
        r.wait_type,
        r.blocking_session_id,
        s.login_name,
        s.host_name,
        s.program_name
    FROM sys.dm_exec_requests AS r
    JOIN sys.dm_exec_sessions AS s
        ON r.session_id = s.session_id
    WHERE r.session_id <> @@SPID
      AND s.is_user_process = 1
      AND r.total_elapsed_time >= 300000
      AND NOT (
          COALESCE(DB_NAME(r.database_id), 'server') = 'master'
          AND r.command = 'EXECUTE'
          AND ISNULL(r.wait_type, '') = 'SP_SERVER_DIAGNOSTICS_SLEEP'
      )
),
ranked AS
(
    SELECT
        *,
        ROW_NUMBER() OVER
        (
            PARTITION BY database_name
            ORDER BY elapsed_seconds DESC, cpu_seconds DESC, logical_reads DESC
        ) AS rn
    FROM long_requests
)
SELECT
    CAST(database_name AS varchar(256)) AS metric_item,
    CAST(MAX(elapsed_seconds) AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    CASE
        WHEN MAX(elapsed_seconds) >= 3600 THEN 'CRITICAL'
        WHEN MAX(elapsed_seconds) >= 900 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'running_requests=' + CAST(COUNT(*) AS varchar(32))
        + ', max_elapsed_seconds=' + CAST(MAX(elapsed_seconds) AS varchar(32))
        + ', top_session_id=' + ISNULL(CAST(MAX(CASE WHEN rn = 1 THEN session_id END) AS varchar(32)), '')
        + ', top_status=' + ISNULL(MAX(CASE WHEN rn = 1 THEN request_status END), '')
        + ', command=' + ISNULL(MAX(CASE WHEN rn = 1 THEN command END), '')
        + ', wait_type=' + ISNULL(MAX(CASE WHEN rn = 1 THEN wait_type END), '')
        + ', blocking_session_id=' + ISNULL(CAST(MAX(CASE WHEN rn = 1 THEN blocking_session_id END) AS varchar(32)), '')
        + ', cpu_seconds=' + ISNULL(CAST(MAX(CASE WHEN rn = 1 THEN cpu_seconds END) AS varchar(32)), '')
        + ', reads=' + ISNULL(CAST(MAX(CASE WHEN rn = 1 THEN reads END) AS varchar(32)), '')
        + ', writes=' + ISNULL(CAST(MAX(CASE WHEN rn = 1 THEN writes END) AS varchar(32)), '')
        + ', logical_reads=' + ISNULL(CAST(MAX(CASE WHEN rn = 1 THEN logical_reads END) AS varchar(32)), '')
        + ', login=' + ISNULL(MAX(CASE WHEN rn = 1 THEN login_name END), '')
        + ', host=' + ISNULL(MAX(CASE WHEN rn = 1 THEN host_name END), '')
        + ', app=' + ISNULL(MAX(CASE WHEN rn = 1 THEN program_name END), '') AS message
FROM ranked
GROUP BY database_name

UNION ALL

SELECT
    CAST('server' AS varchar(256)) AS metric_item,
    CAST('0' AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    'OK' AS status,
    'No user request running longer than 300 seconds.' AS message
WHERE NOT EXISTS
(
    SELECT 1
    FROM long_requests
);

-- -- if have critical then check detail
-- SELECT
--     r.session_id,
--     s.login_name,
--     s.host_name,
--     s.program_name,
--     c.client_net_address AS client_ip,
--     c.local_net_address AS server_ip,
--     s.status AS session_status,
--     r.status AS request_status,
--     r.command,
--     r.start_time,
--     DATEDIFF(SECOND, r.start_time, GETDATE()) AS elapsed_seconds,
--     r.cpu_time / 1000 AS cpu_seconds,
--     r.reads,
--     r.writes,
--     r.logical_reads,
--     r.wait_type,
--     r.blocking_session_id,
--     DB_NAME(r.database_id) AS database_name,
--     OBJECT_NAME(t.objectid, t.dbid) AS object_name_from_sql_text,
--     t.text AS full_sql_text,
--     SUBSTRING(
--         t.text,
--         (r.statement_start_offset / 2) + 1,
--         (
--             CASE r.statement_end_offset
--                 WHEN -1 THEN DATALENGTH(t.text)
--                 ELSE r.statement_end_offset
--             END - r.statement_start_offset
--         ) / 2 + 1
--     ) AS running_statement,
--     qp.query_plan
-- FROM sys.dm_exec_requests AS r
-- JOIN sys.dm_exec_sessions AS s
--     ON r.session_id = s.session_id
-- LEFT JOIN sys.dm_exec_connections c
--     ON r.connection_id = c.connection_id
-- OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
-- OUTER APPLY sys.dm_exec_query_plan(r.plan_handle) qp
-- WHERE r.session_id <> @@SPID
--   AND s.is_user_process = 1
--   AND r.total_elapsed_time >= 300000
--   AND NOT (
--       COALESCE(DB_NAME(r.database_id), 'server') = 'master'
--       AND r.command = 'EXECUTE'
--       AND ISNULL(r.wait_type, '') = 'SP_SERVER_DIAGNOSTICS_SLEEP'
--   );