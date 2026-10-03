"""The ``common.cli`` help: the usage text of every command ``cli.py`` routes itself, and the login, policy and rules a request states.

Split out of ``common/cli.py`` on 2026-10-03 (Q11,
``audits/20261002_audit_typed_requests_and_errors.md`` section 5). ``cli`` re-exports every
name, so no import changes.
"""

from __future__ import annotations
from db_ops.common.cli_catalog import STATED_CONNECTION


#: The host login a host command takes, stated in full - the counterpart of STATED_CONNECTION.
STATED_ACCESS = (
    "  access     (required) the host login, complete: an inline cmd_access - method (ssh|winrm),\n"
    "             host, port, username, auth_type, and a password or an absolute key_file.\n"
    "             common.cli reads no configuration: an app fills it from a server_id\n"
    "             (lib.data_sources.request_fill). Send the request on stdin (-) or as @file.\n"
    "  target     a label for the evidence and the confirmation - the server_id\n"
)

#: The timing a host maintenance operation reads - run-cmd and the file transfers take none.
STATED_POLICY = (
    "  policy     the maintenance policy that times the operation (data/maintenance_policy.json's\n"
    "             defaults with this server's values over them, stated by the app); {} = built-in\n"
)

#: What an operation costs, stated by the app - shared by every command behind the gate.
STATED_RULES = (
    "  rules      what the operation costs to confirm: {level, confirmations, challenge, effects},\n"
    "             the node's own data/emergency_operations.json entry, stated by the app. Absent,\n"
    "             the ladder the package ships prices it; an unlisted operation costs the most.\n"
)

USAGE = (
    "usage: python -m db_ops.common.cli <command> ...\n"
    "commands:\n"
    "  init            Create a tool root here: config, a SQLite store, an empty inventory (db-ops init)\n"
    "  guide           The operating guide for this build; write it as AGENTS.md (db-ops guide)\n"
    "  encrypt-secret  Encrypt secrets/secret_text.json into the store the tool reads\n"
    "  export-data     Write this machine's whole configuration to one bundle file\n"
    "  import-data     Apply such a bundle, so this machine runs the same estate\n"
    "  add-sql         Register + enable a new SQL task (see --help)\n"
    "  metric-toggle   Enable/disable metrics for one server_id (see --help)\n"
    "  list-targets    List the database targets (server_id, db_type, ip:port)\n"
    "  check-credentials  Does every configured target resolve to a real login (see --help)\n"
    "  list-databases  What databases a server has and their state; oracle: CDB/PDB (see --help)\n"
    "  list-schemas    What schemas one database has (see --help)\n"
    "  list-jobs       What scheduled jobs a target has, and which are enabled (see --help)\n"
    "  create-table-from-xlsx  Build a table from a spreadsheet and load it (see --help)\n"
    "  copy-schema     Reproduce one SQL Server schema on another instance; plans first (see --help)\n"
    "  run-sql         Run SQL on one database target from a JSON request object (see --help)\n"
    "  run-cmd         Run one shell command on a configured host (see --help)\n"
    "  shrink-log      EMERGENCY: shrink one database's log file to N MB (see --help)\n"
    "  kill-spid       EMERGENCY: kill one session, after showing whose it is (see --help)\n"
    "  start-job       EMERGENCY: start one SQL Server Agent job by name (see --help)\n"
    "  disable-job     EMERGENCY: stop one job running on its schedule; mssql/oracle/pg (see --help)\n"
    "  authorize       Confirm one named operation for a caller that performs it itself (see --help)\n"
    "  ask             Ask one question on this terminal, for a caller that cannot (see --help)\n"
    "  rotate-password  Change database login passwords on the server AND in the store (see --help)\n"
    "  check-secret     Try to authenticate with each secret and say why any cannot be (see --help)\n"
    "  check-identifiers  Which of this estate's real names appear in files that ship (see --help)\n"
    "  check-secret-literals  Which files that ship hold a VALUE from the secret store (see --help)\n"
    "  lift-example     Refresh a data/*.example.json from your own file, refusing identifiers\n"
    "  build-showcase   Snapshot the published pages with every real name replaced (see --help)\n"
    "  instance-add     Register one database to monitor: inventory, credential, secret (see --help)\n"
    "  app-command-set  Change an app command's schedule or state in data/app_commands.json\n"
    "  sql-command-add  Register WHAT a SQL task runs: scripts, or a python program (see --help)\n"
    "  sql-target-add   Register WHERE a SQL task runs: one server, one schedule (see --help)\n"
    "  remote-credential-add  Register one host's OS login + the cmd_access naming it (see --help)\n"
    "  secret-set       Store one secret encrypted, never in clear; request on stdin (see --help)\n"
    "  probe-host       What a host listens on, and what db_ops can do with it (see --help)\n"
    "  metric-severity  Remap one metric's statuses for one server_id, e.g. WARNING -> LOGGING (see --help)\n"
    "  trace-session    Who is holding an open transaction — the app user behind a SPID (see --help)\n"
    "  inventory-summary  Merge a health overlay and render the inventory summary (see --help)\n"
    "  list-backup-files  List an engine's backups as full/diff/log (oracle|postgresql|sqlserver)\n"
    "  backup-database  Run ONE backup from a self-contained spec: script + host + env (see --help)\n"
    "  prune-backup-files  Delete backups older than the retention window (default 14 days)\n"
    "  delete-file      Delete ONE backup file by full path, anywhere hostcmd reaches (see --help)\n"
    "  delete-files     Delete every named file, one at a time, over one connection (see --help)\n"
    "  pack-backup      Pack a folder or file list into one archive + sha256 (see --help)\n"
    "  pull-file        Copy one file from a host to this worker, hash-verified (see --help)\n"
    "  push-file        Copy one file from this worker to a host, hash-verified (see --help)\n"
    "  restore-full     Apply one named FULL backup (oracle|postgresql|sqlserver) (see --help)\n"
    "  restore-diff     Apply the differential/incremental step (see --help)\n"
    "  restore-log      Apply log backups, with STOPAT for a point in time (see --help)\n"
    "  restore-key      Import the certificate an encrypted backup needs (see --help)\n"
    "  restore-metadata  Apply SQL Server logins/roles/Agent jobs from .sql exports (see --help)\n"
    "  db-status        Is a server up and usable: instance, one database, one schema (see --help)\n"
    "  verify-restore   Is the restored database actually usable, not just 'done' (see --help)\n"
    "  fetch-file       Copy one named file from a host to here (see --help)\n"
    "  send-file        Copy one named file from here to a host (see --help)\n"
    "  pack-files       Pack named files (or a folder) into one archive + sha256 (see --help)\n"
    "  relay-file       Copy one file from one host straight to another, hash-verified\n"
    "  create-db-docker  Build one lab database Docker instance, here or over SSH (stdin only)\n"
    "  metric-batch     Run one target's metric items, one answer each (stdin only)\n"
    "  run-sqlcmd       Run one sqlcmd batch where the SQL Server is: here, ssh, winrm (stdin only)\n"
    "  smb-list         List the files under a folder of a Windows share (stdin only)\n"
    "  smb-get          Fetch one file from a Windows share to here (stdin only)\n"
    "  smb-delete       Delete named files on a Windows share (stdin only)\n"
    "  smb-credential   Store a share login for Windows (cmdkey) (stdin only)\n"
    "  backup-chain     Which parts of a backup directory a restore needs (stdin only)\n"
    "  copy-backup-dir  Copy a backup directory host to host, one tar stream, mirrored (stdin only)\n"
    "  prune-staged-backups  Delete a restore's staged backups past retention (stdin only)\n"
    "  move-db-docker   Move a lab instance, data included, from one host to another (stdin only)\n"
    "  host-facts       Read one host's state: uptime, disks, services, pending reboot (see --help)\n"
    "  self-status      What THIS installation is: version, host, ip, cpu, memory, disk\n"
    "  describe-object  What a shared config object's fields mean: time_window, notify, ... (see --help)\n"
    "  due-check        Would this time_window run now, and if not why not (see --help)\n"
    "  check-objects    Does this node's config obey the shared-object reference (see --help)\n"
    "  check-references  Which config pointer lands nowhere: server_id, credential_name (see --help)\n"
    "  upgrade-config   After upgrading dbabrain: move data/*.json to this version's shapes (see --help)\n"
    "  standardize-field-names  Move data/*.json to the standard field names; plan first (see --help)\n"
    "  host-service     Start/stop/restart services on a host and wait for the end state (see --help)\n"
    "  host-restart     Restart a host and prove it came back (see --help)\n"
    "  sqlserver-precheck    Is this SQL Server instance safe to patch right now (see --help)\n"
    "  sqlserver-apply-cu    Apply a staged SQL Server cumulative update (see --help)\n"
    "  sqlserver-verify-build  Assert an instance reached an expected build (see --help)\n"
    "  sqlserver-export-instance  Export server metadata (logins, Agent, ...) as SQL (see --help)\n"
    "  sqlserver-replay-instance  Replay an instance-metadata bundle onto a target (see --help)\n"
    "  sqlserver-verify-instance  Compare a target against a bundle; orphaned users (see --help)\n"
)

