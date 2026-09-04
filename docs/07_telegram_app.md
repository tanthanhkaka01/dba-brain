# Telegram App

## Purpose

The Telegram App sends pending queue rows through the Telegram Bot API, saves updates, and processes bot commands.

## Package / Files

- `db_ops/telegram/`
- `data/telegram_groups.json`
- `data/telegram_users.json`
- `data/bot_telegram.json`
- `data/telegram_support_commands.json`
- `data/telegram_support_commands.md`
- `assets/sql_telegram_commands/`
- `data/telegram_config.json` (Telegram settings; `config.json` points to it via `telegram_config_file`)

## Runtime Tables

- Reads/writes `telegram_send_messages`.
- Writes `telegram_messages`.
- Writes/updates `telegram_command_messages`.
- Reads/writes `telegram_conversation_states`.
- Reads/writes `telegram_background_tasks` (in-flight background `cli_execute` process tracking).
- Reads `reports` indirectly through compatibility report queue commands.

## Config Files

`data/telegram_config.json` controls Telegram enablement, API URL, timeout, bot token resolution, the group file path, and `level_chat_map` — routes that cannot be declared as a group (a private DM has a positive chat_id) plus any override of the group file; `config.json` only references it via the `telegram_config_file` key. A level routes iff it has a chat there or in the group file: no separate allow-list (the old `alert_levels` key is ignored). `data/bot_telegram.json` can provide bot identity and token secret references. `data/telegram_groups.json` maps notify levels to active chat IDs. `data/telegram_users.json` and `data/telegram_support_commands.json` control command permissions.

`data/telegram_support_commands.md` is the human/BotFather command menu source (paste its command block into BotFather `/setcommands`). The bot executes from `data/telegram_support_commands.json`, not from the menu file.

## Data Flow

Outgoing flow: app/report/command processor inserts `telegram_send_messages` with `send_status = 0` -> `send-queue` sends via Telegram Bot API -> row becomes sent or failed. `send-queue` reads a limited pending set for ordering, then calls `send-one` behavior per `send_tlgmsg_id`: mark one row processing, send one Telegram message, then mark only that row sent or failed. Do not send a whole list of messages and then update statuses in one batch.

Severity emoji: `db_ops.telegram.api.send_message` prefixes every outgoing body with one symbol so an alert is not missed in a wall of text - `▶️` started, `✅` success, `❌` failed, `⚠️` warning, `⏳` running, `🚨` critical/aborted. Producers never write the emoji into the text; they declare **what the message is** and the symbol is applied once at send time (`db_ops/telegram/severity.py`). A message that already leads with a status emoji (the SLA report writes its own) keeps it - tagging never stacks.

**`telegram_send_messages.message_type`** is that declaration: `started`, `success`, `failed`, `warning`, `running`, `critical`, or `plain`. Three states, and the difference between the last two matters:

| Stored | Meaning | Send layer |
| --- | --- | --- |
| one of the six | the producer says what this is | that emoji |
| `plain` | the producer says it carries **no** status (a command reply, a listing, a prompt) | no emoji, **and the header guess is switched off** |
| `NULL` | nobody said | falls back to reading the header |

`plain` is a statement, not an absence. A listing whose body happens to contain "error" or "running" would otherwise be tagged ❌ or ⏳ by a header rule that was never meant to judge it.

> **The column has to be in the SELECT.** `insert_telegram_send_message` wrote `message_type`, but
> `fetch_telegram_send_message` / `fetch_pending_telegram_send_messages` did not list it, so every
> row arrived at the send layer declaring nothing and silently fell back to the header guess — the
> whole table above was dead. It surfaced as the `/spbot_add_sql` schedule prompt going out as
> "❌ Schedule? ..." merely because its text contains the word "timeout", and it also meant the
> `[part 2/2]` report continuations this mechanism exists to tag were still going out bare.
> `row_value()` returns `None` for a missing column instead of raising (so an un-migrated store
> still delivers), which is why nothing failed loudly. Regression test:
> `tests/test_telegram_severity_emoji.py::test_a_queued_row_keeps_its_declared_message_type_when_it_is_read_back`.

