-- POSTGRES_STATEMENT_TOTALS: Workload attribution and query execution totals from pg_stat_statements.
--
-- Why raw cumulative counters since stats reset / restart:
-- pg_stat_statements records cumulative calls, execution time, and rows since the last stats reset
-- (or postmaster startup). Per dbabrain engine design, collectors record and never grade cumulative
-- metrics. Consecutive collections are subtracted downstream in dbabrain/lib during report generation
-- to determine interval workload and statement rankings.
--
-- Why the union of top 25 by total_exec_time and top 25 by calls:
-- Workload attribution requires seeing both CPU/elapsed time consumers and high-frequency statements.
-- Taking the union of the top 25 by total execution time and top 25 by execution calls ensures that
-- shifting statements remain present across consecutive collections so differential rates can be
-- calculated without losing baselines.
--
-- Why guarded through to_regclass and query_to_xml:
-- pg_stat_statements requires shared_preload_libraries and CREATE EXTENSION. A direct SELECT on a
-- missing relation fails at parse time and aborts the entire collection. Guarding access with
-- to_regclass('pg_stat_statements') and dynamic execution via query_to_xml allows PostgreSQL to parse
-- this collector safely even when the extension is not loaded.
--
-- When pg_stat_statements is absent:
-- Emits a single NOT_INSTALLED row with status LOGGING explaining how to enable it. Not configured
-- is an expected operational state, not an engine error.
--
-- Reset tracking:
-- counters_since is extracted from pg_stat_statements_info.stats_reset (PostgreSQL 14+) when present,
-- falling back to pg_postmaster_start_time(). When statistics are reset or entries are deallocated,
-- deltas between collections become invalid; carrying the reset timestamp allows downstream readers
-- to detect resets and suppress misleading negative rates.
--
-- Statement snippet safety:
-- The SQL text snippet is truncated to 200 characters and has every '=' spaced out (' = ') to prevent
-- embedded equality operators from corrupting the key=value message parser. It is placed strictly last.

