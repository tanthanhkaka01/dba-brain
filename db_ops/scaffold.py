"""The first run: turn an empty directory into a working tool root.

**Measured on 2026-08-22, on a real `pip install` into a clean virtualenv, from an empty
directory.** The toolkit resolved its configuration to `site-packages`, and then told the reader:

    Config file not found: .../site-packages/config.json.
    Create it from .../site-packages/config.example.json.

Three things wrong with that, and the third is the one that matters. The path is inside the
install, which nobody should write to. The example it names does not exist there — no example file
ships. And **there was no command to fix it**: the toolkit could be installed and could not be
started, which makes the resolution order in :mod:`db_ops.lib.paths` correct and useless.

`db-ops init` is the missing half. It writes the smallest tree that runs.

**A scaffold is not an example, and this deliberately does not copy `data/*.example.json`.** The
examples are documentation — every field, with `notes` explaining what it decides. The scaffold is
the *least* that is already valid, because what follows it is somebody filling in one database.
Two artifacts, two jobs; copying one into the other would keep them in step by making both worse.

## SQLite, always, on the first run

The store defaults to **SQLite in `runtime/`**, and that is a decision rather than a convenience.
A first run has no PostgreSQL — expecting one means the first thing a new user meets is installing
a database to hold the results of monitoring a database. SQLite needs nothing, and moving to
PostgreSQL later is one edit in `data/store_config.json`, which is why the file exists.

## Two ways in, and the scaffold has to serve both

An operator will eventually add a database through a web console, the way they would in a database
client: host, port, user, password. That console is not in this release.

Right now the way in is **an AI agent editing JSON**, so every file this writes is shaped for that:
valid on its own, with a `notes` array saying what to put in it and what the next step is. An agent
that can read one file and write the next needs no console and no interview — and a human editing
the same JSON gets the same instructions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: The store an install starts with. Rewritten by whoever moves to PostgreSQL later, which is a
#: change of one value plus the block beside it.
SQLITE_STORE = {
    "schema_version": 1,
    "notes": [
        "Where the toolkit keeps its OWN data: metric results, job runs, the delivery queue.",
        "This is not one of the databases you monitor.",
        "'backend' picks which block below is live. It starts on sqlite because a first run has no",
        "PostgreSQL, and needing one to store the results of monitoring is a poor first request.",
        "Move to postgresql when more than one machine writes to the store, or when the history",
        "should outlive this one: fill in the postgresql block and change 'backend' to postgresql.",
    ],
    "backend": "sqlite",
    "sqlite": {
        "path": "runtime/dbabrain.sqlite",
        "connection_string": "sqlite:///runtime/dbabrain.sqlite",
    },
    "postgresql": {
        "host": "",
        "port": 5432,
        "database": "dbabrain",
        "schema": "dbabrain",
        "username": "",
        "password_ref": "",
        "sslmode": "prefer",
        "connect_timeout_seconds": 10,
        "application_name": "dbabrain",
        "connection_string": "",
    },
}

#: The inventory, empty and explaining itself. This is the file the first database goes into, so
#: its notes are the closest thing the toolkit has to an interview.
EMPTY_INVENTORY = {
    "schema_version": 1,
    "notes": [
        "The databases to monitor. Add one object to 'db_instances' and collection will run.",
        "",
        "Minimum for a SQL Server target:",
        "  server_id                a name you choose; it identifies this instance everywhere",
        "  ip, port                 where it listens (SQL Server default port is 1433)",
        "  db_type                  'sqlserver'",
        "  major_version            13=2016, 14=2017, 15=2019, 16=2022. It selects the query variant",
        "  service_name             a LABEL for reports, not a database name. Collection always",
        "                           connects to master and the metric SQL issues its own USE",
        "  default_credential_name  the name of a secret in secrets/secret_text.json",
        "  enabled                  true",
        "",
        "Then put the password in secrets/secret_text.json under that credential name and run",
        "  db-ops encrypt-secret --key-base64 <your passphrase, base64>",
        "",
        "Check it before collecting: db-ops metrics collect --dry-run",
    ],
    "db_instances": [],
}

#: The fallback catalogue, and *only* a fallback — :func:`packaged_catalogue` ships all 90 metrics
#: and is what `init` actually writes. This stays because `init` failing outright is a worse answer
#: than `init` writing three metrics: package data can go missing in ways a wheel test does not
#: cover (a `pip install --no-binary` against a broken sdist, a vendored subset), and a toolkit that
#: still starts and says what it has beats one that cannot start.
STARTER_METRICS = {
    "schema_version": 1,
    "notes": [
        "Which metrics exist, and which shipped query implements each one.",
        "'file' is relative to the metrics root; the package ships the queries, so these resolve",
        "with nothing else installed. Your own copy at assets/metrics/<same path> wins per file.",
        "This is a starter set. The full catalogue is much larger and arrives with the docs.",
    ],
    "collection": {"max_parallel_servers": 1},
    "metrics": [
        {
            "metric_id": 1,
            "metric_code": "INSTANCE_STATUS",
            "db_type": "multi",
            "category": "availability",
            "default_importance": 5,
            "active": True,
            "collector_type": "sql",
            "connection_error_severity": "CRITICAL",
            "execution_error_severity": "CRITICAL",
            "time_window": {"repeat_interval": 300, "timeout": 60},
            "variants": [
                {"db_type": "sqlserver", "min_major_version": 11,
                 "name": "sqlserver_2012_plus",
                 "file": "sqlserver/001_sqlserver_instance_status.sql"},
            ],
        },
        {
            "metric_id": 2,
            "metric_code": "BACKUP_AGE",
            "db_type": "multi",
            "category": "backup",
            "default_importance": 5,
            "active": True,
            "collector_type": "sql",
            "connection_error_severity": "WARNING",
            "execution_error_severity": "WARNING",
            "time_window": {"repeat_interval": 3600, "timeout": 120},
            "variants": [
                {"db_type": "sqlserver", "min_major_version": 11,
                 "name": "sqlserver_modern_2012_plus",
                 "file": "sqlserver/005_sqlserver_backup_age.sql"},
            ],
        },
        {
            "metric_id": 3,
            "metric_code": "DATABASE_STATUS",
            "db_type": "multi",
            "category": "availability",
            "default_importance": 4,
            "active": True,
            "collector_type": "sql",
            "connection_error_severity": "CRITICAL",
            "execution_error_severity": "WARNING",
            "time_window": {"repeat_interval": 300, "timeout": 60},
            "variants": [
                {"db_type": "sqlserver", "min_major_version": 11,
                 "name": "sqlserver_2012_plus",
                 "file": "sqlserver/002_sqlserver_database_status.sql"},
            ],
        },
    ],
}

#: Telegram, off until a token exists. Written anyway so the file an agent has to edit is present
#: and self-describing rather than absent and undiscoverable.
TELEGRAM_CONFIG = {
    "schema_version": 1,
    "notes": [
        "Alert delivery. Leave 'enabled' false until you have a bot token.",
        "",
        "To turn it on:",
        "  1. Create a bot with @BotFather and copy the token",
        "  2. Put it in secrets/secret_text.json as TELEGRAM_BOT_TOKEN",
        "  3. Add your chat to data/telegram_groups.json with a notify_level",
        "  4. Set 'enabled' to true here",
        "",
        "level_chat_map routes a severity to a chat. Anything not mapped falls back to 'private'.",
    ],
    "enabled": False,
    # Three ways in, tried in this order: the environment variable named by `bot_token_env`, then
    # the secret store under `telegram_bot_token_ref`, then the literal `bot_token`. The middle one
    # is the one to use, and the scaffold missed it until a real send failed with "bot token is
    # empty" — a message that names the symptom and not the missing field.
    "bot_token_env": "TELEGRAM_BOT_TOKEN",
    "telegram_bot_token_ref": "TELEGRAM_BOT_TOKEN",
    "secret_text_file": "data/secret_text.json",
    "bot_token": "",
    "api_url": "https://api.telegram.org",
    "timeout_seconds": 20,
    "groups_file": "data/telegram_groups.json",
    "level_chat_map": {},
}

#: The login for each monitored instance. Separate from the inventory on purpose: an instance is a
#: machine and a credential is an account, and one account often serves several machines.
#:
#: Written by `init` because **a credential that is not here can never resolve**, and the failure
#: says "no credential" rather than "you have no users.json". Found by pointing the first version of
#: this scaffold at a real SQL Server, which is the only thing that would have found it.
EMPTY_USERS = {
    "schema_version": 1,
    "notes": [
        "Logins, grouped by the instance they belong to.",
        "",
        "For each entry in data/db_instances.json add one group here with the same server_id:",
        "  {",
        '    "server_id": "MYLAB-SQL01", "db_type": "sqlserver",',
        '    "credentials": [',
        '      {"credential_name": "MSSQL_MYLAB_MONITOR",',
        '       "username": "monitor_user",',
        '       "password_ref": "MSSQL_MYLAB_MONITOR",',
        '       "role": "monitor"}',
        "    ]",
        "  }",
        "",
        "credential_name is what db_instances.json points at with default_credential_name.",
        "password_ref names the secret; the value itself lives in secrets/secret_text.json and is",
        "read from data/encrypted_secret_text.json after you run encrypt-secret-text.",
    ],
    "database_credentials": [],
    "remote_credentials": [],
    "monitor_users": [],
}

EMPTY_TELEGRAM_GROUPS = {
    "schema_version": 1,
    "notes": [
        "Chats that receive alerts. Each entry needs group_id (the numeric chat id) and",
        "notify_level: logging, warning, critical, error, test or private.",
    ],
    "telegram_groups": [],
}

#: What the SQL task runner executes, and where. Both are **empty**, and both have to exist: the
#: runner opens them before it decides it has nothing to do, so their absence is a crash rather
#: than a quiet no-op. They hold this estate's scheduled SQL — not product data — so they scaffold
#: empty rather than from a packaged default, the way the inventory and the credentials do.
EMPTY_SQL_COMMANDS = {
    "schema_version": 1,
    "notes": [
        "SQL the task runner executes on a schedule. Each entry needs a sql_id, the statement or",
        "a script path, and a time_window saying how often it runs.",
        "",
        "Pair each sql_id with one or more rows in sql_targets.json - the command says what to",
        "run, the target says where.",
    ],
    "sql_commands": [],
}

EMPTY_SQL_TARGETS = {
    "schema_version": 1,
    "notes": [
        "Where each sql_command runs: sql_id + target_no, pointing at a server_id from",
        "db_instances.json. One command can have several targets.",
    ],
    "sql_targets": [],
}

#: The plaintext side of the secret store. Never committed, and `.gitignore` already anticipates
#: the path — the encrypted file beside it is what the toolkit actually reads.
SECRET_TEXT = {
    # A FLAT {ref: secret} object - `encrypt_secret_text_file` treats every top-level key as a
    # secret name. The first version of this scaffold wrapped the values in a "secrets" object with
    # a "notes" list beside it, and encryption dutifully produced two secrets called `notes` and
    # `secrets`. Nothing failed: collection then reported "Password ref not found", which points at
    # the inventory rather than at the file that was actually wrong.
    #
    # `_notes` survives only because it starts with an underscore, which the loader ignores.
    "_notes": [
        "Plaintext secrets, one per key: \"REF_NAME\": \"the secret\".",
        "THIS FILE IS NOT READ AT RUN TIME and must never be committed - it is the source you",
        "encrypt from. Every key here is a name that data/users.json points at with password_ref.",
        "",
        "After editing, run:",
        "  db-ops encrypt-secret --key-base64 <your passphrase, base64>",
        "",
        "which writes data/encrypted_secret_text.json, and that is what the toolkit reads.",
        "Keep the passphrase somewhere you will still have it; nothing else can decrypt the store.",
    ],
}

#: Written into the tool root, beside the JSON it describes.
#:
#: Not repository documentation — generated output, and that placement is the point. An agent that
#: has just run `init` is standing in this directory; a guide in a repository it never cloned is a
#: guide it will not read. The same file serves a person, because the instructions are the same
#: instructions and the only difference is who is typing.
AGENTS_GUIDE = """# Running this toolkit for the first time

