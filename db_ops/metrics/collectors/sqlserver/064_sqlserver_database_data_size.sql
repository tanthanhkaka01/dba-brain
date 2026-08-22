-- DATABASE_DATA_SIZE (SQL Server): allocated size of each database's ROWS files, in GB.
--
-- The inventory report has always had a "data size" column and always shown it blank: it reads
-- this metric_code (reports/inventory_health.py) but nothing ever produced it. The contract that
-- report expects is narrow, so keep it: metric_item is the **database name** (it is matched
-- against the same item set as DATABASE_STATUS / DATABASE_CONFIG) and metric_value is a plain
-- number of GB, which _num() parses directly.
--
-- sys.master_files is read from the instance level on purpose: sys.database_files would need a
-- cursor across every database and USE, and a database the login cannot enter would silently
-- drop out of the inventory. master_files is one row per file for every database, visible with
-- VIEW ANY DEFINITION, so an offline or restoring database still reports its allocated size —
-- which is exactly when someone is looking.
--
-- This is **allocated** size (what the files occupy on disk), not used pages. How full those
-- files are is STORAGE_DATA_FILE_SPACE; the two answer different questions and a reader
-- comparing them learns where free space is trapped inside a file.
--
-- Logging only: a database being large is not a fault. STORAGE_DISK_FREE_SPACE and
-- STORAGE_DATA_FILE_SPACE own the alerting on space.
SET NOCOUNT ON;

SELECT
    CAST(d.name AS varchar(400)) AS metric_item,
    CAST(CAST(CAST(SUM(mf.size) * 8.0 / 1048576.0 AS decimal(18, 2)) AS varchar(32)) AS varchar(64)) AS metric_value,
    CAST('GB' AS varchar(32)) AS metric_unit,
    CAST('OK' AS varchar(16)) AS status,
    CAST(N'files=' + CAST(COUNT(*) AS nvarchar(10))
         + N', allocated_gb=' + CAST(CAST(SUM(mf.size) * 8.0 / 1048576.0 AS decimal(18, 2)) AS nvarchar(32))
         + N', state=' + LOWER(d.state_desc)
         + N', recovery=' + LOWER(d.recovery_model_desc) AS varchar(1000)) AS message
FROM sys.master_files AS mf
JOIN sys.databases AS d ON d.database_id = mf.database_id
WHERE mf.type_desc = 'ROWS'
GROUP BY d.name, d.state_desc, d.recovery_model_desc
ORDER BY d.name;
