"""CLI for the shared ``common`` layer: config-write admin + shared lookups.

Every db_ops app has a CLI entrypoint; this is the one for the shared layer. It is a
thin facade — all logic stays in the common modules it fronts:

* ``add-sql`` / ``metric-toggle``  -> :mod:`db_ops.common.config_admin` (atomic config writes)
* ``list-targets``                 -> :mod:`db_ops.common.data_sources` (the same listing
                                      the Telegram ``/spbot_list_server_id`` command replies with)
* ``run-sql``                      -> :mod:`db_ops.common.sql_run` (run SQL on one database
                                      target from a JSON request object)
* ``copy-schema``                  -> :mod:`db_ops.common.schema_copy` (reproduce one SQL
                                      Server schema from instance A on instance B,
                                      planning before it writes)
* ``rotate-password``              -> :mod:`db_ops.common.password_rotation` (change a login's
                                      password on the server and in the secret store together)
* ``check-secret``                 -> :mod:`db_ops.common.secret_check` (prove each secret still
                                      logs in, and say precisely why any cannot be checked)
* ``inventory-summary``            -> :mod:`db_ops.lib.inventory_render` (merge a health
                                      overlay and render the inventory summary)

Every command here takes a **JSON object** — inline, ``@path/to/request.json``, or on stdin
(``-``) — parsed by the shared :func:`_read_json_request`. Never flags for the payload: it is the
shape ``data/*.json`` already has, so config, a Telegram action and a shell caller pass the same
object through untranslated, and a new field never breaks an existing caller.
``tests/test_common_cli_json_contract.py`` holds every command to that, including new ones.

Three commands — ``add-sql``, ``metric-toggle`` and ``list-targets`` — predate the rule and
still accept their original flag/word arguments, so pasted runbook lines keep working. The object
is the contract; the old form is compatibility. :func:`_optional_json_request` tells them apart.

``check-credentials`` used to be a fourth. It moved to ``db_ops/cli.py`` on 2026-08-15: answering
it needs the metrics and Telegram resolvers, and this layer may not import an app. It was the only
reason ``common`` ever did.

Usage::

    python -m db_ops.common.cli add-sql '{"db_type": "sqlserver", "server_id": "...", "sql_name": "...", "sql_file": "..."}'
    python -m db_ops.common.cli metric-toggle '{"server_id": "...", "state": "off", "scope": "collector:cmd"}'
    python -m db_ops.common.cli list-targets
    python -m db_ops.common.cli run-sql '{"target": "ACME-192-0-2-115", "database": "SALESDB", "sql": "SELECT 1 AS x"}'
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from db_ops.common import config_admin
from db_ops.lib.json_io import looks_like_json_request

USAGE = (
    "usage: python -m db_ops.common.cli <command> ...\n"
    "commands:\n"
    "  add-sql         Register + enable a new SQL task (see --help)\n"
    "  metric-toggle   Enable/disable metrics for one server_id (see --help)\n"
    "  list-targets    List the database targets (server_id, db_type, ip:port)\n"
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
    "  rotate-password  Change database login passwords on the server AND in the store (see --help)\n"
    "  check-secret     Try to authenticate with each secret and say why any cannot be (see --help)\n"
    "  check-identifiers  Which of this estate's real names appear in files that ship (see --help)\n"
    "  lift-example     Refresh a data/*.example.json from your own file, refusing identifiers\n"
    "  probe-host       What a host listens on, and what db_ops can do with it (see --help)\n"
    "  metric-severity  Remap one metric's statuses for one server_id, e.g. WARNING -> LOGGING (see --help)\n"
    "  trace-session    Who is holding an open transaction — the app user behind a SPID (see --help)\n"
    "  inventory-summary  Merge a health overlay and render the inventory summary (see --help)\n"
    "  restore-database  Run one configured restore: any target shape, PITR where supported (see --help)\n"
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
    "  verify-restore   Is the restored database actually usable, not just 'done' (see --help)\n"
    "  fetch-file       Copy one named file from a host to here (see --help)\n"
    "  send-file        Copy one named file from here to a host (see --help)\n"
    "  pack-files       Pack named files (or a folder) into one archive + sha256 (see --help)\n"
    "  relay-file       Copy one file from one host straight to another, hash-verified\n"
    "  host-facts       Read one host's state: uptime, disks, services, pending reboot (see --help)\n"
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
    '  {"target": "ACME-192-0-2-115", "output_dir": "runtime/instance_bundles/70-115"}\n'
    "\n"
    "  replay : apply a bundle to a target, in dependency order, with version/edition gates.\n"
    '  {"target": "NEW-HOST", "bundle_dir": "runtime/instance_bundles/70-115",\n'
    '   "phase": "pre-database", "dry_run": true}\n'
    "\n"
    "  verify : compare a target against a bundle; the headline number is orphaned users.\n"
    '  {"target": "NEW-HOST", "bundle_dir": "runtime/instance_bundles/70-115"}\n'
    "\n"
    "Fields:\n"
    "  target       (required) server_id, ip, or '<db_type> <ip> [port]'\n"
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
    "are exported as placeholders and resolved at replay from the encrypted secret store.\n"
    "Replay fails closed, listing every unresolved reference, before executing anything.\n"
    "\n"
    "Prints the gate report as JSON. Exit 0 unless a blocking gate failed.\n"
)

HOST_FACTS_USAGE = (
    "usage: python -m db_ops.common.cli host-facts <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Reads one host's state over its configured cmd_access - Windows or Linux, same output\n"
    "shape - and gates the things an operator would otherwise have to eyeball. Read-only.\n"
    "\n"
    '  {"target": "ACME-192-0-2-250", "services": ["MSSQL$APPDB"]}\n'
    "\n"
    "Fields:\n"
    "  target     (required unless 'access' is given) server_id, ip, or '<db_type> <ip> [port]'\n"
    "  access     an inline cmd_access object, for a host that is not in db_instances.json\n"
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
    "  target/access  as for host-facts\n"
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
    "  target/access   as for host-facts\n"
    "  services        services that must be up again before the restart counts as finished\n"
    "  reason          recorded in the host's shutdown event log and in the evidence file\n"
    "  confirm         (required) true = the payload means it\n"
    "  dry_run         true = run every check and print what would happen, restart nothing\n"
    "  assume_yes      true = unattended automation; nobody is prompted (see below)\n"
    "  window          {\"start\": \"2026-08-03 19:00\", \"end\": \"2026-08-03 21:00\"}, or any\n"
    "                  time_window block; ignore_window: true records the breach and proceeds\n"
    "  wait            per-run timeout overrides (see data/maintenance_policy.json)\n"
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
    "The three SQL Server cumulative-update capabilities. One server_id resolves BOTH halves of\n"
    "the target: the host (cmd_access) and the instance (the SQL login).\n"
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
    "Fields (beyond the host fields above):\n"
    "  installer        full path of the staged CU .exe ON THE TARGET (required for apply-cu)\n"
    "  expected_build   the build the instance must report afterwards (e.g. 16.0.4265.3)\n"
    "  installer_sha256 expected hash of the staged file; skip_hash: true skips the check\n"
    "  instance_name    default: read from the instance itself (SERVERPROPERTY)\n"
    "  credential_name  SQL login to check with; default = the instance's own\n"
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

def _lift_example_command(argv: list[str]) -> int:
    """Refresh one `data/*.example.json` from the operator's own file."""
    from db_ops.common.example_lift import LiftError, lift_example
    from db_ops.lib import response

    if not argv:
        print(LIFT_EXAMPLE_USAGE, file=sys.stderr)
        return response.emit(response.fail("lift-example", "no request given; see --help"))
    request, code = _read_json_request(argv[0], LIFT_EXAMPLE_USAGE)
    if request is None:
        return code

    source = str(request.get("source") or "").strip()
    if not source:
        # The envelope, not a usage dump: a caller must not have to parse prose off stderr to
        # learn that its request was wrong. Usage still goes to stderr for the person.
        print(LIFT_EXAMPLE_USAGE, file=sys.stderr)
        return response.emit(response.fail(
            "lift-example", "'source' is required: the file to lift from."))
    default_dest = Path(source).with_suffix("").with_suffix(".example.json")
    dest = str(request.get("dest") or default_dest)

    try:
        summary = lift_example(
            source=source,
            dest=dest,
            blank_keys=tuple(request.get("blank_keys") or ()),
            write=bool(request.get("write", True)),
        )
    except LiftError as exc:
        return response.emit(response.fail("lift-example", str(exc)))

    # A finding is a fact about the source, not a failure of the command - the same distinction
    # `check-identifiers` makes. Callers gate on `data.identifier_hits`.
    return response.emit(response.ok(
        "lift-example", message=summary["message"], data=summary,
        metrics={"records": summary["records"], "identifier_hits": summary["identifier_hits"]},
    ))


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

