-- Oracle 8i / Windows - PROCESS_DEAD_WEIGHT. How much of the connection ceiling is held by
-- things that are not doing work, and cannot be made to.
--
-- `PROCESS_LIMIT` (102) already reports how full `processes` and `sessions` are. It cannot say
-- *why*, and on this platform the why is what decides the fix. Measured on 192.0.2.235 on
-- 2026-08-19, at 549 of 550 processes: 5 sessions were ACTIVE, 336 were idle, **199 were KILLED
-- and 202 threads were orphaned**. See audits/20260819_audit_oracle_8i_1.235_killed_sessions_hold_the_wall.md.
--
-- The two numbers this adds, and why neither is visible anywhere else:
--
--   killed_sessions   `ALTER SYSTEM KILL SESSION` on 8i/Windows detaches the session from its
--                     thread and leaves the row behind as KILLED. It still holds a `sessions`
--                     slot. Measured 2026-08-19: all 199 killed sessions had a detached paddr and
--                     not one had a live thread.
--
--   orphaned_threads  ...and the thread it left behind holds a `processes` slot with nothing
--                     pointing at it. This is the number that matters most, because `processes`
--                     is the binding limit (ORA-00020) and because only two things clear it:
--                     the client coming back, or Dead Connection Detection reaping a client that
--                     has gone. A kill can only ever add to it. Over 26 minutes on 2026-08-19 the
--                     estate's kill job created 50.8 orphans/hour and DCD reclaimed 30/hour.
--
-- Graded against the `processes` limit rather than as a raw count, because "200 orphans" means
-- something different on a 550-process instance than on a 5000-process one. The bands come from
-- measurement, not taste: 0% is achievable and was observed immediately after the 2026-08-18
-- restart; 33% was today's incident; 45% was the 2026-08-17 outage.
--
-- Every construct here was run against 8.1.7 before being written down - scalar subqueries in the
-- SELECT list included, which 8i does support despite being usually described as a 9i feature.

SELECT
    metric_item,
    TO_CHAR(metric_count) AS metric_value,
    'count' AS metric_unit,
    CASE
        WHEN process_limit <= 0 THEN 'OK'
        WHEN metric_count * 100 / process_limit >= 20 THEN 'CRITICAL'
        WHEN metric_count * 100 / process_limit >= 10 THEN 'WARNING'
        WHEN metric_count > 0 THEN 'LOGGING'
        ELSE 'OK'
    END AS status,
    metric_item || '=' || TO_CHAR(metric_count)
        || ', pct_of_process_limit=' || TO_CHAR(ROUND(metric_count * 100 / GREATEST(process_limit, 1), 1))
        || ', processes=' || TO_CHAR(processes_now) || '/' || TO_CHAR(process_limit)
        || ', sessions=' || TO_CHAR(sessions_now)
        || ', user_threads=' || TO_CHAR(user_threads)
        || ', active_sessions=' || TO_CHAR(active_sessions)
        || ', inactive_sessions=' || TO_CHAR(inactive_sessions)
        || ', ' || detail AS message
FROM (
    SELECT
        'killed_sessions' AS metric_item,
        (SELECT COUNT(*) FROM v$session WHERE status = 'KILLED') AS metric_count,
        'a KILLED row holds a sessions slot until its client reconnects; kill cannot free one' AS detail,
        (SELECT current_utilization FROM v$resource_limit WHERE resource_name = 'processes') AS processes_now,
        (SELECT current_utilization FROM v$resource_limit WHERE resource_name = 'sessions') AS sessions_now,
        (SELECT TO_NUMBER(DECODE(limit_value, 'UNLIMITED', '0', limit_value))
           FROM v$resource_limit WHERE resource_name = 'processes') AS process_limit,
        (SELECT COUNT(*) FROM v$process WHERE background IS NULL) AS user_threads,
        (SELECT COUNT(*) FROM v$session WHERE type = 'USER' AND status = 'ACTIVE') AS active_sessions,
        (SELECT COUNT(*) FROM v$session WHERE type = 'USER' AND status = 'INACTIVE') AS inactive_sessions
    FROM dual
    UNION ALL
    SELECT
        'orphaned_threads',
        (SELECT COUNT(*) FROM v$process p
          WHERE p.background IS NULL
            AND NOT EXISTS (SELECT 1 FROM v$session s WHERE s.paddr = p.addr)),
        'a thread with no session holds a processes slot; only the client returning or DCD frees it',
        (SELECT current_utilization FROM v$resource_limit WHERE resource_name = 'processes'),
        (SELECT current_utilization FROM v$resource_limit WHERE resource_name = 'sessions'),
        (SELECT TO_NUMBER(DECODE(limit_value, 'UNLIMITED', '0', limit_value))
           FROM v$resource_limit WHERE resource_name = 'processes'),
        (SELECT COUNT(*) FROM v$process WHERE background IS NULL),
        (SELECT COUNT(*) FROM v$session WHERE type = 'USER' AND status = 'ACTIVE'),
        (SELECT COUNT(*) FROM v$session WHERE type = 'USER' AND status = 'INACTIVE')
    FROM dual
);
