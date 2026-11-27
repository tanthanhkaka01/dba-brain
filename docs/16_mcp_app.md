# 16 — MCP server (`dbabrain mcp`)

dbabrain as an **MCP server for Claude Desktop and Claude Code** on the user's own machine (0.38.0;
way 1 of [`audits/20261110_plan_mcp_connector.md`](../audits/20261110_plan_mcp_connector.md)). Claude
starts `dbabrain mcp serve`, and every `common.cli` command - and dbabrain's own runtime store - is a
tool it can call, against the user's own inventory, logins and databases.

> **A dbabrain key for what changes something (1.1.0; in 1.0.0 the whole server needed one).** `dbabrain mcp serve` and `call` start with no key: a tool that only reads (`readOnlyHint` - the `list-*` and `check-*` tools, `db-status`, `describe-object`, `table-info`, `db-activity`, `query-plan`, `store_query`, ...) answers, and a tool marked destructive (`run-sql` and `run-sql-write` among them) answers that it needs a key and how to get one - `dbabrain key-request`, free, an email sign-in ([13](./13_common.md#a-dbabrain-key-key-request-key-renew-key-status-100)) - and none of it runs. `serve` says so once on stderr. The line is the tool's hint (`lib.mcp_tools.needs_key`); each call asks the same door as every gated command (`lib.dbabrain_key.gate_here`, kept a minute), so a key set while the server runs counts within a minute, and after a key's end and its 3 days of grace the destructive tools stop again while the readers go on. **`connect`** - linking the node to dbabrain.dev's connector - needs the key up front: without one a run by hand says how to get one and exits 0, after the grace it refuses with exit 3, and the scheduler does not start it. `dbabrain mcp tools` lists every tool with no key, marked `needs key` / `no key`. [03](./03_app_command_daemon.md#the-dbabrain-key-what-starts-without-one-and-the-warnings-100) has the gate.

```
user's machine:  Claude Desktop / Claude Code
                    | stdio (MCP)
                 dbabrain mcp serve      <- DBABRAIN_HOME: the tool root; DBABRAIN_SECRET_KEY: the user's environment
                    |  python -m dbabrain.common.cli <command>   (through dbabrain.transport)
                 the user's databases and hosts            + the runtime store (read-only SELECT)
```

Package `dbabrain/mcp/` (`cli.py`, `server.py`, `catalogue/mcp_tools.json`); rules in
`dbabrain/lib/mcp_tools.py`; settings in `data/mcp_tools.json`.

## What it offers

**One tool per `common.cli` command, plus two on the store** - the operator, 2026-11-11 (translated):
*let MCP run every common cli, SQL included, and read the dbabrain store.*

- **Command tools** - every command the reference (`shared_config_objects.json`) has an `input_` entry
  for: `list-targets`, `db-status`, `run-sql`, `host-facts`, `file-checksum`, `trace-session`, ...
  (`dbabrain mcp tools` lists them; 94 in 1.3.0). The list is read from the reference, so a new command is a
  new tool. A tool takes `request` - the command's own JSON request. **Its description is the tool's own text**
  (`lib.mcp_tool_text`, 1.3.0): what it does, a verb first, and in a family - file transfer, deletion, SQL,
  restore - which sibling to take instead (`fetch-file`: *for a packed backup archive use pull-file*); no
  `dbabrain common.cli `x`:` before it. A command with no text there falls back to its entry's
  `command_lines` or `one_line`, and a test holds every offered tool to having one (Glama scored shared openings
  1 of 5 on 2026-11-26, and on 2026-11-27 the families an agent picks wrongly in). Its input schema is the request's fields from the reference
  (`lib.mcp_tools.request_schema`): type, allowed values, bounds, purpose, required. Left out: what the
  node fills (connection, host login, policy, rules, secrets), what a request is refused for (a secret,
  `sql_access`); `server_id` leads. The description no longer lists the fields.
  What every tool shares - `server_id` names the server and the node completes its login; an emergency
  operation takes `confirm` and a `reason` - is said once, in the server's instructions, not in each tool:
  said in each it was two of a tool's three sentences, and `disable-job` read 97% like `start-job`.
- **A command another tool does is not a tool of its own** (1.3.0): `delete-verified-file` and
  `delete-empty-folder` are their plurals with a one-item list, and `key-renew` is `key-request` again - the same
  sign-in, the new key replacing the old (`lib.mcp_tools.COVERED_TOOLS`). Over MCP they are not listed, and a call
  to one answers which tool does it; the commands stay in common.cli. A plural is no wider than its singular: each
  item states its own path (absolute, no wildcard) and `must_be_under`, a verified file also its name, bytes and
  sha256, and one bad or repeated item refuses the whole list before the host is reached.
  `prune-backup-files` is marked read-only: it answers a retention rule's plan and refuses `delete` (0.38.0).
  `add-sql` is not a tool either (1.439): it is the older one-call form of `sql-command-add` + `sql-target-add`. **`ask` and
  `authorize` are not tools**: they ask a person at the node's terminal, which an MCP call does not have - the
  client's own approval prompt is that person (`TERMINAL_TOOLS`).
- **Commands too alike are one tool, one field choosing** (1.3.0, 1.439; the operator, 2026-11-27: *group a few
  tools that are too alike*) - only where the commands share one request entry and one hint, the choice is one enum
  field and none is an emergency operation (`lib.mcp_tools.GROUPED_TOOLS`):

  | Tool | Field | Commands |
  | --- | --- | --- |
  | `restore` | `step`: full, diff, log | `restore-full`, `restore-diff`, `restore-log` |
  | `list-objects` | `kind`: databases, schemas, jobs | `list-databases`, `list-schemas`, `list-jobs` |
  | `check-config` | `check`: objects, references, credentials | `check-objects`, `check-references`, `check-credentials` |
  | `sqlserver-cu-check` | `check`: precheck, build | `sqlserver-precheck`, `sqlserver-verify-build` |
  | `list-backup-files` | `action`: list, prune-plan | `list-backup-files`, `prune-backup-files` |

  The field leads the tool's input schema and is required; the call runs the command it names, without the
  field. The commands stay as they are in common.cli; over MCP a call by a command's own name answers the tool
  and the value. **A tool switched off under any name it had stays off** (`FORMER_NAMES`): `disabled_tools`
  naming `restore-log` keeps `restore` off, and `prune-backup-files` keeps `list-backup-files` off.
- **`store-schema` / `store-query`** are the store tools' names since 1.3.0 (`store_schema` / `store_query`
  before - the only snake_case names); a call by the old name answers the new one, and an old name in
  `disabled_tools` still switches the tool off. The settings `store_query_max_rows` /
  `store_query_timeout_seconds` keep their names.
- **`dbabrain mcp call` and `mcp tools` print any character** (1.2.0): piped or redirected, the answer leaves
  as UTF-8, as common.cli's does; a console keeps its code page and shows `?` for what it cannot draw. A
  redirected `store_query` died at the first ✅ of a job's message on Windows (2026-11-26).
- **`server_id` is enough.** Where a command needs a connection or a host login, the request is
  completed from the node's inventory and secret store (`lib.data_sources.request_fill`) - the way
  `run-sql-adhoc` and `run-cmd-adhoc` complete theirs. Claude never sees or writes a password.
- **What is inside a database** (1.1.0): `table-info` (columns, indexes, size), `object-source` (a procedure's,
  view's or trigger's text), `db-activity` (running now, blocking, waits, top queries), `query-plan` (the estimated
  plan, compiled and never run) - read before a query is written ([13](./13_common.md)).
- **`store-schema` / `store-query`** - dbabrain's runtime store: its tables, and one SELECT
  (`db.read_only_query`: `lib.read_only_sql`, a read-only transaction, a statement timeout, always
  rolled back, capped at `store_query_max_rows`). `web_users`, `web_sessions` and
  `web_login_attempts` are blocked (`store_blocked_tables`).
- **`run-sql` and `run-sql-write` run with the rights of the login this node stores for the server** - the
  instance's `default_credential_name` in `users.json`, or the request's `credential_name`. What that login may
  do, they may do, DML and DDL included: **`run-sql` is not read-only, and is not marked so** (1.1.0, the
  operator, 2026-11-23). `run-sql` takes one statement as `sql_text`, checks that it is one SELECT
  (`lib.read_only_sql`) and never commits; the check reads text and can be passed, so it is a guard against a
  mistake, not a boundary. `run-sql-write` keeps what it changed only when the request says `commit`. To keep a
  server to reading, store a login there that can only read. Both name each engine's SQL reference.

An app (R03): it never imports `common` - each command runs as `python -m dbabrain.common.cli` through
`dbabrain.transport` - and never another app; it reads the store through `db` and its rules from `lib`.

## What keeps it safe

1. **Each command keeps its own gate.** A destructive one (`kill-spid`, `shrink-log`,
   `host-restart`, `start-job`, `disable-job`, a delete ...) still refuses without the `confirm` /
   `reason` its rules ask for - exactly as from a terminal with no TTY.
2. **The client asks the person.** Every tool carries a `title` and exactly one of MCP's hints - the
   directory's rule: `read_only_hint` for one that only reads (`list-*`, `check-*`, `db-status`,
   `run-sql`, ... - `lib.mcp_tools.READ_ONLY_PREFIXES` / `READ_ONLY_COMMANDS`), `destructive_hint` for
   every other (the emergency operations, deletes, registrars, copies, restores, `run-sql-write`). Claude
   asks before a destructive one; descriptions state what a tool does, never how Claude should behave.