SQLSERVER_INSTANCE_USAGE = (
    "usage: python -m db_ops.common.cli sqlserver-export-instance|sqlserver-replay-instance|"
    "sqlserver-verify-instance <json>|@<file>|- [--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Server-level metadata for one SQL Server instance: logins, server roles and permissions,\n"
    "credentials, linked servers, endpoints, sp_configure, Database Mail, SQL Agent, model.\n"
    "None of it is in a user-database backup - master/msdb/model are excluded on purpose - so\n"
    "a restored instance has the data and none of the machinery. Oracle and PostgreSQL need no\n"
    "equivalent: their physical backups carry this state already.\n"
    "\n"
    "  export : read the instance, write server/*.sql + manifest.json. Read-only.\n"
    '  {"target": "ACME-192-0-2-115", "output_dir": "runtime/instance_bundles/2-115"}\n'
    "\n"
    "  replay : apply a bundle to a target, in dependency order, with version/edition gates.\n"
    '  {"target": "NEW-HOST", "bundle_dir": "runtime/instance_bundles/2-115",\n'
    '   "phase": "pre-database", "dry_run": true}\n'
    "\n"
    "  verify : compare a target against a bundle; the headline number is orphaned users.\n"
    '  {"target": "NEW-HOST", "bundle_dir": "runtime/instance_bundles/2-115"}\n'
    "\n"
    "Each request above also carries \"connection\" (and export/replay a \"policy\") - see Fields.\n"
    "\n"
    "Fields:\n"
    + STATED_CONNECTION +
    "  policy       (export/replay, required) the content of sqlserver_instance_policy.json -\n"
    "               which server settings are portable. Stated by the app, never read here\n"
    "  secrets      (replay) {ref: value} for the refs the bundle's manifest lists\n"
    "  bundle_dir   (replay/verify, required) the folder export wrote\n"
    "  output_dir   (export) default runtime/instance_bundles/<server_id>\n"
    "  include      (export) artifact subset; default every artifact the policy declares\n"
    "  phase        (replay) pre-database | post-database | all. pre-database runs BEFORE the\n"
    "               user databases are restored so their users are not orphaned; post-database\n"
    "               AFTER, because Agent job steps name databases that must exist.\n"
    "  dry_run      (replay) true = report what would run, execute nothing\n"
    "  confirm      (replay) required for a real run - true = the payload means it\n"
    "  on_unsupported (replay) skip (default) | fail\n"
    "\n"
    "Secrets SQL Server will not hand over (credential, linked-server, proxy, Database Mail)\n"
    "are exported as placeholders and resolved at replay from the request's \"secrets\".\n"
    "Replay fails closed, listing every unresolved reference, before executing anything.\n"
    "\n"
    "Prints the gate report as JSON. Exit 0 unless a blocking gate failed.\n"
)

HOST_FACTS_USAGE = (
    "usage: python -m db_ops.common.cli host-facts <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Reads one host's state over the access its request states - Windows or Linux, same output\n"
    "shape - and gates the things an operator would otherwise have to eyeball. Read-only.\n"
    "\n"
    '  {"target": "ACME-192-0-2-250", "services": ["MSSQL$APPDB"], "access": {...}, "policy": {}}\n'
    "\n"
    "Fields:\n"
    + STATED_ACCESS + STATED_POLICY +
    "  services   Windows service names or systemd units to report on\n"
    "  evidence   false to skip the JSON evidence file (default: runtime/evidence/facts/)\n"
    "\n"
    "Prints the gate report as JSON. Exit 0 unless a blocking gate failed.\n"
)

HOST_SERVICE_USAGE = (
    "usage: python -m db_ops.common.cli host-service <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Starts, stops or restarts services on a host and WAITS for the end state. Windows service\n"
    "names and Linux systemd units are the same request; only the command underneath differs.\n"
    "\n"
    '  {"target": "ACME-192-0-2-250", "services": ["MSSQL$APPDB"], "action": "restart",\n'
    '   "confirm": true}\n'
    "\n"
    "Fields:\n"
    "  target/access/policy  as for host-facts\n"
    + STATED_RULES +
    "  services       (required) service names / systemd units\n"
    "  action         status (default, read-only) | start | stop | restart\n"
    "  confirm        (required for anything but status) true = the payload means it\n"
    "  dry_run        true = resolve the target and print what would run, change nothing\n"
    "  assume_yes     true = unattended; waives the typed confirmation (see host-restart)\n"
    "\n"
    "Prints the gate report as JSON. Exit 0 unless a blocking gate failed.\n"
)

