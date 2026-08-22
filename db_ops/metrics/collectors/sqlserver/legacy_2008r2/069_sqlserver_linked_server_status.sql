-- LINKED_SERVER_STATUS — SQL Server 2008 R2 variant.
--
-- Same two facts and the same severity rules as the 2012+ file (see
-- sqlserver/069_sqlserver_linked_server_status.sql); this exists only because that file uses two
-- things 2008 R2 does not have, and each one fails the WHOLE batch at compile time — which is why
-- 192.0.2.245 and 192.0.2.253 reported nothing but
-- "'CONCAT' is not a recognized built-in function name (195)" every hour for days:
--
--   * CONCAT()                          - added in 2012. Replaced with `+`, which means every
--                                         operand has to be made NULL-safe and string-typed by
--                                         hand: CONCAT swallows NULL as '' and casts numbers,
--                                         `+` propagates NULL through the whole message and
--                                         raises 245 on varchar + int.
--   * sys.fn_hadr_is_primary_replica()  - added in 2012 with Availability Groups. There are no
--                                         AGs on 2008 R2, so the replica test is simply dropped;
--                                         the state/updateability filters it sat beside already
--                                         exclude the databases that cannot be read.
--
-- Everything else (sp_testlinkedserver, the error-number classification, FOR XML PATH sampling)
-- is 2005-era and carries over unchanged.

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
DECLARE @db sysname, @sql nvarchar(max);
DECLARE db_cur CURSOR LOCAL FAST_FORWARD FOR
    SELECT d.name
    FROM sys.databases AS d
    WHERE d.database_id > 4
      AND d.state_desc = 'ONLINE'
      AND DATABASEPROPERTYEX(d.name, 'Updateability') = 'READ_WRITE';
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
    -- The `+` rewrite of the 2012+ CONCAT call. Every count is CAST to varchar (varchar + int is
    -- error 245, not concatenation) and everything nullable is ISNULL'd (one NULL operand would
    -- otherwise turn the entire message NULL, losing the whole finding rather than one field).
        'linked_server=' + l.srv_name COLLATE DATABASE_DEFAULT
      + ', usable=' + CASE WHEN l.is_reachable = 1 THEN 'yes' ELSE 'no' END
      + ', failure=' + CASE WHEN l.is_reachable = 1 THEN 'none'
                            ELSE ISNULL(l.fail_state, 'ERROR') COLLATE DATABASE_DEFAULT END
      + CASE WHEN l.fail_state = 'CREDENTIAL_UNREADABLE'
             THEN ' (LOCAL problem: this instance cannot decrypt the stored remote login;'
                  + ' the remote server may be fine. Fix: re-enter the linked server login)'
             ELSE '' END
      + ', product=' + ISNULL(l.product, '?') COLLATE DATABASE_DEFAULT
      + ', provider=' + ISNULL(l.provider, '?') COLLATE DATABASE_DEFAULT
      + ', data_source=' + LEFT(ISNULL(l.data_source, '?') COLLATE DATABASE_DEFAULT, 200)
      + ', referenced_by_procedures=' + CAST(ISNULL(u.proc_count, 0) AS varchar(12))
      + ', referenced_by_objects=' + CAST(ISNULL(u.total_count, 0) AS varchar(12))
      + ', databases_referencing=' + CAST(ISNULL(u.db_count, 0) AS varchar(12))
      + CASE WHEN u.sample_objects IS NULL THEN ''
             ELSE ', sample=' + u.sample_objects COLLATE DATABASE_DEFAULT END
      + CASE WHEN l.is_reachable = 1 THEN ''
             ELSE ', error_number=' + ISNULL(CAST(l.fail_number AS varchar(12)), '?')
                  + ', error=' + ISNULL(l.fail_message, 'unknown') COLLATE DATABASE_DEFAULT END
    AS message
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
