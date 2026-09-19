# Standing up a node, from `pip install` to a running estate

A walkthrough, not a tool root: every command below was run in this order on a real node, and each
one states **what it proves**. Values are placeholders — addresses from RFC 5737 (`192.0.2.x`), a
bot called `@your_bot`, chat ids like `-1001234567890`. Substitute your own.

The toolkit is `dbabrain` on PyPI, and every command is available two ways: the `db-ops` console
script, or `python -m db_ops.<app>.cli`. They are the same code.

---

## 0. What you need first

| | |
| --- | --- |
| Python | 3.12 or newer |
| A database to watch | any SQL Server, Oracle, PostgreSQL or MySQL instance, and a login for it |
| For SQL Server | Microsoft's **ODBC Driver 18**. The PostgreSQL driver is pure Python and needs nothing |
| A passphrase | you choose it now and keep it. It encrypts the secret store, and **nothing else can decrypt that store** |
| Optional | a Telegram bot, if you want alerts and chat commands |

Every shell that runs a step exports two variables:

```powershell
$env:DB_OPS_SECRET_KEY = "<your passphrase>"   # never written to a file by the toolkit
$env:PYTHONIOENCODING  = "utf-8"               # a Windows console is cp1252 and dies on an emoji
```

---

## 1. Install into its own folder

The folder you install into is the **tool root**: the toolkit looks for `config.json` and `data/`
in `DB_OPS_HOME`, else in the directory you are standing in, never beside its own code.

```powershell
mkdir C:\dbabrain-node
cd    C:\dbabrain-node
py -m venv .venv
.venv\Scripts\python.exe -m pip install "dbabrain[postgres,mssql,ssh,winrm]"
.venv\Scripts\python.exe -m db_ops.cli --version
```

**The extras are not optional, and are the part most often got wrong.** `postgres` for a PostgreSQL
store, `mssql` for SQL Server targets, `ssh` for OS-level commands, and `winrm` — without it a
Windows host in a workgroup silently falls back to a local PowerShell that cannot authenticate, and
the failure reads as the *host's* fault.

**Proves:** the version printed is the one you meant to install.

## 2. `init` — write the starter tree, then read it

```powershell
db-ops guide      # prints the getting-started text; writes nothing
db-ops init
```

`init` writes `config.json`, `data/*.json` (inventory, metrics catalogue, schedule, report and
Telegram settings, an empty `restore_config.json`), `secrets/secret_text.json`, and empty `logs/`
and `runtime/`. It **never overwrites**: an existing file is reported and left alone.

Read what it wrote before changing any of it — `data/store_config.json` says **sqlite**, which is
the right first store. Move to PostgreSQL later with `db-ops db use-store postgresql`.

Then say which clock this node's schedules are read against, before anything is scheduled or
stamped:

```powershell
db-ops db --config config.json timezone --set Europe/Berlin    # or UTC, Asia/Singapore, ...
db-ops db --config config.json timezone                        # read it back
```

Every `time_window.from_hour` is a **local** hour read against this setting, so it decides when the
heavy overnight collections actually run. Stored timestamps are always UTC and this cannot move
them. `init` ships `UTC`; state the zone you mean rather than inheriting it, because an unknown name
does not fail — it falls back, and a node on the wrong clock is indistinguishable from one whose
windows are simply never due.

**Proves:** the tree exists, the store is this node's own file, and the clock is the one you chose.

## 3. Run everything before configuring anything

Do this here, not later. The node has no inventory, no secret and no token, which is exactly the
state you are in for the next ten minutes — and every scheduled app must come back **clean** in it.

```powershell
db-ops daemon --config config.json --once
```

One pass over the whole shipped schedule. Every command ships **active**, so this runs them all.

**Proves:** each job reaches `status=done` in seconds and says what is missing rather than failing —
`NOT_CONFIGURED`, `no bot token is configured`, `configured: 0`. `logs/errors.log` holds only its
header. The web console is skipped here on purpose (`long_running_service_not_run_by_once`): it is a
service that never exits, and `--once` waits for what it starts.

