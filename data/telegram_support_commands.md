# Telegram Bot Commands

Paste into BotFather `/setcommands` to register the bot menu.

```
spbot_status - Get bot status
spbot_list_all_command - List every command you can run here, built from the bot's own config
spbot_list_restore_id - List restore IDs with source and target IP
spbot_list_backup_id - List backup IDs with engine, level and schedule
spbot_backup - Run one backup by backup_id, optionally forcing full/diff/log
spbot_report_hourly_metrics - Force hourly metrics report by target IP
spbot_report_metric_history - Report one stored metric for one server over recent hours
spbot_report_inventory - Rebuild the database inventory & health report (HTML)
spbot_create_db_docker - Create a lab database container (postgres/mysql/mssql/oracle) on the worker
spbot_master_cli - Cheat sheet of the master-side CLI commands (deploy, pull config, create DB)
spbot_update_allow_re_inspect - Update Globex AllowReInspect by InternalBarcode
spbot_update_package_barcode - Update Globex Package Barcode
spbot_restore - Run point-in-time restore workflow by restore_id and point_in_time
spbot_json_exp_ticket_detail - Export all ticket detail to json file
spbot_add_sql - Add SQL task
spbot_sql_to_xlsx - Run a read-only SELECT on a server and get the result as an xlsx file
spbot_sql_export - Run a read-only SELECT and get the result as xlsx, csv, txt or xml
spbot_xlsx_to_table - Attach an xlsx or a delimited text file and load it into a new table on a server
spbot_list_server_id - List the database targets (server_id, db_type, ip:port) you can query
spbot_list_sql_tasks - List all configured SQL tasks with their targets and time windows
spbot_run_sql_task - Run one SQL task now by sql_id, ignoring its schedule (optional NAME=VALUE parameters)
spbot_list_metrics - List all metric definitions with their repeat intervals
spbot_metric_toggle - Enable/disable metrics for one server (all, one collector class, or one metric)
spbot_shrink_log - EMERGENCY: shrink one database's log file to N MB (asks yes once)
spbot_kill_spid - EMERGENCY: kill one session, after showing whose it is (asks yes once)
spbot_start_job - EMERGENCY: start one SQL Server Agent job by name (asks yes once)
spbot_disable_job - EMERGENCY: stop a job running on its schedule; lists enabled jobs (asks yes once)
spbot_restart_server - EMERGENCY: restart a server (asks yes, then the server id typed out)
spbot_trace_session - Who is holding an open transaction on SALESDB: AX user, session, age, locks, who is blocked
```

## Command Details