HOST_RESTART_USAGE = (
    "usage: python -m db_ops.common.cli host-restart <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Restarts a host and proves it came back: record the state before, wait for the host to stop\n"
    "answering, wait for it to answer again, wait for its services, then re-read the state. Works\n"
    "on Windows and on Ubuntu/Linux.\n"
    "\n"
    '  {"target": "ACME-192-0-2-250", "services": ["MSSQL$APPDB", "SQLAgent$APPDB"],\n'
    '   "reason": "clear PendingFileRenameOperations before CU26", "confirm": true}\n'
    "\n"
    "Fields:\n"
    "  target/access/policy  as for host-facts\n"
    + STATED_RULES +
    "  services        services that must be up again before the restart counts as finished\n"
    "  reason          recorded in the host's shutdown event log and in the evidence file\n"
    "  confirm         (required) true = the payload means it\n"
    "  dry_run         true = run every check and print what would happen, restart nothing\n"
    "  assume_yes      true = unattended automation; nobody is prompted (see below)\n"
    "  window          {\"start\": \"2026-08-03 19:00\", \"end\": \"2026-08-03 21:00\"}, or any\n"
    "                  time_window block; ignore_window: true records the breach and proceeds\n"
    "  wait            per-run timeout overrides, laid over the request's policy\n"
    "\n"
    "TWO LOCKS, because they answer different questions. \"confirm\": true is INTENT - this payload\n"
    "means to change a machine. Typing \"yes\" at the prompt is PRESENCE - a human is reading THIS\n"
    "target, right now. A payload can be replayed or copied to the wrong host; a person cannot. So\n"
    "at a terminal db_ops prints what is about to happen and waits for the whole word \"yes\";\n"
    "anything else aborts. With no terminal the run is REFUSED unless the request also carries\n"
    "\"assume_yes\": true, so a scheduled job that forgot to declare itself fails instead of\n"
    "rebooting production at 03:00. Whatever authorized the run is recorded in the evidence file.\n"
    "The same control guards host-service and sqlserver-apply-cu.\n"
    "\n"
    "Prints the gate report as JSON. Exit 0 unless a blocking gate failed.\n"
)

SQLSERVER_PATCH_USAGE = (
    "usage: python -m db_ops.common.cli sqlserver-precheck|sqlserver-apply-cu|sqlserver-verify-build\n"
    "       <json>|@<file>|- [--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "The three SQL Server cumulative-update capabilities. The request states BOTH halves of the\n"
    "target: the host (\"access\", as for host-facts) and the instance (\"connection\", the SQL\n"
    "login). An app fills both from one server_id; common.cli reads no configuration.\n"
    "\n"
    "  sqlserver-precheck      read-only: is this instance safe to patch right now\n"
    "  sqlserver-apply-cu      runs every precheck gate again, then the unattended patch\n"
    "  sqlserver-verify-build  read-only: did the instance reach the expected build\n"
    "\n"
    '  {"target": "ACME-192-0-2-250",\n'
    '   "installer": "D:\\\\Softwares\\\\SQLServer2022-KB5093420-x64.exe",\n'
    '   "expected_build": "16.0.4265.3", "installer_sha256": "A0FA...",\n'
    '   "kb": "KB5093420", "window": {"start": "...", "end": "..."}, "confirm": true}\n'
    "\n"
    "Fields (beyond access/policy, as for host-facts):\n"
    + STATED_CONNECTION + STATED_RULES +
    "  installer        full path of the staged CU .exe ON THE TARGET (required for apply-cu)\n"
    "  expected_build   the build the instance must report afterwards (e.g. 16.0.4265.3)\n"
    "  installer_sha256 expected hash of the staged file; skip_hash: true skips the check\n"
    "  instance_name    default: read from the instance itself (SERVERPROPERTY)\n"
    "  setup_account    Windows login setup runs as; gated as a sysadmin when given\n"
    "  overrides        [\"allow-stale-backup\", \"allow-pending-reboot\", \"allow-ha\",\n"
    "                    \"ignore-window\"] - accept a named blocking gate deliberately\n"
    "\n"
    "  confirm/dry_run/assume_yes  as for host-restart: apply-cu prints what it is about to patch\n"
    "                   (including that a CU CANNOT be uninstalled) and waits for a typed \"yes\";\n"
    "                   an unattended run must carry \"assume_yes\": true\n"
    "\n"
    "The full run is: sqlserver-precheck -> host-restart -> sqlserver-precheck ->\n"
    "sqlserver-apply-cu -> host-restart -> sqlserver-verify-build. Setup exit code 3010 means\n"
    "the patch SUCCEEDED and the host must be restarted; it is never re-run.\n"
)

INVENTORY_SUMMARY_USAGE = (
    "usage: python -m db_ops.common.cli inventory-summary <json>|@<file>|- [--config ...]\n"
    "\n"
    "Renders the dated `*-summary.md` from the canonical inventory JSON, and optionally merges a\n"
    "health overlay into that JSON first.\n"
    "\n"
    "The merge/render logic lives in db_ops.lib.inventory_render because both the master-side\n"
    "control app and the worker-side reports app produce this summary. They used to hold a copy\n"
    "each - 265 identical lines that had already drifted apart - which is what the no-cross-app-\n"
    "import rule produces when the shared half is not moved here.\n"
    "\n"
    "The request is a JSON object, given inline, as @path/to/request.json, or on stdin (-):\n"
    "\n"
    '  {\"inventory\": \"data/database-inventory.json\", \"output_dir\": \"runtime/reports\"}\n'
    "\n"
    "Fields (all optional):\n"
    "  inventory    path to the canonical inventory JSON (default: data/database-inventory.json)\n"
    "  overlay      path to a dated health overlay to merge in before rendering\n"
    "  output_dir   where the dated summary is written (default: the current directory)\n"
    "  date         YYYYMMDD stamp for the output name (default: today)\n"
    "\n"
    "Prints JSON: {ok, inventory, file}. Exit 0 on success, 1 on failure.\n"
)

