-- LOCK_TRANSACTION_HOLDERS (Oracle 10g+) - user sessions holding locks or transactions.
--
-- Why: Identifies the root session holding transaction locks, user locks (DBMS_LOCK), or blocking
-- other sessions, rather than the waiting victims (which 009_oracle_blocking_sessions.sql reports).
-- In an incident, a waiter report alone does not reveal the root cause; joining v$session with
-- v$transaction and v$lock exposes transaction duration, undo records generated, and waiters so
-- long-running or abandoned transactions are caught before connection pools cascade into an outage.
--
-- Scope:
--   * User sessions only (v$session.type = 'USER'), excluding the monitoring collector session itself.
--   * In Oracle multitenant (12c+), a Pluggable Database (PDB) session queries container-local sessions
--     in its own PDB; CDB-wide administrative privileges are not required.
--   * Evaluates the local instance dynamic views (v$session, v$lock, v$transaction); gv$ is deliberately
--     not read to prevent cross-instance cluster interconnect broadcast overhead on RAC estates.
--
-- Contract:
--   * Detail rows for findings only: open transaction age >= warning_tran_seconds (300s), OR actively
--     blocking another session, OR holding an idle user lock >= ul_idle_warning_seconds (300s).
--   * CRITICAL severity requires:
--       - transaction age >= critical_tran_seconds (900s) AND actively blocking someone, OR
--       - idle user lock >= ul_idle_abandoned_seconds (60s) AND actively blocking someone, OR
--       - idle user lock >= ul_idle_critical_seconds (1800s);
--     otherwise WARNING.
--   * Summary row: metric_item = 'holders_summary', LOGGING, counting user sessions holding locks with
--     nothing wrong yet; omitted if no such sessions exist.
--   * Thresholds are declared in the top-level params CTE so every number appears exactly once.

WITH params AS (
    SELECT
        300  AS warning_tran_seconds,
        900  AS critical_tran_seconds,
        300  AS ul_idle_warning_seconds,
        1800 AS ul_idle_critical_seconds,
        60   AS ul_idle_abandoned_seconds
    FROM dual
),
blocked_sessions_raw AS (
    SELECT
        blocking_session,
        sid,
        ROW_NUMBER() OVER (PARTITION BY blocking_session ORDER BY sid) AS rn
    FROM v$session
    WHERE blocking_session IS NOT NULL
),
blocked_agg AS (
    SELECT
        blocking_session,
        COUNT(*) AS blocked_count,
        RTRIM(
            XMLAGG(
                CASE WHEN rn <= 50 THEN XMLELEMENT(e, TO_CHAR(sid) || ';') END
                ORDER BY sid
            ).EXTRACT('//text()').GETSTRINGVAL(),
            ';'
        ) AS blocked_sids_list
    FROM blocked_sessions_raw
    GROUP BY blocking_session
),
locks_agg AS (
    SELECT
        sid,
        COUNT(*) AS lock_count,
        SUM(CASE WHEN type = 'UL' THEN 1 ELSE 0 END) AS user_locks
    FROM v$lock
    WHERE lmode > 0
    GROUP BY sid
),
holders AS (
    SELECT
        s.sid,
        s.serial#,
        s.status AS session_status,
        s.username,
        s.osuser,
        s.machine,
        s.program,
        s.module,
        s.event,
        s.blocking_session,
        s.sql_id,
        s.prev_sql_id,
        CASE
            WHEN s.taddr IS NOT NULL AND t.start_date IS NOT NULL
            THEN ROUND(GREATEST((SYSDATE - t.start_date) * 86400, 0))
            ELSE 0
        END AS tran_seconds,
        NVL(t.used_urec, 0) AS used_undo_records,
        NVL(b.blocked_count, 0) AS blocked_sessions,
        NVL(b.blocked_sids_list, '') AS blocked_sids,
        NVL(l.lock_count, 0) AS lock_count,
        NVL(l.user_locks, 0) AS user_locks,
        CASE
            WHEN NVL(l.user_locks, 0) > 0 AND s.status = 'INACTIVE' AND NVL(s.last_call_et, 0) > 0
            THEN s.last_call_et
            ELSE 0
        END AS ul_idle_seconds
    FROM v$session s
    LEFT JOIN v$transaction t
        ON t.addr = s.taddr
    LEFT JOIN locks_agg l
        ON l.sid = s.sid
    LEFT JOIN blocked_agg b
        ON b.blocking_session = s.sid
    WHERE s.type = 'USER'
      AND s.sid <> TO_NUMBER(SYS_CONTEXT('USERENV', 'SID'))
      AND (
          s.taddr IS NOT NULL
          OR NVL(b.blocked_count, 0) > 0
          OR NVL(l.user_locks, 0) > 0
      )
)
SELECT
    metric_item,
    metric_value,
    metric_unit,
    status,
    message
