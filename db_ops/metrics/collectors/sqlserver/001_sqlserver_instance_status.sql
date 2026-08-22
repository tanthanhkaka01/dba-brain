SELECT
    CAST(@@SERVERNAME AS varchar(256)) AS metric_item,
    CAST('ONLINE' AS varchar(32)) AS metric_value,
    CAST(NULL AS varchar(32)) AS metric_unit,
    'OK' AS status,
    CONCAT(
        'SQL Server connection is available. ',
        'server=', @@SERVERNAME,
        '; ip=', COALESCE(CONVERT(varchar(64), CONNECTIONPROPERTY('local_net_address')), 'N/A'),
        '; port=', COALESCE(CONVERT(varchar(16), CONNECTIONPROPERTY('local_tcp_port')), 'N/A'),
        '; transport=', COALESCE(CONVERT(varchar(32), CONNECTIONPROPERTY('net_transport')), 'N/A'),
        '; protocol=', COALESCE(CONVERT(varchar(32), CONNECTIONPROPERTY('protocol_type')), 'N/A'),
        '; auth=', COALESCE(CONVERT(varchar(32), CONNECTIONPROPERTY('auth_scheme')), 'N/A'),

        '; version=', COALESCE(CONVERT(varchar(64), SERVERPROPERTY('ProductVersion')), 'N/A'),
        '; level=', COALESCE(CONVERT(varchar(32), SERVERPROPERTY('ProductLevel')), 'N/A'),
        '; CU=', COALESCE(CONVERT(varchar(32), SERVERPROPERTY('ProductUpdateLevel')), 'N/A'),
        '; update=', COALESCE(CONVERT(varchar(256), SERVERPROPERTY('ProductUpdateReference')), 'N/A'),

        '; edition=', COALESCE(CONVERT(varchar(128), SERVERPROPERTY('Edition')), 'N/A'),
        '; engine=',
            CASE SERVERPROPERTY('EngineEdition')
                WHEN 2 THEN 'Standard'
                WHEN 3 THEN 'Enterprise-compatible'
                WHEN 4 THEN 'Express'
                WHEN 5 THEN 'Azure SQL DB'
                WHEN 8 THEN 'Azure Managed Instance'
                ELSE CONVERT(varchar(10), SERVERPROPERTY('EngineEdition'))
            END,

        '; instance=', COALESCE(CONVERT(varchar(128), SERVERPROPERTY('InstanceName')), 'MSSQLSERVER'),
        '; machine=', COALESCE(CONVERT(varchar(128), SERVERPROPERTY('MachineName')), 'N/A'),
        '; physical=', COALESCE(CONVERT(varchar(128), SERVERPROPERTY('ComputerNamePhysicalNetBIOS')), 'N/A'),
        '; clustered=', CASE WHEN SERVERPROPERTY('IsClustered') = 1 THEN 'Yes' ELSE 'No' END,
        '; pid=', CONVERT(varchar(16), SERVERPROPERTY('ProcessID')),
        '; collation=', CONVERT(varchar(128), SERVERPROPERTY('Collation')),
        '; started=', CONVERT(varchar(19), sqlserver_start_time, 120)
    ) AS message
FROM sys.dm_os_sys_info;