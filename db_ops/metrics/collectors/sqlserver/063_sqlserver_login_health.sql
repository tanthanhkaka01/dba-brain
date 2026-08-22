-- SECURITY_LOGIN_HEALTH (SQL Server): login / account hygiene. WARNING-only by design (never
-- CRITICAL) - these are review signals, not outages. Signals, all per principal:
--   * FAILED_LOGIN  : many failed login attempts in the last 24h (SQL error log) - possible
--                     brute-force or a stale/misconfigured application credential.
--   * LONG_SESSION  : a login whose connection has stayed open a very long time (login_time
--                     older than @long_session_hours) - a login that held a session far too
--                     long, or a leaked/stale connection. Threshold set high so normal app
--                     connection pools do not trip it.
--   * PASSWORD_OLD  : a SQL login whose password has not changed in > @password_max_days
--                     (LOGINPROPERTY 'PasswordLastSetTime'). Windows/AD logins have no local
--                     password age and are skipped (property is NULL).
--   * DORMANT       : a login whose last SUCCESSFUL login (from the error log) is older than
--                     @dormant_days. Best-effort: it only fires when "successful login"
--                     auditing is enabled (Server > Security > Login auditing = "Both ...") and
--                     the event is still within the retained error log; otherwise it stays
--                     silent (no false positives). For reliable dormancy tracking use a SQL
--                     Server Audit that persists login events.
-- Requires VIEW SERVER STATE (sessions), securityadmin / EXECUTE on xp_readerrorlog (error log),
-- and VIEW ANY DEFINITION (principals). A missing right drops only that signal (TRY/CATCH); the
-- metric never fails and never returns CRITICAL.
SET NOCOUNT ON;

DECLARE @failed_threshold  int = 10;   -- >= this many failed logins in 24h => WARNING
DECLARE @long_session_hours int = 72;  -- a login connected longer than this => WARNING
DECLARE @password_max_days int = 180;  -- SQL login password older than this (~6 months) => WARNING
DECLARE @dormant_days      int = 90;   -- no successful login in this many days => WARNING

IF OBJECT_ID('tempdb..#login') IS NOT NULL DROP TABLE #login;
CREATE TABLE #login (kind nvarchar(20), principal nvarchar(300), metric_val nvarchar(64), unit nvarchar(16), detail nvarchar(1000));

-- LONG_SESSION: logins whose oldest open connection exceeds the threshold.
BEGIN TRY
    INSERT INTO #login (kind, principal, metric_val, unit, detail)
    SELECT 'LONG_SESSION',
           ISNULL(NULLIF(s.login_name, N''), N'<none>'),
           CAST(MAX(DATEDIFF(hour, s.login_time, GETDATE())) AS nvarchar(12)), 'hours',
           N'sessions=' + CAST(COUNT(*) AS nvarchar(12))
           + N' max_connected_hours=' + CAST(MAX(DATEDIFF(hour, s.login_time, GETDATE())) AS nvarchar(12))
           + N' (threshold=' + CAST(@long_session_hours AS nvarchar(12)) + N'h)'
    FROM sys.dm_exec_sessions AS s
    WHERE s.is_user_process = 1
      AND s.login_time < DATEADD(hour, -@long_session_hours, GETDATE())
    GROUP BY ISNULL(NULLIF(s.login_name, N''), N'<none>');
END TRY BEGIN CATCH END CATCH;

-- PASSWORD_OLD: SQL logins whose password has not changed in > @password_max_days.
BEGIN TRY
    INSERT INTO #login (kind, principal, metric_val, unit, detail)
    SELECT 'PASSWORD_OLD', sp.name,
           CAST(DATEDIFF(day, CONVERT(datetime, LOGINPROPERTY(sp.name, 'PasswordLastSetTime')), GETDATE()) AS nvarchar(12)), 'days',
           N'password_age_days=' + CAST(DATEDIFF(day, CONVERT(datetime, LOGINPROPERTY(sp.name, 'PasswordLastSetTime')), GETDATE()) AS nvarchar(12))
           + N' last_set=' + CONVERT(varchar(10), CONVERT(datetime, LOGINPROPERTY(sp.name, 'PasswordLastSetTime')), 120)
           + N' (threshold=' + CAST(@password_max_days AS nvarchar(12)) + N'd)'
    FROM sys.server_principals AS sp
    WHERE sp.type = 'S'              -- SQL logins only
      AND sp.is_disabled = 0
      AND sp.name NOT LIKE '##%'
      AND LOGINPROPERTY(sp.name, 'PasswordLastSetTime') IS NOT NULL
      AND DATEDIFF(day, CONVERT(datetime, LOGINPROPERTY(sp.name, 'PasswordLastSetTime')), GETDATE()) > @password_max_days;
END TRY BEGIN CATCH END CATCH;

