-- MAINTENANCE_STATISTICS_AGE (Oracle variant): tables whose optimizer statistics are stale.
--
-- Same question as the SQL Server variant (062) — are the numbers the optimizer plans with still
-- true — answered with what Oracle exposes. Oracle keeps this directly: dba_tab_statistics has
-- last_analyzed, and stale_stats is Oracle's own verdict, set when monitored DML since the last
-- gather passes the staleness threshold (10% by default).
--
-- Both are reported because they catch different failures: stale_stats = 'YES' is a table that
-- changed a lot recently, while a very old last_analyzed with no DML is a table that may never
-- have been gathered at all — the second one does not raise stale_stats, and it is the one that
-- produces the spectacularly bad plan after a release.
--
-- Oracle-maintained schemas are excluded through dba_users.oracle_maintained rather than a
-- hand-written owner list, matching 061_oracle_index_health: the list would need editing for
-- every release that adds an internal schema.
--
-- Only tables over 1000 rows, to match the SQL Server variant's floor. A summary row is always
-- emitted so a well-maintained instance reads OK rather than NO_DATA.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT owner || '.' || table_name AS metric_item,
           TO_CHAR(NVL(ROUND(SYSDATE - last_analyzed), -1)) AS metric_value,
           'days' AS metric_unit,
           'WARNING' AS status,
           'rows=' || TO_CHAR(num_rows)
             || ', last_analyzed=' || NVL(TO_CHAR(last_analyzed, 'YYYY-MM-DD HH24:MI'), 'never')
             || ', stale_stats=' || NVL(stale_stats, 'UNKNOWN')
             || ' | action=DBMS_STATS.GATHER_TABLE_STATS' AS message,
           1 AS sort_rank
    FROM dba_tab_statistics
    WHERE object_type = 'TABLE'
      AND num_rows > 1000
      AND owner NOT IN (SELECT username FROM dba_users WHERE oracle_maintained = 'Y')
      AND (stale_stats = 'YES'
           OR last_analyzed IS NULL
           OR last_analyzed < SYSDATE - 30)

    UNION ALL

    SELECT 'statistics_age :: summary' AS metric_item,
           TO_CHAR(COUNT(*)) AS metric_value,
           'count' AS metric_unit,
           CASE WHEN COUNT(*) > 0 THEN 'WARNING' ELSE 'OK' END AS status,
           'stale_or_old_tables(>1000 rows)=' || TO_CHAR(COUNT(*))
             || ' | stale_stats=YES, never gathered, or last_analyzed older than 30 days' AS message,
           0 AS sort_rank
    FROM dba_tab_statistics
    WHERE object_type = 'TABLE'
      AND num_rows > 1000
      AND owner NOT IN (SELECT username FROM dba_users WHERE oracle_maintained = 'Y')
      AND (stale_stats = 'YES'
           OR last_analyzed IS NULL
           OR last_analyzed < SYSDATE - 30)
) q
ORDER BY sort_rank, metric_item
FETCH FIRST 50 ROWS ONLY
