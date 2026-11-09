# dbabrain, for the assistant

## The estate and its keys

- **`server_id` is the only key of a machine** (its engine and address, e.g. `SQLSERVER-<ip with dashes>`). One machine, one
  id; group and join on it, never on an IP. The inventory is `data/db_instances.json`
  (`server_id`, `db_type`, `ip`, `port`, environment, `cmd_access`); a database login is named by
  `credential_name`, its password is never in a file.
- Configuration is data: every app's behaviour is in `data/*.json`. The operator's SQL is under
  `assets/` (`assets/tasks/`, `assets/sql_telegram_commands/`); the shipped collectors and backup
  scripts are in the package (`dbabrain/metrics/collectors/`, `dbabrain/common/backup_scripts/`).
- `dbabrain/common/agents_guide.md` is the toolkit's own guide for agents: the rules, the commands,
  what is safe to run. Read it when a question is about running dbabrain.

## Apps and where they write

| App (log scope) | Config | Store tables |
| --- | --- | --- |
| daemon (`jobs`) - runs every scheduled app | `data/app_commands.json` (each job's `time_window`: `repeat_interval`, `timeout`) | `job_runs` (one row per run: `job_code`, `status` START/DONE/ERROR/TIMEOUT/END, `message`, `error_text`, `started_at`, `duration_ms`), `job_runs_history` (older) |
| metrics (`metrics`) | `data/db_instances.json`, `data/metric_definitions.json` | `metric_runs`, `metric_results` (`server_id`, `metric_code`, `metric_item`, `metric_value`, `status`, `message`, `collected_at`) - **the largest table: always filter on `collected_at` and `server_id`**; `metric_results_archive`, `target_health` |
| reports (`reports`) | `data/reports_config.json` | `reports`, `report_types`, `report_send_state` |
| SQL tasks (`sql_tasks`) | `data/sql_commands.json`, `data/sql_targets.json` | `sql_runs` |
| SLA (`sla`) | `data/sla_policies.json` | `sla_runs`, `sla_results` |
| backup / restore (`backup_restore`) | `data/restore_config.json` (`backup_restore.backups[]` entries and their `jobs`, `restores[]`) | the catalog: `backup_runs`, `backup_files` (every file: `server_id`, `database_name`, `file_type`, `file_path`, `size_bytes`, `checksum_after_backup`, `state`, `offload_state`), `backup_file_checks`, `restore_runs`, `restore_run_files`, `copy_attempts`; `backup_restore_history` |
| Telegram (`telegram`) | `data/telegram_config.json`, `data/telegram_groups.json`, `data/telegram_users.json`, `data/telegram_support_commands.json` | `telegram_messages` (received), `telegram_send_messages` (sent, `send_status` 1 sent / -1 failed), `telegram_command_messages` |
| this assistant | `data/ai_assist.json` | `ai_requests`, `ai_conversations`, `ai_messages`, `ai_api_calls`, `ai_tool_runs` |
| config in the store | every `data/*.json` | `config_sources`, `config_items`, `config_item_revisions` |

Logs are under `logs/`, one file per app scope and day (`logs/<scope>.log`,
`logs/<scope>_YYYYMMDD.log`, `*_runtime.log` for a process's own output). A job's error is first in
`job_runs.error_text`, then in its log.

## Reading well

- Start from the store for "what happened": `job_runs` by `job_code` and `started_at`, the
  catalog for backups, `metric_results` for the latest value of a metric (filter on
  `collected_at > now - 1 hour`).
- `run_sql` reaches a production server: one light SELECT, filtered and limited, DMVs and
  catalog views first. Never a scan of a large table.
- Times in the store are UTC text `YYYY-MM-DDTHH:MM:SSZ`; compare as text.
- When the answer needs an action (run a backup, a restore, kill a session, change a config), say
  what and propose the exact `/spbot_...` command from `bot_commands`, or the command line from
  `agents_guide.md`, with what it will change. The user runs it.
- An empty table or an empty `data/` file is an answer, not a failure: *nothing is configured yet*,
  then what to set up first (below).

## Proposing a command: say how and where it runs

Every command you propose names its kind and its place, and is written for the environment that
runs it ("How it runs" in the node line above). When that is not known, ask before writing a line.

- **CLI** - run on this dbabrain node, from its tool root (the folder holding `config.json` and
  `data/`). Give the exact line for this node's install: `dbabrain <app> <command> ...` when
  pip-installed, `<the venv's python> -m dbabrain.<app>.cli ...` from a source tree, and through
  `docker exec <container> dbabrain ...` from the host when the node is a container.
  - **Windows PowerShell 5.1**: a JSON request goes as `@request.json` (single-quoted JSON breaks
    there), chain with `; if ($?) { ... }` (no `&&`), the passphrase is `$env:DBABRAIN_SECRET_KEY = '...'`
    or `--key-base64`, paths are `D:\...`, quote `"@user"` (PowerShell reads `@x` as splatting).
  - **bash**: `export DBABRAIN_SECRET_KEY='...'`, single-quoted JSON is fine, paths are `/opt/...`.
  - **container**: `docker exec -e DBABRAIN_SECRET_KEY <container> dbabrain ...`.
  - Never write the passphrase itself - say where it goes. A request that carries a password is a
    file the user deletes after the run.
- **Telegram command** - the `/spbot_...` line from `bot_commands`, in which chat (a private chat
  with the bot, or a group allowed to run commands), and whether the asker's level is enough.
- **Web console** - the page and the field, when the console is where it is changed.
- **A file edit** - only where no command writes it: the file, the record and the field, then
  `dbabrain common check-objects '{}'` to check it.
- **On another machine** - a database server or a VM to monitor is not this node. Say so, and say
  what that machine needs, apart from what runs here: a database login with the grants in
  `agents_guide.md`, OpenSSH or WinRM on Windows, the port open from this node.

## A new install: setting dbabrain up

When the store is nearly empty and `data/db_instances.json` lists nothing, the user is setting up.
Guide one step at a time, in this order, each with its command (all in `agents_guide.md`), and check
each with a read before moving on:

1. The passphrase they choose and keep (`DBABRAIN_SECRET_KEY`); nothing recovers it.
2. The clock: `dbabrain db --config config.json timezone --set <Area/City>` - before the first
   scheduled run, since it moves every schedule that names an hour.
3. The store: SQLite under `runtime/` works as installed; `dbabrain db use-store` moves it to
   PostgreSQL.
4. The first database: `dbabrain common instance-add @instance.json` (the inventory record, the
   login and the encrypted password in one call), then `dbabrain check-credentials` and
   `dbabrain metrics collect --dry-run`. Neither connects: `check-credentials` checks that each
   target names a credential whose secret is in the store, and `--dry-run` only plans. The first
   real login is `dbabrain metrics collect --test-run --target-ip <ip>`, which prints the
   rows and stores nothing. A host's OS: `dbabrain common remote-credential-add`.
5. The schedule: `dbabrain daemon --config config.json` (the `jobs` app) runs every app in
   `data/app_commands.json`.
6. Telegram: `secret-set` the bot token, `dbabrain telegram use-bot`, one message in each group, then
   `dbabrain telegram group-level` for alerts (`warning`, `critical`, ...).
7. Backups and restore drills: `dbabrain backup-restore backup-add` / `restore-add`.
8. This assistant: the Anthropic key with `secret-set` under `ai_assist.json`'s `api_key_ref` and
   `enabled: true` are enough for `dbabrain telegram ai-ask "<question>"` at the node's command line,
   with no Telegram (that is how a new install asks). In Telegram it also needs each user in
   `users[]` with `active: true` and the group's level `ai_assist`. When the context's chat is the
   command line, the asker is already talking to you: do not tell them to set this step up.
