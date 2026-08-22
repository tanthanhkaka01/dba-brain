-- MAINTENANCE_STATISTICS_AGE (MySQL variant): tables whose InnoDB statistics were never
-- refreshed, or refreshed long ago.
--
-- Same question as the SQL Server (062) and Oracle (064) variants. MySQL keeps persistent index
-- statistics in mysql.innodb_table_stats, whose last_update is set by ANALYZE TABLE and by the
-- automatic recalculation that fires after roughly 10% of rows change. A table missing from that
-- table has never had its stats persisted at all — the worst case, and invisible if you only
-- look at ages.
--
-- Only tables over 1000 rows, matching the other variants' floor. Stale = never recorded, or
-- last_update older than 30 days.
--
-- mysql.innodb_table_stats needs SELECT on the mysql schema; a monitoring user without it gets
-- an error rather than a wrong answer, which is the right failure — a silent empty result would
-- read as "all statistics are fresh".
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT CONCAT(t.table_schema, '.', t.table_name)                                      AS metric_item,
           CAST(COALESCE(DATEDIFF(NOW(), s.last_update), -1) AS CHAR)                     AS metric_value,
           'days'                                                                          AS metric_unit,
           'WARNING'                                                                       AS status,
           CONCAT('rows=', COALESCE(t.table_rows, 0),
                  ', last_update=', COALESCE(CAST(s.last_update AS CHAR), 'never recorded'),
                  ', engine=', t.engine,
                  ' | action=ANALYZE TABLE')                                               AS message,
           1                                                                               AS sort_rank
    FROM information_schema.tables AS t
    LEFT JOIN mysql.innodb_table_stats AS s
           ON s.database_name = t.table_schema AND s.table_name = t.table_name
    WHERE t.table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
      AND t.table_type = 'BASE TABLE'
      AND t.table_rows > 1000
      AND (s.last_update IS NULL OR s.last_update < NOW() - INTERVAL 30 DAY)

    UNION ALL

    SELECT 'statistics_age :: summary'                                                     AS metric_item,
           CAST(COUNT(*) AS CHAR)                                                          AS metric_value,
           'count'                                                                         AS metric_unit,
           CASE WHEN COUNT(*) > 0 THEN 'WARNING' ELSE 'OK' END                             AS status,
           CONCAT('stale_or_never_analyzed(>1000 rows)=', COUNT(*),
                  ' | never recorded in mysql.innodb_table_stats, or older than 30 days')   AS message,
           0                                                                               AS sort_rank
    FROM information_schema.tables AS t
    LEFT JOIN mysql.innodb_table_stats AS s
           ON s.database_name = t.table_schema AND s.table_name = t.table_name
    WHERE t.table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
      AND t.table_type = 'BASE TABLE'
      AND t.table_rows > 1000
      AND (s.last_update IS NULL OR s.last_update < NOW() - INTERVAL 30 DAY)
) AS q
ORDER BY q.sort_rank, q.metric_item
LIMIT 50;