Every app queues through **`db_ops.common.telegram_queue.queue_telegram_message`** - one entry point, so the vocabulary cannot fork per app. Callers rarely hold a display type; they hold a `level` (`logging`/`warning`/`error`/`critical`) and often a `phase` (`START`/`END`/`ERROR`) or a status, and `message_type_for()` maps those once. `DbOpsStore.insert_telegram_send_message` stays public for the store's own use and for tests; application code should not call it directly.

Two rules decide the mapping, and both exist because of a way it went wrong:

* **Phase before level**, because level cannot separate a start from a success - both are `logging`.
* **A loud level still overrules an optimistic phase.** `phase=END, level=error` is a run that finished by failing: it reports ❌, never ✅.

`logging` maps to **`plain`**, not to nothing. A producer routing at logging level has *said* this is routine; leaving it unset would mean "nobody knows" and send the layer back to guessing. That gap is why every routine report initially stored `NULL`.

The status words each app actually concludes with are mapped centrally - `done`/`OK`/`PASSED` → success, `error`/`FAILED` → failed, `AT_RISK` → warning, `NO_DATA` → warning (an SLI that could not be computed is not a pass), `ABORTED` → critical. Adding a producer means adding its vocabulary to `_PHASE_TYPES`, not inventing a private mapping in that app. The notify-level words are also accepted as declared types (`error`→failed, `warn`→warning, `logging`→plain), since that is the vocabulary the rest of the config uses.

**CLI** - for a shell script, a scheduled command, or another language. The request is a JSON object, inline, as `@file`, or on stdin, matching `run-sql`:

```bash
python -m db_ops.db.cli queue-telegram-message \
  '{"chat_id": "-1001234567890", "text": "RESTORE FAILED on ACME-...", "message_type": "failed"}'

echo '{"chat_id": "-100...", "text": "Daily summary", "level": "logging"}' \
  | python -m db_ops.db.cli queue-telegram-message -
# -> {"ok": true, "send_tlgmsg_id": 14426, "message_type": "plain"}
```

It echoes the type that was **stored**, so a caller passing a level or a status sees what it resolved to instead of reading the row back. Fields: `chat_id` and `text` (required), then `message_type` or any of `level`/`phase`/`status`, plus `note`, `source_type`, `source_id`, `reply_message_id`, `metadata`. A JSON object rather than flags because a message body carrying quotes, newlines or a leading dash survives it without shell quoting games.

The column is nullable and was never backfilled, so existing rows and any not-yet-migrated producer keep the header heuristic. That heuristic remains the fallback only - it is why a report's `[part 2/n]` continuation chunks used to go out untagged (they start mid-body with no header), which storing the level for every chunk fixes.

Incoming flow: Telegram `getUpdates` -> `telegram_messages` -> command prefix sync into `telegram_command_messages` -> permission/action processing -> optional SQL/CLI execution -> reply rows in `telegram_send_messages`. Command processing follows the same per-row rule: `process-commands` reads pending rows for ordering, then calls `process-one-command` per `telegram_command_message_id` and updates `command_status` for that single command message only.

## Long messages are split, never cut (2026-08-13)

Telegram rejects a body over 4096 characters, and db_ops used to answer that by clipping in four
different places: the SQL task table stopped at 20 rows with `… N more row(s)`, `ops_status` cut at
3880 with `... (truncated)`, `backup_restore.events` cut its JSON payload at 3900, and
`send_message` chopped whatever was still too long. Only the metrics reports split properly, into
`[part i/n]` chunks.

All of it is now one implementation, `db_ops/lib/telegram_text.py`, and it **splits**:

- `send_message` applies it to every outgoing body, so **every producer inherits it** — nothing has
  to know about the limit.
- Clipping is the wrong trade because the reader cannot tell whether what they needed was in the
  part that got dropped, and usually it was: the rows falling off a `/spbot_run_sql_task` result
  were the rows somebody ran the task to see.
