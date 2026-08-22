-- INDEX_UNUSABLE (Oracle, every release) - indexes and index partitions the optimizer cannot use.
--
-- An UNUSABLE index is not a slow index: it is an index that is not there. Queries silently fall
-- back to full scans, and any INSERT/UPDATE touching it fails outright with ORA-01502. It gets
-- into that state from ALTER TABLE MOVE, a partition maintenance operation, or a direct-path
-- load with SKIP_UNUSABLE_INDEXES - all routine work, none of which announces what it broke.
-- Nothing in the catalog looked for this on any Oracle release.
--
-- Empty result means every index is usable, which is why the metric declares
-- empty_result_is_ok: silence here is the good answer, not a collection failure.
--
-- Deliberately UNION ALL of the two dictionary views rather than a join: a non-partitioned index
-- carries its state on dba_indexes, a partitioned one on dba_ind_partitions (its dba_indexes
-- status reads N/A, which is neither valid nor a problem). Checking only the first view is the
-- common way to miss exactly the partitioned indexes that partition maintenance breaks.
--
-- Oracle-maintained schemas are excluded: an UNUSABLE index in SYS/SYSTEM is a recovery
-- situation reported through other means, and listing them here would bury the application's.
SELECT * FROM (
    SELECT
        owner || '.' || index_name AS metric_item,
        'UNUSABLE' AS metric_value,
        'state' AS metric_unit,
        'CRITICAL' AS status,
        'index=' || owner || '.' || index_name ||
            ' on table ' || table_name ||
            ' is UNUSABLE (whole index); queries fall back to full scans and DML raises ORA-01502.'
            AS message
    FROM dba_indexes
    WHERE status = 'UNUSABLE'
      AND owner NOT IN ('SYS', 'SYSTEM', 'OUTLN', 'DBSNMP', 'CTXSYS', 'MDSYS', 'ORDSYS', 'WMSYS',
                     'AURORA$JIS$UTILITY$', 'AURORA$ORB$UNAUTHENTICATED', 'OSE$HTTP$ADMIN',
                     'ORDPLUGINS', 'PERFSTAT', 'TRACESVR', 'REPADMIN')
    UNION ALL
    SELECT
        index_owner || '.' || index_name || ':' || partition_name AS metric_item,
        'UNUSABLE' AS metric_value,
        'state' AS metric_unit,
        'CRITICAL' AS status,
        'index partition=' || index_owner || '.' || index_name || ':' || partition_name ||
            ' is UNUSABLE; queries against this partition fall back to full scans and DML raises ORA-01502.'
            AS message
    FROM dba_ind_partitions
    WHERE status = 'UNUSABLE'
      AND index_owner NOT IN ('SYS', 'SYSTEM', 'OUTLN', 'DBSNMP', 'CTXSYS', 'MDSYS', 'ORDSYS', 'WMSYS',
                     'AURORA$JIS$UTILITY$', 'AURORA$ORB$UNAUTHENTICATED', 'OSE$HTTP$ADMIN',
                     'ORDPLUGINS', 'PERFSTAT', 'TRACESVR', 'REPADMIN')
)
WHERE ROWNUM <= 200;
