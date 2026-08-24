# DBA Brain

**A database operations toolkit for people who are on call.** It collects health metrics from your
SQL Server, Oracle, PostgreSQL and MySQL instances, grades them against policies you write, runs
your scheduled SQL, takes backups and *proves them by restoring them*, checks objectives against
real measurement history, and tells you about it — on a schedule, without an agent on any
monitored machine.

**It sends nothing anywhere by itself.** No telemetry, no usage reporting, no update check. It
connects to the databases and hosts you list, and — only if you turn it on and give it a token —
to a chat service. Nothing else.

> **This project is being renamed.** The distribution and the module path are still `db_ops`, so
> every command below reads `python -m db_ops.<app>.cli`. They become `dbabrain` when the code
> moves to the public repository; nothing else changes with them.
> <!-- TODO(rename): update the distribution name and every `python -m` path in this file once the
> package rename lands. -->

---

## What it is not

- **It is not an AI agent.** It is a toolkit with a CLI, where every capability takes one JSON
  object and answers with one. That shape is what lets a human, a shell script, a chat command and
  an AI agent all drive the same operation without a translation layer — and it is why an
  agent-facing interface can be added later without rewriting anything underneath it. Today
  nothing here talks to a model.
- **It does not replace incident analysis, change approval, or runbooks.** It supports them. A
  restore drill proves a backup is restorable; deciding to restore production is still yours.
- **It is not a dashboard product.** It has a web console for reading reports and editing
  configuration, but the primary interface is a CLI and its primary output is a record in a
  database you own.
- **It installs nothing on your databases.** No agent, no extension, no stored procedure. It logs
  in with a read-only account and asks questions.

## Who it is for

A DBA or small team responsible for a handful to a few dozen instances across more than one
engine, who already know what they want checked and are tired of it living in twelve cron jobs and
a folder of scripts. If you have one PostgreSQL cluster and a hosted monitoring product you are
happy with, this is more machinery than you need.

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
| [`docs/release_process.md`](./docs/release_process.md) | **When a change may be committed, and when a version may be released** — the gate both have to pass |
| [`examples/`](./examples) | Worked configurations you can copy whole |

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