- How much output is reasonable is **the query's** business (`TOP` / `LIMIT` in the SQL), not the
  transport's — it cannot know which half matters.

Two details that are load-bearing:

- **Split happens before decoration.** The severity emoji goes in front of the `[part i/n]` marker,
  and `telegram_severity` reads that marker to tell a first chunk from a continuation — a
  continuation starts mid-body where "running" is a column in a lock dump, not a status.
- **The reply quote goes on the first part, buttons on the last**, and the queue row records the
  *first* part's `message_id`, so a reply quotes where the output starts rather than its tail.

A producer that needs the seams in specific places still calls `split_telegram_message` itself —
the metrics reports do, because each chunk is queued as its own row and so carries its own level.
If a later part fails to send, `send_queue` retries the whole row and re-sends the parts that
already landed; duplicated output on a rare failure beats a result with a silent hole in it.

## How to Run

```powershell
python -m db_ops.telegram.cli --config config.json get-updates --limit 20
python -m db_ops.telegram.cli --config config.json save-updates --limit 20
python -m db_ops.telegram.cli --config config.json save-commands
python -m db_ops.telegram.cli --config config.json process-commands --limit 50
python -m db_ops.telegram.cli --config config.json process-conversations --limit 50
python -m db_ops.telegram.cli --config config.json send-queue
python -m db_ops.telegram.cli --config config.json send-one --send-tlgmsg-id 1
python -m db_ops.telegram.cli --config config.json run-workflow
```

Validate command JSON and run focused tests:

```powershell
python -m json.tool data/telegram_support_commands.json
pytest tests/test_telegram_command_permissions.py
```

## Useful Manual Queries

```sql
SELECT send_tlgmsg_id, row_ins_date, tlgchat_id, send_status, send_date, message_id, note
FROM telegram_send_messages
ORDER BY row_ins_date DESC, send_tlgmsg_id DESC
LIMIT 50;

SELECT telegram_command_message_id, message_date, chat_id, user_id, command_payload, command_status, process_note
FROM telegram_command_messages
ORDER BY message_date DESC, telegram_command_message_id DESC
LIMIT 50;

SELECT state_id, chat_id, user_id, command_text, state_key, status, created_at, updated_at
FROM telegram_conversation_states
ORDER BY created_at DESC, state_id DESC
LIMIT 50;

SELECT task_id, chat_id, user_id, pid, status, created_at, completed_at, stdout_path, stderr_path
FROM telegram_background_tasks
ORDER BY created_at DESC, task_id DESC
LIMIT 20;
```

## Manual Command Setup

Add or change commands in this order:

1. Update `data/telegram_support_commands.md` with BotFather-compatible entries such as `spbot_status - Get bot status`.
2. Paste that list into BotFather `/setcommands` for the bot.
3. Add or update runtime command metadata in `data/telegram_support_commands.json`.
4. If `action_type = "sql_execute"`, place the SQL file under `assets/sql_telegram_commands/` and use `?` placeholders. Do not use `GO` in these SQL files.
5. Validate JSON and run the command tests.

Important command fields include `command_id`, `command_text`, `command_type`, `is_group`, `is_private`, `reply_default`, `reply_text`, `action_type`, `action_config`, and `node_role`.

### Cluster routing — `node_role`

Like `app_commands.json`, each support command carries a `node_role`: `master`, `worker`, or `all`. The processing node handles only commands for its role — a **master** node runs `master`/`all`, a **worker** node runs `worker`/`all`. A command tagged for the other node is skipped (status `skipped_wrong_node_role`, left for that node), not replied to as "unknown". The node's own role comes from the `DB_OPS_NODE_ROLE` env and **defaults to `worker`** when unset (the Telegram workflow `APP-TELEGRAM` runs worker-side with `DB_OPS_NODE_ROLE=worker`), so a command with **no** `node_role` defaults to `worker` and keeps being handled exactly as before. Truly undefined `spbot_*` commands (not in the JSON) still get the normal "unknown command" reply.

## Action Types

### `sql_execute`

Runs a SQL file against a target database. Parameters are injected as positional `?` placeholders. Do not use `GO` separators.