CHECK_SECRET_USAGE = (
    "usage: python -m db_ops.common.cli check-secret <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Tries to authenticate with each secret in the store and reports what happened. A secret is\n"
    "resolved to a target by walking every config that can name it - db_instances (a database\n"
    "login or a cmd_access OS login), docker_db_connections (which carries the published\n"
    "non-default port), restore_config, users.json remote_credentials - and only then the\n"
    "standard key name.\n"
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
    "  allow_name_host  derive the host from the standard key name when no config names the ref\n"
    "                   (default true - this command only reads, so a guess costs nothing)\n"
    "  timeout_seconds  connect timeout (default 8)\n"
    "\n"
    "Statuses: OK | AUTH_FAILED | UNREACHABLE | CONNECT_FAILED | NOT_A_LOGIN (key material or a\n"
    "service token - there is nothing to log in to) | NO_TARGET (a config names it but carries no\n"
    "host) | UNKNOWN_REF. Exit 1 if any secret resolved to NO_TARGET.\n"
)

PROBE_HOST_USAGE = (
    "usage: python -m db_ops.common.cli probe-host <json>|@<file>|- [--config ...]\n"
    "\n"
    "What a host is listening on, and what db_ops can therefore do with it. The question three\n"
    "throwaway socket loops have each answered differently; this is the one answer.\n"
    "\n"
    "The request is a JSON object:\n"
    '  {\"target\": \"ACME-192-0-2-236\",   // server_id / ip - the inventory supplies ip and os\n'
    '   \"host\": \"192.0.2.236\",        // OR the machine outright; then NOTHING is read\n'
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
    "How much it costs to authorize is per operation in data/emergency_operations.json, not a\n"
    "flag: level 100 (takes something down) needs two answers, level 50 needs one.\n"
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
    "Other fields: reason (shown in the banner and stored), credential_name, timeout_seconds.\n"
    "Exit 0 unless a blocking gate failed. Progress goes to stderr, the JSON report to stdout.\n"
)

RUN_SQL_USAGE = (
    "usage: python -m db_ops.common.cli run-sql <json>|@<file>|- [--key ... | --key-base64 ...]\n"
    "\n"
    "Runs SQL against ONE database target and prints the first result set.\n"
    "The request is a JSON object, given inline, as @path/to/request.json, or on stdin (-):\n"
    '  {"target": "ACME-192-0-2-115",   // server_id, or "<db_type> <ip> [port]"\n'
    '   "sql": "SELECT TOP 10 * FROM sys.objects",   // or "sql_file": "query.sql"\n'
    '   "database": "SALESDB",               // optional; default = the instance database\n'
    '   "credential_name": "...",         // optional; default = the instance\'s\n'
    "                                     // default_credential_name (alias: user_ref)\n"
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
    '   "capture": "first",               // optional; first (default) | all. Default keeps the\n'
    "                                     // first result set and drains the rest without\n"
    "                                     // fetching their rows; all keeps them.\n"
    '   "max_result_sets": 20}            // optional; capture:all only. 0 = no cap.\n'
    "\n"
    "\n"
    "WHICH TOOL RUNS IT. The request states the facts; the answer says what they selected.\n"
    "Every field below is optional and empty means 'use what db_instances.json says', so a\n"
    "request that states nothing behaves exactly as it always has:\n"
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

# The standard notify levels. Routing itself moved to the Telegram app
# (`db_ops.telegram.cli route|groups`) — `common` reads no Telegram settings; this list is only
# the vocabulary other commands here validate against.
_TELEGRAM_LEVELS = ("logging", "warning", "critical", "error", "test", "private")


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

def _run_sql_command(argv: list[str]) -> int:
    """Run one JSON request through :func:`db_ops.common.sql_run.run_sql` and print the result.

    The request travels as a JSON object (inline, ``@file``, or ``-`` for stdin) so a caller
    composes config, not command-line flags — the same object an app passes to the API. Failures
    print ``{"ok": false, "error": ...}`` and exit 1, so a shell caller can read either outcome
    from the same JSON instead of parsing stderr.
    """
    import json

    from db_ops.common import sql_run
    from db_ops.lib.secret_text import set_key_env

    if not argv or argv[0] in {"-h", "--help"}:
        print(RUN_SQL_USAGE, file=sys.stderr)
        return 0 if argv else 2

    source = argv[0]
    # The credential password lives in the encrypted secret file, so the run needs the key the
    # same way every other db_ops CLI takes it: --key / --key-base64, else DB_OPS_SECRET_KEY.
    rest = argv[1:]
    key = key_base64 = None
    while rest:
        flag = rest.pop(0)
        value = rest.pop(0) if rest else ""
        if flag == "--key":
            key = value
        elif flag in {"--key-base64", "--key_base64"}:
            key_base64 = value
        else:
            print(f"Unknown run-sql option: {flag}\n\n{RUN_SQL_USAGE}", file=sys.stderr)
            return 2
    try:
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, RUN_SQL_USAGE)
    if request is None:
        return code

    # The rendering is chosen inside the request object, like everything else about the run: a
    # `--format` flag would be the one part of the contract a config file could not carry.
    fmt = request.get("format")
    output_path = request.get("output_path")

    from db_ops.lib import response

    try:
        result = sql_run.run_sql(request)
    except sql_run.SqlRunError as exc:
        return response.emit(response.fail("run-sql", str(exc)))

    from db_ops.lib import result_format

    safe = sql_run.json_safe_result(result)
    rendered = str(fmt or "json").strip().lower()
    if rendered in ("", "json"):
        # The default answers in the response envelope, with the run under `data`. Every other
        # format is a **rendering the request asked for** — an aligned table, a csv, a workbook,
        # raw stdout — and wrapping those in JSON would defeat the point of asking for them. That
        # is the contract working, not an exception to it: the format is chosen *inside* the
        # request object, so a config file can carry it.
        sets = safe.get("result_sets") or []
        return response.emit(response.ok(
            "run-sql",
            message=(f"{safe.get('row_count', 0)} row(s)"
                     + (f", {safe['affected_rows']} affected" if safe.get("affected_rows") else "")
                     + f" from {safe.get('server_id')}"
                     + (f".{safe['database']}" if safe.get("database") else "")
                     + (" (truncated)" if safe.get("truncated") else "")),
            data=safe,
            metrics={"row_count": safe.get("row_count", 0),
                     "affected_rows": safe.get("affected_rows", 0),
                     "result_sets": len(sets)},
        ))

    try:
        text, _extra = result_format.render_result(
            safe, fmt=rendered, output_path=output_path,
        )
    except result_format.ResultFormatError as exc:
        return response.emit(response.fail("run-sql", str(exc)))
    print(text)
    return 0


