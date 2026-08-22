# Security

**The toolkit sends nothing anywhere by itself.** There is no telemetry, no usage reporting, no
update check, no crash upload. It connects to the databases and hosts *you* list in
`data/db_instances.json`, and — only if you enable it and give it a bot token — to the Telegram
API. Nothing else.

**What makes that true is the absence of a bot token, not a setting.** With no token in the
encrypted secret store there is nothing to authenticate with and no message can leave; the sender
stops with an error naming the reference it could not find. A chat has to exist too — a level with
no chat in `data/telegram_groups.json` queues nothing.

`"enabled": false` in `data/telegram_config.json` is a **routing** switch: it is what producers and
the command side ask before they decide to notify. Measured on 2026-08-22, it does not gate the
sender — `send-queue` delivers whatever is already queued if a token and a chat id are present. So
if you have configured delivery and want it to stop, remove the token reference or clear the chat
map rather than relying on that flag alone.

> **Naming, while the project is being renamed.** Environment variables are still `DB_OPS_*` and
> module paths still `db_ops.*`.
> <!-- TODO(rename): update the env prefix and module paths on this page once the rename lands. -->

---

## 1. What it needs on a monitored instance

Far less than an administrator, and that is worth insisting on: a monitoring pass that runs
unattended every five minutes should not be able to change the instance it is measuring.

### PostgreSQL

`pg_monitor` and nothing else. It is the role PostgreSQL ships for exactly this: the elevated
*read* of `pg_stat_*`, `pg_ls_*` and the size functions, with no write anywhere.

```sql
CREATE ROLE monitor_user LOGIN PASSWORD '<set outside source control>';
GRANT CONNECT ON DATABASE postgres TO monitor_user;
GRANT pg_monitor TO monitor_user;
```

Catalogue-only metrics work with less. No superuser, no extension.

### SQL Server

A login with `VIEW SERVER STATE` and `VIEW ANY DEFINITION` at the server level, plus `CONNECT` and
`db_datareader` on the databases you want reported on. `VIEW SERVER STATE` is what the dynamic
management views need; `VIEW ANY DEFINITION` is what the configuration and principal metrics need.

Two signals need a little more, and **a missing right drops only that signal** — the metric still
returns what it could read rather than failing the target: SQL Agent job status wants membership of
`SQLAgentReaderRole` in `msdb`, and the error-log signals want `EXECUTE` on `xp_readerrorlog`.

Server-metadata *export* is a different job with a different account. Reading every login's
password hash needs `CONTROL SERVER`, which is `sysadmin` in all but name — do not grant it to the
monitoring login. Run that export deliberately, as a DBA, and see
[`docs/13_common.md`](./13_common.md).

**The collector always connects to `master`.** The metric SQL issues its own `USE`. A target's
`service_name` is a *label* (`APPDB-PROD`), not a database — using it as the connection database
fails every SQL Server target at once, which is why the connection database is enforced in code
rather than left to configuration.

### Oracle

```sql
CREATE USER monitor_user IDENTIFIED BY "<set outside source control>";
GRANT CREATE SESSION TO monitor_user;
GRANT SELECT_CATALOG_ROLE TO monitor_user;
GRANT SELECT ON sys.v_$diag_alert_ext TO monitor_user;   -- alert-log signal only
```

`SELECT_CATALOG_ROLE` covers the data-dictionary and dynamic performance views the metrics read —
measured on 23ai: 27 of the 28 shipped Oracle collectors run with the first three statements alone.
Individual `SELECT` grants on the specific views work too and are tighter.

**The twenty-eighth is the alert log, and it needs the fourth line.** `LOG_RECENT_CRITICAL` reads
`V$DIAG_ALERT_EXT`, which is granted to `DBA` and **not** to `SELECT_CATALOG_ROLE` — so it fails
with `ORA-00942` on an account that reads every other view without complaint. As on SQL Server, a
missing right there drops that one signal rather than the target.

**Oracle 8i is not covered by that measurement.** `SELECT ANY DICTIONARY` did not exist before 9i
and `SELECT_CATALOG_ROLE` did not reach the `V$` views then, so a pre-9i instance needs the `V_$`
views granted individually — and in practice those targets are reached through the legacy bridge
with a DBA account already (see [`13_common.md`](./13_common.md) → `oracle_bridge`).

### MySQL

