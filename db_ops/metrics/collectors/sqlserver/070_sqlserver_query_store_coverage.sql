-- QUERY_STORE_COVERAGE — the Query Store state of EVERY user database, and its settings.
--
-- Split out of QUERY_STORE_QUERY_ISSUES (023). That metric answers "is a query misbehaving right
-- now", so it runs every 15 minutes; this answers "is Query Store switched on and healthy", which
-- is a configuration fact that changes when somebody changes it. Reporting the two on the same
-- cadence meant a database with Query Store off produced a WARNING every 15 minutes forever — 96
-- identical alerts a day, per database, on an estate where whole instances have it off. The alert
-- stream was mostly this one line, which is how a real finding gets missed. Same finding, once a
-- day, in the morning: see the time_window on QUERY_STORE_COVERAGE in data/metric_definitions.json.
--
-- **Every database gets a row, including the healthy ones.** It used to emit rows only for the
-- databases with Query Store off, which is enough to raise an alert and not enough to answer
-- anything: a database that is fine produced no row, so "is Query Store on for APPDB?" was
-- indistinguishable from "APPDB was never collected", and neither report could show a state at all.
-- The inventory page's per-database table and the server page's Query Store section are both built
-- from these rows, so the OK ones are the point, not overhead. This is the same shape DATABASE_STATUS
-- already has — it enumerates every database on every collection.
--
-- The settings come from sys.database_query_store_options, which is per-database, so they need one
-- dynamic query per database. Only databases that are ONLINE and readable are queried: a
-- non-readable AG secondary rejects the read with error 976, which is correct behaviour there
-- rather than a fault.
--
-- What the states mean, and why the report needs actual_state and not just is_query_store_on:
--   desired_state  what somebody configured (READ_WRITE / READ_ONLY / OFF)
--   actual_state   what it is really doing. **These disagree in the case that matters**: Query
--                  Store that filled its max_storage_size flips itself to READ_ONLY and silently
--                  stops capturing, while sys.databases.is_query_store_on still says 1. Reading
--                  only the desired state reports that database as covered when it has not
--                  captured anything since the day it filled up.
--   readonly_reason a bit mask explaining the flip; 65536 is the size limit, which is the common one.

SET NOCOUNT ON;

DECLARE @sql nvarchar(max);
DECLARE @db sysname;
DECLARE @major int = CAST(SERVERPROPERTY('ProductMajorVersion') AS int);

IF OBJECT_ID('tempdb..#qs_cov') IS NOT NULL DROP TABLE #qs_cov;
IF OBJECT_ID('tempdb..#qs_opt') IS NOT NULL DROP TABLE #qs_opt;

CREATE TABLE #qs_cov
(
    database_name sysname,
    db_state      varchar(30),
    desired_on    bit,
    skip_reason   varchar(40) NULL
);

CREATE TABLE #qs_opt
(
    database_name             sysname,
    desired_state_desc        varchar(60) NULL,
    actual_state_desc         varchar(60) NULL,
    readonly_reason           int NULL,
    current_storage_size_mb   bigint NULL,
    max_storage_size_mb       bigint NULL,
    interval_length_minutes   bigint NULL,
    stale_query_threshold_days bigint NULL,
    max_plans_per_query       bigint NULL,
    query_capture_mode_desc   varchar(60) NULL,
    size_based_cleanup_mode_desc varchar(60) NULL,
    wait_stats_capture_mode_desc varchar(60) NULL,
    flush_interval_seconds    bigint NULL
);

INSERT INTO #qs_cov (database_name, db_state, desired_on, skip_reason)
SELECT
    d.name,
    d.state_desc,
    d.is_query_store_on,
    CASE
        WHEN d.state_desc <> 'ONLINE' THEN 'DATABASE_NOT_ONLINE'
        WHEN sys.fn_hadr_is_primary_replica(d.name) = 0 THEN 'AG_SECONDARY'
        WHEN d.is_query_store_on = 0 THEN 'QUERY_STORE_OFF'
        ELSE NULL
    END
FROM sys.databases AS d
WHERE d.database_id > 4;

