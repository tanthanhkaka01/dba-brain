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
| `timezone` | **Required.** The clock this node *shows* — see §3.1. Default `UTC`. |
| `log_dir`, `runtime_dir` | Where logs and generated output go. Relative to this file. |
| `console_level`, `file_level` | The two logging thresholds. |
| `store_config_file` | Pointer to the runtime store declaration. Default `data/store_config.json`. |
| `telegram_config_file` | Pointer to the delivery settings. Default `data/telegram_config.json`. |
| `master`, `worker` | Read only by the control app, which builds an image on one machine and deploys it to another. A single-machine install can delete both. |

Start from [`config.example.json`](../config.example.json).

### 3.1 `timezone` — the clock this node shows

An IANA name (`Asia/Ho_Chi_Minh`, `America/New_York`, `Europe/Berlin` — these follow daylight
saving) or a fixed offset (`UTC`, `+07:00`, `-03:30`). It decides two things:

* **What every rendered time says.** A report header, a Telegram alert, a CLI listing, a log line,
  and the `YYYYMMDD_HHMMSS` prefix on a generated file. Rendered times carry their offset, always:

  ```
  Snapshot 2026-09-07 07:32:56 +07
  Snapshot 2026-09-07 00:32:56 +00
  ```

* **What a `time_window`'s `from_hour`/`to_hour` mean.** `from_hour: 1, to_hour: 5` is 01:00–05:00
  in *this* zone, on every node, whatever clock the host keeps.

**It does not change what is stored.** Every timestamp column holds UTC as
`%Y-%m-%dT%H:%M:%SZ`, on both backends, and every range query in the tool compares that text
lexically. That is what makes one configurable display clock safe.

**Absent means UTC**, with a warning naming the file — an install that predates the field still
starts. A value that is present and unparseable raises when the config is read, rather than
producing an estate of reports on a clock nobody questions for a month.

**`DB_OPS_TIMEZONE` overrides it**, the same way `DB_OPS_NODE_ROLE` overrides the node role: the
worker runs a copy of the master's `config.json`, so a per-node answer must not require editing it.
`DB_OPS_MESSAGE_UTC_OFFSET_HOURS` — the offset-only env var this replaces — is still read when
neither is set, and is deprecated.

Ask any node what it resolved, and put the answer on the record:

```bash
python -m db_ops.common.cli timezone '{"format":"txt"}'          # reads nothing; answers anyway
python -m db_ops.db.cli --config config.json timezone --record --list
```

The second upserts this node's row in the store's `runtime_nodes` table and reads every node back.
That is the only way to see whether a master and a worker sharing one store agree about the hour —
each reads its own `config.json`, and nothing else in the store can say. It lives in `db.cli` and
not beside the first because `common` may not import `db`.

