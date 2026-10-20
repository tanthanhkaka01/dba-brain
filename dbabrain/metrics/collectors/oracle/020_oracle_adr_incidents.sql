-- ORACLE_ADR_INCIDENTS - recent critical ADR incidents and diagnostic health summary.
--
-- The Automatic Diagnostic Repository (ADR), introduced in Oracle 11g, captures critical database
-- errors (internal ORA-00600, ORA-07445, process crashes, corruptions) as incidents and groups
-- them by problem key in v$diag_problem and v$diag_incident.
--
-- Health Monitor (v$hm_finding), also part of ADR, executes automated and reactive integrity checks
-- across control files, data files, redo/undo, dictionary, and transaction structures. Open findings
-- at CRITICAL priority diagnose severe underlying faults that have not yet been resolved. They are
-- emitted at WARNING severity so DBAs are alerted before an incident or crash occurs.
--
-- Unlike host-based alert log scrapers, all ADR views (v$diag_incident, v$diag_problem, v$diag_info,
-- and v$hm_finding) answer directly over SQL connections and are accessible from a PDB on multitenant
-- (12c through 26ai).
--
-- Contract:
--   * one row per incident created in the last 24 h: CRITICAL, metric_item = incident_id,
--     metric_value = problem_key, message carries create time, status, problem key, flood_controlled.
--   * one row per open critical Health Monitor finding: WARNING, metric_item = 'hm_finding=<finding_id>',
--     metric_value = finding name (truncated to 64), message carries finding_id, run_id, name, type,
--     priority, time_detected, description, damage_description (single line, description fields last).
--   * always one summary row ('adr_summary'): metric_value = active problem count from v$diag_info.
--     WARNING when active problems > 0 and no incidents were created in the 24 h window; OK otherwise.
--     Message carries active_problems, active_incidents, adr_home, recent_incidents, and hm_open_critical.
--
-- All thresholds are declared in the top-level params CTE so every number appears once.

WITH params AS (
    SELECT
        1 AS lookback_days
    FROM dual
),
recent_incidents AS (
    SELECT
        i.incident_id,
        i.problem_id,
        i.create_time,
        i.status,
        i.flood_controlled,
        p.problem_key
    FROM v$diag_incident i
    LEFT JOIN v$diag_problem p
        ON p.problem_id = i.problem_id
    CROSS JOIN params pm
    WHERE i.create_time > SYSTIMESTAMP - NUMTODSINTERVAL(pm.lookback_days, 'DAY')
),
hm_critical_findings AS (
    SELECT
        f.finding_id,
        f.run_id,
        f.name,
        f.type,
        f.priority,
        f.time_detected,
        f.description,
        f.damage_description
    FROM v$hm_finding f
    WHERE f.status = 'OPEN'
      AND f.priority = 'CRITICAL'
),
diag_summary AS (
    SELECT
        NVL(TO_NUMBER(REGEXP_SUBSTR(MAX(CASE WHEN name = 'Active Problem Count' THEN value END), '^[0-9]+')), 0) AS active_problems,
        NVL(TO_NUMBER(REGEXP_SUBSTR(MAX(CASE WHEN name = 'Active Incident Count' THEN value END), '^[0-9]+')), 0) AS active_incidents,
        MAX(CASE WHEN name = 'ADR Home' THEN value END) AS adr_home
    FROM v$diag_info
)
SELECT
    CAST(TO_CHAR(r.incident_id) AS varchar2(256)) AS metric_item,
    CAST(NVL(r.problem_key, 'UNKNOWN') AS varchar2(64)) AS metric_value,
    CAST('incident' AS varchar2(32)) AS metric_unit,
    'CRITICAL' AS status,
    'incident_id=' || TO_CHAR(r.incident_id)
        || ', problem_key=' || NVL(r.problem_key, 'UNKNOWN')
        || ', create_time=' || TO_CHAR(r.create_time, 'YYYY-MM-DD HH24:MI:SS')
        || ', status=' || TO_CHAR(r.status)
        || ', flood_controlled=' || TO_CHAR(r.flood_controlled)
        || ', problem_id=' || TO_CHAR(r.problem_id) AS message
FROM recent_incidents r

UNION ALL

SELECT
    CAST('hm_finding=' || TO_CHAR(f.finding_id) AS varchar2(256)) AS metric_item,
    CAST(SUBSTR(NVL(f.name, 'UNKNOWN'), 1, 64) AS varchar2(64)) AS metric_value,
    CAST('finding' AS varchar2(32)) AS metric_unit,
    'WARNING' AS status,
    'finding_id=' || TO_CHAR(f.finding_id)
        || ', run_id=' || TO_CHAR(f.run_id)
        || ', name=' || NVL(f.name, '')
        || ', type=' || NVL(f.type, '')
        || ', priority=' || NVL(f.priority, '')
        || ', time_detected=' || TO_CHAR(f.time_detected, 'YYYY-MM-DD HH24:MI:SS')
        -- Each text cut to 1000: concatenated whole, two long ones pass Oracle's 4000-byte string
        -- limit (ORA-01489) and the metric would lose every row, the incidents with it.
        || ', description=' || REPLACE(REPLACE(SUBSTR(NVL(f.description, 'none'), 1, 1000), CHR(10), ' '), CHR(13), ' ')
        || ', damage_description=' || REPLACE(REPLACE(SUBSTR(NVL(f.damage_description, 'none'), 1, 1000), CHR(10), ' '), CHR(13), ' ') AS message
FROM hm_critical_findings f

UNION ALL

SELECT
    CAST('adr_summary' AS varchar2(256)) AS metric_item,
    CAST(TO_CHAR(s.active_problems) AS varchar2(64)) AS metric_value,
    CAST('problems' AS varchar2(32)) AS metric_unit,
    CASE
        WHEN s.active_problems > 0 AND (SELECT COUNT(*) FROM recent_incidents) = 0 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'adr_summary: active_problems=' || TO_CHAR(s.active_problems)
        || ', active_incidents=' || TO_CHAR(s.active_incidents)
        || ', adr_home=' || NVL(s.adr_home, 'none')
        || ', recent_incidents=' || TO_CHAR((SELECT COUNT(*) FROM recent_incidents))
        || ', hm_open_critical=' || TO_CHAR((SELECT COUNT(*) FROM hm_critical_findings)) AS message
FROM diag_summary s;
