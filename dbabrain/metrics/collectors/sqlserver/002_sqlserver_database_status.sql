-- DATABASE_STATUS (SQL Server 2012+): state of every database, plus SQL Server Agent service state.
--
-- Why system databases (master, tempdb, model, msdb) are included:
-- System databases were previously omitted (database_id > 4) under the assumption that
-- DATABASE_CONFIG reported them. However, DATABASE_CONFIG only inventories configuration and does
-- not evaluate health state. Leaving them out meant an offline, suspect, or recovery-pending msdb,
-- model, or tempdb would page nothing. They are now graded with the same rule user databases have:
-- not ONLINE = CRITICAL.
--
-- Why SQL Server Agent is graded:
-- Targets monitored without host-level cmd_access have no OS_SERVICE_STATUS checks. An Agent
-- service that is not 'Running' halts scheduled jobs, backups, and maintenance without alerting.
-- Express edition (EngineEdition = 4) does not include SQL Server Agent by design; an absent Agent
-- there is an expected state rather than an outage and is reported at LOGGING.
--
-- Why a stopped Agent on Linux is OK when nothing is scheduled:
-- SQL Server on Linux starts Agent only when mssql-conf sets sqlagent.enabled, and the default is
-- off. sys.dm_server_services still lists the service there, as 'Stopped' with a startup type of
-- 'Automatic' that means nothing on Linux, so the Windows rule paged CRITICAL on every Linux or
-- Docker instance that never asked for Agent (the .251 lab, 2026-10-15). What the OS leaves out is
-- not an outage (the operator, 2026-10-15). It is one once a job is waiting for it: an enabled job
-- with an enabled schedule on a Linux instance whose Agent is not running will never run, which is
-- exactly what this row exists to catch - so that case stays CRITICAL, as on Windows. A login that
-- cannot read msdb cannot tell; the row is then OK with the count unreadable, the OS default.
DECLARE @status TABLE (
    metric_item varchar(256),
    metric_value varchar(32),
    metric_unit varchar(32),
    status varchar(32),
    message varchar(max),
    sort_order int IDENTITY(1,1)
);

INSERT INTO @status (metric_item, metric_value, metric_unit, status, message)
SELECT
    d.name COLLATE DATABASE_DEFAULT AS metric_item,
    d.state_desc COLLATE DATABASE_DEFAULT AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    CASE
        WHEN d.state_desc = 'ONLINE' AND d.is_read_only = 0 THEN 'OK'
        WHEN d.state_desc = 'ONLINE' AND d.is_read_only = 1 THEN 'WARNING'
        ELSE 'CRITICAL'
    END AS status,
    CONCAT(
        'database=', d.name COLLATE DATABASE_DEFAULT,
        ', state=', d.state_desc COLLATE DATABASE_DEFAULT,
        ', user_access=', d.user_access_desc COLLATE DATABASE_DEFAULT,
        ', read_only=', d.is_read_only,
        ', compatibility_level=', d.compatibility_level,
        ', collation_name=', d.collation_name,
        ', is_auto_shrink_on=', d.is_auto_shrink_on,
        ', is_broker_enabled=', d.is_broker_enabled,
        ', service_broker_guid=', d.service_broker_guid,
        ', database_id=', d.database_id
    ) AS message
FROM sys.databases AS d
ORDER BY d.database_id;

IF CAST(SERVERPROPERTY('EngineEdition') AS int) = 4
BEGIN
    INSERT INTO @status (metric_item, metric_value, metric_unit, status, message)
    VALUES (
        'sql_agent_service',
        'Not installed',
        NULL,
        'LOGGING',
        'status_desc=Not installed, startup_type_desc=Not applicable, service_account=Not applicable, note=SQL Server Express edition does not include SQL Server Agent'
    );
END
ELSE IF @@VERSION LIKE '%on Linux%'
    AND NOT EXISTS (
        SELECT 1 FROM sys.dm_server_services
        WHERE servicename LIKE 'SQL Server Agent%' AND status_desc = 'Running'
    )
BEGIN
    DECLARE @scheduled_jobs int = NULL;
    BEGIN TRY
        SELECT @scheduled_jobs = COUNT(DISTINCT j.job_id)
        FROM msdb.dbo.sysjobs AS j
        JOIN msdb.dbo.sysjobschedules AS js ON js.job_id = j.job_id
        JOIN msdb.dbo.sysschedules AS s ON s.schedule_id = js.schedule_id
        WHERE j.enabled = 1 AND s.enabled = 1;
    END TRY
    BEGIN CATCH
        SET @scheduled_jobs = NULL;
    END CATCH

    INSERT INTO @status (metric_item, metric_value, metric_unit, status, message)
    SELECT TOP (1)
        'sql_agent_service',
        CASE WHEN @scheduled_jobs > 0 THEN CAST(ISNULL(status_desc, 'Not running') AS varchar(32)) ELSE 'Not enabled' END,
        NULL,
        CASE WHEN @scheduled_jobs > 0 THEN 'CRITICAL' ELSE 'OK' END,
        CONCAT(
            'service_name=', ISNULL(servicename, 'SQL Server Agent'),
            ', status_desc=', ISNULL(status_desc, 'Not listed'),
            ', startup_type_desc=', ISNULL(startup_type_desc, 'Not applicable'),
            ', service_account=', ISNULL(service_account, 'Not applicable'),
            ', platform=Linux',
            ', scheduled_jobs=', ISNULL(CAST(@scheduled_jobs AS varchar(10)), 'unreadable'),
            CASE WHEN @scheduled_jobs > 0
                 THEN ', note=Agent is not running on this Linux instance and enabled scheduled jobs will not run'
                 WHEN @scheduled_jobs IS NULL
                 THEN ', note=SQL Server on Linux runs Agent only when mssql-conf sets sqlagent.enabled; this login cannot read the msdb jobs that would need it'
                 ELSE ', note=SQL Server on Linux runs Agent only when mssql-conf sets sqlagent.enabled; nothing scheduled needs it'
            END
        )
    FROM (SELECT 1 AS one) AS anchor
    LEFT JOIN sys.dm_server_services AS svc
        ON svc.servicename LIKE 'SQL Server Agent%';
END
ELSE
BEGIN
    INSERT INTO @status (metric_item, metric_value, metric_unit, status, message)
    SELECT TOP (1)
        CAST('sql_agent_service' AS varchar(256)) AS metric_item,
        CAST(status_desc AS varchar(32)) AS metric_value,
        CAST(NULL AS varchar(32)) AS metric_unit,
        CASE
            WHEN status_desc = 'Running' THEN 'OK'
            ELSE 'CRITICAL'
        END AS status,
        CONCAT(
            'service_name=', servicename,
            ', status_desc=', status_desc,
            ', startup_type_desc=', startup_type_desc,
            ', service_account=', service_account
        ) AS message
    FROM sys.dm_server_services
    WHERE servicename LIKE 'SQL Server Agent%';

    IF NOT EXISTS (SELECT 1 FROM @status WHERE metric_item = 'sql_agent_service')
    BEGIN
        INSERT INTO @status (metric_item, metric_value, metric_unit, status, message)
        VALUES (
            'sql_agent_service',
            'Not installed',
            NULL,
            'CRITICAL',
            'status_desc=Not installed, startup_type_desc=Unknown, service_account=Unknown, note=SQL Server Agent service not found in sys.dm_server_services'
        );
    END
END

SELECT
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
FROM @status
ORDER BY sort_order;