FROM (
    -- Detail rows: findings only
    SELECT
        CAST('SID=' || TO_CHAR(h.sid) || ',' || TO_CHAR(h.serial#) AS varchar2(256)) AS metric_item,
        TO_CHAR(h.lock_count) AS metric_value,
        CAST('lock(s)' AS varchar2(32)) AS metric_unit,
        CASE
            WHEN (h.tran_seconds >= p.critical_tran_seconds AND h.blocked_sessions > 0)
              OR (h.ul_idle_seconds >= p.ul_idle_abandoned_seconds AND h.blocked_sessions > 0)
              OR (h.ul_idle_seconds >= p.ul_idle_critical_seconds)
            THEN 'CRITICAL'
            ELSE 'WARNING'
        END AS status,
        'sid=' || TO_CHAR(h.sid)
            || ', serial=' || TO_CHAR(h.serial#)
            || ', blocked_sessions=' || TO_CHAR(h.blocked_sessions)
            || ', tran_seconds=' || TO_CHAR(h.tran_seconds)
            || ', used_undo_records=' || TO_CHAR(h.used_undo_records)
            || ', user_locks=' || TO_CHAR(h.user_locks)
            || ', ul_idle_seconds=' || TO_CHAR(h.ul_idle_seconds)
            || ', status=' || NVL(h.session_status, '')
            || ', username=' || NVL(h.username, '')
            || ', osuser=' || NVL(h.osuser, '')
            || ', machine=' || NVL(h.machine, '')
            || ', program=' || NVL(h.program, '')
            || ', module=' || NVL(h.module, '')
            || ', event=' || NVL(h.event, '')
            || ', blocking_session=' || NVL(TO_CHAR(h.blocking_session), '')
            || ', sql_id=' || NVL(h.sql_id, '')
            || ', prev_sql_id=' || NVL(h.prev_sql_id, '')
            || ', blocked_sids=' || NVL(h.blocked_sids, '') AS message,
        CASE
            WHEN (h.tran_seconds >= p.critical_tran_seconds AND h.blocked_sessions > 0)
              OR (h.ul_idle_seconds >= p.ul_idle_abandoned_seconds AND h.blocked_sessions > 0)
              OR (h.ul_idle_seconds >= p.ul_idle_critical_seconds)
            THEN 1
            ELSE 2
        END AS sort_priority,
        GREATEST(h.tran_seconds, h.ul_idle_seconds) AS lock_seconds
    FROM holders h
    CROSS JOIN params p
    WHERE h.tran_seconds >= p.warning_tran_seconds
       OR h.blocked_sessions > 0
       OR h.ul_idle_seconds >= p.ul_idle_warning_seconds

    UNION ALL

    -- Summary row: other user sessions holding locks with nothing wrong yet
    SELECT
        CAST('holders_summary' AS varchar2(256)) AS metric_item,
        TO_CHAR(COUNT(*)) AS metric_value,
        CAST('session(s)' AS varchar2(32)) AS metric_unit,
        'LOGGING' AS status,
        'sessions=' || TO_CHAR(COUNT(*))
            || ', total_locks=' || TO_CHAR(NVL(SUM(q.lock_count), 0))
            || ', max_tran_seconds=' || TO_CHAR(NVL(MAX(q.tran_seconds), 0)) AS message,
        3 AS sort_priority,
        0 AS lock_seconds
    FROM holders q
    CROSS JOIN params p
    WHERE q.tran_seconds < p.warning_tran_seconds
      AND q.blocked_sessions = 0
      AND q.ul_idle_seconds < p.ul_idle_warning_seconds
    HAVING COUNT(*) > 0
)
ORDER BY sort_priority ASC, lock_seconds DESC;