RUN_CMD_USAGE = (
    "usage: python -m db_ops.common.cli run-cmd <json>|@<file>|- [--config ...] [--key-base64 ...]\n"
    "\n"
    "Run ONE shell command on a host, over the access it already has in db_instances.json.\n"
    "The command-line counterpart of run-sql: same JSON-object contract, same target resolution.\n"
    "\n"
    "The request is a JSON object, given inline, as @path/to/request.json, or on stdin (-):\n"
    '  {\"target\": \"CLOUD-203-0-113-188-ORA-1521\",  // server_id or ip from db_instances.json\n'
    '   \"access\": {...},          // OR an inline cmd_access block, for a host not in config\n'
    '   \"command\": \"df -h /\",     // OR \"script\": \"multi-line text run as a script\"\n'
    '   \"timeout_seconds\": 60,\n'
    '   \"confirm\": true,          // REQUIRED: this runs arbitrary code on a real host\n'
    '   \"assume_yes\": false,      // skip the terminal prompt (for non-interactive callers)\n'
    '   \"format\": \"json\"}         // json (default) | txt | raw\n'
    "\n"
    "\"confirm\" is required because nothing here can tell `df -h` from `rm -rf /`. host-facts and\n"
    "host-service already work this way; this command cannot classify itself, so it always asks.\n"
    "\n"
    "WHAT THE HOST IS. Optional, and empty means 'use db_instances.json'. These decide which\n"
    "shell dialect a script builder may use — platform alone cannot, because Get-CimInstance and\n"
    "ConvertTo-Json are PowerShell 3.0 (Windows Server 2012+) and older hosts need Get-WmiObject:\n"
    '   \"platform\": \"windows\",          // windows | linux\n'
    '   \"os\": \"Windows Server 2012 R2 ... 6.3\",  // parsed to an NT version when it carries one\n'
    '   \"os_major\": 6, \"os_minor\": 3,   // or state it outright\n'
    '   \"runtime\": \"docker\",            // host (default) | docker | k8s\n'
    '   \"profile\": {...}                // the same keys as one block\n'
    "\n"
    "The json answer carries \"host_profile\" (with a \"sources\" map: request or config per field)\n"
    "and \"shell_dialect\" ({tool: cim|wmi, chosen_by, reason}) alongside the command's output.\n"
    "\n"
    "format raw prints stdout verbatim and nothing else, so it pipes. xml/xlsx are not offered:\n"
    "a command's stdout is not a result set, and rendering it as one would be a claim about\n"
    "structure that is not there - use run-sql for that.\n"
    "\n"
    "Exit code is the command's own exit code (capped at 1 for a db_ops-level failure).\n"
)


