-- PERFORMANCE_WAIT_STATS (PostgreSQL 10+): what the server's sessions are waiting on right now,
-- sampled over five seconds inside one statement (1.162).
--
-- PostgreSQL keeps no cumulative wait counters, so the SQL Server variant's two reads of a DMV
-- have no equivalent here. What it has is pg_stat_activity's wait_event at the instant it is read,
-- and the answer is to read it several times and count: a session seen in DataFileRead at 7 of 10
-- samples spent about 70% of the five seconds reading pages. That is sampling, the way ASH and
-- pg_wait_sampling measure it - every sample is one instant, the count is the time.
--
-- **Why the samples are independent, which was the hard part.** 1.162 was first tried with 14 reads
-- of pg_stat_activity 0.5 s apart and pg_stat_clear_snapshot() between them, while a second session
-- ran pg_sleep(6) and ended part-way: it was in all 14 (2026-10-15). Two things held the first read.
-- The backend status is a snapshot kept for the transaction, which pg_stat_clear_snapshot() drops -
-- and a function in FROM is materialised once and rewound on every later scan while its argument
-- does not change, so a cleared snapshot was never read again. Here the function is
-- pg_stat_get_activity() itself, its argument depends on the sample row, so each sample calls it
-- anew after its own pause and clear. Measured on the .251 lab (2026-10-16): a pg_sleep(4) session
-- in samples 1 - 4 of 8 taken one second apart and gone from 5 - 8.
--
-- **The grading is SQL Server's, ported** (014_sqlserver_wait_stats.sql), not re-invented:
--
--   * an idle server is not graded: fewer than busy_floor_sessions waiting on average is OK,
--     whatever waited. SQL Server's floor is 10% of the CPUs and never under 2; PostgreSQL cannot
--     read its CPU count from SQL, so the floor is that minimum, 2.
--   * WARNING when the top wait is one that is a verdict about the server - I/O, in-memory
--     contention (LWLock), a pinned buffer, a commit waiting for a synchronous standby - and holds
--     busy_share_pct (50%) of every sampled wait.
--   * heavyweight lock waits (Lock) are ranked and reported, never graded:
--     LOCK_BLOCKING_SESSIONS and LOCK_TRANSACTION_HOLDERS already alert on blocking with the
--     session, and two alerts for one condition is one too many. This metric is their evidence.
--   * a session waiting on its client (Client), asleep (Timeout) or in an extension is not the
--     server waiting, and is left out like SQL Server's benign list. An active session with no
--     wait event is on CPU; it is counted and reported, not graded - PostgreSQL has no run queue
--     to read, so "on CPU" cannot be told from "waiting for a CPU".
--
-- Only client backends are sampled: a background process's wait (the WAL writer's main loop, the
-- checkpointer's) is how it idles. Read-only and safe on a hot standby; holds one connection for
-- about five seconds of the metric's 150.
WITH params AS (
    SELECT 10 AS samples,
           0.5::float8 AS interval_seconds,
           2.0::numeric AS busy_floor_sessions,
           50.0::numeric AS busy_share_pct
),
taken AS (
    SELECT g.i AS sample, pause.at, a.pid, a.wait_event_type, a.wait_event
    FROM params AS p
    CROSS JOIN LATERAL generate_series(1, p.samples) AS g(i)
    CROSS JOIN LATERAL (
        SELECT pg_sleep(CASE WHEN g.i > 1 THEN p.interval_seconds ELSE 0 END) AS slept,
               pg_stat_clear_snapshot() AS cleared,
               clock_timestamp() AS at
    ) AS pause
    -- The argument depends on the sample row, so every sample calls the function again; NULL is
    -- every backend. See the header: with a constant argument all samples are the first one.
    LEFT JOIN LATERAL pg_stat_get_activity(CASE WHEN pause.at IS NOT NULL THEN NULL::integer END) AS a
           ON a.pid <> pg_backend_pid()
          AND a.backend_type = 'client backend'
          AND a.state = 'active'
),
span AS (
    SELECT count(DISTINCT sample) AS samples,
           (EXTRACT(EPOCH FROM max(at) - min(at)) * 1000)::bigint AS sample_ms
    FROM taken
),
counted AS (
    SELECT COALESCE(t.wait_event_type, 'CPU') AS wait_type,
           COALESCE(t.wait_event, 'CPU') AS wait_event,
           count(*) AS seen
    FROM taken AS t
    WHERE t.pid IS NOT NULL
    GROUP BY 1, 2
),
waits AS (
    SELECT c.*,
           CASE
               WHEN c.wait_type = 'IO' AND c.wait_event LIKE 'DataFile%' THEN 'reading or writing data files'
               WHEN c.wait_type = 'IO' AND c.wait_event LIKE 'WAL%' THEN 'WAL write or flush latency'
               WHEN c.wait_type = 'IO' THEN 'file I/O'
               WHEN c.wait_type = 'LWLock' THEN 'in-memory contention (buffer mapping, WAL insertion, lock manager)'
               WHEN c.wait_type = 'BufferPin' THEN 'a buffer pinned by another session'
               WHEN c.wait_type = 'IPC' AND c.wait_event = 'SyncRep' THEN 'commits waiting for a synchronous standby'
           END AS pressure
    FROM counted AS c
    -- Not the server waiting: on its client, asleep, inside an extension. CPU is kept apart below.
    WHERE c.wait_type NOT IN ('Client', 'Timeout', 'Extension', 'Activity', 'CPU')
),
total AS (
    SELECT COALESCE(sum(seen), 0) AS seen FROM waits
),
on_cpu AS (
    SELECT COALESCE(sum(seen), 0) AS seen FROM counted WHERE wait_type = 'CPU'
),
ranked AS (
    SELECT w.*, row_number() OVER (ORDER BY w.seen DESC, w.wait_type, w.wait_event) AS place
    FROM waits AS w
),
runners AS (
    SELECT COALESCE(string_agg(r.wait_type || ':' || r.wait_event || ' ' || r.seen, ' | ' ORDER BY r.place), 'none') AS text
    FROM ranked AS r
    WHERE r.place BETWEEN 2 AND 4
)
SELECT
    CAST(r.wait_type || ':' || r.wait_event AS varchar(256)) AS metric_item,
    -- Seconds of the sample, the unit SQL Server's rows are in: the share of samples times the span.
    CAST(round(r.seen::numeric / NULLIF(s.samples, 0) * s.sample_ms / 1000.0, 2) AS varchar(32)) AS metric_value,
    CAST('seconds' AS varchar(32)) AS metric_unit,
    CAST(CASE
        WHEN t.seen::numeric / NULLIF(s.samples, 0) < p.busy_floor_sessions THEN 'OK'
        WHEN r.pressure IS NOT NULL AND r.seen * 100.0 / NULLIF(t.seen, 0) >= p.busy_share_pct THEN 'WARNING'
        ELSE 'OK'
    END AS varchar(16)) AS status,
    CAST('top_wait_type=' || r.wait_type || ':' || r.wait_event
        || ', seen_in_samples=' || r.seen
        || ', share_pct=' || round(r.seen * 100.0 / NULLIF(t.seen, 0), 2)
        || ', avg_waiting_sessions=' || round(t.seen::numeric / NULLIF(s.samples, 0), 2)
        || ', avg_on_cpu_sessions=' || round(c.seen::numeric / NULLIF(s.samples, 0), 2)
        || ', samples=' || s.samples
        || ', sample_ms=' || s.sample_ms
        || COALESCE(', meaning=' || r.pressure, '')
        || CASE WHEN r.wait_type = 'Lock' THEN ', graded_by=LOCK_BLOCKING_SESSIONS' ELSE '' END
        || ', runners_up=' || u.text
        || ', note=sampled_not_cumulative' AS varchar(1000)) AS message
FROM ranked AS r, total AS t, on_cpu AS c, span AS s, runners AS u, params AS p
WHERE r.place = 1
UNION ALL
SELECT
    CAST('wait_stats' AS varchar(256)),
    CAST('0' AS varchar(32)),
    CAST('seconds' AS varchar(32)),
    CAST('OK' AS varchar(16)),
    CAST('No client session waited in ' || s.samples || ' samples over ' || s.sample_ms
        || ' ms (' || round(c.seen::numeric / NULLIF(s.samples, 0), 2)
        || ' sessions on CPU on average). The server was idle, not healthy - this is an observation, not a verdict.'
        AS varchar(1000))
FROM span AS s, on_cpu AS c
WHERE NOT EXISTS (SELECT 1 FROM waits)
