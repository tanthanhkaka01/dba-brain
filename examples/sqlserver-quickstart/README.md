# Quickstart: one SQL Server container, seven metrics, one real finding

The same shape as [`postgres-quickstart`](../postgres-quickstart), against the engine most of this
toolkit's metric catalogue is written for. It ends somewhere more interesting: the collection finds
a genuine problem with the instance, you fix the problem, and the finding clears.

Nothing here touches a machine you own. The database is a container on loopback, the store is a
SQLite file inside this directory, and `docker compose down -v` plus deleting this directory
removes all of it.

**The toolkit sends nothing anywhere until you give it a token.** Steps 1–5 make no outbound
connection at all except pulling the container image — not because a flag says so, but because the
secret store holds no bot token and there is nothing to authenticate with. Step 6 is the optional
step that changes that, and it is the only one that talks to anything off this machine.

---

## Before you start

- **Docker**, with roughly 2 GB free for the container.
- **Python 3.12+**, with the toolkit installed and the SQL Server driver:

  ```bash
  pip install -e '.[mssql]'          # from the repository root
  ```

- **Microsoft's ODBC driver**, which is a system package and not a Python one. `pyodbc` is a
  binding, not a driver, and without it every connection fails with a driver-not-found error that
  reads like a Python problem and is not one. See
  [`docs/installation.md`](../../docs/installation.md).

Run every command **from this directory** — that is what makes it the tool root:

```bash
cd examples/sqlserver-quickstart
```

## 1. Start the database

```bash
docker compose up -d
```

Developer edition is free for non-production use and is the full engine — the same dynamic
management views the metric SQL reads in production, not a reduced Express feature set. It takes
about a minute to come up:

```bash
docker inspect -f '{{.State.Health.Status}}' dbops_quickstart_mssql
```

## 2. Create the monitoring login

SQL Server has no equivalent of the postgres image's `initdb` directory, so this is one visible
command rather than a mounted script — read [`setup/01_monitor_login.sql`](./setup/01_monitor_login.sql)
before you run it:

```bash
docker exec -i dbops_quickstart_mssql /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P 'Quickstart_not_a_real_password_1' -C -b \
  -i /dev/stdin < setup/01_monitor_login.sql
```

> **On Git Bash for Windows**, prefix the command with `MSYS_NO_PATHCONV=1`. Git Bash rewrites
> `/opt/...` into a Windows path before Docker sees it and the exec fails with a confusing
> `no such file or directory` naming a path you never typed.

It creates `monitor_user` with **two server-level permissions and no server role**:

| Permission | For |
| --- | --- |
| `VIEW SERVER STATE` | the `sys.dm_*` views — sessions, requests, waits, file space |
| `VIEW ANY DEFINITION` | the catalog views — database options, configuration, principals |
| `db_datareader` in `msdb` | `BACKUP_AGE` reads `msdb.dbo.backupset` |

Neither permission can change anything. `sysadmin` is the reflex to avoid here: it works
immediately, and then nobody ever narrows it.

The script also creates a user database, `APPDB`, so the per-database metrics have something to
report on besides the system databases.

## 3. Store, secret, collect

```bash
python -m db_ops.db.cli --config config.json init

cp secrets/secret_text.example.json secrets/secret_text.json
python -m db_ops.cli encrypt-secret --key-base64 "cXVpY2tzdGFydA=="

export DB_OPS_SECRET_KEY=quickstart     # PowerShell: $env:DB_OPS_SECRET_KEY="quickstart"
python -m db_ops.metrics.cli --config config.json collect --dry-run
python -m db_ops.metrics.cli --config config.json collect
```

```text
METRIC run_id=1 … metric=INSTANCE_STATUS          status=OK      rows=1 inserted=1
METRIC run_id=1 … metric=DATABASE_STATUS          status=OK      rows=1 inserted=1
METRIC run_id=1 … metric=INSTANCE_CONNECTIONS     status=OK      rows=1 inserted=1
METRIC run_id=1 … metric=QUERY_LONG_RUNNING       status=OK      rows=1 inserted=1
METRIC run_id=1 … metric=BACKUP_AGE               status=WARNING rows=1 inserted=1
METRIC run_id=1 … metric=STORAGE_DATA_FILE_SPACE  status=OK      rows=1 inserted=1
METRIC run_id=1 … metric=LOCK_BLOCKING_SESSIONS   status=OK      rows=1 inserted=1

ok_count: 6
warning_count: 1
```

Step 3 of the postgres quickstart explains the secret store in more detail; it works identically
here.

## 4. Read the finding

```bash
python -m db_ops.metrics.cli --config config.json summary-latest
```

```text
[WARNING]
- 127.0.0.1 / Backup age / No full or differential backup found for database APPDB.

[OK]
- 6 metrics OK
```

**That warning is correct.** The container has never been backed up, and the toolkit noticed
without being told what to look for — `BACKUP_AGE` reports the age, and `backup_policy.json`
decides what age is acceptable. Neither of those numbers is in any Python file.

`report` shows the same run with the SQL file behind each answer — the measurement and its
provenance in one table:

```bash
python -m db_ops.metrics.cli --config config.json report
```

```text
| 1 | INSTANCE_STATUS         | sqlserver/001_sqlserver_instance_status.sql   | ONLINE                          |
| 5 | BACKUP_AGE              | sqlserver/005_sqlserver_backup_age.sql        | WARNING: APPDB: no backup       |
| 6 | STORAGE_DATA_FILE_SPACE | sqlserver/006_sqlserver_data_file_space.sql   | APPDB:APPDB: 32.81 pct          |
```

## 5. Fix it, and watch the finding clear