An IANA name needs a zone database. `tzdata` is a core dependency for exactly this reason: Linux
has `/usr/share/zoneinfo` and Windows has nothing, so without it the same config file would work
in the image and fail on a Windows master. A fixed offset needs no zone database at all.

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
| `backup_policy.json` | How old each kind of backup may be, per database. Evaluated one type at a time, so the one database that quietly stopped being backed up cannot hide behind the newest backup on the server. Shipped with working defaults and written by `init`: with no policy the reports report a blind spot rather than a pass, which is correct but useless, so a node is never meant to be without one. |
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
| `shared_config_objects.json` | **Reference, not configuration.** Every field of the shared objects in §5 and of the records an operator edits (`sql_command`, `sql_target`, `db_instance`, `telegram_support_command`, `metric_definition` and the rest - from 0.22.0 every config file in `data/`, plus every request and answer of `common.cli`, and from 0.23.0 the reference's own shape: 153 entries) — required or not, range, default, and what its number is measured against. Identical on every node; read by `common.cli describe-object` and held to the estate by `check-objects`; editing it changes nothing an app does. |

---

## 5. The shared config objects

**Seven of them**, and they are the blocks that appear inside *many* `data/*.json` records rather
than belonging to one file. They are parsed once, in `db_ops/lib`, and passed around as values; a
per-app copy of any of them is a bug.

| # | Object | Kind | Fields | Required | Parsed in | Appears in |
| :-: | --- | --- | :-: | :-: | --- | --- |
| 1 | `time_window` | object | 13 | 0 | `lib/time_window.py` | `app_commands`, `sql_targets`, `metric_definitions`, `reports_config`, `restore_config` (backups + restores) |
| 2 | `notify` | object | 2 | 0 | `lib/notify.py` | `sql_targets`, `restore_config` |
| 3 | `notify_rule` | object | 3 | 0 | `lib/notify.py` | inside `notify`, twice |
| 4 | `output` | object | 4 | 0 | `lib/task_output.py` | `sql_targets` |
| 5 | `cmd_access` | object | 13 | 1 (`method`) | `lib/cmd_access.py` | `db_instances` |
| 6 | `sql_access` | object | 4 | 0 | `lib/sql_access.py` | `db_instances` |
| 7 | `cleanup_retention` | **field** | 1 (+2 legacy spellings) | 1 | `lib/cleanup_retention.py` | `restore_config` (backups + restores) |

**The field-by-field reference is a file, not this page**:
[`data/shared_config_objects.json`](../data/shared_config_objects.json) — for every field, what it
does, whether it is required, what values it takes, its default, and **what its number is measured
against**. Ask it rather than reading it:

```bash
python -m db_ops.common.cli describe-object '{}'                     # every entry, with field counts
python -m db_ops.common.cli describe-object '{"object": "time_window"}'
python -m db_ops.common.cli describe-object '{"object": "time_window", "field": "retry_interval"}'
```

Each field carries a `range_text` for a person **and** a `constraint` a program can evaluate, so the
estate can be held to it:

```bash
python -m db_ops.common.cli check-objects '{}'                  # this node's data/
python -m db_ops.common.cli check-objects '{"format": "txt"}'   # one line per finding
```

It reports three things, and exits 1 if it finds any: a **required** field missing, a **value**
outside what the reference accepts, and a field the reference does not describe — *nothing reads it*,
which matters because a typo in a schedule is otherwise silent: the parser ignores what it does not
recognise and the record runs on the default while looking configured. Two more are reported and
never failed: a `deprecated` spelling the parser still reads, and an `unlisted` value outside an open
enum, such as a Telegram level this estate defined itself.

`tests/test_shared_config_objects_reference.py` checks the file against the code *and* runs that
check over this estate, which is the only reason a reference like this is worth having. Its first run
found `sql_access.mode` and `sql_access.timeout_seconds` in use by 7 instances and described nowhere,
and two declared paths pointing at `backups[]` where the schedule actually lives on
`backups[].jobs[]`.

### 5.1 `time_window` — when something may run

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

Ten **bounds**, one **day-of-week set** and three **intervals**, answering three different questions.

- `weekdays` is *may it run today?* — an array of **ISO weekdays, 1 = Monday to 7 = Sunday**, the
  same numbering `isoweekday()`, `date +%u` and `DB_OPS_WEEKDAY` use. Absent means any day. `[]`
  means **no day is permitted, so the record never runs on a schedule** — which is not the same as
  `repeat_interval: -1` (still runnable on request) or `active: false` (not listed at all); what
  `weekdays: []` parks is the window, leaving the interval and the hour bounds as written. `0` is
  **refused**, not ignored: it is the cron spelling of Sunday, and dropping it would leave `[]`.
  A day listed twice is refused too — it is a set, not a weighting.
  - **It gates due-ness, it does not grant it.** So "the weekly full, Sunday, in the small hours"
    is `weekdays: [7]` **plus** `from_hour: 1, to_hour: 5` **plus** an interval well under a day
    (72000 works). With a multi-day interval the due moment walks, and the week it lands after
    Sunday's window has closed the record skips a whole cycle.
  - It is the one field that is a set rather than a `from_`/`to_` pair, because a pair cannot say
    "Monday and Thursday". Added 2026-09-21; before it, the two engine backup scripts carried the
    weekday as a shell literal and read it on the container host's clock, which is how one backup
    came to evaluate its hour window and its FULL/INCR choice on two different days.
- The ten bounds are a wall-clock question — *may it run at this moment?* — read in the node's
  configured timezone. Every bound is inclusive, every one is optional, `null` means no restriction
  on that dimension, and a pair whose `from_` exceeds its `to_` **wraps** (`from_hour: 22,
  to_hour: 6` spans midnight). `to_hour: 23` is the idiom for "all day"; `to_hour: 0` means
  midnight alone, which is how a schedule fires once a day by accident.
- The three intervals are elapsed time, in seconds, always in UTC.
- `repeat_interval: 0` means run once and leave it running — that is how a server that stays up is
  expressed. `repeat_interval: -1` means manual only: never scheduled, run on request. Use it for
  anything that writes.
- `timeout` must stay **above the slowest thing inside the command**, not above its average. A
  collection pass killed at its timeout loses every metric in it, not only the slow one. It is also
  the grace on a `running` row.
- Something still running when its interval comes round is skipped, not started twice.

#### Every interval is measured from the previous run's START

`repeat_interval: 300` means "start 300 seconds after the last **start**". A task that runs for 240
seconds is therefore due again about 60 seconds after it finishes — 45 in fact, because a run may
begin up to `min(interval × 5%, 30s)` early so that a scheduler sweep boundary does not cost a whole
cycle. That grace is why an interval of 1800 is observed as ~1770.

Measuring from the finish instead makes the declared number unreadable: the same `300` would mean a
5-minute cycle for a fast task and a 9-minute one for a slow one, with nothing in the config to say
which, and each run's duration would push the next one further out.

**It is one rule, in one place, for all four schedulers.**
`db_ops.lib.time_window.due_from_row(time_window, row, now, local_now)` is the entry point every app
calls; `run_anchor` is the only code that picks which column the anchor is read from, and
`explain_due` produces the verdict **and** the sentence explaining it, so a log line cannot disagree
with the decision it describes.

| App | What it schedules | Anchor | Its own defaults |
| --- | --- | --- | --- |
| `jobs` (daemon) | `app_commands[]` | `job_runs.started_at` | retry 60 s, timeout 300 s |
| `sql_tasks` | `sql_targets[]` | `sql_runs.started_at` | retry = `repeat_interval`, repeat 300 s |
| `backup_restore` | backup jobs, restore entries | `job_runs.started_at` | retry = `repeat_interval` |
| `metrics` | one metric on one target | `metric_results.collected_at`, stamped **before** the collector runs | retry 600 s |
| `reports` | a scheduled report | `report_send_state.last_run_at` — the start of the run that last sent | repeat 300 s |

Until 2026-09-19 two of those read something else: `sql_tasks` anchored on `sql_runs.finished_at`
and the reports app on the instant its last send *completed*, so one estate ran three different
meanings of one field. Nothing in any config said so.

**Why this is `lib` and not a `common.cli` command.** The daemon sweeps once a second and metrics
evaluate a verdict per target per metric — thousands a pass — so the rule is imported and evaluated
in-process. `common` is for operations that run as a subprocess, and a subprocess per due check
would cost more than the work being scheduled. Everything that is *not* that hot path asks the same
rule through the CLI instead:

```bash
python -m db_ops.common.cli due-check @data/due_request.json
# -> {"due": false, "reason": "interval not elapsed: 270s/300s",
#     "last_run": "...", "next_due_at": "..."}
```

### 5.1b The pointers between files — `check-references`

`data/` is a small relational database with **no foreign keys**. A backup job names a `server_id`, a
SQL target names a `credential_name` and a `sql_id`, an instance names the OS login that reaches its
host — and until 2026-09-19 nothing compared either side. Every failure of that kind has the same
shape: both files are valid, the config loads, the schedule runs, and the run dies at the moment it
needs the thing that is not there. Observed that morning on the container worker:

```
backup_restore.backup ERROR: backup_id=A1A_DBOPS_STORE_PG_115
A1A_DBOPS_STORE_PG_115/wal: server_id not found in db_instances.json: A1A-…-PG-5433
```

The rules are data — [`data/config_references.json`](../data/config_references.json) — and the check
is one command:

```bash
python -m db_ops.common.cli check-references '{}'                    # this node's data/
python -m db_ops.common.cli check-references '{"data_dir": "…"}'     # a bundle, or a worker's copy
```

Seven pointers are declared today, 207 of them present in this estate. Three things shape how it
reports:

- **Only `active` records fail.** Retiring a target by turning it off is normal; failing on it would
  teach the reader to stop reading. Those are listed under `inactive` instead, because an entry
  somebody re-enables is the next outage.
- **A pointer may have more than one legitimate home.** A notify level is wired either in
  `telegram_groups.json` or in `telegram_config.json`'s `level_chat_map`, so a rule may name several
  targets and landing in any of them is correct. The first run of that rule reported `private` — a
  working route — as dangling.
- **`password_ref` is deliberately not checked here.** The secret store is one encrypted blob, so
  "does this ref exist" needs the passphrase: that is `check-secret`, which answers it by
  authenticating rather than by looking.

**Run it on the node that runs the job.** The incident above was clean on the master — the master's
inventory *had* the instance — and broken on the worker, whose copy predated the rename. That is also
the shape a partial deploy leaves behind, which is why the command takes a data directory.

### 5.2 `notify` — who hears about it

```json
"notify": {
  "logging_on_run":  {"enabled": true, "telegram_chat": "backup", "chat_id": ""},
  "alert_on_error":  {"enabled": true, "telegram_chat": "critical", "chat_id": ""}
}
```

`logging_on_run` announces that it ran; `alert_on_error` announces that it failed. Each is a
`notify_rule` and names a **routing level**, not a chat: the level is looked up in the level → chat
map built from `telegram_groups.json` and `telegram_config.json`. A level with no chat does not
send, which is how a stream is muted — there is no second allow-list to keep in sync. Setting
`chat_id` directly overrides the lookup, for the rare case that needs one specific chat. An empty
`telegram_chat` means "follow the event's own severity", which is why adding a `notify` object
changes nothing until a rule is actually set.

### 5.2b `output` — what a SQL task does with its rows

```json
"output": {"format": "txt", "telegram_chat": "sql", "chat_id": "", "max_rows": null}
```

Required on every SQL task target. `format` is `none` (status only), `plain` (the rows pasted into
messages, ~30 rows each) or a file: `xlsx`, `csv`, `txt`, `xml`, `json`, sent as **one** Telegram
document. Result sets with the same columns are merged into one table first, so a task that runs
once per batch delivers one table, not one per batch. For more than a few dozen rows, a file is
the quiet choice: two messages per run (the run log and the document) whatever the row count.
`telegram_chat` / `chat_id` pick the chat the rows or file go to, the same way a `notify_rule`
does. `max_rows` (1–5000) caps what `plain` fetches; out of range is refused, not clamped.

### 5.3 `cmd_access` and `sql_access` — how a host and a database are reached

`cmd_access` is the OS half (13 fields, `method` required: `local` | `ssh` | `winrm`) and
`sql_access` the database half (4 fields, all optional, absent means `direct`). Two rules in them
cost real time when they are missed:

- **`method: "local"` means *inside the db_ops container*.** With a remote `host` it does not fail —
  it reports the container's own CPU, memory and disk under that host's name. `remote_exec` refuses
  the combination now.
- **`auth_type` defaults to `key`.** A host reached with a password needs it stated, or the login is
  never attempted. And `platform` belongs on the **instance**, not inside `cmd_access`: put there it
  is silently ignored.

### 5.4 `cleanup_retention` — how long backups are kept

One field, **in seconds**, required on every backup job and every restore entry. `0` is a real
setting and means *no age gate* — every file becomes a candidate and the chain rule alone decides
(never the newest full, nor anything at or after it). It does not mean "keep everything"; believing
that let a staging directory reach 16.7 GB. It is required precisely because an absent field reads
exactly like a considered one: on 2026-09-11 six of fourteen restore entries carried none and each
was running on a default nobody had chosen. The older spellings `retention_days` (days) and
`target_retention_seconds` (seconds) are still read, never written.

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

## 9. Moving a whole estate to another machine

Everything above describes editing one file at a time. Standing a *second* machine up on the same
estate is a different job, and doing it by copying directories gets three things wrong: `data/`
holds generated output and fixtures beside the config that matters, nothing checks that what
arrived is what left, and the secret store needs different handling from everything else.

Two commands do it instead.

```bash
# on the machine that already works
db-ops export-data prod-bundle.json

# on the new machine, after `pip install dbabrain`
mkdir estate && cd estate
db-ops import-data prod-bundle.json --plan --root .   # read this first; it writes nothing
db-ops import-data prod-bundle.json --root .
export DB_OPS_SECRET_KEY='<the source machine's passphrase>'
db-ops check-credentials                              # proves the store decrypts here
```

**Say `--root` on a first import, or run `db-ops init` first.** A new empty directory carries
nothing the tool recognises as configuration, so the tool root falls back to the last entry in the
order — the package's own location, which for a pip install is `site-packages`. An import that
lands there is now refused by name. It used to succeed: it wrote the estate into `site-packages`
and printed `imported into ...`, which reads like success and leaves nothing where the next command
looks.

### What is in the bundle

One JSON file, and its contents are **derived from `data/config_catalog.json`** — the same
allow-list §4 describes and the runtime store obeys. A config file that is catalogued crosses; one
that is not does not.

| | |
| --- | --- |
| `config.json` | §3. Every path in it is relative to the tool root, so nothing needs rewriting on the new machine |
| `data/config_catalog.json` | the allow-list itself. It is not listed inside itself, so it is carried by name |
| the catalogued `data/*.json` | §4 — the estate proper |
| `data/encrypted_secret_text.json` | **ciphertext**. See below |
| `assets/**`, `data/ssh_keys/**` | §7. Config names these files by path, so config without them points at nothing |

**`data/database-inventory.json` is deliberately absent.** It is generated output, and
`inventory-workflow` rebuilds it on the new machine against that machine's own estate. Carrying it
would seed a fresh install with yesterday's measurements from somewhere else.

A catalogued file that does not exist on the source machine is **named in the bundle**, not
invented — `export-data` prints the list, and so does `import-data`. An estate that runs no Oracle
has no `docker_db_connections.json`, and that is not an error.

### The passphrase does not travel

The secret store crosses as ciphertext, so the new machine inherits the estate's credentials. The
passphrase is **not in the bundle and there is no field for it to be in**: set `DB_OPS_SECRET_KEY`
on the new machine (§6) and prove it with `db-ops check-credentials`. Use `--no-secrets` on either
side if the new machine should keep its own.

### The bundle is a credential

It names every host, account and chat id in the estate. Never commit it and never attach it to an
issue. `.gitignore` covers `<name>-bundle.json`, which is why `export-data` suggests that shape and
warns when you pick a different one.

### After importing: the schedule may be for a different node role

`data/app_commands.json` gives each command a `node_role`, and the daemon runs only the ones that
match its own. **A process that was not told otherwise is `master`.** An estate exported from a
worker carries `worker` on every command, so importing it and starting the daemon runs nothing at
all — correctly, and for a reason nobody has been given.

`import-data` now says so when it sees that mismatch, and names the fix:

```bash
export DB_OPS_NODE_ROLE=worker      # or set node_role to "all" in data/app_commands.json
```

The daemon also reports it on its first tick (`app.daemon.nothing_scheduled`), but by then it is a
log line on a machine whose logs nobody is watching yet.

### What import refuses to do

- **Write the estate into `site-packages`.** Only when the root was not stated: `--root` is taken as
  said, on purpose. See the note above.
- **Write anything at all if any checksum fails.** Every entry carries a sha256 of the bytes it
  becomes, and all of them are verified first. A truncated transfer leaves the target untouched
  rather than half-applied.
- **Replace a file that already exists here with different content**, unless `--force`. The machine
  you are importing into may already be someone's working install. A file that already *matches* is
  not a conflict, so re-running an interrupted import finishes it.
- **Write outside the tool root.** A path inside a bundle is data, not an instruction: `..`,
  absolute paths and drive letters are refused.

Use `--plan` to see every action before any of them happen. It is the same habit as `--dry-run`
in §8, for the same reason.