SECRET_SET_USAGE = (
    "usage: <request> | python -m db_ops.common.cli secret-set -\n"
    "\n"
    "Stores ONE secret in data/encrypted_secret_text.json without it ever being written in clear:\n"
    "the store is decrypted, the ref added, and the whole of it re-encrypted with the same key.\n"
    "encrypt-secret is the bulk path and needs a plaintext file first; this is the single-entry one\n"
    "- a bot token, an API key, a password no instance-add covers.\n"
    "\n"
    "The request is a JSON object ON STDIN ONLY (-). Inline JSON puts the secret on the command line,\n"
    "where every process on this machine can read it, and @file is the plaintext this exists to avoid.\n"
    "\n"
    '  {"ref": "TELEGRAM_BOT_TOKEN", "value": "<the secret>"}\n'
    "\n"
    "  ref             required. The name users.json / telegram_config.json point at\n"
    "  value           required. Never echoed back\n"
    "  overwrite       optional. Replace a ref that holds a DIFFERENT value (default: refuse)\n"
    "  also_plaintext  optional. Also write it to secrets/secret_text.json when that file exists -\n"
    "                  for a master, where deploy regenerates the store from that file\n"
    "\n"
    "READ THE plaintext_source FIELD OF THE ANSWER. encrypt-secret REPLACES the store with\n"
    "secrets/secret_text.json. If that file exists and does not hold this ref, the next encrypt-secret\n"
    "drops the secret stored here - and on a fresh node, whose scaffold holds no secrets at all, it\n"
    "drops every secret instance-add and this command ever stored.\n"
)


USAGE_APP_COMMAND_SET = """usage: python -m db_ops.common.cli app-command-set <json>|@<file>|-

Change the schedule or the state of ONE app command in data/app_commands.json. This is the only
command that edits that file; until 2026-09-22 nothing did, which is how the estate, a soak node and
the shipped catalogue came to hold three different schedules for the same app at once.

   {"app_code": "APP-BACKUP-RESTORE",              // required: which one. Not the id - app_code is
                                                   // what every log line and job_runs row says
    "time_window": {"repeat_interval": 30},        // a PARTIAL edit: named fields change, the rest
                                                   // of the window is kept
    "data_dir": "data"}                            // optional; defaults to data/

Editable: active, node_role, run_mode, max_parallel, time_window, note, sort_order, display_name.
The field list comes from the app_command entry in data/shared_config_objects.json, not from here.

Refused, and not by oversight: app_command_id, app_code, app_name, log_scope, command_text and
working_dir. Those are what the command IS rather than how it is scheduled - command_text names the
module that runs, so pointing it elsewhere is a release, not a configuration edit. It also will not
create or delete an app command: the records correspond to code that exists.

The answer names the old and the new value of every field it touched. An edit that reported only
"ok" would reproduce, in a new place, the fault this command was written to end.
"""


LIFT_EXAMPLE_USAGE = (
    "usage: python -m db_ops.common.cli lift-example <json>|@<file>|-\n"
    "\n"
    "Copies one of your configuration files over the *.example.json beside it. The examples ship:\n"
    "they are what a stranger copies to get a working tool root, so they drift in one direction -\n"
    "your file gains records as the estate grows and the example does not, until the shipped\n"
    "catalogue describes a tenth of the collectors the package carries.\n"
    "\n"
    "It does NOT scrub. It copies, then runs check-identifiers over the result, and if anything\n"
    "real would be carried across it writes nothing and names the terms. A tool that rewrote what\n"
    "it found would be a second scrubber with its own opinions.\n"
    "\n"
    "It also refuses a source naming an asset file that does not exist - a lifted catalogue that\n"
    "names a missing variant refuses to load, and it fails on somebody else's machine.\n"
    "\n"
    "  source   the file to lift from, e.g. data/metric_definitions.json\n"
    "  dest     where to write it (default: the *.example.json beside the source)\n"
    "  write    false to report what would happen and write nothing\n"
    "\n"
    '  {"source": "data/metric_definitions.json"}\n'
    '  {"source": "data/sla_policies.json", "write": false}\n'
)


CHECK_SECRET_LITERALS_USAGE = (
    "usage: python -m db_ops.common.cli check-secret-literals <json>|@<file>|- "
    "[--key ...|--key-base64 ...] [--config ...]\n"
    "\n"
    "Reports every file that ships holding a VALUE from the secret store. Not a pattern and not\n"
    "a guess: the store is decrypted and its values are searched for literally, so a hit is a\n"
    "real credential of yours sitting in a file that gets published.\n"
    "\n"
    "It exists because the other two scanners cannot see this. gitleaks matches the SHAPE of a\n"
    "credential, so a real password written into a test as an example is allowlisted as a\n"
    "placeholder; check-identifiers matches configured identifiers, and a password is not one.\n"
    "A real SA password shipped in a test from v0.2.0 to v0.4.1 in exactly that gap.\n"
    "\n"
    "It never prints a secret. A finding names the ref, the file and the line.\n"
    "\n"
    "The passphrase comes from --key/--key-base64 or DB_OPS_SECRET_KEY. Without one it refuses,\n"
    "rather than reporting a tree clean that it could not read.\n"
)


CHECK_IDENTIFIERS_USAGE = (
    "usage: python -m db_ops.common.cli check-identifiers <json>|@<file>|- [--config ...]\n"
    "\n"
    "Reports which of THIS estate's real identifiers appear in the files that ship. The terms\n"
    "are read from your own configuration - db_instances.json names every address, server_id,\n"
    "service and credential; the Telegram files name the people - so a hit is a real value you\n"
    "use, never a pattern that happened to match.\n"
    "\n"
    "Each identifier is searched in every spelling this project writes: an address dotted, then\n"
    "hyphenated inside a server_id, then underscored inside a secret ref. Those are one machine,\n"
    "and a grep for the dotted form alone reports the tree clean while two thirds of it remain.\n"
    "\n"
    "Finding something is the answer, not a failure: this exits 0 and the count is data.hits.\n"
    "It exits non-zero only when it could not run at all.\n"
    "\n"
    "  {}                                    the shipping surface, terms from the inventory\n"
    "  {\"paths\": [\"db_ops/sre\"]}              one subtree\n"
    "  {\"extra_terms\": [\"SITECODE\"]}           add a term configuration does not name\n"
    "  {\"allow\": [\"# example:\"]}             lines carrying this fragment are deliberate\n"
)

