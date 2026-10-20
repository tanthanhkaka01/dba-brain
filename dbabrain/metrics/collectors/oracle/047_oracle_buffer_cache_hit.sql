-- BUFFER_CACHE_HIT (Oracle 10g+): buffer cache hit ratio, in percent.
--
-- A minute in which more reads went to disk than the cache could serve: a full scan the cache
-- cannot hold, or a cache too small for the working set. A ratio is a symptom, not a cause - the
-- physical reads a second are in the message, and TOP_DISK_READ_SQL names the statements.
--
-- Read from v$sysmetric, group 2: Oracle's own 60-second interval, the value Enterprise Manager
-- charts. The 8i variant computes the same ratio from counters since startup, which only moves
-- over weeks; this one is the last minute. v$sysmetric is an in-memory V$ view, not AWR: Oracle's
-- licensing guide names V$ACTIVE_SESSION_HISTORY, DBA_HIST_* and DBA_ADDM_* as the Diagnostics
-- Pack's, and not it. It exists from 10g; 9i has no variant.
--
-- Thresholds are check_oracle_health's published defaults (ConSol, the Nagios / Icinga Oracle
-- plugin) - the operator asked for what other monitors do (2026-10-15, item 1.150) - one params
-- CTE each, so changing one is one number. Before this file a 9i+ database had no variant at all,
-- and ORACLE_PERFORMANCE_24H measured nothing there.
WITH params AS (
    SELECT 98 AS warning_below_pct, 95 AS critical_below_pct FROM dual
),
-- A ratio of a minute with almost no work is not a finding: a PDB that read nothing in the minute
-- answers 0, and the .251 lab's idle 23ai PDB read CRITICAL for it the minute it was switched on
-- (2026-10-19, item 1.179). Under this many logical reads a second the row is OK and says it was
-- not graded; the value is kept as read.
idle AS (
    SELECT 100 AS min_logical_reads_per_sec FROM dual
),
m AS (
    SELECT ROUND(value, 2) AS hit_pct, end_time,
           (SELECT MAX(value) FROM v$sysmetric WHERE metric_name = 'Logical Reads Per Sec' AND group_id = 2) AS logical_reads_per_sec
    FROM v$sysmetric
    WHERE metric_name = 'Buffer Cache Hit Ratio' AND group_id = 2
)
SELECT
    CAST('buffer_cache_hit_ratio' AS varchar2(256)) AS metric_item,
    TO_CHAR(m.hit_pct) AS metric_value,
    CAST('percent' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN NVL(m.logical_reads_per_sec, 0) < i.min_logical_reads_per_sec THEN 'OK'
        WHEN m.hit_pct < p.critical_below_pct THEN 'CRITICAL'
        WHEN m.hit_pct < p.warning_below_pct THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    CASE WHEN NVL(m.logical_reads_per_sec, 0) < i.min_logical_reads_per_sec
         THEN 'not graded - under ' || i.min_logical_reads_per_sec || ' logical reads a second in the minute; '
    END
    || 'buffer_cache_hit_pct=' || TO_CHAR(m.hit_pct)
        || ', logical_reads_per_sec=' || NVL(TO_CHAR(ROUND(m.logical_reads_per_sec, 2)), 'n/a')
        || ', interval_end=' || TO_CHAR(m.end_time, 'YYYY-MM-DD HH24:MI:SS')
        || ', physical_reads_per_sec=' || NVL((SELECT TO_CHAR(ROUND(value, 2)) FROM v$sysmetric
                                               WHERE metric_name = 'Physical Reads Per Sec' AND group_id = 2), 'n/a')
        || ', warning_below_pct=' || p.warning_below_pct || ', critical_below_pct=' || p.critical_below_pct AS message
FROM m CROSS JOIN params p CROSS JOIN idle i
UNION ALL
-- Inside a PDB v$sysmetric answers no rows (seen on the .251 26ai lab): this figure is the
-- instance's, shared by every container, and is read from the CDB root or a non-CDB. Not there
-- is not a failure (the operator, 2026-10-15) - one LOGGING row says where it is.
SELECT
    CAST('buffer_cache_hit_ratio' AS varchar2(256)),
    CAST(NULL AS varchar2(64)),
    CAST('percent' AS varchar2(32)),
    'LOGGING',
    'not readable in this container (con_id=' || SYS_CONTEXT('USERENV', 'CON_ID')
        || '): an instance-wide figure, measured on a target connected to the CDB root or a non-CDB'
FROM dual
WHERE NOT EXISTS (SELECT 1 FROM m)
