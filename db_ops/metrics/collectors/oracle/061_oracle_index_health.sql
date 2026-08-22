-- MAINTENANCE_INDEX_FRAGMENTATION (Oracle variant) - unusable index objects.
--
-- Oracle has no direct equivalent of SQL Server's avg_fragmentation_in_percent, and inventing a
-- fragmentation number from clustering factor would be a guess dressed as a measurement. What Oracle
-- does have is a strictly worse condition than fragmentation: an UNUSABLE index, which the optimizer
-- ignores and which makes DML on the table fail outright. That is what this variant reports - the
-- same "indexes need maintenance" question, answered with the fact Oracle actually exposes.
--
-- Covers all three shapes an unusable index takes: whole indexes, index partitions, and index
-- subpartitions. A partitioned index shows VALID at the top level while individual partitions are
-- UNUSABLE, so checking dba_indexes alone would report healthy.
--
-- Oracle-maintained schemas are excluded via dba_users.oracle_maintained rather than a hand-written
-- owner list: the list would need editing for every release that adds an internal schema, and SYS
-- legitimately carries unusable internal partitions (COMMIT_SCN_LOG$_IDX) that nobody is going to
-- rebuild. The exclusion is defined once, in application_indexes below, and every branch including
-- the healthy-case check reads from it -- when the filters were written out per branch instead, the
-- OK row was suppressed by a SYS partition the reported branches had already excluded, and the
-- metric returned nothing at all.
--
-- Always returns at least one row, so a healthy database records an explicit OK.

WITH unusable_objects AS
(
    SELECT
        'index'                                   AS scope,
        i.owner                                   AS owner,
        i.index_name                              AS index_name,
        i.index_name                              AS object_name,
        i.status                                  AS status,
        'table=' || i.table_name
            || ', index_type=' || NVL(i.index_type, '')
            || '. Rebuild required: ALTER INDEX ' || i.owner || '.' || i.index_name
            || ' REBUILD ONLINE;'                 AS detail
    FROM dba_indexes i
    WHERE i.status = 'UNUSABLE'
      AND i.owner IN (SELECT username FROM dba_users WHERE oracle_maintained = 'N')

    UNION ALL

    SELECT
        'index_partition',
        p.index_owner,
        p.index_name,
        p.index_name || ':' || p.partition_name,
        p.status,
        'partition=' || p.partition_name
            || '. Rebuild required: ALTER INDEX ' || p.index_owner || '.' || p.index_name
            || ' REBUILD PARTITION ' || p.partition_name || ';'
    FROM dba_ind_partitions p
    WHERE p.status = 'UNUSABLE'
      AND p.index_owner IN (SELECT username FROM dba_users WHERE oracle_maintained = 'N')

    UNION ALL

    SELECT
        'index_subpartition',
        sp.index_owner,
        sp.index_name,
        sp.index_name || ':' || sp.subpartition_name,
        sp.status,
        'subpartition=' || sp.subpartition_name
            || '. Rebuild required: ALTER INDEX ' || sp.index_owner || '.' || sp.index_name
            || ' REBUILD SUBPARTITION ' || sp.subpartition_name || ';'
    FROM dba_ind_subpartitions sp
    WHERE sp.status = 'UNUSABLE'
      AND sp.index_owner IN (SELECT username FROM dba_users WHERE oracle_maintained = 'N')
)
SELECT
    CAST(u.owner || '.' || u.object_name AS varchar2(256)) AS metric_item,
    CAST(u.status AS varchar2(64)) AS metric_value,
    CAST(NULL AS varchar2(32)) AS metric_unit,
    'CRITICAL' AS status,
    'scope=' || u.scope
        || ', owner=' || u.owner
        || ', index=' || u.index_name
        || ', status=' || u.status
        || ', ' || u.detail AS message
FROM unusable_objects u

UNION ALL

SELECT
    CAST('index_health' AS varchar2(256)) AS metric_item,
    CAST('0' AS varchar2(64)) AS metric_value,
    CAST('unusable' AS varchar2(32)) AS metric_unit,
    'OK' AS status,
    'No UNUSABLE index, index partition or index subpartition found in application schemas.' AS message
FROM dual
WHERE NOT EXISTS (SELECT 1 FROM unusable_objects);
