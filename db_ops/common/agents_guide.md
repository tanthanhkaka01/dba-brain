# Running this toolkit for the first time

**For AI agents (Codex, Claude, ...) and for people.** This file is written into the tool root by
`dbabrain init` and **replaced by every later `init`**, so it always matches the installed version.
A copy somebody edited is saved to `runtime/agents_guide/` first - keep your own notes in another
file. Before a root exists, `dbabrain guide` prints it. Read it before running anything: this
toolkit reaches real databases and sends to real chats.

## 0. The ten rules

1. **Run every command from the tool root** - the directory holding `config.json`, `data/`,
   `runtime/` and this file. `db-ops` finds its configuration by where you stand.
2. **Configuration is data.** Everything the toolkit monitors, runs and sends is in `data/*.json`.
   There is no hidden config in code. Change the JSON, never the installed package.
3. **Prefer a registrar to a hand edit.** `instance-add`, `sql-command-add`, `backup-add`, ... check
   what they write against the reference and refuse by name. A hand edit is checked by nothing
   until it fails on a schedule - so after any hand edit, run `check-objects` and `check-references`.
4. **Never put a secret in a file, an argument or a log.** Passwords and tokens go into the
   encrypted store: `secret-set` on stdin, or `password` inside `instance-add`. Config files hold a
   *reference* (`password_ref`, `credential_name`), never a value.
5. **Every `db-ops common` command takes ONE JSON object** - inline, `@file.json`, or `-` for stdin.
   On Windows, **single-quoted JSON breaks**: write the request to a file and pass `@file.json`.
6. **Ask before anything that changes a database or a host.** Listing, `describe-*`, `check-*`,
   `--dry-run`, `due-check`, `self-status`, `ops-status` are safe. Restores, `run-sql`, `run-cmd`,
   the EMERGENCY commands, `host-restart` and password rotation are not - see section 9.
7. **Exactly one scheduler per estate.** Two daemons on one estate double every collection, backup
   and alert, and the second is invisible in the first's store.
8. **"Not configured" is a state, not a failure.** No Telegram token, no groups, no inventory: the
   app says what is missing and skips. Configure the missing thing; do not "fix" the message.
9. **Set the timezone before anything is scheduled** (section 3). Hours in every schedule mean
   hours on that clock.
10. **Verify by asking the tool, not by reading logs.** Section 8 lists the questions it answers.

## 1. Install and create the root

```bash
python -m venv .venv
.venv/bin/pip install "dbabrain[postgres,mssql,ssh,winrm]"   # Windows: .venv\Scripts\pip
dbabrain init          # writes config.json, data/, runtime/, secrets/ and this AGENTS.md
dbabrain guide         # prints this file; writes nothing
```

Extras: `mssql` (SQL Server: also needs Microsoft ODBC Driver 18), `postgres`, `ssh`, `winrm` (the
OS side of Windows hosts). `init` never overwrites a configuration file you edited; re-run it after
an upgrade to add files a new version introduced and to replace this guide with the new version's.
Then move the existing files to the new version's field names - plan first, apply only after
reading the plan, and **before starting the daemon** on the upgraded node:

```bash
db-ops common upgrade-config '{}'                     # plan; writes nothing
db-ops common upgrade-config '{"dry_run": false}'     # apply; copies each file to runtime/ first
```

**Windows console:** set `PYTHONIOENCODING=utf-8` - the console is cp1252 and some output carries
emoji.

## 2. The secret key

Every secret lives in `data/encrypted_secret_text.json`, encrypted with a passphrase **you choose
and keep**. Nothing can recover it.

```bash
export DB_OPS_SECRET_KEY='<passphrase>'     # PowerShell: $env:DB_OPS_SECRET_KEY = '<passphrase>'
```

Commands also take `--key` or `--key-base64` (base64 is safer in shells where `$`, `#` or `^` mean
something). Without the key, anything that needs a password fails, and the daemon exits.

## 3. The clock

```bash
db-ops db --config config.json timezone --format txt      # what this node resolved; no store
db-ops db --config config.json timezone --set Asia/Ho_Chi_Minh
```