def _run_cmd_command(argv: list[str]) -> int:
    """``run-cmd`` — run one shell command on a configured host.

    In ``common`` for the same reason ``run-sql`` is: reaching a host is not owned by metrics,
    backup or Telegram, and every one of them needs it. ``remote_exec`` has had the transport all
    along; what was missing was a front door, so the gap was filled by hand-typed ``ssh`` — which
    resolves the target differently every time and leaves no record of what was run.
    """
    from db_ops.lib.secret_text import set_key_env

    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(RUN_CMD_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
        elif token == "--key":
            key = rest.pop(0) if rest else None
        elif token in {"--key-base64", "--key_base64"}:
            key_base64 = rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{RUN_CMD_USAGE}", file=sys.stderr)
            return 2

    if not source:
        print(RUN_CMD_USAGE, file=sys.stderr)
        return 2
    try:
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, RUN_CMD_USAGE)
    if request is None:
        return code

    from db_ops.lib import response

    command = str(request.get("command") or "").strip()
    script = str(request.get("script") or "")
    if bool(command) == bool(script.strip()):
        return response.emit(response.fail(
            "run-cmd", 'give exactly one of "command" (one line) or "script" (text).'))

    from db_ops.common import confirm, host_ops
    from db_ops.lib import result_format

    try:
        fmt = result_format.normalize_format(request.get("format") or "json")
    except result_format.ResultFormatError as exc:
        return response.emit(response.fail("run-cmd", str(exc)))
    if fmt in {"xml", "xlsx"}:
        return response.emit(response.fail(
            "run-cmd", "run-cmd supports json, txt or raw; a command's stdout is not a result "
                       "set. Use run-sql for xml/xlsx."))

    from db_ops.config import load_config

    try:
        data_dir = getattr(load_config(config_path), "data_dir", None)
    except Exception:  # noqa: BLE001 - fall back to the package default data dir.
        data_dir = None

    from db_ops.common.evidence import GateReport

    # Gate lines go to stderr so the JSON (or raw stdout) stays machine-readable — the same
    # split every other gate command here uses.
    report = GateReport("run-cmd", echo=lambda line: print(line, file=sys.stderr))
    try:
        target = host_ops.resolve_host(request, data_dir=data_dir)
        # Same two locks as host-service / host-restart: "confirm": true is the payload declaring
        # intent, typing yes at a terminal is a human confirming they are looking at THIS host.
        allowed = confirm.require_confirmation(
            report,
            request,
            operation="run a shell command",
            target=f"{target.describe()} — {target.host}",
            effects=[(command or script.strip().splitlines()[0])[:200]],
        )
        if not allowed:
            return response.emit(response.fail(
                "run-cmd",
                'not confirmed; run-cmd needs "confirm": true and a typed yes '
                '(or "assume_yes": true when unattended).',
                data={"gates": report.to_dict().get("gates")}))
        session = host_ops.open_host_session(target, data_dir=data_dir)
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("run-cmd", str(exc)))

    try:
        timeout = request.get("timeout_seconds")
        # "sudo": true routes through the same helper host-service and host-restart use: the
        # password comes from the target's OWN configured credential and goes in on stdin, never
        # into the request and never onto the remote argv where `ps` would show it. Without this
        # the only way to run one privileged command was to write the password into the script.
        # With a container runtime the sudo belongs to the `docker exec` itself, and
        # `wrap_for_runtime` puts it there; run_privileged would sudo the wrong command.
        in_container = target.profile.runtime in {"docker", "k8s"}
        if bool(request.get("sudo", False)) and not in_container:
            outcome = host_ops.run_privileged(
                session, script if script.strip() else command, timeout_seconds=timeout)
        elif script.strip():
            # `runtime: docker|k8s` puts the script inside the container; a plain host is
            # unchanged. This is the half `hostcmd` had and `host_ops` did not, which is why
            # backup-database could reach a containerised engine and run-cmd could not.
            outcome = session.run_script(
                host_ops.wrap_for_runtime(target, script), timeout_seconds=timeout)
        else:
            outcome = session.run(
                host_ops.wrap_for_runtime(target, command), timeout_seconds=timeout)
    except Exception as exc:  # noqa: BLE001
        # No close() here: `finally` runs before the return completes, so adding one would close
        # the session twice.
        return response.emit(response.fail("run-cmd", str(exc)))
    finally:
        session.close()

    if fmt == "raw":
        # stdout verbatim and nothing else, so `| grep` works. stderr still goes to stderr, where
        # a pipeline can ignore it and a person can still see it.
        sys.stdout.write(outcome.stdout or "")
        if outcome.stderr:
            sys.stderr.write(outcome.stderr)
    elif fmt == "txt":
        print(f"{target.describe()}  exit={outcome.exit_code}")
        if outcome.stdout:
            print(outcome.stdout.rstrip("\n"))
        if outcome.stderr:
            print("--- stderr ---", file=sys.stderr)
            print(outcome.stderr.rstrip("\n"), file=sys.stderr)
    else:
        # `host` says which machine; `host_profile` says what that machine *is* and which shell
        # dialect that implies — the same "say what you chose" the run-sql answer grew, and the
        # only way an operator can tell a Server 2012 fact script from a Server 2003 refusal
        # without re-reading the inventory themselves.
        data = {
            "server_id": target.server_id,
            "host": target.host,
            "host_profile": target.to_dict().get("profile"),
            "shell": str(target.access.get("shell") or ""),
            "shell_dialect": target.to_dict().get("shell_dialect"),
            **outcome.to_dict(),
        }
        answer = (response.ok if outcome.ok else response.fail)
        detail = (outcome.stderr or outcome.stdout or "").strip()
        response.emit(
            answer("run-cmd",
                   message=f"exit={outcome.exit_code} on {target.describe()}",
                   data=data, metrics={"exit_code": outcome.exit_code,
                                       "duration_seconds": outcome.duration_seconds})
            if outcome.ok else
            answer("run-cmd",
                   f"exit={outcome.exit_code}" + (f": {detail[:400]}" if detail else ""),
                   message=f"exit={outcome.exit_code} on {target.describe()}",
                   data=data, metrics={"exit_code": outcome.exit_code,
                                       "duration_seconds": outcome.duration_seconds})
        )
    # **The one command whose exit code is not a summary of its response**, and deliberately so:
    # it passes through the REMOTE command's code. `run-cmd ... ; echo $?` is asking what the
    # command did, not whether db_ops managed to start it — the usage text has promised that since
    # the command existed, and `2` from a remote `grep` means "no match", not "db_ops failed".
    # Everything the envelope rule wants is still true: the answer is one object, `success`
    # mirrors the exit code, and the number is also in `data.exit_code` / `metrics.exit_code`.
    return int(outcome.exit_code or 0)


FILE_TRANSFER_USAGE = (
    "usage: python -m db_ops.common.cli fetch-file|send-file|pack-files|relay-file "
    "<json>|@<file>|- [--config ...] [--key-base64 ...]\n"
    "\n"
    "Move ONE named file between this host and a remote one, over the SSH access the target\n"
    "already has in db_instances.json. fetch-file pulls it here; send-file pushes it there.\n"
    "\n"
    "For a file you can name - a backup piece to inspect, a script to place, a log to collect.\n"
    "NOT for staging a backup set: per-file SFTP across two internet hops measured 10 KB/s here,\n"
    "which is why backup_restore streams a whole directory as one tar instead.\n"
    "\n"
    "More than one file: pack-files makes them ONE archive on the host that already holds them,\n"
    "then fetch-file moves it. The result carries the archive's sha256 and size, so what landed\n"
    "can be proven identical to what was packed - size alone catches a truncated copy, not a\n"
    "corrupted one.\n"
    '  {\"target\": \"...\", \"folder\": \"/opt/oracle/backup/dbops\", \"include\": \"*.bkp\",\n'
    '   \"archive_path\": \"/tmp/pieces.tar\", \"format\": \"tar\"}   // or \"files\": [...]\n'
    "\n"
    "The request is a JSON object, given inline, as @path/to/request.json, or on stdin (-):\n"
    '  {"target": "CLOUD-203-0-113-188-ORA-1521",  // server_id or ip from db_instances.json\n'
    '   "access": {...},            // OR an inline cmd_access block, for a host not in config\n'
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
    "verify each hop separately, which names the wrong hop when the hashes differ:\n"
    '  {"source": {"target": "ACME-192-0-2-249-HOST", "path": "/tmp/bundle.tar.gz"},\n'
    '   "destination": {"target": "ACME-192-0-2-11-MSSQL25-1433", "path": "/tmp/b.tar.gz"},\n'
    '   "overwrite": false, "make_dirs": true}\n'
)