-- The options are read for every readable database, **including the ones with Query Store off**.
-- sys.database_query_store_options has a row whatever the state, and it is the only thing that
-- separates "somebody turned this off" from "this turned itself off": SALESDB on 192.0.2.115
-- reads desired_state=OFF with actual_state=ERROR and 2128 MB of captured data still sitting
-- there — it was on, it broke, and it is now off. Skipping the DMV for off databases reported
-- both cases as the same flat "OFF", which is the one thing the page could not act on.
DECLARE db_cur CURSOR LOCAL FAST_FORWARD FOR
SELECT database_name FROM #qs_cov
WHERE skip_reason IS NULL OR skip_reason = 'QUERY_STORE_OFF';

OPEN db_cur;
FETCH NEXT FROM db_cur INTO @db;

WHILE @@FETCH_STATUS = 0
BEGIN
    -- wait_stats_capture_mode arrived in SQL Server 2017; selecting it on 2016 is a compile
    -- error that would lose the whole collection, so it is only named when the version has it.
    SET @sql = N'
    SELECT
        N''' + REPLACE(@db, '''', '''''') + N''' AS database_name,
        o.desired_state_desc,
        o.actual_state_desc,
        o.readonly_reason,
        o.current_storage_size_mb,
        o.max_storage_size_mb,
        o.interval_length_minutes,
        o.stale_query_threshold_days,
        o.max_plans_per_query,
        o.query_capture_mode_desc,
        o.size_based_cleanup_mode_desc,
        ' + CASE WHEN @major >= 14 THEN N'o.wait_stats_capture_mode_desc' ELSE N'CAST(NULL AS varchar(60))' END + N',
        o.flush_interval_seconds
    FROM ' + QUOTENAME(@db) + N'.sys.database_query_store_options AS o;';

    BEGIN TRY
        INSERT INTO #qs_opt
        EXEC sp_executesql @sql;
    END TRY
    BEGIN CATCH
        -- One unreadable database must not cost the whole estate its Query Store picture: the
        -- row stays, with no settings, and the coverage state below still reports.
        IF 1 = 0 SELECT 1;
    END CATCH;

    FETCH NEXT FROM db_cur INTO @db;
END;

CLOSE db_cur;
DEALLOCATE db_cur;

