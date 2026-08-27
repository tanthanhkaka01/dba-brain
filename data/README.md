# Data — what the toolkit runs, monitors and delivers

**This folder is the configuration.** Every threshold, target, route, schedule and policy lives
here as JSON you own; none of it is a literal in Python. That is the assumption the whole design
rests on — the person affected by a setting can read it, change it, and be reviewed on the change.

**Start from the examples.** Every file below has a `*.example.json` beside it, complete enough to
copy, rename and edit, each carrying a `notes` array explaining what it decides and why. The
examples are the reference; this page is the map.

Paths here are relative to the **tool root** — the directory holding `config.json` and this
folder — so `data/store_config.json` means the `store_config.json` next to this README. How that
directory is located (`DB_OPS_HOME`, then the working directory, then the package) is in
[`docs/configuration.md`](../docs/configuration.md).

`config.json` at the tool root holds only runtime settings — `log_dir`, `runtime_dir`, log levels,
and pointers to the store and Telegram declarations. Everything an app *decides* belongs here.

## Conventions

- `*.example.json`: the sample. Committed, safe to publish, every value a placeholder.
- `*.json`: your real configuration. Never commit one — they carry addresses, chat ids and account
  names, and `.gitignore` does not know which of yours is which.
- **No secret value is ever in this folder** except inside `encrypted_secret_text.json`, which is
  ciphertext. Everywhere else a password is a *reference* by name. See
  [`docs/security.md`](../docs/security.md).
- **Every file here must be read by something.** `notify_levels.json` and `metric_groups.json` were
  deleted on 2026-08-21: both came over from the old SQL Server job tables, both looked exactly like
  live config, and no code had ever read either. Once the web console could edit them that stopped
  being harmless — an editor for settings nothing acts on is a trap, because the save succeeds and
  the estate ignores it. `tests/test_config_sync.py` now fails if a catalogued file has no reader.

  **Both files came back on 2026-08-25**, four days later, and nothing said so.
  `worker-pull-data-config --all-json` listed the *worker's* directory and copied anything the
  master did not have — which, without `--overwrite`, is the only thing that sweep can do, and is
  exactly the set somebody has just deleted. A deletion the next sweep undoes is not a deletion.
  That is why `data_files.json` exists.
- **Every file here is listed in `data_files.json`, and nothing that is not listed travels.** The
  manifest says who owns each file and how it moves between the master and the worker; deploy, the
  pull and the merges all read it before they read anything else. Adding a `data/*.json` without
  adding it there fails `tests/test_data_files_manifest.py` — in both directions, so an entry
  cannot outlive its file either.

## What each file decides

Grouped the way they are reached for. Several of these began life as tables in a SQL Server job
scheduler, which is why a few field names still read like columns.

- `telegram_groups.json`: Telegram routing groups for `logging`, `warning`, `error`, plus command permission.
- `bot_telegram.json`: Telegram bot metadata and `telegram_bot_token_ref`; the real token is stored encrypted in `data/encrypted_secret_text.json` (plaintext source `secrets/secret_text.json`, never committed) and decrypted at runtime with the passphrase.
- `telegram_users.json`: Telegram users captured from `getUpdates`.
- `telegram_support_commands.json`: Telegram bot support command configuration, based on Oracle `TLG_SUPPORT_COMMAND`.
- `db_instances.example.json`: sample DB instance inventory and metric/status fields for batch Telegram notification.
- `db_instances.json`: current DB instance inventory imported from `architecture/database-inventory.json`; SQL Server metrics are explicitly enabled per target.
  - **`service_name` / the target's `db_name` are labels, not databases.** On SQL Server they are
    the instance's service tag (`APPDB-PROD`, `SALESDB-PROD`) and no database of that name exists.
    Metric collection therefore connects to **`master`** and the metric SQL issues its own
    `USE <db>`; using the label as the connection database fails with
    `Cannot open database "..." requested by the login (4060)`. Set `database` only where the
    engine genuinely needs one (PostgreSQL, MySQL) — see `docs/04_metrics_engine.md`.
  - **`cmd_access.method: "local"` means *this* machine**, i.e. inside the worker container. With
    a remote `host` it does not fail, it reports the container's CPU/memory/disk under that
    host's name. Use `ssh` (Linux) or `winrm` (Windows) with a `credential_name`; a mismatch is
    now refused rather than collected.
