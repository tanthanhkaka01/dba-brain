-- POSTGRES_XID_WRAPAROUND (PostgreSQL): transaction ID and multixact ID wraparound safety.
--
-- Why multixact age was added beside transaction-ID age:
-- PostgreSQL manages transaction IDs (datfrozenxid) and multixact IDs (datminmxid) separately.
-- Both are 32-bit cyclical spaces subject to wraparound starvation: if autovacuum fails to
-- advance either horizon, the postmaster enters emergency shutdown and halts all writes
-- across the cluster. Monitoring datfrozenxid alone leaves multixact exhaustion invisible.
--
-- Why separate rows per database with metric_item naming which age:
-- Each database reports two rows: "<datname> :: xid" and "<datname> :: multixact". Naming the
-- horizon directly in metric_item allows an operator or report to immediately pinpoint which
-- mechanism is falling behind without parsing unstructured text.
--
-- Thresholds:
-- autovacuum_freeze_max_age (default 200M) and autovacuum_multixact_freeze_max_age (default 400M)
-- are the engine's hard points where aggressive anti-wraparound autovacuum triggers.
-- Breaching 90% of the threshold is CRITICAL (database is near emergency shutdown); breaching
-- 75% is WARNING (autovacuum is slipping).
--
-- Encapsulated in a top-level thresholds CTE to avoid literal repetition in CASE branches.

WITH thresholds AS (
    SELECT
        0.90::numeric AS critical_ratio,
        0.75::numeric AS warning_ratio
),
raw_ages AS (
    SELECT
        datname,
        'xid' AS age_type,
        age(datfrozenxid)::bigint AS current_age,
        current_setting('autovacuum_freeze_max_age')::bigint AS max_freeze_age,
        'xid_age' AS age_label
    FROM pg_database
    WHERE datallowconn

    UNION ALL

    SELECT
        datname,
        'multixact' AS age_type,
        mxid_age(datminmxid)::bigint AS current_age,
        current_setting('autovacuum_multixact_freeze_max_age')::bigint AS max_freeze_age,
        'mxid_age' AS age_label
    FROM pg_database
    WHERE datallowconn
),
calculated AS (
    SELECT
        r.datname || ' :: ' || r.age_type AS metric_item,
        r.current_age::text AS metric_value,
        CAST('transactions' AS varchar(32)) AS metric_unit,
        CASE
            WHEN r.current_age >= r.max_freeze_age * t.critical_ratio THEN 'CRITICAL'
            WHEN r.current_age >= r.max_freeze_age * t.warning_ratio THEN 'WARNING'
            ELSE 'OK'
        END AS status,
        round(r.current_age::numeric / r.max_freeze_age::numeric * 100, 2) AS percent_consumed,
        r.age_label || '=' || r.current_age
        || ', freeze_max_age=' || r.max_freeze_age
        || ', percent_consumed=' || round(r.current_age::numeric / r.max_freeze_age::numeric * 100, 2)
        AS message
    FROM raw_ages r
    CROSS JOIN thresholds t
)
SELECT
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
FROM calculated
ORDER BY percent_consumed DESC, metric_item;