3. **No secret crosses.** Every answer passes `lib.redaction` - each value of the encrypted secret
   store, as itself and as spelt inside JSON, and the shapes a secret is printed in - before Claude
   reads it; the request a command ran (holding the password it was completed with) is never handed
   back. **And none enters** (0.39.0, R52): a request carrying a secret - a key of
   `lib.mcp_tools.SECRET_KEYS` (`password`, `connection.password`, `encryption_password`, `identity`,
   ...) anywhere in it, or `secret-set` / `ai-key`'s `value` - is refused before it runs, without echoing
   it, and says where to type it instead: on the node (`dbabrain common secret-set -`, `dbabrain
   ai-key`), then named here by its reference (`password_ref`). A command that could only be refused
   (`secret-set`, `secret-remove`, `ai-key`, the three that need an age `identity`) is not offered. A login the node
   completes from its own store is not one - name the `server_id`.
4. **A request reaches what its tool is for, and nothing behind it** (1.1.0). Every command stays a tool; what
   a request may not state is refused by name:
   - **how a server is reached** - `sql_access` with a route of its own, in any tool. A server whose inventory
     record goes through a bridge is still reached that way by its `server_id`. A login the node fills in goes only
     where the inventory sends it, for the console and the bot too;
   - **`run-sql`**: `define` values are put into the text before it is checked, `prelude` only declares bound values,
     `driver` is a name; a file a read-only tool writes goes under `runtime/output/` or `runtime/instance_bundles/`;
   - **what is switched off stays off**: an item of `metric-batch` that runs a command is refused where `run-cmd` is
     off, and its SQL only reads where `run-sql-write` is off. It takes a `server_id` in place of its `target`;
   - **`read-file`** reads no secret file of a node and answers no file holding a value of the store.

   `metric-batch`, `guide` and `inventory-summary` are marked as changing something. A tool marked read-only, and each
   of its fields, is named in `tests/test_an_mcp_request_cannot_reach_past_its_tool.py`: a new one fails there until it
   is judged.