## 4. Start the daemon

```powershell
$env:DB_OPS_NODE_ROLE = "worker"
Start-Process -FilePath ".venv\Scripts\python.exe" `
              -ArgumentList "-m","db_ops.cli","daemon","--config","config.json" `
              -WorkingDirectory (Get-Location) -WindowStyle Hidden -PassThru |
  ForEach-Object { $_.Id | Set-Content daemon.pid }
```

**`DB_OPS_NODE_ROLE=worker` is not optional and is easy to miss.** Every entry in
`app_commands.json` is `node_role: all` or `worker`; a daemon left in the default `master` role
schedules nothing and looks like a healthy idle process.

After two or three minutes:

```powershell
db-ops db --config config.json ops-status '{}'
curl http://127.0.0.1:8080/db_ops/login
```

**Proves:** every command has run at least once, none is overdue, and the console answers 200. The
console and the report pages are served by the scheduled web host — you do not start it yourself.

Stop it with its whole tree when you need to (`taskkill /PID <pid> /T /F` on Windows): the daemon
owns child processes, and killing only the parent leaves them behind.

## 5. The secret store, and how to put one secret in it

Two ways in, and they do not mix well:

```powershell
# ONE secret, nothing in clear on disk - the request goes on stdin, never on the command line
'{"ref": "TELEGRAM_BOT_TOKEN", "value": "<bot token>"}' | db-ops common secret-set -

# MANY secrets at once: fill secrets/secret_text.json, then
db-ops encrypt-secret --key-base64 "<passphrase, base64>"
```

> **`encrypt-secret` REPLACES the store with that file.** The `secrets/secret_text.json` that `init`
> writes holds no secrets, so running it on a node where you have only used `secret-set` (or
> `instance-add`) wipes every secret you stored. `secret-set` prints a warning saying exactly that
> when the file exists without your ref. Pick one route and stay on it; delete the plaintext file
> once you have encrypted it.

A secret is never passed as a command-line argument: argv is readable by every process on the
machine. That is why `secret-set` accepts stdin only.

**Proves:** `data/encrypted_secret_text.json` exists and `db-ops common check-secret '{}'` can read it.

## 6. Telegram — the bot answers, then the alerts flow

```powershell
'{"ref": "TELEGRAM_BOT_TOKEN", "value": "<bot token>"}' | db-ops common secret-set -
db-ops telegram --config config.json use-bot --ref TELEGRAM_BOT_TOKEN
db-ops telegram --config config.json bot-info
```

`use-bot` is what points this node at that token. It matters most on a node built from somebody
else's config bundle: `data/bot_telegram.json` travels inside the bundle, so an imported node
arrives on the bot the bundle came from and says nothing about it — and two pollers on one token
means Telegram refuses one of them, while a retry delivers the same message twice. The ref is
checked against the secret store and the id and username are read back from Telegram rather than
typed.

`bot-info` answers with the id and username the config asks for, and with **`privacy_mode`**. `on`
is Telegram's default and means the bot sees only commands addressed to it; `/spbot_*` still works,
so a half-configured group passes a casual look. Turn it off with `@BotFather` (`/setprivacy`) or
make the bot an admin.

Now let the bot discover who is talking to it. **Send it a message from yourself, and one message in
each group** — a group is recorded only when a message arrives in it, and the running daemon saves
them within seconds. Then say what each is for:

```powershell
db-ops telegram --config config.json user-level  --user @you --level 100
db-ops telegram --config config.json group-level --group "Alerts - critical" --level critical --allow-command 10
db-ops telegram --config config.json route critical
```

Two commands exist for the case where that cannot happen yet, and both are new in 0.19.0:

```powershell
db-ops telegram --config config.json group-add --group-id "-1001234567890" --level critical --allow-command 10
db-ops telegram --config config.json user-level --user @you --level 100 --pending
```

- `group-add` registers a chat that has never posted. A group created *for* alerts has nobody
  talking in it, so intake never sees it — the one case that matters is the one that did not work.
