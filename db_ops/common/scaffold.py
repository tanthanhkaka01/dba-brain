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

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from db_ops.lib.json_io import atomic_write_text

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
        "should outlive this one: fill in the postgresql block below, then switch with",
        "  db-ops db use-store postgresql",
        "which validates the block and keeps this sqlite one, so the way back is not a retype.",
    ],
    "backend": "sqlite",
    "sqlite": {
        "path": "runtime/dbabrain.sqlite",
        "connection_string": "sqlite:///runtime/dbabrain.sqlite",
    },
    "postgresql": {
        "host": "",
        "port": 5432,
        "database_name": "dbabrain",
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
        "'enabled' switches ALERTS to groups, and it ships TRUE: alerts start the moment a token is",
        "stored and a group has a level, with no third step to forget. Nothing is sent before both",
        "exist. Set it false to mute alerts. It is not a master switch - commands sent to the bot are",
        "answered either way.",
        "",
        "To set it up:",
        "  1. Create a bot with @BotFather and copy the token",
        "  2. Pipe {\"ref\": \"TELEGRAM_BOT_TOKEN\", \"value\": \"<token>\"} into",
        "     python -m db_ops.common.cli secret-set -      (stdin: never on the command line)",
        "  3. Message the bot from yourself and from each group, then",
        "     db-ops telegram user-level --user @you --level 100",
        "     db-ops telegram group-level --group \"<title>\" --level warning",
        "  Alerts are already on; that is all.",
        "",
        "level_chat_map routes a severity to a chat. Anything not mapped falls back to 'private'.",
    ],
    # True since 2026-09-11, at the operator's direction: shipping it false made "store the token"
    # and "turn alerts on" two steps, and a node that had done the first answered commands while
    # sending no alert at all - which read as broken. It cannot send early: with no token the
    # Telegram app skips, and with no group level nothing routes (routing.route_for_level).
    "enabled": True,
    # Three ways in, tried in this order: the environment variable named by `bot_token_env`, then
    # the secret store under `telegram_bot_token_ref`, then the literal `bot_token`. The middle one
    # is the one to use, and the scaffold missed it until a real send failed with "bot token is
    # empty" — a message that names the symptom and not the missing field.
    "bot_token_env": "TELEGRAM_BOT_TOKEN",
    # `bot_config_file` is named rather than left to the default, because this file's own notes
    # tell the reader to put the token ref there and a path that is only a default is a path
    # nobody knows to look at.
    "bot_config_file": "data/bot_telegram.json",
    # `telegram_bot_token_ref` is DELIBERATELY ABSENT. It used to be written here as the
    # placeholder "TELEGRAM_BOT_TOKEN", and a value here WINS over `bot_telegram.json` - so a
    # fresh node could not change which bot it was without editing this file too, however
    # carefully it filled in the one the notes point at. Worse, the id and username were still
    # read from the bot file, so the node reported a bot it was not authenticating as. Found
    # 2026-09-14 building a 0.17.0 node one command at a time. Set it here only to deliberately
    # override the bot file; leave it out and the bot file decides, which is what the master
    # does and why the master works.
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

EMPTY_TELEGRAM_USERS = {
    "schema_version": 1,
    "notes": [
        "People the bot has seen, and the permission level each holds. The intake writes an entry",
        "the first time somebody messages the bot, at user_type 0 - `telegram user-level` is what",
        "raises one. Nothing can be written here in advance: a user who has never posted is a user",
        "Telegram has not told this node about.",
        "Ships EMPTY for the same reason telegram_groups.json does. Absent and empty are different",
        "states, and only one of them says 'nobody has spoken to this bot yet'.",
    ],
    "telegram_users": [],
}

