-- POSTGRES_TABLE_BLOAT (PostgreSQL): dead tuples holding space that autovacuum has not reclaimed.
--
-- MAINTENANCE_INDEX_FRAGMENTATION's postgresql variant is marked unsupported with the reason
-- "PostgreSQL uses bloat, not fragmentation ... future bloat metric". This is that metric.
--
-- PostgreSQL never updates a row in place: an UPDATE writes a new tuple and marks the old one
-- dead, and DELETE only marks. Autovacuum reclaims them, so a high dead-tuple ratio is not
-- really "bloat is bad" — it is **autovacuum is not keeping up on this table**, which is the
-- actionable fact. Left alone it costs sequential scans that read dead rows, and it is the same
-- backlog that eventually shows up as XID wraparound pressure (POSTGRES_XID_WRAPAROUND).
--
-- Counts come from pg_stat_user_tables, which is a **statistics estimate**, not a page scan:
-- cheap enough to run often, and the message says so rather than presenting it as exact. The
-- honest alternative (pgstattuple) needs an extension and reads every page.
--
-- Only tables with more than 10 000 live rows are considered — on a small table the ratio swings
-- wildly and means nothing. WARNING at 20% dead, CRITICAL at 40%. A summary row is always
-- emitted so a healthy database reads OK rather than NO_DATA.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT schemaname || '.' || relname AS metric_item,
           to_char(round((100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0))::numeric, 1),
                   'FM990.0') AS metric_value,
           'percent' AS metric_unit,
           CASE WHEN 100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0) >= 40 THEN 'CRITICAL'
                ELSE 'WARNING' END AS status,
           'dead_tuples=' || n_dead_tup
             || ', live_tuples=' || n_live_tup
             || ', size=' || pg_size_pretty(pg_total_relation_size(relid))
             || ', last_autovacuum=' || COALESCE(last_autovacuum::text, 'never')
             || ', last_autoanalyze=' || COALESCE(last_autoanalyze::text, 'never')
             || ' | action=VACUUM (ANALYZE), or lower autovacuum_vacuum_scale_factor on this table'
             AS message,
           1 AS sort_rank
    FROM pg_stat_user_tables
    WHERE n_live_tup > 10000
      AND 100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0) >= 20

    UNION ALL

    SELECT 'table_bloat :: summary' AS metric_item,
           COUNT(*)::text AS metric_value,
           'count' AS metric_unit,
           CASE WHEN COUNT(*) FILTER (
                       WHERE 100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0) >= 40) > 0
                THEN 'CRITICAL'
                WHEN COUNT(*) > 0 THEN 'WARNING' ELSE 'OK' END AS status,
           'bloated_tables(>=20% dead, >10k rows)=' || COUNT(*)
             || ' | estimates from pg_stat_user_tables, not a page scan' AS message,
           0 AS sort_rank
    FROM pg_stat_user_tables
    WHERE n_live_tup > 10000
      AND 100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0) >= 20
) AS q
ORDER BY q.sort_rank, q.metric_item
LIMIT 50;