This directory is a **tool root**: configuration plus the results of monitoring. `db-ops` reads it
because you are standing in it, so run every command below from here.

There are two ways to configure it. Both end at the same JSON.

| | |
| --- | --- |
| **A person** | a web console, adding a database the way a database client does - host, port, user, password. **Not in this release.** |
| **An AI agent, or a person editing files** | write the JSON described below. This is the supported path today. |

## What you must supply

Exactly two things: **one database to monitor**, and **its password**.

### 1. The database - `data/db_instances.json`

Add one object to `db_instances`:

```json
{
  "server_id": "MYLAB-SQL01",
  "ip": "192.0.2.50",
  "port": 1433,
  "db_type": "sqlserver",
  "major_version": 16,
  "service_name": "MYLAB",
  "default_credential_name": "MSSQL_MYLAB_MONITOR",
  "enabled": true,
  "env": "lab"
}
```

Three fields are easy to get wrong, and each fails in a way that does not name itself:

- **`service_name` is a label, not a database.** Collection always connects to `master`, and the
  metric SQL issues its own `USE`. Putting a database name here fails every SQL Server target at
  once with `Cannot open database "..." (4060)`.
- **`major_version` selects the query**, not just documentation: 13=2016, 14=2017, 15=2019,
  16=2022. An older engine gets a variant it can parse instead of failing every cycle.