- `metric_importance_overrides.json`: default and per-instance importance scores for each metric SQL file.

App-owned config files:

- `store_config.json`: which database db_ops stores its **own** runtime data in — `backend` is `sqlite` or `postgresql`, and the matching section holds the full connection details. See "Runtime Store Backend" below.
- `config_catalog.json`: which files in this folder are mirrored into the runtime store, which app
  owns each, and the field(s) that identify one record inside a file. It is the **allow-list** for
  `python -m db_ops.db.cli sync-config`: a file not listed here is never synced, which is why
  `encrypted_secret_text.json`, the `*.example.json` samples and the generated
  `database-inventory.json` are deliberately absent. Adding a new config file to this folder means
  adding it here too (or to the `NOT_SYNCED` list in `tests/test_config_sync.py`, with the reason).
  See "Config Mirror" in `docs/01_runtime_store.md`.
- `webhost_config.json`: the web console's own settings and the app blocks its dashboard draws.
  The `web` block holds the session and permission rules — `session_days` (the cookie and the
  stored session carry the same lifetime, which is why closing the browser does not sign anyone
  out; shorten it to shorten both), the cookie flags, the lockout thresholds, and the
  `min_level_*` gates checked against a user's level of 1..100. The `apps` array is one entry per
  package under `db_ops/` — all fourteen — with the `app_command_ids` each owns, which is how a
  block shows its schedule and its last run. See `docs/12_webhost_app.md`.
- `app_commands.json`: top-level app command definitions for `db_ops.jobs.daemon`.
- `metric_definitions.json`: metric catalog and SQL variant mapping. Each metric also declares what
  a **failed** collection of it is worth: `connection_error_severity` (never reached the target)
  and `execution_error_severity` (connected, but the check failed) — `WARNING` on every metric
  except `INSTANCE_STATUS`, which is `CRITICAL` on both because an instance that does not answer is
  an outage. See `docs/04_metrics_engine.md`.