#: Which bot this node speaks as. Written empty so the file EXISTS and says "no bot yet" - before
#: 2026-09-16 `init` wrote a `telegram_config.json` pointing at this file and did not create it,
#: which is a dangling reference a reader has to guess about. `telegram use-bot --ref <SECRET_REF>`
#: fills it in, reading the id and username back from Telegram rather than taking them typed.
#: The token itself is never here - only the ref naming it in the encrypted store.
EMPTY_BOT_TELEGRAM = {
    "schema_version": 1,
    "notes": [
        "Which bot db_ops speaks as. Set it with: db-ops telegram use-bot --ref <SECRET_REF>",
        "The ref names a key in the encrypted secret store; the token is never written here.",
        "This travels inside a config bundle, so a node imported from another estate arrives on",
        "THAT estate's bot unless use-bot is run. Two pollers on one token is refused by Telegram",
        "and a retry delivers the same message twice.",
    ],
    "telegram_bot_token_ref": "",
    "telegram_bot_id": "",
    "telegram_bot_username": "",
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

#: Written into the tool root as ``AGENTS.md``, beside the JSON it describes.
#:
#: Not repository documentation - generated output, and that placement is the point. An agent that
#: has just run `init` is standing in this directory; a guide in a repository it never cloned is a
#: guide it will not read. The same file serves a person, because the instructions are the same
#: instructions and the only difference is who is typing.
#:
#: The text lives in ``db_ops/common/agents_guide.md`` (package data) rather than in this module since
#: 0.22.0, when it grew from a first-run note into the whole operating guide for an AI agent. It is
#: deliberately NOT named ``AGENTS.md`` inside the package: an agent working on this source tree
#: reads any ``AGENTS.md`` as instructions for the code beside it.
GUIDE_SOURCE = Path(__file__).with_name("agents_guide.md")
AGENTS_GUIDE = GUIDE_SOURCE.read_text(encoding="utf-8")

#: The first line of every generated AGENTS.md. It records which build wrote the file and a hash of
#: the text below it, which is how `init` tells an untouched guide from one a person has annotated -
#: the second is saved before it is replaced (see write_guide).
_STAMP_PREFIX = "<!-- written by dbabrain init"

#: Where an edited AGENTS.md is saved before `init` replaces it, relative to the tool root.
GUIDE_BACKUP_DIR = Path("runtime") / "agents_guide"


def rendered_guide() -> str:
    """AGENTS.md exactly as `init` writes it: a stamp line, then the guide."""
    from db_ops.lib.distribution import PUBLIC_VERSION

    digest = hashlib.sha256(AGENTS_GUIDE.encode("utf-8")).hexdigest()[:16]
    return (f"{_STAMP_PREFIX} {PUBLIC_VERSION}; sha256 {digest} - every init replaces it with the "
            f"installed version's guide, saving an edited copy in runtime/agents_guide/ first -->\n"
            + AGENTS_GUIDE)


def guide_is_untouched(text: str) -> bool:
    """True when ``text`` is a guide some `init` wrote and nobody has edited since.

    A file without the stamp predates it (0.21.0 and earlier) or was written by hand; either way it
    is treated as edited, and saved before it is replaced.
    """
    first, _, body = text.partition("\n")
    if not first.startswith(_STAMP_PREFIX) or " sha256 " not in first:
        return False
    recorded = first.split(" sha256 ", 1)[1].split(" ", 1)[0]
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] == recorded


def write_guide(root: Path, *, force: bool = False) -> tuple[str, Path | None]:
    """Write ``root/AGENTS.md`` as this build's guide: ``(outcome, saved_copy)``.

    ``outcome`` is ``written``, ``unchanged`` or ``replaced``. **The guide always matches the
    installed version** (the operator, 2026-09-24: 0.22.0 and 0.23.0 changed too much for an old one
    to be left in place). Until then an edited guide was kept, so the root an agent opened after an
    upgrade told it the previous version's field names. Nothing a person wrote is lost for it: an
    edited file - or one from before the stamp - is copied to ``runtime/agents_guide/`` first, and
    that copy is ``saved_copy``. ``force`` is accepted for the callers that pass it and changes
    nothing any more.
    """
    del force
    root = Path(root)
    path = root / "AGENTS.md"
    text = rendered_guide()
    saved: Path | None = None
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == text:
            return "unchanged", None
        if not guide_is_untouched(current):
            from db_ops.lib.timezone import utc_file_stamp

            saved = root / GUIDE_BACKUP_DIR / f"AGENTS.{utc_file_stamp()}.md"
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text(current, encoding="utf-8")
    # Atomic (review 0.25.0, B9.2): the operator's edited guide is replaced, not truncated first.
    atomic_write_text(path, text)
    return ("replaced" if saved else "written"), saved


