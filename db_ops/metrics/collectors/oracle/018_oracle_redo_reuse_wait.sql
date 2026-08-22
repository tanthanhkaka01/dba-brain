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
-- Always returns at least one row so the metric is never silently empty.

SELECT
    CAST('group_' || TO_CHAR(l."GROUP#") AS varchar2(256)) AS metric_item,
    CAST(l.status AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    'WARNING' AS status,
    'log_reuse_wait=ARCHIVE_PENDING'
        || ', group=' || TO_CHAR(l."GROUP#")
        || ', thread=' || TO_CHAR(l."THREAD#")
        || ', sequence=' || TO_CHAR(l."SEQUENCE#")
        || ', status=' || l.status
        || ', archived=' || l.archived
        || ', size_mb=' || TO_CHAR(ROUND(l.bytes / 1048576))
        || ', first_time=' || TO_CHAR(l.first_time, 'YYYY-MM-DD HH24:MI:SS')
        || '. Redo group is still required for recovery and not yet archived.' AS message
FROM v$log l
WHERE l.archived = 'NO'
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
    'log_reuse_wait=NOTHING. All redo groups are archived or reusable and every archive '
        || 'destination is VALID.' AS message
FROM dual
WHERE NOT EXISTS
    (
        SELECT 1
        FROM v$log
        WHERE archived = 'NO'
          AND status = 'ACTIVE'
    )
  AND NOT EXISTS
    (
        SELECT 1
        FROM v$archive_dest_status
        WHERE status NOT IN ('VALID', 'INACTIVE')
    );
