-- Replication health, role-aware in a single query:
--   * Primary:
--       - One row per connected standby grading replay lag in bytes.
--       - Replay lag >= 1 GB CRITICAL, >= 100 MB WARNING (reusing the standby's thresholds).
--       - Non-streaming states (startup, catchup, backup, stopping) are WARNING.
--       - Standalone primary with no standby connected is OK (not configured is a state, not a failure).
--       - Guarded against calling pg_current_wal_lsn() on a standby where it raises an error.
--   * Standby:
--       - One row showing receive/replay lag in bytes.
--       - Replay lag >= 100 MB CRITICAL, >= 10 MB WARNING.
--       - Paused WAL replay is CRITICAL.
--
-- All thresholds are declared once in the top-level thresholds CTE, following the engine convention.

WITH thresholds AS (
    SELECT
        1073741824::bigint AS primary_replay_lag_critical_bytes,  -- 1 GB
        104857600::bigint  AS primary_replay_lag_warning_bytes,   -- 100 MB
        104857600::bigint  AS standby_lag_critical_bytes,         -- 100 MB
        10485760::bigint   AS standby_lag_warning_bytes           -- 10 MB
)
-- 1. Primary: per-connected standby replay lag and state
SELECT
    r.application_name AS metric_item,
    r.replay_lag_bytes::text AS metric_value,
    'bytes' AS metric_unit,
    CASE
        WHEN r.replay_lag_bytes >= t.primary_replay_lag_critical_bytes THEN 'CRITICAL'
        WHEN r.replay_lag_bytes >= t.primary_replay_lag_warning_bytes THEN 'WARNING'
        WHEN r.state <> 'streaming' THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'role=primary'
    || ', replica=' || COALESCE(r.application_name, '')
    || ', client=' || COALESCE(host(r.client_addr), '')
    || ', state=' || COALESCE(r.state, '')
    || ', sync=' || COALESCE(r.sync_state, '')
    || ', sent_lsn=' || COALESCE(r.sent_lsn::text, 'NULL')
    || ', sent_lag_bytes=' || r.sent_lag_bytes
    || ', replay_lsn=' || COALESCE(r.replay_lsn::text, 'NULL')
    || ', replay_lag_bytes=' || r.replay_lag_bytes
    AS message
FROM (
    SELECT
        application_name,
        client_addr,
        state,
        sync_state,
        sent_lsn,
        replay_lsn,
        CASE
            WHEN NOT pg_is_in_recovery() AND replay_lsn IS NOT NULL
            THEN COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint, 0)
            ELSE 0
        END AS replay_lag_bytes,
        CASE
            WHEN NOT pg_is_in_recovery() AND sent_lsn IS NOT NULL
            THEN COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)::bigint, 0)
            ELSE 0
        END AS sent_lag_bytes
    FROM pg_stat_replication
    WHERE NOT pg_is_in_recovery()
) r
CROSS JOIN thresholds t

UNION ALL

-- 2. Primary with no connected standby: standalone is a valid state, not a failure
SELECT
    'replication' AS metric_item,
    '0' AS metric_value,
    'replicas' AS metric_unit,
    'OK' AS status,
    'role=primary, standby_count=0, state=standalone | primary has no connected streaming replicas' AS message
WHERE NOT pg_is_in_recovery()
  AND NOT EXISTS (
      SELECT 1
      FROM pg_stat_replication
  )

UNION ALL

-- 3. Standby: receive and replay lag
SELECT
    'standby' AS metric_item,
    s.replay_lag_bytes::text AS metric_value,
    'bytes' AS metric_unit,
    CASE
        WHEN pg_is_wal_replay_paused() THEN 'CRITICAL'
        WHEN s.replay_lag_bytes >= t.standby_lag_critical_bytes THEN 'CRITICAL'
        WHEN s.replay_lag_bytes >= t.standby_lag_warning_bytes THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'role=standby'
    || ', wal_replay_paused=' || pg_is_wal_replay_paused()
    || ', receive_lsn=' || COALESCE(pg_last_wal_receive_lsn()::text, 'NULL')
    || ', replay_lsn=' || COALESCE(pg_last_wal_replay_lsn()::text, 'NULL')
    || ', replay_lag_bytes=' || s.replay_lag_bytes
    || ', replay_timestamp=' || COALESCE(pg_last_xact_replay_timestamp()::text, 'NULL')
    AS message
FROM (
    SELECT
        COALESCE(
            pg_wal_lsn_diff(
                pg_last_wal_receive_lsn(),
                pg_last_wal_replay_lsn()
            )::bigint,
            0
        ) AS replay_lag_bytes
    WHERE pg_is_in_recovery()
) s
CROSS JOIN thresholds t
WHERE pg_is_in_recovery();