### `cli_execute`

The command `/spbot_report_metric_history <server_id> <metric_code> <hours>` calls the Reports App's store-local `metric-history-report` CLI. For example:

```text
/spbot_report_metric_history ACME-192-0-2-108 SYSTEM_CPU_MEMORY 24
```

The three arguments select one exact server/metric pair and a UTC window from `now - hours` through `now`. The Reports App reads the matching stored samples and queues the result at Telegram's `logging` level. This command does not collect metrics; when an argument is omitted, the standard multi-step conversation prompts for it.

Runs a configured CLI command from `command_template` or `command_argv`. Values come from `defaults` merged with conversational `parameters`. `conditional_args` can append arguments based on parameter values. Set `background` or `detached` to `true` for long-running commands; otherwise execution remains synchronous. Start/success/failure/timeout messages are driven by generic templates in `action_config`. For a background command that records its own authoritative outcome in the runtime store (e.g. restore), add a `completion_probe` so the poller reports the real result from `job_runs` instead of relying only on the detached process staying alive and its stdout marker.

This is how the Telegram app runs **other apps without coupling to them**: it spawns the target app's CLI as a subprocess and **never imports it or reads its config**. (Since 2026-08-15 that includes `common` itself: `spbot_add_sql`, `spbot_metric_toggle`, `spbot_xlsx_to_table` and `spbot_sql_to_xlsx` used to import their engine and call it in-process, and now run `python -m db_ops.common.cli add-sql` / `metric-toggle` / `create-table-from-xlsx` / `run-sql` with one JSON object — the same command an operator types. What the bot still does in-process is *read* `db_instances.json` through `data_sources`, the one reader of the data folder, because the operator is never asked for the db_type, instance or credential the request needs. See `docs/05_sql_task_runner.md` and `docs/13_common.md`.) `success_text` may include `{stdout}` to return the command's output verbatim. Example — `spbot_list_restore_id` (no parameters) runs `db_ops.backup_restore.cli list-restores` and replies with `{stdout}` (the restore IDs plus source/target IPs); the backup restore app owns and reads `restore_config.json`, the Telegram app does not. To expose another app's data through the bot, add a CLI subcommand to that app and call it here — do not import the app or read its config files.

`spbot_restore` uses `cli_execute`, following the same generic CLI command pattern as command ID 4. Its required parameters are collected through the conversational flow:

| position | name | notes |
|---|---|---|
| 1 | `restore_id` | Key in `data/restore_config.json` — e.g. `ACME_TO_SQLSERVER_192_168_18_31` |
| 2 | `point_in_time` | Send `LATEST` for the newest backup, or `YYYY-MM-DD HH:MM:SS +HH:MM` for PITR |

`reply_default` is `0`; the generic CLI action queues the configured messages. `spbot_restore` enables background execution. Its conditional arguments omit `--point-in-time` for `LATEST` and append it for a timestamp. Command ID 4 remains synchronous because it does not enable background execution.

**Completion detection (`completion_probe`).** A restore into a container can finish server-side while the dispatched workflow process still lingers, so relying on "process alive + stdout marker + hard `timeout_seconds`" wrongly reported a **timeout** for restores that actually succeeded. `spbot_restore` therefore configures a `completion_probe`: on each poll cycle, `check_cli_background_tasks` looks up `job_runs` for a terminal record whose `job_code` matches the probe (`backup_restore.restore-workflow.end` = success, `.error` = failure) and whose `metadata_json` matches `match_metadata` (`restore_id`), created at/after the task start. The store is authoritative — the newest matching record wins, and if found the poller stops any lingering process and reports the real success/failure immediately, never waiting out the timeout. The timeout still applies only when no terminal record exists yet.

Example JSON entry:

