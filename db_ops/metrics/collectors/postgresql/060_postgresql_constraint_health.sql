-- DATABASE_CONSTRAINT_HEALTH (PostgreSQL): constraint integrity for the connected database.
-- PostgreSQL catalogs are per-database, so this reports the target database only:
--   * FOREIGN KEY / CHECK constraints marked NOT VALID (convalidated = false) — the direct
--     equivalent of a SQL Server UNTRUSTED constraint: existing rows were never checked, so
--     orphaned / invalid data may exist;
--   * user triggers that are DISABLED (tgenabled = 'D').
-- Actual orphaned-row
-- counting per FK is not done here (unbounded) — use a targeted SQL task.
-- ONE alerting row per database (the summary), and the individual problems as LOGGING detail
-- behind it. The detail rows used to be WARNING each: on a real ERP database that is hundreds of
-- warnings for one fact the summary row already states in full, which an operator cannot triage.
-- LOGGING keeps every detail row collected and queryable without alerting on it - the same split
-- the index metrics use. Summaries sort FIRST so the row that still warns survives the row cap.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        (current_database() || '\' || c.conrelid::regclass::text || '.' || c.conname)::varchar(400) AS metric_item,
        (CASE c.contype WHEN 'f' THEN 'FK' WHEN 'c' THEN 'CHECK' ELSE c.contype::text END)::varchar(64) AS metric_value,
        'detail'::varchar(32)   AS metric_unit,
        'LOGGING'::varchar(16)  AS status,
        'NOT VALID (never validated - orphaned/invalid rows possible)'::varchar(1000) AS message,
        1 AS sort_rank
    FROM pg_constraint c
    WHERE NOT c.convalidated AND c.contype IN ('f', 'c')

    UNION ALL

    SELECT
        (current_database() || '\' || t.tgrelid::regclass::text || '.' || t.tgname)::varchar(400) AS metric_item,
        'TRIGGER'::varchar(64)  AS metric_value,
        'detail'::varchar(32)   AS metric_unit,
        'LOGGING'::varchar(16)  AS status,
        'trigger disabled'::varchar(1000) AS message,
        1 AS sort_rank
    FROM pg_trigger t
    WHERE t.tgenabled = 'D' AND NOT t.tgisinternal

    UNION ALL

    SELECT
        (current_database() || ' :: constraints')::varchar(400) AS metric_item,
        (SELECT count(*)::text FROM pg_constraint WHERE NOT convalidated AND contype IN ('f','c'))::varchar(64) AS metric_value,
        'count'::varchar(32) AS metric_unit,
        (CASE WHEN (SELECT count(*) FROM pg_constraint WHERE NOT convalidated AND contype IN ('f','c'))
                 + (SELECT count(*) FROM pg_trigger WHERE tgenabled = 'D' AND NOT tgisinternal) > 0
              THEN 'WARNING' ELSE 'OK' END)::varchar(16) AS status,
        ('not_valid_constraints=' || (SELECT count(*) FROM pg_constraint WHERE NOT convalidated AND contype IN ('f','c'))::text
         || ' disabled_triggers='  || (SELECT count(*) FROM pg_trigger WHERE tgenabled = 'D' AND NOT tgisinternal)::text
        )::varchar(1000) AS message,
        CASE WHEN (SELECT count(*) FROM pg_constraint WHERE NOT convalidated AND contype IN ('f','c'))
                + (SELECT count(*) FROM pg_trigger WHERE tgenabled = 'D' AND NOT tgisinternal) > 0
             THEN 0 ELSE 2 END AS sort_rank
) AS q
ORDER BY q.sort_rank, q.metric_item;
