# Examples

Worked configurations you can copy whole. A **quickstart directory** is a complete **tool root** —
a `config.json` and a `data/` folder — so you can stand in it and run the toolkit against it without
touching anything else. A **file** here is a walkthrough instead: commands to run against a root of
your own, in order; [`docker-node/`](./docker-node) is one too, with the compose file it runs beside
it. And [`showcase/`](./showcase) is neither: it is output, not input — the pages a running estate
actually publishes.

| Example | What it shows |
| --- | --- |
| [`telegram-commands.md`](./telegram-commands.md) | What the bot can be asked, with the answers it actually gives: the node's own state and published URLs, the targets, the scheduled SQL and its history, the backup and restore ids, your own command history — each with the clearance it needs and what its answer is good for. Includes why a secret must never be typed into a command line. |
| [`docker-node/`](./docker-node) | **The same node in Docker, and how it is upgraded.** Pull the published image, `init` a tool root through it, hand the container the folders it writes, start it with the [`docker-compose.yml`](./docker-node/docker-compose.yml) beside the walkthrough — then upgrade it to the next release (back up, keep the old image, `upgrade-config` with the new one while stopped) and roll it back. Written from a rehearsal on a lab host: the install on 0.23.0 and on 0.24.0, the upgrade between them, a rollback and the upgrade again. Every line of the compose file says why it is there — the fixed `hostname` measured, the rest learned from a production worker. |
| [`standing-up-a-node.md`](./standing-up-a-node.md) | **The whole path, start to finish:** `pip install` → `init` → prove every scheduled app runs with nothing configured → start the daemon → store the bot token and level your groups → register the first database → OS metrics and gateway targets → the pages it publishes. Written from a node actually stood up this way, with what each step proves, and a list of what bites people. **Read this if you are new.** |
| [`an-estate-by-command-on-a-new-machine.md`](./an-estate-by-command-on-a-new-machine.md) | **A whole estate moved onto a new machine, one command per item:** 105 secrets, 9 Telegram groups, 64 instances, 28 host logins, SQL tasks, 47 backups and 30 restore drills, the schedule, then the checks and the clock. Nothing imported, no config file opened in an editor. Written from a real move to a brand-new PC with `pip install dbabrain`, carried out by an AI agent from a plan of one block per item: what each step measured, what the first 45 minutes looked like, why PowerShell 5.1 needs the JSON on stdin, and what bites people on a move. |
| [`lab-create-backup-restore.md`](./lab-create-backup-restore.md) | **A lab database, backed up and restored, for every engine.** `dbabrain sre create-db-docker` builds a throwaway SQL Server, PostgreSQL or Oracle in Docker on two machines. Then register it, back it up, restore it onto the other machine, and restore it to a moment in the past. Written from the 0.23.0 release drill, with the timings it measured. Also covers what Oracle XE and MySQL can and cannot do, and what bites people. |
| [`lab-sql-tasks.md`](./lab-sql-tasks.md) | **Scheduled SQL on the lab databases, all three engines.** A 10-, a 5- and a 1-minute task on SQL Server, PostgreSQL and Oracle, each due again 10 s after it starts, so they overlap: how each engine splits a script, registering with `sql-command-add` and `sql-target-add`, what `max_parallel` does to nine long tasks (4 against 10, measured), the messages, and what a daemon restart leaves behind. Written from the 0.23.0 release drill. |
| [`showcase/`](./showcase) | **What the output actually looks like.** The four report pages a live estate publishes — fleet inventory, server metrics, per-server index usage, SLA — captured whole, with every name that belongs to that estate replaced by a stable fake one. Real fleets, real fragmentation, real verdicts; nothing invented and nothing traceable. Open `*_index.html` and follow the links. Serve the folder or open the published site — a repository shows HTML as source, and the fleet page fetches its data per server. |
| [`postgres-quickstart/`](./postgres-quickstart) | The smallest configuration that does real work: one throwaway PostgreSQL container, a least-privilege monitoring login, seven metrics collected into a local SQLite store. No message delivery, no scheduler, nothing to uninstall. **Start here** — it needs no system packages, because the PostgreSQL driver is pure Python. |
| [`sqlserver-quickstart/`](./sqlserver-quickstart) | The same shape against the engine most of the metric catalogue is written for, and it ends somewhere more interesting: the collection finds a **real problem** with the instance, you fix it, and the finding clears. Also shows the three things SQL Server does differently — `service_name` is a label and never a database, the engine version picks the query, and severity is graded by the target's environment. Needs Microsoft's ODBC driver. |

## How a tool root is found

The toolkit does **not** look for its configuration next to its own code. It asks, in order:

1. `DB_OPS_HOME`, if set;
2. the current working directory, if it holds `data/` or `config.json`;
3. the package location, as the fallback that keeps a source checkout and the container working.

`DB_OPS_DATA_DIR` moves the data folder on its own, for an installed copy whose configuration
lives where the operator keeps configuration rather than beside the code.

That is why an example directory works by being *stood in*:

```bash
cd examples/postgres-quickstart
python -m db_ops.metrics.cli --config config.json collect --dry-run
```

Full detail in [`docs/configuration.md`](../docs/configuration.md).

## Placeholders

Every address in `data/*.example.json` and in these examples comes from the ranges reserved for
documentation — `192.0.2.x`, `198.51.100.x`, `203.0.113.x` (RFC 5737) and `example.com`
(RFC 2606) — so an example host can never be somebody's real machine. `127.0.0.1` appears only
where the value is genuinely loopback, as it is for a container running on your own laptop.
