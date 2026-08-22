-- BACKUP_LAST_RESULT (Oracle variant) - newest FULL / DIFF / LOG backup, one row per type.
--
-- Deliberately emits the SAME contract as 022_sqlserver_last_backup_result.sql, down to the
-- message keys, because everything downstream reads that contract and nothing downstream knows
-- what engine a row came from: db_ops.common.backup_policy.collect_evidence parses
-- `database=`, `recovery_model=`, `backup_type=` and `backup_finish_date=` out of the message,
-- and the inventory report's Backup column is built from what it returns. An Oracle-shaped
-- message here would mean a second policy engine and a second report path for the same question.
--
-- Type mapping, from v$backup_datafile (NOT v$rman_backup_job_details):
--
--   incremental_level NULL or 0  -> FULL   a level 0 IS the baseline a chain restores from
--   incremental_level 1          -> DIFF   RMAN's cumulative/differential incremental
--   v$backup_set.backup_type='L' -> LOG    archivelog backup = the log-chain equivalent
--
-- `file# > 0` is what makes this correct. v$backup_set counts controlfile and spfile autobackups
-- as backup_type='D', so "newest D backup set" on this lab reads 3 minutes old when the newest
-- datafile backup is a day old - 2125 sets of which ~1426 are autobackups. Reading datafile
-- backups directly is the only way the FULL age means "the data is recoverable to here".
--
-- recovery_model is reported as the SQL Server model whose meaning matches, so one policy file
-- covers both engines: ARCHIVELOG and FULL both mean "point-in-time recovery is possible and log
-- backups are required"; NOARCHIVELOG and SIMPLE both mean "no log backups, restore to the last
-- full only". data/backup_policy.json keys LOG off recovery_model, so this mapping is what makes
-- a missing archivelog backup a violation on Oracle instead of silently "not required".
--
-- Every type is emitted even when it has never run: a missing FULL is the single most important
-- thing this metric can report, and an absent row reports nothing at all.

WITH db AS
(
    SELECT
        CAST(name AS varchar2(256)) AS database_name,
        CASE WHEN log_mode = 'ARCHIVELOG' THEN 'FULL' ELSE 'SIMPLE' END AS recovery_model_desc
    FROM v$database
),
backup_types AS
(
    SELECT 'FULL' AS backup_type, 1 AS type_order FROM dual
    UNION ALL SELECT 'DIFF', 2 FROM dual
    UNION ALL SELECT 'LOG',  3 FROM dual
),
latest AS
(
    -- Datafile backups only (file# > 0 excludes the controlfile), newest completion per type.
    SELECT
        CASE WHEN NVL(incremental_level, 0) = 0 THEN 'FULL' ELSE 'DIFF' END AS backup_type,
        MAX(completion_time) AS backup_finish_date
    FROM v$backup_datafile
    WHERE file# > 0
    GROUP BY CASE WHEN NVL(incremental_level, 0) = 0 THEN 'FULL' ELSE 'DIFF' END

    UNION ALL

    SELECT
        'LOG' AS backup_type,
        MAX(completion_time) AS backup_finish_date
    FROM v$backup_set
    WHERE backup_type = 'L'
)
SELECT
    CAST(db.database_name || ' / ' || bt.backup_type AS varchar2(256)) AS metric_item,

    -- hours_since_last_backup, matching the SQL Server variant. -1 = never.
    CAST(
        NVL(TO_CHAR(ROUND((SYSDATE - l.backup_finish_date) * 24)), '-1')
        AS varchar2(32)
    ) AS metric_value,

    CAST('hours_since_last_backup' AS varchar2(32)) AS metric_unit,

    -- Ageing is the policy engine's job, not this metric's: it holds the per-database thresholds
    -- and knows which types are required. Reporting OK here and letting the policy judge is what
    -- keeps a NOT_REQUIRED DIFF from being an alert on every Oracle target in the estate.
    CAST('OK' AS varchar2(32)) AS status,

    CAST(
          'database=' || db.database_name
        || ', recovery_model=' || db.recovery_model_desc
        || ', backup_type=' || bt.backup_type
        || ', backup_finish_date='
            || NVL(TO_CHAR(l.backup_finish_date, 'YYYY-MM-DD HH24:MI:SS'), 'NULL')
        AS varchar2(4000)
    ) AS message
FROM db
CROSS JOIN backup_types bt
LEFT JOIN latest l
    ON l.backup_type = bt.backup_type
ORDER BY bt.type_order;