def _file_transfer_command(argv: list[str], direction: str) -> int:
    """``fetch-file`` / ``send-file`` / ``pack-files`` / ``relay-file`` — the CLI face of
    :mod:`db_ops.common.file_transfer`.

    In ``common`` rather than an app because no app owns it: metrics, backup and the restore
    drills all reach hosts, and "put this file there" is not any one of their jobs. It exists at
    all because the alternative kept being a hand-typed ``scp``, which answers once and takes its
    target resolution and its edge cases with it.
    """
    from db_ops.lib.secret_text import set_key_env

    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(FILE_TRANSFER_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
        elif token == "--key":
            key = rest.pop(0) if rest else None
        elif token in {"--key-base64", "--key_base64"}:
            key_base64 = rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{FILE_TRANSFER_USAGE}", file=sys.stderr)
            return 2

    if not source:
        print(FILE_TRANSFER_USAGE, file=sys.stderr)
        return 2
    try:
        # Reaching the host needs its OS credential out of the encrypted store, exactly as
        # host-facts does.
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, FILE_TRANSFER_USAGE)
    if request is None:
        return code

    from db_ops.config import load_config

    try:
        data_dir = getattr(load_config(config_path), "data_dir", None)
    except Exception:  # noqa: BLE001 - fall back to the package default data dir.
        data_dir = None

    from db_ops.common import file_transfer

    runner = {
        "fetch": file_transfer.fetch_file,
        "send": file_transfer.send_file,
        "pack": file_transfer.pack_files,
        "relay": file_transfer.relay_file,
    }[direction]
    from db_ops.lib import response

    command = {"fetch": "fetch-file", "send": "send-file", "pack": "pack-files",
               "relay": "relay-file"}[direction]
    try:
        result = runner(request, data_dir=data_dir)
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail(command, str(exc)))
    # `status` is the fact worth reading first — COPIED / REPLACED / SKIPPED_EXISTS are three
    # different outcomes of a successful transfer, and a caller that only saw "ok" could not
    # tell "I moved it" from "it was already there".
    status = str(result.get("status") or "")
    size = result.get("bytes")
    return response.emit(response.ok(
        command,
        message=f"{command} {status or 'done'}"
                + (f" ({size} bytes)" if isinstance(size, int) else "") + ".",
        data=result,
        metrics={"bytes": size} if isinstance(size, int) else {},
    ))


def _optional_json_request(argv: list[str], usage: str) -> tuple[dict | None, int]:
    """The JSON-object contract for commands whose payload used to be a flag or a bare word.

    Returns ``({}, 0)`` for no argument at all, so the commands that legitimately take no
    input (``list-targets`` with no argument) still work bare.

    ``add-sql``, ``metric-toggle`` and ``list-targets`` predate the "one JSON object in" rule and
    were among the six exceptions the 2026-08-06 audit found (``check-credentials`` was a fourth
    until it moved to ``db_ops/cli.py``). They now take the object like every other
    command. Their old argument forms still parse — an operator's muscle memory and the
    examples already pasted into runbooks keep working — but the object is the contract, and
    it is the only form a config file or a Telegram action can carry unchanged.

    A leading ``{``, ``@`` or ``-`` is what marks the JSON form. None of the legacy forms can
    start with those characters (levels are words, flags start with ``--``), so the two are
    distinguishable without a mode flag.

    Four outcomes, all as ``(request, exit_code)``:

    * ``({}, 0)``     — no argument; the command runs with its defaults.
    * ``(dict, 0)``   — a JSON object was given.
    * ``(None, 0)``   — the legacy argument form; the caller parses ``argv`` itself.
    * ``(None, >0)``  — malformed JSON or a non-object root; already reported, just return it.
    """
    if not argv:
        return {}, 0
    if looks_like_json_request(argv[0]):
        return _read_json_request(argv[0], usage)
    return None, 0


def _restore_database_command(argv: list[str]) -> int:
    """``restore-database`` - delegated to :mod:`db_ops.common.cli_restore`.

    A one-line dispatch on purpose: this file routes commands, it does not house them.
    """
    from db_ops.common import cli_restore

    return cli_restore.run(argv, read_request=_read_json_request)


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
    "Fields:\n"
    "  target           (required) server_id, or '<db_type> <ip> [port]'\n"
    "  database         database whose transaction log usage is reported (default: login's)\n"
    "  session_id       trace exactly one SPID instead of scanning\n"
    "  min_tran_seconds ignore transactions younger than this (default 60)\n"
    "  blocking_only    only sessions that are blocking someone\n"
    "  credential_name / data_dir / timeout_seconds — as run-sql\n"
    "\n"
    "Read-only: it runs through run-sql, which always rolls back.\n"
)


def _trace_session_command(argv: list[str]) -> int:
    """``trace-session`` — the CLI face of :mod:`db_ops.common.session_trace`."""
    if not argv or argv[0] in {"-h", "--help"}:
        print(TRACE_SESSION_USAGE, file=sys.stderr)
        return 2
    request, code = _read_json_request(argv[0], TRACE_SESSION_USAGE)
    if request is None:
        return code
    from db_ops.common import session_trace
    from db_ops.lib.secret_text import set_key_env
    from db_ops.common.sql_run import SqlRunError

    from db_ops.lib import response

    set_key_env(request.get("key"), request.get("key_base64"))
    try:
        result = session_trace.trace_sessions(request)
    except (session_trace.SessionTraceError, SqlRunError) as exc:
        return response.emit(response.fail("trace-session", str(exc)))
    # The readable per-session lines stay on stderr, where a person watching sees them while the
    # answer on stdout stays one object.
    for session in result["sessions"]:
        print(session_trace.describe(session), file=sys.stderr)
    count = len(result["sessions"])
    return response.emit(response.ok(
        "trace-session",
        message=(f"{count} open transaction(s) on {result.get('server_id') or 'the target'}"
                 if count else
                 f"no open transaction older than the threshold on "
                 f"{result.get('server_id') or 'the target'}"),
        data=result,
        metrics={"session_count": count},
    ))


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


def _metric_severity_command(argv: list[str]) -> int:
    """``metric-severity`` — the CLI face of :func:`config_admin.set_metric_severity_map`.

    JSON only, with no legacy flag form: ``severity_map`` is a mapping, and the flag spellings
    that would express one (``--map WARNING=LOGGING``, repeated) are a second syntax to learn for
    the one command that does not need it.
    """
    from db_ops.lib import response

    if not argv or argv[0] in {"-h", "--help"}:
        print(METRIC_SEVERITY_USAGE, file=sys.stderr)
        return 2
    request, code = _read_json_request(argv[0], METRIC_SEVERITY_USAGE)
    if request is None:
        return code
    try:
        result = config_admin.set_metric_severity_map(
            server_id=str(request.get("server_id") or ""),
            metric_code=str(request.get("metric_code") or ""),
            severity_map=request.get("severity_map"),
            metric_item=str(request.get("metric_item") or ""),
            note=str(request.get("note") or ""),
            data_dir=request.get("data_dir"),
        )
    except config_admin.ConfigAdminError as exc:
        return response.emit(response.fail("metric-severity", str(exc)))
    changes = result.get("changes") or []
    return response.emit(response.ok(
        "metric-severity",
        message=(f"{result.get('server_id')} {result.get('metric_code')}: "
                 + ("; ".join(str(line) for line in changes) if changes else "no change")),
        data=result,
        metrics={"change_count": len(changes)},
    ))