def _config(app_name: str) -> dict:
    return {
        "notes": [
            "Runtime paths, and which declaration files to read.",
            "Every relative path here is resolved against this file's directory, so the whole tree",
            "can be moved or copied without editing anything.",
            "'timezone' is the clock this node shows: an IANA name (Asia/Ho_Chi_Minh) or a fixed",
            "offset (+07:00). Stored timestamps stay UTC; this is what rendered times and a",
            "time_window's from_hour/to_hour mean. Set it before the first scheduled run.",
        ],
        "app_name": app_name,
        # UTC rather than this machine's zone: `init` runs where the operator happens to be, the
        # daemon runs wherever it is deployed, and a default read off the installing machine is
        # how the tool ended up with three different clocks in the first place.
        "timezone": "UTC",
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
    # The eight that had no seed at all until 2026-09-16. `config_catalog.json` lists thirty files
    # and the package shipped twelve, so `init` could not write these and no registration command
    # creates them: a clean install was permanently incomplete for all eight, four of them
    # graders' policies. That is the 0.16.0 failure again, where an absent `backup_policy.json`
    # made every database compliant and printed "4/4 DB within policy" over a 168-day-old log
    # backup. Which ones carry defaults and which ship empty is decided by whose fact they hold -
    # a rule the product owns ships in force, a fact about this estate ships empty with its shape
    # intact, so the app can name the missing field instead of inventing a lab.
    "data/capacity_policy.json": "metrics/catalogue/capacity_policy.json",
    "data/metric_importance_overrides.json": "metrics/catalogue/metric_importance_overrides.json",
    "data/restore_drill_policy.json": "backup_restore/catalogue/restore_drill_policy.json",
    "data/maintenance_policy.json": "backup_restore/catalogue/maintenance_policy.json",
    "data/sqlserver_instance_policy.json": "common/catalogue/sqlserver_instance_policy.json",
    "data/docker_db_connections.json": "sre/catalogue/docker_db_connections.json",
    "data/sre_config.json": "sre/catalogue/sre_config.json",
    "data/network_reservations.json": "control/catalogue/network_reservations.json",
    # What each dangerous operation costs to authorize. Without it every confirmed command is
    # priced at the STRICTEST level - two answers, the second the target's id typed out - while
    # every one of them collects exactly one `yes`, so `/spbot_kill_spid`, `/spbot_shrink_log`,
    # `/spbot_start_job`, `/spbot_disable_job` and `/spbot_run_sql_task` were all refused on a
    # fresh install and worked on this estate, which has the file. Found on 2026-09-04 by the
    # public tree's suite: the confirmation tests pass where the file exists and fail where it
    # does not, which is the difference between the two trees.
    "data/emergency_operations.json": "common/catalogue/emergency_operations.json",
    # Reference rather than configuration, and seeded for exactly that reason: without it a
    # node answers "what does retry_interval mean" with nothing, and the console has no
    # field help to draw. Identical on every node - see db_ops/lib/shared_objects.py.
    "data/shared_config_objects.json": "common/catalogue/shared_config_objects.json",
    "data/config_references.json": "common/catalogue/config_references.json",
    # Ships **empty**, and that is the content rather than a placeholder: a restore target is an
    # estate fact with no sensible default. What it carries is the shape and the word "empty".
    #
    # It is here because the absence was not readable. `data_files.json` lists
    # `restore_config.json` as one of the toolkit's files, `init` never wrote one, and switching
    # the backup/restore command on made the app fail every cycle with the whole of its error text
    # being `'prod_backup_share'` - a key name, naming neither the missing file nor the app that
    # wanted it. The loader reports "nothing configured" properly now; this gives the operator the
    # file the manifest already promised them.
    "data/restore_config.json": "backup_restore/catalogue/restore_config.json",
    # The one file here that must NOT ship empty. An absent backup policy does not make the
    # backup report quiet - it makes it *wrong*: with no rule, every type is "not required",
    # every database is OK, and the fleet page prints "15/15 DB within policy" across a server
    # whose newest LOG backup is 168 days old. Measured on 2026-09-14 on two nodes holding
    # identical backup evidence - one with this file reported nine servers in violation, one
    # without reported the estate compliant, and no line anywhere said which of them to believe.
    # `db_ops.lib.backup_policy` now refuses to grade without a policy, and this entry is the
    # other half: a fresh install starts with the common daily-full-plus-log plan already in
    # force, so it grades from the first collection instead of waiting to be told how.
    "data/backup_policy.json": "backup_restore/catalogue/backup_policy.json",
}