```bash
docker exec dbops_quickstart_mssql /opt/mssql-tools18/bin/sqlcmd \
  -S localhost -U sa -P 'Quickstart_not_a_real_password_1' -C \
  -Q "BACKUP DATABASE APPDB TO DISK='/var/opt/mssql/data/APPDB.bak' WITH INIT, COMPRESSION"

python -m db_ops.metrics.cli --config config.json collect --metric-code BACKUP_AGE --force
```

```text
METRIC run_id=2 … metric=BACKUP_AGE status=OK rows=1 inserted=1
warning_count: 0
```

`--force` means *never mind the interval* — `BACKUP_AGE` runs hourly and you have not waited an
hour. It deliberately does **not** mean *never mind the window*: metrics confined to night hours
stay confined, because those are windowed precisely to keep them off a production instance in the
daytime. That needs `--include-windowed`, and on a production instance you should mean it.

## 6. Send it to Telegram

Optional, and the only step that talks to anything outside this machine. Everything above works
with delivery off; this is what it takes to turn it on, and it is three files.

**Get a bot.** Message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, and keep the
token it gives you. Then send your new bot any message — a bot cannot open a conversation, so
until you speak to it first there is nowhere for it to reply.

**1. Put the token in the secret store**, the same way the database password went in at step 3:

```bash
# secrets/secret_text.json — add the second entry
{
  "MSSQL_QUICKSTART_MONITOR_USER": "Quickstart_not_a_real_password_1",
  "QUICKSTART_TELEGRAM_BOT_TOKEN": "<the token BotFather gave you>"
}
```

```bash
python -m db_ops.cli encrypt-secret --key-base64 "cXVpY2tzdGFydA=="
```

The token never goes in a file under `data/`. `data/bot_telegram.json` names it — that is all it
does. A bot token is a bearer credential: whoever holds it can read every chat the bot is in and
post as it.

**2. Find your chat id** and put it in `data/telegram_groups.json`:

```bash
python -m db_ops.telegram.cli --config config.json get-updates
```

The reply carries `"chat": {"id": ...}`. It is **positive for a direct message and negative for a
group**. Replace both placeholder `group_id` values with it — one chat covers the `warning` and
`logging` levels here; in a real estate they are separate chats, because `logging` is the record
and `warning` is the one somebody reads.

**3. Turn delivery on** in `data/telegram_config.json`:

```json
"enabled": true,
```

Then build the alert out of what was collected, and send it. Collection has already happened, so
this is two commands:

```bash
python -m db_ops.metrics.cli  --config config.json alert-summary --include-warning
python -m db_ops.telegram.cli --config config.json send-message --chat-id <your chat id> --text "<the summary>"
```

It reads the **stored results** rather than re-querying anything, so it needs no passphrase, costs
the instance nothing, and says exactly what the last collection found:

```text
Target: QUICKSTART / 127.0.0.1
Metric: BACKUP_AGE
Status: WARNING
Importance: 5
Message: No full or differential backup found for database APPDB.
```

What arrives in the chat is the finding itself, not a notification that something happened — which
is the whole point: an alert you can act on without opening anything else.

> **The scheduled path is `v0.2.0`.** `queue-metrics-reports` and `send-queue` — the version that
> dedupes, splits long messages, routes by severity level and runs unattended — live in the
> `reports` app, which this release does not ship. `No module named 'db_ops.reports'` is that, not
> a broken install.

### Three ways this fails, and what each one says

| What you see | What it means |
| --- | --- |
| `Telegram bot token ref 'QUICKSTART_TELEGRAM_BOT_TOKEN' was not found in secret text` | Step 1 was skipped, or `encrypt-secret` was not re-run after editing the plaintext file |
| `bot token is empty` | The token is in the store but `data/telegram_config.json` does not name its reference |
| `Bad Request: chat not found` | The chat id is wrong, or you have not messaged the bot yet — a bot cannot open a conversation, so until you speak to it there is nowhere to reply |

## Clean up

```bash
docker compose down -v
```

Then delete `runtime/`, `logs/`, `data/encrypted_secret_text.json` and `secrets/secret_text.json`,
or the whole directory. Those four are git-ignored — including the plaintext secret file, which is
the one that would otherwise carry your bot token into a commit.

---

## What this example shows that the PostgreSQL one does not

| | |
| --- | --- |
| **`service_name` is a label, not a database** | `data/db_instances.json` sets `"service_name": "QUICKSTART"`, and no database of that name exists. Collection always connects to `master` and the metric SQL issues its own `USE`. Passing the label as the connection database fails *every* SQL Server target at once, which is why the rule is enforced in `db_ops/metrics/executor.py` rather than left to configuration. |
| **A version picks the query** | `"major_version": 16` selects the `min_major_version: 11` variant. The shipped catalogue pairs most SQL Server metrics with a `legacy_2008r2` variant too, so an engine too old to parse the modern query gets one it can instead of failing every cycle. |
| **Severity is data** | `LOCK_BLOCKING_SESSIONS` carries a `report_policy` in `data/metric_definitions.json`: blocking under five seconds is suppressed, and the same finding is graded by the target's `env` — it pages in `prod` and is only logged in `lab`, which is what this target is. No code knows those numbers. |
| **A finding you can act on** | The backup warning is real, specific, and clears when you fix the thing it is about. |

## What to read next

| Question | Where |
| --- | --- |
| The other engines, and the two prerequisites that are not pip-installable | [`docs/installation.md`](../../docs/installation.md) |
| What every configuration file decides | [`docs/configuration.md`](../../docs/configuration.md) |
| The least-privilege login per engine, and what the toolkit sends anywhere | [`docs/security.md`](../../docs/security.md) |
| The full SQL Server metric catalogue | [`docs/04_metrics_engine.md`](../../docs/04_metrics_engine.md) |