def _read_json_request(source: str, usage: str) -> tuple[dict | None, int]:
    """Read a JSON object request from an inline string, ``@file`` or stdin (``-``).

    Shared by the JSON-request commands so ``run-sql``, ``queue-telegram-message`` and
    ``rotate-password`` all accept the same three forms and report a bad payload identically.
    """
    if source == "-":
        payload_text = sys.stdin.read()
    elif source.startswith("@"):
        from pathlib import Path

        path = Path(source[1:])
        if not path.exists():
            print(f"Request file not found: {path}", file=sys.stderr)
            return None, 2
        payload_text = path.read_text(encoding="utf-8-sig")
    else:
        payload_text = source
    try:
        request = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "error": f"request is not valid JSON: {exc}"},
                         ensure_ascii=False))
        return None, 1
    if not isinstance(request, dict):
        print(json.dumps({"ok": False, "error": "request must be a JSON object."},
                         ensure_ascii=False))
        return None, 1
    return request, 0


def _rotate_password_command(argv: list[str]) -> int:
    """``rotate-password`` — the CLI face of :mod:`db_ops.common.password_rotation`.

    Kept in the shared CLI rather than an app's: a password change is not owned by metrics, backup
    or Telegram, and every one of them breaks the same way when the store and the server disagree.

    The store write happens here rather than inside the rotation module so the plaintext source and
    the encrypted blob are updated together — the deploy regenerates the blob from the plaintext, so
    writing only one of them is silently undone on the next deploy.
    """
    from db_ops.common import password_rotation
    from db_ops.lib import response
    from db_ops.lib.secret_text import set_key_env

    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(ROTATE_PASSWORD_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
        elif token == "--key":
            key = rest.pop(0) if rest else None
        elif token in {"--key-base64", "--key_base64"}:
            key_base64 = rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{ROTATE_PASSWORD_USAGE}", file=sys.stderr)
            return 2

    if not source:
        print(ROTATE_PASSWORD_USAGE, file=sys.stderr)
        return 2
    try:
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, ROTATE_PASSWORD_USAGE)
    if request is None:
        return code

    from db_ops.config import load_config

    try:
        config = load_config(config_path)
        data_dir = getattr(config, "data_dir", None)
    except Exception:  # noqa: BLE001 - the data dir falls back to the package default.
        data_dir = None

    try:
        outcome = password_rotation.rotate(request, data_dir=data_dir)
        if not request.get("dry_run"):
            plaintext = request.get("plaintext_store", "secrets/secret_text.json")
            outcome["stored"] = password_rotation.persist_rotated(
                outcome, data_dir=data_dir, plaintext_store=plaintext or None
            )
    except password_rotation.PasswordRotationError as exc:
        return response.emit(response.fail("rotate-password", str(exc)))
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("rotate-password", str(exc)))

    # `strip_secrets` first, always: the outcome carries the passwords it just set, and the
    # envelope is printed, logged and forwarded. Redact before building the response, not after.
    safe = password_rotation.strip_secrets(outcome)
    results = safe.get("results") or []
    rotated = [item for item in results if item.get("ok")]
    failed = [item for item in results if not item.get("ok")]
    message = f"rotated {len(rotated)} of {len(results)} login(s)"
    if outcome.get("ok"):
        return response.emit(response.ok(
            "rotate-password", message=message + ".", data=safe,
            metrics={"rotated": len(rotated), "failed": len(failed)}))
    reason = str(safe.get("error") or "") or (
        "; ".join(f"{item.get('ref') or item.get('credential_name')}: {item.get('error')}"
                  for item in failed) or "rotation did not succeed")
    return response.emit(response.fail(
        "rotate-password", reason, message=message + ".", data=safe,
        metrics={"rotated": len(rotated), "failed": len(failed)}))


def _probe_host_command(argv: list[str]) -> int:
    """``probe-host`` — the CLI face of :mod:`db_ops.common.host_probe`.

    **The target lookup lives here, not in the module below.** ``host_probe`` decides what open
    ports mean and reads nothing; resolving ``ACME-192-0-2-236`` to an ip and an OS caption is a
    question about this machine's ``data/`` folder, and the composition root is the layer allowed
    to ask it (``docs/13_common.md`` rule 3, ``tests/test_common_layers.py``). A request that
    states ``host`` skips this entirely and the whole command touches no file.
    """
    from db_ops.common import host_probe
    from db_ops.lib import response

    source = ""
    config_path = "config.json"
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(PROBE_HOST_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{PROBE_HOST_USAGE}", file=sys.stderr)
            return 2

    request, code = _read_json_request(source or "{}", PROBE_HOST_USAGE)
    if request is None:
        return code

    instance = None
    target = str(request.get("target") or "").strip()
    if target and not str(request.get("host") or "").strip():
        from db_ops.common import data_sources
        from db_ops.config import load_config

        try:
            data_dir = getattr(load_config(config_path), "data_dir", None)
        except Exception:  # noqa: BLE001 - fall back to the package default data dir.
            data_dir = None
        try:
            instance = data_sources.resolve_target_instance(target, data_dir=data_dir)
        except Exception as exc:  # noqa: BLE001 - an unknown target is an operator message.
            return response.emit(response.fail("probe-host", str(exc)))

    try:
        outcome = host_probe.probe(request, instance=instance)
    except host_probe.HostProbeError as exc:
        return response.emit(response.fail("probe-host", str(exc)))
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("probe-host", str(exc)))

    ports = outcome["ports"]
    return response.emit(response.ok(
        "probe-host",
        message=f"{outcome['server_id']} ({outcome['host']}): {outcome['verdict']} - {outcome['detail']}",
        data=outcome,
        metrics={"probed": len(ports), "open": len(outcome["open_ports"]),
                 "management_ports": len(outcome["management_ports"])},
    ))