#: The package root: the seeds `init` writes ship with the component that owns each, and this module
#: moved into `common` in 0.24.0 (rules R41), so it reads them relative to the package, not to itself.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]

PACKAGED_CATALOGUE = PACKAGE_ROOT / "metrics" / "catalogue" / "metric_definitions.json"


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
        return json.loads((PACKAGE_ROOT / relative).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


#: What `init` writes, as (relative path, content). Directories come from the paths.
def _files(app_name: str) -> list[tuple[str, dict]]:
    return [
        ("config.json", _config(app_name)),
        ("data/store_config.json", SQLITE_STORE),
        ("data/db_instances.json", EMPTY_INVENTORY),
        ("data/metric_definitions.json", packaged_catalogue()),
        # Every shipped default, read from PACKAGED_DEFAULTS rather than listed again here.
        # It WAS listed again here, and on 2026-09-14 that cost `data/backup_policy.json`: the
        # entry was added to the map, the file shipped in the wheel, `packaged_default` found it,
        # and `init` still did not write it, because the second list had not been touched. A
        # fresh 0.17.0 root came up with no backup policy - the exact hole the release exists to
        # close. One list, and a new catalogue file now needs one entry instead of two.
        #
        # Skipped rather than failed when the package did not carry one: a missing default should
        # cost one config file, not the whole init.
        *(
            (name, content)
            for name in PACKAGED_DEFAULTS
            # handled above, because it is the one with a fallback when the package lacks it
            if name != "data/metric_definitions.json"
            and (content := packaged_default(name)) is not None
        ),
        ("data/users.json", EMPTY_USERS),
        ("data/telegram_config.json", TELEGRAM_CONFIG),
        ("data/telegram_groups.json", EMPTY_TELEGRAM_GROUPS),
        ("data/telegram_users.json", EMPTY_TELEGRAM_USERS),
        ("data/bot_telegram.json", EMPTY_BOT_TELEGRAM),
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
    #: An edited AGENTS.md, saved before this version's guide replaced it.
    guide_saved_copy: Path | None = None


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
        # The plaintext secrets source is the one file here that is created private; the rest are
        # configuration the daemon reads, perhaps as another user than the one running `init`.
        atomic_write_text(path, json.dumps(content, indent=2) + "\n",
                          private=relative.startswith("secrets/"))
        written.append(relative)

    # The guide goes in last, and it is the one file `init` always refreshes: after `pip install -U`
    # the guide an agent reads must describe the build it is driving. An edited one is saved to
    # runtime/agents_guide/ first - the stamp on its first line is how the two are told apart.
    outcome, saved = write_guide(root)
    (skipped if outcome == "unchanged" else written).append("AGENTS.md")

    return InitResult(root=root, written=written, skipped=skipped, guide_saved_copy=saved)


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
