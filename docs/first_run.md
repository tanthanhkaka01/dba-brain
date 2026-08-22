# First run — from `pip install` to a collected metric

There are **two ways to drive this toolkit**, and they differ in who writes the configuration, not
in what runs afterwards. Both end at the same place: a tool root full of JSON, a store, and a
scheduled collection.

| | Who configures it | Status |
| --- | --- | --- |
| **A person, in a browser** | A web console: add an instance the way a database client does — host, port, user, password — and it collects | **Planned.** See [§4](#4-the-browser-path-what-exists-and-what-does-not) for what exists today and what is missing |
| **An agent, writing JSON** | Every decision is a file. An agent writes the files and runs three commands | **This is the supported path.** [§2](#2-the-agent-path) is written to be followed by a program |

> **`v0.1.0` ships the agent path.** The browser path is deliberately later: the configuration
> surface has to be right before something generates it, and a console that writes files nobody has
> agreed the shape of is a second source of truth.

---

## 1. What both paths need

**A tool root.** One directory holding `config.json`, `data/` and `secrets/`. Stand in it and every
command finds it — the working directory is the second entry in the resolution order, so no
environment variable is needed. Everything the toolkit writes (`logs/`, `runtime/`) appears there
and nowhere else, so deleting the directory leaves nothing behind.

```bash
pip install 'dbabrain[mssql]'      # or [postgres], [oracle], [mysql]
mkdir my-estate && cd my-estate
mkdir data secrets
```

<!-- TODO(rename): the distribution is `db_ops` until the rename lands; commands below read
     `python -m db_ops.<app>.cli` and become `dbabrain` with the package. -->

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

### 2.1 The nine files

Measured from a working install, not listed from the loaders. Seven files to collect a metric, two
more to deliver it.

| File | Decides |
| --- | --- |
| `config.json` | where logs, runtime and the other config files live |
| `data/store_config.json` | which database holds the results — SQLite or PostgreSQL |
| `data/db_instances.json` | **the estate**: one record per monitored instance |
| `data/users.json` | credentials, by name — never a password, only a `password_ref` |
| `secrets/secret_text.json` | the plaintext passwords, encrypted in step 2.3 and then deletable |
| `data/metric_definitions.json` | which metrics exist and how each grades |
| `data/app_commands.json` | what the daemon runs, and how often |
| `data/telegram_config.json` + `data/bot_telegram.json` + `data/telegram_groups.json` | delivery — only if you want alerts |
| `data/reports_config.json` | which report turns results into a message |

Every one has a `*.example.json` beside it in the package with a `notes` array explaining what it
decides and why. **Copy the example and edit it** — the examples are complete, and a hand-built file
usually goes wrong by omission rather than by error.

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
- **`disabled_collector_types`** switches off whole families. `cmd` and `docker` need a shell on the
  machine (`cmd_access`); leave them off until you have configured one, or every cycle records
  failures for collectors you never intended to run.

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

```json
// secrets/secret_text.json
{ "MSSQL_PROD_MONITOR": "the real password" }
```

```bash
export DB_OPS_SECRET_KEY='your passphrase'
python -m db_ops.control.cli encrypt-secret-text
```

That writes `data/encrypted_secret_text.json` — ciphertext, a random per-file salt and the
key-derivation parameters, and nothing else, which is why it is safe to commit while
`secrets/secret_text.json` is git-ignored and can be deleted afterwards.

**Verify before moving on**, because a wrong key produces an empty store rather than an error:

```bash
python -m db_ops.common.cli check-secret '{}'
```

### 2.4 Create the store and collect

```bash
python -m db_ops.db.cli --config config.json init
python -m db_ops.metrics.cli --config config.json collect --dry-run
python -m db_ops.metrics.cli --config config.json collect
```

`--dry-run` resolves targets, credentials and metric variants and connects to nothing. Run it first:
it is where a wrong `server_id`, a missing credential or an unmatched metric variant shows up with
the name of the thing that is wrong.

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

Three files and two commands. `data/bot_telegram.json` names the token by reference,
`data/telegram_groups.json` maps a notify level to a chat, `data/telegram_config.json` turns
delivery on.

```bash
python -m db_ops.reports.cli  --config config.json queue-metrics-reports
python -m db_ops.telegram.cli --config config.json send-queue
```

The worked version of this, with the three failures a first attempt actually meets, is
[`examples/sqlserver-quickstart/README.md`](../examples/sqlserver-quickstart/README.md) step 6. The
one worth knowing in advance: **a queue run consumes the metric rows it read whether or not it
queued anything**, so an attempt made before the chats exist marks them reported, and the next run
says "already reported" and delivers nothing. Collect again and re-queue.

### 2.6 Put it on a schedule

`data/app_commands.json` is what the daemon runs and how often. Start it last, once the commands
above work by hand:

```bash
python -m db_ops.jobs.daemon --config config.json --delay-seconds 10
```

### 2.7 What an agent should assert at each step

| After | Assert |
| --- | --- |
| 2.2 | `collect --dry-run` resolves the target and names a credential |
| 2.3 | `check-secret` authenticates every ref it was asked about |
| 2.4 | `error_count: 0`, and `result_count` equals the metrics you expect |
| 2.5 | `"queued"` is non-zero, then `"sent"` equals `"read"` |
| 2.6 | the daemon logs a run for each app command, and `err=0` |

Each of these fails silently if skipped: a target that cannot be resolved is reported as a target
with no results, a secret that cannot be decrypted looks like an empty store, and a queue that
delivered nothing looks the same as a quiet estate.

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

**And `webhost` is not in the `v0.1.0` install.** The release ships the packages the agent path
needs; the console arrives with the flow above rather than before it.

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
