-- Replication health, role-aware in a single query:
--   * Primary:
--       - One row per connected standby.
--       - WARNING if no standby connected.
--   * Standby:
--       - One row showing receive/replay lag in bytes.
--       - Replay timestamp is informational only.

SELECT
    application_name AS metric_item,
    state AS metric_value,
    NULL::text AS metric_unit,
    CASE
        WHEN state = 'streaming' THEN 'OK'
        ELSE 'WARNING'
    END AS status,
    'role=primary'
    || ', replica=' || COALESCE(application_name, '')
    || ', client=' || COALESCE(host(client_addr), '')
    || ', state=' || COALESCE(state, '')
    || ', sync=' || COALESCE(sync_state, '')
    || ', sent_lag_bytes='
    || COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)::bigint,0)
    AS message
FROM pg_stat_replication
WHERE NOT pg_is_in_recovery()

UNION ALL

SELECT
    'replication' AS metric_item,
    '0' AS metric_value,
    'replicas' AS metric_unit,
    'WARNING' AS status,
    'Primary has no connected streaming replicas.' AS message
WHERE NOT pg_is_in_recovery()
  AND NOT EXISTS (
      SELECT 1
      FROM pg_stat_replication
  )

UNION ALL

SELECT
    'standby' AS metric_item,

    COALESCE(
        pg_wal_lsn_diff(
            pg_last_wal_receive_lsn(),
            pg_last_wal_replay_lsn()
        )::bigint,
        0
    )::text AS metric_value,

    'bytes' AS metric_unit,

    CASE
        WHEN pg_is_wal_replay_paused() THEN 'CRITICAL'

        WHEN COALESCE(
                 pg_wal_lsn_diff(
                     pg_last_wal_receive_lsn(),
                     pg_last_wal_replay_lsn()
                 ),
                 0
             ) >= 104857600
        THEN 'CRITICAL'          -- 100 MB

        WHEN COALESCE(
                 pg_wal_lsn_diff(
                     pg_last_wal_receive_lsn(),
                     pg_last_wal_replay_lsn()
                 ),
                 0
             ) >= 10485760
        THEN 'WARNING'           -- 10 MB

        ELSE 'OK'
    END AS status,

    'role=standby'
    || ', wal_replay_paused=' || pg_is_wal_replay_paused()
    || ', receive_lsn=' || COALESCE(pg_last_wal_receive_lsn()::text,'NULL')
    || ', replay_lsn=' || COALESCE(pg_last_wal_replay_lsn()::text,'NULL')
    || ', replay_lag_bytes='
    || COALESCE(
           pg_wal_lsn_diff(
               pg_last_wal_receive_lsn(),
               pg_last_wal_replay_lsn()
           )::bigint,
           0
       )
    || ', replay_timestamp='
    || COALESCE(pg_last_xact_replay_timestamp()::text,'NULL')
    AS message

WHERE pg_is_in_recovery();