# First run — from `pip install` to a collected metric

There are **two ways to drive this toolkit**, and they differ in who writes the configuration, not
in what runs afterwards. Both end at the same place: a tool root full of JSON, a store, and a
scheduled collection.

| | Who configures it | Status |
| --- | --- | --- |
| **A person, in a browser** | A web console: add an instance the way a database client does — host, port, user, password — and it collects | **Planned.** See [§4](#4-the-browser-path-what-exists-and-what-does-not) — the console ships, the add-an-instance flow does not |
| **An agent, writing JSON** | Every decision is a file. An agent writes the files and runs three commands | **This is the supported path.** [§2](#2-the-agent-path) is written to be followed by a program |

> **The agent path is the supported one.** The browser path is deliberately later: the
> configuration surface has to be right before something generates it, and a console that writes
> files nobody has agreed the shape of is a second source of truth. `webhost` ships from `v0.2.0`
> and serves the reports and a configuration editor; what it does not yet have is the flow that
> takes a host, a port and a password and produces a collecting target.

---

## 1. What both paths need

**A tool root.** One directory holding `config.json`, `data/` and `secrets/`. Stand in it and every
command finds it — the working directory is the second entry in the resolution order, so no
environment variable is needed. Everything the toolkit writes (`logs/`, `runtime/`) appears there
and nowhere else, so deleting the directory leaves nothing behind.

**`db-ops init` makes one.** Do not build it by hand:

```bash
pip install 'dbabrain[mssql]'      # or [postgres], [oracle], [mysql]
                                   # add [ssh] or [winrm] for the OS metrics (2.8)
mkdir my-estate && cd my-estate
db-ops init
```

It writes nine files and then prints the next six commands. It **never overwrites** — the files it
writes are the ones you edit immediately afterwards, so a second `init` cannot destroy your work.

One of the nine is **`AGENTS.md`, written into the tool root rather than into any repository**: an
agent that has just run `init` is standing in that directory, and a guide in a repository it never
cloned is a guide it will not read.

<!-- TODO(rename): the distribution is `db_ops` until the rename lands. The `db-ops` command
     below keeps its name; `python -m db_ops.<app>.cli` becomes `python -m dbabrain.<app>.cli`. -->

**One driver is not pip-installable.** SQL Server needs Microsoft's ODBC driver, a system package —
`pyodbc` is a binding, not a driver. PostgreSQL needs nothing: `pg8000` is pure Python, which is why
it is the easiest engine to start with. See [`installation.md`](./installation.md).

**A worked example beats a blank directory.** `examples/sqlserver-quickstart/` and
`examples/postgres-quickstart/` are complete tool roots that run against a throwaway container.
Copy one and edit it rather than starting from nothing — every file below exists there, filled in.

---

## 2. The agent path

Written as a contract: each step says what to write, what to run, and how to know it worked. An
agent should check the verification of each step before starting the next one, because every
failure below is silent in a different way.

### 2.1 What `init` wrote, and which two files you edit

| File | Decides | You edit it |
| --- | --- | :---: |
| `AGENTS.md` | how to drive this tool root — written for whatever comes next | — |
| `config.json` | where logs, runtime and the other config files live | — |
| `data/store_config.json` | which database holds the results. **SQLite, always, on a first run** | — |
| `data/db_instances.json` | **the estate**: one record per monitored instance | **yes** |
| `data/users.json` | credentials by name — never a password, only a `password_ref` | **yes** |
| `secrets/secret_text.json` | the plaintext passwords, encrypted in 2.3 and then deletable | **yes** |
| `data/metric_definitions.json` | which metrics exist and how each grades — **all 90** | only to add metrics |
| `data/reports_config.json` | which reports exist and how often — daily, hourly warning, hourly critical | only to retime them |
| `data/telegram_support_commands.json` | the bot commands the toolkit answers in a chat | — |
| `data/app_commands.json` | **what the daemon runs and how often** | to enable or retime |
| `data/telegram_config.json`, `data/telegram_groups.json` | delivery — only if you want alerts | for 2.5 |

**SQLite on a first run is a decision, not a convenience.** Expecting PostgreSQL would mean the
first thing a new user meets is installing a database to hold the results of monitoring a database.
The `postgresql` block is written out in full and inert, so moving later is an edit rather than a
discovery of which fields exist.

**A scaffold is not an example.** `init` writes the *least* that is already valid; the
`data/*.example.json` files in the repository are documentation — every field, with notes. Read the
example when you want to know what a field is for; edit the scaffold when you want to run.

**Four of those files are product data and arrive complete**, because they describe what the
toolkit can do rather than what you monitor: the metric catalogue and its 90 collectors, the report
definitions, the bot commands, and the schedule. You never write them from scratch. Before
2026-08-23 three of the four were missing after `init`, and the daemon failed on every cycle
against a file nobody had been told to create.

### 2.2 Name the instance

`data/db_instances.json`. The minimum that collects, from a verified install:

```json
{
  "db_instances": [
    {
      "ord": 1,
      "db_instance_name": "prod_sqlserver",
      "server_id": "ACME-192-0-2-115-MSSQL-1433",
      "site": "HQ",
      "env": "prod",
      "ip": "192.0.2.115",
      "port": 1433,
      "db_type": "sqlserver",
      "instance_name": "MSSQLSERVER",
      "major_version": 16,
      "service_name": "SALESDB-PROD",
      "database_names": ["master", "msdb", "SALESDB"],
      "default_credential_name": "sqlserver_prod_monitor",
      "enabled": true,
      "metrics": { "enabled": true, "disabled_collector_types": ["cmd", "docker"], "metric_overrides": {} },
      "reports": { "enabled": true },
      "alerts": { "enabled": false }
    }
  ]
}
```

Four fields are easy to get wrong, and each fails in a way that does not name itself:

- **`server_id` is the only key.** One machine, one id, never duplicated — everything else joins on
  it. Two records with one id is a silent merge.
- **`service_name` is a label, not a database.** On SQL Server the collector always connects to
  `master` and the metric SQL issues its own `USE`. Passing the label as the connection database
  fails *every* SQL Server target at once with `Cannot open database`.
- **`env` changes severity.** A metric can be graded differently in `prod` and `lab`; a lab machine
  labelled `prod` pages somebody.
- **`disabled_collector_types`** switches off whole families. `cmd` and `docker` need a shell on
  the machine (`cmd_access`, below); a target without one **skips** those metrics with a line
  saying so, so leaving them enabled costs nothing but tells you nothing either.

Then the credential, in `data/users.json` — a name and a reference, never a password:

```json
{
  "database_credentials": [
    {
      "server_id": "ACME-192-0-2-115-MSSQL-1433",
      "db_type": "sqlserver",
      "service_name": "SALESDB-PROD",
      "instance_name": "MSSQLSERVER",
      "credentials": [
        {
          "credential_name": "sqlserver_prod_monitor",
          "username": "monitor_user",
          "password_ref": "MSSQL_PROD_MONITOR",
          "role": "MONITOR"
        }
      ]
    }
  ],
  "remote_credentials": []
}
```

`credential_name` is the handle `db_instances.json` points at, and `password_ref` is a key in the
encrypted store. Renaming either silently breaks the join.

**Grant the login as little as possible.** [`security.md`](./security.md) has the measured
least-privilege set per engine — two server permissions on SQL Server, `pg_monitor` on PostgreSQL,
and the two that are not guessable on Oracle and MySQL.


### 2.3 Put the password in the store, not in a file

`secrets/secret_text.json` is a **flat map of reference to secret**, and nothing else:

```json
{ "MSSQL_PROD_MONITOR": "the real password" }
```

A key beginning with `_` is skipped, so `_notes` is commentary rather than a secret.

```bash
db-ops encrypt-secret --key-base64 <your passphrase, base64>
```

`encrypt-secret` is a top-level command, so the key flag follows it. **The app CLIs are the other
way round** — see 2.4. Prefer `--key-base64` on Windows: a passphrase containing `$` or `#` is
shell-hostile in PowerShell.

That writes `data/encrypted_secret_text.json` — ciphertext, a random per-file salt and the
key-derivation parameters, and nothing else, which is why it is safe to commit while
`secrets/secret_text.json` is git-ignored and can be deleted afterwards.

**Verify before moving on**, because a wrong key produces an empty store rather than an error:

```bash
db-ops check-credentials          # does every configured target resolve to a real login
```

### 2.4 Create the store and collect

```bash
db-ops metrics collect --dry-run
db-ops metrics --key-base64 <your passphrase, base64> collect
```

**`--key-base64` goes before the subcommand**, not after it. The app parses it, its subparser does
not, so `metrics collect --key-base64 …` fails with `unrecognized arguments` — which reads like a
wrong flag rather than a wrong position.

`--dry-run` resolves targets, credentials and metric variants and connects to nothing. Run it first:
it is where a wrong `server_id`, a missing credential or an unmatched metric variant shows up with
the name of the thing that is wrong. **`0 targets` is the answer to an empty inventory, not an
error** — it means 2.2 has not been done yet.

A successful run prints one line per metric and a summary:

```text
METRIC run_id=1 target=ACME-.../sqlserver/SALESDB-PROD metric=INSTANCE_STATUS status=OK rows=1 inserted=1
...
ok_count: 6
warning_count: 1
error_count: 0
```

**`error_count: 0` is the check that matters.** A metric that could not run records an error and the
scan continues — one bad target never aborts the estate — so a non-zero count is the only thing that
says a target is misconfigured rather than healthy.

### 2.5 Deliver it

Put the bot token in the secret store the same way the database password went in, name it in
`data/telegram_config.json`, and put your chat id in `data/telegram_groups.json`. A bot cannot open
a conversation, so message it once first — then `db-ops telegram get-updates` shows the chat id.

**The direct way — build the alert from what was collected, and send it:**

```bash
db-ops metrics  alert-summary --include-warning     # the text, from what was just collected
db-ops telegram send-message --chat-id <id> --text "<that text>"
```

`alert-summary` reads the stored results rather than re-querying anything, so it says what the last
collection found and costs the monitored instance nothing.

**Or the scheduled way**, which is what you want once this runs unattended — it dedupes, splits
messages over Telegram's 4096-character limit, and routes by severity level:

```bash
db-ops reports  --config config.json queue-metrics-reports
db-ops telegram --config config.json send-queue
```

> One thing to know before the first run: **a queue run consumes the metric rows it read whether or
> not it queued anything**, so an attempt made before `data/telegram_groups.json` has a chat marks
> them reported and the next run says "already reported". Collect again and re-queue.

The worked version, against a throwaway container, is
[`examples/sqlserver-quickstart/README.md`](../examples/sqlserver-quickstart/README.md) step 6.

### 2.6 Put it on a schedule

`data/app_commands.json` is what the daemon runs and how often, and `init` already wrote it. Run
the commands above by hand first — the daemon runs *those same commands*, so anything broken is
easier to read in the foreground — then start it:

```bash
db-ops daemon --config config.json --delay-seconds 10 --key-base64 <your passphrase, base64>
```

Each entry is one app on its own interval, inside its own allowed hours. Trim the list to what you
actually run: the default includes SQL tasks, SLA, backup/restore and the web console, and an app
you have not configured fails every cycle rather than doing nothing.

**Two things to know when it appears to do nothing.**

`node_role` decides which machine runs a command. The scaffold ships `"all"`, which runs anywhere.
A two-node estate sets `"master"` or `"worker"` per command and declares each node's role with the
`DB_OPS_NODE_ROLE` environment variable — deliberately not in `config.json`, so a config copied
between nodes cannot mislabel one. If nothing runs, the daemon says which of the three reasons it
is (nothing defined, nothing active, or everything filtered out by role) and names the fix.

The daemon runs its children with **its own interpreter**, not whatever `python` means on the
machine. Installing into a virtualenv and scheduling `python -m db_ops...` used to hand every child
a Python without the toolkit in it — every command failing with `ModuleNotFoundError`, once a
minute, in output nobody reads.

### 2.7 What an agent should assert at each step

| After | Assert |
| --- | --- |
| 2.1 | `db-ops init` reported the files it wrote, and `AGENTS.md` is one of them |
| 2.2 | `db-ops metrics collect --dry-run` resolves **1 target**, not 0, and names a credential |
| 2.3 | `db-ops check-credentials` resolves every target to a real login |
| 2.4 | `error_count: 0`, and `result_count` equals the metrics you expect |
| 2.5 | `alert-summary` prints the target you configured; the send returns a `message_id` |
| 2.6 | the daemon logs a run for each app command, and `err=0` |

Each of these fails silently if skipped: a target that cannot be resolved is reported as a target
with no results, a secret that cannot be decrypted looks like an empty store, and a queue that
delivered nothing looks the same as a quiet estate.

That is one database, monitored and delivering. **2.8 adds the other kind of target** — the host
itself, which needs no database credential at all.
### 2.8 The OS metrics: monitoring a host with no database on it

`db-ops init` writes the whole catalogue — 93 metrics, of which **14 read the operating system
rather than a database**: CPU, memory, disk, uptime, load, service state, the top processes by
memory. They are worth doing early for a reason that is easy to miss: **they need no database
credential and no database at all.** A host you cannot log into yet still answers them, and "the
disk is full" explains most of the database alarms you would otherwise go on to debug.

They need two things. **The SSH or WinRM extra**, which is not installed by default:

```bash
pip install 'dbabrain[ssh]'        # or [winrm] for Windows targets
```

Without it every OS metric warns `paramiko is required for SSH`, naming this command.

And **a way onto the machine**, which is a `cmd_access` block on the target:

```json
{
  "server_id": "ACME-192-0-2-115-MSSQL-1433",
  "platform": "linux",
  "cmd_access": {
    "enabled": true,
    "method": "ssh",
    "host": "192.0.2.115",
    "port": 22,
    "shell": "bash",
    "credential_name": "acme_linux_ssh",
    "auth_type": "password"
  }
}
```

**`platform` sits on the target, not inside `cmd_access`** — it is a fact about the machine, and
it is what picks the Linux or Windows variant of each OS metric. Leave it out and every `cmd`
metric fails with `Target platform is required`. An `os` field of `"Ubuntu 22.04"` or
`"Windows Server 2022"` is enough on its own; `platform` is the explicit form.

**`auth_type` is the field that catches people, and it defaults to `key`.** A block that names a
`credential_name` holding a password but leaves `auth_type` out sends paramiko no credential at
all, and the failure comes back as `No authentication methods available` — which sounds like the
server refusing you and is really this file. Set it explicitly: `"password"` with a
`credential_name`, or `"key"` with a `key_file` (a bare name resolves inside `data/ssh_keys/`).

The credential itself goes in `data/users.json` under `remote_credentials`, **nested under the
`server_id`** — not in a flat list:

```json
{
  "remote_credentials": [
    {
      "server_id": "ACME-192-0-2-115-MSSQL-1433",
      "credentials": [
        {"credential_name": "acme_linux_ssh", "username": "dbaops",
         "password_ref": "ACME_LINUX_SSH_PASSWORD"}
      ]
    }
  ]
}
```

`password_ref` names an entry in `secrets/secret_text.json` — and, exactly like a database
password, **it is not read from there at run time**. Add it to that file and encrypt, or the
collection warns `Password ref not found in environment or the secret store`:

```bash
db-ops encrypt-secret --key-base64 <your passphrase, base64>
```

Then collect, and check that the OS rows arrived:

```bash
db-ops metrics collect
db-ops metrics summary-latest
```

**Windows targets take the same shape** with `"method": "winrm"`, `"platform": "windows"` and
`"shell": "powershell"`; the catalogue carries a PowerShell variant of every OS metric and picks
it from `platform`, so nothing else changes.

If a target has no `cmd_access`, nothing breaks — collection skips those metrics and says why:

```
METRIC_SKIP target=... metric=OS_CPU_USAGE reason=unsupported: no cmd_access is configured
on this target (an OS metric needs a way onto the host)
```

---

## 3. Configuration is data, and that is what makes the agent path work

There is no configuration in code. Thresholds, targets, routes, schedules and severity maps are all
files, and the same file is read by a human editor, by the console, and by whatever writes JSON. An
agent driving this toolkit is not using a side door — it is using the only door there is.

Two consequences worth stating for anything generating these files:

- **Every `db_ops.common.cli` command takes one JSON object** — inline, `@file`, or on stdin — in
  the same shape as the `data/*.json` files. So a value read out of configuration can be passed
  straight into a command without translation.
- **A file the loader does not know is invisible, however present it is on disk.** Adding a record
  to a collection is enough; adding a *new file* means adding it to the catalog too.

---

## 4. The browser path: what exists and what does not

The web console is real and is not the thing described at the top of this page.

**Today** `db_ops/webhost/` serves the generated reports and a console with login, a dashboard of
the apps, configuration editing and a "Run now" button. It edits files that already exist.

**What is missing** is the flow this project intends: *add an instance the way a database client
does* — host, port, user, password, test the connection, save — with the console writing
`db_instances.json`, `users.json` and the encrypted secret entry itself, so that a person who has
never read this page can get a first collection.

**`webhost` ships from `v0.2.0`.** What is missing is not the app — it serves the reports and a
console that edits configuration today — but the add-an-instance flow above, which is what turns
the console into something a person can start from.

Until then a person configures the same way an agent does — by copying an example tool root and
editing JSON — which is why [§2](#2-the-agent-path) is written to be readable by both.

---

## 5. Where to go next

| Question | Where |
| --- | --- |
| A worked run against a throwaway container | [`examples/sqlserver-quickstart/`](../examples/sqlserver-quickstart/README.md) |
| What every configuration file decides | [`configuration.md`](./configuration.md) |
| The least-privilege login per engine | [`security.md`](./security.md) |
| The metric catalogue | [`04_metrics_engine.md`](./04_metrics_engine.md) |
| How a request crosses the layers | [`architecture.md`](./architecture.md) |
