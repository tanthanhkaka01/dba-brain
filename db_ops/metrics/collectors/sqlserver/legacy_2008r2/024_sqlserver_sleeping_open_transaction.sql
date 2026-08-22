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
        + ', program=' + ISNULL(s.program_name, '')
        + ', idle_minutes='
        + CAST(DATEDIFF(MINUTE, s.last_request_end_time, GETDATE()) AS varchar(20))
        AS message
FROM sys.dm_exec_sessions s
INNER JOIN sys.dm_tran_session_transactions st
    ON st.session_id = s.session_id
WHERE s.is_user_process = 1
  AND s.status = 'sleeping'
GROUP BY
    s.session_id,
    s.login_name,
    s.host_name,
    s.program_name,
    s.last_request_end_time;