```json
{
  "command_id": 5,
  "command_text": "spbot_restore",
  "reply_default": 0,
  "command_type": 2,
  "is_group": 1,
  "is_private": 1,
  "action_type": "cli_execute",
  "action_config": {
    "working_dir": "tools/db_ops",
    "command_argv": ["{python}", "-m", "db_ops.backup_restore.cli", "restore-workflow", "--config", "{config_path}", "--restore-id", "{restore_id}"],
    "conditional_args": [
      {"parameter": "point_in_time", "not_equals": "LATEST", "argv": ["--point-in-time", "{point_in_time}"]}
    ],
    "background": true,
    "detached": true,
    "start_text": "Restore workflow started for {restore_id}. point_in_time={point_in_time}",
    "success_text": "Restore workflow completed for {restore_id}. point_in_time={point_in_time}",
    "failure_text": "Restore workflow failed for {restore_id}. point_in_time={point_in_time}\nExit code: {exit_code}\nError: {error_summary}",
    "timeout_text": "Restore workflow timed out for {restore_id}. point_in_time={point_in_time}. Exceeded {timeout_seconds}s.",
    "success_output_contains": "restore-workflow completed status=SUCCESS",
    "completion_probe": {
      "table": "job_runs",
      "success_job_code": "backup_restore.restore-workflow.end",
      "failure_job_code": "backup_restore.restore-workflow.error",
      "match_metadata": {"restore_id": "{restore_id}"}
    },
    "timeout_seconds": 7200,
    "parameters": [
      {"name": "restore_id", "source": "arg", "position": 1, "required": true, "prompt_text": "Please input restore_id"},
      {"name": "point_in_time", "source": "arg", "position": 2, "required": true, "consume_rest": true, "validator": "regex", "pattern": "^(LATEST|\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2} [+-]\\d{2}:\\d{2})$", "prompt_text": "Please input point_in_time: LATEST or YYYY-MM-DD HH:MM:SS +HH:MM"}
    ]
  }
}
```