5. **Every answer is capped** at `max_chars` and says how much was cut.
6. **Every call is recorded** (`log_calls`): a `job_runs` row `mcp.<tool>` - the tool, the
   `server_id`, the node's user, duration, done or failed - shown on the console's runs page.
7. **A tool is switched off by name** in `disabled_tools`; `enabled: false` offers none.

## The settings - `data/mcp_tools.json`

Every field stated (R50); `init` writes the shipped file, `upgrade-config` (`stated-mcp-tools`)
writes any field a node lacks.

```json
{
  "enabled": true,
  "log_calls": true,
  "max_chars": 60000,
  "timeout_seconds": 600,
  "store_query_max_rows": 500,
  "store_query_timeout_seconds": 60,
  "store_blocked_tables": ["web_users", "web_sessions", "web_login_attempts"],
  "disabled_tools": []
}
```

| Field | What |
| --- | --- |
| `enabled` | the server offers any tool at all |
| `log_calls` | a `job_runs` row per call |
| `max_chars` | an answer's length; more is cut, and said (1000 and up) |
| `timeout_seconds` | one command's run (10 and up) |
| `store_query_max_rows` / `store_query_timeout_seconds` | `store_query`'s cap and statement timeout |
| `store_blocked_tables` | store tables neither listed nor readable |
| `disabled_tools` | tool names not offered, e.g. `["run-cmd", "rotate-password"]` |

## The user's steps

