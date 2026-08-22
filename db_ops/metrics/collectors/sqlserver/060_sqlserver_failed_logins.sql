-- SECURITY_FAILED_LOGINS (SQL Server): failed login attempts in the last 24 hours.
-- Reads the current SQL error log via xp_readerrorlog filtered to 'Login failed'. Requires the
-- monitor login to have securityadmin (or EXECUTE on xp_readerrorlog); when it does not, one OK
-- row explains that rather than failing the metric. Emits a per-login count plus a 24h summary.
-- WARNING when the total in the window is high (possible brute-force / misconfigured app).
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#el') IS NOT NULL DROP TABLE #el;
CREATE TABLE #el (LogDate datetime, ProcessInfo nvarchar(100), LogText nvarchar(4000));

DECLARE @ok bit = 1, @err nvarchar(2000) = NULL;
BEGIN TRY
    INSERT INTO #el (LogDate, ProcessInfo, LogText)
    EXEC sys.xp_readerrorlog 0, 1, N'Login failed';
END TRY
BEGIN CATCH
    SET @ok = 0;
    SET @err = ERROR_MESSAGE();
END CATCH;

IF @ok = 0
BEGIN
    SELECT
        CAST('failed_logins :: 24h' AS varchar(400)) AS metric_item,
        CAST('unknown' AS varchar(64)) AS metric_value,
        CAST('count' AS varchar(32)) AS metric_unit,
        CAST('OK' AS varchar(16)) AS status,
        CAST(LEFT(N'error log not readable by the monitor login (grant securityadmin or EXECUTE on xp_readerrorlog): '
             + ISNULL(@err, N''), 1000) AS varchar(1000)) AS message;
END
ELSE
BEGIN
    ;WITH recent AS (
        SELECT
            LTRIM(RTRIM(SUBSTRING(
                LogText,
                CHARINDEX('''', LogText) + 1,
                NULLIF(CHARINDEX('''', LogText, CHARINDEX('''', LogText) + 1), 0) - CHARINDEX('''', LogText) - 1
            ))) AS login_name
        FROM #el
        WHERE LogDate >= DATEADD(hour, -24, GETDATE())
          AND LogText LIKE '%Login failed%'
    ),
    agg AS (
        SELECT ISNULL(NULLIF(login_name, ''), '<unparsed>') AS login_name, COUNT(*) AS cnt
        FROM recent GROUP BY ISNULL(NULLIF(login_name, ''), '<unparsed>')
    )
    SELECT metric_item, metric_value, metric_unit, status, message
    FROM (
        SELECT
            CAST('failed_login\' + a.login_name AS varchar(400)) AS metric_item,
            CAST(CAST(a.cnt AS varchar(12)) AS varchar(64)) AS metric_value,
            CAST('count' AS varchar(32)) AS metric_unit,
            CAST(CASE WHEN a.cnt >= 20 THEN 'WARNING' ELSE 'OK' END AS varchar(16)) AS status,
            CAST(N'failed login attempts for this principal in the last 24h' AS varchar(1000)) AS message,
            CASE WHEN a.cnt >= 20 THEN 0 ELSE 1 END AS sort_rank
        FROM agg AS a

        UNION ALL

        SELECT
            CAST('failed_logins :: 24h' AS varchar(400)) AS metric_item,
            CAST(CAST((SELECT ISNULL(SUM(cnt), 0) FROM agg) AS varchar(12)) AS varchar(64)) AS metric_value,
            CAST('count' AS varchar(32)) AS metric_unit,
            CAST(CASE WHEN (SELECT ISNULL(SUM(cnt), 0) FROM agg) >= 20 THEN 'WARNING' ELSE 'OK' END AS varchar(16)) AS status,
            CAST(N'distinct_logins=' + CAST((SELECT COUNT(*) FROM agg) AS nvarchar(12))
                 + N' total_failed_24h=' + CAST((SELECT ISNULL(SUM(cnt), 0) FROM agg) AS nvarchar(12)) AS varchar(1000)) AS message,
            2 AS sort_rank
    ) AS q
    ORDER BY q.sort_rank, q.metric_item;
END

IF OBJECT_ID('tempdb..#el') IS NOT NULL DROP TABLE #el;
