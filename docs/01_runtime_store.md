# Runtime Store

## Purpose

The Runtime Store is the central state layer for `db_ops`. It stores operational history for app commands, SQL tasks, metrics, reports, Telegram messages, the Telegram send queue, SLA runs and backup restore history.

It runs on **PostgreSQL or SQLite**, chosen by one key in `data/store_config.json`. Nothing else in the codebase decides it: the four store classes go through `db_ops/db/backend.py`, and no app opens a database connection of its own.

> This is the store db_ops keeps its **own** data in. The databases db_ops *monitors* are unrelated and live in `data/db_instances.json`.

**SQLite is the right start** — it needs nothing installed, and a first install should not begin by
provisioning a database for the tool itself. **PostgreSQL is the right answer once the store
matters**: more than one node writing to it, or history you want to outlive the machine. A busy
estate reaches millions of `metric_results` rows within months, at which point this is production
data and wants a real server behind it.

Switching is one word in `data/store_config.json` plus a migration (see [Migrating SQLite to PostgreSQL](#migrating-sqlite-to-postgresql)). Both sections
stay in the file afterwards, fully specified, so switching back is not a rediscovery of host, port
and credentials.

**A leftover `runtime/db_ops.sqlite` after a cutover is a trap.** It still exists, it is no longer
written to, and reading it gives stale answers that look current. Ask a node what it is actually
writing to rather than inferring from a path:

```bash
python -m db_ops.db.cli --config config.json store-info
```

### Who may touch the store

| Layer | Rule |
| --- | --- |
| `db_ops/db/store.py` — `DbOpsStore` | owns `schema_meta`, `job_runs`, `sql_runs`, `reports*`, `telegram_*` |
| `db_ops/db/metric_store.py` — `MetricStore` | owns `metric_runs`, `metric_results`, `metric_results_archive`, `target_health` |
| `db_ops/db/sla_store.py` — `SlaStore` | owns `sla_runs`, `sla_results` |
| `db_ops/db/backup_restore_history.py` — `BackupRestoreHistory` | owns `backup_restore_history` |
| every other module | **must not open a connection.** Call a store method. |

**All four now live below the apps.** They did not always: `MetricStore` moved out of `metrics/`, and `SlaStore` and `BackupRestoreHistory` out of their apps on 2026-08-11. A store class inside an app is shared API in a place nothing else may import from, and the cost was concrete — `db/cli.py` composes the schema from all four, so it had to import *up* into `sla` and `backup_restore`, the one standing exception in `tests/test_import_boundaries.py`. Each old path keeps a re-export shim, and no DDL or schema version changed in the move, so a deployed store needed no migration.

That last table row is enforced by convention, and it is worth keeping: four modules used to call `sqlite3.connect()` themselves — `jobs/status.py`, `reports/inventory_health.py`, `reports/server_report.py` and `control/worker_status.py`. Each worked only because the store happened to be SQLite, and together they were four copies of "read `metric_results`" that had already drifted apart. They now call `MetricStore.fetch_freshness()`, `fetch_health_metrics()` and `fetch_server_series()`. The last three raw queries in app code — the backup-health report's evidence rows, its daily guard and the alert dedupe check — became `MetricStore.fetch_results_since()`, `DbOpsStore.report_exists_on_local_date()` and `DbOpsStore.recent_alert_exists()` on the same date. If you need a new read, add a method to the owning store class rather than opening a connection.

To check nothing has regressed:

```bash
grep -rn "sqlite3.connect" --include=*.py db_ops/ | grep -v "db_ops/db/"
```

That should return nothing. `db_ops/db/backend.py` is the only place a connection is opened; `schema_export.py` and `sqlite_to_postgres.py` are SQLite-specific tools by design.

## Backend Declaration

Which database the store uses is declared in one file:

```text
data/store_config.json
```

`backend` is the switch — `sqlite` or `postgresql` — and the section it names carries the full connection details for that backend. `db_ops.config.load_config` parses it into `config.store` (a `StoreConfig`), and `python -m db_ops.jobs.status` reports the resolved backend so a node can be asked what it is actually writing to.

| Field | Meaning |
| --- | --- |
| `backend` | `sqlite` or `postgresql`. `postgres` is accepted as an alias. |
| `sqlite.path` | Store file location. Relative paths resolve against the tool root, so `runtime/db_ops.sqlite` means `<tool_root>/runtime/db_ops.sqlite` on every node. |
| `sqlite.connection_string` | `sqlite:///<path>` URL. Authoritative when set — the path is read back out of it, so the file cannot name two destinations. |
| `postgresql.*` | Host, port, database, schema, username, `password_ref`, sslmode, connect timeout, application name. |
| `postgresql.connection_string` | Full URL with a `{password}` placeholder. Authoritative when set; otherwise built from the fields above. |

No password is ever written to this file. `password_ref` names a key in `data/encrypted_secret_text.json`, decrypted at runtime with `DB_OPS_SECRET_KEY` and substituted into `{password}` by `StoreConfig.resolved_connection_string()`. The `connection_string` property is the password-free form and is what gets logged.

### How one codebase speaks both dialects

`db_ops/db/backend.py` is the whole of it. The store classes were written against `sqlite3` — ~69 `with self.connect()` blocks across the four of them, and ~370 `row["column"]` accesses package-wide — so branching at each site was never an option. Instead:

* **SQLite returns a real `sqlite3.Connection`.** No wrapper, no translation. The default path is byte-for-byte what it always was, so this work cannot regress an existing deployment.
* **PostgreSQL returns `PostgresConnection`**, which presents the same connection/cursor/row surface on top of pg8000 — the driver already shipped for Postgres metrics, so there is no new dependency.

Three properties of the two dialects kept that adapter small:

1. **pg8000 accepts `qmark` paramstyle**, so all ~128 `?` placeholders work untouched. It also means a literal `%` (`LIKE '%abc%'`) needs no escaping — the default `format` paramstyle would have required `%%` in every such query.
2. **Both engines implement `INSERT … ON CONFLICT … DO UPDATE`**, so the store's upserts port as-is.
3. Nearly every `strftime` in the codebase is **Python's** `datetime.strftime`. In SQL it appears only in 19 column `DEFAULT`s and one `UPDATE`.

What the adapter does translate:

| SQLite | PostgreSQL | Why |
| --- | --- | --- |
| `PRAGMA journal_mode`, `synchronous`, `foreign_keys`, `busy_timeout` | skipped | tuning with no equivalent; issued on every connection, so erroring would break every call |
| `PRAGMA table_info(t)` | `information_schema.columns` query returning `name` | **not** tuning — it is how six additive-migration sites ask "does this column exist?". Skipping it would report every column missing and re-`ALTER` columns that already exist |
| `strftime('%Y-%m-%dT%H:%M:%SZ','now')` | `to_char(now() AT TIME ZONE 'UTC', …)` | produces the byte-identical string the store writes from Python |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT GENERATED **BY DEFAULT** AS IDENTITY` | `BY DEFAULT` so the migration can insert original ids; `ALWAYS` would reject them |
| `INTEGER` / `REAL` / `BLOB` (DDL only) | `BIGINT` / `DOUBLE PRECISION` / `BYTEA` | restricted to DDL so a query mentioning "integer" in a string literal is untouched |
| `cursor.lastrowid` | `lastval()` | session-scoped, and every `with store.connect()` owns its connection, so nothing else can move it |
| `json_valid()`, `json_extract()` | compatibility functions created in the store schema | `json_valid` is in 12 CHECK constraints; a regex rewrite would have to understand JSON path syntax. As functions, the store's SQL text stays valid on both engines |

`PostgresConnection` also mirrors `sqlite3`'s context-manager contract (commit on clean exit, roll back on exception) and additionally **closes** the connection — the store never reuses one after its `with` block, and PostgreSQL has a bounded `max_connections`, so leaving them open would exhaust the server.

### Schema initialization and concurrency

`initialize()` is called at the top of ~60 store methods. On SQLite that is a cheap no-op script against a local file; on PostgreSQL it is ~55 DDL statements over the network from **eight concurrent app-command processes**, and concurrent catalog writes fail outright with `tuple concurrently updated`. That put the daemon into a crash loop on the first cutover attempt. Three things now prevent it:

1. **In-process memo** — once per process per store.
2. **`schema_meta` version check** — the daemon's app commands are new processes every run, so the memo never helps them. One indexed `SELECT` answers "already built?" instead of running the DDL. Each store owns a version constant (`SCHEMA_VERSION`, `METRIC_SCHEMA_VERSION`, `SLA_SCHEMA_VERSION`, `HISTORY_SCHEMA_VERSION`); bump one when its tables or additive migrations change.
3. **Session-scoped advisory lock** — serializes the DDL when it does run. It must be *session*-scoped: `pg_advisory_xact_lock` looked right and protected nothing, because `executescript` commits after each script and that commit released the lock mid-initialization.

`python -m db_ops.db.cli init` passes `force=True`, which skips both guards — an explicit "build the schema" request should do the work, and it is the only way to re-run a repair on a store whose recorded version has not changed.

### Pitfalls this port hit, and what fixes them

Every one of these was found by running the real thing, not by reading code. They are listed because each is invisible on SQLite and fatal on PostgreSQL:

| Symptom | Cause | Fix |
| --- | --- | --- |
| `function strftime(unknown, unknown, unknown) does not exist` | SQLite's **three-argument** `strftime('%Y-…','now','-N days')` inlined in `archive_old_results`. The translator only rewrites the two-argument UTC-now form. | Compute windows in Python (`metrics.storage.cutoff_text`) and bind them. A guard test now fails if the three-argument form reappears in store SQL. |
| `relation "sqlite_master" does not exist` | SQLite's catalog probed directly to ask "does `metric_results` exist?". | `backend.table_exists()` — portable, uses `information_schema` on PostgreSQL. |
| `duplicate key value violates unique constraint "reports_new_pkey"` | The store's own table-rebuild migrations copy rows **with their ids**, which does not advance a `GENERATED BY DEFAULT` identity sequence (SQLite's `AUTOINCREMENT` counter does move). | `backend.resync_identity_sequences()` sweeps the schema at the end of `initialize()`. |
| `relation "telegram_command_messages_new" already exists` — permanent crash loop | The rebuild migrations do `CREATE <t>_new` → copy → drop → rename in **two transactions**. Die in between and the leftover wedges every later startup. | `DROP TABLE IF EXISTS <t>_new` before creating, making the rebuild resumable. |
| `column "claimed_at" … does not exist` during migration | `CREATE TABLE IF NOT EXISTS` silently leaves a pre-existing table with the wrong shape — here one a rebuild had recreated from an older column list. | The migration reconciles columns (`ALTER TABLE … ADD COLUMN`) before copying. |
| `cannot truncate a table referenced in a foreign key constraint` | Re-running the migration when FKs already exist. | Foreign keys are read from `pg_constraint` and dropped before loading — names cannot be reconstructed, because the store's rebuilds leave PostgreSQL-generated ones like `reports_new_report_type_fkey`. |
| `timed out` mid-`CREATE INDEX` | pg8000's `timeout` is a **socket** timeout on every operation, so `connect_timeout_seconds` silently became a statement timeout. | The socket returns to blocking once connected. |

**Before switching a node's backend, rehearse it.** Run the store classes and the real app commands against a scratch schema (`DB_OPS_STORE_CONFIG` pointing at a config with a different `schema`), with several processes at once. Every failure above would have surfaced there in minutes instead of on the live worker — that check is what finally made the cutover clean.

Windows are compared against a cutoff computed in Python (`metrics.storage.cutoff_text`) rather than with `strftime('…','now','-N days')`. One of those queries passed the `-N days` modifier as a **bound parameter**, which no textual rewrite could have reached.

### Store CLI

`python -m db_ops.db.cli` owns the PostgreSQL store's provisioning and the migration off SQLite:

| Command | What it does |
| --- | --- |
| `store-info` | Prints the resolved backend, connection string, SQLite path/size, and the PostgreSQL target. Read-only. |
| `init` | Creates/upgrades the schema on the **active** backend, by running `initialize(force=True)` on all four store classes. `force` bypasses the memo and version guards, so it also re-runs repairs (identity-sequence resync). |
| `check [--counts]` | Connects to the **active** backend and reports table count, `schema_version`, and optionally rows per table. |
| `create-store-database` | Creates the PostgreSQL database + schema on the primary. Idempotent. Refuses a standby unless `--allow-standby`. |
| `migrate-sqlite-to-postgres` | Copies the SQLite store into it. `--dry-run` prints the generated DDL and per-table row counts without writing; `--delta` brings an existing target up to date instead of reloading; `--skip-data` resumes after a failure in the index phase. |
| `verify-migration` | Compares row counts per table between SQLite and PostgreSQL. |
| `snapshot-sqlite --output PATH` | `VACUUM INTO` copy of the live store — a consistent source for a migration. |
| `export-sqlite-schema` | Writes the SQLite structure JSON snapshot. |

`init` and `check` never name a backend — they use whatever `store_config.json` declares, so the same two commands work on both. The same tree, switched only by `backend`, gives the same shape of answer:

```
$ python -m db_ops.db.cli --config config.json check --counts     # live, backend: postgresql
backend: postgresql
target : postgresql://postgres:{password}@192.0.2.249:5433/db_ops?...
tables : 18
schema_version: 1
  backup_restore_history          513
  job_runs                    1043239
  metric_results              3414935
  metric_results_archive      2953846
  metric_runs                   22370
  ...
  telegram_send_messages        14388
```

`check` needs the passphrase (`--key-base64`, or `DB_OPS_SECRET_KEY` in the environment) to
resolve `password_ref` — a PostgreSQL backend cannot be inspected without it. `store-info` is the
read-only exception: it prints the declaration and the password-free connection string.

The provisioning and migration commands read the `postgresql` block even **while `backend` is still `sqlite`**, so you can build and verify the new store before flipping the switch.

`DB_OPS_STORE_CONFIG` points at a different store declaration without editing the shipped file — useful for testing a Postgres target from a workstation:

```bash
DB_OPS_STORE_CONFIG=/tmp/pg_store_config.json python -m db_ops.db.cli check
```

### Migrating SQLite to PostgreSQL

The schema is introspected from the live SQLite database (`sqlite_master` + `PRAGMA`), never restated in the migration code — the store's tables are created in four modules (`db/store.py`, `db/metric_store.py`, `db/sla_store.py`, `db/backup_restore_history.py`) and grow by additive migration, so a hand-written PostgreSQL copy would drift. What the translation guarantees:

- SQLite affinity rules → `BIGINT` / `TEXT` / `DOUBLE PRECISION` / `BYTEA` / `NUMERIC`.
- `INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGINT GENERATED **BY DEFAULT** AS IDENTITY`, so the original ids are inserted as-is; the sequence is re-based from the copied data afterwards. (`ALWAYS` would reject them, and skipping the re-base makes the first post-migration insert collide.)
- Composite primary keys, `UNIQUE` table constraints (kept as constraints, so `ON CONFLICT` upserts keep working), and `DESC` index direction all survive.
- The store's `strftime('%Y-%m-%dT%H:%M:%SZ','now')` default becomes `to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')` — byte-identical output. An untranslatable expression default **raises** rather than being dropped.
- Rows move via `COPY ... FROM STDIN WITH (FORMAT text)`. Text format is used because its `\N` NULL marker is unambiguous: the store holds both `NULL` and `''` in the same TEXT columns, and CSV cannot separate them reliably. A value that literally *is* `\N` is escaped and survives as text.
- Values are coerced to the target type in Python first, so a type mismatch names the table, column and value. SQLite is dynamically typed — an INTEGER column really can contain text.

Order is tables → `COPY` → indexes → foreign keys → identity re-base → verify. Indexes are built after the load because the big tables (`metric_results`, `metric_results_archive`, `job_runs`) carry most of the store's indexes and loading into them is far slower. A foreign key that fails is reported and skipped rather than rolling back the rest — a long-running store can hold rows whose parent was pruned by a retention job.

**Run it on the node that owns the SQLite file.** On the worker both databases are local, so drive it through the control app rather than copying 5 GB over the network:

```bash
python -m db_ops.control.cli worker-run --key-base64 "<K>" -- \
    python -m db_ops.db.cli create-store-database --key-base64 "<K>"

python -m db_ops.control.cli worker-run --key-base64 "<K>" -- \
    python -m db_ops.db.cli migrate-sqlite-to-postgres --key-base64 "<K>" --dry-run

python -m db_ops.control.cli worker-run --key-base64 "<K>" -- \
    python -m db_ops.db.cli migrate-sqlite-to-postgres --key-base64 "<K>"
```

Restartability is per table: each table is truncated immediately before it is loaded, so an interrupted run resumes with `--only-tables <names>` without duplicating rows. Add `--skip-indexes` when loading in several passes and run once more without it at the end.

### Cutover

A migration against the live store is a point-in-time copy — the daemon keeps writing while it runs. So the switch is done with the daemon stopped:

```bash
# 1. deploy the current build, so the worker has the store CLI (backend still sqlite)
python -m db_ops.control.cli deploy --key-base64 "<K>"

# 2. stop the daemon so SQLite stops changing
ssh <worker> 'cd /opt/db_ops && docker compose stop db_ops_daemon'

# 3. provision + migrate + verify (run on the worker: both databases are local there)
python -m db_ops.db.cli create-store-database --key-base64 "<K>"
python -m db_ops.db.cli migrate-sqlite-to-postgres --key-base64 "<K>"
python -m db_ops.db.cli verify-migration --key-base64 "<K>"

# 4. flip data/store_config.json:  "backend": "postgresql"
# 5. deploy again so the worker gets the changed config, and start the daemon
python -m db_ops.control.cli deploy --key-base64 "<K>"
python -m db_ops.db.cli check --counts --key-base64 "<K>"
```

**`--delta` is what you want for a re-run.** A full copy of this store is ~10 minutes; the delta is ~3. Per table it picks one of three strategies: reload whole (small, or no identity key — exact, and the only way to pick up `UPDATE`s, which nearly every store table receives), append rows above the target's max id plus re-sync the recent window, or — for a keyless table like `metric_results_archive` — delta on its timestamp column. It also **prunes** rows the source no longer has, because `archive_old_results()` trims old rows and an append-only delta would otherwise leave the target *larger* than the source.

`snapshot-sqlite` gives a consistent source without stopping anything (needs free disk equal to the store size), but a migration from a snapshot still misses whatever the daemon wrote afterwards — it is for rehearsing and timing the cutover, not for the cutover itself.

**Rolling back** is setting `backend` back to `sqlite` and deploying. The SQLite file is not modified by the migration (it is opened read-only), so it remains a valid store for as long as you keep it. Anything written while PostgreSQL was live stays only in PostgreSQL.

Both backends are supported, so nothing blocks the flip. Two things to know before doing it:

* `db_ops.jobs.status` and `db_ops.db.cli check` both report the active backend — use them to confirm what a node is actually writing to, rather than inferring it from a path. (`db_ops.jobs.cli` is the daemon runner and has no subcommands; the status report is its own module.)
* The worker's image must contain the store CLI. An image predating it cannot report its own status, and `control worker-status` now says so and exits rather than falling back to a second, divergent SQLite query with a hardcoded path.

### Path Resolution Priority

1. `sqlite_path` set explicitly in the config being loaded — still supported, still what the standalone-EXE layouts use, and it overrides the store file.
2. An inline `store` block in the main config, merged key-by-key over the file.
3. `data/store_config.json` (or `store_config_file` / `DB_OPS_STORE_CONFIG` when they point elsewhere).
4. The tool default, `<tool_root>/runtime/db_ops.sqlite`.

Every app keeps reading `config.sqlite_path`; that value is now derived from the store declaration, so the two can never point at different files.

## Schema Initialization

Most tables are initialized by `DbOpsStore.initialize()` in `db_ops/db/store.py`. Metrics also initialize `metric_runs`, `metric_results`, `metric_results_archive` and `target_health` from `db_ops/db/metric_store.py`. Backup restore initializes `backup_restore_history` from `db_ops/db/backup_restore_history.py`. The SLA/SLO app initializes `sla_runs` and `sla_results` from `db_ops/db/sla_store.py`.

The config mirror initializes `config_sources`, `config_collections`, `config_items` and `config_item_revisions` from `db_ops/db/config_store.py`. The web console initializes `web_users`, `web_sessions` and `web_login_attempts` from `db_ops/db/web_auth_store.py`, and the run queue initializes `app_command_requests` from `db_ops/db/run_requests.py`.

`python -m db_ops.db.cli init` runs all seven against the active backend, which is the easiest way to create a store from scratch.

On SQLite the store enables WAL mode, `synchronous=NORMAL`, foreign keys and a busy timeout on each connection; on PostgreSQL those pragmas are skipped and `search_path` is set to the configured schema instead. Schema migrations add missing columns and rebuild selected Telegram/report tables when older shapes are found — these are additive and idempotent, and they work on both backends because `PRAGMA table_info` is translated rather than skipped.

Each store class takes either a path or a config:

```python
DbOpsStore(config.sqlite_path)        # a path always means SQLite
DbOpsStore.from_config(config)        # follows data/store_config.json
```

The path form is deliberate: ~20 call sites in `db_ops/` and the whole test suite (~75 together) pass one, and none of them should be able to reach a real PostgreSQL server by accident. Application code should use `from_config`.

## Table Ownership by App

| Table | Main owner | Used by |
| --- | --- | --- |
| `schema_meta` | Runtime Store | Schema version tracking. |
| `job_runs` | Runtime Store / App Command Daemon | App command starts, finishes, failures, timeouts, and manual CLI job events. |
| `job_runs_history` | Runtime Store | Rows aged out of `job_runs` (15-day retention). Moved, not deleted — same trade as `metric_results_archive`. |

`job_runs` cannot be pruned on its own: `app_command_requests.job_run_id` is a real foreign key with no `ON DELETE`, so one finished request pointing into the batch failed the whole delete — and the daemon swallows a failed sweep by design, so the busiest table in the store stopped pruning and said so in one log line per interval. The archive moves the referencing requests first, in the same transaction. Found on 2026-09-04 on a live daemon: `23503 … Key (log_id)=(1601189) is still referenced from table "app_command_requests"`.

| `sql_runs` | SQL Task Runner | SQL task execution history and interval checks. |
| `metric_runs` | Metrics Engine | One row per metrics collection run. |
| `metric_results` | Metrics Engine | Raw metric results; Reports and SLA read this table. |
| `metric_results_archive` | Metrics Engine | Rows aged out of `metric_results` — same columns plus `archived_at`. Kept, not deleted; the largest table after `metric_results`. |
| `target_health` | Metrics Engine | Per-target health summaries rebuilt from a metric run. |
| `report_types` | Reports App | Report type lookup and routing level. |
| `reports` | Reports App | Generated report payloads and push status. |
| `report_send_state` | Reports App | Scheduled report dedupe and last-run state by report/channel. |
| `telegram_messages` | Telegram App | Saved Telegram updates. |
| `telegram_command_messages` | Telegram App | Parsed `/spbot...` command rows. |
| `telegram_conversation_states` | Telegram App | ForceReply-style pending conversation state. |
| `telegram_send_messages` | Telegram App | Outgoing Telegram send queue. |
| `telegram_background_tasks` | Telegram App | In-flight background restore subprocesses (pid, stdout/stderr paths, status). |
| `backup_restore_history` | Backup Restore App | Restore history for database/file/status checks. |
| `sla_runs` | SLA/SLO App | One row per SLA validation run (per-status counts and window end). |
| `sla_results` | SLA/SLO App | One row per policy **per instance**: SLI, SLO, error budget, and status. |
| `config_sources` | Runtime Store / Web Host | One row per synced `data/*.json` file, with the app that owns it. |
| `config_collections` | Runtime Store / Web Host | One row per keyed array inside a file, with the field(s) that key a record. |
| `config_items` | Runtime Store / Web Host | The config itself, one row per record. Soft-deleted only (`is_active`). |
| `config_item_revisions` | Runtime Store / Web Host | Every recorded state of a config row: what it said, when, and who changed it. |
| `web_users` | Web Host | Console accounts: username, PBKDF2 password hash, level 1..100. Disabled, never deleted. |
| `web_sessions` | Web Host | Signed-in sessions, three months by default. Holds a token *fingerprint*, never the token. |
| `web_login_attempts` | Web Host | Every login attempt, successful or not, with the reason, IP and user agent. |
| `app_command_requests` | Web Host / App Command Daemon | "Run now" requests. The console writes them; the daemon starts the command and links the run back. |
| `app_command_requests_history` | Web Host / App Command Daemon | Requests whose run has aged out of `job_runs`. Moved with it, in the same transaction — the foreign key is what makes the order matter. **Created by the sweep when it is missing**: `RunRequestStore` builds it only when the console runs, so every store upgraded from an earlier build had the requests table and no archive, and the sweep went on failing. Created once before the first batch, never inside one — `executescript` commits, and copy+delete being one transaction is what stops a row being archived twice. |

## Core Tables

Key columns, read from the live PostgreSQL store's `information_schema.columns`. (`runtime/db_ops_sqlite_structure.json` is a **SQLite-only, point-in-time** export — see [Schema Export Command](#schema-export-command) — so verify against the active backend, not against that file.)

| Table | Key columns |
| --- | --- |
| `job_runs` | `log_id`, `created_at`, `started_at`, `finished_at`, `job_code`, `level`, `status`, `message`, `duration_ms`, `error_text`, `host_name`, `metadata_json`. |
| `sql_runs` | `sql_run_id`, `created_at`, `run_key`, `sql_id`, `sql_code`, `target_no`, `server_id`, `db_type`, `service_name`, `instance_name`, `database_name`, `credential_name`, `status`, `level`, `message`, `started_at`, `finished_at`, `duration_ms`, `row_count`, `result_json`, `error_text`, `metadata_json`. |
| `metric_runs` | `run_id`, `started_at`, `finished_at`, `status`, `target_count`, `metric_count`, `result_count`, `error_count`, `warning_count`, `critical_count`, `message`. |
| `metric_results` | `result_id`, `run_id`, `target_id`, `server_id`, `ip`, `db_type`, `db_name`, `metric_code`, `metric_item`, `metric_value`, `metric_unit`, `status`, `importance`, `message`, `collected_at`, `daily_report_created`; plus the collector-diagnostic columns `raw_stdout`, `raw_stderr`, `exit_code`, `execution_time`, `collector_type`, `category`, `error_type`, `normalized_error_signature`. |
| `metric_results_archive` | Every `metric_results` column, plus `archived_at`. |
| `target_health` | `target_health_id`, `run_id`, `target_id`, `server_id`, `ip`, `db_type`, `db_name`, `status`, `score`, count columns, `message`, `collected_at`. |
| `reports` | `report_id`, `report_code`, `report_name`, `report_type`, `report_level`, `status`, `report_text`, `source_type`, `source_id`, `created_at`, `pushed_at`, `telegram_send_message_id`, `metadata_json`. |
| `telegram_send_messages` | `send_tlgmsg_id`, `row_ins_date`, `tlgchat_id`, `message_text`, `send_status`, `send_date`, `message_id`, `reply_message_id`, `source_type`, `source_id`, `message_type`, `metadata_json`. |
| `job_runs_history` | Every `job_runs` column, plus `archived_at`. |
| `app_command_requests_history` | Every `app_command_requests` column, plus `archived_at`. No foreign key and no constraints: an archive that can refuse a row is one that can block the sweep. |
| `backup_restore_history` | `restore_id`, `database_name`, `backup_file`, `restore_start`, `restore_end`, `duration_seconds`, `status`, `error_message`, `created_at`. |
| `sla_runs` | `sla_run_id`, `started_at`, `finished_at`, `status`, `policy_count`, `result_count`, `passed_count`, `at_risk_count`, `failed_count`, `no_data_count`, `window_end`, `message`. |
| `sla_results` | `sla_result_id`, `sla_run_id`, `policy_id`, `name`, `target_id`, `scope`, `category`, `status`, `objective_percent`, `actual_percent`, `error_budget_percent`, `budget_consumed_percent`, `budget_remaining_percent`, `total_count`, `good_count`, `bad_count`, `no_data`, `window_hours`, `window_start`, `window_end`, `failures_by_status_json`, `collected_at`. |
| `config_sources` | `config_source_id` (PK), `source_file` (UNIQUE), `app_code`, `display_name`, `description`, `source_ord`, `is_active`, `created_at`, `updated_at`. |
| `config_collections` | `config_collection_id` (PK), `config_source_id` (FK), `collection`, `key_fields_json`, `label_field`, `collection_ord`, `is_active`; UNIQUE `(config_source_id, collection)`. |
| `config_items` | `config_item_id` (PK), `config_collection_id` (FK), `item_key`, `item_ord`, `label`, `item_json`, `content_hash`, `metadata_json`, `revision`, `is_active`, `created_at`, `updated_at`, `deactivated_at`, `updated_by`, `note`. |
| `config_item_revisions` | `config_revision_id` (PK), `config_item_id` (FK), `revision`, `item_json`, `content_hash`, `is_active`, `change_type`, `changed_at`, `changed_by`, `note`; UNIQUE `(config_item_id, revision)`. |
| `web_users` | `web_user_id` (PK), `username`, `display_name`, `email`, `password_hash`, `password_ref`, `user_level`, `is_active`, `failed_login_count`, `locked_until`, `last_login_at`, `created_at`, `updated_at`, `deactivated_at`, `created_by`, `updated_by`, `note`. |
| `web_sessions` | `web_session_id` (PK), `web_user_id` (FK), `token_fingerprint` (UNIQUE), `csrf_token`, `issued_at`, `expires_at`, `last_seen_at`, `client_ip`, `user_agent`, `is_active`, `revoked_at`, `revoked_reason`. |
| `web_login_attempts` | `web_login_attempt_id` (PK), `web_user_id` (FK, nullable), `username_tried`, `succeeded`, `reason`, `client_ip`, `user_agent`, `attempted_at`. |
| `app_command_requests` | `request_id` (PK), `app_command_id`, `status` (pending/claimed/started/done/cancelled/expired), `requested_by`, `request_source`, `requested_at`, `claimed_at`, `started_at`, `finished_at`, `job_run_id` (FK -> `job_runs.log_id`), `note`. |

## Important Indexes

High-use indexes include:

- `ix_job_runs_job_code_created_at`, `ix_job_runs_status_created_at`
- `ix_sql_runs_run_key_created_at`, `ix_sql_runs_sql_code_created_at`, `ix_sql_runs_status_created_at`
- `ix_metric_results_target_metric_collected`, `ix_metric_results_collected_at`, `ix_metric_results_status`, `ix_metric_results_daily_report_created`
- `ux_target_health_run_target`, `ix_target_health_status`, `ix_target_health_collected_at`
- `ix_reports_status_created`, `ix_reports_code_created`, `ix_report_send_state_updated`
- `ix_telegram_send_messages_status_created`, `ix_telegram_command_messages_status_date`
- `ix_backup_restore_history_status_created`, `ix_backup_restore_history_db_created`
- `ux_config_items_active` — **partial**, over `(config_collection_id, item_key) WHERE is_active = 1`. Only one *active* record may hold a key, so retiring one frees the key for a new record without deleting the old row.
- `ix_config_items_collection`, `ix_config_items_key`, `ix_config_sources_app`, `ix_config_item_revisions_item`
- `ux_web_users_active` — **partial**, over `username WHERE is_active = 1`. Same rule as config: disabling an account keeps its row and frees the username.
- `uq_web_sessions_token`, `ix_web_sessions_user`, `ix_web_sessions_expires`, `ix_web_login_attempts_user`, `ix_web_login_attempts_username`
- `ux_app_command_requests_pending` — **partial**, over `app_command_id WHERE status = 'pending'`. One queued run per app: an impatient double-click is told it is already queued instead of running the app twice.

## Config Mirror (`config_*` tables)

The store holds a copy of `data/*.json`, **record by record**. This is what lets something other
than a text editor change what db_ops runs: a web UI can list what is configured, edit one record,
and show who changed it — none of which a JSON file on a worker's disk can answer.

`data/config_catalog.json` declares which files are mirrored, which app owns each, and how one
record inside a file is identified. It is also the allow-list: a file not in the catalog is never
synced. `data/encrypted_secret_text.json` is deliberately absent, as are the `*.example.json`
samples and the generated `database-inventory.json`.

Each file becomes:

- one `config_items` row per record in a declared collection (`metric_code`, `group_id`,
  `sql_id`+`target_no`, …), and
- one `__document__` row holding everything the file has that is not a keyed record — the scalars,
  the `notes` arrays, the nested policy objects.

Together those rebuild the file exactly (`config_sync.rebuild_payload`), which is what makes the
store a mirror rather than a lossy summary. `tests/test_config_sync.py` holds every catalogued file
to that round trip.

**Nothing is ever deleted.** A record that has left its file is flagged `is_active = 0` and keeps
its row, its JSON and its revision trail. Because `ux_config_items_active` is *partial*, the key is
then free again: re-adding it inserts a **new** row beside the retired one instead of resurrecting
it, so the old record still reads exactly as it did when it was retired.

```bash
# what would change, writing no config row
python -m db_ops.db.cli sync-config '{"dry_run": true}'

# mirror everything, recording who did it
python -m db_ops.db.cli sync-config '{"actor": "thanh"}'

# one file, or one app's files
python -m db_ops.db.cli sync-config '{"files": ["metric_definitions.json"]}'
python -m db_ops.db.cli sync-config '{"apps": ["telegram"]}'

# read it back (the read side the web UI is built on)
python -m db_ops.db.cli config-items '{"app_code": "metrics", "payloads": false}'
```

**The mirror runs both ways.** `sync-config` reads the files into the store; `export-config`
writes the store back out. The second direction is what makes an edit in the web console take
effect: the apps still read `data/*.json`, so a change that stopped at the store would be one the
operator watched succeed and that nothing acted on. Every console write does both, and the file is
rebuilt from the store rather than patched, so the two cannot drift apart.

```bash
# store -> data/*.json (the console does this on every save)
python -m db_ops.db.cli export-config '{}'
python -m db_ops.db.cli export-config '{"files": ["reports_config.json"]}'
```

A file is left untouched when its rebuilt text already matches — these files sit on a bind mount
the deploy compares, and an identical rewrite still shows as an edit nobody made. A file the store
knows nothing about is **skipped, never emptied**: no rows means "the sync has not run", never
"the file should be blank".

Editing goes through `db_ops/db/config_edit.py`, which the console and the CLI both call, so a
record saved from a browser is validated by exactly the rules a record saved from a shell is. It
refuses: renaming a record in place (that is a delete and an add, and doing half of it silently
leaves two records under one identity), a key that is already live, a literal secret, and retiring
the `__document__` row — which holds every non-record key in the file, so "delete" on it means
"empty the file".

**The store and `data/` can drift, and a deploy checks.** The store is shared between master and
worker; `data/` is per node. A record edited in the console lands in the store and in the
*worker's* files, leaving the master behind — so `deploy` and `build-image` compare the two first
and stop rather than shipping the old values back. See "The config-drift gate" in
[11_control_app.md](11_control_app.md).

Both commands take the JSON-object payload every db_ops CLI command takes, and both accept a
`store` declaration block so a caller can name a store that is not this node's own.

Three things are refused rather than stored, each per file, so one bad file costs one file and not
the estate:

- **two records on one key** — one would silently overwrite the other;
- **a literal secret** (`password`, `bot_token`, …) — these rows are queried, rendered and backed
  up; db_ops names secrets by ref (`password_ref`) and the value belongs in the encrypted store;
- **a file that is missing** is reported, never applied — deactivating every record of a file that
  is simply not on this node would look exactly like an operator deleting them all.

## Console Accounts (`web_*` tables)

The web console (`docs/12_webhost_app.md`) keeps its accounts and sessions here rather than in a
file, for the same reason the config mirror exists: a browser cannot edit a JSON file on a
worker's disk, and "who is signed in" is state.

Two things are deliberately **not** in these tables:

- **the password** — only a PBKDF2-HMAC-SHA256 encoding of it, at the same 200 000 iterations
  `data/encrypted_secret_text.json` uses (the constant is imported from `db_ops.lib.secret_text`,
  so the tool has one KDF setting and not two);
- **the session token** — only its SHA-256 fingerprint. Reading `web_sessions` from a backup, a
  replica or a psql prompt does not let anyone log in as anybody.

`password_ref` names an entry in `data/encrypted_secret_text.json` holding a **readable copy** of
the password — the same place every database credential already lives. It is written by
`user-add` / `user-password` unless `--no-remember` is passed, and read back with
`user-password-show`. Be clear about the trade: with that copy the hash is no longer the only one,
and anyone holding the passphrase can read the password. It is a small reduction *here* because
the same file already holds the postgres superuser and the SQL Server DBA logins; and the login
path never consults it, so it is a note for a person, not a second way in.

A session expires on its own `expires_at`, applied when it is read, so a stale session stops
working whether or not any sweeper is running. Accounts are managed from
`python -m db_ops.webhost.cli user-add | user-level | user-password | user-password-show |
user-disable | sessions`.

## Run Requests (`app_command_requests`)

The console's "Run now" button does not run anything. It writes a row here, and the **daemon**
starts the command on its next scan — because the daemon owns the working directory, the log
scope, the forwarded secret key, the timeout reaper and the `job_runs` row every run writes. A
console that spawned its own subprocess would be a second executor with none of that: no reaper,
so a hung command runs forever, and a `job_runs` row saying `running` until somebody notices.

```bash
python -m db_ops.db.cli run-app '{"app_command_id": "APP-METRICS", "requested_by": "thanh"}'
python -m db_ops.db.cli run-app '{"list": true}'
```

What the shape guarantees:

- **One run per request.** `ux_app_command_requests_pending` is partial over
  `app_command_id WHERE status = 'pending'`, so a double-click queues once and the second press is
  told so.
- **It overrides the schedule, never a run in flight.** A request ignores the allowed-hours window
  and the repeat interval — "run it now" is asked outside the window, right after the last run —
  but never starts a second copy of something already running.
- **It expires.** A pending request older than 15 minutes is retired instead of fired: if nobody
  picked it up the daemon was down, and running the app when it comes back means running it at a
  moment nobody chose.
- **The run is ordinary.** It writes the same `job_runs` row a scheduled run writes, with
  `run_request_id` and `requested_by` in the metadata, so "why did this run at 03:00" is
  answerable from `job_runs` alone.
- **It closes.** The daemon marks the request `done` as it reaps the process, and sweeps any left
  `started` by a daemon that was killed before it could.

## Queries Used by Apps

The App Command Daemon reads latest `job_runs` grouped by `job_code` to decide if each `app_command_id` is due.

The SQL Task Runner reads latest `sql_runs` by `run_key` and writes one row per attempted task/target run. It also reads **every** row still `status='running'` (`fetch_running_sql_runs`, served by `ix_sql_runs_status_created_at`) before each scan: a run abandoned by a killed process stops being the latest of its `run_key` as soon as the next cycle starts, and the reaper that turns it into an `error` and alerts on it would never see it again.

The Metrics Engine writes `metric_runs`, writes many `metric_results`, then rebuilds `target_health` for the run.

The Reports App reads latest `metric_results`, writes `reports`, updates `report_send_state`, and pushes rows into `telegram_send_messages`.

The Telegram App reads pending queue rows where `telegram_send_messages.send_status = 0`, writes send outcomes with `send_status`, `send_date`, and `message_id`, saves updates into `telegram_messages`, and copies command messages into `telegram_command_messages`.

The Backup Restore App writes `backup_restore_history` for restore starts/finishes and also records workflow-level events in `job_runs`.

## Useful Manual Troubleshooting Queries

```sql
-- latest metric results
SELECT *
FROM metric_results
ORDER BY collected_at DESC, result_id DESC
LIMIT 100;

-- pending Telegram queue
SELECT *
FROM telegram_send_messages
WHERE send_status = 0
ORDER BY row_ins_date ASC, send_tlgmsg_id ASC;

-- failed or errored app command runs
SELECT *
FROM job_runs
WHERE status IN ('error', 'timeout', 'FAILED')
   OR level IN ('error', 'critical')
ORDER BY created_at DESC, log_id DESC;

-- latest restore runs
SELECT *
FROM backup_restore_history
ORDER BY restore_start DESC, restore_id DESC
LIMIT 20;

-- latest SQL task failures
SELECT *
FROM sql_runs
WHERE upper(status) <> 'SUCCESS'
ORDER BY created_at DESC, sql_run_id DESC
LIMIT 50;

-- report rows waiting to be pushed
SELECT *
FROM reports
WHERE status = 'created'
ORDER BY created_at ASC, report_id ASC;
```

## Cleanup / Retention Queries

**`job_runs` retention is automatic too — the daemon sweeps it.** `db_ops.jobs.daemon` calls
`DbOpsStore.archive_old_job_runs(retention_days=15)` once every 300 s, right after it has
scheduled due app commands, and **moves** aged rows into `job_runs_history` (`archived_at`
stamped on the way in). Nothing is deleted.

Two properties make it safe to run from the scheduler loop:

* **Bounded work per pass** — one 20 000-row transaction, capped by `max_batches`. The first
  sweep on this store had ~2.5 months to clear; uncapped, that single move would have stalled
  app-command scheduling for as long as it took. Capped, the backlog drains over successive
  passes and steady state (~13k rows/day age out) finishes inside the first batch every time.
* **Every failure is swallowed.** A daemon that cannot archive is a table that grows; a daemon
  that dies is every app command on the node not running.

Tuning lives in `db_ops/jobs/daemon.py` (`JOB_RUNS_RETENTION_DAYS`,
`JOB_RUNS_SWEEP_INTERVAL_SECONDS`, `JOB_RUNS_SWEEP_BATCH_SIZE`, `JOB_RUNS_SWEEP_MAX_BATCHES`).
A sweep that moved anything logs `app.daemon.job_runs_sweep`.

**Metric retention is automatic — do not hand-delete `metric_results`.** Every `metrics collect`
run starts by calling `MetricStore.archive_old_results(retention_days=archive_days)`
(`metrics/collector.py`, default 30 days, `--archive-days` on the CLI). It **moves** aged rows into
`metric_results_archive` in one transaction; nothing is dropped. A manual `DELETE` here destroys
history the archive was built to keep, and it is why `metric_results_archive` is the second-largest
table in the store.

The remaining tables have no automatic retention, so the queries below are the manual path. **They
are PostgreSQL** — the active backend. SQLite's `strftime('…','now','-N days')` does not exist in
PostgreSQL, and inlining it is exactly the bug that killed the metrics and SLA app commands right
after the cutover (see the pitfalls table above); the store's own code now computes every window in
Python and binds it.

Timestamps are stored as ISO-8601 UTC **text** (`2026-07-31T06:21:18Z`), not as `timestamptz`, so a
cutoff is compared as a string:

```sql
-- sent Telegram queue rows older than 30 days
DELETE FROM telegram_send_messages
WHERE send_status = 1
  AND row_ins_date < to_char(now() AT TIME ZONE 'UTC' - interval '30 days',
                             'YYYY-MM-DD"T"HH24:MI:SS"Z"');

-- saved Telegram updates and parsed commands older than 30 days
DELETE FROM telegram_command_messages
WHERE created_at < to_char(now() AT TIME ZONE 'UTC' - interval '30 days',
                           'YYYY-MM-DD"T"HH24:MI:SS"Z"');

DELETE FROM telegram_messages
WHERE created_at < to_char(now() AT TIME ZONE 'UTC' - interval '30 days',
                           'YYYY-MM-DD"T"HH24:MI:SS"Z"');

```

`job_runs` is deliberately **not** in that list any more: the daemon sweep owns it, and a manual
`DELETE` there destroys history the archive exists to keep. To read archived runs, query
`job_runs_history` — same columns, plus `archived_at`.

Count first with the same predicate, run inside a transaction, and confirm local retention
requirements before committing. On a SQLite store the equivalent cutoff is
`strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 days')` — do not paste that at PostgreSQL, and do not
paste the PostgreSQL form at SQLite.

## Schema Export Command

```powershell
python -m db_ops.cli --config config.json --export-sqlite-schema --schema-output-dir runtime
```

**This is SQLite-only, whatever the active backend is.** `db/schema_export.py` opens
`DbOpsStore(sqlite_path)` — the path form, which always means SQLite — initializes it, and reads
`sqlite_master`/`PRAGMA`. On a PostgreSQL node it therefore describes the local
`runtime/db_ops.sqlite` file, not the store the apps are writing to, and `runtime/db_ops_sqlite_structure.json`
is a snapshot of exactly that. It stays useful for the migration path (the SQLite schema is what
`migrate-sqlite-to-postgres` introspects) and as a schema reference, since both backends are built
from the same four `initialize()` methods.

To describe the **live** store instead, ask the active backend:

```bash
python -m db_ops.db.cli --config config.json check --counts        # tables, schema_version, rows
```

```sql
-- columns of the live PostgreSQL store
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'db_ops'
ORDER BY table_name, ordinal_position;
```
