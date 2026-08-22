# 12. Web Host App

The **webhost** app (`db_ops/webhost/`) is the browser-facing side of db_ops. It does two things
on one listener:

1. **publishes the generated reports** over HTTP so they can be opened instead of copied off the
   worker (`/report_dba/...`), and
2. **serves the web console** — login, sessions, a dashboard of all fourteen db_ops apps, config
   editing, and a "Run now" button (`/db_ops/...`).

It is small and self-contained, built on the Python standard-library `http.server` — no external
web server, no framework, no JavaScript bundle, no asset to fetch. Like the other db_ops apps it
has a CLI (`python -m db_ops.webhost.cli`) and is driven by the app-command daemon (`APP-WEBHOST`).

Typical URLs on the worker:

```
http://<worker>:8080/report_dba/database-inventory.html   the reports
http://<worker>:8080/db_ops/                              the console
```

The two are a **URL prefix apart on the same port**, not two servers. One listener means one
firewall rule, and the reports the console links to are same-origin.

## What it does

| Responsibility | Mechanism |
| --- | --- |
| Serve a report directory under a URL prefix | `build_webroot` makes `webroot/<mount>` a symlink to `--root`, so files appear under `/<mount>/`. |
| Keep a stable link to the newest report | `refresh_latest` points `<root>/<latest>` (default `database-inventory.html`) at the newest file matching `--latest-glob`, refreshed every `--refresh-seconds`. |
| Serve a snapshot at-or-before a moment | A custom request handler answers `?date=` by serving the newest report whose timestamp is `<= date` (see below). |

The reports themselves are produced by the **reports app**
(`inventory-workflow --beauty 1`, see [06_reports_app.md](06_reports_app.md)), which writes
`<YYYYMMDD_HHMMSS>_database-inventory-report.html` into `runtime/reports`. The webhost only
publishes them; it never renders.

Other apps also drop pages into the same `runtime/reports` root and are served the same way.
The **SLA/SLO app** (`validate --publish-web`, see [09_sla_slo_compliance_app.md](09_sla_slo_compliance_app.md))
writes `sla.html` and a landing `index.html` hub, served at `http://<worker>:8080/report_dba/sla.html`
and `/report_dba/`. Because `index.html` exists, hitting `/report_dba/` returns the hub instead of a
directory listing; dated snapshots remain reachable by direct URL.

## The web console

`/db_ops/` is a signed-in view of the estate. It is implemented as a **pure request -> response
function** (`db_ops/webhost/app.py::WebApp.handle`) with the socket handling left entirely in
`server.py`, which is what lets the whole console be tested offline —
`tests/test_webhost_console.py` drives it with no socket, no browser and no live store.

### Accounts and levels

Accounts live in the runtime store (`web_users`), not in a file. A password is stored only as a
PBKDF2-HMAC-SHA256 encoding at the same 200 000 iterations the encrypted secret store uses
(`db_ops/lib/web_auth.py`); the plaintext is never written anywhere.

Each account carries a **level from 1 to 100** — deliberately the same ladder
`telegram_users.user_type` uses, so there is one permission scale for the whole tool. What each
level unlocks is set in `data/webhost_config.json`:

| Setting | Default | Gates |
| --- | --- | --- |
| `min_level_view` | 1 | Signing in and reading the dashboard. |
| `min_level_edit` | 50 | Changing config records. |
| `min_level_run` | 50 | Running an app from the console. |
| `min_level_admin` | 90 | Account and session administration. |

The password is also written to `data/encrypted_secret_text.json` under
`WEB_CONSOLE_<USERNAME>`, so an operator can look it up instead of resetting it:

```powershell
python -m db_ops.webhost.cli user-password-show --username thanh --key-base64 <KEY>
```

Pass `--no-remember` to keep only the hash. What the copy costs is worth saying plainly: the hash
stops being the only one, and anyone with `DB_OPS_SECRET_KEY` can read the password. That is a
real reduction and a small one *here* — the same file already holds the postgres superuser and the
SQL Server DBA logins — and **the login path never consults it**, so it is a note for a person,
not a second way in. `web_users.password_ref` records where the copy lives, or is empty when there
is none.

