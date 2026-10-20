-- DATABASE_SUSPECT_PAGES (PostgreSQL 12+): data pages whose checksum did not verify when read.
--
-- A checksum failure is PostgreSQL's 824: a page came back from storage different from what was
-- written. The read fails with an ERROR in the server log, and nothing else says so - until 1.159
-- (2026-10-15) no dbabrain metric read it, so corruption on PostgreSQL paged nobody while the same
-- event on SQL Server (msdb.dbo.suspect_pages) is CRITICAL. pg_stat_database.checksum_failures
-- and checksum_last_failure exist from 12; 10 and 11 have no counter to read.
--
-- The counter is cumulative since the statistics were last reset and never goes down, so it is
-- graded by when the last failure happened, not by the count: within the window CRITICAL, older
-- than that recorded at LOGGING - a page repaired months ago would otherwise page for ever. The
-- window is the SQL Server variant's own 30 days, so the metric means the same on both engines.
--
-- The row with no datname is the shared catalogs (pg_database, pg_authid ...): corruption there
-- breaks every database, so it is reported, named as such.
--
-- data_checksums off (the default before PostgreSQL 18's initdb) means a failure cannot be
-- detected at all. That is how the cluster was built, not a fault of today: one LOGGING row says
-- so, and no database rows follow because every counter would read 0 and mean nothing.
--
-- No row when checksums are on and nothing failed (empty_result_is_ok).
WITH params AS (
    SELECT 30 AS recent_days
),
failures AS (
    SELECT
        COALESCE(d.datname, '(shared catalogs)') AS database_name,
        d.checksum_failures,
        d.checksum_last_failure,
        d.stats_reset
    FROM pg_stat_database AS d
    WHERE current_setting('data_checksums') = 'on'
      AND COALESCE(d.checksum_failures, 0) > 0
)
SELECT
    f.database_name AS metric_item,
    f.checksum_failures::text AS metric_value,
    'failures'::text AS metric_unit,
    CASE
        WHEN f.checksum_last_failure >= now() - make_interval(days => p.recent_days) THEN 'CRITICAL'
        ELSE 'LOGGING'
    END AS status,
    'database=' || f.database_name
        || ', checksum_failures=' || f.checksum_failures
        || ', last_failure=' || COALESCE(to_char(f.checksum_last_failure, 'YYYY-MM-DD HH24:MI:SS TZ'), 'unknown')
        || ', recent_window_days=' || p.recent_days
        || ', counters_since=' || COALESCE(to_char(f.stats_reset, 'YYYY-MM-DD HH24:MI:SS TZ'), 'cluster start')
        AS message
FROM failures AS f
CROSS JOIN params AS p
UNION ALL
SELECT
    'data_checksums',
    'off',
    NULL,
    'LOGGING',
    'data_checksums=off, note=this cluster was initialised without data checksums, so a corrupt page is not detected when it is read'
WHERE current_setting('data_checksums') <> 'on'
ORDER BY 4, 1;
