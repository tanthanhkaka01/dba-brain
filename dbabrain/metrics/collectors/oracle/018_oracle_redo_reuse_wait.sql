-- LOG_REUSE_WAIT (Oracle variant) - what is stopping a redo log group from being reused.
--
-- SQL Server exposes this directly (log_reuse_wait_desc). Oracle has no single column for it, but
-- the operational question is identical: which redo cannot be overwritten yet, and why. A group is
-- reusable once it has been archived and is no longer needed for instance recovery, so the states
-- that matter are:
--   * ACTIVE and not archived  -> still needed AND not yet archived: this is the stall risk.
--   * a failed/deferred archive destination -> archiving cannot drain, so groups stay unreusable.
--
-- The CURRENT group is deliberately excluded: it is always archived='NO' because it is being
-- written to right now. Alerting on it would fire constantly on every healthy database, which is
-- the fastest way to get a monitoring channel muted.
--
-- An archiver that cannot archive (every online group needs archiving / ARCHIVER STUCK) is
-- CRITICAL because online redo cannot grow and instance will stall on next log switch.
--
-- Always returns at least one row so the metric is never silently empty.
--
-- All thresholds are declared in the top-level params CTE so each number appears once.
--
-- Only in ARCHIVELOG mode: a NOARCHIVELOG database never archives, so every group reads
-- archived='NO' for ever and an ACTIVE group after each log switch would read as a stuck archiver.
-- There, redo is reused as soon as the checkpoint completes, and the metric says so.

WITH params AS (
    SELECT
        100 AS all_groups_unarchived_pct
    FROM dual
),
db AS (
    SELECT log_mode FROM v$database
),
log_summary AS (
    SELECT
        l."GROUP#",
        l."THREAD#",
        l."SEQUENCE#",
        l.status,
        l.archived,
        l.bytes,
        l.first_time,
        COUNT(*) OVER () AS total_groups,
        COUNT(CASE WHEN l.archived = 'NO' THEN 1 END) OVER () AS unarchived_groups
    FROM v$log l
)
SELECT
    CAST('group_' || TO_CHAR(l."GROUP#") AS varchar2(256)) AS metric_item,
    CAST(l.status AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    CASE
        WHEN (l.unarchived_groups * 100 / l.total_groups) >= p.all_groups_unarchived_pct THEN 'CRITICAL'
        ELSE 'WARNING'
    END AS status,
    CASE
        WHEN (l.unarchived_groups * 100 / l.total_groups) >= p.all_groups_unarchived_pct
            THEN 'log_reuse_wait=ARCHIVER_STUCK'
        ELSE 'log_reuse_wait=ARCHIVE_PENDING'
    END
        || ', group=' || TO_CHAR(l."GROUP#")
        || ', thread=' || TO_CHAR(l."THREAD#")
        || ', sequence=' || TO_CHAR(l."SEQUENCE#")
        || ', status=' || l.status
        || ', archived=' || l.archived
        || ', size_mb=' || TO_CHAR(ROUND(l.bytes / 1048576))
        || ', first_time=' || TO_CHAR(l.first_time, 'YYYY-MM-DD HH24:MI:SS')
        || CASE
            WHEN (l.unarchived_groups * 100 / l.total_groups) >= p.all_groups_unarchived_pct
                THEN '. Archiver is stuck: every online redo log group needs archiving and online redo cannot grow.'
            ELSE '. Redo group is still required for recovery and not yet archived.'
        END AS message
FROM log_summary l
CROSS JOIN params p
CROSS JOIN db
WHERE db.log_mode = 'ARCHIVELOG'
  AND l.archived = 'NO'
  AND l.status = 'ACTIVE'

UNION ALL

-- An archive destination that cannot accept redo keeps every group unreusable.
SELECT
    CAST(ads.dest_name AS varchar2(256)) AS metric_item,
    CAST(ads.status AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    CASE WHEN ads.status = 'ERROR' THEN 'CRITICAL' ELSE 'WARNING' END AS status,
    'log_reuse_wait=ARCHIVE_DESTINATION'
        || ', dest=' || ads.dest_name
        || ', status=' || ads.status
        || ', type=' || ads.type
        || ', destination=' || NVL(ads.destination, '')
        || ', error=' || NVL(ads.error, 'none') AS message
FROM v$archive_dest_status ads
WHERE ads.status NOT IN ('VALID', 'INACTIVE')

UNION ALL

SELECT
    CAST('log_reuse_wait' AS varchar2(256)) AS metric_item,
    CAST('NOTHING' AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    'OK' AS status,
    CASE
        WHEN db.log_mode = 'ARCHIVELOG'
            THEN 'log_reuse_wait=NOTHING. All redo groups are archived or reusable and every archive '
                 || 'destination is VALID.'
        ELSE 'log_reuse_wait=NOTHING. log_mode=' || db.log_mode
             || ': redo is reused once its checkpoint completes, without archiving.'
    END AS message
FROM db
WHERE (db.log_mode <> 'ARCHIVELOG'
       OR NOT EXISTS
    (
        SELECT 1
        FROM v$log
        WHERE archived = 'NO'
          AND status = 'ACTIVE'
    ))
  AND NOT EXISTS
    (
        SELECT 1
        FROM v$archive_dest_status
        WHERE status NOT IN ('VALID', 'INACTIVE')
    );