An account is **disabled, never deleted** (`is_active = 0`), the same rule the config mirror
follows. The row, its level and its login history stay; the username becomes free to issue to
somebody else, because `ux_web_users_active` is a *partial* unique index over active rows only.

Failed logins are counted and an account locks for `lockout_minutes` after `max_failed_logins`
attempts. Every attempt — success or failure, known username or not — is recorded in
`web_login_attempts` with the IP and user agent.

**The form tells the browser one thing for every failure**: "Wrong username or password." The
store records *why* it actually failed, so an operator reading `web_login_attempts` can tell a
typo from a disabled account; the page does not, because a login form that distinguishes them is
a way to enumerate who works here. A lockout is the single exception — it has to be
distinguishable or people retry forever against a door that will not open for fifteen minutes.

### Sessions last three months

Signing in issues a random token, sets it as a cookie, and writes a `web_sessions` row.

- **The cookie carries `Max-Age`** (7 776 000 seconds = 90 days), not a session lifetime. That one
  attribute is the whole reason **closing Chrome or Firefox does not log anyone out**: a cookie
  with no `Max-Age` is a *session cookie* the browser discards on exit, by design.
- **The store never holds the token**, only its SHA-256 fingerprint. Reading `web_sessions` from a
  backup, a replica or a psql prompt does not let anyone log in as anybody.
- The cookie is `HttpOnly` (unreachable from any script on the page) and `SameSite=Lax` (a
  cross-site POST arrives without it). Set `cookie_secure: true` in `webhost_config.json` once the
  console is behind HTTPS.
- **Expiry is a property of the row, not of a job.** `resolve_session` retires any session it finds
  past its `expires_at`, so a stale session stops working whether or not a sweeper is running.
- A session never outlives its account: disabling a user, or changing their password, revokes
  every session they hold. That default is the point — a password is changed because it leaked or
  because somebody is leaving, and a live three-month cookie undoes both.

### Layout: the app list, then one app

Every signed-in page is the same two columns. On the left, a fixed list of **all fourteen apps in
the docs' order** (01 Runtime Store … 14 Shared Rules), each with a dot for how it is doing; on the
right, whatever you clicked.

It was a grid of fourteen cards, and fourteen cards is a wall: everything competing for attention,
nothing readable, and the app you wanted somewhere in the middle. A fixed list puts the same names
in the same place on every page — including the config pages — so the eye learns where "Telegram
App" is and stops reading. The sidebar dot is what the grid was actually good at, kept: it is the
**worst** state among an app's commands, never an average, because a sidebar that averaged its
apps would hide the broken one.

`/db_ops/` opens on an **overview**: counts for the estate, then only what needs attention —
failing, overdue, or queued. Deliberately not every app's detail at once, which is what made the
two apps that were failing indistinguishable from the twelve that were fine.

### An app's page

`/db_ops/app/<app_code>` shows one app: its scheduled commands with the schedule **spelled the way
an operator says it** ("every 1s", "every 2h", "runs once and stays up" for `repeat_interval: 0`,
"manual only" for `-1`), the timeout, the node role, the command line, and how the last 24 hours
went — healthy / overdue / failing, run and failure counts, and the last error — followed by the
config it owns. An app with no scheduled command says so rather than looking broken.

**An app that owns exactly one config file opens it there**, records and all, instead of showing a
table with a single row to click — that click told the operator nothing, and for the daemon
`app_commands.json` *is* what the page is for. Apps owning several keep the table, because there
the choice is real. Either way a record links to the same editor, so there is one write path and
not two, and the file's own page stays reachable at `/db_ops/config/<file>`.

Three sources are joined, and **all three are read through the store**, not off this checkout's
disk:

| Shown | Read from |
| --- | --- |
| The blocks themselves | `webhost_config.json`, as mirrored into `config_items` |
| Schedules and command lines | `app_commands.json`, as mirrored into `config_items` |
| Healthy / overdue / failing | `job_runs`, via `db_ops.db.ops_status` |

The status column is the **same computation the control app's hourly Telegram summary uses**, so
the console and the alert cannot disagree about whether an app is overdue. If the run history
cannot be read the block still renders, saying "no run history" — losing the status column must
not cost the page.