def _check_identifiers_command(argv: list[str]) -> int:
    """``check-identifiers`` — the CLI face of :mod:`db_ops.common.identifier_scan`.

    Read-only, and the one command here that opens nothing: no host, no database, no secret. It
    reads configuration to learn what to look for and then reads files. That matters because this
    is the check a release gate runs, and a gate that can *do* something is a gate nobody will let
    run unattended.
    """
    from db_ops.common import identifier_scan

    source = ""
    config_path = "config.json"
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(CHECK_IDENTIFIERS_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{CHECK_IDENTIFIERS_USAGE}", file=sys.stderr)
            return 2

    request, code = _read_json_request(source or "{}", CHECK_IDENTIFIERS_USAGE)
    if request is None:
        return code

    from db_ops.config import load_config
    from db_ops.lib import response

    try:
        data_dir = getattr(load_config(config_path), "data_dir", None)
    except Exception:  # noqa: BLE001 - fall back to the package default data dir.
        data_dir = None

    try:
        outcome = identifier_scan.scan(request, data_dir=data_dir)
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("check-identifiers", str(exc)))

    # `success` is *the scan ran*. A finding is a fact about the tree, and reporting it as a failed
    # command would make "db_ops could not look" and "there are 177 identifiers" the same answer -
    # the same distinction `check-secret` makes. Callers gate on `data.hits`.
    print(identifier_scan.format_report(outcome), file=sys.stderr)
    hits = int(outcome["hits"])
    message = (
        f"{hits} hit(s) in {outcome['files_with_findings']} file(s) "
        f"from {outcome['identifiers_searched']} configured identifier(s)"
    )
    return response.emit(response.ok(
        "check-identifiers", message=message + ".", data=outcome,
        metrics={
            "hits": hits,
            "files_with_findings": int(outcome["files_with_findings"]),
            "files_scanned": int(outcome["files_scanned"]),
            "identifiers_searched": int(outcome["identifiers_searched"]),
        },
    ))


def _check_secret_command(argv: list[str]) -> int:
    """``check-secret`` — the CLI face of :mod:`db_ops.common.secret_check`.

    Read-only sibling of ``rotate-password``: same JSON-object input, same target resolution, but it
    only proves a login rather than changing one. Kept next to it so an audit and a rotation cannot
    disagree about where a secret lives.
    """
    from db_ops.common import secret_check
    from db_ops.lib.secret_text import set_key_env

    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(CHECK_SECRET_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
        elif token == "--key":
            key = rest.pop(0) if rest else None
        elif token in {"--key-base64", "--key_base64"}:
            key_base64 = rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{CHECK_SECRET_USAGE}", file=sys.stderr)
            return 2

    try:
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source or "{}", CHECK_SECRET_USAGE)
    if request is None:
        return code

    from db_ops.config import load_config

    try:
        data_dir = getattr(load_config(config_path), "data_dir", None)
    except Exception:  # noqa: BLE001 - fall back to the package default data dir.
        data_dir = None

    from db_ops.lib import response

    try:
        outcome = secret_check.check(request, data_dir=data_dir)
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("check-secret", str(exc)))

    # `success` is *the audit ran*, the same distinction `check-credentials` makes: a secret that
    # cannot log in is a fact about the estate, and reporting it as a failed command would make
    # "db_ops could not check" and "the password is wrong" the same answer. The per-secret verdicts
    # are `data.results`, and `data.ok` keeps the module's own meaning — every ref resolved to a
    # target — for callers that were reading it.
    summary = outcome.get("summary") or {}
    unresolved = [item for item in (outcome.get("results") or [])
                  if item.get("status") == "NO_TARGET"]
    counts = ", ".join(f"{status} {count}" for status, count in sorted(summary.items()))
    message = f"checked {outcome.get('selected', 0)} secret(s)" + (f": {counts}" if counts else "")
    if unresolved:
        message += f"; {len(unresolved)} could not be resolved to a target"
    return response.emit(response.ok(
        "check-secret", message=message + ".", data=outcome,
        metrics={"selected": int(outcome.get("selected") or 0), **{
            str(status).lower(): int(count) for status, count in summary.items()}},
    ))


def _gate_command(argv: list[str], usage: str, runner_name: str) -> int:
    """The twelve host/patch/instance commands: one JSON-object contract, one answer shape.

    They share one handler because they share one shape — a JSON request in, a
    :class:`db_ops.common.evidence.GateReport` dict out, exit 0 unless a blocking gate failed.
    Progress goes to **stderr** and only the JSON result to stdout, so an operator can watch a
    30-minute restart happen while a caller still pipes the result into `jq`.

    Sharing the handler is also why they converted to the response envelope in **one edit** on
    2026-08-16 — twelve of the twenty-one commands that were still answering in an ad-hoc
    ``{"ok": …}``. The gate report itself is unchanged and now sits under ``data``.
    """
    from db_ops.lib.secret_text import set_key_env

    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(usage)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
        elif token == "--key":
            key = rest.pop(0) if rest else None
        elif token in {"--key-base64", "--key_base64"}:
            key_base64 = rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{usage}", file=sys.stderr)
            return 2

    if not source:
        print(usage, file=sys.stderr)
        return 2
    try:
        # Reaching a host needs the OS credential out of the encrypted store, exactly like
        # run-sql needs the database one.
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, usage)
    if request is None:
        return code

    from db_ops.config import load_config

    try:
        data_dir = getattr(load_config(config_path), "data_dir", None)
    except Exception:  # noqa: BLE001 - fall back to the package default data dir.
        data_dir = None

    from db_ops.common import (host_ops, job_control, sqlserver_emergency,
                               sqlserver_instance, sqlserver_patch)

    runners = {
        "host-facts": host_ops.host_facts,
        "host-service": host_ops.service_control,
        "host-restart": host_ops.restart_host,
        "shrink-log": sqlserver_emergency.shrink_log,
        "kill-spid": sqlserver_emergency.kill_spid,
        "start-job": sqlserver_emergency.start_job,
        "disable-job": job_control.disable_job,
        "sqlserver-precheck": sqlserver_patch.precheck,
        "sqlserver-apply-cu": sqlserver_patch.apply_cu,
        "sqlserver-verify-build": sqlserver_patch.verify_build,
        "sqlserver-export-instance": sqlserver_instance.export_instance,
        "sqlserver-replay-instance": sqlserver_instance.replay_instance,
        "sqlserver-verify-instance": sqlserver_instance.verify_instance,
    }
    from db_ops.lib import response

    try:
        outcome = runners[runner_name](
            request, data_dir=data_dir, echo=lambda line: print(line, file=sys.stderr, flush=True)
        )
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail(runner_name, str(exc)))

    # The whole gate report goes under `data`, unchanged: `blockers`, `gates`, `facts`,
    # `evidence_file` and the rest are what an incident review reads, and moving to the envelope
    # must not cost any of them. `GateReport.to_dict()` has an `operation` key of its own — it
    # names the *operation*, this one names the *command* — and nesting keeps both.
    blockers = [str(name) for name in (outcome.get("blockers") or [])]
    target = str(outcome.get("target") or "")
    status = str(outcome.get("status") or "")
    where = f" on {target}" if target else ""
    metrics = outcome.get("counts") if isinstance(outcome.get("counts"), dict) else {}

    if outcome.get("ok"):
        return response.emit(response.ok(
            runner_name, message=f"{runner_name} {status or 'ok'}{where}.",
            data=outcome, metrics=metrics))
    # A blocking gate is *the* reason, and naming it beats "it failed": these commands restart
    # hosts and patch instances, and the next action differs per gate.
    reason = str(outcome.get("error") or "")
    if not reason:
        reason = (f"blocked by {', '.join(blockers)}" if blockers
                  else f"{runner_name} did not succeed{where}.")
    return response.emit(response.fail(
        runner_name, reason, message=f"{runner_name} {status or 'failed'}{where}.",
        data=outcome, metrics=metrics))


