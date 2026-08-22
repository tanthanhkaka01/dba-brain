-- MAINTENANCE_INDEX_USAGE (PostgreSQL): every index in the connected database with its usage
-- counters, so "should this index exist at all" can be answered.
--
-- PostgreSQL is the one engine in this estate whose catalog answers the question properly.
-- pg_stat_user_indexes.idx_scan is a real per-index read counter, and from PG 16 last_idx_scan
-- says *when* it was last read - which SQL Server cannot tell you at all.
--
-- THREE DIFFERENCES FROM THE SQL SERVER VARIANT, none of them cosmetic:
--
-- 1. **The counters do not reset on restart.** SQL Server's DMVs are zeroed by every failover, so
--    that variant spends most of its length warning about short samples. Here the counters
--    accumulate until someone calls pg_stat_reset() or a crash discards the stats file, and
--    pg_stat_database.stats_reset is the clock that says which. A NULL there means never reset:
--    the sample covers the whole life of the statistics, which is the strongest evidence for a
--    drop this estate can produce. It is reported either way so nobody has to assume.
--
-- 2. **There is no per-index write counter.** SQL Server's user_updates is the maintenance cost of
--    an index; PostgreSQL keeps write counters per *table* only. So the cost is reported as the
--    table's n_tup_ins + n_tup_upd + n_tup_del, and an unread index on a written table is the
--    expensive case - it is being maintained on every one of those writes for nothing. That is
--    what separates UNUSED from COLD here, rather than SQL Server's "has no row in the DMV".
--
-- 3. **An INVALID index is worse than a disabled one.** A failed CREATE INDEX CONCURRENTLY leaves
--    an index the planner refuses to use and the writer still maintains: all of the cost, none of
--    the benefit, and no error anywhere. It is reported as is_disabled=1 so it lands in the
--    report's disabled section, with its own action text - the fix is DROP and rebuild, never
--    REBUILD, because PostgreSQL has no REBUILD.
--
-- Never a drop candidate, whatever the counters say: a primary key, an index backing a UNIQUE or
-- EXCLUDE constraint, or any unique index. The first two cannot even be dropped directly - the
-- constraint owns them, so DROP INDEX fails and the action text says DROP CONSTRAINT instead.
-- Getting that wrong puts an instruction in a report that does not work and should not be run.
--
-- SCOPE: the connected database only. PostgreSQL cannot read another database's catalog from one
-- connection, so every summary row states which database it covered - a page that silently
-- described one database while looking like the whole cluster would be worse than no page.
--
-- NO CLUSTER-WIDE SUMMARY ROW. This SQL is declared `per_database`, so the collector runs it once
-- per database and anything calling itself a total would be emitted once per database under the
-- same metric_item — three rows claiming to be the server total, of which the report would keep
-- whichever landed last. The per-database summaries above are the truth, and
-- `index_report.collect_index_rows` adds them up.
--
-- Status stays OK except for invalid indexes: this is an inventory for a review, not an alert.
-- last_idx_scan is read through to_jsonb so the metric still runs on PG 12-15, where the column
-- does not exist and naming it directly would fail the whole statement.

