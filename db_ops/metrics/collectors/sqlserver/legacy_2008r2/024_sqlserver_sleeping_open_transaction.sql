-- The 2008 R2 variant. It carries the client's address like the current one does (0.25.0):
-- host_name is whatever the client says it is, and an application can send any name or none;
-- client_net_address is the address SQL Server saw the connection come from. The view has it on
-- 2008 R2. Without it, 144 of 214 rows on 2026-09-30 - all from the two 2008 R2 servers - named no
-- address (0.26.0 section 1.71).
SELECT
    'SPID=' + CAST(s.session_id AS varchar(20)) AS metric_item,

    CAST(COUNT(st.transaction_id) AS varchar(32)) AS metric_value,

    'transaction(s)' AS metric_unit,

    CASE
        WHEN DATEDIFF(MINUTE, s.last_request_end_time, GETDATE()) >= 365 * 24 * 60
            THEN 'CRITICAL'
        WHEN DATEDIFF(MINUTE, s.last_request_end_time, GETDATE()) >= 1 * 24 * 60
            THEN 'WARNING'
        ELSE 'OK'
    END AS status,

    'Sleeping session with open transaction. '
        + 'login=' + ISNULL(s.login_name, '')
        + ', host=' + ISNULL(s.host_name, '')
        + ', client_ip=' + ISNULL(CAST(c.client_net_address AS varchar(48)), '')
        + ', program=' + ISNULL(s.program_name, '')
        + ', idle_minutes='
        + CAST(DATEDIFF(MINUTE, s.last_request_end_time, GETDATE()) AS varchar(20))
        AS message
FROM sys.dm_exec_sessions s
INNER JOIN sys.dm_tran_session_transactions st
    ON st.session_id = s.session_id
LEFT JOIN sys.dm_exec_connections c
    ON c.session_id = s.session_id
   AND c.parent_connection_id IS NULL   -- one row per session: a MARS session has child connections
WHERE s.is_user_process = 1
  AND s.status = 'sleeping'
GROUP BY
    s.session_id,
    s.login_name,
    s.host_name,
    c.client_net_address,
    s.program_name,
    s.last_request_end_time;