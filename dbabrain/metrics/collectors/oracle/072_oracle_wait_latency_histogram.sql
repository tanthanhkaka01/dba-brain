-- ORACLE_WAIT_LATENCY_HISTOGRAM: Cumulative wait event latency histogram for core I/O and log sync events.
--
-- Why these five events:
-- Single-block read ('db file sequential read'), multi-block read ('db file scattered read'),
-- direct I/O ('direct path read'), commit sync ('log file sync'), and background redo log write
-- ('log file parallel write') capture the fundamental storage and commit latency characteristics
-- of an Oracle database engine.
--
-- Why raw cumulative counters since instance startup:
-- v$event_histogram records bucketed wait counts (le_1ms, le_2ms, le_4ms, ...) accumulated since
-- instance startup. Per dbabrain engine principles, collectors record and never grade cumulative
-- metrics. Calculating interval deltas or deriving latency percentiles across two collections is
-- handled downstream in dbabrain/lib during report generation.
--
-- Why every watched event is always emitted:
-- Emitting every event in the watched list—even when an event has had zero waits since startup
-- and has no rows in v$event_histogram—maintains a constant metric shape across consecutive
-- collections so differential parsers can calculate interval deltas without gap handling.
--
-- Multitenant compatibility:
-- v$event_histogram and v$instance are accessible from both CDB root and PDB sessions across
-- Oracle 11g, 12c, 19c, 21c, 23ai, and 26ai.

WITH watched_events AS (
    SELECT 'db file sequential read' AS event FROM dual UNION ALL
    SELECT 'db file scattered read'  AS event FROM dual UNION ALL
    SELECT 'direct path read'        AS event FROM dual UNION ALL
    SELECT 'log file sync'           AS event FROM dual UNION ALL
    SELECT 'log file parallel write' AS event FROM dual
),
inst AS (
    SELECT TO_CHAR(startup_time, 'YYYY-MM-DD HH24:MI:SS') AS counters_since
    FROM v$instance
),
hist AS (
    SELECT
        event,
        SUM(wait_count) AS total_waits,
        LISTAGG('le_' || TO_CHAR(wait_time_milli) || 'ms:' || TO_CHAR(wait_count), '|')
            WITHIN GROUP (ORDER BY wait_time_milli) AS buckets
    FROM v$event_histogram
    WHERE event IN (
        'db file sequential read',
        'db file scattered read',
        'direct path read',
        'log file sync',
        'log file parallel write'
    )
    GROUP BY event
)
SELECT
    CAST(we.event AS varchar2(256)) AS metric_item,
    CAST(TO_CHAR(NVL(h.total_waits, 0)) AS varchar2(32)) AS metric_value,
    CAST('waits' AS varchar2(32)) AS metric_unit,
    'LOGGING' AS status,
    CAST('event=' || we.event
        || ', counters_since=' || i.counters_since
        || ', buckets=' || NVL(h.buckets, 'none') AS varchar2(1000)) AS message
FROM watched_events we
CROSS JOIN inst i
LEFT JOIN hist h
    ON h.event = we.event
ORDER BY we.event;
