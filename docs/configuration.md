# Configuration

Everything this toolkit does is decided by JSON files you own. This page is the map: where those
files are looked for, what each one decides, and the rule the whole design rests on.

> **Naming, while the project is being renamed.** Environment variables are still `DB_OPS_*` and
> the module paths still `db_ops.*`. They become `DBABRAIN_*` and `dbabrain.*` when the code moves
> to the public repository; nothing else here changes with them.
> <!-- TODO(rename): update the env prefix and module paths on this page once the rename lands. -->

---

## 1. Configuration is data, not literals in Python

A new threshold, target, route, schedule, severity or policy belongs in a JSON file. Never in the
source.

This is not tidiness. The design assumes the person affected by a setting can read it, change it,
and be reviewed on the change — and a value compiled into a Python module takes all three away.
Three constants in this tree proved it the hard way: a report URL, a secret reference, and an
address-prefix filter that silently dropped one subnet from every inventory page. Each was correct
for one estate and invisible to everyone else, and each is now a configuration key.

The corollary is worth stating too: **if a file under `data/` is not read by any code, delete it.**
Configuration nobody acts on is worse than configuration you cannot edit, because it reads as a
setting and is not one.

---

## 2. Where the configuration is

The toolkit does **not** derive the location of its configuration from where its own code sits on
disk. That answer is right in exactly two layouts — a source checkout and the container, because in
both the package sits beside `data/` — and wrong for every installed copy, which resolved its data
directory to a `site-packages/data` that does not exist and never will.

So the answer has an order, and the package's own location is the **last** entry in it:

| | Question it answers | |
| --- | --- | --- |
| 1 | `DB_OPS_HOME` | What did the operator state? |
| 2 | The current working directory, **if** it holds `data/` or `config.json` | Where is the operator standing? |
| 3 | The package location | The fallback that keeps a checkout and the container working. |

Two details that are deliberate:

- **Step 2 needs a marker.** Any directory would be an invention: someone running the tool from
  their home directory has said nothing about configuration, so the search falls through instead
  of treating that directory as a tool root.
- **A `DB_OPS_HOME` that does not exist is an error, not a fallback.** Falling through would
  swallow a typo and then quietly read a *different* estate's configuration. That is the one
  failure here worth being loud about.

`DB_OPS_DATA_DIR` moves the data folder on its own. Once the tool is installed the two move
independently: the code goes wherever pip puts it, and the configuration stays where you keep
configuration.

Guarded by `tests/test_tool_root_resolution.py`, and by
`tests/test_no_self_derived_project_root.py`, which refuses the old `Path(__file__).parents[n]`
idiom anywhere but the one module allowed to ask where its own code lives.

### Per-app configuration

Most commands take `--config`. When they do not, each app resolves its own, in this order:

1. `--config` on the command line;
2. the app's environment variable — `DB_OPS_METRICS_CONFIG`, `DB_OPS_REPORTS_CONFIG`,
   `DB_OPS_SLA_CONFIG`, `DB_OPS_TELEGRAM_CONFIG`, `DB_OPS_SQL_TASKS_CONFIG`,
   `DB_OPS_BACKUP_RESTORE_CONFIG`, `DB_OPS_JOBS_CONFIG`, `DB_OPS_SRE_CONFIG`;
3. an app-specific file beside the shared config or in the working directory —
   `config.metrics.json`, `config.reports.json`, and so on;
4. `config.json`.

The resolved source is printed to stderr on startup (`[db_ops.config] app=metrics source=cli
config=config.json`), because the first question when a command reads the wrong settings is which
file it read.

---

## 3. `config.json` — runtime paths and pointers

The smallest file, and deliberately so. It says where the toolkit puts its own output and which
declaration files it reads. It holds no threshold, target or schedule.

| Key | Decides |
| --- | --- |
| `app_name` | The name this installation reports itself as. |
| `log_dir`, `runtime_dir` | Where logs and generated output go. Relative to this file. |
| `console_level`, `file_level` | The two logging thresholds. |
| `store_config_file` | Pointer to the runtime store declaration. Default `data/store_config.json`. |
| `telegram_config_file` | Pointer to the delivery settings. Default `data/telegram_config.json`. |
| `master`, `worker` | Read only by the control app, which builds an image on one machine and deploys it to another. A single-machine install can delete both. |