The config table on each app's page is the entry point for editing. See "Config Mirror" in
[01_runtime_store.md](01_runtime_store.md).

### The running log

The **Logging Engine** page (`/db_ops/app/logging_ops`) shows the log this node is writing, newest
line first, a hundred lines at a time, with older lines arriving as you scroll. A picker chooses
which log; it opens on whichever moved last, because that is the one being written now.

It is on that page and no other. Every app writes a log, but the logging engine is the component
an operator opens *to read them* — putting the same panel on all fourteen pages would repeat it
fourteen times and still leave nothing where people look.

Some decisions worth knowing:

- **Newest first, read from the end.** The file is seeked backwards a chunk at a time
  ([`db_ops/lib/log_tail.py`](../db_ops/lib/log_tail.py)), so opening a 400 MB log costs one page,
  not a scan. Memory is bounded by the chunk, never by the file.
- **The scroll cursor is a byte offset, not a page number.** Lines are being appended while you
  read; anything counted in lines would shift under you and repeat or skip rows.
- **Rotated copies are not listed.** `metrics_20260819.log` and its thirty siblings would bury the
  files anybody wants. They stay on disk and stay readable by name.
- **A line that is not one of ours is kept whole.** `*_runtime.log` is raw stdout and a traceback
  is not pipe-delimited anything — and the line that does not fit the format is usually the reason
  someone opened the log.
- **The log name is matched against the directory listing**, never joined onto it. The name
  arrives from a URL, and `../../etc/passwd` joins as happily as `metrics.log` does.

The panel is the console's only JavaScript, inline for the same reason the CSS is: no bundle, no
CDN, no build step, because this page is served from inside the worker container on a network that
may not reach the internet. With scripting off it degrades to the newest hundred lines, rendered by
the server, which is still the useful part.

```
GET /db_ops/api/logs?file=metrics.log&before=<offset>&limit=100
```

### Editing config

Each app's page opens its config: the file itself when it owns only one, a list when it owns
several. A file page lists its records by key;
a record page draws the record as a **grid of named fields** — one row per value, with a typed
input: a number box for `repeat_interval`, a checkbox for `active`, one-item-per-line for
`db_types`, and a nested block like `time_window` or `notify` as its own section with its rows
indented under it.

The grid is generated **from the record itself** ([`db_ops/lib/record_form.py`](../db_ops/lib/record_form.py)),
never from a hand-written list of known fields. That distinction is the whole design: these
records have no fixed shape — a SQL target carries `time_window` and `notify`, a metric definition
carries per-`db_type` `variants`, `users.json` carries a list of credential objects — and a form
built from a list would silently delete everything it was not told about on the first save. Every
leaf becomes a row carrying the JSON type it came from, so `0` stays a number, `false` stays a
boolean, `null` stays null, and `"0"` stays a string. The contract is

```
rebuild(flatten(record)) == record
```

checked against **every record in `data/`** by `tests/test_record_form.py`, and by a console test
that opens a record, presses Save, and asserts the revision did not move.

A shape the grid cannot draw as a row — a list of objects, most of all — keeps a small JSON box
**for that field only**, so the reader is looking at one field's worth of JSON instead of the whole
record.

Underneath the grid is a collapsed **"Edit as JSON"** box holding the whole record. It is not a
leftover: the grid edits the fields a record already *has*, so adding or removing a key is only
possible there. Whichever form is submitted is what gets saved — which one was used is read off
the submission itself (the grid posts `f:...` fields, the box posts `payload`) rather than from a
mode flag that could disagree with both.

**A save writes two things**: the store row (with a revision, an author and a timestamp) and
`data/<file>.json`, rebuilt from the store. The second is not bookkeeping — the apps read the
files, so a change that stopped at the store would be one you watched succeed and that nothing
acted on. See "Config Mirror" in [01_runtime_store.md](01_runtime_store.md).

**An edit made here does not reach the master by itself.** The console writes the store (shared)
and *this node's* `data/`. On the worker that is enough for the apps to pick it up on their next
run, but the master's files stay behind — so the next deploy would ship the old values back. The
deploy now refuses to do that silently: see "The config-drift gate" in
[11_control_app.md](11_control_app.md). To bring an edit to the master by hand:

