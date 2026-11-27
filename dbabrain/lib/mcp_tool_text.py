"""What each MCP tool says it does - its own sentence, a verb first, and which sibling to take instead (1.3.0, 1.439).

Glama's TDQS review of 1.2.0 (2026-11-27, C 2.5/5): *many tool families overlap heavily - fetch/send/pull/push/relay/
copy-files, run-sql / run-sql-write / run-sqlcmd, delete-file / delete-verified-files / delete-empty-folders ... an
agent can easily pick the wrong variant.* Until 1.3.0 a tool's description was its request entry's ``one_line`` - a
sentence written to describe a REQUEST (``db-status``: *How deep to check an instance*; ``inventory-summary``: *The
inventory to summarise*), opened by ``dbabrain common.cli `x`:``, which pushed what the tool does out of the first
words an agent reads. The operator, 2026-11-27 (translated): *ok, do it - and group a few tools that are too alike*.

So each tool's text is written here, for an agent choosing a tool: what it does, and - in a family - when to take the
other one. The reference's ``one_line`` stays what it is for: describing a request (``describe-object``,
``--help``). A command with no line here falls back to it, and a test holds every offered tool to having one.
"""

from __future__ import annotations

TOOL_TEXT: dict[str, str] = {
    # --- inventory and the node itself -------------------------------------------------------------------------
    "list-targets": "List every server this node can address - its server_id, engine, host and whether it is "
                    "active. Start here: every other tool names a server by this server_id.",
    "self-status": "Report what this dbabrain installation is - version, host, role, CPU, memory, disk and the "
                   "app commands it schedules - read from the machine itself, without the store.",
    "inventory-summary": "Render the inventory's dated *-summary.md (every instance, its engine and state), "
                         "optionally merging a health overlay into the inventory JSON first.",
    "instance-add": "Register a new database instance and its login in this node's inventory (db_instances.json, "
                    "users.json).",
    "instance-active": "Switch one registered instance on or off for every app at once (its active flag); "
                       "nothing is removed.",
    "instance-remove": "Remove one server's record from db_instances.json - a plan unless apply. To remove only "
                       "its login use credential-remove; any other list record, entry-remove.",
    "credential-remove": "Remove one login from users.json by its credential_name - a plan unless apply. The "
                         "instance itself goes by instance-remove.",
    "entry-remove": "Remove one record from any data/*.json list, found by matching fields - a plan unless apply. "
                    "For an instance or a login, instance-remove and credential-remove also clear what points at it.",
    "report-server-remove": "Take one server off the report's canonical inventory (not off db_instances.json) - a "
                            "plan unless apply.",
    "remote-credential-add": "Register an OS login (SSH or WinRM) for a host, and the cmd_access that uses it, so "
                             "run-cmd and the host-* tools can reach that host.",
    "key-status": "Read this node's dbabrain key offline (holder, expiry) and ask dbabrain.dev whether it is "
                  "still good.",
    "key-request": "Get or renew this node's dbabrain key: an email sign-in on dbabrain.dev approved in a browser; "
                   "the new key is kept in the encrypted store, replacing any old one.",

    # --- configuration checks and upkeep -----------------------------------------------------------------------
    "check-secret": "Prove stored secrets by using them: log in to the host or database each one belongs to and "
                    "report which authenticate.",
    "check-identifiers": "Scan a folder tree for this estate's real identifiers (addresses, server_ids, "
                         "credentials, people) before files are shared or published.",
    "check-secret-literals": "Scan a folder tree for secret values from this node's store written in plain text.",
    "describe-object": "Explain what a config object's fields mean - a shared block (time_window, notify, "
                       "cmd_access ...) or a whole record (backup_entry, sql_command, db_instance ...).",
    "check-config": "Check this node's config files, from config alone (connecting nowhere) - `check` objects: "
                    "required fields and values against the reference this version ships; references: pointers "
                    "from one file into another that land nowhere; credentials: every target resolves a login. To "
                    "prove a password really works, use check-secret.",
    "upgrade-config": "Move this node's config files to this version's shapes after an upgrade - a plan unless "
                      "dry_run is false.",
    "standardize-field-names": "Rewrite this node's data/*.json to the standard field names (enabled -> active, "
                               "db_name -> database_name ...) - a plan unless dry_run is false.",
    "lift-example": "Copy one config file into its shipped *.example.json, with this estate's values replaced.",
    "init": "Create a new dbabrain tool root in a directory: config, a SQLite store, an empty inventory and the "
            "files to edit next; nothing is overwritten without force.",
    "guide": "Print this build's operating guide for agents and operators, or write it as AGENTS.md.",
    "export-data": "Write this node's whole configuration (optionally its secrets and assets) to one bundle file, "
                   "for import-data on another machine.",
    "import-data": "Apply a bundle written by export-data to a tool root, so this machine runs the same estate - "
                   "a plan when plan_only.",
    "build-showcase": "Copy the published HTML reports into a folder with every estate identifier replaced by a "
                      "stable fake one, for showing to outsiders.",

    # --- secrets ----------------------------------------------------------------------------------------------
    "encrypt-secret": "Encrypt the plaintext secret file into the encrypted store this node reads; run it after "
                      "every edit of the plaintext file.",
    "rotate-password": "Change database logins' passwords on the server and in the secret store as one step: "
                       "change, re-authenticate on a new connection, then store.",
    "seal-ssh-key": "Seal an SSH private key (or generate and seal one) at its path, its passphrase kept as a "
                    "secret ref.",
    "smb-credential": "Store the login Windows uses for a host's share paths (cmdkey), for the smb-* tools.",

    # --- SQL --------------------------------------------------------------------------------------------------
    "run-sql": "Run one SELECT on one server and return its rows - checked to be a single SELECT, always rolled "
               "back. To change data use run-sql-write; for a sqlcmd batch (GO, RESTORE ...) on SQL Server, "
               "run-sqlcmd.",
    "run-sqlcmd": "Run one sqlcmd batch where the SQL Server runs (on its host or in its container) - for T-SQL "
                  "that needs sqlcmd, such as GO-separated scripts. A single statement goes by run-sql or "
                  "run-sql-write.",
    "list-objects": "List what an instance holds, with the state the engine reports - `kind` databases (online, "
                    "restoring, read-only ...), schemas of one database, or jobs of the instance's own scheduler "
                    "(SQL Server Agent, Oracle's scheduler, pg_cron) and whether each is enabled.",
    "table-info": "Describe one table: its columns (type, length, nullable, identity, computed, default), its "
                  "indexes (keys, included columns, filter, unique, disabled), row count and reserved MB.",
    "object-source": "Return the definition of a procedure, function, view or trigger by name (on Oracle also a "
                     "package or type).",
    "query-plan": "Show one statement's estimated plan without running it: SQL Server SHOWPLAN_XML with cost, top "
                  "operators and missing indexes; PostgreSQL and MySQL EXPLAIN without ANALYZE.",
    "db-activity": "Show what a server runs now: running requests, who blocks whom, current waits, and the top "
                   "queries since their plans were cached.",
    "trace-session": "List every open transaction on one SQL Server with the application user behind it (decoded "
                     "from context_info), filtered by age, session or blocking.",
    "db-status": "Check one instance's state at the depth asked - reachable, each database queryable - with one "
                 "answer shape whatever the engine; a real statement is run, not just a state column read.",
    "copy-schema": "Reproduce one SQL Server schema from one instance on another - tables, indexes, constraints, "
                   "modules, data of catalogue tables - printing the plan unless plan is false.",
    "create-table-from-xlsx": "Load a spreadsheet or CSV into a new table on one server.",

    # --- SQL Server Agent jobs and emergency actions ----------------------------------------------------------
    "start-job": "Start one SQL Server Agent job now - an emergency action, confirmed and with a reason.",
    "disable-job": "Disable one SQL Server Agent job so its schedule stops firing - an emergency action, "
                   "confirmed and with a reason.",
    "kill-spid": "Kill one SQL Server session by its spid - an emergency action, confirmed and with a reason.",
    "shrink-log": "Shrink one SQL Server database's transaction log file - an emergency action, confirmed and with "
                  "a reason.",
    "sqlserver-cu-check": "Check one SQL Server instance around a cumulative update, read-only - `check` precheck: "
                          "whether it is safe to patch now (host, installer, SQL gates, maintenance window); build: "
                          "whether it reached the build the update should give.",

    # --- SQL Server patching and instance metadata ------------------------------------------------------------
    "sqlserver-apply-cu": "Apply a staged cumulative update to one SQL Server instance; run sqlserver-cu-check "
                          "with check precheck first and check build after.",
    "sqlserver-export-instance": "Export an instance's server-level metadata (logins, roles, permissions, linked "
                                 "servers, Agent jobs) as SQL files and a manifest - read-only.",
    "sqlserver-replay-instance": "Apply an exported metadata bundle (from sqlserver-export-instance) to a target "
                                 "instance, in dependency order.",
    "sqlserver-verify-instance": "Compare a target instance with an exported metadata bundle and list what "
                                 "differs - read-only.",
    "restore-metadata": "Apply named .sql metadata files (logins, roles, permissions, jobs) to a restored SQL "
                        "Server instance, in the order given. For a whole exported bundle use "
                        "sqlserver-replay-instance.",

    # --- SQL tasks and scheduling ---------------------------------------------------------------------------
    "sql-command-add": "Register a SQL task's script - what it runs - by name. Then give it a server and a "
                       "schedule with sql-target-add.",
    "sql-target-add": "Give a registered SQL task (sql-command-add) a server to run on, when to run, and where its "
                      "output and alerts go.",
    "app-command-set": "Change one scheduled app command's schedule or state, field by field.",
    "due-check": "Say whether a schedule would run now and, if not, why - evaluated by the same rule the "
                 "scheduler uses.",

    # --- metrics --------------------------------------------------------------------------------------------
    "metric-batch": "Run one target's metric items one after another and return each raw answer - the metrics "
                    "app's own execution step.",
    "metric-toggle": "Switch a metric, a collector type or all metrics on or off for one instance.",
    "metric-severity": "Remap how one metric's statuses are graded for one instance (for example warning -> ok), "
                       "one remap or a list of them.",
    "metric-env": "Set or remove the inputs a collector script reads for one instance (a folder, a service list, "
                  "a port) in its metrics.collector_env.",

    # --- hosts ----------------------------------------------------------------------------------------------
    "probe-host": "Probe a host for its management ports (SSH, WinRM, RDP ...) to see how it can be reached.",
    "host-facts": "Read one host's state - OS, uptime, CPU, memory, disks - and the state of the services named; "
                  "changes nothing.",
    "host-service": "Start, stop or restart named services on a host and wait for the state asked. To restart the "
                    "whole machine use host-restart.",
    "host-restart": "Restart a whole host and prove it came back with its named services running.",
    "run-cmd": "Run one shell command or script on a host over its OS login (SSH, or WinRM on Windows) and return "
               "its output.",

    # --- files on hosts -------------------------------------------------------------------------------------
    "list-files": "List every entry under a folder on a host - kind, bytes, modified time - without hashing. For "
                  "backup files by database and kind use list-backup-files; on a Windows share, smb-list.",
    "read-file": "Read one small file on a host, its bytes proven by size and sha256.",
    "file-checksum": "Hash files on the host that holds them (or on this node) and return one sha256 each.",
    "fetch-file": "Copy one named file from a host to this node. For a packed backup archive whose SHA-256 must "
                  "match at both ends use pull-file; host to host, relay-file.",
    "send-file": "Copy one named file from this node to a host. For a packed backup archive whose SHA-256 must "
                 "match at both ends use push-file.",
    "relay-file": "Copy one file from one host to another through this node, its SHA-256 checked at both ends.",
    "pull-file": "Download one packed backup archive from a host to this node, hashed on both sides; a mismatch "
                 "is refused. Any other single file goes by fetch-file.",
    "push-file": "Upload one packed backup archive from this node to a host, hashed on both sides; a mismatch is "
                 "refused. Any other single file goes by send-file.",
    "pack-files": "Pack named files on a host into one archive and return its SHA-256 - files you name one by one. "
                  "For a whole backup folder use pack-backup.",
    "pack-backup": "Pack a backup folder (or a list of its files) into one tar or zip on the host that holds it, "
                   "hashed there - so a backup set travels as one file (then pull-file / push-file).",
    "copy-files": "Copy named files from one host to another directly (host to host, nothing removed) - the "
                  "backup repository copy, optionally encrypted on the way.",
    "delete-file": "Delete one stray file by force, inside a folder it must be under. For files whose name, bytes "
                   "and sha256 must match a record first, use delete-verified-files.",
    "delete-verified-files": "Delete a list of files, each only if its name, size and sha256 on the host match "
                             "the request; no wildcards, each inside its required folder, and the first that does "
                             "not match stops the list. A one-item list deletes one file.",
    "delete-empty-folders": "Delete a list of folders, each only if it is empty (hidden entries count), deepest "
                            "first; never recursive, and the first not deleted stops the list.",
    "encrypt-file": "Encrypt one file with age where it lies on the repository host, for a key's public half.",

    # --- Windows shares -------------------------------------------------------------------------------------
    "smb-list": "List every file under a folder of a Windows share, with size and modified time.",
    "smb-get": "Download one file from a Windows share to a local path.",
    "smb-delete": "Delete one file on a Windows share - one exact path, never a pattern.",

    # --- backups --------------------------------------------------------------------------------------------
    "backup-database": "Run one backup (full, diff or log) on the host the database runs on, and report whether it "
                       "really completed.",
    "backup-stop": "Stop one running backup on its host: its process tree first, then its database sessions.",
    "list-backup-files": "List the backup files a directory holds, by database and kind (full, diff, log) with "
                         "their times - `action` list; or prune-plan: which of them a retention rule would remove "
                         "(a plan only, nothing is deleted).",

    # --- restores -------------------------------------------------------------------------------------------
    "restore": "Run one restore step on one engine - `step` full (on PostgreSQL the base backup chain), diff "
               "(differential or incremental on top of the full) or log (logs, WAL or archived logs, to the latest "
               "or a point in time). Run them in that order, then verify-restore.",
    "restore-key": "Import the backup-encryption certificate an encrypted SQL Server backup can only be restored "
                   "with - before restore step full.",
    "prepare-staging": "Make a restore's staging folder ready and say which backup files still have to travel "
                       "there; copies and removes nothing.",
    "finish-staging": "After the backup files have travelled (copy-files), give each its source's times and open "
                      "the staging folder to the engine that restores from it.",
    "verify-restore": "Check that restored databases are really usable - not left RESTORING, MOUNTED or in "
                      "recovery - by asking the engine.",

    # --- lab databases --------------------------------------------------------------------------------------
    "create-db-docker": "Build one lab database instance in Docker (PostgreSQL, MySQL, SQL Server, Oracle), here or "
                        "on an Ubuntu host over SSH.",
    "move-db-docker": "Move one lab database Docker instance, data included, to another host.",

    # --- the runtime store (tools of the MCP server itself) -------------------------------------------------
    "store-schema": "List the tables of dbabrain's own runtime store (job runs, metrics, the backup catalog, SLA) "
                    "and their columns, or one table's columns with types.",
    "store-query": "Run one read-only SELECT on dbabrain's own runtime store (job runs, metrics, the backup "
                   "catalog, SLA), always rolled back. The tables are listed by store-schema.",
}

#: ``run-sql-write`` - the write half of run-sql, its own tool.
SQL_WRITE_TEXT = ("Run one statement that changes data or schema on one server (INSERT, UPDATE, DELETE, DDL), kept "
                  "only when the request says `commit` (or `autocommit`) true - otherwise rolled back. A SELECT goes "
                  "by run-sql.")