`init` writes `UTC`. Stored timestamps are always UTC; the timezone decides what is **shown** and
what a `time_window`'s `from_hour` / `to_hour` mean. Change it **before** the first scheduled run:
changing it later moves every schedule that names an hour.

## 4. Add a database to monitor

One call writes the inventory record, the login and the encrypted password:

```json
{"server_id": "ACME-SQL01", "db_type": "sqlserver", "ip": "192.0.2.50", "port": 1433,
 "major_version": 16, "service_name": "ACME-SQL01", "environment": "prod",
 "username": "monitor_user", "password": "<the password>"}
```

```bash
db-ops common instance-add @instance.json     # then delete instance.json: it holds a password
db-ops common instance-add --help             # every field, per engine
```

Mistakes that do not name themselves:

- **`server_id` is the only key a machine has.** Every other file joins on it - never on an ip.
- **`service_name` is a label on SQL Server, not a database.** Collection connects to `master`.
  Putting a database name there fails every SQL Server target with `Cannot open database (4060)`.
- **PostgreSQL and MySQL need `database_name`** - the database to connect to (`postgres` for the
  instance itself). Oracle connects by service.
- **`major_version` selects which SQL runs**: 13=2016, 14=2017, 15=2019, 16=2022 on SQL Server.
- **`db_type: "host"`** is a machine with no database (OS metrics only). Give it `platform`
  (`windows`/`linux`) and a `cmd_access` block, then its OS login with `remote-credential-add`.
- **`cmd_access.method: "local"` runs on THIS machine** - on a remote host it reports this
  machine's CPU under that host's name. Use `ssh` or `winrm`, and set `auth_type` explicitly
  (it defaults to `key`).

Least privilege for monitoring SQL Server:

```sql
CREATE LOGIN monitor_user WITH PASSWORD = '...';
GRANT VIEW SERVER STATE TO monitor_user;
GRANT VIEW ANY DEFINITION TO monitor_user;
USE msdb; CREATE USER monitor_user FOR LOGIN monitor_user;
ALTER ROLE db_datareader ADD MEMBER monitor_user;     -- backup age reads msdb.dbo.backupset
```

The older path still works: add the record to `data/db_instances.json`, the password to
`secrets/secret_text.json`, then `db-ops encrypt-secret`. That command **replaces** the store with
that file, so never run it once `secret-set` or `instance-add` has stored anything the file lacks.

Then prove it before trusting it:

```bash
db-ops check-credentials                      # every target resolves to a real login
db-ops metrics collect --dry-run              # resolves targets and queries; connects to nothing
db-ops metrics collect                        # collects once
db-ops metrics summary-latest                 # reads the result back
```

## 5. Know the files before editing them

| File in `data/` | What it holds | Registrar |
| --- | --- | --- |
| `db_instances.json` | the inventory: one record per database or host, and per-server metric switches | `instance-add`, `metric-toggle`, `metric-severity` |
| `users.json` | logins per server (references only, no passwords) | `instance-add`, `remote-credential-add` |
| `encrypted_secret_text.json` | every secret, encrypted | `secret-set`, `instance-add` |
| `app_commands.json` | which apps the daemon runs, and when | `app-command-set` |
| `metric_definitions.json` | the metrics, their SQL/commands and schedules (shipped; edit with care) | - |
| `sql_commands.json` / `sql_targets.json` | scheduled SQL: WHAT runs / WHERE and WHEN | `sql-command-add` / `sql-target-add` |
| `restore_config.json` | backups (`backups[]`) and restore drills (`restores[]`) | `backup-add`, `restore-add` |
| `reports_config.json` | the scheduled reports, the inventory included (`rp_inventory_health`) | - (edit; `check-objects` after) |
| `telegram_config.json`, `telegram_groups.json` | alert routing | `telegram use-bot`, `group-level` |
| `store_config.json` | where results go: SQLite (default) or PostgreSQL | `db use-store` |
| `shared_config_objects.json` | **reference**: every config record, every `common` request and answer, and its own shape | read-only |

Ask the reference instead of guessing a field:

```bash
db-ops common describe-object '{}'                                   # every object it describes
db-ops common describe-object '{"object": "time_window"}'            # all fields of one
db-ops common describe-object '{"object": "sql_target", "field": "output"}'
db-ops common describe-object '{"object": "input_run_sql"}'           # a command's request
db-ops common describe-object '{"object": "output_run_sql"}'          # and its answer
```

