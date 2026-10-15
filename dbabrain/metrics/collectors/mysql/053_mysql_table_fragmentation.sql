-- MAINTENANCE_INDEX_FRAGMENTATION (MySQL variant): tablespace space that InnoDB is holding but
-- not using.
--
-- InnoDB has no avg_fragmentation_in_percent. What it has is `data_free`: space already taken
-- from the filesystem inside the table's tablespace that only that table can reuse. After a
-- large DELETE or a long history of row churn, data_free grows and the .ibd file never shrinks —
-- the MySQL shape of the same question SQL Server answers with fragmentation, and the one
-- OPTIMIZE TABLE actually fixes.
--
-- Reported as free percent of the tablespace so a 40%-empty 50 GB table outranks a 90%-empty
-- 20 MB one. Only tables over 100 MB are considered: below that the reclaim is not worth the
-- table rebuild OPTIMIZE TABLE performs (it locks, and on a large table it locks for a long
-- time — which is why the action is stated rather than implied).
--
-- All of this comes from information_schema statistics, which for InnoDB are **estimates**
-- refreshed on ANALYZE. That is stated in the message rather than presented as exact.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT CONCAT(t.table_schema, '.', t.table_name)                                     AS metric_item,
           ROUND(100.0 * t.data_free / NULLIF(t.data_length + t.index_length + t.data_free, 0), 1)
                                                                                          AS metric_value,
           'percent'                                                                      AS metric_unit,
           CASE WHEN 100.0 * t.data_free / NULLIF(t.data_length + t.index_length + t.data_free, 0) >= 50
                THEN 'WARNING' ELSE 'OK' END                                              AS status,
           CONCAT('free_mb=', ROUND(t.data_free / 1048576, 1),
                  ', data_mb=', ROUND(t.data_length / 1048576, 1),
                  ', index_mb=', ROUND(t.index_length / 1048576, 1),
                  ', engine=', t.engine,
                  ' | action=OPTIMIZE TABLE (rebuilds and locks the table)')              AS message,
           1                                                                              AS sort_rank
    FROM information_schema.tables AS t
    WHERE t.table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
      AND t.table_type = 'BASE TABLE'
      AND t.data_length + t.index_length + t.data_free > 104857600
      AND 100.0 * t.data_free / NULLIF(t.data_length + t.index_length + t.data_free, 0) >= 25

    UNION ALL

    SELECT 'table_fragmentation :: summary'                                               AS metric_item,
           CAST(COUNT(*) AS CHAR)                                                         AS metric_value,
           'count'                                                                        AS metric_unit,
           CASE WHEN SUM(CASE WHEN 100.0 * t.data_free
                                   / NULLIF(t.data_length + t.index_length + t.data_free, 0) >= 50
                              THEN 1 ELSE 0 END) > 0
                THEN 'WARNING' ELSE 'OK' END                                              AS status,
           CONCAT('fragmented_tables(>=25% free, >100MB)=', COUNT(*),
                  ' | InnoDB data_free, an information_schema estimate')                  AS message,
           0                                                                              AS sort_rank
    FROM information_schema.tables AS t
    WHERE t.table_schema NOT IN ('information_schema', 'performance_schema', 'mysql', 'sys')
      AND t.table_type = 'BASE TABLE'
      AND t.data_length + t.index_length + t.data_free > 104857600
      AND 100.0 * t.data_free / NULLIF(t.data_length + t.index_length + t.data_free, 0) >= 25
) AS q
ORDER BY q.sort_rank, q.metric_item
LIMIT 50;