WITH stats AS (
    SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()
),
idx AS (
    SELECT
        s.schemaname,
        s.relname                                        AS table_name,
        s.indexrelname                                   AS index_name,
        s.indexrelid,
        s.idx_scan,
        s.idx_tup_read,
        s.idx_tup_fetch,
        (to_jsonb(s.*) ->> 'last_idx_scan')              AS last_idx_scan,
        i.indisunique,
        i.indisprimary,
        i.indisvalid,
        i.indisready,
        (i.indpred IS NOT NULL)                          AS is_partial,
        am.amname                                        AS access_method,
        pg_relation_size(s.indexrelid)                   AS index_bytes,
        -- A constraint owning the index is what makes DROP INDEX fail. 'p' primary, 'u' unique,
        -- 'x' exclusion - all three own their index and all three need DROP CONSTRAINT.
        EXISTS (SELECT 1 FROM pg_constraint c
                 WHERE c.conindid = s.indexrelid AND c.contype IN ('p', 'u', 'x')) AS constraint_backed,
        COALESCE(t.n_tup_ins, 0) + COALESCE(t.n_tup_upd, 0) + COALESCE(t.n_tup_del, 0) AS table_writes,
        GREATEST(COALESCE(t.last_analyze, '-infinity'::timestamptz),
                 COALESCE(t.last_autoanalyze, '-infinity'::timestamptz))               AS last_analyzed
    FROM pg_stat_user_indexes s
    JOIN pg_index i    ON i.indexrelid = s.indexrelid
    JOIN pg_class ic   ON ic.oid = s.indexrelid
    JOIN pg_am am      ON am.oid = ic.relam
    LEFT JOIN pg_stat_user_tables t ON t.relid = s.relid
),
classified AS (
    SELECT idx.*,
           CASE WHEN idx.idx_scan > 0        THEN 'USED'
                WHEN idx.table_writes > 0    THEN 'UNUSED'
                ELSE                              'COLD'
           END AS kind,
           -- The same exclusions the SQL Server variant uses, spelled in PostgreSQL's terms.
           -- There is no clustered index here, so there is nothing to exclude for being the
           -- table itself.
           (NOT idx.indisunique AND NOT idx.indisprimary AND NOT idx.constraint_backed
            AND idx.idx_scan = 0) AS droppable
    FROM idx
)
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        current_database() || '.' || c.schemaname || '.' || c.table_name || '.' || c.index_name AS metric_item,
        c.idx_scan::text        AS metric_value,
        'idx_scan'              AS metric_unit,
        CASE WHEN NOT c.indisvalid OR NOT c.indisready THEN 'WARNING' ELSE 'OK' END AS status,
        c.kind
            || ': db=' || current_database()
            || ', schema=' || c.schemaname
            || ', table=' || c.table_name
            || ', index_name=' || c.index_name
            || ', index_id=' || c.indexrelid::text
            || ', type_desc=' || c.access_method
            || ', is_unique=' || CASE WHEN c.indisunique THEN '1' ELSE '0' END
            || ', is_primary_key=' || CASE WHEN c.indisprimary THEN '1' ELSE '0' END
            || ', is_unique_constraint=' || CASE WHEN c.constraint_backed AND NOT c.indisprimary THEN '1' ELSE '0' END
            -- An invalid index is reported as disabled so it reaches the report's disabled
            -- section; the action text below is what makes the difference visible.
            || ', is_disabled=' || CASE WHEN c.indisvalid AND c.indisready THEN '0' ELSE '1' END
            || ', has_filter=' || CASE WHEN c.is_partial THEN '1' ELSE '0' END
            -- idx_scan is PostgreSQL's ONE read counter. It is reported under its own name and
            -- also as user_seeks, which is what the shared report parser keys the drop rule on;
            -- user_scans/user_lookups stay 0 because this engine has no such split and inventing
            -- one would put numbers on the page that no catalog produced.
            || ', idx_scan=' || c.idx_scan::text
            || ', idx_tup_read=' || c.idx_tup_read::text
            || ', idx_tup_fetch=' || c.idx_tup_fetch::text
            || ', user_seeks=' || c.idx_scan::text
            || ', user_scans=0, user_lookups=0'
            -- Per TABLE, not per index: PostgreSQL keeps no per-index write counter. This is the
            -- maintenance cost an unread index on this table is charging on every write.
            || ', user_updates=' || c.table_writes::text
            || ', table_writes=' || c.table_writes::text
            || ', size_mb=' || ROUND(c.index_bytes / 1048576.0, 2)::text
            || ', last_read=' || COALESCE(c.last_idx_scan, 'never')
            || ', last_stats_update=' || CASE WHEN c.last_analyzed = '-infinity'::timestamptz
                                              THEN 'never'
                                              ELSE to_char(c.last_analyzed, 'YYYY-MM-DD HH24:MI:SS') END
            || ', counters_since=' || COALESCE((SELECT to_char(stats_reset, 'YYYY-MM-DD HH24:MI:SS') FROM stats),
                                               'never reset')
            || CASE
                 WHEN NOT c.indisvalid OR NOT c.indisready THEN
                     ' | INVALID index: the planner will not use it and every write still maintains it — all of the cost, none of the benefit. '
                     || CASE WHEN c.constraint_backed
                             THEN 'action=DROP the constraint and re-add it (the constraint owns this index)'
                             ELSE 'action=DROP INDEX and re-create it, CONCURRENTLY if the table is busy' END
                 WHEN c.indisprimary THEN ' | primary key: enforces uniqueness; action=KEEP (low reads are normal)'
                 WHEN c.constraint_backed THEN ' | constraint index: DROP INDEX fails on it; action=KEEP, and if it must go, DROP CONSTRAINT'
                 WHEN c.indisunique THEN ' | unique index: may be relied on for correctness, not speed; action=review carefully before DROP'
                 WHEN c.kind = 'UNUSED' THEN
                     ' | never read, and the table took ' || c.table_writes::text
                     || ' write(s) that all maintained it; action=review, then DROP INDEX'
                 WHEN c.kind = 'COLD' THEN
                     ' | never read, and the table is not written either — a dormant table, not a wasted index; action=review, then DROP INDEX'
                 ELSE '' END AS message,
        CASE c.kind WHEN 'UNUSED' THEN 1 WHEN 'COLD' THEN 2 WHEN 'USED' THEN 3 ELSE 4 END AS sort_rank
    FROM classified c

    UNION ALL

    -- The per-database summary. On PostgreSQL this is also the whole run, because a connection
    -- sees one database — the two are emitted separately anyway so the report keys on the same
    -- `db=` marker it does for SQL Server.
    SELECT
        current_database() || ' :: index_usage summary'  AS metric_item,
        COUNT(*)::text                                   AS metric_value,
        'summary'                                        AS metric_unit,
        'OK'                                             AS status,
        'db=' || current_database()
            || ', indexes_total=' || COUNT(*)::text
            || ', used=' || COUNT(*) FILTER (WHERE kind = 'USED')::text
            || ', unused=' || COUNT(*) FILTER (WHERE kind = 'UNUSED')::text
            || ', cold=' || COUNT(*) FILTER (WHERE kind = 'COLD')::text
            || ', disabled=' || COUNT(*) FILTER (WHERE NOT indisvalid OR NOT indisready)::text
            || ', disabled_clustered=0'
            || ', droppable=' || COUNT(*) FILTER (WHERE droppable)::text
            || ', tables=' || COUNT(DISTINCT schemaname || '.' || table_name)::text
            || ', total_size_mb=' || ROUND(SUM(index_bytes) / 1048576.0, 2)::text
            || ', droppable_size_mb=' || ROUND(COALESCE(SUM(index_bytes) FILTER (WHERE droppable), 0) / 1048576.0, 2)::text
            || ', counters_since=' || COALESCE((SELECT to_char(stats_reset, 'YYYY-MM-DD HH24:MI:SS') FROM stats),
                                               'never reset')
            || ' | scope=this database only; PostgreSQL cannot read another database''s catalog from one connection'
            AS message,
        0 AS sort_rank
    FROM classified
    GROUP BY 1

) q
ORDER BY q.sort_rank, q.metric_item;