After ANY edit:

```bash
db-ops common check-objects '{}'       # every record against the reference: missing / value / unknown
db-ops common check-references '{}'    # every pointer (server_id, credential_name, ...) lands somewhere
```

Both exit 1 on a violation. `deprecated` and `unlisted` are notices, not failures.

**One name per concept** (since 0.22.0). Write these; the old spellings are still read, reported as
`deprecated`, and moved by `upgrade-config`:

| Concept | Write | Not |
| --- | --- | --- |
| a record is on or off (instance, backup, restore, SQL command and target, Telegram chat and person) | `active` | `enabled`, `status: "active"` |
| a feature switch inside a block (`metrics`, `telegram_config`, a `notify` rule) | `enabled` | - it stays |
| position in a list | `sort_order` | `ord`, `app_ord`, `menu_order` |
| the database to connect to | `database_name` | `database`, `db_name` |
| a list of database names | `database_names` | - |
| a restore's source -> target pairs | `database_mappings` | `databases` |
| a restore's machines, on the entry | `server_id`, `target_server_id` | `source.id`, `target.id` |
| a password, as a key into the encrypted store | `password_ref` | `password_env` (which now means only an environment variable's name: SSH auth, `--remote-password-env`) |
| a title shown to people | `display_name` | `sql_name`, an SLA's `name` |
| free text on a login | `note` | `notes` |
| environment | `environment` | `env` |
| SQL Server major version | `major_version` | `sqlserver_major_version` |

Requests to `db-ops common` follow the same names (`database_name`, `sql_text`, `destination`,
`file_path`); every answer uses them - `describe-object '{"object": "output_<command>"}'` shows one.

## 6. Schedules - `time_window`

Every scheduled record carries one:

```json
"time_window": {"from_hour": 1, "to_hour": 5, "repeat_interval": 72000,
                "retry_interval": 3600, "timeout": 7200, "weekdays": [7]}
```

- `repeat_interval` (seconds) is measured from the previous run's **start**. `-1` = manual only
  (listed, never scheduled). `0` = run once / run as a service.
- `weekdays` is ISO: `1` Monday ... `7` Sunday. Absent = any day; `[]` = **never** on a schedule.
- **`weekdays` decides which day, `repeat_interval` still decides whether due.** A weekly job is
  `weekdays: [7]` with `repeat_interval: 72000` - not 604800, which drifts and skips whole weeks.
- An app command's interval is a **floor** under everything inside that app: a SQL task declaring
  60 s inside an app running every 300 s runs every ~300 s.

Explain a verdict instead of waiting for it (`local_now` is required, or only the interval is
checked):

```json
{"time_window": {"from_hour": 1, "to_hour": 5, "repeat_interval": 72000, "weekdays": [7]},
 "local_now": "2026-09-27T01:30:00+07:00"}
```

```bash
db-ops common due-check @due.json
```

## 7. Run it on a schedule

```bash
export DB_OPS_NODE_ROLE=worker          # see below
export DB_OPS_SECRET_KEY='<passphrase>'
db-ops daemon --config config.json --once     # one pass of every due command, then exit
db-ops daemon --config config.json            # stay up
```

**`DB_OPS_NODE_ROLE`** (`master` when unset) decides which app commands run here: each has a
`node_role` - `master`, `worker` or `all`. `init` writes every command as `all`, so a single node
runs everything whatever its role. Once commands are split between nodes, a node whose role matches
none of them runs nothing and still looks like a healthy idle process - `self-status` lists what
this node schedules.

Nothing restarts the daemon if it stops. Run it under a service manager (systemd, Windows Task
Scheduler, or `restart: unless-stopped` in a container). When stopping it, stop its children too
(the web host and the app runs it started) - they outlive the parent.

Change an app's schedule with a command, not an editor:

```bash
db-ops common app-command-set '{"app_code": "APP-METRICS", "time_window": {"repeat_interval": 120}}'
```

## 8. Ask it what is happening

| Question | Command |
| --- | --- |
| What is this installation, what does it schedule | `db-ops common self-status '{"format":"txt"}'` (the bot's `/spbot_self_status` adds each app's last run) |
| Did every app run on time; which is failing or overdue | `db-ops db --config config.json ops-status '{}'` |
| Is the store there, and how big | `db-ops db --config config.json check --counts` |
| Which targets exist | `db-ops common list-targets '{}'` |
| Do the credentials authenticate | `db-ops common check-secret '{}'` |
| Latest metric results | `db-ops metrics summary-latest` |
| Which backups/restores are configured, on which days | `db-ops backup-restore list-backups` / `list-restores` |
| Which SQL tasks exist and when they run | `db-ops sql-tasks list-tasks --all` |
| What an SLA says | `db-ops sla validate` |

Logs are in `logs/`; `logs/errors.log` holds only errors. Read them after the questions above,
not instead of them.

## 9. What changes things - ask a person first

| Command | What it does |
| --- | --- |
| `db-ops backup-restore restore-*`, `common restore-database` | **overwrites the target database**. Never against production without explicit instruction |
| `db-ops common run-sql` / `run-cmd` | runs your SQL / shell command on a real target |
| `shrink-log`, `kill-spid`, `start-job`, `disable-job` | EMERGENCY operations; they ask for confirmation, and `authorize` exists for a caller that performs the step itself |
| `host-restart`, `host-service` | restarts a machine or its services |
| `rotate-password` | changes passwords on the server **and** in the store |
| `delete-file`, `delete-files`, `prune-backup-files` | deletes backups |
| `sqlserver-apply-cu` | patches SQL Server |
| `db-ops encrypt-secret` | **replaces** the secret store with `secrets/secret_text.json` |
| `db-ops import-data '{"bundle": "...", "force": true}'` | replaces this root's configuration with a bundle |

A **restore drill** needs room: the copy plus the restored files, often twice the backup size, on
a host that may also carry the store. Size it before switching one on.

## 10. Scheduled SQL

```bash
db-ops common sql-command-add @command.json   # WHAT: script(s) + engine; --help for the shape
db-ops common sql-target-add  @target.json    # WHERE/WHEN: one server, database, schedule, output
db-ops sql-tasks list-tasks --sql-id <id> --all
```

A task runs on `sqlserver` or `oracle` only. `sql_text` in the request writes the script for you.
One task on three servers is one command and three targets - `{target_server_id}` and
`{target_database}` in `input.args` are filled per target. Unknown request keys are refused by name.

- **No `database_name` on a SQL Server target means `master`.** The script's own `USE` moves from there.
- **`instance_name` must be an instance the server's record in `db_instances.json` has** (compared
  ignoring case). `sql-target-add` refuses one it does not have and names the ones it does.