- `--pending` records a level against a username the node has not met. Intake runs only under the
  daemon, so on a brand-new node your own first command is refused until it has. The first message
  from that username adopts the level. **Without `--pending` an unknown name is still refused**: it
  is far more often a typo, and a level left waiting for whoever claims it is a permission granted
  to nobody in particular.

- Intake records every sender at level **0**, so until you run `user-level` the bot answers your own
  commands with *"Permission denied (user_type=0)"*.
- A command with `command_type` N needs a user at N or above, and in a group the group's
  `allow_command` as well.
- `route <level>` prints where a level will actually go. Check it before you trust it: an unmapped
  level answers `alert: false, chat_id: ""`, which looks identical to a level deliberately kept quiet.
- Alerts are **on by default** (`enabled: true` in `data/telegram_config.json`). Nothing can be sent
  before a token exists and a group has a level, so storing the token and levelling a group is all
  it takes. Set `enabled: false` to mute alerts; commands are answered either way.
- To pin a level to one chat regardless of group levels, set it in `level_chat_map` in
  `data/telegram_config.json` — **the explicit map wins** over a group's `notify_level`.

One test message, to prove delivery rather than assume it:

```powershell
db-ops telegram --config config.json send-message --chat-id "-1001234567890" --text "node online"
```

**Proves:** the token belongs to the bot you think it does, your own commands are allowed, each level
resolves to the chat you meant, and a message arrives there.

## 7. Add the first database — one command, not four files

```powershell
'{"server_id": "ACME-192-0-2-10-SQL01", "db_type": "sqlserver", "ip": "192.0.2.10",
  "port": 1433, "major_version": 16, "service_name": "MSSQLSERVER", "platform": "windows",
  "env": "prod", "username": "monitor_user", "password": "<password>"}' | db-ops common instance-add -
```

It writes the inventory record, the credential in `users.json`, and the password **encrypted only** —
replacing four hand-edits that used to repeat the `server_id` in two files and the credential name in
three, with nothing checking they agreed.

Three fields fail in ways that do not name themselves:

- **`service_name` is a label, not a database.** Metric collection on SQL Server always connects to
  `master`.
- **`major_version` picks the query variant.** Get it wrong and a metric fails on syntax.
- **`default_credential_name` is a reference, never a password.**

Anything else in the object is passed through to the record: `cmd_access`, `sql_access`, `metrics`,
`note`. Then prove it, in this order:

```powershell
db-ops check-credentials                     # every target resolves to a real login
db-ops common check-secret '{}'              # each secret actually logs in
'{"target": "ACME-192-0-2-10-SQL01", "sql": "SELECT @@VERSION"}' | db-ops common run-sql -
```

**Proves:** the credential resolves, the login works, and the driver can reach the instance. A target
unreachable on the network is not a failure of this step; a target with no resolvable credential is.

Add the rest **one at a time**, and check each before the next. A file with twenty targets and one
mistake takes longer to debug than twenty files with one target.

## 8. OS metrics, and a target that needs a gateway

**An OS login** for CPU, disk, services and event logs over WinRM or SSH. One command writes all
three parts — the `remote_credentials` entry, the secret behind it, and the `cmd_access` block on
the instance that names it:

```powershell
'{"server_id": "ACME-192-0-2-10-SQL01", "host": "192.0.2.10",
  "username": "svc_dbops", "password": "<password>",
  "credential_name": "remote_192_0_2_10_admin",
  "method": "winrm", "auth_type": "password", "port": 5985,
  "shell": "powershell", "platform": "windows"}' | db-ops common remote-credential-add -
```

It refuses the three ways this used to go wrong as a hand-edit, which is why it exists:

- `auth_type` defaults to `key`. An ssh **password** with no explicit `auth_type` resolves to no
  credential, so the password is never read and the failure looks like a broken host.
- `method: "local"` runs **inside this node**, so with a remote `host` it reports this machine's CPU
  under that host's name. Use `ssh` or `winrm`.
- `platform` belongs on the instance, not inside `cmd_access`.

An `enabled: false` you set deliberately is kept: re-registering the login does not switch a
disabled `cmd_access` back on.

