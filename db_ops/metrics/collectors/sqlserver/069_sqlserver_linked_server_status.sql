-- LINKED_SERVER_STATUS — does each linked server still answer, and does anything still use it?
--
-- Two facts reported together, because neither is actionable alone:
--
--   * reachability  - sp_testlinkedserver, which is the only check that actually opens the remote
--                     connection. sys.servers merely lists what was defined; a linked server whose
--                     target was decommissioned years ago still looks perfectly fine there.
--   * usage         - how many programmable objects reference the linked server by name.
--
-- The severity comes from the combination, which is the point:
--
--   unreachable + referenced by code -> CRITICAL   something WILL fail the next time it runs
--   unreachable + referenced nowhere -> WARNING    dead configuration, cleanup not an incident
--   reachable                        -> OK         (still reports the reference count, so an
--                                                   unused linked server is visible before it rots)
--
-- Usage is counted by searching module text for the linked server name, which is how a four-part
-- name (`SRV.db.schema.obj`) and an OPENQUERY/OPENROWSET call both appear. It is a text match, so
-- it over-counts a name that is also an ordinary word - the message says which objects matched so
-- the reader can judge, rather than presenting a bare number as certainty.

SET NOCOUNT ON;

IF OBJECT_ID('tempdb..#ls') IS NOT NULL DROP TABLE #ls;
IF OBJECT_ID('tempdb..#ls_use') IS NOT NULL DROP TABLE #ls_use;

CREATE TABLE #ls
(
    srv_name     sysname,
    product      nvarchar(128) NULL,
    provider     nvarchar(128) NULL,
    data_source  nvarchar(4000) NULL,
    is_reachable bit NULL,
    fail_number  int NULL,
    fail_state   varchar(24) NULL,
    fail_message nvarchar(2000) NULL
);

CREATE TABLE #ls_use
(
    srv_name    sysname,
    db_name     sysname,
    object_name sysname,
    object_type varchar(20)
);

INSERT INTO #ls (srv_name, product, provider, data_source)
SELECT s.name, s.product, s.provider, s.data_source
FROM sys.servers AS s
WHERE s.is_linked = 1;

-- ---------------------------------------------------------------- reachability
DECLARE @srv sysname;
DECLARE ls_cur CURSOR LOCAL FAST_FORWARD FOR SELECT srv_name FROM #ls;
OPEN ls_cur;
FETCH NEXT FROM ls_cur INTO @srv;
WHILE @@FETCH_STATUS = 0
BEGIN
    BEGIN TRY
        EXEC sys.sp_testlinkedserver @srv;
        UPDATE #ls SET is_reachable = 1 WHERE srv_name = @srv;
    END TRY
    BEGIN CATCH
        -- One dead linked server must not abort the scan of the others.
        --
        -- The failures that land here are NOT all "the target is down", and calling them all
        -- UNREACHABLE sends an operator to the wrong machine. Error 15466 in particular means the
        -- LOCAL instance could not decrypt the stored remote-login password (service master key
        -- changed, or the instance was restored elsewhere) - the remote server may be perfectly
        -- healthy, and the fix is local: re-enter the linked server login. One SMK problem
        -- otherwise fans out into an alert per linked server, all blaming innocent hosts.
        UPDATE #ls
        SET is_reachable = 0,
            fail_number = ERROR_NUMBER(),
            fail_state =
                CASE
                    WHEN ERROR_NUMBER() = 15466 THEN 'CREDENTIAL_UNREADABLE'
                    -- 18487/18488 are an EXPIRED / must-change password, which reads as a
                    -- generic ERROR unless it is listed: the first run of this metric found
                    -- exactly that on a linked-server target. The distinction is
                    -- the whole fix - a rejected login is a credential to renew, not a host
                    -- to go and restart.
                    WHEN ERROR_NUMBER() IN (18456, 18452, 4064, 18487, 18488) THEN 'LOGIN_REJECTED'
                    WHEN ERROR_MESSAGE() LIKE '%Named Pipes%'
                      OR ERROR_MESSAGE() LIKE '%not found or was not accessible%'
                      OR ERROR_MESSAGE() LIKE '%network-related%'
                      OR ERROR_MESSAGE() LIKE '%timeout%' THEN 'UNREACHABLE'
                    ELSE 'ERROR'
                END,
            fail_message = LEFT(REPLACE(REPLACE(ERROR_MESSAGE(), CHAR(13), ' '), CHAR(10), ' '), 300)
        WHERE srv_name = @srv;
    END CATCH;
    FETCH NEXT FROM ls_cur INTO @srv;
END
CLOSE ls_cur;
DEALLOCATE ls_cur;

-- ---------------------------------------------------------------- usage in code
-- sys.fn_hadr_is_primary_replica does not exist before SQL Server 2012 SP1, and this variant is
-- selected from major_version 11 - which also admits 2012 RTM. A missing scalar function is a
-- BIND error, not a run-time one, so the whole file failed to compile and the metric returned
-- nothing at all on 192.0.2.12 (11.0.2100.60). Resolving the call through dynamic SQL means
-- it is only ever compiled on an instance that has it; everywhere else the set is simply empty,
-- which is the correct answer for an instance that cannot have an Availability Group replica.
--
-- Explicit COLLATE: the table variable takes the *database* collation while sys.databases.name
-- takes the *instance* collation, and on a server where they differ the join raises a collation
-- conflict instead of filtering.
DECLARE @ag_secondary TABLE (db_name nvarchar(128) COLLATE DATABASE_DEFAULT PRIMARY KEY);
IF OBJECT_ID('sys.fn_hadr_is_primary_replica') IS NOT NULL
    INSERT INTO @ag_secondary (db_name)
    EXEC sys.sp_executesql
        N'SELECT name COLLATE DATABASE_DEFAULT FROM sys.databases
          WHERE sys.fn_hadr_is_primary_replica(name) = 0;';

