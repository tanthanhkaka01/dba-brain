-- The login the toolkit uses, and nothing more than it needs.
--
-- Every metric in this example is a read of a system view. `pg_monitor` is the role PostgreSQL
-- ships for exactly that: it grants the elevated *read* of pg_stat_*, pg_ls_*, and the size
-- functions, and grants no write anywhere. A monitoring pass that runs as a superuser can change
-- the instance it is measuring, which is a strictly worse thing to leave running unattended
-- every five minutes than a login that cannot.
--
-- The password matches the one you will put in secrets/secret_text.json in step 3 of the README.
-- This file only runs the first time the container's data directory is created, so change both
-- together and recreate the container (`docker compose down -v && docker compose up -d`).

CREATE ROLE monitor_user WITH LOGIN PASSWORD 'quickstart_only_not_a_real_password';
GRANT pg_monitor TO monitor_user;

-- CONNECT on each database it should report on. Without this the instance-level metrics still
-- work and the per-database ones report a connection failure, which is a confusing way to find
-- out about a missing grant.
GRANT CONNECT ON DATABASE postgres TO monitor_user;
GRANT CONNECT ON DATABASE appdb TO monitor_user;
