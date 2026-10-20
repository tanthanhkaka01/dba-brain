-- DATABASE_SUSPECT_PAGES (Oracle 10g+): blocks RMAN has found corrupt, from v$database_block_corruption.
--
-- Oracle's counterpart of msdb.dbo.suspect_pages: until 1.160 (2026-10-15) this metric was SQL
-- Server's only, and a corrupt block on Oracle paged nobody. The view is filled by RMAN - every
-- BACKUP, and BACKUP VALIDATE / VALIDATE - and emptied when a later RMAN pass finds the block
-- repaired, so a row is corruption Oracle still believes is there. It knows only what RMAN has read:
-- a database whose backups skip a tablespace, or that is backed up by something other than RMAN,
-- is not covered by it.
--
-- NOLOGGING is not damage: the block was loaded without redo, and is unrecoverable from a backup
-- taken before that load - a reason to take one, WARNING. Every other type (ALL ZERO, FRACTURED,
-- CHECKSUM, CORRUPT, LOGICAL) is a block that cannot be read as written: CRITICAL, as SQL Server's
-- 823 / 824 / torn page are.
--
-- One row per file and corruption type, the block ranges counted. No row when nothing is corrupt
-- (empty_result_is_ok). Read from inside a PDB, the view shows that container's blocks.
SELECT
    CAST('file#=' || c.file# || ' :: ' || c.corruption_type AS varchar2(400)) AS metric_item,
    CAST(SUM(c.blocks) AS varchar2(32)) AS metric_value,
    CAST('blocks' AS varchar2(32)) AS metric_unit,
    CASE WHEN c.corruption_type = 'NOLOGGING' THEN 'WARNING' ELSE 'CRITICAL' END AS status,
    CAST('file#=' || c.file#
         || ', file=' || NVL(MAX(f.name), 'unknown')
         || ', corruption_type=' || c.corruption_type
         || ', blocks=' || SUM(c.blocks)
         || ', ranges=' || COUNT(*)
         || ', first_block=' || MIN(c.block#)
         || ', min_corruption_scn=' || MIN(c.corruption_change#)
         AS varchar2(1000)) AS message
FROM v$database_block_corruption c
LEFT JOIN v$datafile f
    ON f.file# = c.file#
GROUP BY c.file#, c.corruption_type
ORDER BY CASE WHEN c.corruption_type = 'NOLOGGING' THEN 1 ELSE 0 END, c.file#
