-- SERVER_DIAGNOSTICS (SQL Server 2008 / 2008 R2): memory dumps diagnostic collector.
--
-- Why memory dumps are graded by age:
-- Memory dumps are SQL Server's internal incident records. An access violation (AV), an assertion
-- failure, a non-yielding scheduler, or an unresolvable latch timeout triggers an engine minidump
-- and writes a .mdmp file to the log directory. The instance frequently recovers and carries on as
-- if nothing happened, while internal state may have become corrupt or unstable.
--
-- Following the pattern established for the SQL error log (017_sqlserver_errorlog_recent_critical.sql),
-- recent dumps created within @dump_recent_minutes (default 120 minutes / 2 hours) page as CRITICAL.
-- Dumps that are older but still within the retention window @dump_window_days (default 7 days) alert
-- as WARNING so they remain visible on daily inspection dashboards without waking on-call staff for
-- a week. Dumps older than 7 days are not reported per-item. When no dumps exist within the window,
-- a single OK row 'memory_dumps' reports the total count of dumps retained in the DMV and the
-- timestamp of the newest dump.
--
-- Why sp_server_diagnostics is omitted in this legacy variant:
-- sp_server_diagnostics was introduced in SQL Server 2012 for Failover Cluster Instances and Always On
-- Availability Groups. It does not exist on SQL Server 2008 or 2008 R2. Consequently, this legacy
-- variant has no component rows and monitors memory dumps only.
--
-- Why sys.dm_server_memory_dumps is guarded via dynamic SQL:
-- sys.dm_server_memory_dumps was introduced in SQL Server 2008 R2 SP1. Earlier builds (SQL Server 2008
-- RTM/SP and 2008 R2 RTM) do not have this DMV and a static query reference would fail compilation
-- at bind time with error 208. Following the convention in legacy_2008r2/002_sqlserver_database_status.sql,
-- the DMV is guarded with an OBJECT_ID check and dynamic SQL, reporting one LOGGING row
-- ("not readable on this build") when the DMV is missing.
SET NOCOUNT ON;

DECLARE @dump_recent_minutes int = 120;
DECLARE @dump_window_days int = 7;

DECLARE @status TABLE (
    metric_item varchar(256),
    metric_value varchar(32),
    metric_unit varchar(32),
    status varchar(32),
    message varchar(1000),
    severity_rank int
);

IF OBJECT_ID('sys.dm_server_memory_dumps') IS NULL
BEGIN
    INSERT INTO @status (metric_item, metric_value, metric_unit, status, message, severity_rank)
    VALUES (
        'memory_dumps',
        'UNKNOWN',
        NULL,
        'LOGGING',
        'status_desc=UNKNOWN, note=not readable on this build (sys.dm_server_memory_dumps requires SQL Server 2008 R2 SP1+)',
        4
    );
END
ELSE
BEGIN
    -- Dynamic SQL because sys.dm_server_memory_dumps does not exist before 2008 R2 SP1.
    -- Compiling a static reference on 2008 R2 RTM or 2008 would fail at bind time.
    DECLARE @dump_sql nvarchar(max);
    SET @dump_sql = N'
    WITH parsed AS
    (
        SELECT
            filename,
            CASE
                WHEN CHARINDEX(''\'', REVERSE(filename)) > 0
                    THEN RIGHT(filename, CHARINDEX(''\'', REVERSE(filename)) - 1)
                WHEN CHARINDEX(''/'', REVERSE(filename)) > 0
                    THEN RIGHT(filename, CHARINDEX(''/'', REVERSE(filename)) - 1)
                ELSE filename
            END AS file_name_only,
            creation_time,
            size_in_bytes,
            CAST(ROUND(size_in_bytes / 1048576.0, 2) AS decimal(19,2)) AS size_mb,
            CASE
                WHEN DATEDIFF(minute, creation_time, SYSDATETIMEOFFSET()) < 0 THEN 0
                ELSE DATEDIFF(minute, creation_time, SYSDATETIMEOFFSET())
            END AS age_minutes
        FROM sys.dm_server_memory_dumps
    ),
    in_window AS
    (
        SELECT
            file_name_only,
            creation_time,
            size_mb,
            age_minutes,
            CASE
                WHEN age_minutes <= @dump_recent_minutes THEN ''CRITICAL''
                ELSE ''WARNING''
            END AS dump_status,
            CASE
                WHEN age_minutes <= @dump_recent_minutes THEN 1
                ELSE 2
            END AS rank_num
        FROM parsed
        WHERE creation_time >= DATEADD(day, -@dump_window_days, SYSDATETIMEOFFSET())
    )
    SELECT
        CAST(''memory_dump :: '' + file_name_only AS varchar(256)) AS metric_item,
        CAST(size_mb AS varchar(32)) AS metric_value,
        CAST(''MB'' AS varchar(32)) AS metric_unit,
        CAST(dump_status AS varchar(32)) AS status,
        CAST(
            ''file='' + file_name_only
            + '', created='' + CONVERT(varchar(19), creation_time, 120)
            + '', age_minutes='' + CAST(age_minutes AS varchar(32))
            + '', size_mb='' + CAST(size_mb AS varchar(32))
            AS varchar(1000)
        ) AS message,
        rank_num AS severity_rank
    FROM in_window

    UNION ALL

    SELECT
        CAST(''memory_dumps'' AS varchar(256)) AS metric_item,
        CAST(COUNT(*) AS varchar(32)) AS metric_value,
        CAST(''count'' AS varchar(32)) AS metric_unit,
        CAST(''OK'' AS varchar(32)) AS status,
        CAST(
            ''dumps_total='' + CAST(COUNT(*) AS varchar(32))
            + '', newest_dump='' + ISNULL(CONVERT(varchar(19), MAX(creation_time), 120), ''none'')
            + '', dump_window_days='' + CAST(@dump_window_days AS varchar(32))
            + '', note=no memory dumps within window''
            AS varchar(1000)
        ) AS message,
        3 AS severity_rank
    FROM sys.dm_server_memory_dumps
    -- HAVING, not WHERE: an aggregate with no GROUP BY returns its one row even when WHERE
    -- removed every input, so this said "0 dumps, none" beside the dumps listed above.
    HAVING NOT EXISTS (
        SELECT 1
        FROM sys.dm_server_memory_dumps
        WHERE creation_time >= DATEADD(day, -@dump_window_days, SYSDATETIMEOFFSET())
    );';

    INSERT INTO @status (metric_item, metric_value, metric_unit, status, message, severity_rank)
    EXEC sys.sp_executesql @dump_sql,
        N'@dump_recent_minutes int, @dump_window_days int',
        @dump_recent_minutes = @dump_recent_minutes,
        @dump_window_days = @dump_window_days;
END;

SELECT
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
FROM @status
ORDER BY
    severity_rank,
    metric_item;
