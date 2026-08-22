-- Oracle 8i incident metric - BUFFER_CACHE_HIT. Buffer cache hit ratio from v$sysstat.
-- Low ratio with rising physical reads points to undersized db_block_buffers / heavy full
-- scans (the PI_WIP_DETAIL disk-read hotspot from the incident).
SELECT
    'buffer_cache_hit_ratio' AS metric_item,
    TO_CHAR(ROUND((1 - (phy.value / (cur.value + con.value))) * 100, 2)) AS metric_value,
    'percent' AS metric_unit,
    CASE
        WHEN (1 - (phy.value / (cur.value + con.value))) * 100 < 80 THEN 'WARNING'
        WHEN (1 - (phy.value / (cur.value + con.value))) * 100 < 90 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'db_block_gets=' || cur.value || ', consistent_gets=' || con.value ||
        ', physical_reads=' || phy.value AS message
FROM v$sysstat cur, v$sysstat con, v$sysstat phy
WHERE cur.name = 'db block gets'
  AND con.name = 'consistent gets'
  AND phy.name = 'physical reads';