- `sql_commands.json`: SQL task command definitions.
- `sql_targets.json`: SQL task target, optional execution `database_name`, `time_window` mapping,
  `time_window.repeat_interval: -1` for manual (never scheduled; forced runs only), and the `output` block
  (`format`: `xlsx` / `plain` / `none` — absent means `plain`; `max_rows` for how many rows a `plain`
  target sends to the chat, 1..5000, default 1000 — past a few hundred use `xlsx` instead, because
  the limit is Telegram's rate limit rather than memory). See `docs/05_sql_task_runner.md`.
- `reports_config.json`: scheduled report definitions consumed by `db_ops.reports.cli run-scheduled`.
- `backup_policy.json`: what each database is required to be backed up with — required FULL/DIFF/LOG
  types (LOG keyed by recovery model) and the warn/critical age for each, with per-server and
  per-database `overrides`. The reports evaluate every eligible database against this rather than
  taking the newest backup on the instance as the instance's answer. See `docs/06_reports_app.md`.
- `restore_config.json`: backup restore source/target/database mapping.
- `maintenance_policy.json`: timing budgets and thresholds for host maintenance operations
  (`db_ops.common.host_ops`, `db_ops.common.sqlserver_patch`): how long to wait for a host to go
  down, come back and finish starting its services, plus the disk/backup-age gates a SQL Server
  cumulative update must pass. Resolution is built-in defaults < `defaults` < `servers.<server_id>`
  < the request's own `wait` block, so one slow host is a config entry rather than a code change.
  See `docs/13_common.md`.
- `sqlserver_instance_policy.json`: what is portable between two SQL Server instances, for
  `db_ops.common.sqlserver_instance` (`sqlserver-export-instance` / `-replay-instance` /
  `-verify-instance`). A SQL Server backup covers user databases only — `master`/`msdb`/`model`
  are excluded — so logins, server roles, credentials, linked servers, endpoints, `sp_configure`,
  Database Mail and SQL Agent survive nowhere in the bundle. This file declares the artifact set
  and its replay order, which phase each belongs to (before or after the user databases are
  restored), and which `sp_configure` values are portable versus host-specific. Oracle and
  PostgreSQL need no equivalent: their physical backups carry this state already. See
  `docs/13_common.md`.
- `sla_policies.json`: SLA/SLO objectives validated against metric history.
- `users.json`: consolidated credentials — `database_credentials` (per-server SQL Server/Oracle login metadata, grouped by `db_type`), `remote_credentials` (OS/SSH/WinRM), and `monitor_users`. Replaces the former `sqlserver_users.json` / `oracle_users.json` / `remote_users.json` / `monitor_users.json`. Names and `password_ref` values only — real secrets live encrypted in `data/encrypted_secret_text.json`.

## Runtime Store Backend

The database db_ops writes its own operational data to (job runs, metric results, reports, SLA
runs, Telegram messages, backup/restore history) is declared in:

```text
data/store_config.json
data/store_config.example.json
```

This is **not** about the databases db_ops monitors — those live in `db_instances.json`.

```json
{
  "backend": "sqlite",
  "sqlite": { "path": "runtime/db_ops.sqlite", "connection_string": "sqlite:///runtime/db_ops.sqlite" },
  "postgresql": { "host": "...", "port": 5433, "database": "db_ops", "schema": "db_ops",
                  "username": "postgres", "password_ref": "POSTGRES_WORKER",
                  "connection_string": "postgresql://postgres:{password}@.../db_ops?..." }
}
```

Rules:

- `backend` picks the live section: `sqlite` or `postgresql` (`postgres` accepted as an alias).
- `connection_string` is authoritative when non-empty; the sibling fields are the readable
  breakdown and build the string when it is blank. Only one of the two is ever read, so the file
  cannot state two different destinations.
- Relative paths resolve against the **tool root**, not `data/` — `runtime/db_ops.sqlite` means
  `<tool_root>/runtime/db_ops.sqlite` on every node.
- No password here. `password_ref` names a key in `data/encrypted_secret_text.json`, decrypted at
  runtime with `DB_OPS_SECRET_KEY` and substituted into the `{password}` placeholder.
- An explicit `sqlite_path` in the config being loaded still overrides this file (the
  standalone-EXE layouts rely on it); an inline `store` block overrides it too.
- **Start on `sqlite`, move to `postgresql` when the store matters** — more than one node writing
  to it, or history you want to outlive the machine. Both engines are fully live: `DbOpsStore` and
  the metrics/SLA/backup-restore stores all go through `db_ops.db.backend`, so `backend` is the
  only switch, and the unused section stays in the file fully specified so switching back is not a
  rediscovery of host, port and credentials. See `docs/01_runtime_store.md`.

Check what a node resolved:

```powershell
python -m db_ops.jobs.cli status
python -m db_ops.db.cli store-info
```

Provision the PostgreSQL store and migrate SQLite into it with `python -m db_ops.db.cli`
(`create-store-database`, `migrate-sqlite-to-postgres`, `verify-migration`, `snapshot-sqlite`).
Run it on the node that owns the SQLite file — see `docs/01_runtime_store.md`.

## Telegram Bot

Bot metadata is stored in:

```text
data/bot_telegram.json
```

The file does not store the token value. `telegram_bot_token_ref` must match a key in the secret store — encrypted at rest in `data/encrypted_secret_text.json`, generated from the plaintext source (ignored, never committed):

```text
secrets/secret_text.json
```

## Telegram Groups

Real data file:

```text
data/telegram_groups.json
```

Sample file:

```text
data/telegram_groups.example.json
```

Standard DB Ops levels:

| Level | Meaning |
| --- | --- |
| `logging` | Normal operational log messages |
| `warning` | Warning messages that need follow-up |
| `error` | Failures that need action |

Command permission:

| allow_command | Meaning |
| --- | --- |
| `0` | No bot commands |
| `1` | User commands |
| `2` | Admin commands, including user commands |

New groups loaded from Telegram `getUpdates` are written with `allow_command = 0` by default. `telegram_groups.json` stores group chats only, where `chat_id < 0`.

Telegram user type:

| user_type | Meaning |
| --- | --- |
| `0` | Disabled/no commands. This is the default for new users loaded from `getUpdates`. |
| `1` | Normal user |
| `2` | Admin |

## Telegram Commands

Command configuration follows the Oracle `TLG_SUPPORT_COMMAND` structure, with `ROLE_NUM` renamed to `command_type`:

```text
data/telegram_support_commands.json
data/telegram_support_commands.md
```

Use `telegram_support_commands.md` as a simple human-editable command list (paste its command block into BotFather `/setcommands`):

```text
command1 - Description
command2 - Another description
```

Runtime command messages are not stored in JSON. They are copied from `telegram_messages` to `telegram_command_messages` in the runtime store when message text starts with `/spbot`.

Commands can be sent with or without the bot username suffix, for example `/spbot_status` and `/spbot_status@it_dev_code_sp_bot`.

SQL-backed commands use `action_type = sql_execute` and `action_config.parameters`, where each parameter maps to an argument position from the Telegram message.

If a required SQL command parameter has `prompt_text` and the user omits that argument, the processor creates a conversation state in the runtime store and queues a ForceReply prompt. The next non-command message from the same chat/user is consumed as that parameter.

Command message status:

| command_status | Meaning |
| --- | --- |
| `0` | Pending/not processed |
| `1` | Processed |
| `-1` | Skipped/not processed |
| `-2` | No matching command configuration |

`command_type` is the **minimum clearance level** a command needs — not a fixed enumeration. Any
level works (1, 2, 3, 10, 100) with no code change; see `db_ops/telegram/commands.py::can_run_command`.

| command_type | Meaning |
| --- | --- |
| `< 0` | Disabled: never runs. Use `-1` to switch a command off. |
| `0` | **Public**: runs for everyone, no clearance checked. |
| `>= 1` | Needs `allow_command >= command_type` **and** `user_type >= command_type`. |

Note `0` is the public tier, **not** "disabled" — only a negative value disables a command.

Chat permission and user permission are both required. Group chat permission is
`telegram_groups.allow_command`; a **private chat uses the user's own `user_type` as the chat
permission**, so a DM is gated on `user_type` alone. Unknown users and unknown chats both resolve
to `0`.

```text
command_type = 2   requires allow_command >= 2  and user_type >= 2
command_type = 10  requires allow_command >= 10 and user_type >= 10
```

### 50 and 100 are the emergency tiers

The clearance number and the danger of an operation rise together, so the emergency commands reuse
`command_type` for both: it decides **who may ask**, and the matching entry in
[`emergency_operations.json`](./emergency_operations.example.json) decides **how hard it is to confirm**.

| Level | What it means | Confirmation | Commands |
| --- | --- | --- | --- |
| `50` | Large blast radius, nothing goes down | one typed `yes` | `/spbot_shrink_log`, `/spbot_kill_spid`, `/spbot_start_job`, `/spbot_disable_job` |
| `100` | Takes a machine or a service down | `yes`, then the **server id typed out** | `/spbot_restart_server` |

The second answer at level 100 is deliberately not a second `yes`. Two identical answers in a row
are one answer typed twice — the hand learns the rhythm and stops reading. Reproducing the target's
own id cannot be done without looking at it, and it means a payload written for one host is refused
against another.

Two files, because they answer different questions and `common` reads no Telegram settings: the
clearance lives here, the confirmation cost lives in `emergency_operations.json`, and
`tests/test_emergency_operations.py` pins that they carry the same number for the same operation.
The confirmation is enforced by the CLI, not by this config — calling
`python -m db_ops.common.cli host-restart` directly costs exactly the same two answers.

Levels in use (raised on 2026-07-31, after an audit found that an unregistered Telegram user
reached the listing commands):

| Level | Commands |
| --- | --- |
| `0` | `spbot_status` |
| `1` | the `spbot_list_*` listings |
| `2` | the read-only `spbot_report_*` commands |
| `10` | everything that writes, executes SQL, or maps the estate — `sql_to_xlsx`, `add_sql`, `run_sql_task`, `restore`, `backup`, `create_db_docker`, `metric_toggle`, `master_cli`, the two `update_*` commands, `json_exp_ticket_detail` |

Fetch Telegram updates and persist messages, groups, and users. Messages are stored in the runtime store, while groups and users remain JSON configuration files:

```powershell
python -m db_ops.telegram.cli --config config.json save-updates --limit 20
```

## DB Instance Metrics Template

Use this file as the starting point for batch DB metrics notification:

```text
data/db_instances.json
data/db_instances.example.json
```

Important fields:

- `ord`
- `ip`
- `instance_name`
- `db_type`
- `service_name`
- `database_names`
- `default_credential_name`
- `os`
- `status_os`
- `status_db`
- `status_connect`
- `metrics.cpu_pct`
- `metrics.memory_used_pct`
- `metrics.disk_used_pct`
- `metrics.blocked_sessions`
- `metrics.backup_age_hours`
- `metrics.last_error`
- `metric_importance`

Use `ip + port + db_type + instance_name + service_name` to identify one monitored database service. `service_name` is a target label, not an expected SQL Server database name. `database_names` is an optional list of known user databases; populate it from inventory or from already-collected database-discovery metric rows such as `DATABASE_STATUS`, `BACKUP_AGE`, and `BACKUP_LAST_RESULT`. Reports must only summarize existing collected rows. `default_credential_name` selects the credential metadata from `data/users.json` (`database_credentials`, matched by `db_type`) for metrics collection, and SQL task targets can fall back to it when their own `credential_name` is blank. That credential file should use names and `password_ref` values only; the real secret values are stored encrypted in `data/encrypted_secret_text.json` (plaintext source `secrets/secret_text.json`, never committed).

## Per-Instance Metric Importance

Per-instance overrides are stored in:

```text
data/metric_importance_overrides.json
```

Use score `0` only when a metric is not applicable for that instance, for example the Oracle 10g+ blocked-session query on Oracle 8i.

## Backup Restore Plan

Backup restore source/target pairs are stored in:

```text
data/restore_config.json
```

This file owns:

- production backup shares;
- restore VM target share and local paths;
- SMB/VM credential refs;
- `copy_recent_hours` and backup file patterns;
- optional `databases[]` source-to-target database mapping.

Leave this out of root `config.json` unless doing a temporary local override.

## SLA/SLO Policies

SLA/SLO compliance policies are stored in:

```text
data/sla_policies.json
```

The SLA app reads metric history from the runtime store and evaluates these policy objectives. Policy thresholds, target ids, metric codes, and windows belong in this file, not in root `config.json`.

## Lab Database Docker Connections

Database Docker instances provisioned by the SRE app are registered in:

```text
data/docker_db_connections.json
```

This file is written/updated automatically by:

```bash
python -m db_ops.sre.cli create-db-docker --name pg_lab_01 --engine postgres \
  --version 16 --mode single --host-port 5433 --password-env POSTGRES_PASSWORD
```

Each entry records `id`, `engine`, `host`, `port`, `database`, `username`,
`password_env` (a **reference**, never the password value), a `docker` block
(`instance_name`, `mode`, `version`, `compose_path`, and `replicas` for ha-lab),
and `created_by`. Registration is an idempotent upsert keyed by `id`
(`<NAME>` upper-cased); pass `--no-register` to skip it.

The instance files themselves (compose + `.env`) live on the worker under
`/opt/db_ops/containers/<name>/`. The `.env` holds `DB_PASSWORD`, resolved at
provision time from the `--password-env` environment variable or the encrypted
secret store — it is intentionally not committed.

Run it inside the worker container from the master, then pull the updated config
back, with the control app:

```bash
python -m db_ops.control.cli worker-create-db-docker --key-base64 "<key>" \
  --name pg_lab_01 --engine postgres --version 16 --mode single \
  --host-port 5433 --password-env POSTGRES_PASSWORD --pull-config

python -m db_ops.control.cli worker-pull-data-config --key-base64 "<key>" \
  --all-json --merge-secrets --overwrite
```

`--merge-secrets` safely unions the worker encrypted store, the master encrypted
store, and the local gitignored `secrets/secret_text.json`. It updates both
master stores and refuses to write when the same ref has different values.

> In-container provisioning drives the host Docker daemon. The runtime compose
> mounts `/var/run/docker.sock` and passes `/opt/db_ops/containers` through at the
> same absolute path; the worker image must also provide a Docker client + compose
> plugin (`apt: docker.io docker-compose-v2`). Use `--dry-run` to preview the
> generated compose/`.env`/connection without touching anything.