DB_STATUS_USAGE = (
    "usage: python -m db_ops.common.cli db-status <json>|@<file>|- [--config ...]\n"
    "\n"
    "What state a server is in, at the depth you ask for. One shape whichever engine answers,\n"
    "so a caller does not need to know which engine it is talking to.\n"
    "\n"
    "The instance is checked at EVERY depth: a verdict about a database on an instance nobody\n"
    "could reach is a guess, not a verdict. A state column is never the whole answer either -\n"
    "a real statement is run, because a database can read ONLINE and still refuse a query while\n"
    "it finishes an upgrade step.\n"
    "\n"
    "  {\"target\": \"ACME-192-0-2-248\"}                                  // depth: instance\n"
    "  {\"target\": \"ACME-192-0-2-248\", \"depth\": \"database\"}            // every database\n"
    "  {\"target\": \"ACME-192-0-2-248\", \"depth\": \"database\",\n"
    "   \"databases\": [\"SALES_STG\"]}                                   // just these\n"
    "  {\"target\": \"ACME-192-0-2-248\", \"depth\": \"schema\",\n"
    "   \"database\": \"APPDB\", \"schemas\": [\"sales\"]}\n"
    "\n"
    "Each request above also carries \"connection\" - see Fields.\n"
    "\n"
    "Fields:\n"
    + STATED_CONNECTION +
    "  depth           instance (default) | database | schema\n"
    "  databases       names to check at depth database; default every database\n"
    "  database        (required at depth schema on sqlserver/postgresql) which one to look in\n"
    "  schemas         names to check at depth schema; default every schema\n"
    "  timeout_seconds connect/statement timeout\n"
    "\n"
    "PostgreSQL and Oracle restore at the level of the INSTANCE, so for a restore the instance\n"
    "depth is the whole answer there. SQL Server restores one database at a time, so each can\n"
    "fail on its own and the database depth is what that engine needs.\n"
    "\n"
    "data: {\"ok\", \"depth\", \"db_type\", \"server_id\", \"instance\": {\"ok\", \"state\", ...},\n"
    "       \"items\": [{\"name\", \"kind\", \"state\", \"ok\", \"detail\"}], \"checked\", \"failed\"}\n"
)


CHECK_SECRET_USAGE = (
    "usage: python -m db_ops.common.cli check-secret <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Tries to authenticate with each secret in the store and reports what happened. A secret is\n"
    "resolved to a target by walking every config that can name it - db_instances (a database\n"
    "login or a cmd_access OS login), docker_db_connections (which carries the published\n"
    "non-default port), restore_config, users.json remote_credentials. Never the ref's own name:\n"
    "a secret is sent only to a host the configuration names for it.\n"
    "\n"
    "When cmd_access does not state a method the protocol is PROBED: SSH on 22, then WinRM on\n"
    "5985/5986. The estate is mixed, and asking an Ubuntu host over WinRM reports it unreachable\n"
    "when that is only the wrong question.\n"
    "\n"
    "The request is a JSON object; {} checks the whole store:\n"
    "\n"
    '  {\"match\": \"DBA_USER_DBA\"}\n'
    "\n"
    "Fields (all optional):\n"
    "  refs             list of password_ref names to check\n"
    "  match            regex matched against password_ref NAMES\n"
    "  timeout_seconds  connect timeout (default 8)\n"
    "  allow_name_host  removed in 0.26.0 - a request carrying true is refused\n"
    "\n"
    "Statuses: OK | AUTH_FAILED | UNREACHABLE | CONNECT_FAILED | NOT_A_LOGIN (key material or a\n"
    "service token - there is nothing to log in to) | NO_TARGET (no config names the ref, or the\n"
    "one that does carries no host) | UNKNOWN_REF. Exit 1 if any secret resolved to NO_TARGET.\n"
)

PROBE_HOST_USAGE = (
    "usage: python -m db_ops.common.cli probe-host <json>|@<file>|-\n"
    "\n"
    "What a host is listening on, and what db_ops can therefore do with it. The question three\n"
    "throwaway socket loops have each answered differently; this is the one answer.\n"
    "\n"
    "The request is a JSON object. Nothing is read: the address is the request's, and an app\n"
    "holding only a server_id fills it (lib.data_sources.request_fill).\n"
    '  {\"host\": \"192.0.2.236\",         // (required) the machine\n'
    '   \"target\": \"ACME-192-0-2-236\",  // the label the answer carries (the server_id)\n'
    '   \"os\": \"Windows Server 2003\",    // optional, and it changes the verdict (see below)\n'
    '   \"ports\": [22, 5985, 3389],       // default: ssh, msrpc, smb, the 4 DB ports, rdp, winrm\n'
    '   \"timeout_seconds\": 3}\n'
    "\n"
    "verdict is one of:\n"
    "  manageable        SSH or WinRM answers - a command can run and a login can be proven.\n"
    "  interactive_only  RDP answers and no management port does. With a known OS the detail\n"
    "                    says whether that is fixable: Windows Server 2003 ships no WinRM and\n"
    "                    cannot run the OpenSSH server, so there is no service to go enable.\n"
    "  service_only      a database port answers but no management port.\n"
    "  unreachable       nothing answered. A refusal still proves the host is up; a timeout\n"
    "                    does not, and the detail says which happened.\n"
    "\n"
    "Each port reports open/refused/timeout separately, because on a live host a refusal means\n"
    "the service is off and a timeout means a filter - the distinction that told .235/.236 apart\n"
    "from a firewalled box. Exit 0 unless the probe could not be set up.\n"
)


ROTATE_PASSWORD_USAGE = (
    "usage: python -m db_ops.common.cli rotate-password <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Changes a database login's password ON THE SERVER and records it in the secret store, as\n"
    "one operation - the two drift apart when either is done alone.\n"
    "\n"
    "Per target: connect with the current password, issue the engine's change statement,\n"
    "re-authenticate on a NEW connection, and only then store the value. A failed verify is\n"
    "rolled back. A target whose current password already fails is SKIPPED, never guessed at.\n"
    "Every target gets its own generated password; sharing one would rebuild the weakness a\n"
    "rotation exists to remove.\n"
    "\n"
    "The request is a JSON object, given inline, as @path/to/request.json, or on stdin (-):\n"
    "\n"
    '  {"match": "DBA_USER_DBA", "dry_run": true}\n'
    "\n"
    "Fields (all optional except that one of refs/match must select something):\n"
    "  refs             list of password_ref names to rotate\n"
    "  match            regex matched against password_ref NAMES (never values)\n"
    "  dry_run          true = connect and report READY, change nothing. Do this first.\n"
    "  password_length  generated length (default 28, minimum 12)\n"
    "  passwords        {password_ref: value} to set a specific value instead of generating\n"
    "  host_overrides   {password_ref: ip} to pin which node of a clustered instance to use\n"
    "  timeout_seconds  connect timeout (default 10)\n"
    "\n"
    "Prints JSON: {ok, selected, summary, results[]}. Passwords are never printed or logged.\n"
    "Statuses: SUCCESS | READY (dry run) | SKIPPED (not attempted) | FAILED (attempted, no change\n"
    "kept). Exit 0 when nothing FAILED, 1 otherwise.\n"
)