1. `pip install "dbabrain[mcp]"` (the SDK is the `mcp` extra, D-38.1 - the base install does not grow
   for a user who never uses Claude); `dbabrain mcp serve` without it says which extra to install.
   **The Docker image carries it** (0.40.0): the image is built from `requirements.txt`, which lists
   `mcp` and `websockets` beside the engine drivers, so a container node can `dbabrain mcp connect`
   (until then the worker had the code and not the SDK).
2. `dbabrain init` in a folder of its own (the tool root), and fill `data/*.json` - at least
   `db_instances.json` and `users.json`, and the secret store.
3. The secret key in **your own environment**, never in Claude's config file:
   Windows `setx DBABRAIN_SECRET_KEY "<passphrase>"` (a new terminal / a restarted Claude reads it);
   Linux / macOS `export DBABRAIN_SECRET_KEY=...` in the profile Claude is started from.
4. Add the server:
   - **Claude Code:** `claude mcp add dbabrain --env DBABRAIN_HOME=D:/dbabrain -- dbabrain mcp serve`
   - **Claude Desktop:** in `claude_desktop_config.json`:

     ```json
     {"mcpServers": {"dbabrain": {"command": "dbabrain", "args": ["mcp", "serve"],
                                  "env": {"DBABRAIN_HOME": "D:/dbabrain"}}}}
     ```

5. Check without Claude: `dbabrain mcp tools`, and one call -
   `dbabrain mcp call db-status @request.json` with `{"request": {"server_id": "..."}}`.

`--home DIR` does what `DBABRAIN_HOME` does; the commands a tool runs are child processes and find the
tool root through the same variable.

## Limits, said plainly

- **Where the Claude session runs decides it** - the server is a local process, so it is started by a
  Claude that runs on a machine with dbabrain, its `data/` and its key:

  | Claude | `dbabrain mcp` |
  | --- | --- |
  | Claude Desktop, Claude Code in a terminal or an IDE | yes |
  | **Remote Control** - a Claude Code session on your machine, driven from claude.ai or the phone | **yes**: the session and its MCP servers run on your machine |
  | Claude Code on the web (a cloud session in Anthropic's sandbox) | no: the sandbox does not reach your network, inventory or key |
  | claude.ai chat, web and mobile | through way 2 (below) - the demo on dbabrain.dev, not your own install |
- **Query results pass through Anthropic.** What a tool answers is sent to the model; connecting your
  own install to your own servers is your decision, as it is for any MCP server.
- **stdio only.** `serve` speaks MCP on stdin/stdout and prints nothing else there; its messages go
  to stderr.

## Way 2 - claude.ai through dbabrain.dev: every user their own databases (0.38.0)

claude.ai on the web and the phone reach a **remote** connector, `https://mcp.dbabrain.dev/mcp`. Built
2026-11-11 at the operator's word - first *use Cloudflare and the Oracle A1 VM, do it now*, then *every
user can use it* (each with their own databases). The code is `connector/` (its README):

```
claude.ai (account X) --OAuth + MCP--> Worker mcp.dbabrain.dev     sign-in: Cloudflare Access, any email
                                          | routes by the signed-in account
                                          v
                              NodeRelay (a Durable Object per account) <== WebSocket opened by
                                          |                                X's `dbabrain mcp connect`
                                          | (an account with no node of its own)
                                          v
                              the public demo (lab data, read-only tools) behind its own tunnel
```

- **Each user's own node:** `dbabrain mcp connect` on a machine that reaches their databases links it to
  their account once (a device sign-in: a code, a link, sign in, approve) and keeps one outbound
  WebSocket - no port, no tunnel program. The token is kept in the node's encrypted secret store
  (`DBABRAIN_MCP_NODE_TOKEN`); the Worker keeps only its SHA-256, and a new link revokes the old one.
- **One account reaches only its own node:** the Worker routes by the signed-in email to that email's
  relay object; a node token resolves to its own account only.
- **No node yet:** the account reaches the **public demo** - two lab databases with a made-up shop,
  only the read-only tools (computed from the hints, `connector/demo/seed.py`), 60 s per command, a 30 s
  statement cap, CPU caps on its containers, and at most 20 calls a minute per account. It is what the
  directory's reviewers see. With `DEMO_EMAILS` empty instead, such an account is offered one tool,
  `how_to_connect`.
- **What the demo cannot reach:** no host port, no host folder, no host network - its VM is shared with
  another service. It is built from the public tree (`export-public`, G-02 clean).
- **Asked for what it must not give, 2026-11-12 (1.344)** - an OS command, a file, a write, another
  account's data, through every tool it offers (14, all read-only). **Nothing reached the machine**, by
  four fences one behind the other: the tools that act on a host or a file are not offered (`run-cmd`,
  `read-file`, `host-facts`, ... answer *Unknown tool*); `run-sql` takes one SELECT and refuses the words
  and functions that act; the lab logins cannot do it anyway (PostgreSQL `demo_reader`: no superuser, no
  server program, no server file, no CREATE; Oracle: no network ACL, no directory, no Java permission);
  and the containers - none privileged, no added capability, no Docker socket, no published port, the
  server a non-root user - sit on an internal network with no route to the internet, the cloud's metadata
  address or the VM. What it found and what was done: the read-only check let a function inside quotes, a
  statement handed over as text and an Oracle package's function through (fixed in `lib.read_only_sql`;
  the lab logins had refused each); and **the demo's own store handed any account the others'** -
  `store_query` on `job_runs` shows `asked_by` - so `job_runs`, `job_runs_history` and `sql_runs` are not
  readable on the demo (`seed.py`, `DEMO_STORE_BLOCKED`). Left to the operator: Oracle's `DEMO_READER`
  holds the image's `DB_DEVELOPER_ROLE` (it may create objects in its own schema, where the demo's table
  is) - only the check stands before a write there, until the role is taken from it.