- **The database is the server's to confirm**, not a list's: the task connects to it, and if it does
  not open the answer says why - it does not exist (with the name you probably meant), the login
  cannot open it, the login was refused, or the instance was not reached.
- **`autocommit: true` on the command for a script that manages its own transactions** - an engine
  that commits as it goes. Without it each file runs in one transaction, committed at the end, and
  the script's own `COMMIT`s are nested inside it.
- **A SQL Server warning is not a failure** (SQLSTATE `01xxx`, e.g. 8153 *Null value is eliminated by
  an aggregate*): the run is `done` at level `warning` and Telegram says *SQL task done with a
  warning*. Nothing after the warning could be read - not even an error - so a file run in one
  transaction is rolled back there instead, and the message says why.

## 11. Backups and restore drills

```bash
db-ops backup-restore backup-add @backup.json
db-ops backup-restore list-backups
db-ops backup-restore backup --backup-id <id> --job <job> --force    # one run now
```

- One entry, several **jobs** (`full`, `diff`, `log`, or `database_full` + `database` + `wal`), each
  with its own `time_window` and `cleanup_retention` in **seconds**.
- A PostgreSQL full and incremental stay in **one** entry: the incremental chains onto the newest
  backup in that entry's `backup_dir`.
- The engine may run in a container (`DOCKER_CONTAINER`, taken from the instance's
  `container_name`) or directly on the host.
- Oracle entries take ownership of the database's RMAN retention policy unless `RMAN_CONFIGURE`
  is `skip`.