AUTHORIZE_USAGE = (
    'usage: python -m db_ops.common.cli authorize <json>|@<file>|-\n'
    '                                   [--config ...]\n'
    '\n'
    'The confirmation gate on its own, for an operation this CLI does not perform. Every other\n'
    'gate here confirms and then acts; a caller whose work lives elsewhere -- an app, which may\n'
    'not import common -- asks for the authorization and performs the work itself.\n'
    '\n'
    '  authorize {"operation": "run-sql-task", "target_id": "24",\n'
    '             "target_label": "sql_id 24 payroll engine",\n'
    '             "effects": ["runs on 1 target: ACME-192-0-2-115"],\n'
    '             "confirm": "yes", "reason": "asked over telegram",\n'
    '             "authorized_by": {"channel": "telegram"}}\n'
    '\n'
    'How much it costs is the request\'s "rules" - the node\'s own data/emergency_operations.json\n'
    'entry, stated by the app, since common.cli reads no configuration; without it the ladder the\n'
    'package ships prices it, and an operation no ladder lists costs the most, not the least. The answer\n'
    'may be typed at the prompt, carried in the request ("confirm": "yes", how a chat command\n'
    'passes the reply it collected), or waived by "assume_yes": true for genuinely unattended\n'
    'automation. Exit 0 means authorized; the gate report is the JSON on stdout.\n'
)

ASK_USAGE = (
    'usage: python -m db_ops.common.cli ask <json>|@<file>|-\n'
    '\n'
    'One question on the controlling terminal, for a caller that reaches this CLI through a pipe\n'
    'and so cannot ask it itself - the deploy\'s config-drift gate asks "adopt / keep / abort".\n'
    'The same terminal, deadline and "silence is no answer" as the confirmation gate.\n'
    '\n'
    '  ask {"prompt": "  adopt / keep / abort ? "}\n'
    '  ask {"prompt": "Continue? ", "choices": ["yes", "no"], "tries": 3}\n'
    '  ask {"prompt": "Continue? ", "choices": ["yes", "no"], "answer": "no"}   // collected already\n'
    '\n'
    'choices           the accepted answers (lower case); none means any line is the answer\n'
    'tries             how often a person is asked again after an answer not in choices (1)\n'
    'deadline_seconds  how long to wait for a line (default 120); nothing by then is ""\n'
    'answer            a reply the caller already has, held to choices like a typed one\n'
    '\n'
    'data: {"interactive", "answer", "source"} - interactive false and answer "" when there is no\n'
    'terminal to ask on; an empty answer is never a choice made for the person.\n'
)


EMERGENCY_USAGE = (
    "usage: python -m db_ops.common.cli <shrink-log|kill-spid|start-job|disable-job> <json>|@<file>|-\n"
    "                                   [--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "The three emergency actions on a SQL Server instance. JSON object in, gate report out --\n"
    "the same contract as run-sql and host-restart.\n"
    "\n"
    '  shrink-log  {\"target\": \"ACME-192-0-2-115\", \"database\": \"SALESDB\", \"size_mb\": 5120,\n'
    '               \"confirm\": true, \"reason\": \"log filled L:\"}\n'
    '  kill-spid   {\"target\": \"ACME-192-0-2-115\", \"spid\": 723, \"confirm\": true}\n'
    '  start-job   {\"target\": \"ACME-192-0-2-115\", \"job_name\": \"Backup_Log\", \"confirm\": true}\n'
    '  disable-job {\"target\": \"ACME-192-0-2-115\", \"job_name\": \"OptimizeIndex_Weekly\",\n'
    '               \"confirm\": true, \"reason\": \"filling L: again\"}\n'
    "\n"
    "disable-job is the only one of these that is not SQL Server-only: it disables an Agent job,\n"
    "an Oracle DBMS_SCHEDULER or DBMS_JOB entry, or a pg_cron row, dispatching on what list-jobs\n"
    "says owns the name. It stops future runs only - a run already in progress keeps going.\n"
    "\n"
    "Each request above also carries \"connection\" (the SQL login) and \"rules\" - see below.\n"
    "\n"
    "How much it costs to authorize is per operation, not a flag: level 100 (takes something\n"
    "down) needs two answers, level 50 needs one. The request's \"rules\" state it - the node's\n"
    "own data/emergency_operations.json entry, sent by the app; without them the ladder the\n"
    "package ships prices it. common.cli reads no configuration.\n"
    "\n"
    "  \"confirm\": true      intent. Required by every one of them. The answers are then typed\n"
    "                       at the terminal.\n"
    "  \"confirm\": \"yes\"     answer 1, supplied in the request instead of at a prompt -- how the\n"
    "                       Telegram processor passes on what a human already replied.\n"
    "  \"confirm_target\"     answer 2, level 100 only: the target's own server_id, typed out.\n"
    "                       A payload written for one host is refused by another.\n"
    "  \"assume_yes\": true   unattended automation. Recorded as such: no human was asked.\n"
    "  \"dry_run\": true      run the pre-checks and print what would happen. Never prompts.\n"
    "\n"
    "Other fields: reason (shown in the banner and stored), timeout_seconds, and\n"
    + STATED_CONNECTION + STATED_RULES +
    "Exit 0 unless a blocking gate failed. Progress goes to stderr, the JSON report to stdout.\n"
)

