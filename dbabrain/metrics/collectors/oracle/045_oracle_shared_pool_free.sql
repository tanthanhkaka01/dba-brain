-- SHARED_POOL_FREE (Oracle 10g+): free memory in the shared pool, in percent.
--
-- Running out of it is ORA-04031, which LOG_RECENT_CRITICAL also pages on when it happens; this is
-- the minute before. Under automatic memory management (sga_target / memory_target) Oracle grows
-- the pool on demand, so a low figure there is less of a warning than with a fixed pool - the
-- message says which the instance runs.
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
    SELECT 10 AS warning_below_pct, 5 AS critical_below_pct FROM dual
),
m AS (
    SELECT ROUND(value, 2) AS free_pct, end_time
    FROM v$sysmetric
    WHERE metric_name = 'Shared Pool Free %' AND group_id = 2
)
SELECT
    CAST('shared_pool_free' AS varchar2(256)) AS metric_item,
    TO_CHAR(m.free_pct) AS metric_value,
    CAST('percent' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN m.free_pct < p.critical_below_pct THEN 'CRITICAL'
        WHEN m.free_pct < p.warning_below_pct THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'shared_pool_free_pct=' || TO_CHAR(m.free_pct)
        || ', interval_end=' || TO_CHAR(m.end_time, 'YYYY-MM-DD HH24:MI:SS')
        || ', sga_target=' || NVL((SELECT value FROM v$parameter WHERE name = 'sga_target'), 'n/a')
        || ', memory_target=' || NVL((SELECT value FROM v$parameter WHERE name = 'memory_target'), 'n/a')
        || ', warning_below_pct=' || p.warning_below_pct || ', critical_below_pct=' || p.critical_below_pct AS message
FROM m CROSS JOIN params p
UNION ALL
-- Inside a PDB v$sysmetric answers no rows (seen on the .251 26ai lab): this figure is the
-- instance's, shared by every container, and is read from the CDB root or a non-CDB. Not there
-- is not a failure (the operator, 2026-10-15) - one LOGGING row says where it is.
SELECT
    CAST('shared_pool_free' AS varchar2(256)),
    CAST(NULL AS varchar2(64)),
    CAST('percent' AS varchar2(32)),
    'LOGGING',
    'not readable in this container (con_id=' || SYS_CONTEXT('USERENV', 'CON_ID')
        || '): an instance-wide figure, measured on a target connected to the CDB root or a non-CDB'
FROM dual
WHERE NOT EXISTS (SELECT 1 FROM m)