WITH ext_check AS (
    SELECT to_regclass('pg_stat_statements') IS NOT NULL AS has_ext
),
info_check AS (
    SELECT to_regclass('pg_stat_statements_info') IS NOT NULL AS has_info
),
reset_xml AS (
    SELECT
        query_to_xml(
            'SELECT to_char(stats_reset, ''YYYY-MM-DD HH24:MI:SS'') AS reset_time FROM pg_stat_statements_info',
            false, false, ''
        ) AS x
    FROM info_check ic
    WHERE ic.has_info
),
clock AS (
    SELECT
        COALESCE(
            (
                SELECT NULLIF((xpath('/table/row/reset_time/text()', rx.x))[1]::text, '')
                FROM reset_xml rx
            ),
            to_char(pg_postmaster_start_time(), 'YYYY-MM-DD HH24:MI:SS')
        ) AS counters_since
),
raw_xml AS (
    SELECT
        query_to_xml(
            'SELECT queryid, dbid, calls, total_exec_time, mean_exec_time, stddev_exec_time, rows, query FROM pg_stat_statements',
            false, false, ''
        ) AS x
    FROM ext_check ec
    WHERE ec.has_ext
),
unpacked AS (
    SELECT
        t.queryid,
        t.dbid,
        t.calls,
        t.total_exec_time,
        t.mean_exec_time,
        t.stddev_exec_time,
        t.rows,
        t.query
    FROM raw_xml rx
    CROSS JOIN LATERAL XMLTABLE('/table/row' PASSING rx.x
        COLUMNS
            queryid          text             PATH 'queryid',
            dbid             oid              PATH 'dbid',
            calls            bigint           PATH 'calls',
            total_exec_time  double precision PATH 'total_exec_time',
            mean_exec_time   double precision PATH 'mean_exec_time',
            stddev_exec_time double precision PATH 'stddev_exec_time',
            rows             bigint           PATH 'rows',
            query            text             PATH 'query'
    ) t
),
grouped_statements AS (
    SELECT
        u.queryid,
        COALESCE(MAX(d.datname), 'unknown') AS dbname,
        SUM(u.calls) AS calls,
        SUM(u.total_exec_time) AS total_exec_time,
        CASE WHEN SUM(u.calls) > 0 THEN SUM(u.total_exec_time) / SUM(u.calls) ELSE 0 END AS mean_exec_time,
        COALESCE(MAX(u.stddev_exec_time), 0.0) AS stddev_exec_time,
        SUM(u.rows) AS rows,
        MAX(u.query) AS query
    FROM unpacked u
    LEFT JOIN pg_database d ON d.oid = u.dbid
    GROUP BY u.queryid
),
ranked_statements AS (
    SELECT
        g.*,
        ROW_NUMBER() OVER (ORDER BY g.total_exec_time DESC) AS r_time,
        ROW_NUMBER() OVER (ORDER BY g.calls DESC) AS r_calls
    FROM grouped_statements g
),
cluster_total AS (
    SELECT
        SUM(u.calls) AS calls,
        SUM(u.total_exec_time) AS total_exec_time,
        CASE WHEN SUM(u.calls) > 0 THEN SUM(u.total_exec_time) / SUM(u.calls) ELSE 0 END AS mean_exec_time,
        SUM(u.rows) AS rows
    FROM unpacked u
),
formatted_rows AS (
    -- 1. Cluster summary row: all_statements
    SELECT
        0 AS sort_order,
        COALESCE(ct.total_exec_time, 0.0) AS sort_time,
        CAST('all_statements' AS varchar(256)) AS metric_item,
        CAST(ROUND(COALESCE(ct.total_exec_time, 0.0)::numeric, 2)::text AS varchar(32)) AS metric_value,
        CAST('ms' AS varchar(32)) AS metric_unit,
        CAST('LOGGING' AS varchar(16)) AS status,
        CAST(
            'queryid=all'
            || ', dbname=all'
            || ', calls=' || COALESCE(ct.calls, 0)::text
            || ', total_exec_time_ms=' || ROUND(COALESCE(ct.total_exec_time, 0.0)::numeric, 2)::text
            || ', mean_exec_time_ms=' || ROUND(COALESCE(ct.mean_exec_time, 0.0)::numeric, 2)::text
            || ', stddev_exec_time_ms=0.00'
            || ', rows=' || COALESCE(ct.rows, 0)::text
            || ', counters_since=' || c.counters_since
            || ', text=all statements cluster-wide totals'
            AS text
        ) AS message
    FROM ext_check ec
    CROSS JOIN clock c
    LEFT JOIN cluster_total ct ON true
    WHERE ec.has_ext

    UNION ALL

    -- 2. Top queries union (top 25 by total_exec_time OR top 25 by calls)
    SELECT
        1 AS sort_order,
        rs.total_exec_time AS sort_time,
        CAST('queryid:' || rs.queryid AS varchar(256)) AS metric_item,
        CAST(ROUND(rs.total_exec_time::numeric, 2)::text AS varchar(32)) AS metric_value,
        CAST('ms' AS varchar(32)) AS metric_unit,
        CAST('LOGGING' AS varchar(16)) AS status,
        CAST(
            'queryid=' || rs.queryid
            || ', dbname=' || rs.dbname
            || ', calls=' || rs.calls::text
            || ', total_exec_time_ms=' || ROUND(rs.total_exec_time::numeric, 2)::text
            || ', mean_exec_time_ms=' || ROUND(rs.mean_exec_time::numeric, 2)::text
            || ', stddev_exec_time_ms=' || ROUND(COALESCE(rs.stddev_exec_time, 0.0)::numeric, 2)::text
            || ', rows=' || rs.rows::text
            || ', counters_since=' || c.counters_since
            || ', text=' || COALESCE(
                LEFT(
                    BTRIM(
                        REGEXP_REPLACE(
                            REGEXP_REPLACE(
                                REGEXP_REPLACE(rs.query, E'[\r\n\t]+', ' ', 'g'),
                                '=', ' = ', 'g'
                            ),
                            '\s+', ' ', 'g'
                        )
                    ),
                    200
                ),
                ''
            )
            AS text
        ) AS message
    FROM ranked_statements rs
    CROSS JOIN clock c
    WHERE rs.r_time <= 25 OR rs.r_calls <= 25

    UNION ALL

    -- 3. Extension not installed fallback row
    SELECT
        0 AS sort_order,
        0.0 AS sort_time,
        CAST('pg_stat_statements' AS varchar(256)) AS metric_item,
        CAST('NOT_INSTALLED' AS varchar(32)) AS metric_value,
        CAST('state' AS varchar(32)) AS metric_unit,
        CAST('LOGGING' AS varchar(16)) AS status,
        CAST(
            'pg_stat_statements is not installed or not in shared_preload_libraries; skipped. '
            || 'Add pg_stat_statements to shared_preload_libraries in postgresql.conf and run CREATE EXTENSION pg_stat_statements; '
            || 'not configured is a state, not a failure'
            AS text
        ) AS message
    FROM ext_check ec
    WHERE NOT ec.has_ext
)
SELECT
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
FROM formatted_rows
ORDER BY sort_order ASC, sort_time DESC;
