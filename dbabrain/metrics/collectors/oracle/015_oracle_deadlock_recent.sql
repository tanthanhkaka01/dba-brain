-- LOCK_DEADLOCK_RECENT (Oracle 11g+): deadlocks in the last 24 hours, from the alert log (1.149's
-- port, 2026-10-16).
--
-- Oracle breaks a deadlock by failing one statement with ORA-00060 and writes the line to the alert
-- log, with the trace file that holds the graph. v$diag_alert_ext reads that log from SQL (11g and
-- later, an in-memory V$ view over ADR - no Diagnostics Pack), so the count needs no file access.
-- SQL Server's rule, ported as it is so the two engines mean the same thing: five or more in 24
-- hours CRITICAL, any WARNING, none OK; one row, item 'deadlock'.
--
-- The line is matched by its text and at any message level: ORA-00060 is written at level 16, an
-- ordinary message, which is why LOG_RECENT_CRITICAL - levels 1 and 2 - never counted it.
--
-- v$diag_alert_ext scans the whole XML alert log on every read; on a long-lived instance that is
-- seconds. The variant runs hourly (its repeat_interval in the catalogue), as LOG_RECENT_CRITICAL
-- does, not at the metric's 150 s set by SQL Server's ring buffer. A deadlock still shows within the
-- hour and stays counted for the day. The cumulative count since startup is in the Workload table
-- (v$sysstat enqueue deadlocks).
WITH params AS (
    SELECT 24 AS window_hours, 5 AS critical_count FROM dual
),
d AS (
    SELECT COUNT(*) AS deadlocks,
           MAX(a.originating_timestamp) AS last_at,
           MAX(SUBSTR(REPLACE(REPLACE(a.message_text, CHR(10), ' '), CHR(13), ' '), 1, 300))
               KEEP (DENSE_RANK LAST ORDER BY a.originating_timestamp) AS last_text
    FROM v$diag_alert_ext a
    CROSS JOIN params p
    WHERE a.originating_timestamp > SYSTIMESTAMP - NUMTODSINTERVAL(p.window_hours, 'HOUR')
      AND a.message_text LIKE '%ORA-00060%'
)
SELECT
    CAST('deadlock' AS varchar2(256)) AS metric_item,
    TO_CHAR(d.deadlocks) AS metric_value,
    CAST('deadlocks_24h' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN d.deadlocks >= p.critical_count THEN 'CRITICAL'
        WHEN d.deadlocks > 0 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'deadlocks_24h=' || TO_CHAR(d.deadlocks)
        || ', source=v$diag_alert_ext'
        || ', last_deadlock=' || NVL(TO_CHAR(d.last_at, 'YYYY-MM-DD HH24:MI:SS'), 'none')
        || ', last_text=' || NVL(d.last_text, 'none') AS message
FROM d
CROSS JOIN params p
