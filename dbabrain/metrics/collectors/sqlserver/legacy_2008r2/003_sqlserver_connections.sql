-- INSTANCE_CONNECTIONS (SQL Server 2008 / 2008 R2): active user sessions, running requests, blocking,
-- and connection utilization against configured 'user connections'.
--
-- Why connection utilization is checked against sys.configurations:
-- If an administrator sets 'user connections' (sp_configure) to bound connection pools or protect
-- against connection storms, reaching that ceiling rejects new logins with error 17809.
-- When 'user connections' is 0, the engine defaults to its architectural ceiling (32767), meaning
-- there is no administrative ceiling configured; the ratio is recorded at LOGGING rather than
-- alerting. When a ceiling is configured (> 0), utilization is graded: >= 90% is CRITICAL and
-- >= 80% is WARNING.
--
-- The collector emits both:
-- 1. 'server': active user process sessions, running requests, and blocking sessions (retaining
--    today's behavior: blocked > 0 is WARNING).
-- 2. 'connection_limit_ratio': the ratio of active sessions to the configured limit, graded when
--    configured and LOGGING when 0.
DECLARE @connection_limit_critical_pct int = 90;
DECLARE @connection_limit_warning_pct int = 80;

WITH config_val AS (
    SELECT ISNULL((SELECT CAST(value_in_use AS int) FROM sys.configurations WHERE name = 'user connections'), 0) AS configured_limit
),
session_stats AS (
    SELECT
        COUNT(DISTINCT s.session_id) AS active_sessions,
        COUNT(DISTINCT r.session_id) AS running_requests,
        COALESCE(SUM(CASE WHEN r.blocking_session_id <> 0 THEN 1 ELSE 0 END), 0) AS blocked_sessions
    FROM sys.dm_exec_sessions AS s
    LEFT JOIN sys.dm_exec_requests AS r
        ON r.session_id = s.session_id
    WHERE s.is_user_process = 1
)
SELECT
    CAST('server' AS varchar(256)) AS metric_item,
    CAST(st.active_sessions AS varchar(32)) AS metric_value,
    CAST('sessions' AS varchar(32)) AS metric_unit,
    CASE
        -- The ceiling is graded on connection_limit_ratio alone: graded here too, one breach paged twice.
        WHEN st.blocked_sessions > 0
        THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'active_sessions=' + CAST(st.active_sessions AS varchar(32))
        + ', running_requests=' + CAST(st.running_requests AS varchar(32))
        + ', blocked_sessions=' + CAST(st.blocked_sessions AS varchar(32))
        + ', configured_limit=' + CAST(cfg.configured_limit AS varchar(32))
        + ', limit_used_pct=' + CASE
            WHEN cfg.configured_limit > 0
            THEN CAST(CAST(st.active_sessions * 100.0 / cfg.configured_limit AS decimal(10,2)) AS varchar(32))
            ELSE 'unlimited'
        END AS message
FROM session_stats AS st
CROSS JOIN config_val AS cfg

UNION ALL

SELECT
    CAST('connection_limit_ratio' AS varchar(256)) AS metric_item,
    CASE
        WHEN cfg.configured_limit > 0
        THEN CAST(CAST(st.active_sessions * 100.0 / cfg.configured_limit AS decimal(10,2)) AS varchar(32))
        ELSE '0'
    END AS metric_value,
    CAST('pct' AS varchar(32)) AS metric_unit,
    CASE
        WHEN cfg.configured_limit = 0
        THEN 'LOGGING'
        WHEN (st.active_sessions * 100.0 / cfg.configured_limit) >= @connection_limit_critical_pct
        THEN 'CRITICAL'
        WHEN (st.active_sessions * 100.0 / cfg.configured_limit) >= @connection_limit_warning_pct
        THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'configured_limit=' + CAST(cfg.configured_limit AS varchar(32))
        + ', active_sessions=' + CAST(st.active_sessions AS varchar(32))
        + ', limit_used_pct=' + CASE
            WHEN cfg.configured_limit > 0
            THEN CAST(CAST(st.active_sessions * 100.0 / cfg.configured_limit AS decimal(10,2)) AS varchar(32))
            ELSE '0'
        END
        + ', note=' + CASE
            WHEN cfg.configured_limit = 0
            THEN 'no configured ceiling (engine max 32767); ratio is not graded'
            ELSE 'graded against configured user connections limit'
        END AS message
FROM session_stats AS st
CROSS JOIN config_val AS cfg;