-- WARNING, not CRITICAL, for every not-capturing case: a database with Query Store off still
-- serves its application. What is lost is the ability to diagnose it afterwards — worth a warning,
-- not a page. An AG secondary is reported OK, because being unreadable there is correct behaviour.
SELECT
    CAST(c.database_name AS varchar(256)) AS metric_item,
    CAST(
        CASE
            WHEN c.skip_reason = 'DATABASE_NOT_ONLINE' THEN 'NOT_ONLINE'
            WHEN c.skip_reason = 'AG_SECONDARY'        THEN 'AG_SECONDARY'
            -- The actual state, even when the database reports Query Store as off: OFF and
            -- ERROR are both "not capturing" and only one of them is somebody's decision.
            ELSE ISNULL(o.actual_state_desc, CASE WHEN c.desired_on = 0 THEN 'OFF' ELSE 'UNKNOWN' END)
        END AS varchar(32)
    ) AS metric_value,
    CAST('state' AS varchar(32)) AS metric_unit,
    CAST(
        CASE
            WHEN c.skip_reason IN ('DATABASE_NOT_ONLINE', 'QUERY_STORE_OFF') THEN 'WARNING'
            -- Configured read-write but actually read-only, or in error: it stopped capturing on
            -- its own and nothing said so. This is the case is_query_store_on cannot see.
            WHEN o.actual_state_desc = 'ERROR' THEN 'WARNING'
            WHEN o.desired_state_desc = 'READ_WRITE' AND o.actual_state_desc <> 'READ_WRITE' THEN 'WARNING'
            ELSE 'OK'
        END AS varchar(32)
    ) AS status,
    CAST(
        'db_name=' + c.database_name
        + ', db_state=' + c.db_state
        + ', query_store_on=' + CAST(c.desired_on AS varchar(1))
        + ', issue_type=' + ISNULL(c.skip_reason, 'NONE')
        -- Why it is not capturing, which "OFF" alone never said. TURNED_OFF is somebody's
        -- decision; the others are Query Store having stopped on its own.
        + ISNULL(', off_reason=' +
            CASE
                WHEN c.skip_reason = 'DATABASE_NOT_ONLINE' THEN 'DATABASE_NOT_ONLINE'
                WHEN c.skip_reason = 'AG_SECONDARY' THEN 'AG_SECONDARY'
                WHEN o.actual_state_desc = 'ERROR' THEN 'ERROR_STATE'
                WHEN o.readonly_reason & 65536 = 65536 THEN 'SIZE_LIMIT_REACHED'
                WHEN o.actual_state_desc = 'READ_WRITE' THEN NULL
                WHEN o.desired_state_desc = 'OFF' AND o.actual_state_desc = 'OFF' THEN 'TURNED_OFF'
                WHEN o.desired_state_desc <> 'OFF' AND o.actual_state_desc = 'OFF'
                    THEN 'STOPPED_WHILE_ENABLED'
                WHEN o.actual_state_desc IS NULL AND c.desired_on = 0 THEN 'TURNED_OFF'
                ELSE 'UNKNOWN'
            END, '')
        + ISNULL(', desired_state=' + o.desired_state_desc, '')
        + ISNULL(', actual_state=' + o.actual_state_desc, '')
        + ISNULL(', readonly_reason=' + CAST(o.readonly_reason AS varchar(20)), '')
        + ISNULL(', current_storage_mb=' + CAST(o.current_storage_size_mb AS varchar(20)), '')
        + ISNULL(', max_storage_mb=' + CAST(o.max_storage_size_mb AS varchar(20)), '')
        + ISNULL(', storage_used_pct=' + CAST(CAST(
              100.0 * o.current_storage_size_mb / NULLIF(o.max_storage_size_mb, 0)
              AS decimal(5, 1)) AS varchar(20)), '')
        + ISNULL(', capture_mode=' + o.query_capture_mode_desc, '')
        + ISNULL(', cleanup_mode=' + o.size_based_cleanup_mode_desc, '')
        + ISNULL(', wait_stats_capture=' + o.wait_stats_capture_mode_desc, '')
        + ISNULL(', stale_query_threshold_days=' + CAST(o.stale_query_threshold_days AS varchar(20)), '')
        + ISNULL(', interval_length_minutes=' + CAST(o.interval_length_minutes AS varchar(20)), '')
        + ISNULL(', flush_interval_seconds=' + CAST(o.flush_interval_seconds AS varchar(20)), '')
        + ISNULL(', max_plans_per_query=' + CAST(o.max_plans_per_query AS varchar(20)), '')
        + CASE
            WHEN c.skip_reason = 'QUERY_STORE_OFF' AND o.actual_state_desc = 'ERROR'
                THEN ' - Query Store is OFF and its last actual state was ERROR: it was enabled,'
                     + ' failed, and is now off. Captured data may still be present; it has to be'
                     + ' cleared (ALTER DATABASE ... SET QUERY_STORE CLEAR) before it can be turned'
                     + ' back on cleanly'
            WHEN c.skip_reason = 'QUERY_STORE_OFF'
                THEN ' - Query Store is switched off, so no query history is being captured and a'
                     + ' slowdown cannot be diagnosed later'
            WHEN c.skip_reason = 'DATABASE_NOT_ONLINE'
                THEN ' - database is not ONLINE, so nothing is captured and nothing can be queried'
            WHEN c.skip_reason = 'AG_SECONDARY'
                THEN ' - readable only on the primary replica; not a fault on this node'
            WHEN o.actual_state_desc = 'ERROR'
                THEN ' - Query Store is in ERROR state and has stopped capturing; it needs to be'
                     + ' set to READ_WRITE (and may need a cleanup) before it records anything again'
            WHEN o.desired_state_desc = 'READ_WRITE' AND o.actual_state_desc <> 'READ_WRITE'
                THEN ' - configured READ_WRITE but actually ' + ISNULL(o.actual_state_desc, 'UNKNOWN')
                     + CASE WHEN o.readonly_reason & 65536 = 65536
                            THEN ': the max_storage_size limit was reached, so it silently stopped capturing'
                            ELSE '' END
            ELSE ''
          END
        AS varchar(4000)
    ) AS message
FROM #qs_cov AS c
LEFT JOIN #qs_opt AS o
    ON o.database_name = c.database_name
ORDER BY
    CASE WHEN c.skip_reason IS NULL AND ISNULL(o.actual_state_desc, '') = 'READ_WRITE' THEN 1 ELSE 0 END,
    c.database_name ASC;