Start from [`config.example.json`](../config.example.json).

---

## 4. The `data/` files

Every file below has a `*.example.json` beside it in the repository, complete enough to copy,
rename and edit, with a `notes` array explaining what it decides and why. **Read the example, not
just the table.**

### The estate

| File | Decides |
| --- | --- |
| `db_instances.json` | **The monitored estate.** One record per `server_id`, with the address, engine, credential name, how OS-level commands reach the machine, and per-instance metric and report toggles. Every other file joins to this one by `server_id`. |
| `users.json` | **The credential registry.** Which account is used where — database logins, remote OS accounts, and neighbouring-tool accounts recorded so they are not lost. Holds no password: every entry names a reference. |
| `docker_db_connections.json` | The disposable lab databases the SRE app provisions, and where they run. Written by the tool as well as read by it. |

`server_id` is the only join key, and it is worth treating as one: one machine and instance, one
id, never reused for a different machine. Group and join on it, never on an address — an address
is a property of a machine, not its identity.

### What is measured

| File | Decides |
| --- | --- |
| `metric_definitions.json` | **The catalogue.** Every metric the collector knows: its code, which engines and versions it applies to, which SQL or script implements it, its schedule and its timeout. The same for every operator. |
| `metric_importance_overrides.json` | How much a metric matters **on your instances**. Kept separate from the catalogue because a metric worth waking someone for in production is noise on a sandbox. |
| `capacity_policy.json` | When a projected exhaustion becomes a finding — "inside the time it takes to provision space" is an organisational fact, not a property of the disk. |
| `backup_policy.json` | How old each kind of backup may be, per database. Evaluated one type at a time, so the one database that quietly stopped being backed up cannot hide behind the newest backup on the server. |
| `restore_drill_policy.json` | How old a successful restore drill may be before it stops counting as evidence. |
| `sla_policies.json` | The objectives the collected metrics are graded against, with their windows, targets and error budgets. |

### What runs

| File | Decides |
| --- | --- |
| `app_commands.json` | **The scheduler's list.** Which apps run, how often, in which hours, with what timeout, on which node role. |
| `sql_commands.json` | Which SQL scripts exist, and their shape (one file, an ordered list, or a folder). The SQL itself is a reviewable file under `assets/`, never a string in configuration. |
| `sql_targets.json` | Where and when each of those scripts runs, how its output comes back, and who hears about a failure. |
| `reports_config.json` | Which reports are built, how often, and how stale a measurement may be before it stops being reported. |
| `restore_config.json` | The backup jobs, and the restores that prove them. |
| `maintenance_policy.json` | Timing budgets and refusal gates for host maintenance — restart-and-wait, service control, patching. |
| `sqlserver_instance_policy.json` | What is portable between two SQL Server instances when one is rebuilt from the other. |
| `emergency_operations.json` | How hard each dangerous operation is to confirm. Read by the shared operations layer, which knows nothing about chats. |

### Delivery and access

| File | Decides |
| --- | --- |
| `telegram_config.json` | The transport: enabled or not, where the token comes from, and the level → chat routing table. |
| `bot_telegram.json` | Which bot. Kept apart from the transport so swapping bots is one file. |
| `telegram_groups.json` | The chats, and the routing level each one receives. |
| `telegram_users.json` | Who may talk to the bot, and at what clearance. |
| `telegram_support_commands.json` | What the bot will do when asked, and the clearance each command demands. |
| `webhost_config.json` | The web console: session rules, permission levels, and the component blocks the dashboard draws. |

### The toolkit's own plumbing

| File | Decides |
| --- | --- |
| `store_config.json` | **Where the toolkit keeps its own data.** SQLite or PostgreSQL, and the connection for it. |
| `encrypted_secret_text.json` | The encrypted secret store. Generated, never hand-edited — see [`docs/security.md`](./security.md). |
| `config_catalog.json` | Which of the files above are mirrored into the store for the web console to read and edit, and how a record inside each is identified. A file missing from here is invisible to the console. |
| `sre_config.json` | How lab environments are built: hypervisor, templates, network, and per-engine install defaults. |

---

## 5. Two objects that appear everywhere

They are parsed once, in the shared layer, and reused. A per-app copy of either is a bug.

### `time_window` — when something may run