```powershell
python -m db_ops.db.cli export-config '{"files": ["app_commands.json"]}' --key-base64 <K>
```

**Retiring keeps the row.** The record leaves the file and its row goes `is_active = 0`, keeping
its JSON and its whole revision trail. The key becomes free, so the same `metric_code` can be
added back later as a new record with its own history. Retired records are hidden until you ask
for them (`?retired=1`).

Every write is gated twice, and a refusal says which:

| Gate | What it stops |
| --- | --- |
| CSRF token | A form from somewhere else. `SameSite=Lax` already blocks the cross-site POST; this is the second lock. |
| `min_level_edit` (50) | An account that may look but not change. A viewer sees no edit controls at all — a button that answers 403 is a worse answer than no button. |

The write path itself (`db_ops/db/config_edit.py`) refuses a record whose key fields do not match
the record being edited (renaming is a delete and an add), a key that is already live, a literal
secret, a payload that is not an object, and retiring the file-settings row.

### Running an app

Each scheduled command carries a **Run now** button. It does not run anything: it writes a row in
`app_command_requests`, and the **daemon** starts the command on its next scan. That split is the
whole design — the daemon owns the working directory, the log scope, the forwarded key and the
timeout reaper, and a console that spawned its own subprocess would have none of them.

- A request **overrides the schedule** (allowed hours, repeat interval) but never starts a second
  copy of a command already running.
- Pressing twice queues once; the button is replaced by the request's status while it is in
  flight.
- A request nobody picked up within 15 minutes expires rather than firing when the daemon returns.
- The run writes an ordinary `job_runs` row carrying `requested_by`, so it shows up on this
  dashboard, in the control app's summary and in the Telegram alert like any other run.
- Gated by the CSRF token and `min_level_run` (50).

The same request can be queued from a shell:

```powershell
python -m db_ops.db.cli run-app '{"app_command_id": "APP-METRICS", "requested_by": "thanh"}'
python -m db_ops.db.cli run-app '{"list": true}'
```

### Managing accounts

```powershell
# the first account (the console says so when there are none)
python -m db_ops.webhost.cli user-add --username thanh --level 100 --password-stdin

python -m db_ops.webhost.cli user-list
python -m db_ops.webhost.cli user-level    --username thanh --level 50
python -m db_ops.webhost.cli user-password --username thanh --password-stdin
python -m db_ops.webhost.cli user-password-show --username thanh --key-base64 <KEY>
python -m db_ops.webhost.cli user-disable  --username someone --note "left the team"
python -m db_ops.webhost.cli sessions --username thanh
python -m db_ops.webhost.cli sessions --username thanh --revoke
```

**Prefer `--password-stdin`.** `--password` exists for convenience but argv is world-readable in
the process table — the same reason the store declaration travels on stdin. A password passed that
way on a shared host should be treated as disclosed.

### Routes

| Route | Method | Needs a session | What it does |
| --- | --- | --- | --- |
| `/db_ops/login` | GET, POST | no | The form; authenticates and sets the cookie. |
| `/db_ops/logout` | POST | no | Revokes the session and clears the cookie. |
| `/db_ops/` | GET | yes | The overview: estate counts, and what needs attention. |
| `/db_ops/app/<app_code>` | GET | yes | One app: its commands, its status, its config. |
| `/db_ops/api/session` | GET | yes | Who is signed in, and until when. |
| `/db_ops/api/apps` | GET | yes | The app blocks as JSON. |
| `/db_ops/api/config` | GET | yes | Mirrored config records, filterable. |
| `/db_ops/api/logs` | GET | yes | One page of a log file, newest first. |
| `/db_ops/config/<file>` | GET | yes | One config file's records, by collection. |
| `/db_ops/config/<file>/<collection>/<key>` | GET | yes | One record, as editable JSON, with its history. |
| `/db_ops/config/<file>/<collection>[/<key>]` | POST | yes, level 50 | Create or update a record. |
| `/db_ops/config/<file>/<collection>/<key>/delete` | POST | yes, level 50 | Retire a record. |
| `/db_ops/apps/<app_command_id>/run` | POST | yes, level 50 | Queue a run. |

