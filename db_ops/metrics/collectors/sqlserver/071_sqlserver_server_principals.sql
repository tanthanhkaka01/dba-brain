-- SECURITY_SERVER_PRINCIPALS (SQL Server): the instance's login inventory. One row per server
-- principal (SQL login, Windows login, Windows group), with its server role memberships, notable
-- server-scope permissions, default database, and password age for SQL logins.
--
-- WHY THIS EXISTS SEPARATELY FROM SECURITY_LOGIN_HEALTH. That metric is exception-based: it
-- reports the logins with a problem (old password, long session, failed attempts, dormant) and a
-- summary row. It cannot answer "who can connect to this instance", because a healthy login never
-- appears in it. The server-metrics report needs the whole list beside DATABASE_USER_PERMISSIONS,
-- which already gives the per-database half.
--
-- STATUS IS ALWAYS OK, DELIBERATELY. This is an inventory, and the alerting on these same
-- principals already belongs to SECURITY_LOGIN_HEALTH (password age, dormancy) and
-- SECURITY_FAILED_LOGINS. Grading sysadmin membership as WARNING here would raise one on every
-- instance in the estate for `sa` and the engine's own service accounts, on the first collection,
-- for ever. High privilege is stated in the message instead, and ordered first so it survives the
-- collector's row cap.
--
-- 2008 R2 compatible on purpose (one variant covers every version in the catalog): no CONCAT, no
-- IIF, no TRY_CONVERT, and no sys.server_principals.authentication_type_desc - all of them 2012+.
SET NOCOUNT ON;

SELECT
    CAST(p.name COLLATE DATABASE_DEFAULT AS varchar(400))       AS metric_item,
    CAST(p.type_desc COLLATE DATABASE_DEFAULT AS varchar(64))   AS metric_value,
    CAST(NULL AS varchar(32))                                   AS metric_unit,
    CAST('OK' AS varchar(16))                                   AS status,
    CAST(LEFT(
          'type=' + p.type_desc COLLATE DATABASE_DEFAULT
        + ', disabled=' + CASE WHEN p.is_disabled = 1 THEN 'yes' ELSE 'no' END
        + ', default_db=' + ISNULL(p.default_database_name COLLATE DATABASE_DEFAULT, '-')
        + ', created=' + CONVERT(varchar(10), p.create_date, 120)
        -- Only SQL logins have one; a Windows principal's password is the domain's business.
        + CASE
            WHEN p.type = 'S' AND LOGINPROPERTY(p.name, 'PasswordLastSetTime') IS NOT NULL
                THEN ', password_last_set=' + CONVERT(varchar(10),
                        CAST(LOGINPROPERTY(p.name, 'PasswordLastSetTime') AS datetime), 120)
                   + ', password_age_days=' + CAST(DATEDIFF(day,
                        CAST(LOGINPROPERTY(p.name, 'PasswordLastSetTime') AS datetime),
                        GETDATE()) AS varchar(12))
            ELSE ''
          END
        + CASE
            WHEN p.type = 'S'
                THEN ', check_policy=' + CASE WHEN sl.is_policy_checked = 1 THEN 'on' ELSE 'off' END
            ELSE ''
          END
        + ', server_roles=[' + ISNULL(roles.list, '') + ']'
        + ', server_perms=[' + ISNULL(perms.list, '') + ']'
        + CASE WHEN roles.is_high = 1 OR perms.is_high = 1 THEN ' | HIGH_PRIVILEGE' ELSE '' END,
        1000) AS varchar(1000))                                 AS message
FROM sys.server_principals AS p
LEFT JOIN sys.sql_logins AS sl
       ON sl.principal_id = p.principal_id
OUTER APPLY (
    SELECT
        STUFF((
            SELECT ',' + r.name COLLATE DATABASE_DEFAULT
            FROM sys.server_role_members AS rm
            JOIN sys.server_principals  AS r ON r.principal_id = rm.role_principal_id
            WHERE rm.member_principal_id = p.principal_id
            ORDER BY r.name
            FOR XML PATH(''), TYPE).value('.', 'varchar(900)'), 1, 1, '') AS list,
        CASE WHEN EXISTS (
            SELECT 1
            FROM sys.server_role_members AS rm
            JOIN sys.server_principals  AS r ON r.principal_id = rm.role_principal_id
            WHERE rm.member_principal_id = p.principal_id
              -- The four that can take the instance, or take it over: sysadmin outright,
              -- securityadmin by granting itself sysadmin, serveradmin by shutting it down,
              -- and setupadmin by adding a linked server that runs as someone else.
              AND r.name IN ('sysadmin', 'securityadmin', 'serveradmin', 'setupadmin'))
        THEN 1 ELSE 0 END AS is_high
) AS roles
OUTER APPLY (
    SELECT
        STUFF((
            SELECT ',' + sp.state_desc COLLATE DATABASE_DEFAULT + ' ' + sp.permission_name COLLATE DATABASE_DEFAULT
            FROM sys.server_permissions AS sp
            WHERE sp.grantee_principal_id = p.principal_id
              AND sp.class = 100                       -- server scope only
              AND sp.permission_name <> 'CONNECT SQL'  -- every login has it; noise
            ORDER BY sp.permission_name
            FOR XML PATH(''), TYPE).value('.', 'varchar(900)'), 1, 1, '') AS list,
        CASE WHEN EXISTS (
            SELECT 1
            FROM sys.server_permissions AS sp
            WHERE sp.grantee_principal_id = p.principal_id
              AND sp.class = 100
              AND sp.state IN ('G', 'W')
              AND sp.permission_name IN (
                    'CONTROL SERVER', 'IMPERSONATE ANY LOGIN', 'ALTER ANY LOGIN',
                    'ALTER ANY SERVER ROLE', 'ALTER ANY CREDENTIAL'))
        THEN 1 ELSE 0 END AS is_high
) AS perms
WHERE p.type IN ('S', 'U', 'G')     -- SQL login, Windows login, Windows group
  AND p.name NOT LIKE '##%'         -- internal certificate-mapped principals
ORDER BY
    CASE WHEN roles.is_high = 1 OR perms.is_high = 1 THEN 0 ELSE 1 END,
    p.name;