RUN_SQL_USAGE = (
    "usage: python -m db_ops.common.cli run-sql @<file>|-\n"
    "\n"
    "Runs SQL against ONE database target and prints the first result set.\n"
    "The request is a JSON object, as @path/to/request.json or on stdin (-) - it carries a\n"
    "password, so never inline:\n"
    '  {"connection": {...},              // (required) the login - see Fields below\n'
    '   "target": "ACME-192-0-2-115",     // the label the answer carries (the server_id)\n'
    '   "sql": "SELECT TOP 10 * FROM sys.objects",   // or "sql_file": "query.sql"\n'
    '   "database": "SALESDB",            // optional; SQL Server default is master\n'
    '   "secrets": {"<ref>": "..."},      // optional; the refs an 8i bridge\'s sql_access\n'
    "                                     // names (its signing secret) - no store is opened\n"
    '   "max_rows": 50000,                // optional; result is truncated past this\n'
    '   "timeout_seconds": 30,            // optional; connect timeout\n'
    '   "commit": false,                  // optional; default false = always rolled back\n'
    '   "autocommit": false,              // optional; true = no transaction. Needed to\n'
    "                                     // reproduce a metric: metric SQL catches errors\n"
    "                                     // inside a cursor, and in a transaction one caught\n"
    "                                     // error dooms the batch (3930). Read-only SQL only.\n"
    '   "params": [505, "SALESDB"],          // optional; BOUND to the placeholders in the SQL,\n'
    "                                     // never pasted into it. Positional: ? for pyodbc,\n"
    "                                     // %s for pg8000/pymssql. An object is refused -\n"
    "                                     // named binding is spelled differently per driver.\n"
    '   "prelude": "DECLARE @spid int = ?;",   // optional; prepended to EVERY batch, because\n'
    "                                     // a T-SQL variable does not survive a GO. Build it\n"
    "                                     // with lib.sql_text.build_parameter_prelude, which\n"
    "                                     // validates each name and type first.\n"
    '   "named_params": {"job_no": "AA1"},  // optional; Oracle / PostgreSQL only. BOUND where\n'
    "                                     // the SQL says :job_no, in the driver's own style;\n"
    "                                     // not with params/prelude. A name no statement\n"
    "                                     // says is refused before connecting.\n"
    '   "capture": "first",               // optional; first (default) | all. Default keeps the\n'
    "                                     // first result set and drains the rest without\n"
    "                                     // fetching their rows; all keeps them.\n"
    '   "max_result_sets": 20}            // optional; capture:all only. 0 = no cap.\n'
    "\n"
    "Fields:\n"
    + STATED_CONNECTION +
    "\n"
    "WHICH TOOL RUNS IT. The request states the facts; the answer says what they selected.\n"
    "Every field below is optional and empty means 'use what the connection states':\n"
    '   \"major_version\": 8,               // engine major version. THE field that decides a\n'
    "                                     // driver: python-oracledb speaks 12.1+ only, so an\n"
    "                                     // Oracle below that is refused here with the fix in\n"
    "                                     // the message instead of failing as DPY-3010.\n"
    '   \"driver\": \"pymssql\",              // name the driver instead of letting the rule pick.\n'
    "                                     // SQL Server only; sql_access already did this for\n"
    "                                     // the transport, the driver had no equivalent.\n"
    '   \"oracle_client_mode\": \"thick\",    // thin (default) | thick. thick loads an Oracle\n'
    "                                     // client library and is how a pre-12.1 server is\n"
    "                                     // reached without the bridge.\n"
    '   \"platform\": \"windows\", \"os\": \"Windows Server 2019 ... 10.0 (Build 17763)\",\n'
    '   \"runtime\": \"docker\",              // host (default) | docker | k8s\n'
    '   \"profile\": {...}                  // the same keys as one block, when a caller has them all\n'
    "\n"
    'The answer carries "engine" (what it ran against, with a "sources" map saying which of\n'
    "request/config supplied each fact) and \"tool\" ({tool, chosen_by, reason}). chosen_by is\n"
    "request | config | rule | default - it names where to go and edit when the choice surprises\n"
    "you. Both are present on the legacy-bridge path too, so one shape reads either transport.\n"
    "\n"
    'The answer always carries "result_sets": [{columns, rows, row_count, truncated}, ...] - one\n'
    "entry under the default, every set under capture:all. The top-level columns/rows stay the\n"
    "FIRST set, unchanged. \"result_sets_truncated\" says max_result_sets cut something off.\n"
    "\n"
    "Neither params nor prelude works through the legacy Oracle bridge (sql_access api /\n"
    "subprocess): that tool binds nothing, so such a request is refused rather than run without\n"
    "the values.\n"
    "\n"
    "How the result is rendered is part of the request, not a flag, so a config file can carry it:\n"
    '   "format": "json"        json (default) | txt | xml | xlsx | raw\n'
    '   "output_path": "runtime/exports/x.xlsx"   // required by xlsx; relative = tool root\n'
    "\n"
    "  json  the only one safe to parse.        txt   aligned table, for reading.\n"
    "  xml   structure without a JSON parser.   raw   values only, tab-separated, no header,\n"
    "  xlsx  writes a workbook, prints where.         so `| cut -f2` works.\n"
    "A SQL NULL prints as NULL in every text format - never as an empty string, which would be\n"
    "indistinguishable from an empty one.\n"
    "\n"
    "Exit 0 on success, 1 on a run failure (the JSON output carries {\"ok\": false, \"error\"}).\n"
)


LIST_TARGETS_USAGE = (
    "usage: python -m db_ops.common.cli list-targets [<json>|@<file>|-]\n"
    "\n"
    "List the database targets (server_id, db_type, ip:port) - the same listing the\n"
    "Telegram /spbot_list_server_id command replies with.\n"
    "\n"
    '  {"format": "json"}   json (default) | txt\n'
    "\n"
    "json answers in the standard response envelope, with the targets in data.targets, so a\n"
    "program can consume it. txt is the human listing this command used to print unconditionally\n"
    "- kept for pasted runbook lines, and it is still what the Telegram reply renders.\n"
)


RUN_CMD_USAGE = (
    "usage: python -m db_ops.common.cli run-cmd @<file>|-\n"
    "\n"
    "Run ONE shell command on a host, over the login the request states.\n"
    "The command-line counterpart of run-sql: same JSON-object contract.\n"
    "\n"
    "The request is a JSON object, as @path/to/request.json or on stdin (-):\n"
    '  {\"access\": {...},          // (required) the host login - see Fields below\n'
    '   \"target\": \"CLOUD-203-0-113-188-ORA-1521\",  // the label (the server_id)\n'
    '   \"command\": \"df -h /\",     // OR \"script\": \"multi-line text run as a script\"\n'
    '   \"timeout_seconds\": 60,\n'
    '   \"confirm\": true,          // REQUIRED: this runs arbitrary code on a real host\n'
    '   \"assume_yes\": false,      // skip the terminal prompt (for non-interactive callers)\n'
    '   \"format\": \"json\"}         // json (default) | txt | raw\n'
    "\n"
    "\"confirm\" is required because nothing here can tell `df -h` from `rm -rf /`. host-facts and\n"
    "host-service already work this way; this command cannot classify itself, so it always asks.\n"
    "\n"
    "Fields:\n"
    + STATED_ACCESS +
    "\n"
    "WHAT THE HOST IS. Optional, and empty means 'what access states'. These decide which\n"
    "shell dialect a script builder may use — platform alone cannot, because Get-CimInstance and\n"
    "ConvertTo-Json are PowerShell 3.0 (Windows Server 2012+) and older hosts need Get-WmiObject:\n"
    '   \"platform\": \"windows\",          // windows | linux\n'
    '   \"os\": \"Windows Server 2012 R2 ... 6.3\",  // parsed to an NT version when it carries one\n'
    '   \"os_major\": 6, \"os_minor\": 3,   // or state it outright\n'
    '   \"runtime\": \"docker\",            // host (default) | docker | k8s\n'
    '   \"profile\": {...}                // the same keys as one block\n'
    "\n"
    "The json answer carries \"host_profile\" (with a \"sources\" map naming who stated each field)\n"
    "and \"shell_dialect\" ({tool: cim|wmi, chosen_by, reason}) alongside the command's output.\n"
    "\n"
    "format raw prints stdout verbatim and nothing else, so it pipes. xml/xlsx are not offered:\n"
    "a command's stdout is not a result set, and rendering it as one would be a claim about\n"
    "structure that is not there - use run-sql for that.\n"
    "\n"
    "Exit code is the command's own exit code (capped at 1 for a db_ops-level failure).\n"
)


