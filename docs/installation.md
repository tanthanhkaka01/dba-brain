# Installation

> **Naming, while the project is being renamed.** The distribution and the module path are still
> `db_ops`, so every command below reads `python -m db_ops.<app>.cli`. They become `dbabrain` when
> the code moves to the public repository. Nothing else on this page changes with them.
> <!-- TODO(rename): update the distribution name and the `python -m` paths here once the package
> rename lands. -->

## What you need

| | |
| --- | --- |
| Python | **3.12 or newer.** Measured, not chosen: the CSV writer uses `csv.QUOTE_STRINGS`, which arrived in 3.12 and is what keeps a NULL and an empty string apart in an export. The floor was 3.11 until the test matrix ran on it and disagreed. |
| A database to point it at | Any supported engine you can reach and log in to. Nothing else has to be installed on the monitored machine — there is no agent. |
| A database driver | One extra per engine you monitor. See [Engine extras](#engine-extras). |
| Disk | Small. The toolkit's own store is a SQLite file until you move it to PostgreSQL. |

The toolkit runs on Linux, Windows and macOS. It reaches Windows targets from Linux and Linux
targets from Windows; the transport is a per-target setting, not a property of where you installed
it.

## Install with pip

```bash
python -m venv .venv
.venv/bin/pip install 'dbabrain[postgres]'       # Windows: .venv\Scripts\pip
```

From a checkout:

```bash
git clone https://github.com/dba_userkaka01/dba-brain.git
cd dba-brain
python -m venv .venv
.venv/bin/pip install -e '.[postgres,dev]'
```

Check it:

```bash
.venv/bin/python -m db_ops.db.cli --help
```

### Engine extras

The core install pulls two libraries and no database driver at all: `cryptography` for the
encrypted secret store, and `PyYAML` for reading legacy inventory files. **A DBA who runs only
PostgreSQL must not be made to install an ODBC driver and an Oracle client to start**, which is
why every driver is an extra.

| Extra | Installs | For |
| --- | --- | --- |
| `[postgres]` | `pg8000` | PostgreSQL. Pure Python — nothing else to install. |
| `[mysql]` | `PyMySQL` | MySQL and MariaDB. Pure Python. |
| `[oracle]` | `oracledb` | Oracle. Needs no client library for Oracle Database 12.1 and newer. |
| `[mssql]` | `pyodbc`, `pymssql` | SQL Server. **Also needs a system ODBC driver** — see below. |
| `[ssh]` | `paramiko` | OS-level metrics and backup scripts on Linux targets. |
| `[winrm]` | `pypsrp` | OS-level metrics and maintenance on Windows targets. |
| `[all]` | all of the above | |
| `[dev]` | `pytest` | Running the test suite. |

Combine them: `pip install 'dbabrain[postgres,mssql,ssh]'`.

## The two prerequisites that bite

Everything else installs from PyPI. These two do not.

### SQL Server: the ODBC driver is a system package

`pyodbc` is a binding, not a driver. Without Microsoft's ODBC Driver for SQL Server installed on
the machine running the toolkit, every SQL Server connection fails with a driver-not-found error
that reads like a Python problem and is not one.

- **Debian / Ubuntu** — add Microsoft's package repository, then
  `apt-get install msodbcsql18 unixodbc`.
- **RHEL / Oracle Linux** — the same repository, `dnf install msodbcsql18 unixODBC`.
- **Windows** — the *ODBC Driver 18 for SQL Server* installer from Microsoft.
- **macOS** — `brew install msodbcsql18` from Microsoft's tap.

**Keep driver 17 alongside 18 if you monitor SQL Server 2008 R2 or older.** Driver 18 defaults to
requiring an encrypted connection with a verifiable certificate; instances that old offer neither,
and refuse. The shipped container image installs both for exactly this reason, and additionally
lowers the OpenSSL minimum protocol so those instances can negotiate at all — a host-level setting
you will have to make yourself on a machine that is not the container.

### Oracle: a client library, but only for old servers

`oracledb` runs in **thin mode** by default and speaks to Oracle Database 12.1 and newer with no
Oracle software installed. That covers most estates and needs nothing on this page.

You need **Oracle Instant Client** (thick mode) only for a server older than 12.1, or for the few
features thin mode does not implement. Install it, and make sure the process can find it —
`LD_LIBRARY_PATH` on Linux, `PATH` on Windows.

Oracle **8i** and other releases no modern client can connect to at all are handled differently:
the target declares `sql_access.method: "api"` in `data/db_instances.json` and the query is
forwarded to a small bridge process running beside a client that can. See
[`docs/13_common.md`](./13_common.md).

## Run from a container

The shipped image is Ubuntu with everything the awkward targets need already in it: both ODBC
drivers, PowerShell for Windows targets, `openssh-client`, `rsync` and `smbclient` for copying
backups, and the Docker client for provisioning lab databases.

```bash
docker build -t db_ops:local .
```

Configuration and secrets are **not** baked in. The image carries the code; `config.json`, `data/`
and `assets/` are mounted at run time, and the secret passphrase is supplied on the command line
or in the environment. That is what lets one image serve several estates, and it is why an image
someone else built is not a copy of your credentials.

```bash
docker run -d --name db_ops_daemon \
  -v "$PWD/config.json:/app/tools/db_ops/config.json:ro" \
  -v "$PWD/data:/app/tools/db_ops/data" \
  -v "$PWD/logs:/app/tools/db_ops/logs" \
  -v "$PWD/runtime:/app/tools/db_ops/runtime" \
  -e DB_OPS_NODE_ROLE=worker \
  -e DB_OPS_SECRET_KEY='<passphrase>' \
  db_ops:local daemon
```

**It runs as uid 10001, not root.** A monitoring daemon reads databases over the network and
writes logs; it needs no privilege in the container. The one thing that follows is the mounts: they
arrive owned by whoever created them on the host, so either give that user the directories or run
as root —

```bash
chown -R 10001:10001 data logs runtime     # the directories the container writes to
docker run --user 0:0 ...                  # or keep the old behaviour, in one flag
```

An unwritable mount is reported by name before anything starts, rather than surfacing later as a
traceback from whichever component wrote first.

**`db-ops` is on the PATH inside the image**, and the package is installed rather than only copied,
so every command in this documentation works from any directory in the container — not only from
the one the daemon happens to start in.

The image lays the project out under `/app/tools/db_ops`, which is why `working_dir` in
`data/app_commands.json` is the string `tools/db_ops` — a logical alias for the tool root that
resolves to the checkout on a workstation and to that path in the container, so one configuration
file works in both.

## First run

The five-minute version, against a throwaway PostgreSQL container, is
[`examples/postgres-quickstart/`](../examples/postgres-quickstart). Against your own estate:

1. **Create a tool root.** `db-ops init` writes one — twelve files, and it prints the next six
   commands. Four of them arrive complete because they are product data rather than your estate:
   the 90-metric catalogue, the report definitions, the bot commands, and the daemon's schedule. A directory holding `data/` or `config.json` *is* a tool root: stand in it, or point
   `DB_OPS_HOME` at it. Do not build one by hand.
2. **Declare one target and one credential.** `data/db_instances.json` and `data/users.json`.
3. **Put the password in the secret store.** Write `secrets/secret_text.json`, then
   `db-ops encrypt-secret --key-base64 <base64-passphrase>`.
4. **Check it resolves before it connects.** `db-ops check-credentials`, then
   `db-ops metrics collect --dry-run`.
5. **Collect.** `db-ops metrics --key-base64 <base64-passphrase> collect` — the key goes *before*
   the subcommand.
6. **Only then schedule anything.** `data/app_commands.json` is already written; trim it to the
   apps you actually run, then start the daemon. It runs the same commands you just ran by hand,
   with its own interpreter — so a venv install schedules the Python it was installed into.

**Step by step, with what to assert after each one: [`first_run.md`](./first_run.md).**

Steps 3 to 5 are the loop worth repeating per target. A scheduler started before a manual run has
succeeded turns a configuration mistake into an intermittent one.

See [`docs/configuration.md`](./configuration.md) for what each file decides, and
[`docs/security.md`](./security.md) for what the toolkit needs on a monitored instance and what it
sends anywhere.

## Upgrading

```bash
pip install --upgrade db_ops
python -m db_ops.db.cli --config config.json init      # idempotent; applies any schema change
```

`init` is safe to run against an existing store: it creates what is missing and upgrades what is
out of date. Your `data/*.json` is yours and is never rewritten by an upgrade.

## Uninstalling

`pip uninstall db_ops` removes the code. Your tool root — configuration, logs, the store — is a
directory you created and is left alone; delete it if you want it gone. Nothing is installed on
the monitored databases, so there is nothing to remove there.
