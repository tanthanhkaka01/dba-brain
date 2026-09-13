# DBA Brain

**A database operations toolkit for people who are on call.** It collects health metrics from your
SQL Server, Oracle, PostgreSQL and MySQL instances, grades them against policies you write, runs
your scheduled SQL, takes backups and *proves them by restoring them*, checks objectives against
real measurement history, and tells you about it — on a schedule, without an agent on any
monitored machine.

**It sends nothing anywhere by itself.** No telemetry, no usage reporting, no update check. It
connects to the databases and hosts you list, and — only if you turn it on and give it a token —
to a chat service. Nothing else.

**Two ways to read what it is, and both are accurate:**

| | |
| --- | --- |
| **A database tool for SRE / DevOps** | Nothing is installed on a monitored machine — it reaches instances and hosts the way an operator does, over a connection and a credential you already have. Every decision is a JSON file you keep in version control, so a threshold change is a diff and a review rather than an edit to somebody's script. It schedules itself, or your scheduler drives it. |
| **A database operations layer for an AI agent** | Every capability is one command that takes **one JSON object and answers with one**, with five keys always present. There is no natural-language layer to translate through and no separate agent API: the command a person runs is byte-for-byte the command an agent runs. |

The second is not a later ambition. It is why the shape is what it is — and it is the **supported
path today**, because the console that would let a person add an instance in a browser is not
finished yet (see [Two ways to drive it](#two-ways-to-drive-it)).

**One code path is the point, not a convenience.** An agent cannot reach a capability a human
cannot audit, because there is only one of each. Every run is a row in `job_runs` and a line in a
log with the same shape whoever started it, so "what did the agent do last night" is the same
query as "what did the daemon do last night".

---

## The goal

**To be a tool a DBA, a DevOps engineer or an SRE can rely on — one that works alongside an AI
agent and saves a large amount of its tokens, and works just as well with no agent at all.**

Three commitments, and they are meant to be held to:

| | |
| --- | --- |
| **Reliable** | Every operation is typed, logged and repeatable. A scheduled run that failed can be replayed by hand from the same JSON request. Backups are not reported as successful — they are **restored, and the restore is the proof**. |
| **Cheap for an agent to use** | An agent working a database from a shell pays, in context, for every raw result set it reads — on every check, most of them healthy. Here it sends a small request and reads a **graded verdict**, then pulls the evidence only for what came back bad. |
| **Not dependent on one** | No model is required, configured, or contacted. Remove the agent and the daemon, the schedules, the reports and the alerts all still run. The agent is a caller, never a component. |

The shape of that, because it is the whole argument in one picture:

```text
  WITHOUT a tool                              WITH DBA Brain
  agent -> raw SQL -> agent                   agent -> DBA Brain -> database

  "check this database"                       "check this database"
    -> SELECT ... dm_os_wait_stats              -> one JSON request
    <- every row, into the context
    -> SELECT ... sys.databases                 <- one graded envelope:
    <- every row, into the context                    41 checks, 40 OK,
    -> SELECT ... backupset                           1 WARNING: appdb  
    <- every row, into the context                    log backup 31h old
    -> ... once per check, per run
    <- all of it, healthy or not               -> fetch the evidence for
                                                  the ONE that came back bad
  The model reads the evidence and
  derives the verdict, every time.            The verdict was derived in
  It pays for the 95% that was fine.          reviewed SQL, once, by a person.
```

The saving is not compression — it is **not sending what nobody needed to read**. Grading a metric
is a fixed decision that belongs in SQL somebody reviewed, not a judgement re-made by a model on
every cycle from raw rows. The agent's context is then spent on the thing that is actually hard:
what to do about the one check that failed.

**And the target that is not met yet:** a person who has never seen this project should go from
`pip install dbabrain` to a collecting install by pointing an AI agent at these docs — no estate
bundle from another machine, and no assumption that they already know what a database ought to be
checked for. Today that path expects more DBA knowledge than it should. Closing that gap is the
current priority; see [`docs/first_run.md`](./docs/first_run.md) for where it stands.

> **This project is being renamed.** The distribution and the module path are still `db_ops`, so
> every command below reads `python -m db_ops.<app>.cli`. They become `dbabrain` when the code
> moves to the public repository; nothing else changes with them.
> <!-- TODO(rename): update the distribution name and every `python -m` path in this file once the
> package rename lands. -->

---

## What it is not

- **It is not an AI agent — it is what an agent operates.** Nothing here talks to a model, holds a
  prompt, or decides anything on its own. It is a toolkit with a CLI, where every capability takes
  one JSON object and answers with one, and that shape is what lets a human, a shell script, a chat
  command and an agent drive the same operation without a translation layer. The distinction is
  worth keeping: a layer that does not reason is a layer you can audit, and the judgement about
  whether to restore production stays with whoever is accountable for it.
- **It does not replace incident analysis, change approval, or runbooks.** It supports them. A
  restore drill proves a backup is restorable; deciding to restore production is still yours.
- **It is not a dashboard product.** It has a web console for reading reports and editing
  configuration, but the primary interface is a CLI and its primary output is a record in a
  database you own.
- **It installs nothing on your databases.** No agent, no extension, no stored procedure. It logs
  in with a read-only account and asks questions.

## Who it is for

A DBA or small team responsible for a handful to a few dozen instances across more than one
engine, tired of it living in twelve cron jobs and a folder of scripts. If you have one PostgreSQL
cluster and a hosted monitoring product you are happy with, this is more machinery than you need.

Two more, deliberately:

**The person who gets paged about a database without being a DBA** — the DevOps or SRE owner of a
service, in a company that has no DBA at all. They need the same operations and should not have to
design a monitoring strategy to get them, so **the shipped catalogue is meant to be that
expertise**: adopting this should be configuring an estate, not deciding what a healthy database
looks like. That is the intent — `docs/first_run.md` is honest about how far it holds today.

**Whoever is building an agent that has to touch a database** and does not want to hand it a shell.
The alternative to a typed operation is an agent composing SQL and `psql` invocations from prose,
where the blast radius is whatever it happened to write. Here what it can do is the set of commands
that exist: each one reviewed when it was written, each one logged the way a human's run is.

---

## Capabilities

Ordered to match the reference docs; the **ORD** number links to each one.

| ORD | Component | Package / config | Responsibility |
| :---: | --- | --- | --- |
| [01](./docs/01_runtime_store.md) | Runtime store | `db_ops/db`, `data/store_config.json` | The database the toolkit keeps its **own** data in — job runs, measurements, report state, the delivery queue, restore history. SQLite to start, PostgreSQL when you outgrow it; the backend is one word in one file. |
| [02](./docs/02_logging_engine.md) | Logging engine | `db_ops/logging_ops`, `logs/` | Scoped application logs, runtime logs, shared errors, and daily archives. |
| [03](./docs/03_app_command_daemon.md) | App command daemon | `db_ops/jobs`, `data/app_commands.json` | The scheduler: runs each app on its own interval inside its allowed hours, skips one that is still running, and forwards the secret passphrase to every child process. |
| [04](./docs/04_metrics_engine.md) | Metrics engine | `db_ops/metrics`, `data/db_instances.json`, `data/metric_definitions.json` | Around ninety metrics across four engines — availability, capacity, performance, recoverability, security, maintenance — collected and normalised into one shape. |
| [05](./docs/05_sql_task_runner.md) | SQL task runner | `db_ops/sql_tasks`, `data/sql_commands.json`, `data/sql_targets.json` | Your own SQL, on a schedule or on request, against approved targets, delivered as text or a spreadsheet. The SQL is a reviewable file, never a string in configuration. |
| [06](./docs/06_reports_app.md) | Reports | `db_ops/reports`, `data/reports_config.json` | Turns measurements into scheduled reports and inventory pages, with a freshness gate so a stale number is never reported as a current one. |
| [07](./docs/07_telegram_app.md) | Chat delivery and commands | `db_ops/telegram`, the Telegram data files | Delivers the outgoing queue one message at a time, and executes the commands people send back — gated by the person's clearance *and* the chat's. |
| [08](./docs/08_backup_restore_app.md) | Backup / restore | `db_ops/backup_restore`, `data/restore_config.json` | Runs backups, restores them onto a disposable target, verifies the result, and records what was proven and when. |
| [09](./docs/09_sla_slo_compliance_app.md) | SLA / SLO compliance | `db_ops/sla`, `data/sla_policies.json` | Computes indicators from stored measurement history, evaluates objectives with an error budget, and reports what is left of it. |
| [10](./docs/10_sre_app.md) | SRE | `db_ops/sre`, `data/sre_config.json` | Provisions the disposable lab databases that drills and rehearsals need — single instances or small HA clusters, in Docker or on VMs. |
| [11](./docs/11_control_app.md) | Control | `db_ops/control`, `config.json` | Builds and deploys the toolkit to another node, and watches the toolkit itself — the one app that reports on the others. |
| [12](./docs/12_webhost_app.md) | Web host | `db_ops/webhost`, `data/webhost_config.json` | Serves the rendered reports over HTTP and hosts the console. Publishes files; never generates them. |
| [13](./docs/13_common.md) | **Common** — shared operations | `db_ops/common` | Reaching a host, running SQL, moving a file, rotating a password, confirming something dangerous. Every command takes one JSON object. **Invoked as a CLI, never imported.** |
| [14](./docs/14_lib.md) | **Lib** — shared rules | `db_ops/lib` | Values and rules that are pure functions of their arguments: time windows, notify routing, severity, formatting. Imports nothing from the rest of the project. **Only ever imported, never run as a CLI.** |

**Fourteen components, and the list is closed.** ORD 01–12 are the apps — one directory, one CLI
each — and 13/14 are the two shared layers they all sit on.

> **Every component has a doc, and every doc has a component.** One `docs/NN_*.md` per package,
> both directions, enforced by `tests/test_docs_cover_every_component.py`. A component is not
> finished until its doc exists.
>
> That is a claim few projects can make, and it was written the hard way: one shared layer reached
> 46 modules and roughly 6,500 lines with no documentation and a fully green suite, because nothing
> connected the two directories.

---

## Install

Python **3.12+**. Every database driver is an extra, so a DBA who runs only PostgreSQL is not made
to install an ODBC driver and an Oracle client to start.

```bash
python -m venv .venv
.venv/bin/pip install 'dbabrain[postgres]'      # Windows: .venv\Scripts\pip
```

| Extra | For |
| --- | --- |
| `[postgres]` | PostgreSQL — pure Python, nothing else to install |
| `[mysql]` | MySQL / MariaDB — pure Python |
| `[oracle]` | Oracle — no client library needed for 12.1 and newer |
| `[mssql]` | SQL Server — **also needs Microsoft's system ODBC driver** |
| `[ssh]`, `[winrm]` | OS-level metrics and maintenance on Linux / Windows targets |
| `[all]` | everything |

Full instructions, including the two prerequisites that are not pip-installable and the container
image: **[`docs/installation.md`](./docs/installation.md)**.

## What it produces

Before installing anything, look at the output. **[`examples/showcase/`](./examples/showcase)** holds
the four report families a live estate publishes — fleet inventory, per-server metrics, index usage,
SLA — captured whole, with every name that belongs to that estate replaced by a stable fake one.
Real fleets, real fragmentation, real verdicts; nothing invented and nothing traceable.

A repository cannot show you an HTML page — GitHub renders `.html` as source — so they are published
as a site by [`.github/workflows/pages.yml`](./.github/workflows/pages.yml), or you can serve the
folder yourself:

```bash
cd examples/showcase
python -m http.server 8000      # then open http://localhost:8000/
```

Most of those pages open straight from disk. The fleet metrics page does not: it is one page for the
whole estate and fetches one series file per server as you pick it, and a browser refuses that from
a `file://` page. That is the browser's rule, not a defect in the copy.

## Five minutes

A throwaway container, a least-privilege login, seven real measurements — and nothing to uninstall
afterwards:

**[`examples/postgres-quickstart/`](./examples/postgres-quickstart)** — needs no system packages.
**[`examples/sqlserver-quickstart/`](./examples/sqlserver-quickstart)** — same shape, and it finds
a real problem with the instance, then clears it once you fix it.

```bash
cd examples/postgres-quickstart
docker compose up -d
python -m db_ops.db.cli      --config config.json init
python -m db_ops.metrics.cli --config config.json collect --dry-run
python -m db_ops.metrics.cli --config config.json collect
python -m db_ops.metrics.cli --config config.json report
```

---

## Two ways to drive it

After `pip install`, someone has to describe the estate — which instances exist, which credentials
reach them, where alerts go. There are two ways to do that, and they differ only in who writes the
files.

| | Who configures it | Status |
| --- | --- | --- |
| **A person, in a browser** | A web console: add an instance the way a database client does — host, port, user, password — and it collects | **Planned.** The console ships and edits configuration; the add-an-instance flow does not |
| **An agent, writing JSON** | Every decision is a file. An agent writes them and runs three commands | **This is the supported path today** |

The agent path is the supported one on purpose: the configuration surface has to be settled before
something generates it, and a console that writes files nobody has agreed the shape of becomes a
second source of truth. The console itself ships — it serves the reports and edits configuration —
but adding an instance from a host, a port and a password is not in it yet.

**Both paths are written out step by step, with what to assert after each one, in
[`docs/first_run.md`](./docs/first_run.md).** It is written to be followed by a program and to be
readable by a person, because until the console lands they do the same thing.

---

## From nothing to a running estate

Every command below is real and in this release. The long form, with what each step proves and what
bites people, is [`examples/standing-up-a-node.md`](./examples/standing-up-a-node.md).

Two things belong in every shell that runs these:

```bash
export DB_OPS_SECRET_KEY="<your passphrase>"   # or --key / --key-base64 per command
export PYTHONIOENCODING=utf-8                  # a Windows console is cp1252 and dies on the emoji
```

### 1. Install, and write the starting files

```bash
pip install "dbabrain[postgres,mssql,ssh,winrm]"
mkdir my-estate && cd my-estate
db-ops guide          # writes nothing; says what the next steps are
db-ops init           # config.json, data/*.json, secrets/secret_text.json
```

`[winrm]` is not optional if you monitor a Windows host outside a domain: without `pypsrp` the OS
collectors fall back to a local shell that cannot authenticate, and the run looks fine while proving
nothing.

### 2. The store

```bash
python -m db_ops.db.cli --config config.json init
python -m db_ops.db.cli --config config.json check      # tables, schema_version
```

SQLite under `runtime/` by default — nothing else to install. `data/store_config.json` is where you
move to PostgreSQL later; `db use-store sqlite|postgres` switches it.

### 3. Prove the schedule runs before configuring anything

```bash
python -m db_ops.jobs.cli --config config.json --once
```

**Every shipped command must reach `status=done` on a root where nothing is configured**, reporting
what is missing rather than failing. The web host is skipped by `--once` on purpose — it is a
long-running service. If something errors here, stop: it will not get better once there is data.

### 4. Add a database — one command, not four hand-edits

```bash
python -m db_ops.common.cli instance-add - <<'JSON'
{"server_id": "ACME-192-0-2-9", "db_type": "sqlserver", "ip": "192.0.2.9",
 "port": 1433, "service_name": "MSSQLSERVER", "major_version": 16,
 "env": "prod", "username": "dba_monitor", "password": "<the password>"}
JSON
```

It writes the inventory record, the credential and the **encrypted** secret together; the password
never reaches the disk in the clear. Read it from stdin (`-`) rather than passing it inline, or the
password is in your shell history and in `argv`.

Three fields fail in ways that do not name themselves:

- **`service_name` is a label, not a database.** Metric collection connects to `master` on SQL
  Server, always. Naming a database here fails every target with `Cannot open database (4060)`.
- **`major_version` picks the query variant.** Wrong version, wrong SQL, confusing errors.
- **`default_credential_name` is a reference**, not a password.

Adding it is all there is: `enabled` defaults on and the flags cascade
`enabled → metrics → reports → alerts`, so the instance is collected **and** appears on the fleet
inventory, its own metrics page, the index report and SLA with no second switch. To keep one
registered but off the reports, say `"reports": {"enabled": false}` — that keeps it collected.

For an OS-only host (no database), add `cmd_access` with `method: "ssh"` or `"winrm"`, a
`credential_name`, and `auth_type` **stated explicitly** — it defaults to `key`. `method: "local"`
with a remote `host` reports the local machine's CPU under that host's name, and is refused.

```bash
python -m db_ops.common.cli list-targets '{}'          # what is registered
python -m db_ops.common.cli check-secret '{}'          # can each credential actually authenticate
python -m db_ops.metrics.cli --config config.json collect --dry-run
python -m db_ops.metrics.cli --config config.json collect
```

### 5. Start the daemon

```powershell
Start-Process -FilePath ".venv\Scripts\db-ops.exe" `
              -ArgumentList "daemon","--config","config.json" `
              -WorkingDirectory (Get-Location) -WindowStyle Hidden
```

Set `timezone` in `config.json` to your own zone **before** this. It decides what a `time_window`'s
`from_hour`/`to_hour` mean, and the default is UTC — on a `+07` estate that puts the heavy nightly
metrics (`DATABASE_CHECKDB`, `MAINTENANCE_INDEX_*`, `OS_REBOOT_PENDING`) in the middle of the
working day.

The reports and the console come up on `http://<host>:8080/report_dba/` and `/db_ops/`.

### 6. Telegram — the bot, its groups, and who may command it

```bash
# 1. the token, straight into the encrypted store - stdin only, never inline
python -m db_ops.common.cli secret-set - <<'JSON'
{"ref": "TELEGRAM_BOT_TOKEN", "value": "<token from BotFather>"}
JSON

# 2. who the token belongs to
python -m db_ops.telegram.cli --config config.json bot-info

# 3. add the bot to your groups as an ADMIN, post one message in each, then discover them
python -m db_ops.telegram.cli --config config.json save-updates

# 4. give each discovered group a level, and yourself the level that may run commands
python -m db_ops.telegram.cli --config config.json group-level --group -1001234567890 --level 40
python -m db_ops.telegram.cli --config config.json user-level  --user @you --level 100

# 5. prove the routing before trusting it - `route` takes the level as a positional argument
python -m db_ops.telegram.cli --config config.json route warning
python -m db_ops.telegram.cli --config config.json send-message --chat-id -1001234567890 --text "hello"
```

A **non-admin bot cannot read group messages** unless privacy mode is off — that is the usual reason
`save-updates` finds nothing. Alerts ship on; nothing can be sent before a token exists *and* a
group has a level, so storing the token is the switch. The explicit `level_chat_map` wins over a
group's own `notify_level`, so change both or neither.

**Never type a password into a bot command line.** A command line is stored — in Telegram's history
and in this node's tables. Answer secret prompts at the prompt (they are stored masked) or use a
`secret_ref`.

### 7. Backups

Backups are declared in `data/restore_config.json` under `backups`, each with its jobs and a
`cleanup_retention` **in seconds** — the field is required, because a backup directory nobody prunes
fills a disk and "keep forever" should be something somebody wrote down.

```bash
python -m db_ops.backup_restore.cli --config config.json list-backups     # what is configured
python -m db_ops.backup_restore.cli --config config.json backup           # run what is due
python -m db_ops.backup_restore.cli --config config.json prune-backups    # apply retention
```

One backup from a self-contained spec, without configuring anything:

```bash
python -m db_ops.common.cli backup-database @request.json
python -m db_ops.common.cli list-backup-files @request.json   # full / diff / log, per engine
```

### 8. Restore, and the drill that proves a backup is real

Restores are declared beside the backups under `restores`. A restore is how you learn a backup is a
backup rather than a file.

```bash
python -m db_ops.backup_restore.cli --config config.json restore-latest    # newest FULL, with recovery
python -m db_ops.backup_restore.cli --config config.json restore-workflow  # copy, restore, then clean up
python -m db_ops.common.cli restore-database @request.json                 # one configured restore, PITR where supported
```

**A physical restore carries the source's logins with it.** After a drill the target answers to the
*source's* password, so the credential you stored for the target goes stale — re-point it or rotate
it, and expect that the first collection after a drill fails until you do.

Both halves run on the schedule as `APP-BACKUP-RESTORE`; an install with nothing configured reports
"nothing configured" and finishes `done`.

### 9. Check what you built

```bash
python -m db_ops.common.cli self-status '{}'                       # this node, and the URLs it serves
python -m db_ops.reports.cli --config config.json inventory-workflow --days 7 --beauty 1
python -m db_ops.sla.cli --config config.json validate
```

The fleet page should list every instance you registered, each row linking to its own metrics page
and — for SQL Server targets, once the nightly index metric has run — its index report.

---

## How it is configured

Everything is JSON you own. **A new threshold, target, route, schedule or severity belongs in
`data/*.json`, never as a literal in Python** — the design assumes the person affected by a setting
can read it, change it, and be reviewed on the change.

```text
config.json      → where runtime output goes, and which declaration files to read
data/*.json      → what to run, monitor, report and deliver
secrets/         → the plaintext source of the encrypted secret store (git-ignored)
assets/          → the SQL and scripts that implement it
```

Every configuration file has a `*.example.json` beside it, complete enough to copy, rename and
edit, with a `notes` array explaining what it decides and why.

**The tool finds its configuration by a stated order**, not by where its code sits on disk:
`DB_OPS_HOME`, then the working directory if it holds `data/` or `config.json`, then the package
location. That is what makes an installed copy work, and it is why standing in an example
directory is enough to run against it.

See **[`docs/configuration.md`](./docs/configuration.md)**.

## Secrets

Passwords and tokens live encrypted in `data/encrypted_secret_text.json` — PBKDF2-HMAC-SHA256 over
a per-file salt, sealed with Fernet. The passphrase is supplied at run time and is never written to
disk, a log, or the store. Configuration names a *reference*; the value is resolved from the
environment first and the encrypted store second, so an organisation with an external secret
manager never has to use the built-in one.

A request carrying a password is passed on **stdin**, never as a command-line word — argv is
world-readable in the process table.

See **[`docs/security.md`](./docs/security.md)**, which also covers the least-privilege login the
toolkit needs on each engine, the audit trail, and running with no outbound network access.

## How it is put together

Fourteen components, two shared layers, four rules about who may call whom — and a guard test
beside each rule, because a diagram describes what someone intended and a test describes what is
true this morning.

> `common` **may not be imported** — it is only ever run as a CLI.
> `lib` **may not run a CLI** — it is only ever imported.
> No app imports another app.
> No shared layer imports an app.

See **[`docs/architecture.md`](./docs/architecture.md)**.

---

## Documentation

| Start here | |
| --- | --- |
| [`docs/installation.md`](./docs/installation.md) | pip and Docker, the engine extras, the ODBC and Oracle prerequisites |
| [`docs/first_run.md`](./docs/first_run.md) | From `pip install` to a collected metric — the agent path in full, and what the browser path will be |
| [`docs/configuration.md`](./docs/configuration.md) | Where configuration lives, and what every file decides |
| [`docs/security.md`](./docs/security.md) | Secrets, least privilege, the audit trail, air-gapped operation |
| [`docs/architecture.md`](./docs/architecture.md) | The components, the layers, and the rules that keep them apart |
| [`examples/`](./examples) | Worked configurations you can copy whole |
| [`examples/showcase/`](./examples/showcase) | The pages a real estate publishes, scrubbed — what the output actually looks like |

| Component reference | |
| --- | --- |
| [`docs/01_runtime_store.md`](./docs/01_runtime_store.md) | Store backends, schema, migration, troubleshooting |
| [`docs/02_logging_engine.md`](./docs/02_logging_engine.md) | Log scopes, files, and archive behaviour |
| [`docs/03_app_command_daemon.md`](./docs/03_app_command_daemon.md) | Scheduling, time windows, timeouts, key forwarding |
| [`docs/04_metrics_engine.md`](./docs/04_metrics_engine.md) | The metric catalogue per engine, and least-privilege setup |
| [`docs/05_sql_task_runner.md`](./docs/05_sql_task_runner.md) | SQL task configuration, parameters, and execution |
| [`docs/06_reports_app.md`](./docs/06_reports_app.md) | Report creation, schedules, and finding delivery |
| [`docs/07_telegram_app.md`](./docs/07_telegram_app.md) | Delivery, bot commands, and permissions |
| [`docs/08_backup_restore_app.md`](./docs/08_backup_restore_app.md) | Backup, restore, verification, and history |
| [`docs/09_sla_slo_compliance_app.md`](./docs/09_sla_slo_compliance_app.md) | Objectives, indicators, and error budgets |
| [`docs/10_sre_app.md`](./docs/10_sre_app.md) | Lab provisioning, HA topologies, and delegated workflows |
| [`docs/11_control_app.md`](./docs/11_control_app.md) | Build, deploy, and watching the toolkit itself |
| [`docs/12_webhost_app.md`](./docs/12_webhost_app.md) | Serving reports, the stable `latest` link, snapshot selection |
| [`docs/13_common.md`](./docs/13_common.md) | The shared operations layer, command by command |
| [`docs/14_lib.md`](./docs/14_lib.md) | The pure rules layer, the purity guarantee, and the module index |

| Project | |
| --- | --- |
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | How to contribute, and the rules that exist because something broke |
| [`SECURITY.md`](./SECURITY.md) | Reporting a vulnerability |
| [`CHANGELOG.md`](./CHANGELOG.md) | What changed, written for someone deciding whether to upgrade |
| [`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md) | |

---

## Tests

The suite is **offline**: no database, no network, no delivery. That is why it runs anywhere and
why you can refactor with it.

```bash
.venv/bin/python -m pytest tests -q                    # full suite
.venv/bin/python -m pytest tests/test_<area>.py -q     # while developing
```

Tests read as prose — a module docstring explaining *why the behaviour matters*, then test names
that are sentences.

## License

[Apache License 2.0](./LICENSE). See [`NOTICE`](./NOTICE).
