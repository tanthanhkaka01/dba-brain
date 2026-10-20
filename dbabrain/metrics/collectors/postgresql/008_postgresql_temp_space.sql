-- STORAGE_TEMP_SPACE (PostgreSQL 12+): temporary files in use now, by backend, against the limit
-- PostgreSQL enforces (1.149's port, 2026-10-16).
--
-- SQL Server grades tempdb and Oracle its temporary tablespaces by how full they are. PostgreSQL has
-- neither: a sort or hash that does not fit in work_mem spills to files on the data filesystem,
-- which has no size of its own to be full against. What PostgreSQL does enforce is temp_file_limit,
-- per process - past it the query fails with "temporary file size exceeds temp_file_limit". So the
-- approved temp thresholds, 85 % WARNING and 95 % CRITICAL, are read against that: the backend with
-- the most temp files in use now, as a share of the limit. With no limit (-1, the default) nothing is
-- graded and the row reports what is in use.
--
-- pg_ls_tmpdir() lists each tablespace's pgsql_tmp (12+, pg_monitor or superuser); a file is named
-- pgsql_tmp<pid>.<n>, which says whose it is. The cumulative side - temp files and bytes created over
-- an hour - is in the Workload table (pg_stat_database.temp_files / temp_bytes). A parallel query's
-- shared file sets are directories, which pg_ls_tmpdir does not list: its temp usage is undercounted.
WITH params AS (
    SELECT 95 AS critical_pct, 85 AS warning_pct
),
limit_kb AS (
    -- temp_file_limit in kB; -1 is no limit.
    SELECT setting::bigint AS kb FROM pg_settings WHERE name = 'temp_file_limit'
),
files AS (
    SELECT t.spcname,
           (regexp_match(f.name, '^pgsql_tmp([0-9]+)'))[1]::integer AS pid,
           f.size
    FROM pg_tablespace AS t
    CROSS JOIN LATERAL pg_ls_tmpdir(t.oid) AS f
    WHERE t.spcname <> 'pg_global'
),
backends AS (
    SELECT pid, sum(size) AS bytes, count(*) AS files,
           string_agg(DISTINCT spcname, ' ') AS tablespaces
    FROM files
    GROUP BY pid
),
top AS (
    SELECT b.pid, b.bytes, b.files, b.tablespaces,
           a.usename, a.application_name, a.state,
           row_number() OVER (ORDER BY b.bytes DESC) AS place
    FROM backends AS b
    LEFT JOIN pg_stat_activity AS a ON a.pid = b.pid
),
total AS (
    SELECT COALESCE(sum(bytes), 0) AS bytes, COALESCE(sum(files), 0) AS files, count(*) AS backends
    FROM backends
)
SELECT
    CAST('temp_space' AS varchar(256)) AS metric_item,
    CAST(round(t.bytes / 1048576.0, 2) AS varchar(32)) AS metric_value,
    CAST('MB' AS varchar(32)) AS metric_unit,
    CAST(CASE
        WHEN l.kb > 0 AND top.bytes >= l.kb * 1024.0 * p.critical_pct / 100 THEN 'CRITICAL'
        WHEN l.kb > 0 AND top.bytes >= l.kb * 1024.0 * p.warning_pct / 100 THEN 'WARNING'
        ELSE 'OK'
    END AS varchar(16)) AS status,
    CAST('temp_in_use_mb=' || round(t.bytes / 1048576.0, 2)
        || ', temp_files=' || t.files
        || ', backends=' || t.backends
        || ', temp_file_limit_mb=' || CASE WHEN l.kb > 0 THEN round(l.kb / 1024.0, 2)::text ELSE 'none' END
        || COALESCE(', largest_pid=' || top.pid
            || ', largest_mb=' || round(top.bytes / 1048576.0, 2)
            || CASE WHEN l.kb > 0 THEN ', largest_pct_of_limit=' || round(top.bytes * 100.0 / (l.kb * 1024.0), 2) ELSE '' END
            || ', largest_user=' || COALESCE(top.usename::text, 'unknown')
            || ', largest_application=' || COALESCE(NULLIF(top.application_name, ''), 'unknown')
            || ', largest_state=' || COALESCE(top.state, 'unknown')
            || ', tablespaces=' || top.tablespaces, '')
        || ', note=in_use_now_not_cumulative' AS varchar(1000)) AS message
FROM total AS t
CROSS JOIN limit_kb AS l
CROSS JOIN params AS p
LEFT JOIN top ON top.place = 1
