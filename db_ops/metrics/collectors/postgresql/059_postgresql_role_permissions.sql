-- DATABASE_USER_PERMISSIONS (PostgreSQL): role (user) inventory and per-database privileges.
-- PostgreSQL roles are cluster-wide, so this is readable from any database:
--   * one row per non-internal role with its attributes (SUPERUSER/CREATEROLE/CREATEDB/
--     REPLICATION/BYPASSRLS/LOGIN), validity, and role memberships;
--   * one row per explicit database-level grant (CONNECT/CREATE/TEMP) from pg_database.datacl.
-- Security inventory: status OK for normal roles; WARNING for a login role that is SUPERUSER
-- (or has BYPASSRLS), the PostgreSQL equivalent of the SQL Server high-privilege flag.
-- Uses only pg_roles (attributes, password masked) so no superuser privilege is required.
SELECT metric_item, metric_value, metric_unit, status, message
FROM (
    SELECT
        (r.rolname || '@cluster')::varchar(400) AS metric_item,
        'ROLE'::varchar(64)                     AS metric_value,
        NULL::varchar(32)                       AS metric_unit,
        (CASE WHEN r.rolcanlogin AND (r.rolsuper OR r.rolbypassrls)
              THEN 'WARNING' ELSE 'OK' END)::varchar(16) AS status,
        left(
            'login=' || r.rolcanlogin::text
            || CASE WHEN r.rolsuper       THEN ' SUPERUSER'   ELSE '' END
            || CASE WHEN r.rolcreaterole  THEN ' CREATEROLE'  ELSE '' END
            || CASE WHEN r.rolcreatedb    THEN ' CREATEDB'    ELSE '' END
            || CASE WHEN r.rolreplication THEN ' REPLICATION' ELSE '' END
            || CASE WHEN r.rolbypassrls   THEN ' BYPASSRLS'   ELSE '' END
            || COALESCE(' valid_until=' || r.rolvaliduntil::text, '')
            || COALESCE(
                 ' member_of=[' || (
                     SELECT string_agg(g.rolname, ',' ORDER BY g.rolname)
                     FROM pg_auth_members m
                     JOIN pg_roles g ON g.oid = m.roleid
                     WHERE m.member = r.oid
                 ) || ']', ''),
            1000)::varchar(1000) AS message,
        0 AS sort_priv
    FROM pg_roles r
    WHERE r.rolname NOT LIKE 'pg\_%'

    UNION ALL

    SELECT
        (d.datname || '\' || COALESCE(gr.rolname, 'PUBLIC'))::varchar(400) AS metric_item,
        'DB_GRANT'::varchar(64) AS metric_value,
        NULL::varchar(32)       AS metric_unit,
        'OK'::varchar(16)       AS status,
        left('priv=' || string_agg(acl.privilege_type, ',' ORDER BY acl.privilege_type),
             1000)::varchar(1000) AS message,
        1 AS sort_priv
    FROM pg_database d
    CROSS JOIN LATERAL aclexplode(d.datacl) AS acl
    LEFT JOIN pg_roles gr ON gr.oid = acl.grantee
    WHERE d.datallowconn AND NOT d.datistemplate
    GROUP BY d.datname, gr.rolname
) AS q
ORDER BY (status = 'WARNING') DESC, sort_priv, metric_item;
