-- PROCESS_LIMIT (Oracle variant) - process and session utilization vs configured limits.
--
-- Directly guards against connection exhaustion failures (ORA-00020: maximum number of processes
-- exceeded and ORA-12500: TNS listener failed to start a dedicated server process).
--
-- Evaluates current_utilization from v$resource_limit for 'processes' and 'sessions' against limit_value.
-- Handles 'UNLIMITED' or whitespace-padded text; skips grading resources with no limit.
-- Approved thresholds: >= 90 % CRITICAL, >= 80 % WARNING.
--
-- All thresholds are declared in the top-level params CTE so every number appears once.

WITH params AS (
    SELECT
        90 AS critical_pct,
        80 AS warning_pct
    FROM dual
),
raw_limits AS (
    SELECT
        r.resource_name,
        r.current_utilization,
        r.max_utilization,
        TRIM(r.limit_value) AS clean_limit,
        CASE
            WHEN UPPER(TRIM(r.limit_value)) = 'UNLIMITED' THEN NULL
            WHEN TRIM(TRANSLATE(TRIM(r.limit_value), '0123456789', ' ')) IS NOT NULL THEN NULL
            ELSE TO_NUMBER(TRIM(r.limit_value))
        END AS numeric_limit
    FROM v$resource_limit r
    WHERE r.resource_name IN ('processes', 'sessions')
),
-- Inside a PDB v$resource_limit answers no rows (the lab's 26ai FREEPDB1, 2026-10-15): the limits
-- are the instance's. v$parameter still states them there and v$process / v$session can be
-- counted, so a target connected to a PDB is graded from those instead of answering NO_DATA.
-- Counted, not tracked: there is no high-water mark, and if the view shows only the PDB's own
-- sessions the share is understated - the message names the source so a reader knows which.
fallback AS (
    SELECT
        pr.name AS resource_name,
        CASE pr.name
            WHEN 'processes' THEN (SELECT COUNT(*) FROM v$process)
            ELSE (SELECT COUNT(*) FROM v$session)
        END AS current_utilization,
        CAST(NULL AS number) AS max_utilization,
        TRIM(pr.value) AS clean_limit,
        CASE
            WHEN TRIM(TRANSLATE(TRIM(pr.value), '0123456789', ' ')) IS NOT NULL THEN NULL
            ELSE TO_NUMBER(TRIM(pr.value))
        END AS numeric_limit
    FROM v$parameter pr
    WHERE pr.name IN ('processes', 'sessions')
      AND NOT EXISTS (SELECT 1 FROM raw_limits)
),
all_limits AS (
    SELECT rl.resource_name, rl.current_utilization, rl.max_utilization, rl.clean_limit,
           rl.numeric_limit, 'v$resource_limit' AS source
    FROM raw_limits rl
    UNION ALL
    SELECT f.resource_name, f.current_utilization, f.max_utilization, f.clean_limit,
           f.numeric_limit, 'v$parameter and a count of v$process / v$session' AS source
    FROM fallback f
),
graded AS (
    SELECT
        rl.resource_name,
        rl.current_utilization,
        rl.max_utilization,
        rl.clean_limit,
        rl.numeric_limit,
        rl.source,
        CASE
            WHEN rl.numeric_limit IS NOT NULL AND rl.numeric_limit > 0
                THEN ROUND(rl.current_utilization * 100 / rl.numeric_limit, 2)
            ELSE NULL
        END AS used_pct
    FROM all_limits rl
)
SELECT
    CAST(g.resource_name AS varchar2(256)) AS metric_item,
    CAST(TO_CHAR(g.current_utilization) AS varchar2(32)) AS metric_value,
    CAST('count' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN g.numeric_limit IS NULL OR g.numeric_limit <= 0 THEN 'OK'
        WHEN g.used_pct >= p.critical_pct THEN 'CRITICAL'
        WHEN g.used_pct >= p.warning_pct THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'resource=' || g.resource_name ||
        ', current=' || TO_CHAR(g.current_utilization) ||
        ', max_seen=' || NVL(TO_CHAR(g.max_utilization), 'unknown') ||
        ', limit=' || g.clean_limit ||
        CASE
            WHEN g.used_pct IS NOT NULL THEN ', used_pct=' || TO_CHAR(g.used_pct) || '%'
            ELSE ', used_pct=UNLIMITED'
        END ||
        ', source=' || g.source AS message
FROM graded g
CROSS JOIN params p
ORDER BY g.resource_name;