```json
"time_window": {
  "from_year": null, "to_year": null,
  "from_month": null, "to_month": null,
  "from_day": 1, "to_day": 31,
  "from_hour": 1, "to_hour": 5,
  "from_minute": null, "to_minute": null,
  "repeat_interval": 72000,
  "retry_interval": 3600,
  "timeout": 7200
}
```

- Null bounds mean no restriction.
- `repeat_interval` is seconds, counted from the **previous run's start**. A command that takes 9
  seconds with an interval of 10 becomes due 1 second after it exits.
- `repeat_interval: 0` means run once and leave it running — that is how a server that stays up is
  expressed. `repeat_interval: -1` means manual only: never scheduled, run on request. Use it for
  anything that writes.
- `timeout` must stay **above the slowest thing inside the command**, not above its average. A
  collection pass killed at its timeout loses every metric in it, not only the slow one.
- Something still running when its interval comes round is skipped, not started twice.

### `notify` — who hears about it

```json
"notify": {
  "logging_on_run":  {"enabled": true, "telegram_chat": "backup", "chat_id": ""},
  "alert_on_error":  {"enabled": true, "telegram_chat": "critical", "chat_id": ""}
}
```

`logging_on_run` announces that it ran; `alert_on_error` announces that it failed. Each names a
**routing level**, not a chat: the level is looked up in the level → chat map built from
`telegram_groups.json` and `telegram_config.json`. A level with no chat does not send, which is
how a stream is muted — there is no second allow-list to keep in sync. Setting `chat_id` directly
overrides the lookup, for the rare case that needs one specific chat.

---

## 6. Environment variables

The environment carries only two kinds of thing: **which node this is, and secrets.** Everything
else is JSON. Start from [`.env.example`](../.env.example).

| Variable | For |
| --- | --- |
| `DB_OPS_SECRET_KEY` | The passphrase for the encrypted secret store. Every command that connects anywhere needs it. |
| `DB_OPS_HOME` | The tool root, when the tool is installed and the configuration is elsewhere. |
| `DB_OPS_DATA_DIR` | The data folder alone. |
| `DB_OPS_NODE_ROLE` | `master` or `worker`. Declared per node so a configuration file copied between machines can never mislabel one. Unset means `master`. |
| `DB_OPS_STORE_CONFIG` | Overrides the store declaration path, for a side-install or a test. |
| `DB_OPS_<APP>_CONFIG` | Per-app configuration path — see §2. |
| `TELEGRAM_BOT_TOKEN` | The bot token, if you supply it by environment instead of the secret store. The variable *name* comes from `bot_token_env`. |
| `DB_OPS_LOG_SCOPE` | Overrides the log scope name, when one app runs under several names. |

**A credential reference doubles as an environment variable name.** At use time a `password_ref`
is resolved from the environment first and from the encrypted store only if the environment does
not carry it — so an operator who keeps secrets in an external manager and injects them as
environment variables never has to use the built-in store at all.

---

## 7. Assets

`assets/` holds the SQL and scripts that *implement* what the configuration schedules: metric
queries, backup and restore scripts, host checks, and your own SQL tasks.

Two owners share that vocabulary, and the lookup reflects it. **Your copy wins per file, the
package's built-in is the fallback** — so a query that needs one adjustment for your environment is
fixed by putting your version at the same path under your tool root, and every other query still
comes from the package. A path in configuration
(`"assets/metrics/postgresql/001_postgresql_instance_status.sql"`) names *what it wants*, not where
the installer put it: the shipped files live with the component that runs them
(`db_ops/metrics/collectors/`, `db_ops/common/backup_scripts/`, and so on), and
`BUILTIN_ASSET_ROOTS` in `db_ops/lib/paths.py` is the one place the two spellings meet.

---

## 8. Changing configuration safely

The scheduled apps run against real databases. Before a change to anything production-facing:

1. Read the file you are about to change, and the `notes` in its example.
2. Run the affected command manually. Most support `--dry-run`, which names every target and
   metric without opening a connection — that is how a target resolving to the wrong instance is
   found before it resolves to the wrong instance at 2am.
3. Look at `logs/` and at the run history in the store.
4. Run the test suite if you changed anything the code reads.

A configuration change is live the moment the next scheduled pass reads it. If you deploy to
another node, that is a separate, explicit step — see [`docs/11_control_app.md`](./11_control_app.md).
