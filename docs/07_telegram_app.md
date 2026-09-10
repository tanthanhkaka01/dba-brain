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
- Writes `telegram_workflow_steps` — one row per **asked** step: the prompt, the options offered,
  the answer, and which step of a run is live. See [Back, Skip and Cancel](#back-skip-and-cancel-2026-09-09).
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

### The caller's own identity — `{chat_id}` and `{user_id}`

Besides the parameters a person types, `command_argv` can name two values nobody supplies: the
chat that asked and the person who asked. `{chat_id}` exists so a command whose result is a
*deliverable* — an xlsx from a SQL task — can send the file back where it was requested instead of
to the target's configured notify chat. `{user_id}` exists for the opposite kind of command, one
whose answer is *about the caller*: `/spbot_list_my_commands` reads that person's own history, and
taking the person as an argument would let anyone read anyone's by typing a number.

Both are set from the message being processed, never from `defaults` and never from an argument.
`{user_id}` is always set, empty when the message carries no sender (a channel post): an absent
key would leave the literal `{user_id}` standing in the argv, and a CLI handed that would search
for a person by that name and report an empty history rather than saying it does not know who is
asking.

### `/spbot_list_my_commands`

One person's own last 10 distinct commands, each written back as the single line that runs it
again. Runs `db-ops db telegram-command-history` like any other `cli_execute` command, and the
work — the join, the rebuild, the rendering — is `db_ops.common.telegram_command_history`, which
does not know what a bot is.

The reason it is not simply "the last ten messages" is the [prompt
chain](#multi-step-conversation-parameter-chaining) below: a command answered one question at a
time leaves a message saying `/spbot_run_sql_task` and nothing else, with `18` and `0 30` as
separate messages further down that look like ordinary conversation. The arguments live in
`telegram_conversation_states`, positionally, and joining them back in that order produces
`/spbot_run_sql_task 18 0 30` — one line to copy.

**Private chat only** (`is_group: 0`). The history spans every chat this person has used the bot
in, so answering it in a group would read a private command out loud there.

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

Only a **required** parameter is prompted for — plus an optional one that declares
`allow_skip: true` (see [the step schema](#the-step-schema-2026-09-09)). Three rules bend that,
all declared in `action_config` rather than in code, because whether a question applies can depend
on an answer already given:

| Rule | Effect |
| --- | --- |
| `"skip_when": {"condition": "target_has_no_database", "parameter": "target_ip", "value": "-"}` | Does **not** ask a required question that cannot apply — an OS-only host has no db_type or port — and fills the value instead. |
| `"ask_when": {"parameter": "remote_auth", "equals": "secret_ref"}` | **The general one (2026-09-09).** Asks the step only when a named earlier answer matches. Unlike the two below it takes no hardcoded condition name, so a new branch is config rather than Python. |
| `"prompt_when": {"condition": "sql_task_has_parameters", "parameter": "sql_id"}` | **Does** ask an optional question that this run needs. `/spbot_run_sql_task`'s `task_params` is optional because most tasks declare none; a task that *requires* one was therefore run with none and failed telling the operator to pass a `--param` they were never asked for. The condition holds when the sql_id already answered names a task declaring parameters — asked of the sql_tasks app through `python -m db_ops.sql_tasks.cli list-tasks --sql-id N`, never by reading its config. |

A prompt may contain `{sql_task_parameters}`, replaced with the names that task declares — the
operator picked a task by number, so "the parameters this task declares" is not something they
can answer without being told. Answering `-` means "no values": Telegram cannot send an empty
message, and `-` is the same sentinel `skip_when` fills in.

### Answering everything in one message (the inline form)

The conversation is a **fallback, not a toll gate**. Every parameterised command can be run in one
message, and only what is still missing is asked:

```
/spbot_run_sql_task 18 0 30      -> nothing missing, runs immediately, no prompt at all
/spbot_run_sql_task 18           -> position 1 filled, the bot asks the rest
/spbot_run_sql_task              -> asks from the first question
```

Arguments fill **positions**, in order, split with `shlex.split` (so a value with a space must be
quoted — see [Spaces in an answer](#spaces-in-an-answer--what-actually-decides-2026-09-09)). The dispatcher then looks for the first missing answer exactly as
it would mid-conversation, which is why a partly-typed command resumes where it stops rather than
starting over.

**All of them are recorded.** Answers typed ahead of the question are written to
`telegram_workflow_steps` as `answer_kind='inline'` before the run proceeds, so a command answered
entirely in one message still has a full step trail — with the same masking for `secret` steps.
Without that, the trail would begin at the first *prompted* step and silently drop everything the
operator had already supplied.

#### Copy-paste a whole command: one step, one word

This is the case the no-space rule is really about. Answering the prompts one by one, a space is
harmless — the reply is taken whole. **Pasting the whole command in one message is where a space
becomes a second argument**, and every later parameter shifts by one:

```
/spbot_add_sql SRV my daily report ...     -> "my", "daily", "report" are three arguments
/spbot_add_sql SRV "my daily report" ...   -> one argument, but nobody types quotes in a chat
```

So a command meant to be copy-pasted should answer every step with an id, a code or a `choice`
value. A step that legitimately needs free text with spaces goes **last** and is declared
`consume_rest: true`; a multi-word answer anywhere else still works when it is *answered at the
prompt* (it is quoted when the command is written back), and only breaks when someone types the
command inline without quotes. The full table is in
[Spaces in an answer](#spaces-in-an-answer--what-actually-decides-2026-09-09).

#### A long SQL body, and anything else full of spaces

Three ways, and the first two are the ones to use:

| How | What happens |
| --- | --- |
| **Answer the prompt** with the SQL as one message | The reply is stored **verbatim** — newlines, quotes, indentation. Nothing splits it. This is the normal path for `/spbot_add_sql` and `/spbot_sql_to_xlsx` |
| **Attach a `.sql` file** in reply to the prompt | The parameter declares `accept_file: true`; the document is downloaded and its text becomes the value. Best for anything long, and it survives Telegram's 4096-character message limit |
| Paste it inline after the command | Works, because the parameter is `consume_rest: true` — see the note below on what that had to be taught |

`consume_rest` means the parameter takes **everything from its position onward**, which is why it
may only be the last one. It is how `sql_text` (`/spbot_add_sql` position 5, `/spbot_sql_to_xlsx`
position 2) and `task_params` (`/spbot_run_sql_task` position 3) are declared.

> **Fixed 2026-09-09.** That tail used to be rebuilt by joining the `shlex` tokens with single
> spaces, which mangled pasted SQL twice over and said nothing either time:
>
> | Pasted | Reached the CLI as |
> | --- | --- |
> | `WHERE name = 'Tan Thanh'` | `WHERE name = Tan Thanh` — shlex ate the quotes |
> | `SELECT id`<br>`-- only the active ones`<br>`FROM users WHERE ok = 1` | `SELECT id -- only the active ones FROM users WHERE ok = 1` — flattened to one line, so the comment swallowed the query and it ran as `SELECT id` |
>
> A `consume_rest` tail is now taken from the raw message text
> (`split_with_verbatim_tail`), so quotes, line breaks and indentation arrive exactly as typed.
> Head arguments are still read with quotes honoured, because `render_command_line` writes them
> that way. Pinned by `tests/test_telegram_command_line_round_trip.py`.

**A binary body cannot go inline at all.** `/spbot_xlsx_to_table`'s `xlsx_base64` declares
`accept_file: true` with `file_encoding: "base64"`: a spreadsheet is a zip, and decoding it as
text either raises or silently corrupts it. Attach the file.

#### Positions count the steps a branch will not ask

This is the one thing `ask_when` does **not** simplify, and it is worth knowing before typing a
long command:

| | |
| --- | --- |
| In a conversation | only the questions this run needs are asked — `remote_auth: password` is followed straight by the password |
| On the command line | every parameter still occupies its **defined** position, including the branches this run skips |

```
/spbot_demo_flow lab01 password hunter2-hunter2      ❌ the password lands in the skipped
                                                        password_ref slot, is discarded with it,
                                                        and the bot asks for the password again
/spbot_demo_flow lab01 password - hunter2-hunter2    ✅ the placeholder holds the skipped position
```

The failure is quiet — a value that went into a slot the branch discards looks exactly like a
value nobody gave — so for a branched command with many steps, **answering the prompts is the
reliable form** and the inline form is for short, familiar commands.

> `/spbot_create_db_docker` was renumbered on 2026-09-09 when `remote_auth` was inserted at
> position 10. Any inline invocation of it written before that date has its later arguments
> shifted by one and must be retyped — or, better, answered through the prompts.

### Spaces in an answer — what actually decides (2026-09-09)

**Nothing in the config says "this step must be one word".** There is no such field and there is
deliberately none: measured on this estate, **14 of 21** registered task names contain spaces
(`TANS Employee mapping`, `Deploy Material WareHouse INT-185`), they are answered at a prompt every
week, and they work. What decides is *where* the answer arrives and *which* field the step
declares:

| Field in `telegram_support_commands.json` | Effect | Read by |
| --- | --- | --- |
| `consume_rest: true` | This parameter takes **everything from its position onward**, spaces and newlines included. Only allowed on the last parameter | `consume_rest_position` / `command_args_from_text` (`command_processor.py:398`), and the argv builder (`:3329`) |
| `accept_file: true` | The answer may be a **document** instead of text; the file's contents become the value | the conversation loop (`command_processor.py:212`) |
| `file_encoding: "base64"` | That document is **binary** (a spreadsheet), carried as base64 rather than decoded as text | `command_processor.py:219` |
| `options` + `allow_text_input: false` | A closed list, so the answer is one of the values — which are themselves single tokens, guarded by a test | `db_ops/lib/workflow_steps.py` |

All four are data. Nothing about any particular command is hard-coded.

**Where a space is harmless, and where it is not:**

| | Multi-word answer |
| --- | --- |
| Answering a **prompt** | ✅ kept verbatim — the reply is one message and is stored whole |
| A **middle** argument, replayed from `/spbot_list_my_commands` | ✅ `render_command_line` quotes it, and the reader honours quotes |
| The **last** argument when it is `consume_rest` | ✅ taken verbatim from the raw message |
| The **last** argument when it is **not** `consume_rest` | ❌ written unquoted, so the replay parses it as several arguments |
| Typed **inline** without quotes | ❌ `shlex.split` makes it several arguments, shifting every later parameter |

So the rule to design against is narrower than "one word everywhere":

> **A step whose answer may contain spaces must either be the `consume_rest` tail, or must not be
> the last answer of the run.** For the copy-paste form to work at all, prefer ids, codes and
> `choice` values for everything else.

What enforces it: `options[].value` must be a single token and `consume_rest` may only be the last
parameter — both in `tests/test_telegram_commands_are_shippable.py`. The rest is the shape of the
command you write, which is why it is written down here.

### Back, Skip and Cancel (2026-09-09)

Every prompt now carries a keyboard and every workflow understands three words. They are one state
machine in `db_ops/lib/workflow_steps.py` — a pure module with no store and no Telegram in it —
rather than a string comparison inside each command, which is how a workflow ends up with as many
ideas of "back" as it has commands.

| Word | Button | What happens | Offered |
| --- | --- | --- | --- |
| `cancel` | 🚫 Cancel | The run ends. `telegram_conversation_states.status = 'cancelled'`, the trail row is `cancelled`, the reply says `No changes were made.` and the keyboard is removed. **Nothing executes.** | always |
| `back` | ⬅️ Back | The previous **asked** step is asked again, and its answer is cleared | when the run has asked more than one step |
| `skip` | ⏭️ Skip | The step's `skip_value` (default `-`) is stored and the run moves on | only where the step allows it |

**Back walks the ask history, not `position - 1`.** With `ask_when` branching some positions are
never asked, so counting backwards would re-ask a question the run excluded and then treat its
answer as meaningful. The history lives in `state_json.history`.

**Re-choosing a branch forgets the one abandoned.** Answer `password`, type one, go Back, choose
`key_file`: the password is cleared, because a value belonging to a branch this run no longer
reaches would otherwise still be handed to the CLI.

Buttons are a **reply keyboard**, not an inline one: a tap arrives as an ordinary text message
through the intake that already works, and typing the word by hand is exactly as valid. The cost
is that the store cannot tell a tap from typing — `answer_kind` therefore says `option` when the
answer matched one of the offered values and `text` when it did not, and claims nothing about
which finger produced it. Inline keyboards need `callback_query`, which `updates.py` does not
store; that is a later change.

### The step schema (2026-09-09)

Additive — a parameter that declares none of these behaves exactly as before.

| Key | Default | Meaning |
| --- | --- | --- |
| `input_type` | `text` | `choice` puts `options` on buttons; `secret` keeps the answer out of the trail |
| `options` | `[]` | `[{"label": "Yes", "value": "yes"}]`, or the short form `["yes", "no"]`. The **label** is shown, the **value** is stored |
| `allow_text_input` | `true` | `false` refuses anything that is not one of the options, **at that step** |
| `allow_skip` | `not required` | Whether ⏭️ Skip appears. On an **optional** step, declaring it `true` is also what makes the step get asked at all — an optional parameter is otherwise never prompted for, which is why Skip could not exist before |
| `skip_value` | `"-"` | What a skip stores. `-` is the sentinel every CLI here already reads as "not given" |
| `ask_when` | — | `{"parameter": "remote_auth", "equals": "secret_ref"}` — the step is asked only when an earlier answer matches. Also accepts `not_equals`, `in`, `not_in` |

A step whose `ask_when` does not hold resolves to its `skip_value` when the argv is built, so the
`conditional_args` rules that test `not_equals: "-"` keep working unchanged. Leaving it empty would
pass that test and produce a flag with no value.

**Answers are validated at the step**, not when the command finally runs. A value mistyped at step
2 of a 14-step workflow used to be reported after step 14, by which point it could not be fixed.

#### The branch, as `/spbot_create_db_docker` uses it

```
deploy_target
   ├── worker            → 9 questions, no SSH question at all
   └── <ip>              → remote_user
         └── remote_auth [Secret ref] [Password] [SSH key file]
               ├── secret_ref → remote_password_ref   and nothing more
               ├── password   → remote_password_text  (secret)
               └── key_file   → remote_key_name
```

Before this, all fourteen questions were asked every time and the operator answered `-` to the
ones that did not apply — including, after giving a stored secret ref, to both the password and
the key file.

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