def _inventory_summary_command(argv: list[str]) -> int:
    """``inventory-summary`` — the CLI face of :mod:`db_ops.lib.inventory_render`.

    Both apps that render this summary now call the same code; this gives it a face of its own so
    the next caller has no reason to copy it a third time.
    """
    from db_ops.lib import inventory_render, response

    source = ""
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(INVENTORY_SUMMARY_USAGE)
            return 0
        if token == "--config":
            rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{INVENTORY_SUMMARY_USAGE}", file=sys.stderr)
            return 2

    request, code = _read_json_request(source or "{}", INVENTORY_SUMMARY_USAGE)
    if request is None:
        return code

    inventory = str(request.get("inventory") or inventory_render.DEFAULT_INVENTORY)
    try:
        overlay = request.get("overlay")
        if overlay:
            merged = inventory_render._merge_overlay(  # noqa: SLF001 - same package
                json.loads(Path(inventory).read_bytes().decode("utf-8-sig")),
                json.loads(Path(str(overlay)).read_bytes().decode("utf-8-sig")),
            )
            inventory_render._write_inventory(Path(inventory), merged)  # noqa: SLF001
        result = inventory_render.build_inventory_summary(
            inventory=inventory,
            output_dir=str(request.get("output_dir") or "."),
            date=request.get("date"),
        )
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("inventory-summary", str(exc)))

    data = {"inventory": inventory}
    data.update(result if isinstance(result, dict) else {"result": result})
    return response.emit(response.ok(
        "inventory-summary",
        message=f"Wrote {data.get('file') or 'the summary'}.",
        data=data,
    ))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    if argv[0] == "list-targets":
        from db_ops.common import data_sources as target_resolve
        from db_ops.lib import response

        request, code = _optional_json_request(argv[1:], LIST_TARGETS_USAGE)
        if request is None and code:
            return code
        # The human listing was the *only* thing this printed until 2026-08-16, which made it one
        # of two commands a program could not consume at all. It is still one line away — behind
        # `format: txt` — because pasted runbook lines and the Telegram reply both want it.
        if str((request or {}).get("format") or "json").strip().lower() == "txt":
            print(target_resolve.format_target_list())
            return 0
        targets = target_resolve.list_target_instances()
        enabled = [item for item in targets if item.get("enabled", True)]
        return response.emit(response.ok(
            "list-targets",
            message=(f"{len(enabled)} target(s) you can address"
                     + (f"; {len(targets) - len(enabled)} disabled and not listed as runnable."
                        if len(targets) != len(enabled) else ".")),
            data={"targets": targets},
            metrics={"target_count": len(targets), "enabled_count": len(enabled)},
        ))
    if argv[0] == "run-sql":
        return _run_sql_command(argv[1:])
    if argv[0] == "run-cmd":
        return _run_cmd_command(argv[1:])
    if argv[0] == "trace-session":
        return _trace_session_command(argv[1:])
    if argv[0] == "metric-severity":
        return _metric_severity_command(argv[1:])
    if argv[0] == "rotate-password":
        return _rotate_password_command(argv[1:])
    if argv[0] == "check-secret":
        return _check_secret_command(argv[1:])
    if argv[0] == "check-identifiers":
        return _check_identifiers_command(argv[1:])
    if argv[0] == "lift-example":
        return _lift_example_command(argv[1:])
    if argv[0] == "probe-host":
        return _probe_host_command(argv[1:])
    if argv[0] == "restore-database":
        return _restore_database_command(argv[1:])
    if argv[0] in {"restore-full", "restore-diff", "restore-log",
                   "restore-key", "restore-metadata", "verify-restore"}:
        from db_ops.common import cli_restorestep

        return cli_restorestep.run(argv[0], argv[1:], read_request=_read_json_request)
    if argv[0] in {"pack-backup", "pull-file", "push-file"}:
        from db_ops.common import cli_filetransfer

        return cli_filetransfer.run(argv[0], argv[1:], read_request=_read_json_request)
    if argv[0] in {"list-backup-files", "prune-backup-files"}:
        from db_ops.common import cli_backup_files

        return cli_backup_files.run(argv[0], argv[1:], read_request=_read_json_request)
    if argv[0] in {"list-databases", "list-schemas", "list-jobs",
                   "create-table-from-xlsx"}:
        from db_ops.common import cli_catalog

        return cli_catalog.run(argv[0], argv[1:], read_request=_read_json_request)
    if argv[0] == "copy-schema":
        from db_ops.common import cli_schema

        return cli_schema.run(argv[0], argv[1:], read_request=_read_json_request)
    if argv[0] in {"delete-file", "delete-files"}:
        from db_ops.common import cli_delete_files

        return cli_delete_files.run(argv[0], argv[1:], read_request=_read_json_request)
    if argv[0] == "backup-database":
        from db_ops.common import cli_backup

        return cli_backup.run(argv[1:], read_request=_read_json_request)
    if argv[0] == "inventory-summary":
        return _inventory_summary_command(argv[1:])
    if argv[0] == "fetch-file":
        return _file_transfer_command(argv[1:], "fetch")
    if argv[0] == "send-file":
        return _file_transfer_command(argv[1:], "send")
    if argv[0] == "pack-files":
        return _file_transfer_command(argv[1:], "pack")
    if argv[0] == "relay-file":
        return _file_transfer_command(argv[1:], "relay")
    if argv[0] == "host-facts":
        return _gate_command(argv[1:], HOST_FACTS_USAGE, "host-facts")
    if argv[0] == "host-service":
        return _gate_command(argv[1:], HOST_SERVICE_USAGE, "host-service")
    if argv[0] == "host-restart":
        return _gate_command(argv[1:], HOST_RESTART_USAGE, "host-restart")
    if argv[0] in {"shrink-log", "kill-spid", "start-job", "disable-job"}:
        return _gate_command(argv[1:], EMERGENCY_USAGE, argv[0])
    if argv[0] in {"sqlserver-precheck", "sqlserver-apply-cu", "sqlserver-verify-build"}:
        return _gate_command(argv[1:], SQLSERVER_PATCH_USAGE, argv[0])
    if argv[0] in {"sqlserver-export-instance", "sqlserver-replay-instance",
                   "sqlserver-verify-instance"}:
        return _gate_command(argv[1:], SQLSERVER_INSTANCE_USAGE, argv[0])
    return config_admin.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
