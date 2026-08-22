-- DATABASE_CONSTRAINT_HEALTH (MySQL 8.0.16+): CHECK constraints that are not enforced.
-- InnoDB FOREIGN KEYs are always enforced in MySQL (there is no per-FK disabled state), so the
-- integrity risk surface here is CHECK constraints created or altered with ENFORCED = NO
-- (information_schema.TABLE_CONSTRAINTS.ENFORCED = 'NO'). One WARNING summary row; the unenforced CHECK
-- constraint plus one summary row (OK when clean). Gated to MySQL 8.0.16+ in the metric
-- definition, where the ENFORCED column exists.
-- Detail rows are LOGGING, not WARNING: the summary row already states the count, and one
-- warning per constraint turns a single finding into hundreds an operator cannot triage.
-- They stay collected and queryable. Summaries sort FIRST so the row that warns survives
-- the row cap. Same split the index metrics use.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        CAST(CONCAT(tc.CONSTRAINT_SCHEMA, '.', tc.TABLE_NAME, '.', tc.CONSTRAINT_NAME) AS CHAR(400)) AS metric_item,
        CAST('CHECK' AS CHAR(64)) AS metric_value,
        CAST('detail' AS CHAR(32)) AS metric_unit,
        CAST('LOGGING' AS CHAR(16)) AS status,
        CAST('CHECK constraint not enforced (ENFORCED=NO) - invalid rows possible' AS CHAR(1000)) AS message,
        1 AS sort_rank
    FROM information_schema.TABLE_CONSTRAINTS tc
    WHERE tc.CONSTRAINT_TYPE = 'CHECK'
      AND tc.ENFORCED = 'NO'
      AND tc.CONSTRAINT_SCHEMA NOT IN ('mysql', 'sys', 'performance_schema', 'information_schema')

    UNION ALL

    SELECT
        CAST('constraints :: summary' AS CHAR(400)) AS metric_item,
        CAST(CAST((
            SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS t2
            WHERE t2.CONSTRAINT_TYPE = 'CHECK' AND t2.ENFORCED = 'NO'
              AND t2.CONSTRAINT_SCHEMA NOT IN ('mysql','sys','performance_schema','information_schema')
        ) AS CHAR) AS CHAR(64)) AS metric_value,
        CAST('count' AS CHAR(32)) AS metric_unit,
        CAST(CASE WHEN (
            SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS t3
            WHERE t3.CONSTRAINT_TYPE = 'CHECK' AND t3.ENFORCED = 'NO'
              AND t3.CONSTRAINT_SCHEMA NOT IN ('mysql','sys','performance_schema','information_schema')
        ) > 0 THEN 'WARNING' ELSE 'OK' END AS CHAR(16)) AS status,
        CAST('unenforced_check_constraints (InnoDB FKs are always enforced in MySQL)' AS CHAR(1000)) AS message,
        0 AS sort_rank
) AS q
ORDER BY q.sort_rank, q.metric_item
LIMIT 200
