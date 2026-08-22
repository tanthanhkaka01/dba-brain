IF OBJECT_ID('tempdb..#errorlog') IS NOT NULL
    DROP TABLE #errorlog;

CREATE TABLE #errorlog
(
    LogDate datetime,
    ProcessInfo nvarchar(64),
    [Text] nvarchar(4000)
);

-- The 24-hour window is passed to xp_readerrorlog (parameter 5) instead of being applied to
-- the temp table afterwards, because "read it all, then filter" is only cheap on a log that
-- gets cycled. On 192.0.2.250 the current log had not been cycled since 2025-10-27 and held
-- 1,549,864 lines, of which 157,862 were from the last day: loading all of them took 190
-- seconds against this metric's 60-second timeout, so the metric had failed on every single run
-- for days and the server was effectively unmonitored for I/O errors and stack dumps. Filtering
-- inside the XP reads the same 24 hours in 18 seconds.
--
-- The WHERE below keeps its own LogDate test: this bounds the read, that one is the contract.
DECLARE @since_hours int = 24;
DECLARE @since datetime = DATEADD(hour, -@since_hours, GETDATE());

INSERT INTO #errorlog
EXEC master.dbo.xp_readerrorlog
    0, 1, NULL, NULL, @since, NULL, N'desc';

-- base_log/matched_errors used to be CTEs, and matched_errors was referenced three times
-- further down (the aggregate, the worst-line pick, and the per-type detail). A CTE is not
-- materialised: each reference re-runs the whole 26-predicate LIKE scan over every log line,
-- so the expensive part of this metric was being paid three times. On a server logging ~160k
-- lines a day that alone kept it over the 60-second timeout even after the read was bounded.
-- Scanning once into #matched and reading that three times is the same result for a third of
-- the work.
IF OBJECT_ID('tempdb..#matched') IS NOT NULL
    DROP TABLE #matched;

CREATE TABLE #matched
(
    LogDate     datetime,
    ProcessInfo nvarchar(64),
    [Text]      nvarchar(4000),
    error_type  varchar(32)
);

;WITH base_log AS
(
    SELECT
        LogDate,
        ProcessInfo,
        [Text]
    FROM #errorlog
    WHERE LogDate >= DATEADD(hour, -24, GETDATE())
      AND ISNULL([Text], N'') NOT LIKE N'%Machine supports memory error recovery%'
      AND ISNULL([Text], N'') NOT LIKE N'%SQL memory protection is enabled to recover from memory corruption%'
      AND ISNULL([Text], N'') NOT LIKE N'%This is an informational message only%'

      -- Ignore AG internal restore/seeding trace messages.
      AND NOT
      (
          [Text] LIKE N'%Always On: DebugTraceVarArgs%'
          AND [Text] LIKE N'%RESTORE T-SQL String for VDI Client%'
      )
)
INSERT INTO #matched (LogDate, ProcessInfo, [Text], error_type)
    SELECT
        LogDate,
        ProcessInfo,
        [Text],
        error_type =
            CASE
                WHEN [Text] LIKE N'%Error: 823%' THEN 'ERROR_823_IO'
                WHEN [Text] LIKE N'%Error: 824%' THEN 'ERROR_824_LOGICAL_IO'
                WHEN [Text] LIKE N'%Error: 825%' THEN 'ERROR_825_RETRY_IO'
                WHEN [Text] LIKE N'%Error: 17886%' THEN 'ERROR_17886_CONNECTION_SESSION'
                WHEN [Text] LIKE N'%I/O requests taking longer%' THEN 'IO_STALL_15S'
                WHEN [Text] LIKE N'%I/O error%' THEN 'IO_ERROR'
                WHEN [Text] LIKE N'%stack dump%' THEN 'STACK_DUMP'
                WHEN [Text] LIKE N'%insufficient memory%' THEN 'MEMORY_PRESSURE'
                WHEN [Text] LIKE N'%out of memory%' THEN 'OUT_OF_MEMORY'
                WHEN [Text] LIKE N'%Severity: 25%' THEN 'SEVERITY_25'
                WHEN [Text] LIKE N'%Severity: 24%' THEN 'SEVERITY_24'
                WHEN [Text] LIKE N'%Severity: 23%' THEN 'SEVERITY_23'
                WHEN [Text] LIKE N'%Severity: 22%' THEN 'SEVERITY_22'
                WHEN [Text] LIKE N'%Severity: 21%' THEN 'SEVERITY_21'
                WHEN [Text] LIKE N'%Severity: 20%' THEN 'SEVERITY_20'
                WHEN [Text] LIKE N'%Severity: 19%' THEN 'SEVERITY_19'
                WHEN [Text] LIKE N'%Severity: 18%' THEN 'SEVERITY_18'
                WHEN [Text] LIKE N'%Severity: 17%' THEN 'SEVERITY_17'

                WHEN [Text] LIKE N'%corrupt%'
                  OR [Text] LIKE N'%corruption%'
                  OR [Text] LIKE N'%consistency-based I/O error%'
                  OR [Text] LIKE N'%torn page%'
                  OR [Text] LIKE N'%checksum error%'
                  OR [Text] LIKE N'%checksum failure%'
                  OR [Text] LIKE N'%incorrect checksum%'
                  OR [Text] LIKE N'%does not match the computed checksum%'
                THEN 'CORRUPTION'

                ELSE 'OTHER_CRITICAL'
            END
    FROM base_log
    WHERE
          [Text] LIKE N'%Severity: 17%'
       OR [Text] LIKE N'%Severity: 18%'
       OR [Text] LIKE N'%Severity: 19%'
       OR [Text] LIKE N'%Severity: 20%'
       OR [Text] LIKE N'%Severity: 21%'
       OR [Text] LIKE N'%Severity: 22%'
       OR [Text] LIKE N'%Severity: 23%'
       OR [Text] LIKE N'%Severity: 24%'
       OR [Text] LIKE N'%Severity: 25%'
       OR [Text] LIKE N'%I/O requests taking longer%'
       OR [Text] LIKE N'%I/O error%'
       OR [Text] LIKE N'%Error: 823%'
       OR [Text] LIKE N'%Error: 824%'
       OR [Text] LIKE N'%Error: 825%'
       OR [Text] LIKE N'%Error: 17886%'
       OR [Text] LIKE N'%corrupt%'
       OR [Text] LIKE N'%corruption%'
       OR [Text] LIKE N'%consistency-based I/O error%'
       OR [Text] LIKE N'%torn page%'
       OR [Text] LIKE N'%checksum error%'
       OR [Text] LIKE N'%checksum failure%'
       OR [Text] LIKE N'%incorrect checksum%'
       OR [Text] LIKE N'%does not match the computed checksum%'
       OR [Text] LIKE N'%stack dump%'
       OR [Text] LIKE N'%insufficient memory%'
       OR [Text] LIKE N'%out of memory%';