```sql
CREATE USER 'monitor_user'@'%' IDENTIFIED BY '<set outside source control>';
GRANT PROCESS ON *.* TO 'monitor_user'@'%';
GRANT SELECT ON performance_schema.* TO 'monitor_user'@'%';
GRANT SELECT ON mysql.innodb_table_stats TO 'monitor_user'@'%';
GRANT SELECT ON appdb.* TO 'monitor_user'@'%';   -- once per monitored database
```

Measured on MySQL 8.0.46: with exactly those grants, **eleven of the twelve shipped MySQL metrics
collect**. Three things about that list are not guessable, and two of them fail *silently*, which
is the failure mode worth spending a paragraph on — an under-granted account monitors nothing and
reports healthy.

- **`information_schema` cannot be granted and does not need to be.** `GRANT SELECT ON
  information_schema.*` is refused outright (`ERROR 1044`), even for `root`. Every account may read
  it; what it returns is filtered by what that account is *otherwise* allowed to see. So the last
  line above is the one that matters: without `SELECT` on a monitored database, that database is
  simply absent from `information_schema.tables` and `schemata`, and `DATABASE_STATUS`,
  `STORAGE_DATA_FILE_SPACE` and `DATABASE_DATA_SIZE` return **no rows and no error**.
- **`PROCESS` is what makes other accounts' work visible.** Without it `information_schema.
  processlist` returns your own session and nothing else — measured: 1 row where the instance had
  4 — so `QUERY_LONG_RUNNING` reports nothing while a query runs for an hour. `innodb_trx` is the
  loud one: it raises `ERROR 1227` instead of filtering.
- **`mysql.innodb_table_stats` is in neither obvious schema.** `MAINTENANCE_STATISTICS_AGE` reads
  it, and it lives in the `mysql` catalogue rather than in `information_schema` or
  `performance_schema`.

`SELECT` on `performance_schema` is read by one metric, `PERFORMANCE_WAIT_STATS`, and that one
does fail loudly without it. Nothing shipped reads replication status, so no replication privilege
is needed.

The twelfth metric is `LOCK_BLOCKING_SESSIONS`, and no grant fixes it: it reads
`information_schema.INNODB_LOCK_WAITS`, which MySQL **removed in 8.0** (verified working on 5.7.44,
`ERROR 1109` on 8.0.46). Read a failure of that metric on 8.0 as a version problem, not a
permission one.

**MariaDB is not covered by that measurement.** It keeps `INNODB_LOCK_WAITS` far longer than MySQL
did and its privilege names diverge; treat the list above as a starting point there and check the
metric results rather than assuming.

### The machine, not the database

OS-level metrics (CPU, memory, disk, uptime, listening ports) need a shell on the machine, declared
per target in `cmd_access` as `ssh` or `winrm` with its own credential. An account that can read
those facts is enough; the maintenance operations that need more are separate, confirmed, and
opt-in.

Two refusals are built in rather than documented as advice:

- **`cmd_access.method: "local"` with a remote `host` is refused.** It would run the command
  wherever the process happens to be and report that machine's CPU under the remote machine's
  name — a wrong answer that looks exactly like a right one.
- **One broken target does not abort a scan.** A malformed `cmd_access` is recorded against that
  target. A single mis-configured entry costs one target, not the whole estate's monitoring.

---

## 2. Secrets

### The store

Passwords, tokens and passphrases live encrypted in `data/encrypted_secret_text.json`. That file
is safe to commit: it holds ciphertext, a random per-file salt, and the key-derivation parameters,
and nothing else.

| | |
| --- | --- |
| Key derivation | PBKDF2-HMAC-SHA256, 200,000 iterations, random per-file salt |
| Encryption | Fernet — AES-128-CBC with an HMAC-SHA256 authentication tag |
| Passphrase | Supplied at run time. **Never written to disk, a log, or the store.** |

The plaintext source you edit is `secrets/secret_text.json`, a flat map of reference → value. It is
git-ignored and stays that way. Re-encrypt after every change:

```bash
python -m db_ops.control.cli encrypt-secret-text --key-base64 "<base64-passphrase>"
```

The command verifies a decrypt round-trip before writing, so a bad file never reaches disk. It
**refuses to run with no passphrase** rather than guessing one: encrypting under the wrong
passphrase produces a store that decrypts nowhere, and the round-trip check cannot catch it — it
verifies against the same wrong key.

`--key-base64` is not extra security. Base64 is not encryption; it exists so that `$` and `#` in a
passphrase survive a shell. The encrypted file is exactly as safe as the passphrase behind it.