FILE_TRANSFER_USAGE = (
    "usage: python -m db_ops.common.cli fetch-file|send-file|pack-files|relay-file @<file>|-\n"
    "\n"
    "Move ONE named file between this host and a remote one, over the SSH login the request\n"
    "states in \"access\". fetch-file pulls it here; send-file pushes it there.\n"
    "\n"
    "For a file you can name - a backup piece to inspect, a script to place, a log to collect.\n"
    "NOT for staging a backup set: per-file SFTP across two internet hops measured 10 KB/s here,\n"
    "which is why backup_restore streams a whole directory as one tar instead.\n"
    "\n"
    "More than one file: pack-files makes them ONE archive on the host that already holds them,\n"
    "then fetch-file moves it. The result carries the archive's sha256 and size, so what landed\n"
    "can be proven identical to what was packed - size alone catches a truncated copy, not a\n"
    "corrupted one.\n"
    '  {\"access\": {...}, \"folder\": \"/opt/oracle/backup/dbops\", \"include\": \"*.bkp\",\n'
    '   \"archive_path\": \"/tmp/pieces.tar\", \"format\": \"tar\"}   // or \"files\": [...]\n'
    "\n"
    "The request is a JSON object, as @path/to/request.json or on stdin (-):\n"
    '  {"access": {...},            // (required) the host login - see Fields below\n'
    '   "target": "CLOUD-203-0-113-188-ORA-1521",  // the label (the server_id)\n'
    '   "remote_path": "/opt/oracle/backup/dbops/FREE_L0_20260802_f32div12_3555_1_1.bkp",\n'
    '   "local_path": "runtime/incoming/FREE_L0_20260802_f32div12_3555_1_1.bkp",\n'
    '   "overwrite": false,         // default false; a same-size destination is skipped either way\n'
    '   "make_dirs": true}          // default true; create the destination directory\n'
    "\n"
    "A relative local_path resolves against the tool root, never the process's working directory.\n"
    "Every transfer is size-verified; a short copy removes what it wrote and exits 1.\n"
    "\n"
    "relay-file moves a file from one host STRAIGHT to another, streaming through here\n"
    "without staging it on this disk, and comparing the sha256 taken at both ends of the\n"
    "whole trip. Linux/SSH on both sides. Two commands would stage the bytes here and\n"
    "verify each hop separately, which names the wrong hop when the hashes differ.\n"
    "Each side states its own login:\n"
    '  {"source": {"access": {...}, "target": "ACME-192-0-2-249-HOST", "path": "/tmp/bundle.tar.gz"},\n'
    '   "destination": {"access": {...}, "target": "ACME-192-0-2-11-LABSQL-1433", "path": "/tmp/b.tar.gz"},\n'
    '   "overwrite": false, "make_dirs": true}\n'
    "\n"
    "Fields:\n"
    + STATED_ACCESS
)


TRACE_SESSION_USAGE = (
    "usage: python -m db_ops.common.cli trace-session '<json>'|@<file>|-\n"
    "\n"
    "Every open transaction on one SQL Server, with WHO the application says is behind it.\n"
    "On a three-tier estate every session reads login=<service account> host=<app server>, which\n"
    "names nobody; Dynamics AX writes the real caller into context_info and this decodes it.\n"
    "\n"
    '  {\"target\": \"ACME-192-0-2-115\", \"database\": \"SALESDB\", \"min_tran_seconds\": 300}\n'
    '  {\"target\": \"ACME-192-0-2-115\", \"database\": \"SALESDB\", \"session_id\": 505}\n'
    '  {\"target\": \"ACME-192-0-2-115\", \"database\": \"SALESDB\", \"blocking_only\": true}\n'
    "\n"
    "Each request above also carries \"connection\" - see Fields.\n"
    "\n"
    "Fields:\n"
    + STATED_CONNECTION +
    "  database         database whose transaction log usage is reported (default: login's)\n"
    "  session_id       trace exactly one SPID instead of scanning\n"
    "  min_tran_seconds ignore transactions younger than this (default 60)\n"
    "  blocking_only    only sessions that are blocking someone\n"
    "  timeout_seconds  connect/statement timeout\n"
    "\n"
    "Read-only: it runs through run-sql, which always rolls back.\n"
)


METRIC_SEVERITY_USAGE = (
    "usage: python -m db_ops.common.cli metric-severity '<json>'|@<file>|-\n"
    "\n"
    "Remap one metric's statuses for one server_id, in db_instances.json. The write behind\n"
    "\"this finding is real but standing, and nobody is going to act on it\": collection and\n"
    "history are untouched, it just stops being an alert.\n"
    "\n"
    '  {\"server_id\": \"ACME-192-0-2-115\",\n'
    '   \"metric_code\": \"LOCK_SLEEPING_OPEN_TRANSACTION\",\n'
    '   \"severity_map\": {\"WARNING\": \"LOGGING\"},\n'
    '   \"note\": \"why this is not an incident\"}\n'
    "\n"
    "Fields:\n"
    "  server_id    (required) the instance, as written in db_instances.json\n"
    "  metric_code  (required) validated against metric_definitions.json\n"
    "  severity_map (required) {from: to}; OK LOGGING WARNING CRITICAL ERROR NO_DATA.\n"
    "               {} or null removes the remap and restores the metric's own grading.\n"
    "  metric_item  scope the remap to one item instead of the whole metric on this server\n"
    "  note         why, stored next to it and read back through Telegram\n"
    "  data_dir     folder holding db_instances.json (default: data/)\n"
)


SELF_STATUS_USAGE = (
    "usage: python -m db_ops.common.cli self-status <json>|@<file>|- [--config ...]\n"
    "\n"
    "What THIS installation is and how much room it has left: version, host name and ip,\n"
    "node role, cpu, memory and disk, and the app commands it schedules. Reads itself - no\n"
    "SSH, no store - so it still answers when the store is unreachable. Each app command's\n"
    "last run is in the store, which this command does not open: the caller states it\n"
    "(/spbot_self_status does, from its node's store).\n"
    "\n"
    "The request is a JSON object, given inline, as @path/to/request.json, or on stdin (-):\n"
    '  {"format": "txt",      // optional; txt for the chat listing, json (default) for the envelope\n'
    '   "last_runs": {"APP-X": {"status": "done", "started_at": "2026-09-26T01:00:00Z"}},\n'
    '                         // optional; the newest run of each app command in the last day\n'
    '   "store_error": "..."} // optional; why the caller could not read them\n'
)