;WITH agg AS
(
    SELECT
        COUNT(*) AS error_count_24h,
        MAX(LogDate) AS last_error_time,
        SUM
        (
            CASE
                WHEN error_type IN
                (
                    'ERROR_823_IO',
                    'ERROR_824_LOGICAL_IO',
                    'ERROR_825_RETRY_IO',
                    'STACK_DUMP',
                    'CORRUPTION',
                    'OUT_OF_MEMORY',
                    'SEVERITY_21',
                    'SEVERITY_22',
                    'SEVERITY_23',
                    'SEVERITY_24',
                    'SEVERITY_25'
                )
                THEN 1
                ELSE 0
            END
        ) AS critical_count_24h
    FROM #matched
),
by_type AS
(
    SELECT
        error_type,
        COUNT(*) AS error_count,
        MAX(LogDate) AS last_error_time
    FROM #matched
    GROUP BY error_type
),
type_message AS
(
    SELECT
        breakdown =
            STUFF
            (
                (
                    SELECT
                        '; ' + b.error_type
                        + '=' + CAST(b.error_count AS varchar(20))
                        + ' last='
                        + ISNULL
                        (
                            CONVERT(varchar(19), b.last_error_time, 120),
                            'NULL'
                        )
                    FROM by_type b
                    ORDER BY
                        b.error_count DESC,
                        b.error_type
                    FOR XML PATH(''), TYPE
                ).value('.', 'varchar(max)'),
                1,
                2,
                ''
            )
),
last_error AS
(
    SELECT TOP (1)
        LogDate,
        error_type,
        ProcessInfo,
        [Text]
    FROM #matched
    ORDER BY LogDate DESC
)
SELECT
    CAST('errorlog' AS varchar(256)) AS metric_item,
    CAST(ISNULL(a.error_count_24h, 0) AS varchar(32)) AS metric_value,
    CAST('errors_24h' AS varchar(32)) AS metric_unit,
    CASE
        WHEN ISNULL(a.critical_count_24h, 0) > 0 THEN 'CRITICAL'
        WHEN ISNULL(a.error_count_24h, 0) > 0 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    CAST
    (
        'errors_24h='
        + CAST(ISNULL(a.error_count_24h, 0) AS varchar(20))
        + ', critical_errors_24h='
        + CAST(ISNULL(a.critical_count_24h, 0) AS varchar(20))
        + ', last_error_time='
        + ISNULL(CONVERT(varchar(19), a.last_error_time, 120), 'NULL')
        + ', breakdown=['
        + ISNULL(t.breakdown, 'none')
        + ']'
        + ', latest_type='
        + ISNULL(l.error_type, 'none')
        + ', latest_process='
        + ISNULL(l.ProcessInfo, 'NULL')
        + ', latest_text='
        + ISNULL
        (
            LEFT
            (
                REPLACE
                (
                    REPLACE(l.[Text], CHAR(13), ' '),
                    CHAR(10),
                    ' '
                ),
                1000
            ),
            'NULL'
        )
        AS varchar(4000)
    ) AS message
FROM agg a
CROSS JOIN type_message t
LEFT JOIN last_error l
    ON 1 = 1;