- **`default_credential_name` is a reference**, not a password. It names an entry in the secret
  store; the next step is what puts a value behind it.

### 2. The password - `secrets/secret_text.json`, then encrypt

```json
{ "MSSQL_MYLAB_MONITOR": "the-password" }
```

Then:

```bash
db-ops encrypt-secret --key-base64 <passphrase in base64>
```

That writes `data/encrypted_secret_text.json`, and **that** is the file the toolkit reads.
`secrets/secret_text.json` is never read at run time and must never be committed.

Keep the passphrase. Nothing else can decrypt the store, and there is no recovery.

## Then run it

```bash
db-ops metrics collect --dry-run                              # resolves targets, connects to nothing
db-ops metrics --key-base64 <passphrase in base64> collect    # collects
db-ops metrics summary-latest                                 # reads the result back
```

`--dry-run` is the check worth making first: it proves the instance resolved and the metric
queries were found, without needing the database to be reachable or the password to be right.

## The least privilege that works

The starter metrics need very little. On SQL Server:

```sql
CREATE LOGIN monitor_user WITH PASSWORD = '...';
GRANT VIEW SERVER STATE TO monitor_user;
GRANT VIEW ANY DEFINITION TO monitor_user;
USE msdb; CREATE USER monitor_user FOR LOGIN monitor_user;
ALTER ROLE db_datareader ADD MEMBER monitor_user;   -- BACKUP_AGE reads backupset
```