### How a secret is named, and how it is resolved

Configuration never carries a secret value. It carries a **reference** — `password_ref`,
`password_env`, `secret_ref`, `authentication_info_ref` — and the reference is resolved at use
time:

1. an environment variable of that name, if set;
2. the encrypted store.

That order is the escape hatch: an organisation that keeps secrets in an external manager injects
them as environment variables and never uses the built-in store at all.

A reference that configuration names and neither source carries **raises**. A store the tool
cannot authenticate to must fail loudly, not connect as nobody.

Name a reference after the thing it opens, not after the person who holds it. An account handed
over keeps its reference; one named after somebody has to be renamed across every file the day
they leave.

### Where secrets are *not* written

- **Not in argv.** Any request carrying a password is passed on **stdin**, never as a command-line
  word — argv is world-readable in the process table on every machine this runs on.
- **Not in logs.** Connection strings are built in two forms: a password-free one that is what
  gets logged and reported, and a resolved one that exists only at the moment of connecting.
  Store declarations travel with a `redact()` form for error messages.
- **Not in the runtime store.** The store holds run history, measurements and findings. It holds
  no credential.
- **Not in metric output.** The collectors deliberately do not emit SQL text, `archive_command`,
  passwords or connection strings.
- **Not in the image.** The container carries the code. Configuration, `data/` and the passphrase
  arrive at run time, which is what lets one image serve several estates and means an image is not
  a copy of your credentials.

### Losing the passphrase

The encrypted store cannot be recovered without it. Treat it as the root password it effectively
is: hold it where you hold root passwords, and re-encrypt from the plaintext source if you rotate
it.

---

## 3. Authorisation for dangerous operations

Some operations take a machine or a service down. Two independent files gate them, and they answer
different questions on purpose:

| File | Question |
| --- | --- |
| `telegram_support_commands.json` + `telegram_users.json` | **Who may ask.** Both the person's clearance and the chat's clearance must allow the command. |
| `emergency_operations.json` | **How hard it is to confirm**, once asked. |

The second knows nothing about chats, which is what makes it apply equally to a chat message, a
scheduled run and someone typing at a shell.

| Level | Confirmation |
| --- | --- |
| 100 — takes a machine or service down | Two answers: `yes`, then **the target's own identifier typed out**. That cannot be given from muscle memory, and a payload written for one host is rejected by another. |
| 50 — no downtime, large blast radius | One typed `yes`. |
| 0 — read-only or self-contained | None. |

Each operation also carries an `effects` list, written for the person reading the prompt at 3am:
what actually happens, not what the command is called.

---

## 4. The audit trail

Everything the toolkit does is recorded in its own store, and the store is a database you can
query.

| Table | Records |
| --- | --- |
| `job_runs` | Every scheduled command: when it started, how it ended, and what it said. |
| `metric_results` | Every measurement, with its target, metric, status and value. Archived rather than deleted. |
| `sla_runs`, `sla_results` | Every objective evaluation and its verdict. |
| `backup_restore_history` | Every backup and every restore, with what was verified and when. |
| `telegram_send_messages` | Every message queued and its delivery state. |
| `config_items` | The mirror of your configuration files, so a change made through the console is a row with a history. |

Log files under `logs/` carry the same events per scope, with a daily archive.

The reason this is a security property and not just an operations one: **a change made through the
web console or the bot leaves a row, and so does a failure to make it.** An operation nobody can
account for afterwards is a worse problem than an operation nobody was allowed to run.

---

## 5. Running with no outbound network access

Every capability except message delivery works with no route off the machines you monitor.

- **Metrics, reports, SLA validation, backup and restore** connect only to the targets in
  `db_instances.json`.
- **Delivery** is off unless `data/telegram_config.json` sets `"enabled": true`, and it is the only
  thing that reaches a third party.
- **Reports are files.** They are rendered under `runtime/reports`; the web host serves them from
  disk and generates nothing. It can be left off entirely.
- **Installation** needs PyPI once, or a wheel you carry in. The container image needs a registry
  once. Neither is a runtime dependency.
- **The test suite is offline** — no database, no network, no delivery. That is what lets it run
  anywhere, and it is checked rather than promised.

An air-gapped install is therefore the default shape, not a special mode.

---

## 6. Reporting a vulnerability

Do not open a public issue. Follow [`SECURITY.md`](../SECURITY.md).
