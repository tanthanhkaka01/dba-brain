-- LOG_RECENT_CRITICAL (Oracle variant) - recent critical alert log entries.
--
-- Counterpart of 017_sqlserver_errorlog_recent_critical.sql. The Oracle alert log is reachable from
-- SQL through the ADR view v$diag_alert_ext (11g and newer), so this needs no file access on the
-- host - which is what the old "needs ADR/file access" note assumed.
--
-- message_level follows ADR severity: 1 = CRITICAL, 2 = SEVERE. Both are included; anything higher
-- (informational) is noise for this metric.
--
-- ORA-1 (unique constraint violated) and similar application-level errors are excluded: they are
-- application bugs surfacing in the alert log, not database health, and they can arrive in volumes
-- that bury real incidents. Genuine infrastructure errors (ORA-27xxx I/O, ORA-6xx internal,
-- ORA-16xxx Data Guard, ORA-2xx control file/redo) are kept.
--
-- The row cap is deliberate: v$diag_alert_ext is an external-table read over the whole XML alert log
-- and an unbounded scan on a long-lived instance can exceed the metric timeout.
--
-- ORA-00600, ORA-07445, ORA-01578, ORA-01110, ORA-04031, ORA-00257, ORA-00353, ORA-00470/ORA-00471,
-- and ORA-27102 are internal crashes, corruptions, and fatal resource exhausts that require a DBA to
-- be paged immediately (CRITICAL); any other unsuppressed ORA- line is WARNING.
--
-- All thresholds are declared in the top-level params CTE so every number appears once.

WITH params AS (
    SELECT
        1  AS lookback_days,
        50 AS max_rows
    FROM dual
)
SELECT
    CAST(TO_CHAR(a.originating_timestamp, 'YYYY-MM-DD HH24:MI:SS') AS varchar2(256)) AS metric_item,
    CAST(NVL(REGEXP_SUBSTR(a.message_text, 'ORA-[0-9]+'), 'ALERT') AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    CASE
        WHEN REGEXP_LIKE(a.message_text, 'ORA-0*(600|7445|1578|1110|4031|257|353|470|471|27102)([^0-9]|$)') THEN 'CRITICAL'
        ELSE 'WARNING'
    END AS status,
    'time=' || TO_CHAR(a.originating_timestamp, 'YYYY-MM-DD HH24:MI:SS')
        || ', level=' || TO_CHAR(a.message_level)
        || ', component=' || NVL(a.component_id, '')
        || ', error=' || NVL(REGEXP_SUBSTR(a.message_text, 'ORA-[0-9]+'), 'none')
        || ', text=' || SUBSTR(REPLACE(REPLACE(a.message_text, CHR(10), ' '), CHR(13), ' '), 1, 400) AS message
FROM v$diag_alert_ext a
CROSS JOIN params p
WHERE a.message_level <= 2
  AND a.originating_timestamp > SYSTIMESTAMP - NUMTODSINTERVAL(p.lookback_days, 'DAY')
  AND NOT REGEXP_LIKE(a.message_text, 'ORA-0*(1|1403|1422|1476|942|904)[^0-9]')
  AND ROWNUM <= p.max_rows
ORDER BY a.originating_timestamp DESC;
