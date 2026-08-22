SELECT
    CAST(@@SERVERNAME AS varchar(256)) AS metric_item,
    CAST('ONLINE' AS varchar(32)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    'OK' AS status,
    'SQL Server connection is available.'
        + ' server=' + CAST(@@SERVERNAME AS varchar(256))
        + '; ip=' + ISNULL(CAST(CONNECTIONPROPERTY('local_net_address') AS varchar(64)), 'N/A')
        + '; port=' + ISNULL(CAST(CONNECTIONPROPERTY('local_tcp_port') AS varchar(16)), 'N/A')
        + '; transport=' + ISNULL(CAST(CONNECTIONPROPERTY('net_transport') AS varchar(32)), 'N/A')
        + '; protocol=' + ISNULL(CAST(CONNECTIONPROPERTY('protocol_type') AS varchar(32)), 'N/A')
        + '; auth=' + ISNULL(CAST(CONNECTIONPROPERTY('auth_scheme') AS varchar(32)), 'N/A')
        + '; version=' + ISNULL(CAST(SERVERPROPERTY('ProductVersion') AS varchar(64)), 'N/A')
        + '; level=' + ISNULL(CAST(SERVERPROPERTY('ProductLevel') AS varchar(32)), 'N/A')
        + '; CU=' + ISNULL(CAST(SERVERPROPERTY('ProductUpdateLevel') AS varchar(32)), 'N/A')
        + '; update=' + ISNULL(CAST(SERVERPROPERTY('ProductUpdateReference') AS varchar(256)), 'N/A')
        + '; edition=' + ISNULL(CAST(SERVERPROPERTY('Edition') AS varchar(128)), 'N/A')
        + '; engine='
            + CASE CAST(SERVERPROPERTY('EngineEdition') AS int)
                WHEN 2 THEN 'Standard'
                WHEN 3 THEN 'Enterprise-compatible'
                WHEN 4 THEN 'Express'
                WHEN 5 THEN 'Azure SQL Database'
                WHEN 8 THEN 'Azure SQL Managed Instance'
                ELSE CAST(SERVERPROPERTY('EngineEdition') AS varchar(10))
              END
        + '; instance=' + ISNULL(CAST(SERVERPROPERTY('InstanceName') AS varchar(128)), 'MSSQLSERVER')
        + '; machine=' + ISNULL(CAST(SERVERPROPERTY('MachineName') AS varchar(128)), 'N/A')
        + '; physical=' + ISNULL(CAST(SERVERPROPERTY('ComputerNamePhysicalNetBIOS') AS varchar(128)), 'N/A')
        + '; clustered=' + CASE WHEN CAST(SERVERPROPERTY('IsClustered') AS int) = 1 THEN 'Yes' ELSE 'No' END
        + '; pid=' + CAST(SERVERPROPERTY('ProcessID') AS varchar(16))
        + '; collation=' + CAST(SERVERPROPERTY('Collation') AS varchar(128))
        + '; started=' + CONVERT(varchar(19), sqlserver_start_time, 120)
        AS message
FROM sys.dm_os_sys_info;