| Command | Description |
|---|---|
| `/spbot_shrink_log` | **EMERGENCY, clearance 50.** `DBCC SHRINKFILE` one database's log down to a size you give in MB. Asks for the server, the database, the size, then one `yes`. Before it asks it reports the file's current size, how much is in use, the recovery model and `log_reuse_wait` — a log waiting on `LOG_BACKUP` is warned about, because shrinking one frees nothing and it grows straight back. Never touches the recovery model: flipping to SIMPLE to force truncation is what breaks a log chain. |
| `/spbot_kill_spid` | **EMERGENCY, clearance 50.** `KILL` one session. Asks for the server and the SPID, then one `yes` — but first it reports whose session it is: login, host, program, database, open transactions, how long the transaction has been open, how long the session has been idle and how many sessions are blocked behind it. Refuses system sessions (`spid <= 50`) and a SPID that is no longer active, because ids get reused. Reports the rollback estimate when there is one. |
| `/spbot_trace_session` | **Read-only.** Every open transaction on `ACME-192-0-2-115` / SALESDB, with **who** is behind it. Through the AOS every session reads `login=axdbadmin host=ACMEAOS04 program=axOnline`, which names nobody; this decodes the AX caller out of `context_info` and resolves it through `USERINFO`, so a line reads `app_user=ACMECEN01.PU (ORDER PROCESSING PU&NON), app_session=4654, tran_age=389m, log_bytes=1875268, locks=3931, blocking=52,1266`. One optional argument: a **SPID**, or **0** (the default) for every transaction older than five minutes. It runs through `run-sql`, which always rolls back, so unlike `/spbot_kill_spid` it changes nothing and asks for no confirmation. `log_bytes` is the field to read before killing anything: static means the session is holding locks and doing nothing, growing means real work would be lost. |
| `/spbot_start_job` | **EMERGENCY, clearance 50.** Start one SQL Server Agent job by exact name. Asks for the server, the job name, then one `yes`. Refuses a job that is already running, warns when the job is disabled (starting it runs it once; it stays disabled on its schedule), and says plainly that success means *started*, not *finished* — check the job history for the outcome. |
| `/spbot_disable_job` | **EMERGENCY, clearance 50.** Stop one scheduled job running on its schedule. Asks for the server, then the job name — **listing the jobs that are actually enabled on that server**, so the exact name does not have to be recalled — then one `yes`. Works on SQL Server (Agent), Oracle (DBMS_SCHEDULER *and* the older DBMS_JOB, dispatching on whichever owns the name) and PostgreSQL (pg_cron); says plainly when a PostgreSQL server has no scheduler installed at all rather than answering "no jobs". Disable only — there is no drop-job, because a dropped Agent job takes its schedule, steps and history with it. A job already disabled reports OK, not an error. **A run already in progress is not stopped.** |
| `/spbot_restart_server` | **EMERGENCY, clearance 100.** Restart a host. Two answers: `yes`, and then the server id typed out in full. The second answer is not a second `yes` on purpose — it cannot be given from muscle memory, and it means a message written for one host is refused by another. Every service on the host stops, sessions are lost, and an FCI may fail over. |
| `/spbot_list_all_command` | List every command **you** can run in **this** chat, with its real arguments and clearance. Built from `telegram_support_commands.json` at run time — a command added to that file appears here with no other edit. Commands above your clearance, commands that only run in the other kind of chat, and disabled ones are hidden, each counted with its own reason. Public (clearance 0), no parameters. |
| `/spbot_status` | Get bot status |
| `/spbot_list_restore_id` | List the restore IDs that can be run: **both** kinds — the SMB restores and the script-driven ones (Oracle/PostgreSQL/SQL Server drills) — with each one's source and target. Only active entries are listed; a footnote counts the inactive ones. No parameters. |
| `/spbot_list_backup_id` | List the backup IDs that can be run: engine, target server, level (`full`/`diff`/`log`, or `auto (Sun=full, else diff)` when the script derives it) and schedule. `[encrypted]` marks a set written with a passphrase. Only active entries; a footnote counts the rest. No parameters. |
| `/spbot_backup` | Run one backup now by `backup_id`, always with `--force` (ignores the schedule, but never a run already in flight). Optional second argument sets the level: `full` \| `diff` \| `log`, or `-` to let the script decide as it would on a scheduled run. One word for every engine — it is translated to that engine's own name (Oracle 0/1, PostgreSQL full/incr, SQL Server full/diff/log). Use `/spbot_list_backup_id` to find the id. |
| `/spbot_report_hourly_metrics` | Force hourly metrics report for a target (a `server_id`, or `<db_type> <ip> [port]`). Resolves to the unique `server_id` internally. |
| `/spbot_report_metric_history` | Report one stored metric for one server over a recent hourly window without collecting metrics |
| `/spbot_report_inventory` | Rebuild the Database Inventory & Health report from the last 7 days of metrics (no parameters) and reply with the report URL |
| `/spbot_master_cli` | Reply with the master-side CLI cheat sheet (run from the repo root): worker status, deploy, pulling config/secrets back, creating a lab database, running anything inside the worker, report URLs. Static reply — it runs nothing. |
| `/spbot_create_db_docker` | Create a lab database container, on the worker host **or directly on a remote Ubuntu VM over SSH** — one unified parameter set for every engine. Private chat only. Fourteen parameters, in order: **name · engine · version · mode · host_port · password_env (DB secret ref) · password_text (DB password value) · deploy_target · remote_user · remote_password_ref · remote_password_text · remote_key_name · recreate · install_docker**. See the Usage section below for what each means and inline examples. Both passwords come in two forms — a **ref** already in the encrypted secret store, or a **text value** (handed to the CLI through the environment, never rendered onto a command line). `deploy_target` = `worker` runs in-container on the worker host (as before); an **IP** provisions on that Ubuntu VM over SSH (`remote_user` + one of the SSH password forms). Engines: `postgres`/`mysql`/`mssql`/`oracle` (26ai = tag `23.26.2`/`latest`; oracle `ha-lab` = Data Guard 1+1). `recreate=yes` = `--force`: **destroys the existing containers and their data volumes**. Afterwards pull the new connection + secret back to the master with `control worker-pull-data-config --all-json --include-secrets --overwrite`. |
| `/spbot_update_allow_re_inspect` | Update Globex AllowReInspect by InternalBarcode |
| `/spbot_update_package_barcode` | Update Globex Package Barcode |
| `/spbot_restore` | Run point-in-time restore workflow by restore_id and point_in_time |
| `/spbot_json_exp_ticket_detail` | Export all ticket detail to JSON file and send it back as a Telegram document |
| `/spbot_add_sql` | Register and enable a new SQL task from a conversation: **server_id → sql_name → schedule → output → sql_text**. `db_type`, instance and credential are read from `db_instances.json` by `server_id`, never typed. Schedule accepts `manual` (runs only via `/spbot_run_sql_task`), `default`, or `from_hour to_hour repeat_interval timeout`. Output accepts `xlsx` (send a workbook), `plain` (rows as text), `none` (status only). Private chat only. |
| `/spbot_sql_export` | The same read-only SELECT as `/spbot_sql_to_xlsx`, but you choose the file: **xlsx**, **csv**, **txt** (aligned table) or **xml**. Asks for a **target**, then the **format**, then the SQL text. The format comes *before* the SQL because the SQL argument consumes the rest of the message. Clearance 10, runs on the worker. |
| `/spbot_sql_to_xlsx` | Run a **read-only SELECT** on a server and send the first result set back as an `.xlsx` document. Asks for a **target** (a `server_id`, or `<db_type> <ip> [port]`) and the SQL text (paste it or attach a `.sql` file). Works on **any** engine in the inventory — SQL Server, PostgreSQL, MySQL, Oracle. Any statement that changes rows is refused and rolled back — but rollback is **not** a sandbox (see `db_ops/common/sql_run.py`), so this runs as the instance's DBA login. Clearance 10, runs on the worker. |
| `/spbot_xlsx_to_table` | Attach an `.xlsx` **or a delimited text file** (`.txt` / `.csv` / `.tsv`, including a block copied straight out of Excel — tab, comma, semicolon or pipe) and get a **queryable table**. Asks for a **server_id**, a **database**, a **schema**, then the file; the format is read from the file itself, not from its name. Every column is created `NVARCHAR(4000)` (or the engine's equivalent) — a type guessed from a spreadsheet is wrong on the row nobody checked; `ALTER` afterwards, having seen the data. Row 1 (of the first sheet, for a workbook) is the column names; blanks become `column_2`, duplicates get a suffix. For a text file the reply says how it was read (`tab-delimited text (utf-16-le)`), because the delimiter and encoding are guessed. The table name is **not** asked for: it is `temp_<random>` and the reply tells you what it was. Pass a 5th word to choose one. **An existing table is never overwritten** — the command fails and says so. **No size limit of ours**, but Telegram itself will not serve a bot more than **20 MB** per file, whatever you attached; above that, put the file on the worker and use the `create-table-from-xlsx` CLI with `"file_path"`. Private chat only, clearance 10, runs on the worker. |
| `/spbot_list_server_id` | List the database targets you can address in other commands: `server_id`, `db_type`, `ip:port`, instance name. Use it to find the value to pass as a target. |
| `/spbot_run_sql_task` | Run one SQL task now by `sql_id`, `--force`: it ignores the schedule, the interval and the active flag, but never a run already in flight. Each target replies separately. **Optional second argument and beyond: `NAME=VALUE` pairs** for the parameters that task declares in `sql_commands.json` — e.g. `/spbot_run_sql_task 12 spid=505 db=SALESDB`. The script reads them as `@spid` / `@db`; the values are **bound**, never pasted into the SQL, so a chat message cannot become a statement. A value containing spaces goes in quotes. A task that declares no parameters takes no extra arguments. Use `/spbot_list_sql_tasks` for the id. |
| `/spbot_list_sql_tasks` | List every **active** SQL task (`sql_commands.json` + `sql_targets.json`): sql_id, sql_code, and per-target server/database/time window (a target with `time_window.repeat_interval: -1` shows `manual (run with /spbot_run_sql_task)` instead of a window, and any target with an `output` block shows `output=<format>`). Inactive commands and targets are hidden (a footnote counts them), as is any command left with no active target. If the listing exceeds one Telegram message, the full configuration is attached as a JSON document instead. No parameters. |
| `/spbot_run_sql_task` | Run one SQL task now by `sql_id` (the number `/spbot_list_sql_tasks` shows). Runs every target configured for that task, in the background, and each target reports its own result as a separate message — the runner already queues those. Always `--force`, which the CLI requires for a targeted run: it ignores the time window, the repeat interval **and the active flag**, so an id that `/spbot_list_sql_tasks` hides as inactive will still run if you name it. Requires clearance 10, the same as `/spbot_restore` and `/spbot_add_sql`, because a task may be an UPDATE against production. |
| `/spbot_list_metrics` | List every **active** metric definition (`metric_definitions.json`): metric_code, collector type, db_type, and repeat interval (`every Ns` / `run-once`). Inactive definitions are hidden and counted in a footnote. If the listing exceeds one Telegram message, it is attached as a JSON document instead. No parameters. |
| `/spbot_metric_toggle` | Enable/disable metric collection for one `server_id` in `db_instances.json` (atomic write; the bot runs the `common.cli metric-toggle` command itself, so there is one engine and one caller path). Scope: `all` (the target's `metrics.enabled`), `collector:<sql\|cmd\|docker\|k8s>` (one collector class via `disabled_collector_types`), or one metric_code (`metric_overrides.<CODE>.enabled`; enabling also removes the code from the legacy `report_policy.disabled_metric_codes`). Clearance 10, private chat only — switching collection off produces no alert of its own. Afterwards, pull the change back to the master with `control worker-pull-data-config --all-json --overwrite`. |

## Usage

### `/spbot_report_metric_history`

Builds and queues a report from metric data already stored in DB Ops. It does not run metric collection. The time window starts at the current time minus `hours` and ends at the current time.

**Inline (all args at once):**
```
/spbot_report_metric_history ACME-192-0-2-115 INSTANCE_STATUS 24
```

Arguments:

- `server_id`: one server identifier, using letters, numbers, dot, underscore, colon, or hyphen
- `metric_code`: one metric code, for example `INSTANCE_STATUS`
- `hours`: positive integer lookback window in hours

The command is restricted to bot administrators (`command_type=10`) and runs on the worker node.

### `/spbot_restore`

Starts a restore workflow in the background. The bot will prompt for parameters if not provided inline.

**Inline (all args at once):**
```
/spbot_restore ACME_TO_SQLSERVER_192_168_18_31 2026-06-13 21:00:00 +07:00
```

**Interactive (bot prompts each parameter):**
```
/spbot_restore
> Please input restore_id
ACME_TO_SQLSERVER_192_168_18_31
> Please input point_in_time (example: 2026-05-30 18:00:00 +07:00) or send LATEST
LATEST
```

`point_in_time` accepts:
- `LATEST` — restore to the newest available backup
- `YYYY-MM-DD HH:MM:SS +HH:MM` — restore to a specific point in time (requires log backups)
### `/spbot_json_exp_ticket_detail`

Runs SQL task `SQLSERVER-014-EXPORT-TICKET-DETAIL-JSON` on server `ACME-192-0-2-245`, database `HR`, writes the `ResultJson` output to `runtime/exports/telegram/*.json`, and sends the generated JSON file back to Telegram.

### Target specs (server_id or `<db_type> <ip> [port]`)

Commands that address one database accept a **target** in either form (resolved against `data/db_instances.json`). `server_id` is the unique per-instance key and is preferred; an IP can be shared by several instances (e.g. HA lab containers on one host), which is why the IP form may need a port.

- a `server_id` — e.g. `ACME-192-0-2-248`
- `<db_type> <ip>[:port]` — e.g. `mssql 192.0.2.248 1433` **or** `mssql 192.0.2.248:1433` (the `ip:port` form matches the `/spbot_list_server_id` output). `db_type` accepts friendly words (`mssql`=`sqlserver`, `pg`=`postgresql`, ...). If the **port is omitted**, the first instance at that ip (top-1 by config order) is used.

Run `/spbot_list_server_id` to see every valid target. The multi-word `<db_type> <ip>[:port]` form is best used via the interactive prompt (one target per message); inline, use the single-token `server_id` form. Commands using this: `/spbot_sql_to_xlsx`, `/spbot_report_hourly_metrics`.

### `/spbot_list_server_id`

Replies with the list of database targets you can query — `server_id | db_type ip:port | instance` — one per line (OS-only hosts with no database are omitted). **Disabled targets are not listed**: the listing exists to be copied from into another command, and a disabled target is one the resolver refuses. A footnote counts them. No parameters.

### `/spbot_list_backup_id`

Replies with every backup ID that can be run:

```
Backup IDs (7):
- CLOUD_MSSQL_FULL  [encrypted]
    sqlserver CLOUD-203-0-113-188-MSSQL-1433
    level: full | schedule: 01-05h
- CLOUD_ORA_DB  [encrypted]
    oracle CLOUD-203-0-113-188-ORA-1521
    level: auto (Sun=full, else diff) | schedule: 01-05h
- CLOUD_PG_WAL
    postgresql CLOUD-203-0-113-188-PG-5433
    level: wal (no full/diff level) | schedule: every 15m

Run one:  /spbot_backup <backup_id> <full|diff|log|->
Use - to let the schedule's own rule pick the level.
(4 inactive backups hidden; set active:true to use them.)
```

`level:` is the part worth reading before running one — it says whether the level is fixed or
derived, which is what decides if the second argument of `/spbot_backup` means anything for that
id. A log/archive job says `(no full/diff level)` because it has no such choice. No parameters.

### `/spbot_backup`

Run one backup immediately, by `backup_id`:

```
/spbot_backup CLOUD_MSSQL_FULL
/spbot_backup CLOUD_ORA_DB full
/spbot_backup CLOUD_ORA_DB -
```

Always runs with `--force`, which means **ignore the schedule** — not "ignore a run already in
flight". A second press while the first is still running is refused rather than starting a
second backup against the same database.

The optional level is one word for every engine and is translated per engine (Oracle `0`/`1`,
PostgreSQL `full`/`incr`, SQL Server `full`/`diff`/`log`). Pass `-` to leave the decision to the
script, exactly as a scheduled run would.

### `/spbot_list_sql_tasks`

Replies with every configured SQL task and its schedule, one command per block:

```
#15 SQLSERVER-015-RUN-ENGINE-V2-YESTERDAY-TODAY
  -> ACME-192-0-2-111 db=APPDB_Testing day 1..31 hour 1..5 every 72000s timeout 7200s

(2 inactive entries hidden; set active:true to use them.)
```

Time windows are the node-local (+07) `from_*`/`to_*` bounds; `every Ns` is `repeat_interval` and `run-once` means `repeat_interval=0`. When the listing is longer than one Telegram message, the full `sql_commands.json` + `sql_targets.json` content is attached as a JSON document. No parameters.

### `/spbot_list_metrics`

Replies with every metric definition and its repeat interval:

```
INSTANCE_STATUS [sql/multi] every 60s
OS_DISK_USAGE [cmd/multi] every 600s
```

Inactive definitions are not listed — a trailing footnote counts them instead. When the listing is longer than one Telegram message, it is attached as a JSON document (metric_code, db_type, collector_type, active, repeat_interval, timeout). No parameters.

### `/spbot_metric_toggle`

Enable/disable metric collection for one server (admin, private chat only). Three parameters, prompted if not inline:

```
/spbot_metric_toggle ACME-192-0-2-115 off OS_DISK_USAGE
/spbot_metric_toggle ACME-192-0-2-249-PGLAB-5433 off collector:cmd
/spbot_metric_toggle ACME-192-0-2-245 on all
```

- `server_id`: the target to change (see `/spbot_list_server_id`)
- `state`: `on` or `off`
- `scope`: `all` = the whole target (`metrics.enabled`) · `collector:<sql|cmd|docker|k8s>` = one collector class (`metrics.disabled_collector_types`) · a metric_code = one metric (`metrics.metric_overrides.<CODE>.enabled`; see `/spbot_list_metrics`)

The change is written atomically to the worker's `data/db_instances.json` and takes effect on the next collection tick. Enabling a single metric also removes it from the legacy `report_policy.disabled_metric_codes` list. The reply reminds you to pull the config back to the master (`control worker-pull-data-config --all-json --overwrite`).

### `/spbot_create_db_docker`

One command, one parameter set, every engine — deploys either **in-container on the worker host** or **directly on a remote Ubuntu VM over SSH**. Private chat only (passwords are involved). Fourteen positional parameters:

| # | Parameter | Meaning |
|---|---|---|
| 1 | `name` | Instance name (letters, numbers, `_`, `-`). |
| 2 | `engine` | `postgres` / `mysql` / `mssql` / `oracle`. |
| 3 | `version` | Image tag. oracle 26ai = `23.26.2` or `latest`. |
| 4 | `mode` | `single` / `ha-lab`. |
| 5 | `host_port` | Host port, or `-` for the engine default. |
| 6 | `password_env` | **DB password — secret ref** (`-` = default `<NAME>_PASSWORD`). |
| 7 | `password_text` | **DB password — value** (stored in the secret store, passed via env). Send `-` to **reuse the ref in position 6 exactly as it is stored** — nothing is written, and an existing ref is neither overwritten nor treated as a collision. |
| 8 | `deploy_target` | `worker` = in-container on the worker host (192.0.2.249); or an **IP** = provision on that Ubuntu VM over SSH. |
| 9 | `remote_user` | SSH user on the VM (`-` when `deploy_target=worker`). |
| 10 | `remote_password_ref` | **SSH password — secret ref** already in the store (`-` to skip). |
| 11 | `remote_password_text` | **SSH password — value** (passed via env, never argv; `-` to skip). |
| 12 | `remote_key_name` | **SSH key** file name in `data/ssh_keys/` on the worker (key-auth VMs, e.g. Oracle Cloud), or `-`. |
| 13 | `recreate` | `yes` = `--force` (destroys existing containers + volumes) / `no`. |
| 14 | `install_docker` | `yes` = install docker + compose on the VM over SSH if missing (needs the SSH user to have sudo), add it to the docker group, create the containers dir. `no` for `worker` or a VM that already has Docker. |

SSH auth for a remote VM has three mutually exclusive forms — an SSH **password ref** (pos 10), an SSH **password value** (pos 11), or an SSH **key** (pos 12, a file placed in `data/ssh_keys/` on the worker). Give one and set the others to `-`. The DB password (pos 6/7) likewise takes a ref or a text value. Both password values are handed to the CLI through the environment, never on a command line.

Order: `name engine version mode host_port password_env password_text deploy_target remote_user remote_password_ref remote_password_text remote_key_name recreate install_docker`.

**Inline, one message — deploy on the worker host (the old path):**
```
/spbot_create_db_docker ora_dg_lab oracle 23.26.2 ha-lab 15210 - 'DbPass#123' worker - - - - yes no
```

**Inline — deploy on a remote Ubuntu VM over SSH, SSH password by ref:**
```
/spbot_create_db_docker ora_lab oracle 23.26.2 single 1521 - 'DbPass#123' 198.51.100.146 dba_user REMOTE_198_51_100_146_DBA_USER - - no no
```

**Inline — fresh Ubuntu VM, SSH password as text, install Docker first:**
```
/spbot_create_db_docker pg_lab postgres 18 single 5433 - 'DbPass#123' 198.51.100.146 dba_user - 'my-ssh-pass' - no yes
```

**Inline — key-auth VM (Oracle Cloud): SSH key in data/ssh_keys/ on the worker, install Docker:**
```
/spbot_create_db_docker pg_cloud postgres 18 single 5433 - 'DbPass#123' 203.0.113.188 ubuntu - - oracle-cloud.key no yes
```

With `install_docker=yes` the bot SSHes in and installs docker + the compose plugin if they are missing (via `get.docker.com`, falling back to the distro packages), adds the SSH user to the docker group, and creates the containers dir — so a bare Ubuntu VM works from one command. With `install_docker=no` the VM must already have docker + the compose plugin and a writable containers dir (`sudo mkdir -p /opt/db_ops/containers && sudo chown <user>: /opt/db_ops/containers`). The connection is registered on the master with the VM's IP as host. Long post-start steps (Oracle Data Guard) run detached on the VM, so the one command completes on its own. Leave any parameter blank in the step-by-step flow and the bot prompts for it; quote values that contain spaces when sending inline.

### `/spbot_sql_to_xlsx`

Runs one **read-only SELECT** on the SQL Server target and sends the first result set back as an `.xlsx` document. The statement is executed without committing; if it changes any rows (INSERT/UPDATE/DELETE/DDL, incl. `SELECT INTO`) it is **refused and rolled back**. The result is capped at 50,000 rows (you are told when it is truncated).

**Inline (single-token target only):**
```
/spbot_sql_to_xlsx ACME-192-0-2-248 SELECT TOP 100 * FROM Globex_Prod.dbo.SomeTable
```

**Interactive (bot prompts each parameter):**
```
/spbot_sql_to_xlsx
> Target? a server_id (e.g. ACME-192-0-2-248) or: <db_type> <ip> [port] ...
mssql 192.0.2.248
> Paste the SELECT statement now (single message), or attach a .sql file. Only read-only SELECT is allowed:
SELECT Id, Name FROM Globex_Prod.dbo.Employee
```

Arguments:

- `target`: a `server_id`, or `<db_type> <ip> [port]` (see "Target specs" above) — connection + credential are resolved from `data/db_instances.json`
- `sql_text`: the SELECT statement — paste it as one message or attach a `.sql` file

### `/spbot_list_all_command`

Replies with every command you can run, in this chat, right now — derived from
`data/telegram_support_commands.json` itself. No parameters.

```
/spbot_list_all_command
```
```
Bot commands you can run here: 22
/spbot_sql_export <target> <format> <sql_text...>  [clearance 10]
/spbot_sql_to_xlsx <target> <sql_text...>  [clearance 10]
/spbot_status
...
(3 hidden: above your clearance.)
```

Each line is generated: `<required>` and `[optional]` come from the command's own `parameters`
block, `...` marks the `consume_rest` argument that swallows the rest of the message, and the
clearance is its `command_type`. **Adding a command to the JSON is the only step** — this listing
picks it up, which is what the table above and the BotFather block cannot do (both are hand-kept,
and `tests/test_listing.py` exists because they drifted twice).

What it hides, and why each is counted separately rather than as one number:

- **above your clearance** — something to ask an admin about;
- **only runs in a group / in a private chat** — something to fix by moving;
- **turned off with `command_type < 0`** — a config change, not a permission.

Offering a command the permission check would refuse is the failure `db_ops/common/listing.py`
exists to prevent, and it matters most here: this is the command someone types *because* they do
not yet know what they may run.

### `/spbot_sql_export`

The same query, with the file format as an argument. `/spbot_sql_to_xlsx` is unchanged and still
produces exactly what its name says — this exists because adding a format argument to *that*
command would have re-read every existing invocation: its SQL argument consumes the rest of the
message, so the first word of someone's SELECT would have become the format.

**Inline:**
```
/spbot_sql_export ACME-192-0-2-248 csv SELECT TOP 100 * FROM Globex_Prod.dbo.SomeTable
```

**Interactive:**
```
/spbot_sql_export
> Target? ...
ACME-192-0-2-248
> Format? xlsx (spreadsheet), csv (spreadsheet or another tool), txt (aligned table), xml (structured).
csv
> Paste the SELECT statement now ...
SELECT Id, Name FROM Globex_Prod.dbo.Employee
```

Arguments:

- `target`: as above
- `format`: `xlsx` | `csv` | `txt` | `xml`. `raw` and `json` are refused — they render fine but are
  not documents anyone opens; `raw` exists to be piped in a shell.
- `sql_text`: the SELECT statement — **must come last**, it consumes the rest of the message

In `csv` a SQL NULL is an empty **unquoted** field and `""` is the empty string (PostgreSQL's
`COPY ... WITH CSV` convention); in `txt` and `xml` it is spelled out. The rendering is
`db_ops/common/result_format.py`, the same code a scheduled SQL task's `output.format` uses, so an
ad-hoc export and a scheduled one are the same artifact.

The command is restricted to bot administrators (`command_type=10`) and runs on the worker node.
