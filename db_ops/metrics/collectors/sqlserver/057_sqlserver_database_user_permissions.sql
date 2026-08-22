-- DATABASE_USER_PERMISSIONS (SQL Server): per-database principal (user) inventory with
-- role memberships, notable database-scope permissions, and login mapping. One row per
-- (database, user). Security inventory: status is OK for normal principals; WARNING for
-- classic risks (guest CONNECT enabled, high-privilege membership such as db_owner /
-- db_securityadmin / db_accessadmin / db_ddladmin, or a CONTROL grant at database scope).
--
-- Runs from the instance (master); a cursor walks each ONLINE, accessible user database and
-- reads its catalog views under that database's context. Databases the monitoring login
-- cannot read are skipped (TRY/CATCH), never failing the whole metric. High-privilege /
-- WARNING rows are ordered first so they survive the collector's row cap.
SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#dbusers') IS NOT NULL DROP TABLE #dbusers;
CREATE TABLE #dbusers (
    db_name        sysname,
    principal_name sysname,
    principal_type nvarchar(60)  NULL,
    auth_type      nvarchar(60)  NULL,
    roles          nvarchar(2000) NULL,
    is_high        bit           NOT NULL DEFAULT (0),
    is_risk        bit           NOT NULL DEFAULT (0),
    note           nvarchar(1000) NULL
);

DECLARE @db sysname, @sql nvarchar(max);

DECLARE db_cursor CURSOR LOCAL FAST_FORWARD FOR
    SELECT d.name
    FROM sys.databases AS d
    WHERE d.database_id > 4           -- user databases (skip master/tempdb/model/msdb)
      AND d.state = 0                 -- ONLINE only
      AND d.source_database_id IS NULL -- skip database snapshots
      AND d.is_read_only = 0
      AND HAS_DBACCESS(d.name) = 1     -- the monitoring login can enter it
    ORDER BY d.name;

OPEN db_cursor;
FETCH NEXT FROM db_cursor INTO @db;
WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'
        USE ' + QUOTENAME(@db) + N';

        -- Real users (SQL/Windows/group/certificate/asymmetric-key mapped), excluding
        -- the built-in dbo/sys/INFORMATION_SCHEMA; guest is handled separately below.
        INSERT INTO #dbusers (db_name, principal_name, principal_type, auth_type, roles, is_high, is_risk, note)
        SELECT
            DB_NAME(),
            dp.name,
            dp.type_desc,
            CAST(NULL AS nvarchar(60)),   -- auth type omitted for 2008 R2 compatibility
            STUFF((
                SELECT N'','' + r.name
                FROM sys.database_role_members AS rm
                JOIN sys.database_principals  AS r ON r.principal_id = rm.role_principal_id
                WHERE rm.member_principal_id = dp.principal_id
                ORDER BY r.name
                FOR XML PATH(N''''), TYPE).value(N''.'', N''nvarchar(2000)''), 1, 1, N''''),
            CASE WHEN EXISTS (
                    SELECT 1
                    FROM sys.database_role_members AS rm
                    JOIN sys.database_principals  AS r ON r.principal_id = rm.role_principal_id
                    WHERE rm.member_principal_id = dp.principal_id
                      AND r.name IN (N''db_owner'', N''db_securityadmin'', N''db_accessadmin'', N''db_ddladmin''))
                 OR EXISTS (
                    SELECT 1 FROM sys.database_permissions AS perm
                    WHERE perm.grantee_principal_id = dp.principal_id
                      AND perm.state IN (N''G'', N''W'')
                      AND perm.permission_name IN (N''CONTROL'', N''ALTER ANY USER'', N''ALTER ANY ROLE'', N''TAKE OWNERSHIP'', N''IMPERSONATE''))
                 THEN 1 ELSE 0 END,
            0,
            LEFT(N''login='' + ISNULL(SUSER_SNAME(dp.sid), N''<orphaned/none>''), 1000)
        FROM sys.database_principals AS dp
        WHERE dp.type IN (N''S'', N''U'', N''G'', N''E'', N''X'', N''C'', N''K'')
          AND dp.name NOT IN (N''dbo'', N''guest'', N''sys'', N''INFORMATION_SCHEMA'')
          AND dp.is_fixed_role = 0;

        -- guest access: a real risk when guest holds CONNECT in a user database.
        IF EXISTS (
            SELECT 1
            FROM sys.database_permissions AS perm
            JOIN sys.database_principals  AS g ON g.principal_id = perm.grantee_principal_id
            WHERE g.name = N''guest'' AND perm.permission_name = N''CONNECT'' AND perm.state = N''G'')
        BEGIN
            INSERT INTO #dbusers (db_name, principal_name, principal_type, auth_type, roles, is_high, is_risk, note)
            VALUES (DB_NAME(), N''guest'', N''GUEST'', N''NONE'', N'''', 0, 1,
                    N''guest has CONNECT in this database (public access enabled) - consider REVOKE CONNECT FROM guest'');
        END;
    ';

    BEGIN TRY
        EXEC sys.sp_executesql @sql;
    END TRY
    BEGIN CATCH
        INSERT INTO #dbusers (db_name, principal_name, principal_type, note)
        VALUES (@db, N'<not-readable>', N'ERROR', LEFT(N'Could not read principals: ' + ERROR_MESSAGE(), 1000));
    END CATCH;

    FETCH NEXT FROM db_cursor INTO @db;
END
CLOSE db_cursor;
DEALLOCATE db_cursor;

SELECT
    CAST(u.db_name + N'\' + u.principal_name AS varchar(400)) AS metric_item,
    CAST(ISNULL(u.principal_type, N'USER') AS varchar(64))    AS metric_value,
    CAST(NULL AS varchar(32))                                 AS metric_unit,
    CAST(CASE WHEN u.is_risk = 1 THEN 'WARNING' ELSE 'OK' END AS varchar(16)) AS status,
    CAST(LEFT(
        ISNULL(u.note, N'')
        + CASE WHEN NULLIF(u.roles, N'') IS NOT NULL THEN N' | roles=[' + u.roles + N']' ELSE N'' END
        + CASE WHEN u.is_high = 1 THEN N' | HIGH_PRIVILEGE' ELSE N'' END
        + CASE WHEN u.auth_type IS NOT NULL THEN N' | auth=' + u.auth_type ELSE N'' END,
        1000) AS varchar(1000)) AS message
FROM #dbusers AS u
ORDER BY u.is_risk DESC, u.is_high DESC, u.db_name, u.principal_name;

IF OBJECT_ID('tempdb..#dbusers') IS NOT NULL DROP TABLE #dbusers;
