-- Oracle 8i legacy variant - LOCK_BLOCKING_SESSIONS. Blocker/waiter pairs from v$lock.
-- Empty result = no blocking. This is the core check for the Forms "Not Responding" freeze.
SELECT
    TO_CHAR(l1.sid) || '->' || TO_CHAR(l2.sid) AS metric_item,
    l1.type AS metric_value,
    NULL AS metric_unit,
    'CRITICAL' AS status,
    'blocker_sid=' || l1.sid || ', waiter_sid=' || l2.sid ||
        ', lock_type=' || l1.type || ', id1=' || l1.id1 || ', id2=' || l1.id2 AS message
FROM v$lock l1, v$lock l2
WHERE l1.id1 = l2.id1
  AND l1.id2 = l2.id2
  AND l1.block = 1
  AND l2.request > 0;
