-- LOCK_TRANSACTION_HOLDERS (PostgreSQL 10+): sessions holding locks, with detail for findings only.
--
-- Why:
-- Most lock monitoring focuses on the waiter: 009_postgresql_blocking_sessions.sql
-- (LOCK_BLOCKING_SESSIONS) names the waiting session and grades it by wait time.
-- 054_postgresql_transactions.sql (POSTGRES_LONG_TRANSACTIONS) grades all transactions by age
-- (5 min WARNING, 30 min CRITICAL). This metric is the HOLDER side - the detail behind both:
-- it identifies the client backends holding the locks that cause blocking or hold lock resources.
--
-- Why detail rows for findings only plus one summary row:
-- Active database clusters routinely have dozens or hundreds of client backends holding routine
-- locks for concurrent queries with nothing wrong. Emitting one row per holder at OK/LOGGING would
-- hit the collector's row cap on busy clusters and drown real findings in noise. Detail rows are
-- emitted only for genuine findings: transactions open at or past the warning threshold, sessions
-- blocking anyone, or sessions holding an advisory lock idle past the advisory warning threshold.
-- Everything else is aggregated into a single 'holders_summary' row at status LOGGING.
--
-- Why CRITICAL requires "old AND somebody waits", or abandoned advisory locks:
-- An open transaction by itself is telemetry; it is not an outage until another session is blocked
-- waiting on it (or until 054's long-transaction threshold fires). A session-level advisory lock
-- (pg_advisory_lock taken without an open transaction) held by an idle backend cannot be released
-- until the backend unlocks it or disconnects; because waiters on advisory locks frequently retry
-- or time out, waiting duration may never grow large. Such a session is graded directly on idle time:
-- CRITICAL after 60 s when blocking someone, or after 30 min idle regardless of waiters.
--
-- Why no scan guard is needed unlike SQL Server's:
-- On SQL Server, sys.dm_tran_locks enumerates dynamic lock manager allocations where millions of
-- row/page locks can accumulate and latch contention can stall concurrent workloads. In PostgreSQL,
-- the shared lock table (LockMethodData) in shared memory is statically allocated at startup,
-- bounded by max_locks_per_transaction * (max_connections + max_prepared_transactions). Furthermore,
-- PostgreSQL does not keep individual row locks in pg_locks (row locks are tuple infomask flags and
-- multixacts on disk pages). As a result, pg_locks cannot balloon into millions of rows.
-- Reading pg_locks takes each lock partition's LWLock briefly in shared mode, but bounded table size
-- ensures the scan completes in milliseconds without stalling the cluster.
--
-- Compatibility:
-- Written for PostgreSQL 10+: uses backend_type = 'client backend' (introduced in PG 10),
-- pg_blocking_pids() (PG 9.6+), and standard pg_locks/pg_stat_activity views without any
-- features newer than PostgreSQL 10 (no MATERIALIZED CTE keyword). Read-only; no DDL or DML.

WITH params AS (
    SELECT
        300::bigint  AS warning_tran_seconds,
        900::bigint  AS critical_tran_seconds,
        300::bigint  AS advisory_idle_warning_seconds,
        1800::bigint AS advisory_idle_critical_seconds,
        60::bigint   AS advisory_idle_abandoned_seconds
),
blocked_waiters AS (
    SELECT
        bl.blocking_pid AS pid,
        w.pid AS waiter_pid
    FROM pg_stat_activity w
    CROSS JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS bl(blocking_pid)
    WHERE w.pid <> pg_backend_pid()
      AND bl.blocking_pid <> pg_backend_pid()
),
blocker_agg AS (
    SELECT
        bw.pid,
        COUNT(DISTINCT bw.waiter_pid)::int AS blocked_sessions,
        (ARRAY_AGG(DISTINCT bw.waiter_pid ORDER BY bw.waiter_pid))[1:50] AS blocked_pids
    FROM blocked_waiters bw
    GROUP BY bw.pid
),
lock_counts AS (
    SELECT
        l.pid,
        COUNT(*)::bigint AS lock_count,
        COUNT(*) FILTER (WHERE l.locktype = 'advisory')::bigint AS advisory_lock_count
    FROM pg_locks l
    WHERE l.granted = true
      AND l.pid IS NOT NULL
      AND l.pid <> pg_backend_pid()
    GROUP BY l.pid
),
client_backends AS (
    SELECT
        a.pid,
        a.usename,
        a.datname,
        a.client_addr,
        a.application_name,
        a.state,
        a.wait_event_type,
        a.wait_event,
        a.xact_start,
        a.state_change,
        a.query
    FROM pg_stat_activity a
    WHERE a.backend_type = 'client backend'
      AND a.pid <> pg_backend_pid()
),
holders AS (
    SELECT
        cb.pid,
        cb.usename,
        cb.datname,
        cb.client_addr,
        cb.application_name,
        cb.state,
        cb.wait_event_type,
        cb.wait_event,
        cb.query,
        COALESCE(lc.lock_count, 0)::bigint AS lock_count,
        COALESCE(b.blocked_sessions, 0)::int AS blocked_sessions,
        COALESCE(array_to_string(b.blocked_pids, ';'), '') AS blocked_pids,
        CASE
            WHEN cb.xact_start IS NOT NULL
            THEN GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - cb.xact_start))::bigint)
            ELSE 0::bigint
        END AS xact_seconds,
        CASE
            WHEN cb.xact_start IS NULL
            THEN COALESCE(lc.advisory_lock_count, 0)::bigint
            ELSE 0::bigint
        END AS session_advisory_locks,
        CASE
            WHEN cb.xact_start IS NULL
             AND COALESCE(lc.advisory_lock_count, 0) > 0
             AND cb.state = 'idle'
             AND cb.state_change IS NOT NULL
            THEN GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - cb.state_change))::bigint)
            ELSE 0::bigint
        END AS advisory_idle_seconds
    FROM client_backends cb
    LEFT JOIN lock_counts lc ON lc.pid = cb.pid
    LEFT JOIN blocker_agg b ON b.pid = cb.pid
    WHERE COALESCE(lc.lock_count, 0) > 0
       OR COALESCE(b.blocked_sessions, 0) > 0
),
all_rows AS (
    SELECT
        GREATEST(h.xact_seconds, h.advisory_idle_seconds) AS sort_seconds,
        'pid=' || h.pid::text AS metric_item,
        h.lock_count::text AS metric_value,
        'lock(s)'::text AS metric_unit,
        CASE
            WHEN (h.xact_seconds >= p.critical_tran_seconds AND h.blocked_sessions > 0)
              OR (h.advisory_idle_seconds >= p.advisory_idle_abandoned_seconds AND h.blocked_sessions > 0)
              OR (h.advisory_idle_seconds >= p.advisory_idle_critical_seconds)
            THEN 'CRITICAL'
            ELSE 'WARNING'
        END AS status,
        'pid=' || h.pid
        || ', blocked_sessions=' || h.blocked_sessions
        || ', xact_seconds=' || h.xact_seconds
        || ', session_advisory_locks=' || h.session_advisory_locks
        || ', advisory_idle_seconds=' || h.advisory_idle_seconds
        || ', state=' || COALESCE(h.state, '')
        || ', wait_event_type=' || COALESCE(h.wait_event_type, '')
        || ', wait_event=' || COALESCE(h.wait_event, '')
        || ', user=' || COALESCE(h.usename, '')
        || ', client_addr=' || COALESCE(host(h.client_addr), '')   -- host(): ::text adds the mask, /32
        || ', application_name=' || COALESCE(REPLACE(REPLACE(h.application_name, ',', ' '), ';', ' '), '')
        || ', database=' || COALESCE(h.datname, '')
        || ', lock_count=' || h.lock_count
        || ', blocked_pids=' || COALESCE(h.blocked_pids, '')
        || ', last_query=' || REPLACE(REPLACE(REPLACE(SUBSTRING(COALESCE(h.query, '') FROM 1 FOR 200), E'\r', ' '), E'\n', ' '), ',', ' ')
        AS message
    FROM holders h
    CROSS JOIN params p
    WHERE h.xact_seconds >= p.warning_tran_seconds
       OR h.blocked_sessions > 0
       OR h.advisory_idle_seconds >= p.advisory_idle_warning_seconds

    UNION ALL

    SELECT
        0::bigint AS sort_seconds,
        'holders_summary' AS metric_item,
        COUNT(*)::text AS metric_value,
        'session(s)'::text AS metric_unit,
        'LOGGING' AS status,
        'sessions=' || COUNT(*)
        || ', total_locks=' || COALESCE(SUM(h.lock_count), 0)
        || ', max_xact_seconds=' || COALESCE(MAX(h.xact_seconds), 0)
        || ', pids=' || COALESCE(array_to_string((ARRAY_AGG(h.pid ORDER BY h.pid))[1:200], ';'), '')
        AS message
    FROM holders h
    CROSS JOIN params p
    WHERE h.xact_seconds < p.warning_tran_seconds
      AND h.blocked_sessions = 0
      AND h.advisory_idle_seconds < p.advisory_idle_warning_seconds
    HAVING COUNT(*) > 0
)
SELECT
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
FROM all_rows
ORDER BY
    CASE WHEN status = 'CRITICAL' THEN 1 WHEN status = 'WARNING' THEN 2 ELSE 3 END ASC,
    sort_seconds DESC,
    metric_item ASC;