Anything else under the prefix is a 404. An unauthenticated page request redirects to the login
form; an unauthenticated **`/api/`** request answers `401` with JSON, so a `fetch()` gets an error
it can read rather than a login page it would parse as data.

## CLI

```powershell
python -m db_ops.webhost.cli --config config.json serve \
  --root runtime/reports \
  --mount report_dba \
  --port 8080
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--root` | `<runtime>/reports` | Directory to publish. |
| `--mount` | `report_dba` | URL path prefix (`/<mount>/...`). |
| `--port` | `8080` | TCP port to listen on. |
| `--bind` | `0.0.0.0` | Address to bind. |
| `--webroot` | `<runtime>/webroot` | Holds the `<mount>` symlink handed to the HTTP handler. |
| `--latest` | `database-inventory.html` | Stable filename always pointing at the newest report. |
| `--latest-glob` | `*_database-inventory-report.html` | Which reports the `latest` link tracks. |
| `--refresh-seconds` | `60` | How often the `latest` link is refreshed. |
| `--console` / `--no-console` | on | Serve the web console beside the reports. |
| `--console-mount` | from `webhost_config.json` (`db_ops`) | URL prefix for the console. |

Config is resolved as `config.webhost.json` if present, else `config.json`. The console opens the
runtime store, so `serve` needs the secret passphrase (`--key-base64`, or `DB_OPS_SECRET_KEY`) when
the store is PostgreSQL — the daemon forwards it automatically.

The console's own settings (mount, cookie name, session length, the `min_level_*` gates) come from
`data/webhost_config.json` **on disk**, not from the config mirror. That split is deliberate: they
are what the server needs *to start*, and a store that is unreachable or has never been synced
would otherwise leave the console with no idea where to serve itself. The app blocks it draws do
come from the mirror — those are presentation and can wait for a query.

## Accessing a snapshot by date

The default link always serves the newest report. Append `?date=` (or `?at=`) to **any** report
URL to get the newest snapshot **at or before** a moment:

```
.../database-inventory.html                          -> latest report
.../database-inventory.html?date=2026-06-24          -> newest snapshot of that day (treated as end-of-day)
.../database-inventory.html?date=2026-06-24T18:00:00 -> newest snapshot at or before that time
.../server-metrics.html?server=acme-1&date=2026-08-01 -> that server's page as it was on 1 Aug
.../index-usage_acme-1.html?date=2026-08-01           -> that server's index report on 1 Aug
```

- Accepts a bare date (`YYYY-MM-DD`, taken as the end of that day → the latest snapshot of the
  day) or a datetime (`YYYY-MM-DDTHH:MM:SS`, also space-separated, with partial `HH` / `HH:MM`).
- An unparseable date, or one earlier than every snapshot, falls back to the latest report — the
  newest build is a better answer than a 404.
- The served file's stamp is echoed in the **`X-Report-Snapshot`** response header, so you can
  confirm exactly which snapshot was returned. Its absence means you got the live file.

**Two publishing schemes are resolved, because the reports are written two different ways.**

| Report | Published as | Snapshot resolved from |
| --- | --- | --- |
| `database-inventory.html` | a symlink onto a per-run stamped file with a *different* name | `--latest-glob`, i.e. `<YYYYMMDD_HHMMSS>_database-inventory-report.html` |
| `server-metrics.html` + its `server-metrics_<slug>.json` | a stable name, overwritten every run | `<YYYYMMDD>_<same name>` |
| `index-usage_<slug>.html` | a stable name, overwritten every run | `<YYYYMMDD>_<same name>` |

The second group is archived **once per calendar day**, by
[`db_ops.lib.report_archive`](../db_ops/lib/report_archive.py), overwritten by each later
run that day. Stamping them per run was measured and rejected: ~12 MB a build against a two-hourly
workflow is ~4.3 GB a month of near-duplicate files. One copy per day is not a lossy approximation
of what `?date=` can ask for — a date-only query addresses exactly one snapshot per day — and it
costs a twelfth as much. A day-only stamp is compared as `YYYYMMDD_235959` so the two stamp widths
order against each other, and so a day's archive (the last build of that day) is reachable from
its own date.