- **What is kept on dbabrain.dev:** the account's email, its OAuth grant, its node token's hash, and - since
  2026-11-12 (1.343) - an **audit trail**: one row per sign-in, node link and MCP request (time, account, the
  request's method, the tool's name, own node or demo, the HTTP status, country), kept 180 days and read
  only with the operator's token (`connector/README.md`, `connector/audit.py`). Never a query, a tool's
  arguments or an answer (relayed in memory) and never a database password (`/privacy`).
- **What a call did is recorded where the tool ran:** the `job_runs` row `mcp.<tool>` carries `asked_by` -
  the signed-in address the Worker states (`x-dbabrain-user`, trusted only behind the origin's token or on
  the node's own connection) - on the account's node, or in the demo's store for an account with none.

### dbabrain keys on the same Worker (1.0.0)

The device sign-in also issues **dbabrain keys** (`connector/worker/src/key-token.ts`,
`key-issue.ts`): `POST /node/link/start` with `{"purpose": "key", "node"}`, the approval page says
what the key unlocks and for how long, and `/node/link/poll` answers `{email, dbabrain_key}` once. A
key is `dbbk1.<payload>.<Ed25519 signature>`, signed with the Worker secret `KEY_SIGNING_JWK` (its
public `x` ships in `lib.dbabrain_key.SIGNING_PUBLIC_KEYS`); 14 days, 365 for an account in
`OPERATOR_EMAILS`; one active key per account (the earlier is marked `replaced`). KV keeps each key's
SHA-256 -> `{kid, email, iat, exp, state, node}`, never the key. `GET /key/status` (bearer: the key)
answers its state - `active`, `grace`, `ended`, `replaced`, `revoked`, `unknown`; `POST
/admin/key/revoke {email | kid}` takes `AUDIT_TOKEN`. The audit trail gains `key_issue` (method
`free` / `operator`) and `key_revoke`. `dbabrain mcp connect` needs a key on the node; `serve` and
`call` answer the tools that only read without one, and need it for a tool marked destructive (1.1.0);
`tools` needs none.

### The AI through dbabrain.dev - the relay (1.0.0)

`POST /ai/v1/messages` (`connector/worker/src/ai-relay.ts`, rules in `ai-rules.ts`) is Anthropic's
Messages API for a node that holds a dbabrain key (`x-api-key: <the key>`) and no Anthropic key of its
own (D-36.1: no Claude key is ever handed out). The Worker checks the key (`checkKey`: signed, in its
term, not replaced or revoked, `ai_relay` among its features), sets the free plan's **cheapest model**
(read from its price table - `claude-haiku-5-5` today) and **effort `medium`** - a request naming
others, streaming, server tools, `speed`, betas or more than 8,000 `max_tokens` is refused with what is
allowed - then reserves the turn in the account's `AiLedger` Durable Object (the month's **$1.00**, UTC;
20 turns per 10 minutes) and sends it with the operator's `ANTHROPIC_API_KEY`. The answer's `usage` is
priced by the Worker's table and charged; a turn already running finishes, so the overshoot is one turn.
A spent month is 403 ("resets on the 1st"), the rate 429, the operator's key or credit failing 503
("unavailable - your own Anthropic key still works"). There is no global cap (D8): the credit is
prepaid with auto-reload off. `GET /ai/v1/usage` answers the account's month; **`GET /admin/ai/usage?month=YYYY-MM`**
(`AUDIT_TOKEN`) answers every account's - turns answered and refused, tokens, cost - summed after each turn in an
`AiLedger` named `_accounts` (13 months kept). A refused turn leaves an `ai_call` audit row with its status too. The secret `AI_RELAY`
(`on` / `off`) switches it in one command. Kept per turn: account, key id, model, tokens, cost, time
(the ledger, 400 days) and an `ai_call` audit row - never the question or the answer.

