-- LOG_FILE_SPACE (PostgreSQL 10+): the write-ahead log, PostgreSQL's transaction log.
--
-- SQL Server's variant answers "how full is each database's log". PostgreSQL has one WAL for the
-- whole cluster, in pg_wal, recycled at checkpoints down to about max_wal_size - so its question
-- is how many WAL files there are, how large they are together, and whether something is holding
-- them past what the checkpoints would keep: an archiver that fails, a replication slot nobody
-- reads, wal_keep_size. Until 2026-10-26 this metric had no PostgreSQL variant and a server page
-- for one could not say how much disk its log took.
--
-- Every figure is read from the server: the files from pg_ls_waldir() (superuser or pg_monitor),
-- the limits from the settings. WARNING when the WAL is more than twice what the settings keep
-- (max_wal_size + wal_keep_size) - not a disk percentage, which this login cannot see.
SELECT
    'pg_wal' AS metric_item,
    TO_CHAR(w.size_mb, 'FM999999990.00') AS metric_value,
    'MB' AS metric_unit,
    CASE
        WHEN w.size_mb > 2 * (s.max_wal_mb + s.keep_mb) THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'wal_files=' || w.files
        || ', wal_size_mb=' || TO_CHAR(w.size_mb, 'FM999999990.00')
        || ', max_wal_size_mb=' || TO_CHAR(s.max_wal_mb, 'FM999999990')
        || ', min_wal_size_mb=' || TO_CHAR(s.min_wal_mb, 'FM999999990')
        || ', wal_keep_size_mb=' || TO_CHAR(s.keep_mb, 'FM999999990')
        || ', wal_segment_mb=' || TO_CHAR(s.segment_mb, 'FM999990')
        || ', archive_mode=' || current_setting('archive_mode')
        || ', oldest_file=' || COALESCE(w.oldest, 'none')
        || ', newest_file=' || COALESCE(w.newest, 'none') AS message
FROM (
    SELECT count(*) AS files,
           round(COALESCE(sum(size), 0) / 1048576.0, 2) AS size_mb,
           min(name) AS oldest,
           max(name) AS newest
    FROM pg_ls_waldir()
    WHERE name ~ '^[0-9A-F]{24}$'
) w,
(
    SELECT pg_size_bytes(current_setting('max_wal_size')) / 1048576.0 AS max_wal_mb,
           pg_size_bytes(current_setting('min_wal_size')) / 1048576.0 AS min_wal_mb,
           -- wal_keep_size is PostgreSQL 13's; before it the setting does not exist and keeps 0.
           COALESCE(pg_size_bytes(NULLIF(current_setting('wal_keep_size', true), '')), 0)
               / 1048576.0 AS keep_mb,
           pg_size_bytes(current_setting('wal_segment_size')) / 1048576.0 AS segment_mb
) s;
