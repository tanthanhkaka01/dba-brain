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