**A gateway** for an engine the modern driver cannot speak to (an old Oracle, for instance) is a
`sql_access` block naming a bridge and the secret it authenticates with:

```json
"sql_access": {"method": "api", "bridge_url": "http://192.0.2.20:8765/query",
               "secret_ref": "TOKEN_BRIDGE", "mode": "sysdba", "timeout_seconds": 300}
```

```powershell
'{"ref": "TOKEN_BRIDGE", "value": "<bridge token>"}' | db-ops common secret-set -
```

**Nothing warns you if that ref is missing** — `check-credentials` passes, `check-secret` calls it
`NO_TARGET`, and every collection for that target fails with *"bridge secret not found"*. Store it
when you add the block.

## 9. Backups and restore drills, by command

Both are entries in `data/restore_config.json`, and both are registered rather than hand-written:

```powershell
'{"backup_id": "ACME_SQL01_NIGHTLY", "server_id": "ACME-192-0-2-10-SQL01",
  "db_type": "sqlserver", "backup_root": "\\192.0.2.30\SQLBK",
  "cleanup_retention": 1209600,
  "time_window": {"from_hour": 1, "to_hour": 5, "repeat_interval": 3600},
  "notify": {"logging_on_run": {"enabled": true, "telegram_chat": "backup"},
             "alert_on_error": {"enabled": true, "telegram_chat": "backup"}}}' |
  db-ops backup_restore backup-add -

db-ops backup_restore list-backups
```

`restore-add` takes the same shape for a drill that restores somewhere else. Three fields are
required and each has cost somebody a real incident:

- **`time_window`** — without one a job is not "unscheduled". It gets an always-open window and a
  300-second repeat: one restore entry registered without it restored a 183 GB database, finished,
  and started again four seconds later, all night, every run reporting success.
- **`notify`** — without one an entry still notifies, but at the neutral `logging` and `error`
  levels rather than its own chat. From the group you are actually watching, that reads as silence.
- **`cleanup_retention`**, in **seconds**, on every job and every entry.

**Proves:** `list-backups` and `list-restores` show the entry with the window it will actually use.

**A restore's verdict is computed, not asserted.** The workflow ends with a `verify` phase that
queries every database it touched; one that will not answer fails the run, whatever the steps before
it reported. A drill that used to read `done` may therefore now read `failed` — what fell is the
false part. Nothing checks free space on the target before a restore starts: size a drill to fit.

## 9a. SQL tasks, by command

`add-sql` (and `/spbot_add_sql`) writes a script, a command and one target in one call — the
shortest route to a one-server task. Anything it cannot say takes two calls instead: **what** runs,
then **where**, once per server.

```powershell
# WHAT runs. `sql_text` writes the script for you; `script_path` names one that already exists.
'{"sql_name": "Row count of the audit table", "db_type": "sqlserver",
  "sql_text": "SELECT COUNT(*) AS n FROM dbo.audit_log;"}' |
  db-ops common sql-command-add -

# WHERE it runs. One call per target; `sql_id` is the one the first call answered with.
'{"sql_id": 1, "server_id": "ACME-192-0-2-10-SQL01", "database_name": "PAYROLL_Test",
  "time_window": {"repeat_interval": 300, "timeout": 600}}' |
  db-ops common sql-target-add -

db-ops sql-tasks list-tasks --sql-id 1 --all
```

- A task fed by a Python program (`input_type: "python"`) may use `{target_server_id}` and
  `{target_database}` in `input.args`; they are filled per target, so **one** command serves every
  server it is registered on.
- Both calls validate before writing: a missing script, an undeclared `{name}`, a taken `target_no`
  or a target whose `sql_id` has no command are refused, not discovered on the schedule.
- The schedule is the target's own `repeat_interval`. The daemon scan and the SQL task app both run
  every second, so neither rounds it up.
- **`active: false` on a target keeps the scheduler away, not a forced run.** `--force` (and
  `/spbot_run_sql_task`) runs every target of the task, including the switched-off ones.