No server role, and no access to your data.

## Alerts to Telegram

Optional, and off until a token exists.

1. Create a bot with `@BotFather`, copy the token.
2. Add it to `secrets/secret_text.json` as `TELEGRAM_BOT_TOKEN`, and re-run `encrypt-secret-text`.
3. Add the chat to `data/telegram_groups.json` with a `notify_level`.
4. Set `enabled` to `true` in `data/telegram_config.json`.

## Where the results go

**SQLite, in `runtime/`.** Nothing to install: a first run has no PostgreSQL, and needing one to
hold the results of monitoring is a poor first request.

Move later by filling in the `postgresql` block in `data/store_config.json` and changing `backend`
to `postgresql`. The block is already there with every field, which is why the file looks larger
than a first run needs.

## What this release does and does not do

It collects metrics from SQL Server and alerts to Telegram. Backup and restore validation, SLA
checks, scheduled SQL tasks, reports, host provisioning and the web console are **not** in this
release; they arrive later.
"""


def _config(app_name: str) -> dict:
    return {
        "notes": [
            "Runtime paths, and which declaration files to read.",
            "Every relative path here is resolved against this file's directory, so the whole tree",
            "can be moved or copied without editing anything.",
        ],
        "app_name": app_name,
        "log_dir": "logs",
        "runtime_dir": "runtime",
        "console_level": "INFO",
        "file_level": "INFO",
        "store_config_file": "data/store_config.json",
        "telegram_config_file": "data/telegram_config.json",
    }


#: Product data the package ships, and what `init` writes it as.
#:
#: Each sits beside the component that owns it — the house rule for shipped assets — because these
#: describe *what the toolkit can do*, not what one estate monitors. The metric catalogue names the
#: collectors in `db_ops/metrics/collectors/`; the report definitions name the reports `reports`
#: knows how to build; the support commands name the bot commands `telegram` answers; the app
#: commands name the apps `jobs` can schedule and how often.
#:
#: **All four are needed before the daemon does anything**, which is what put them here. Measured
#: on 2026-08-23 in a clean `pip install`: `db-ops init`, one target, then `db-ops daemon` — the
#: metrics command ran and the other two failed every cycle on a missing file, in a child process
#: whose output nobody watches. Every one of those commands worked by hand.
PACKAGED_DEFAULTS: dict[str, str] = {
    "data/metric_definitions.json": "metrics/catalogue/metric_definitions.json",
    "data/reports_config.json": "reports/catalogue/reports_config.json",
    "data/telegram_support_commands.json": "telegram/catalogue/telegram_support_commands.json",
    "data/app_commands.json": "jobs/catalogue/app_commands.json",
    # The two the **web console** needs, and the reason it showed "0 apps" on a fresh install
    # until 2026-08-24. `config_catalog.json` says which files the store may hold and under which
    # app - without it `db sync-config` refuses outright, so nothing reaches the store at all.
    # `webhost_config.json` is the console's own layout: the blocks it draws and which app command
    # each one owns. The console iterates *blocks*, so with that file absent it rendered an empty
    # dashboard however many commands were configured and active.
    "data/config_catalog.json": "db/catalogue/config_catalog.json",
    "data/webhost_config.json": "webhost/catalogue/webhost_config.json",
    # `sla validate` reads this and nothing else writes it, so without it the SLA app is
    # installed and cannot run. The policies are definitions - which SLIs exist, how each
    # grades - not this estate's targets, so they are product data like the rest here.
    "data/sla_policies.json": "sla/catalogue/sla_policies.json",
    # The payload APP-CONTROL passes to `ops-status`. A file rather than inline JSON so it
    # survives both shells the daemon might run under - see the note inside it.
    "data/ops_status_request.json": "db/catalogue/ops_status_request.json",
    # The list every transfer reads first (`db_ops.lib.data_files`). Product data - every name in
    # it is the toolkit's own - and the code falls back to the packaged copy when a tool root has
    # none, so this entry is about giving the operator a file to *edit* rather than about making
    # the tool work. It ships because a manifest nobody can see is one nobody maintains.
    "data/data_files.json": "control/catalogue/data_files.json",
}

PACKAGED_CATALOGUE = Path(__file__).parent / "metrics" / "catalogue" / "metric_definitions.json"


def packaged_catalogue() -> dict:
    """The full metric catalogue the package ships, or the starter set if it is not there.

    **This is the whole reason a new install can collect anything beyond a heartbeat.** Until
    2026-08-23 `init` wrote three hand-written SQL Server metrics, so the 90 metrics in this
    repository — every OS metric, every Oracle, MySQL and PostgreSQL metric, every Docker metric —
    existed for whoever cloned the repository and for nobody who installed the package. The
    collectors were already shipping; nothing named them.

    The OS metrics matter most here and are the reason this was worth changing. They need no
    database at all — they read CPU, memory, disk and uptime over `cmd_access` — so they are the
    part of the catalogue that works on a host the toolkit cannot log into yet. A target with no
    `cmd_access` skips them with a line saying exactly that, so shipping them switched on costs a
    reader nothing and shows them the capability exists.
    """
    return packaged_default("data/metric_definitions.json") or STARTER_METRICS


def packaged_default(written_as: str) -> dict | None:
    """The shipped default for *written_as*, or ``None`` when the package does not carry it.

    ``None`` rather than an exception: a missing packaged file should cost one config file, not
    the whole `init`. The caller decides whether it has a fallback worth using.
    """
    relative = PACKAGED_DEFAULTS.get(written_as)
    if not relative:
        return None
    try:
        return json.loads((Path(__file__).parent / relative).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


#: What `init` writes, as (relative path, content). Directories come from the paths.
def _files(app_name: str) -> list[tuple[str, dict]]:
    return [
        ("config.json", _config(app_name)),
        ("data/store_config.json", SQLITE_STORE),
        ("data/db_instances.json", EMPTY_INVENTORY),
        ("data/metric_definitions.json", packaged_catalogue()),
        # The three the daemon needs and nothing else writes. Skipped rather than failed if the
        # package did not carry one - a first run without scheduled reports still collects.
        *(
            (name, content)
            for name in ("data/reports_config.json", "data/telegram_support_commands.json",
                         "data/app_commands.json", "data/config_catalog.json",
                         "data/data_files.json",
                         "data/webhost_config.json", "data/sla_policies.json",
                         "data/ops_status_request.json")
            if (content := packaged_default(name)) is not None
        ),
        ("data/users.json", EMPTY_USERS),
        ("data/telegram_config.json", TELEGRAM_CONFIG),
        ("data/telegram_groups.json", EMPTY_TELEGRAM_GROUPS),
        ("data/sql_commands.json", EMPTY_SQL_COMMANDS),
        ("data/sql_targets.json", EMPTY_SQL_TARGETS),
        ("secrets/secret_text.json", SECRET_TEXT),
    ]

#: Directories that must exist even though nothing is written into them yet. A run that has to
#: create its own log directory fails differently on every platform.
DIRECTORIES = ("data", "logs", "runtime", "secrets", "assets/metrics")


class ScaffoldError(RuntimeError):
    """`init` refused. It never overwrites, so the message says what is already there."""


@dataclass
class InitResult:
    root: Path
    written: list[str]
    skipped: list[str]


def initialise(root: Path, *, app_name: str = "dbabrain", force: bool = False) -> InitResult:
    """Write the smallest tree that runs, into *root*.

    **Never overwrites without being asked.** The files this writes are the ones a user or an agent
    edits immediately afterwards, so a second `init` that silently reset them would destroy the only
    work anybody had done. An existing file is reported and left alone.
    """
    root = Path(root).expanduser().resolve()
    written: list[str] = []
    skipped: list[str] = []

    for name in DIRECTORIES:
        (root / name).mkdir(parents=True, exist_ok=True)

    for relative, content in _files(app_name):
        path = root / relative
        if path.exists() and not force:
            skipped.append(relative)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
        written.append(relative)

    # The guide goes in last and by the same rule: never overwritten, because somebody may have
    # annotated it, and the notes a reader adds to a first-run guide are the ones worth keeping.
    guide = root / "AGENTS.md"
    if guide.exists() and not force:
        skipped.append("AGENTS.md")
    else:
        guide.write_text(AGENTS_GUIDE, encoding="utf-8")
        written.append("AGENTS.md")

    return InitResult(root=root, written=written, skipped=skipped)


def next_steps(root: Path) -> str:
    """What to do now — written to be followed by a person or by an agent reading stdout.

    Numbered, one action per line, with the exact command. An agent that can read this and edit
    JSON needs nothing else; a human reading the same lines is not being talked down to.
    """
    return "\n".join([
        f"Tool root ready at {root}",
        "",
        "Next, to monitor one SQL Server:",
        "",
        "  1. Add the instance to data/db_instances.json",
        "     The notes in that file list every field it needs.",
        "",
        "  2. Add its login to data/users.json, under the same server_id.",
        "     An instance is a machine and a credential is an account, so they are separate files.",
        "",
        "  3. Add the password to secrets/secret_text.json under the password_ref you used,",
        "     then encrypt it:",
        "       db-ops encrypt-secret --key-base64 <passphrase in base64>",
        "",
        "  4. Check the target resolves and the credential is found, without connecting:",
        "       db-ops check-credentials",
        "       db-ops metrics collect --dry-run",
        "",
        "  5. Collect:",
        "       db-ops metrics --key-base64 <passphrase in base64> collect",
        "",
        "  6. Read the result:",
        "       db-ops metrics summary-latest",
        "",
        "Alerts to Telegram are step 6, and data/telegram_config.json says how.",
        "",
        "Results are stored in SQLite under runtime/ - nothing else to install.",
        "data/store_config.json is where you move to PostgreSQL later.",
    ])
