-- SECURITY_CERTIFICATE_EXPIRY (SQL Server): certificate inventory + expiry across the instance.
-- Covers the master store (server/TDE/backup-encryption certificates) and each user database's
-- certificates. An expired or soon-to-expire certificate is a real operational risk: an expired
-- TDE/backup certificate can block restores. Status: CRITICAL if already expired, WARNING if it
-- expires within 30 days, OK otherwise. Internal (##...##) certificates are excluded.
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#certs') IS NOT NULL DROP TABLE #certs;
CREATE TABLE #certs (scope sysname, cert_name sysname, start_date datetime NULL, expiry datetime NULL);

-- master store (server-level + TDE + backup-encryption certificates)
BEGIN TRY
    INSERT INTO #certs (scope, cert_name, start_date, expiry)
    SELECT N'master', c.name, c.start_date, c.expiry_date
    FROM master.sys.certificates AS c
    WHERE c.name NOT LIKE '##%';
END TRY BEGIN CATCH END CATCH;

DECLARE @db sysname, @sql nvarchar(max);
DECLARE db_cursor CURSOR LOCAL FAST_FORWARD FOR
    SELECT d.name FROM sys.databases AS d
    WHERE d.database_id > 4 AND d.state = 0 AND d.source_database_id IS NULL AND HAS_DBACCESS(d.name) = 1
    ORDER BY d.name;
OPEN db_cursor; FETCH NEXT FROM db_cursor INTO @db;
WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'USE ' + QUOTENAME(@db) + N';
        INSERT INTO #certs (scope, cert_name, start_date, expiry)
        SELECT DB_NAME(), c.name, c.start_date, c.expiry_date
        FROM sys.certificates AS c WHERE c.name NOT LIKE ''##%'';';
    BEGIN TRY EXEC sys.sp_executesql @sql; END TRY BEGIN CATCH END CATCH;
    FETCH NEXT FROM db_cursor INTO @db;
END
CLOSE db_cursor; DEALLOCATE db_cursor;

SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        CAST(c.scope + N'\' + c.cert_name AS varchar(400)) AS metric_item,
        CAST(CASE WHEN c.expiry IS NULL THEN 'no-expiry' ELSE CONVERT(varchar(10), c.expiry, 120) END AS varchar(64)) AS metric_value,
        CAST('date' AS varchar(32)) AS metric_unit,
        CAST(CASE WHEN c.expiry IS NULL THEN 'OK'
                  WHEN c.expiry < GETDATE() THEN 'CRITICAL'
                  WHEN c.expiry < DATEADD(day, 30, GETDATE()) THEN 'WARNING'
                  ELSE 'OK' END AS varchar(16)) AS status,
        CAST(N'days_to_expiry=' + ISNULL(CAST(DATEDIFF(day, GETDATE(), c.expiry) AS nvarchar(12)), N'n/a')
             + N' start=' + ISNULL(CONVERT(varchar(10), c.start_date, 120), N'?') AS varchar(1000)) AS message,
        CASE WHEN c.expiry < GETDATE() THEN 0
             WHEN c.expiry < DATEADD(day, 30, GETDATE()) THEN 1 ELSE 2 END AS sort_rank
    FROM #certs AS c

    UNION ALL

    SELECT
        CAST('certificates :: summary' AS varchar(400)) AS metric_item,
        CAST(CAST((SELECT COUNT(*) FROM #certs) AS varchar(12)) AS varchar(64)) AS metric_value,
        CAST('count' AS varchar(32)) AS metric_unit,
        CAST(CASE WHEN EXISTS (SELECT 1 FROM #certs WHERE expiry < GETDATE()) THEN 'CRITICAL'
                  WHEN EXISTS (SELECT 1 FROM #certs WHERE expiry < DATEADD(day, 30, GETDATE())) THEN 'WARNING'
                  ELSE 'OK' END AS varchar(16)) AS status,
        CAST(N'expired=' + CAST((SELECT COUNT(*) FROM #certs WHERE expiry < GETDATE()) AS nvarchar(12))
             + N' expiring_30d=' + CAST((SELECT COUNT(*) FROM #certs WHERE expiry >= GETDATE() AND expiry < DATEADD(day, 30, GETDATE())) AS nvarchar(12)) AS varchar(1000)) AS message,
        3 AS sort_rank
) AS q
ORDER BY q.sort_rank, q.metric_item;

IF OBJECT_ID('tempdb..#certs') IS NOT NULL DROP TABLE #certs;
