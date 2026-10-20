-- DATABASE_STATUS (SQL Server 2008 / 2008 R2): database status and SQL Server Agent service state.
--
-- Why system databases (master, tempdb, model, msdb) are included:
-- System databases were previously omitted (database_id > 4) under the assumption that
-- DATABASE_CONFIG reported them. However, DATABASE_CONFIG only inventories configuration and does
-- not evaluate health state. Leaving them out meant an offline, suspect, or recovery-pending msdb,
-- model, or tempdb would page nothing. They are now graded with the same rule user databases have:
-- not ONLINE = CRITICAL.
--
-- Why SQL Server Agent is graded and guarded via dynamic SQL:
-- Targets monitored without host-level cmd_access have no OS_SERVICE_STATUS checks. An Agent
-- service that is not 'Running' halts scheduled jobs, backups, and maintenance without alerting.
-- Express edition (EngineEdition = 4) does not include SQL Server Agent by design; an absent Agent
-- there is an expected state rather than an outage and is reported at LOGGING.
-- Furthermore, sys.dm_server_services was introduced in SQL Server 2008 R2 SP1; earlier builds
-- (2008 RTM/SP and 2008 R2 RTM) do not have this DMV and a static reference would fail compilation
-- at bind time. The existence check and dynamic SQL prevent compile failures on older builds while
-- reporting LOGGING ("not readable on this build").
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
    'database=' + d.name COLLATE DATABASE_DEFAULT
        + ', state=' + d.state_desc COLLATE DATABASE_DEFAULT
        + ', user_access=' + d.user_access_desc COLLATE DATABASE_DEFAULT
        + ', read_only=' + CAST(d.is_read_only AS varchar(8))
        + ', compatibility_level=' + CAST(d.compatibility_level AS varchar(8))
        + ', database_id=' + CAST(d.database_id AS varchar(10))
        + ', collation_name=' + CAST(d.collation_name AS VARCHAR(100))
        + ', is_auto_shrink_on=' + CAST(d.is_auto_shrink_on AS VARCHAR(100))
        + ', is_broker_enabled=' + CAST(d.is_broker_enabled AS VARCHAR(100))
        + ', service_broker_guid=' + CAST(d.service_broker_guid AS VARCHAR(100))
        AS message
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
ELSE IF OBJECT_ID('sys.dm_server_services') IS NULL
BEGIN
    INSERT INTO @status (metric_item, metric_value, metric_unit, status, message)
    VALUES (
        'sql_agent_service',
        'UNKNOWN',
        NULL,
        'LOGGING',
        'status_desc=UNKNOWN, startup_type_desc=UNKNOWN, service_account=UNKNOWN, note=not readable on this build (sys.dm_server_services requires SQL Server 2008 R2 SP1+)'
    );
END
ELSE
BEGIN
    -- Dynamic SQL because sys.dm_server_services does not exist before 2008 R2 SP1.
    -- Compiling a static reference on 2008 R2 RTM or 2008 would fail at bind time.
    DECLARE @agent_sql nvarchar(max);
    SET @agent_sql = N'
    SELECT TOP (1)
        CAST(''sql_agent_service'' AS varchar(256)) AS metric_item,
        CAST(status_desc AS varchar(32)) AS metric_value,
        CAST(NULL AS varchar(32)) AS metric_unit,
        CASE
            WHEN status_desc = ''Running'' THEN ''OK''
            ELSE ''CRITICAL''
        END AS status,
        ''service_name='' + CAST(servicename AS varchar(256))
            + '', status_desc='' + CAST(status_desc AS varchar(64))
            + '', startup_type_desc='' + CAST(startup_type_desc AS varchar(64))
            + '', service_account='' + CAST(service_account AS varchar(256)) AS message
    FROM sys.dm_server_services
    WHERE servicename LIKE ''SQL Server Agent%'';';

    INSERT INTO @status (metric_item, metric_value, metric_unit, status, message)
    EXEC sys.sp_executesql @agent_sql;

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