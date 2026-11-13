# 16 — MCP server (`dbabrain mcp`)

dbabrain as an **MCP server for Claude Desktop and Claude Code** on the user's own machine (0.38.0;
way 1 of [`audits/20261110_plan_mcp_connector.md`](../audits/20261110_plan_mcp_connector.md)). Claude
starts `dbabrain mcp serve`, and every `common.cli` command - and dbabrain's own runtime store - is a
tool it can call, against the user's own inventory, logins and databases.

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
  for: `list-targets`, `db-status`, `list-databases`, `run-sql`, `host-facts`, `file-checksum`,
  `trace-session`, ... (`dbabrain mcp tools` lists them). The list is read from the reference, so a new
  command is a new tool with nothing written here. A tool takes `request` - the command's own JSON
  request - and its description is the reference's: what the command does and each field.
- **`server_id` is enough.** Where a command needs a connection or a host login, the request is
  completed from the node's inventory and secret store (`lib.data_sources.request_fill`) - the way
  `run-sql-adhoc` and `run-cmd-adhoc` complete theirs. Claude never sees or writes a password.
- **`store_schema` / `store_query`** - dbabrain's runtime store: its tables, and one SELECT
  (`db.read_only_query`: `lib.read_only_sql`, a read-only transaction, a statement timeout, always
  rolled back, capped at `store_query_max_rows`). `web_users`, `web_sessions` and
  `web_login_attempts` are blocked (`store_blocked_tables`).
- **`run-sql` only reads; `run-sql-write` is its other half** (the directory refuses one tool that both reads
  and writes): `run-sql` takes one statement as `sql_text`, held to `lib.read_only_sql` before it runs, never
  committed (a DDL commits by itself on Oracle and MySQL, so a rollback alone would not do); `run-sql-write`
  keeps what it changed only when the request says `commit`. Both name each engine's SQL reference.

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
   back.
4. **Every answer is capped** at `max_chars` and says how much was cut.
5. **Every call is recorded** (`log_calls`): a `job_runs` row `mcp.<tool>` - the tool, the
   `server_id`, the node's user, duration, done or failed - shown on the console's runs page.
6. **A tool is switched off by name** in `disabled_tools`; `enabled: false` offers none.

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

## The Connectors Directory

Anthropic's directory lists a remote connector anyone can add (claude.com/docs/connectors/building/
submission): submitted by the operator at claude.ai/directory/manage -> Submit new -> MCP connector,
`https://mcp.dbabrain.dev/mcp`. What the review requires and where it is met:

| Requirement | Met by |
| --- | --- |
| a remote HTTPS server, OAuth | the Worker (Access sign-in, OAuth 2.1 with dynamic client registration) |
| every tool a `title` and `readOnlyHint` or `destructiveHint` | `server.tool_list` - one of the two on every tool |
| no tool that both reads and writes | `run-sql` (read-only, checked, never committed) / `run-sql-write` |
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
| `dbabrain mcp connect [--home DIR] [--relink]` | this machine as YOUR node of dbabrain.dev: links it once (a code to approve after signing in), then keeps one outbound WebSocket; every request is answered by the SDK's own server in this process and logged with `asked_by` |
| `dbabrain mcp call TOOL ARGS [--home DIR]` | one tool without Claude; `ARGS` is JSON, `@file` or `-`; a command tool takes `{"request": {...}}` |

Proven 2026-11-11 on the master: the SDK's own stdio client listed 101 tools (99 commands, 2 on the
store), `run-sql` / `db-status` / `list-databases` answered from a SQL Server by its `server_id`,
`store_query` refused a `DELETE`, and each call was a `job_runs` row.
