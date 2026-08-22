# Quickstart: one PostgreSQL container, seven metrics

The smallest configuration that does real work. At the end you will have collected seven real
measurements from a real database engine into a local store, and you will have seen where every
decision that produced them is written down.

Nothing here touches a machine you own. The database is a container on loopback, the store is a
SQLite file inside this directory, and `docker compose down -v` plus deleting this directory
removes all of it.

**The toolkit sends nothing anywhere until you give it a token.** Every step below runs with no
outbound network access except pulling the container image — not because a flag says so, but
because the secret store holds no bot token and there is nothing to authenticate with. The SQL
Server quickstart's [step 6](../sqlserver-quickstart/README.md#6-send-it-to-telegram) is what
turning delivery on actually takes.

---

## Before you start

- Docker, for the throwaway PostgreSQL.
- Python 3.11 or newer, with the toolkit installed and the PostgreSQL driver:

  ```bash
  pip install -e '.[postgres]'        # from the repository root
  ```

  The `postgres` extra is `pg8000`, a pure-Python driver — no client library to install. See
  [`docs/installation.md`](../../docs/installation.md).

Run every command below **from this directory**. That is not incidental: the toolkit finds its
configuration by asking `DB_OPS_HOME`, then the working directory if it holds `data/` or
`config.json`, then the package location. Standing here is what makes this directory the tool
root, so `logs/`, `runtime/` and the store all appear here and nowhere else.

```bash
cd examples/postgres-quickstart
```

## 1. Start the database

```bash
docker compose up -d
```

It creates a `monitor_user` login with the `pg_monitor` role and nothing else
([`initdb/01_monitor_user.sql`](./initdb/01_monitor_user.sql)) — the least privilege every metric
in this example needs, and no privilege to change the instance it is measuring.

Wait for it to report healthy:

```bash
docker inspect -f '{{.State.Health.Status}}' dbops_quickstart_pg
```

## 2. Create the store

```bash
python -m db_ops.db.cli --config config.json init
```

That is the toolkit's own database — job runs, metric results, reports, history. It is SQLite here
(`data/store_config.json`), which is why this step needs nothing installed. The same file already
carries a filled-in PostgreSQL section: switching a real installation over is a one-word edit of
`backend`, not a rediscovery of host, port and credentials.

## 3. Put the password in the secret store

Passwords are never written in configuration. `data/users.json` names a *reference*,
`POSTGRES_QUICKSTART_MONITOR_USER`, and the value lives encrypted in
`data/encrypted_secret_text.json`.

```bash
cp secrets/secret_text.example.json secrets/secret_text.json
python -m db_ops.cli encrypt-secret --key-base64 "cXVpY2tzdGFydA=="
```

- `secrets/secret_text.json` is the plaintext source and is git-ignored. Only the encrypted file
  is safe to keep.
- The passphrase here is the word `quickstart`, base64-encoded so shell quoting cannot mangle it.
  Base64 is not encryption — it only avoids `$` and `#` being eaten by a shell. The encrypted file
  is exactly as safe as the passphrase behind it.
- The passphrase is supplied at run time and never stored. Every command that reaches a database
  needs it, as `DB_OPS_SECRET_KEY` or as `--key` / `--key-base64`.

## 4. Collect

Look first, run second:

```bash
export DB_OPS_SECRET_KEY=quickstart          # Windows PowerShell: $env:DB_OPS_SECRET_KEY="quickstart"

python -m db_ops.metrics.cli --config config.json collect --dry-run
python -m db_ops.metrics.cli --config config.json collect
```

```text
METRIC run_id=1 target=QUICKSTART-PG-5432/postgresql/QUICKSTART metric=INSTANCE_STATUS status=OK rows=1 inserted=1
METRIC run_id=1 target=QUICKSTART-PG-5432/postgresql/QUICKSTART metric=DATABASE_STATUS status=OK rows=2 inserted=2
...
result_count: 11
ok_count: 11
```

`--dry-run` is worth the habit. It names every metric that would run against every target without
opening a connection, which is how you find out that a target resolves to the wrong instance
*before* it does.

## 5. Read what you collected

```bash
python -m db_ops.metrics.cli --config config.json report
python -m db_ops.metrics.cli --config config.json summary-latest
```

`report` prints one row per metric with the SQL file behind it — the answer and its provenance in
the same table. `summary-latest` collapses the same data to what is critical, what is a warning,
and what is fine.

## 6. Read what it found

```bash
python -m db_ops.metrics.cli --key-base64 "cXVpY2tzdGFydA==" alert-summary --include-warning
```

```text
Target: QUICKSTART / 127.0.0.1
Metric: INSTANCE_CONNECTIONS
Status: OK
Importance: 4
Message: active_sessions=1, running_requests=1, blocked_sessions=0
```

`alert-summary` reads the **stored results** rather than re-querying anything, so it costs the
instance nothing. It is also what you would send to a chat — see the SQL Server quickstart's
[step 6](../sqlserver-quickstart/README.md#6-send-it-to-telegram).

> **The scheduled reporting app is `v0.2.0`.** `db_ops.reports.cli run-scheduled` builds and queues
> reports on a schedule; it is not in this release, and `No module named 'db_ops.reports'` is that
> rather than a broken install.

Producing a finding and delivering it are
separate steps, and the second one is optional.

## 7. Let the scheduler do it

`data/app_commands.json` already holds both commands, switched off. Set `"active": true` on the
entries you want and start the daemon:

```bash
python -m db_ops.jobs.daemon --config config.json --delay-seconds 5 --key-base64 "cXVpY2tzdGFydA=="
```

It runs each active entry on its own interval, inside its allowed hours, and forwards the
passphrase to every child process. Stop it with Ctrl-C.

Run the commands by hand first, as steps 4 to 6 do. A scheduler that starts a command you have
never watched succeed only makes the first failure harder to find.

## Clean up

```bash
docker compose down -v
```

Then delete `runtime/`, `logs/`, `data/encrypted_secret_text.json` and `secrets/secret_text.json`,
or the whole directory.

---

## What to read next

Each file in `data/` carries a `notes` array explaining what it decides and why. Then:

| Question | Where |
| --- | --- |
| How do I install this properly, and what do the other engines need? | [`docs/installation.md`](../../docs/installation.md) |
| What is every configuration file for? | [`docs/configuration.md`](../../docs/configuration.md) |
| What does it need on my production instances, and what does it send anywhere? | [`docs/security.md`](../../docs/security.md) |
| How is it put together? | [`docs/architecture.md`](../../docs/architecture.md) |

To point this at something real, three edits do it: an entry in `data/db_instances.json`, a
credential in `data/users.json`, and its password in the secret store. The full example files —
one per configuration file, with every field explained — are `data/*.example.json` at the
repository root.
