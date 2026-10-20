-- POSTGRES_CHECKPOINT_PRESSURE (PostgreSQL 10-16): checkpoint pressure via pg_stat_bgwriter.
--
-- Why raw cumulative counters since stats_reset:
-- In PostgreSQL versions prior to 17, checkpoint counters reside in pg_stat_bgwriter as
-- checkpoints_timed and checkpoints_req. These counters accumulate continuously since stats_reset
-- (or postmaster startup).
--
-- House rule (docs/04_metrics_engine.md - "Cumulative counters are not current performance"):
-- Collectors record cumulative counters and do not grade them. Dividing cumulative counters produces
-- an average over the entire uptime: a past bulk load keeps the ratio high for months, while a
-- spike today is diluted to invisibility by historic timed checkpoints. Status is LOGGING and
-- report_policy declares collect_only: true. Consecutive collections are differenced downstream
-- in dbabrain/lib/interval_rates.py to grade interval checkpoint pressure against approved
-- thresholds (>= 40% WARNING, >= 70% CRITICAL).
--
-- Thresholds are declared in the thresholds CTE so downstream readers and documentation reference
-- identical numbers without hardcoded literals in CASE expressions.

WITH thresholds AS (
    SELECT
        70.0::numeric AS checkpoint_req_critical_pct,
        40.0::numeric AS checkpoint_req_warning_pct
),
stats AS (
    SELECT
        checkpoints_timed,
        checkpoints_req,
        checkpoints_timed + checkpoints_req AS total_checkpoints,
        CASE
            WHEN (checkpoints_timed + checkpoints_req) > 0
            THEN round((100.0 * checkpoints_req / (checkpoints_timed + checkpoints_req))::numeric, 2)
            ELSE 0.00
        END AS req_checkpoint_pct,
        to_char(COALESCE(stats_reset, pg_postmaster_start_time()), 'YYYY-MM-DD HH24:MI:SS') AS counters_since
    FROM pg_stat_bgwriter
)
SELECT
    CAST('checkpoint_pressure' AS varchar(256)) AS metric_item,
    CAST(to_char(s.req_checkpoint_pct, 'FM990.00') AS varchar(32)) AS metric_value,
    CAST('percent' AS varchar(32)) AS metric_unit,
    CAST('LOGGING' AS varchar(16)) AS status,
    CAST(
        'checkpoints_timed=' || s.checkpoints_timed
        || ', checkpoints_req=' || s.checkpoints_req
        || ', total_checkpoints=' || s.total_checkpoints
        || ', req_checkpoint_pct=' || to_char(s.req_checkpoint_pct, 'FM990.00')
        || ', counters_since=' || s.counters_since
        || ' | cumulative since stats_reset; grading against thresholds (warn 40%, crit 70%) requires downstream interval differencing'
        AS text
    ) AS message
FROM stats s
CROSS JOIN thresholds t;