## The Connectors Directory

Anthropic's directory lists a remote connector anyone can add (claude.com/docs/connectors/building/
submission): submitted by the operator at claude.ai/directory/manage -> Submit new -> MCP connector,
`https://mcp.dbabrain.dev/mcp`. What the review requires and where it is met:

| Requirement | Met by |
| --- | --- |
| a remote HTTPS server, OAuth | the Worker (Access sign-in, OAuth 2.1 with dynamic client registration) |
| every tool a `title` and `readOnlyHint` or `destructiveHint` | `server.tool_list` - one of the two on every tool |
| no tool that both reads and writes | `run-sql` (one SELECT checked, never committed - marked as changing something: it runs with the stored login's rights) / `run-sql-write` (commits) |
| a custom query tool names its API | `run-sql` / `run-sql-write` name each engine's SQL reference |
| descriptions state facts, never steer Claude | `lib.mcp_tools.describe`, a test |
| privacy policy, documentation, support, icon | `https://mcp.dbabrain.dev/privacy`, `/docs`, support@dbabrain.dev, `/icon.svg` |
| a test account with data | any email: with no node it reaches the public demo |

## Commands

| Command | What |
| --- | --- |
| `dbabrain mcp serve [--home DIR]` | the MCP server on stdio |
| `dbabrain mcp tools [--home DIR] [--json]` | the tools this node offers, each read-only / changes / DESTRUCTIVE |
| `dbabrain mcp serve --http [--bind ADDR] [--port N] [--allowed-host HOST]` | way 2's origin: Streamable HTTP on `/mcp`, stateless, JSON answers; every request needs `Authorization: Bearer <DBABRAIN_MCP_HTTP_TOKEN>` (32 characters or more, from the environment only - the Worker on dbabrain.dev holds it); `/healthz` answers `ok` without it; `--allowed-host` is the Host the tunnel delivers (DNS-rebinding protection) |
| `dbabrain mcp connect [--home DIR] [--relink]` | this machine as YOUR node of dbabrain.dev: links it once (a code to approve after signing in), then keeps one outbound WebSocket; every request is answered by the SDK's own server in this process and logged with `asked_by`. A link not approved, or a token refused, ends in one sentence (exit 1) |
| `dbabrain mcp connect --wait-for-link` | the same, **as a service** (0.40.0): never starts a link - with no token it says once that the node is not linked and reads the secret store again every minute; a refused token is set aside until a new one is stored. The token comes from a `connect` run once by hand. The estate runs it on the worker as `APP-MCP-CONNECT` (repeat 0, timeout 0, retry 60); it is not in the shipped schedule, which holds only what runs on an empty install. Held by `tests/test_a_node_service_waits_for_its_link.py` |
| (the node's secrets) | read again when `data/encrypted_secret_text.json` changes (its mtime and size; 0.40.0): a secret a config push brings is usable, and redacted, at the next request, with no restart. Held by `tests/test_an_mcp_node_reads_a_secret_stored_after_it_started.py` |
| `dbabrain mcp call TOOL ARGS [--home DIR]` | one tool without Claude; `ARGS` is JSON, `@file` or `-`; a command tool takes `{"request": {...}}` |

Proven 2026-11-11 on the master: the SDK's own stdio client listed 101 tools (99 commands, 2 on the
store), `run-sql` / `db-status` / `list-databases` answered from a SQL Server by its `server_id`,
`store_query` refused a `DELETE`, and each call was a `job_runs` row.