**`server-metrics.html` propagates the date itself.** The page is only an index; its series are
fetched on demand, so the date travels into the fetch URL and into every link it renders (server
picker, index-usage link, back-to-inventory). Without that, a page dated last week would draw this
morning's charts under last week's header. The index-usage pages do the same for their picker.

The datetime shown inside the inventory report (`Snapshot 2026-06-24 07:46:37`) is parsed from that
file's own filename stamp — so a snapshot fetched via `?date=` always shows the moment it belongs
to, not the current wall-clock time.

## Scheduling

`APP-WEBHOST` in `data/app_commands.json` starts the server:

```json
"command_text": "python -m db_ops.webhost.cli --config config.json serve --root runtime/reports --mount report_dba --port 8080"
```

It uses `repeat_interval: 0` = **run-once** (see [03_app_command_daemon.md](03_app_command_daemon.md)):
the daemon starts it a single time and it keeps serving in the foreground; it is not restarted on
an interval like the polling apps.

## Symlinks and platforms

The `latest` link and the `<mount>` mapping are **symlinks**. On Linux (the worker target) they
always work. On Windows they may require privilege; failures are logged, not fatal — the server
still serves the timestamped files directly, but the fixed `database-inventory.html` link and the
`/<mount>/` prefix may be unavailable until the timestamped file is requested by name.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `database-inventory.html` 404s but `?date=...` works | The `latest` symlink could not be created (Windows privilege). Runs fine on the Linux worker. |
| `latest` still shows an old snapshot | The `latest` link refreshes every `--refresh-seconds` (default 60s); wait one cycle after a new report renders. |
| `?date=` returns an older report than expected | No report exists at or before that moment; selection is newest `<= date`. Check `runtime/reports` for the stamps present. |
| Report shows only a date, no time | The file was rendered by an older db_ops version (date-only stamp). Newly rendered reports include the time. |
| Reachable from the worker host but not other machines | A network/routing issue (e.g. a VM bridged over Wi-Fi), not the webhost — it binds `0.0.0.0`. Verify the listener with `ss -tlnp | grep 8080` and that nothing upstream blocks the port. |
| Console says "No accounts exist yet" | True — create the first with `webhost.cli user-add`. The store is reachable; it is empty. |
| Signed out every time the browser is closed | The cookie lost its `Max-Age`. Check the `Set-Cookie` header on the login response; `session_days` in `webhost_config.json` drives it. |
| Login always fails right after a deploy | The account is locked (`max_failed_logins` reached). Check `web_login_attempts.reason`; it clears after `lockout_minutes`, or reset with `user-password`. |
| Console 500s but the reports still serve | Working as intended — the console catches its own failures so an exception cannot close the connection the report server shares. The message is on the page and in `logs/webhost_runtime.log`. |
| An app page shows "no run history" | `job_runs` could not be read (wrong store, or no passphrase). The schedule still renders; only the status column is lost. |
| The sidebar dot is grey for an app | It has no scheduled command of its own — a library or a CLI-only component. |
| The log panel says "No log files under ..." | Nothing has been written there yet, or `--config` resolved a different `log_dir`. |
| A log stops loading older lines | The start of the file was reached; the panel says "That is the whole file." |
| A save says "This form has expired" | The page was rendered under a session that has since ended, or the POST came from elsewhere. Reload and retry. |
| "Run now" queued but nothing ran | The daemon is not running, or the command's `node_role` is not this node's. The request expires after 15 minutes and says so in `app_command_requests.note`. |
| A config edit is refused as a rename | The key fields in the JSON do not match the record being edited. Renaming is a delete and an add — retire the old key, then add the new one. |
| A grid save says "'x' is not a whole number" | A number box was given text. The message names the field's full path (`time_window.repeat_interval`); fix that box. |
| A field is missing from the grid | The grid shows the fields the record has. To add one, open "Edit as JSON" and save it there. |
| Clearing a number box set the field to null | Intended: zero is a real interval, so an empty box means "unset" rather than 0. |
| `user-password-show` says the ref is missing | The account was created with `--no-remember`, or before the copy existed. The password cannot be recovered from the hash; set a new one. |
