-- DATABASE_LOG_SIZE (SQL Server): allocated size of each database's LOG files, in GB.
--
-- Same contract and the same reason as DATABASE_DATA_SIZE (064): the inventory report reads this
-- code per database and showed a blank column because nothing produced it. metric_item is the
-- database name; metric_value is a plain number of GB.
--
-- Read beside the data size this is the number that tells the story: a log file several times
-- the size of its data files is a log that grew once, under a broken backup chain or one long
-- transaction, and was never shrunk back. LOG_FILE_SPACE says how *full* the log is right now
-- and LOG_REUSE_WAIT says what is holding it — this says how big it got.
--
-- Logging only; the alerting on log space belongs to LOG_FILE_SPACE.
SET NOCOUNT ON;

SELECT
    CAST(d.name AS varchar(400)) AS metric_item,
    CAST(CAST(CAST(SUM(mf.size) * 8.0 / 1048576.0 AS decimal(18, 2)) AS varchar(32)) AS varchar(64)) AS metric_value,
    CAST('GB' AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST(N'log_files=' + CAST(COUNT(*) AS nvarchar(10))
         + N', allocated_gb=' + CAST(CAST(SUM(mf.size) * 8.0 / 1048576.0 AS decimal(18, 2)) AS nvarchar(32))
         + N', recovery=' + LOWER(d.recovery_model_desc)
         -- Growth in percent on a log file is how a 1 GB log becomes 40 GB in a night: each
         -- growth is bigger than the last, and every one of them blocks writes while it zeroes.
         + N', pct_growth_files=' + CAST(SUM(CASE WHEN mf.is_percent_growth = 1 THEN 1 ELSE 0 END) AS nvarchar(10)) AS varchar(1000)) AS message
FROM sys.master_files AS mf
JOIN sys.databases AS d ON d.database_id = mf.database_id
WHERE mf.type_desc = 'LOG'
GROUP BY d.name, d.recovery_model_desc
ORDER BY d.name;
