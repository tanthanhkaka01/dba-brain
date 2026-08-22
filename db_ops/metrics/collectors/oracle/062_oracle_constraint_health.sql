-- DATABASE_CONSTRAINT_HEALTH (Oracle): constraint integrity for the database.
-- Reports foreign-key / check constraints that are DISABLED or NOT VALIDATED (the Oracle
-- equivalent of a SQL Server disabled/untrusted constraint - existing rows were never checked,
-- so orphaned / invalid data may exist) and disabled triggers, excluding Oracle-maintained
-- schemas. One WARNING summary row per problem set plus LOGGING detail. Requires SELECT on
-- the DBA_* views (SELECT_CATALOG_ROLE).
-- Detail rows are LOGGING, not WARNING: the summary row already states the count, and one
-- warning per constraint turns a single finding into hundreds an operator cannot triage.
-- They stay collected and queryable. Summaries sort FIRST so the row that warns survives
-- the row cap. Same split the index metrics use.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT metric_item, metric_value, metric_unit, status, message, sort_rank
    FROM (
        SELECT
            CAST(c.owner || '.' || c.table_name || '.' || c.constraint_name AS varchar2(400)) AS metric_item,
            CAST(DECODE(c.constraint_type, 'R', 'FK', 'C', 'CHECK', c.constraint_type) AS varchar2(64)) AS metric_value,
            CAST('detail' AS varchar2(32)) AS metric_unit,
            CAST('LOGGING' AS varchar2(16)) AS status,
            CAST('status=' || c.status || ' validated=' || c.validated
                 || CASE WHEN c.validated = 'NOT VALIDATED' THEN ' (orphaned/invalid rows possible)' ELSE '' END
                 AS varchar2(1000)) AS message,
            1 AS sort_rank
        FROM dba_constraints c
        WHERE c.constraint_type IN ('R', 'C')
          AND (c.status = 'DISABLED' OR c.validated = 'NOT VALIDATED')
          AND c.owner NOT IN ('SYS','SYSTEM','DBSNMP','OUTLN','XDB','CTXSYS','MDSYS','ORDSYS',
                              'WMSYS','AUDSYS','OLAPSYS','APPQOSSYS','GSMADMIN_INTERNAL','LBACSYS')

        UNION ALL

        SELECT
            CAST(t.owner || '.' || t.trigger_name AS varchar2(400)) AS metric_item,
            CAST('TRIGGER' AS varchar2(64)) AS metric_value,
            CAST('detail' AS varchar2(32)) AS metric_unit,
            CAST('LOGGING' AS varchar2(16)) AS status,
            CAST('trigger disabled' AS varchar2(1000)) AS message,
            1 AS sort_rank
        FROM dba_triggers t
        WHERE t.status = 'DISABLED'
          AND t.owner NOT IN ('SYS','SYSTEM','DBSNMP','OUTLN','XDB','CTXSYS','MDSYS','ORDSYS',
                              'WMSYS','AUDSYS','OLAPSYS','APPQOSSYS','GSMADMIN_INTERNAL','LBACSYS')

        UNION ALL

        SELECT
            CAST('constraints :: summary' AS varchar2(400)) AS metric_item,
            CAST(TO_CHAR(
                (SELECT COUNT(*) FROM dba_constraints c2
                 WHERE c2.constraint_type IN ('R','C')
                   AND (c2.status = 'DISABLED' OR c2.validated = 'NOT VALIDATED')
                   AND c2.owner NOT IN ('SYS','SYSTEM','DBSNMP','OUTLN','XDB','CTXSYS','MDSYS','ORDSYS',
                                        'WMSYS','AUDSYS','OLAPSYS','APPQOSSYS','GSMADMIN_INTERNAL','LBACSYS'))
            ) AS varchar2(64)) AS metric_value,
            CAST('count' AS varchar2(32)) AS metric_unit,
            CAST(CASE WHEN
                (SELECT COUNT(*) FROM dba_constraints c3
                 WHERE c3.constraint_type IN ('R','C')
                   AND (c3.status = 'DISABLED' OR c3.validated = 'NOT VALIDATED')
                   AND c3.owner NOT IN ('SYS','SYSTEM','DBSNMP','OUTLN','XDB','CTXSYS','MDSYS','ORDSYS',
                                        'WMSYS','AUDSYS','OLAPSYS','APPQOSSYS','GSMADMIN_INTERNAL','LBACSYS')) > 0
                THEN 'WARNING' ELSE 'OK' END AS varchar2(16)) AS status,
            CAST('disabled_or_not_validated_constraints (excludes Oracle-maintained schemas)' AS varchar2(1000)) AS message,
            0 AS sort_rank
        FROM dual
    )
    ORDER BY sort_rank, metric_item
)
WHERE ROWNUM <= 200