DECLARE @db sysname, @sql nvarchar(max);
DECLARE db_cur CURSOR LOCAL FAST_FORWARD FOR
    SELECT d.name
    FROM sys.databases AS d
    WHERE d.database_id > 4
      AND d.state_desc = 'ONLINE'
      AND DATABASEPROPERTYEX(d.name, 'Updateability') = 'READ_WRITE'
      AND NOT EXISTS (SELECT 1 FROM @ag_secondary AS s
                      WHERE s.db_name = d.name COLLATE DATABASE_DEFAULT);
OPEN db_cur;
FETCH NEXT FROM db_cur INTO @db;
WHILE @@FETCH_STATUS = 0
BEGIN
    SET @sql = N'
        INSERT INTO #ls_use (srv_name, db_name, object_name, object_type)
        SELECT l.srv_name, @db_name, o.name,
               CASE o.type WHEN ''P'' THEN ''PROCEDURE''
                           WHEN ''V'' THEN ''VIEW''
                           WHEN ''FN'' THEN ''FUNCTION''
                           WHEN ''IF'' THEN ''FUNCTION''
                           WHEN ''TF'' THEN ''FUNCTION''
                           WHEN ''TR'' THEN ''TRIGGER''
                           ELSE o.type_desc COLLATE DATABASE_DEFAULT END
        FROM ' + QUOTENAME(@db) + N'.sys.sql_modules AS m
        JOIN ' + QUOTENAME(@db) + N'.sys.objects AS o ON o.object_id = m.object_id
        CROSS JOIN #ls AS l
        WHERE m.definition LIKE N''%'' + l.srv_name + N''%'';';
    BEGIN TRY
        EXEC sp_executesql @sql, N'@db_name sysname', @db_name = @db;
    END TRY
    BEGIN CATCH
        -- An unreadable database costs its own row, not the whole scan.
        SET @sql = NULL;
    END CATCH;
    FETCH NEXT FROM db_cur INTO @db;
END
CLOSE db_cur;
DEALLOCATE db_cur;

-- ---------------------------------------------------------------- report
SELECT
    l.srv_name COLLATE DATABASE_DEFAULT AS metric_item,
    CASE WHEN l.is_reachable = 1 THEN 'REACHABLE'
         ELSE ISNULL(l.fail_state, 'ERROR') COLLATE DATABASE_DEFAULT END AS metric_value,
    CAST('objects' AS varchar(32)) AS metric_unit,
    CASE
        WHEN l.is_reachable = 1 THEN 'OK'
        WHEN u.proc_count > 0 THEN 'CRITICAL'   -- code depends on a server that does not answer
        ELSE 'WARNING'                          -- defined, dead, and unused: cleanup
    END AS status,
    CONCAT(
        'linked_server=', l.srv_name COLLATE DATABASE_DEFAULT,
        ', usable=', CASE WHEN l.is_reachable = 1 THEN 'yes' ELSE 'no' END,
        ', failure=', CASE WHEN l.is_reachable = 1 THEN 'none'
                           ELSE ISNULL(l.fail_state, 'ERROR') COLLATE DATABASE_DEFAULT END,
        CASE WHEN l.fail_state = 'CREDENTIAL_UNREADABLE'
             THEN ' (LOCAL problem: this instance cannot decrypt the stored remote login;'
                  + ' the remote server may be fine. Fix: re-enter the linked server login)'
             ELSE '' END,
        ', product=', ISNULL(l.product, '?') COLLATE DATABASE_DEFAULT,
        ', provider=', ISNULL(l.provider, '?') COLLATE DATABASE_DEFAULT,
        ', data_source=', LEFT(ISNULL(l.data_source, '?') COLLATE DATABASE_DEFAULT, 200),
        ', referenced_by_procedures=', ISNULL(u.proc_count, 0),
        ', referenced_by_objects=', ISNULL(u.total_count, 0),
        ', databases_referencing=', ISNULL(u.db_count, 0),
        CASE WHEN u.sample_objects IS NULL THEN ''
             ELSE ', sample=' + u.sample_objects COLLATE DATABASE_DEFAULT END,
        CASE WHEN l.is_reachable = 1 THEN ''
             ELSE ', error_number=' + ISNULL(CAST(l.fail_number AS varchar(12)), '?')
                  + ', error=' + ISNULL(l.fail_message, 'unknown') COLLATE DATABASE_DEFAULT END
    ) AS message
FROM #ls AS l
OUTER APPLY
(
    SELECT
        proc_count  = SUM(CASE WHEN x.object_type = 'PROCEDURE' THEN 1 ELSE 0 END),
        total_count = COUNT(*),
        db_count    = COUNT(DISTINCT x.db_name),
        sample_objects = STUFF((
            SELECT TOP 5 ' | ' + y.db_name + '.' + y.object_name
            FROM #ls_use AS y
            WHERE y.srv_name = l.srv_name
            ORDER BY y.db_name, y.object_name
            FOR XML PATH(''), TYPE).value('.', 'nvarchar(1000)'), 1, 3, '')
    FROM #ls_use AS x
    WHERE x.srv_name = l.srv_name
) AS u
ORDER BY
    CASE WHEN l.is_reachable = 1 THEN 2 ELSE 1 END,
    ISNULL(u.proc_count, 0) DESC,
    l.srv_name COLLATE DATABASE_DEFAULT;