- A restore names its machines as `server_id` (read from) and `target_server_id` (restored onto),
  both server_ids in `db_instances.json` - `check-references` fails one that names neither. A target
  nobody monitors is registered anyway, switched off: `instance-add` with `"active": false`.
- `database_mappings` lists `{source_database, target_database}` pairs; absent means every database
  in the backup set, under its own name.
- **Every database a restore brings back gets `DBCC CHECKDB` afterwards** unless the entry says
  `"checkdb": false`. A failed check fails the run, reported as *restored and recovered, but the
  integrity check failed*, with SQL Server's message numbers. On a SQL Server container, Msg 1823 /
  7928 means the check could not create its snapshot on that volume.

## 12. Alerts and commands on Telegram (optional)

```bash
echo '{"ref": "TELEGRAM_BOT_TOKEN", "value": "<token from @BotFather>"}' | db-ops common secret-set -
db-ops telegram use-bot --ref TELEGRAM_BOT_TOKEN
db-ops telegram bot-info
```

Send the bot one message yourself and one in each group, then name their levels:

```bash
db-ops telegram user-level --user "@you" --level 100      # quote @: PowerShell reads @x as splatting
db-ops telegram group-level --group "<group title>" --level warning
db-ops telegram route critical                            # which chat a level reaches
```

**One bot token, one poller.** Two nodes polling one token get HTTP 409 and send twice. A test
node gets its own bot. Message bodies cap at 4096 characters.

## 13. Web console and reports

```bash
db-ops webhost --config config.json user-add --username admin --level 100 --password-stdin
db-ops reports --config config.json use-base-url --this-node
```

The daemon serves the console and the reports (`APP-WEBHOST`); `self-status` prints the URLs.
The console reads reports and edits configuration; adding a database is `instance-add`.

## 14. Results store

SQLite in `runtime/` by default. PostgreSQL: fill the `postgresql` block in
`data/store_config.json`, then `db-ops db use-store postgresql` and `db-ops db init`. Several nodes
may share one PostgreSQL database **only on different schemas**, and still only one scheduler per
estate.

## 15. Moving or copying an estate

```bash
db-ops export-data '{"bundle": "estate-bundle.json", "format": "txt"}'   # old machine: config + encrypted secrets
db-ops import-data '{"bundle": "estate-bundle.json", "plan_only": true, "format": "txt"}'   # new root: what would change
db-ops import-data '{"bundle": "estate-bundle.json", "force": true, "format": "txt"}'
```

Each takes one JSON object (0.24.0): an option is a key, and the old `--plan` / `--force` / `--root`
flags are refused by name.

The bundle carries `store_config.json`: point the new node at its own store before starting it,
and stop the old scheduler first.

## 16. When something is wrong

| Symptom | First thing to check |
| --- | --- |
| The daemon runs and nothing happens | `self-status` (what does this node schedule?), then `DB_OPS_NODE_ROLE` against each command's `node_role` |
| Every SQL Server target fails with 4060 | `service_name` holds a database name (section 4) |
| A schedule runs at the wrong hour | the timezone (section 3) |
| A weekly job skips weeks | `repeat_interval` too long beside `weekdays` (section 6) |
| A field seems ignored | `check-objects` - a misspelled field is `unknown`, and the parser drops it silently |
| `check-objects` reports `deprecated` | an old field name (section 5): `upgrade-config` moves it |
| A SQL task fails naming the instances a server has | the target's `instance_name` is not one of them (section 10) |
| A SQL task fails *database … does not exist … did you mean* | the target's `database_name` - use the server's spelling (section 10) |
| A SQL task is `done` with a warning | SQL Server sent a warning; nothing after it was read (section 10) |
| A restore says *restored and recovered, but the integrity check failed* | the data is there; `DBCC CHECKDB` failed (section 11) |
| Password errors after a restore drill | the target now answers to the SOURCE's logins |
| Telegram 409 | a second poller on the same token |
| The daemon exited at start | `DB_OPS_SECRET_KEY` not set |

## Where the full documentation is

<https://github.com/tanthanhkaka01/dba-brain> - `docs/first_run.md` step by step,
`docs/configuration.md` for every file and field, `docs/architecture.md` for how the parts fit,
and one `docs/NN_*.md` per component. Every command also answers `--help`.