**Proves:** `list-tasks --all` shows the command with every target and the `active` flag each has.

## 10. Watch it work

The daemon needs no further help. Within a few minutes:

```powershell
db-ops db --config config.json check          # tables and row counts, climbing
db-ops db --config config.json ops-status '{}'
```

Three things make the node usable **by a person** rather than only by its own scheduler:

```powershell
db-ops webhost --config config.json user-add --username you --level 100 --password-stdin --remember
db-ops db --config config.json sync-config '{"actor": "you"}'
db-ops reports --config config.json use-base-url --this-node
```

- **`user-add`** creates the console account. It lives in the **store**, not in `data/`, so a node
  pointed at a store that already has the account is told it exists — check with `user-list` rather
  than forcing it.
- **`sync-config`** loads the `data/*.json` the console reads, and **names every catalogued file
  this node does not have**. Read that list: a missing policy file is not an empty policy, and
  before this release it was reported as `ok`.
- **`use-base-url`** writes the address this node's pages are published on, which every cross-page
  link and every Telegram link is built from. `--this-node` takes it from the node itself;
  a URL sets it explicitly; `--clear` goes back to the derived answer. Without it a node built from
  someone else's config bundle publishes links to **the machine it was cloned from** — and it is
  refused inside a container, where the address the node can see is not one anyone else reaches.

And on the pages it publishes, at `http://<this node>:8080/`:

| Page | What it is |
| --- | --- |
| `/report_dba/` | the hub: links to whatever has been produced |
| `/report_dba/database-inventory.html` | the fleet: every server, storage, health triage |
| `/report_dba/server-metrics.html` | per server: workload, waits, memory, top queries, the log by database |
| `/report_dba/sla.html` | SLA / SLO compliance |
| `/db_ops/login` | the console: config, logs, run a command |

The inventory page is built by `APP-REPORTS-INVENTORY-WORKFLOW`, hourly. On its first run it **seeds**
`runtime/reports/database-inventory.json` from your `db_instances.json` and merges fresh health into
it on every run after that; with no instance registered it reports `NOT_CONFIGURED` and publishes
nothing. The per-server page needs **two** collections before any rate exists, and 24-hour columns
need a day of history — a new node fills in as it goes.

## 11. What bites people, in one place

| | |
| --- | --- |
| `encrypt-secret` after `secret-set` | **replaces** the store from `secrets/secret_text.json`, wiping secrets that are only in the encrypted copy |
| A secret on the command line | visible to every process. `secret-set` takes stdin only, and on Windows single-quoted JSON is mangled by the shell — pipe it, or use `@file` for anything that is not secret |
| `daemon --once` | runs jobs, skips services. It is a smoke test, not a way to start the web host |
| Daemon in the default `master` role | schedules nothing, looks healthy |
| A group that never posted | is invisible to the bot, so alerts have nowhere to go |
| A new bot answering old commands | a freshly tokened node works through Telegram's 24-hour backlog at once, including commands sent before it existed |
| `metrics.enabled: false` on an instance | collection is off for that target; zero rows is the correct outcome, not a fault |
| Timezone | `config.json` ships `"UTC"`. Set it with `db timezone --set <ZONE>` before the first report is stamped — an unknown name falls back instead of failing |

## 12. When something is wrong

```powershell
Get-Content logs\errors.log                  # only a header = nothing has failed
db-ops db --config config.json ops-status '{}'   # which app failed, when, and whether it is overdue
db-ops check-credentials                     # a target with no resolvable credential
db-ops common self-status '{"format":"txt"}'  # what this node is, and the addresses it serves
db-ops common db-status '{"target": "ACME-192-0-2-248"}'   # is one instance, database or schema up and usable
```

`db-status` answers at three depths — the instance, one database, one schema — for SQL Server,
PostgreSQL and Oracle, and reports what that engine actually has rather than pretending the three
are the same shape. A JSON object in, a JSON object out, so a script can read it.

Each app also writes `logs/<app>_runtime.log` with the command line it ran, its exit code and the
first lines of its output — which is usually the whole answer.