`"working_dir": "tools/db_ops"` is the **logical alias for the tool root**, not a folder in
this repository — it resolves to the repository root locally and to `/app/tools/db_ops` in the
worker container, so the same config works on both. See
[03_app_command_daemon.md](03_app_command_daemon.md#working_dir-and-the-toolsdb_ops-alias).

## Multi-Step Conversation (Parameter Chaining)

When `action_type` requires more than one parameter and the initial command message does not include all arguments, the bot collects them one at a time across multiple messages:

1. Command message arrives (`/spbot_restore`) — processor detects first missing parameter, queues prompt, creates `telegram_conversation_states` row with `status = 'waiting'`.
2. User replies — `process-conversations` reads the reply, stores the value in the state's `args` array, checks for the next missing parameter. If one exists, it marks the current state `done`, queues the next prompt, and creates a new `waiting` state. If all parameters are collected, the action executes immediately.
3. Cycle repeats until all parameters are filled.

Important ordering rule: the current `waiting` state is updated to `done` **before** `upsert_telegram_conversation_state` is called for the next parameter, because the upsert first sets all existing `waiting` rows for the chat/user to `replaced`.

Only a **required** parameter is prompted for. Two rules bend that, both declared in
`action_config` rather than in code, because whether a question applies can depend on an answer
already given:

| Rule | Effect |
| --- | --- |
| `"skip_when": {"condition": "target_has_no_database", "parameter": "target_ip", "value": "-"}` | Does **not** ask a required question that cannot apply — an OS-only host has no db_type or port — and fills the value instead. |
| `"prompt_when": {"condition": "sql_task_has_parameters", "parameter": "sql_id"}` | **Does** ask an optional question that this run needs. `/spbot_run_sql_task`'s `task_params` is optional because most tasks declare none; a task that *requires* one was therefore run with none and failed telling the operator to pass a `--param` they were never asked for. The condition holds when the sql_id already answered names a task declaring parameters — asked of the sql_tasks app through `python -m db_ops.sql_tasks.cli list-tasks --sql-id N`, never by reading its config. |

A prompt may contain `{sql_task_parameters}`, replaced with the names that task declares — the
operator picked a task by number, so "the parameters this task declares" is not something they
can answer without being told. Answering `-` means "no values": Telegram cannot send an empty
message, and `-` is the same sentinel `skip_when` fills in.

### A prompt that lists the answers (`prompt_choices`, 2026-08-17)

A parameter may declare `prompt_choices`, and the prompt then arrives with the values that
parameter will actually accept listed under it:

```json
"prompt_choices": {"command": "list-schemas", "data_key": "schemas",
                   "request": {"target": "{server_id}", "database": "{database}"}}
```

`command` is a `common` CLI command, `data_key` names the list inside its `data`, and each entry's
`name` is what gets shown. `{parameter_name}` in the request is filled from the answer **already
given** for that parameter — which is what lets the steps chain: `/spbot_xlsx_to_table` asks for a
server, then lists that server's databases, then lists the chosen database's schemas.

| Prompt | Runs |
| --- | --- |
| Database name? | `list-databases {"target": "<server_id>"}` |
| Schema? | `list-schemas {"target": "<server_id>", "database": "<database>"}` |

Two properties are deliberate and both are held by `tests/test_telegram_prompt_choices.py`:

* **The listing is an aid, never a gate.** Every way of failing to produce one — unreachable
  instance, wrong credential, an engine `list-schemas` does not know, a timeout, a response
  without the key — sends the **bare prompt** instead. A prompt is the only thing that keeps the
  conversation moving, and typing the name has always worked. A listing feature that can leave the
  flow with no question asked would be worse than no listing.
* **The list is capped at 40** (`lib.listing.MAX_PROMPT_CHOICES`) and says how many it left out.
  The estate has instances with well over a hundred databases against a 4096-character message
  limit; a list that silently stopped would tell the operator their database does not exist.

The command runs with a 25 s deadline of its own, because it opens a connection to the server
while the operator is waiting.

### Answering a prompt with a file

A parameter may declare `"accept_file": true`, and a reply that carries a document instead of
text is then downloaded and used as the value. `"file_encoding"` says how to turn the bytes into
one:

| `file_encoding` | Used by | What the action receives |
| --- | --- | --- |
| absent (default) | `/spbot_add_sql`, `/spbot_sql_to_xlsx` — a `.sql` body | the file decoded as `utf-8-sig`, stripped |
| `"base64"` | `/spbot_xlsx_to_table` — a workbook or a text file | the raw bytes, base64-encoded |

The distinction is not cosmetic. A `.xlsx` is a zip; decoding it as text either raises or, on a
lenient codec, *succeeds* and hands the action something that is no longer the file. base64 is
also exactly what a JSON request can carry, so the value drops straight into the `common` CLI
payload and the Telegram path and the shell path cannot drift. It is also why
`/spbot_xlsx_to_table` can take a text file without a second parameter: the action gets bytes and
decides for itself what they are.

The download itself is **not size-capped by db_ops** (the 1 MB cap was removed on 2026-08-15).
Telegram's own 20 MB ceiling on what a bot may fetch still applies and is reported as Telegram's.

### `/spbot_xlsx_to_table`

`action_type = "create_table_from_xlsx"`. Four prompts — **server_id → database → schema → the
file** — then `db_ops.common.table_load.create_table_from_xlsx` runs with the same JSON object
`python -m db_ops.common.cli create-table-from-xlsx` takes.

Two decisions are worth knowing because they are deliberate and easy to "fix" wrongly:

- **`table_name` is not prompted for.** Blank generates `temp_<random>`, and the reply names it.
  The common case is "I need this queryable now", and one more prompt between an operator and
  the thing they wanted is a step at which people give up. A fifth word on the command line
  chooses a name.
- **`if_exists` is left at the module default `error`.** It is *not* pinned to `drop` in
  `action_config`, so re-sending a corrected file cannot destroy the previous table from a chat
  message with no confirmation. A deployment that wants otherwise sets it in `action_config` —
  the config is the one place that decides.

Every column is created `NVARCHAR(4000)` (or the engine's equivalent); see
`db_ops/common/table_load.py` for why a guessed type is worse. Private chat only.

**The attachment does not have to be a workbook.** An `.xlsx` *or* a delimited text file —
`.txt`, `.csv`, `.tsv`, a block selected in Excel and pasted into Notepad — is accepted, and
which one it is comes from the file's first bytes, never from its name (a workbook is a zip and
starts `PK\x03\x04`; a renamed file is routine). For a text file the encoding and the delimiter
are guessed — UTF-16 with or without a BOM, UTF-8, the Windows codepage; tab, semicolon, comma or
pipe counted on the header line — and the reply says which was used, because a wrong delimiter
still produces plausible-looking columns. A row carrying *more* values than the header names is
refused with its line number rather than clipped. `db_ops/lib/delimited_import.py` has the
detail; `"delimiter"` in `action_config` overrides the guess.

**There is no db_ops size limit on the attachment.** There was a 1 MB cap until 2026-08-15, which
refused the ordinary 5 MB export this command exists for. What remains is Telegram's own: the Bot
API serves a bot at most **20 MB** per file however large the upload was, and no setting here can
lift it — `get_file_bytes` says so in those words rather than passing "file is too big" through.
Above that, put the file where the worker can read it and call the `common` CLI with
`"file_path"`. Row count is capped at `max_rows` (default 1,000,000), and the reply says when it
bit.

Permission rules:

- Private chats require a known user in `data/telegram_users.json`.
- Group chats require a known group in `data/telegram_groups.json`.
- Admin commands require admin-level user permission.
- Newly discovered groups/users default to no command permission.

## Commands that are confirmed (clearance 50 and 100)

Five of these exist so an incident can be handled from a phone rather than from a workstation;
the sixth is not an incident command and is confirmed for its own reason, below:

| Command | Operation | Clearance | Confirmation |
| --- | --- | --- | --- |
| `/spbot_shrink_log` | `shrink-log` | 50 | one `yes` |
| `/spbot_kill_spid` | `kill-spid` | 50 | one `yes` |
| `/spbot_start_job` | `start-job` | 50 | one `yes` |
| `/spbot_disable_job` | `disable-job` | 50 | one `yes` |
| `/spbot_run_sql_task` | `run-sql-task` | 50 | one `yes` |
| `/spbot_restart_server` | `host-restart` | 100 | `yes`, then the **server id typed out** |

`/spbot_run_sql_task` is not an incident command, and it is on this list anyway: a forced run
skips the schedule *and the active flag*, so it writes to production outside every window that was
agreed. It is the case that shows why the two questions are separate — it was raised to clearance
50 on 2026-09-04 and still cost nothing to run, because clearance answers who may ask and nothing
else.

Where its `yes` sits is worth reading before adding a confirmation to any other command. The
command already had a `consume_rest` parameter (`task_params`, the values the task itself
declares), and a `consume_rest` value is `" ".join(args[position - 1:])` — everything from its slot
to the end of the message. So a confirmation **behind** it is also *inside* it: the word `yes`
would be appended to the parameters the task runs with. In front of it, both readings are
unambiguous:

```text
/spbot_run_sql_task 24 yes 2026-09-01 2026-09-02     one line: <sql_id> yes <the task's own values>
/spbot_run_sql_task 24                               or one question at a time: values, then yes
```

An answer that is not `yes` is refused before anything starts, so the old habit —
`/spbot_run_sql_task 24 2026-09-01` — stops rather than running with a date where the confirmation
belongs. This is the same collision that made an earlier attempt at a confirmation here get
removed; the note on `sql_id` 24 in `sql_commands.json` records it.

They are ordinary `cli_execute` commands: the parameter chaining above collects the arguments *and*
the confirmation answers, one prompt at a time, then passes them to the CLI that performs the
operation — as a JSON object for the five `common` commands, as `--confirm yes` for the SQL task
runner. Nothing about the safety model is specific to Telegram: every one of them reads its answer
through `db_ops.common.confirm`, so a shell caller pays exactly the same price.

**The confirmation is enforced in the CLI, not here.** `data/telegram_support_commands.json`
decides who may ask (`command_type`) and what the prompts say;
[`data/emergency_operations.json`](../data/emergency_operations.example.json) decides what the answers must
be, and `db_ops/common/confirm.py` checks them. Running
`python -m db_ops.common.cli host-restart '{...}'` from a shell costs exactly the same two answers.
An operation that is missing from `emergency_operations.json` gets the strictest treatment, not the
weakest — a command added to the CLI and forgotten in the config becomes harder to run, never
easier.

The answers travel in the request rather than being typed at a terminal, because there is no
terminal on a phone:

```json
{"target": "ACME-192-0-2-115", "confirm": "yes",
 "confirm_target": "ACME-192-0-2-115",
 "authorized_by": {"channel": "telegram"}}
```

That is not a bypass of the prompt — it *is* the prompt, asked over Telegram. It is also distinct
from `"assume_yes": true`, which stays available for genuinely unattended automation and is recorded
in the evidence as such, so a run nobody watched never reads afterwards like a run somebody
approved.

Why the second answer at level 100 is not a second `yes`: two identical answers in a row are one
answer typed twice. Reproducing the target's own id has to be read off the prompt, and it makes a
message written for one host fail against another.

Each command reports what it found **before** it asks — the log file's size and `log_reuse_wait`,
the session's login and transaction age and how many sessions are blocked behind it, the job's
current run state. A prompt that says "kill 723?" when 723 is already gone teaches an operator that
the prompt is noise.

## Common Issues

- Pending queue is not sending: check `telegram.enabled`, bot token resolution, `tlgchat_id`, and `send_status`.
- Commands are saved but not processed: inspect `command_status` and `process_note`.
- Command not found: keep `/spbot...` text, BotFather menu, and `telegram_support_commands.json` aligned.
- SQL command fails: confirm SQL file path, target credentials, placeholder count, and no `GO` batch separators.

## Config Priority

The Telegram app resolves its config file using this chain:

1. `--config <path>` CLI argument.
2. `DB_OPS_TELEGRAM_CONFIG` environment variable.
3. `config.telegram.json` next to `config.json`, or in the current working directory.
4. `config.json` shared fallback.

The selected source is printed to stderr on startup. The `update_offset` for `save-updates` and `run-workflow` is read from and written back to the resolved config file.

App-specific config file: `config.telegram.json`

## Standalone Mode vs Full-Suite Mode

**Full-suite mode** (default): the bot reads `config.json`, shares the runtime store with all other apps, and picks up `telegram_send_messages` rows written by any of them.

**Standalone mode**: copy `config.telegram.json` and `data/telegram_config.json`, `data/bot_telegram.json`, `data/telegram_groups.json`, `data/telegram_users.json`, `data/telegram_support_commands.json` next to the EXE. Point the store at the shared database. The `send-queue` command only delivers rows from apps that write to that same store.

Required keys: in `data/telegram_config.json` — `enabled` and the bot token (via `bot_token`, env var `TELEGRAM_BOT_TOKEN`, or the encrypted secret file); in the config — `log_dir`, a resolvable runtime store, and `telegram_config_file` pointing at the Telegram settings.

## Optional Integrations

**`queue-metrics-reports` command**: calls `db_ops.reports.cli queue-metrics-reports` as a subprocess, passing `--config <resolved_config_path>` so the reports app resolves its own config from the correct location. If the reports app is absent or returns a non-zero exit code, this command raises `RuntimeError`. The send queue (`send-queue`) and all other commands are unaffected.

**`telegram_send_messages` rows from other apps**: `send-queue` delivers any row in the table regardless of which app wrote it. If no other apps are running, the queue is simply empty and `send-queue` exits cleanly.

**Bot command SQL execution**: `process-commands` for `action_type = "sql_execute"` requires database credentials accessible from `data/telegram_support_commands.json`. If credentials or SQL files are missing, the individual command fails with an error reply; the bot continues processing other commands.

## EXE Packaging Notes

- The `update_offset` is written back to the config file after each `save-updates` or `run-workflow` run. The resolved config file must be writable.
- SQL telegram command files must be co-located or reachable; paths in `telegram_support_commands.json` are relative to the data directory.

