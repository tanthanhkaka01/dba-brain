-- Oracle 8i incident metric - LIBRARY_CACHE. Reloads/invalidations per namespace
-- (v$librarycache). High reloads indicate shared-pool pressure / poor cursor sharing
-- (literal SQL), a contributor to the shared-pool incidents.
SELECT
    namespace AS metric_item,
    TO_CHAR(reloads) AS metric_value,
    'reloads' AS metric_unit,
    CASE
        WHEN gets > 0 AND reloads / gets > 0.02 THEN 'WARNING'
        ELSE 'OK'
    END AS status,
    'namespace=' || namespace || ', gets=' || gets || ', gethits=' || gethits ||
        ', reloads=' || reloads || ', invalidations=' || invalidations AS message
FROM v$librarycache
ORDER BY reloads DESC;
