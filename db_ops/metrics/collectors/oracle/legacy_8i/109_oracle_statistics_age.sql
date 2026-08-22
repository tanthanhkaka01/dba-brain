-- Oracle 8i legacy variant - MAINTENANCE_STATISTICS_AGE. How stale the optimizer statistics are.
--
-- The metric already had a 12c+ variant built on dba_tab_statistics, which does not exist here,
-- so every 8i instance was skipped entirely - on the release where the problem is worst. 8i
-- gathers statistics with ANALYZE rather than DBMS_STATS and records the result on
-- dba_tables.last_analyzed, which is all this needs.
--
-- A table that has never been analyzed is reported as its own case rather than as an infinite
-- age: on 8i that means the optimizer is running the rule-based path for it, which is a
-- different conversation from "the numbers are old".
--
-- Only tables with rows worth planning for are listed - a stale statistic on an empty lookup
-- table changes no plan, and including them turned this into a list nobody read. Status is
-- LOGGING, not WARNING: like the 12c+ variant this is an inventory to work through, and a stale
-- statistic stays stale until someone updates it, so it would otherwise alert forever
-- (audits/20260811_audit_repeating_metric_alerts.md explains why that pattern was stopped).
SELECT * FROM (
    SELECT
        owner || '.' || table_name AS metric_item,
        NVL(TO_CHAR(ROUND(SYSDATE - last_analyzed)), 'never') AS metric_value,
        'days' AS metric_unit,
        'LOGGING' AS status,
        'table=' || owner || '.' || table_name ||
            ', last_analyzed=' || NVL(TO_CHAR(last_analyzed, 'YYYY-MM-DD'), 'never') ||
            ', age_days=' || NVL(TO_CHAR(ROUND(SYSDATE - last_analyzed)), 'n/a') ||
            ', num_rows=' || NVL(TO_CHAR(num_rows), 'unknown') AS message
    FROM dba_tables
    WHERE owner NOT IN ('SYS', 'SYSTEM', 'OUTLN', 'DBSNMP', 'CTXSYS', 'MDSYS', 'ORDSYS', 'WMSYS',
                     'AURORA$JIS$UTILITY$', 'AURORA$ORB$UNAUTHENTICATED', 'OSE$HTTP$ADMIN',
                     'ORDPLUGINS', 'PERFSTAT', 'TRACESVR', 'REPADMIN')
      AND (last_analyzed IS NULL OR SYSDATE - last_analyzed > 30)
      AND (num_rows IS NULL OR num_rows > 1000)
    ORDER BY NVL(SYSDATE - last_analyzed, 99999) DESC
)
WHERE ROWNUM <= 200;