-- FAILED_LOGIN: principals with many failed attempts in the last 24h.
BEGIN TRY
    IF OBJECT_ID('tempdb..#elf') IS NOT NULL DROP TABLE #elf;
    CREATE TABLE #elf (LogDate datetime, ProcessInfo nvarchar(100), LogText nvarchar(4000));
    INSERT INTO #elf (LogDate, ProcessInfo, LogText) EXEC sys.xp_readerrorlog 0, 1, N'Login failed';

    INSERT INTO #login (kind, principal, metric_val, unit, detail)
    SELECT 'FAILED_LOGIN', g.login_name, CAST(g.cnt AS nvarchar(12)), 'count',
           N'failed_attempts_24h=' + CAST(g.cnt AS nvarchar(12))
           + N' (threshold=' + CAST(@failed_threshold AS nvarchar(12)) + N')'
    FROM (
        SELECT x.login_name, COUNT(*) AS cnt
        FROM (
            SELECT ISNULL(NULLIF(LTRIM(RTRIM(SUBSTRING(
                       LogText, CHARINDEX('''', LogText) + 1,
                       NULLIF(CHARINDEX('''', LogText, CHARINDEX('''', LogText) + 1), 0) - CHARINDEX('''', LogText) - 1
                   ))), N''), N'<unparsed>') AS login_name
            FROM #elf
            WHERE LogDate >= DATEADD(hour, -24, GETDATE()) AND LogText LIKE '%Login failed%'
        ) AS x
        GROUP BY x.login_name
    ) AS g
    WHERE g.cnt >= @failed_threshold;
END TRY BEGIN CATCH END CATCH;

-- DORMANT: last successful login older than @dormant_days (needs successful-login auditing).
BEGIN TRY
    IF OBJECT_ID('tempdb..#els') IS NOT NULL DROP TABLE #els;
    CREATE TABLE #els (LogDate datetime, ProcessInfo nvarchar(100), LogText nvarchar(4000));
    INSERT INTO #els (LogDate, ProcessInfo, LogText) EXEC sys.xp_readerrorlog 0, 1, N'Login succeeded';

    INSERT INTO #login (kind, principal, metric_val, unit, detail)
    SELECT 'DORMANT', g.login_name,
           CAST(DATEDIFF(day, g.last_success, GETDATE()) AS nvarchar(12)), 'days',
           N'last_login=' + CONVERT(varchar(19), g.last_success, 120)
           + N' days_since=' + CAST(DATEDIFF(day, g.last_success, GETDATE()) AS nvarchar(12))
           + N' (threshold=' + CAST(@dormant_days AS nvarchar(12)) + N'd; needs successful-login audit)'
    FROM (
        SELECT x.login_name, MAX(x.LogDate) AS last_success
        FROM (
            SELECT LogDate, ISNULL(NULLIF(LTRIM(RTRIM(SUBSTRING(
                       LogText, CHARINDEX('''', LogText) + 1,
                       NULLIF(CHARINDEX('''', LogText, CHARINDEX('''', LogText) + 1), 0) - CHARINDEX('''', LogText) - 1
                   ))), N''), N'<unparsed>') AS login_name
            FROM #els
            WHERE LogText LIKE '%Login succeeded%'
        ) AS x
        GROUP BY x.login_name
    ) AS g
    WHERE g.last_success < DATEADD(day, -@dormant_days, GETDATE());
END TRY BEGIN CATCH END CATCH;

-- Output: the always-present summary carries the alert; the per-login rows are LOGGING detail.
-- One warning per login made a single finding ("26 SQL logins have a password older than 180
-- days") arrive as 26 separate warnings, which is 26 things to read and one thing to do.
-- The detail rows are still collected, so naming the logins is one query away.
-- Summary sorts FIRST: it is the row that warns, so it is the row that must survive the cap.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        CAST(LOWER(l.kind) + N'\' + l.principal AS varchar(400)) AS metric_item,
        CAST(l.metric_val AS varchar(64)) AS metric_value,
        CAST(l.unit AS varchar(32)) AS metric_unit,
        CAST('LOGGING' AS varchar(16)) AS status,
        CAST(l.detail AS varchar(1000)) AS message,
        1 AS sort_rank
    FROM #login AS l

    UNION ALL

    SELECT
        CAST('login_health :: summary' AS varchar(400)) AS metric_item,
        CAST(CAST((SELECT COUNT(*) FROM #login) AS varchar(12)) AS varchar(64)) AS metric_value,
        CAST('count' AS varchar(32)) AS metric_unit,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM #login) THEN 'WARNING' ELSE 'OK' END AS varchar(16)) AS status,
        CAST(N'failed=' + CAST((SELECT COUNT(*) FROM #login WHERE kind = 'FAILED_LOGIN') AS nvarchar(12))
             + N' long_session=' + CAST((SELECT COUNT(*) FROM #login WHERE kind = 'LONG_SESSION') AS nvarchar(12))
             + N' password_old=' + CAST((SELECT COUNT(*) FROM #login WHERE kind = 'PASSWORD_OLD') AS nvarchar(12))
             + N' dormant=' + CAST((SELECT COUNT(*) FROM #login WHERE kind = 'DORMANT') AS nvarchar(12)) AS varchar(1000)) AS message,
        0 AS sort_rank
) AS q
ORDER BY q.sort_rank, q.metric_item;

IF OBJECT_ID('tempdb..#login') IS NOT NULL DROP TABLE #login;
IF OBJECT_ID('tempdb..#elf')   IS NOT NULL DROP TABLE #elf;
IF OBJECT_ID('tempdb..#els')   IS NOT NULL DROP TABLE #els;
