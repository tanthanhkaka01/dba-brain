"""CLI for the db_ops runtime store: provision the PostgreSQL store and migrate SQLite into it.

    # 1. inspect what the store declaration resolves to
    python -m db_ops.db.cli store-info

    # 2. create the database + schema on the PostgreSQL primary (idempotent)
    python -m db_ops.db.cli create-store-database --key-base64 <K>

    # 3. dry-run the migration: prints the DDL and the row count per table, writes nothing
    python -m db_ops.db.cli migrate-sqlite-to-postgres --key-base64 <K> --dry-run

    # 4. migrate for real, then verify
    python -m db_ops.db.cli migrate-sqlite-to-postgres --key-base64 <K>
    python -m db_ops.db.cli verify-migration --key-base64 <K>

Every command reads the PostgreSQL target from ``data/store_config.json`` even while
``backend`` is still ``sqlite``: the point is to build and check the new store before flipping
the switch. The SQLite source defaults to the store the config resolves to, which on the worker
is the worker's own file — run these inside the worker container so both databases are local:

    python -m db_ops.control.cli worker-run --key-base64 <K> -- \
        python -m db_ops.db.cli migrate-sqlite-to-postgres --key-base64 <K>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from db_ops.lib import response, secret_text
# One request parser for the whole tool. The JSON-object contract is `common`'s to define; this
# module is a caller of it, not a second implementation — two would drift on `@file` and stdin.
from db_ops.common import data_sources
from db_ops.common.cli import _read_json_request
from db_ops.config import DEFAULT_CONFIG_PATH, load_config, resolve_config_path
from db_ops.db import postgres_store
from db_ops.db import sqlite_to_postgres as migration
from db_ops.db.schema_export import export_sqlite_schema


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DB Ops runtime store: PostgreSQL provisioning and SQLite migration.",
    )
    parser.add_argument("--config", default=None, help="Path to config JSON.")
    subparsers = parser.add_subparsers(dest="command")

    info = subparsers.add_parser(
        "store-info", help="Show the resolved store declaration (backend, connection, paths).")
    info.set_defaults(handler=_handle_store_info)

    init = subparsers.add_parser(
        "init",
        help="Create/upgrade the store schema on the ACTIVE backend (sqlite or postgresql).")
    _add_secret_args(init)
    init.set_defaults(handler=_handle_init)

    check = subparsers.add_parser(
        "check",
        help="Connect to the ACTIVE backend and report tables, schema version and row counts.")
    _add_secret_args(check)
    check.add_argument("--counts", action="store_true", help="Also count rows per table.")
    check.set_defaults(handler=_handle_check)

    create = subparsers.add_parser(
        "create-store-database",
        help="Create the db_ops store database + schema on the PostgreSQL primary (idempotent).")
    _add_secret_args(create)
    create.add_argument("--dry-run", action="store_true",
                        help="Report what would be created without creating it.")
    create.add_argument("--allow-standby", action="store_true",
                        help="Do not refuse when the target is a read-only standby (not recommended).")
    create.set_defaults(handler=_handle_create_store_database)

    mig = subparsers.add_parser(
        "migrate-sqlite-to-postgres",
        help="Copy the SQLite runtime store into the PostgreSQL store database.")
    _add_secret_args(mig)
    _add_source_args(mig)
    mig.add_argument("--dry-run", action="store_true",
                     help="Print the generated DDL and per-table row counts; write nothing.")
    mig.add_argument("--only-tables", nargs="+", default=(),
                     help="Migrate just these tables (use to resume an interrupted run).")
    mig.add_argument("--exclude-tables", nargs="+", default=(),
                     help="Migrate everything except these tables.")
    mig.add_argument("--batch-rows", type=int, default=migration.DEFAULT_BATCH_ROWS,
                     help=f"Rows per COPY batch (default {migration.DEFAULT_BATCH_ROWS}).")
    mig.add_argument("--delta", action="store_true",
                     help="Bring the target up to date instead of reloading it: small tables are "
                          "reloaded whole, larger ones get rows above the target's max id appended "
                          "plus a re-sync of the recent window. Use after an earlier full migration.")
    mig.add_argument("--reload-under", type=int, default=migration.DEFAULT_RELOAD_UNDER_ROWS,
                     help="Delta mode: reload tables with at most this many rows whole "
                          f"(default {migration.DEFAULT_RELOAD_UNDER_ROWS}).")
    mig.add_argument("--resync-days", type=int, default=migration.DEFAULT_RESYNC_DAYS,
                     help="Delta mode: also re-sync rows newer than this many days on large tables, "
                          f"where UPDATEs land (default {migration.DEFAULT_RESYNC_DAYS}; 0 disables).")
    mig.add_argument("--skip-data", action="store_true",
                     help="Do not copy rows; only create tables and then build indexes, foreign "
                          "keys and identity sequences. Use to resume a run that finished copying "
                          "and then failed during the index phase.")
    mig.add_argument("--skip-indexes", action="store_true",
                     help="Do not build indexes. Use when loading tables in several passes; "
                          "run once more without it at the end.")
    mig.add_argument("--skip-foreign-keys", action="store_true",
                     help="Do not add foreign keys.")
    mig.add_argument("--show-ddl", action="store_true", help="Print the DDL that was executed.")
    mig.set_defaults(handler=_handle_migrate)

    ver = subparsers.add_parser(
        "verify-migration", help="Compare row counts per table between SQLite and PostgreSQL.")
    _add_secret_args(ver)
    _add_source_args(ver)
    ver.set_defaults(handler=_handle_verify)

    snap = subparsers.add_parser(
        "snapshot-sqlite",
        help="Write a transactionally consistent copy of the live SQLite store (VACUUM INTO).")
    snap.add_argument("--sqlite-path", default=None,
                      help="Source store (default: the path the config resolves to).")
    snap.add_argument("--output", required=True, help="Snapshot file to create.")
    snap.set_defaults(handler=_handle_snapshot)

    exp = subparsers.add_parser(
        "export-sqlite-schema", help="Write the SQLite structure JSON snapshot.")
    exp.add_argument("--output-dir", default="runtime")
    exp.set_defaults(handler=_handle_export_schema)

    return parser


def _add_secret_args(parser: argparse.ArgumentParser) -> None:
    secret_text.add_key_argument(parser)
    parser.add_argument(
        "--password", default=None,
        help="Store password, overriding the secret store. Prefer password_ref + --key/--key-base64; "
             "this exists for provisioning from a machine without the passphrase to hand.")


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sqlite-path", default=None,
        help="SQLite source (default: the store the config resolves to, i.e. this node's own file).")


def _postgres_target(args):
    """The PostgreSQL store block, plus the resolved SQLite source path."""
    config = load_config(resolve_config_path("db", getattr(args, "config", None)))
    postgres = config.store.postgresql
    if not (postgres.host and postgres.database):
        raise SystemExit(
            "The postgresql block in data/store_config.json has no host/database. Fill it in "
            "before provisioning or migrating."
        )
    sqlite_path = Path(getattr(args, "sqlite_path", None) or config.sqlite_path)
    return config, postgres, sqlite_path


def _resolved_key(args) -> str | None:
    return secret_text.resolve_cli_key(getattr(args, "key", None), getattr(args, "key_base64", None))


def _handle_store_info(args) -> int:
    config = load_config(resolve_config_path("db", args.config))
    store = config.store
    print(f"backend            : {store.backend}")
    print(f"declared in        : {store.config_file or '(none - legacy config.json sqlite_path)'}")
    print(f"active connection  : {store.connection_string}")
    print(f"sqlite path        : {config.sqlite_path}")
    exists = Path(config.sqlite_path).exists()
    if exists:
        size_mb = Path(config.sqlite_path).stat().st_size / 1048576
        print(f"sqlite file        : present, {size_mb:.1f} MB")
    else:
        print("sqlite file        : missing")
    postgres = store.postgresql
    print(f"postgres target    : {postgres.connection_string or '(not configured)'}")
    print(f"postgres schema    : {postgres.schema or 'public'}")
    print(f"postgres password  : ref={postgres.password_ref or '(none)'}")
    return 0


#: The store's schema is created by seven classes, each owning its own tables. ``init``/``check``
#: drive all of them so neither command has a partial view of the store.
STORE_CLASSES = ("DbOpsStore", "MetricStore", "SlaStore", "BackupRestoreHistory", "ConfigStore",
                 "WebAuthStore", "RunRequestStore")


def _store_classes():
    # All four live below this layer now. Until 2026-08-11 two of them were inside their apps, so
    # composing the schema here meant importing *up* into `sla` and `backup_restore` — the standing
    # exception in tests/test_import_boundaries.py. Moving the two stores into `common` retired it,
    # rather than documenting the inversion a third time.
    from db_ops.db.backup_restore_history import BackupRestoreHistory
    from db_ops.db.config_store import ConfigStore
    from db_ops.db.metric_store import MetricStore
    from db_ops.db.run_requests import RunRequestStore
    from db_ops.db.sla_store import SlaStore
    from db_ops.db.web_auth_store import WebAuthStore
    from db_ops.db import DbOpsStore

    return (DbOpsStore, MetricStore, SlaStore, BackupRestoreHistory, ConfigStore, WebAuthStore,
            RunRequestStore)


def _active_target(args):
    """A :class:`StoreTarget` for whichever backend ``data/store_config.json`` declares.

    This is what makes ``init``/``check`` work identically on SQLite and PostgreSQL: the commands
    never name a backend, they use the declared one.
    """
    from db_ops.db.backend import StoreTarget

    config = load_config(resolve_config_path("db", getattr(args, "config", None)))
    return config, StoreTarget.from_config(
        config, key=_resolved_key(args), password=getattr(args, "password", None)
    )


def _handle_init(args) -> int:
    config, target = _active_target(args)
    print(f"backend: {target.store.backend}")
    print(f"target : {target.store.connection_string}")
    for store_class in _store_classes():
        # force: 'init' is an explicit build/repair request, so the schema-version guard
        # that makes routine initialize() calls cheap must not skip it.
        store_class(target).initialize(force=True)
        print(f"  {store_class.__name__:24} schema ready")
    print("Store initialized.")
    return 0


def _handle_check(args) -> int:
    config, target = _active_target(args)
    print(f"backend: {target.store.backend}")
    print(f"target : {target.store.connection_string}")
    if target.is_sqlite:
        exists = Path(target.sqlite_path).exists()
        print(f"file   : {'present' if exists else 'MISSING'} ({target.sqlite_path})")
        if not exists:
            print("Store file does not exist yet. Run: python -m db_ops.db.cli init")
            return 1

    tables = _list_tables(target)
    print(f"tables : {len(tables)}")
    if not tables:
        print("No tables found. Run: python -m db_ops.db.cli init")
        return 1

    with target.connect() as conn:
        row = conn.execute(
            "SELECT schema_version FROM schema_meta WHERE schema_name = ?", ("db_ops",)
        ).fetchone()
        print(f"schema_version: {row['schema_version'] if row else '(unset)'}")
        if args.counts:
            for table in tables:
                # Table names come from the catalog, not from user input.
                count = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
                print(f"  {table:34} {count:>12}")
    return 0


def _list_tables(target) -> list[str]:
    """Table names on the active backend, from whichever catalog it has."""
    with target.connect() as conn:
        if target.is_sqlite:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = current_schema() ORDER BY table_name"
            ).fetchall()
    return [str(row["name"]) for row in rows]


def _handle_create_store_database(args) -> int:
    _, postgres, _ = _postgres_target(args)
    result = postgres_store.create_store_database(
        postgres,
        key=_resolved_key(args),
        password=args.password,
        dry_run=args.dry_run,
        require_primary=not args.allow_standby,
    )
    print(result.summary())
    if result.server_version:
        print(f"  server: {result.server_version.splitlines()[0]}")
    if not args.dry_run:
        print(f"  connect with: {postgres.connection_string}")
        print("  next: migrate-sqlite-to-postgres, then flip 'backend' to 'postgresql' in "
              "data/store_config.json")
    return 0


def _handle_migrate(args) -> int:
    _, postgres, sqlite_path = _postgres_target(args)
    result = migration.migrate(
        postgres,
        sqlite_path=sqlite_path,
        key=_resolved_key(args),
        password=args.password,
        only_tables=args.only_tables,
        exclude_tables=args.exclude_tables,
        batch_rows=args.batch_rows,
        dry_run=args.dry_run,
        delta=args.delta,
        reload_under=args.reload_under,
        resync_days=args.resync_days,
        skip_data=args.skip_data,
        skip_indexes=args.skip_indexes,
        skip_foreign_keys=args.skip_foreign_keys,
    )
    if args.dry_run or args.show_ddl:
        for phase in ("tables", "indexes", "foreign_keys"):
            for statement in result.ddl.get(phase, []):
                print(f"{statement};")
        print()
    print(f"source: {sqlite_path}")
    print(result.report())
    return 0 if result.ok else 1


def _handle_verify(args) -> int:
    _, postgres, sqlite_path = _postgres_target(args)
    result = migration.verify(
        postgres, sqlite_path=sqlite_path, key=_resolved_key(args), password=args.password
    )
    print(f"source: {sqlite_path}")
    print(result.report())
    return 0 if result.ok else 1


def _handle_snapshot(args) -> int:
    config = load_config(resolve_config_path("db", args.config))
    source = Path(args.sqlite_path or config.sqlite_path)
    target = migration.snapshot_sqlite(source, args.output)
    size_mb = target.stat().st_size / 1048576
    print(f"snapshot written: {target} ({size_mb:.1f} MB) from {source}")
    return 0


def _handle_export_schema(args) -> int:
    config = load_config(resolve_config_path("db", args.config))
    print(export_sqlite_schema(sqlite_path=config.sqlite_path, output_dir=args.output_dir))
    return 0



# --- Commands that open the runtime store -------------------------------------------------
#
# These three moved here from ``db_ops.common.cli`` on 2026-08-15. `common` is the API layer and
# the shared library; it writes to no database, because a caller that has the JSON result back
# already has everything it needs to write the row itself. Anything that opens the runtime store
# belongs to ORD 01, which owns it.
#
# ``ops-status`` in particular: its whole point is that it still answers when every app is
# failing, so it must depend on no app. Sitting in `db` — below all of them — is what keeps that
# true. The scheduled entry in ``data/app_commands.json`` calls this module now.
#
# They keep the JSON-object contract: one object in on argv/@file/stdin, one JSON object out. The
# request parser itself stays in `common` (``_read_json_request``) so the whole tool has one, not
# two that drift on `@file` or stdin handling.

QUEUE_TELEGRAM_USAGE = (
    "usage: python -m db_ops.db.cli queue-telegram-message <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Queues ONE outgoing Telegram message and prints the queued row as JSON.\n"
    "The request is a JSON object, given inline, as @path/to/request.json, or on stdin (-):\n"
    "\n"
    '  {"chat_id": "-1001234567890",\n'
    '   "text": "RESTORE FAILED on ACME-192-0-2-249",\n'
    '   "message_type": "failed"}\n'
    "\n"
    "Fields:\n"
    "  chat_id       (required) target chat id\n"
    "  text          (required) message body\n"
    "  message_type  what this message is: started|success|failed|warning|running|critical|plain.\n"
    "                Omit it and pass what you do have instead - level/phase/status - and the\n"
    "                type is derived the same way every app derives it.\n"
    "  level         logging|warning|error|critical\n"
    "  phase         START|END|ERROR|RUNNING\n"
    "  status        a run's own outcome (done|error|PASSED|FAILED|AT_RISK|...)\n"
    "  note, source_type, source_id, reply_message_id, metadata\n"
)


def _queue_telegram_message_command(argv: list[str]) -> int:
    """``queue-telegram-message`` — the CLI face of :mod:`db_ops.db.telegram_queue`.

    Python callers should import :func:`queue_telegram_message` directly; this exists for the
    things that cannot — a shell script, a scheduled command, another language — so they queue a
    message the same way an app does instead of hand-writing an INSERT with no ``message_type``.

    The request is a **JSON object**, matching ``run-sql``: one shape to learn for the shared
    CLI, and a payload that survives a message body containing quotes, newlines or a leading
    dash without any shell quoting games.
    """
    from db_ops.lib.secret_text import set_key_env
    from db_ops.db.telegram_queue import queue_telegram_message
    from db_ops.config import load_config
    from db_ops.db import DbOpsStore

    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(QUEUE_TELEGRAM_USAGE)
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
            print(f"Unexpected argument: {token}\n\n{QUEUE_TELEGRAM_USAGE}", file=sys.stderr)
            return 2

    if not source:
        print(QUEUE_TELEGRAM_USAGE, file=sys.stderr)
        return 2

    try:
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, QUEUE_TELEGRAM_USAGE)
    if request is None:
        return code

    from db_ops.lib import response

    chat_id = str(request.get("chat_id") or "").strip()
    text = str(request.get("text") or "")
    if not chat_id or not text:
        return response.emit(response.fail(
            "queue-telegram-message", "chat_id and text are required."))

    try:
        # The store travels in the request when the caller states it: backend, host, database,
        # login and the already-resolved password. That is the model everything else here follows
        # - `run-sql` has always been handed its target - and it is what lets a caller name a store
        # that is not this node's own, which an in-process call could do and a subprocess could
        # not. Falling back to config.json keeps the bare `queue-telegram-message '{...}'` form
        # working for shell callers that have no store to state.
        if request.get("store"):
            from db_ops.db.declaration import parse as parse_store

            store = DbOpsStore(parse_store(request["store"]))
        else:
            store = DbOpsStore.from_config(load_config(config_path))
        send_tlgmsg_id = queue_telegram_message(
            store=store,
            chat_id=chat_id,
            text=text,
            message_type=request.get("message_type"),
            level=request.get("level"),
            phase=request.get("phase"),
            status=request.get("status"),
            note=str(request.get("note") or ""),
            source_type=request.get("source_type") or "common_cli",
            source_id=request.get("source_id"),
            reply_message_id=request.get("reply_message_id"),
            metadata=request.get("metadata"),
        )
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("queue-telegram-message", str(exc)))

    # Echo the type that was actually stored: the caller passed a level or a status and needs to
    # see what it resolved to, rather than reading the row back to find out.
    from db_ops.lib.telegram_severity import normalize_message_type

    resolved = normalize_message_type(request.get("message_type"))
    if not resolved:
        from db_ops.db.telegram_queue import message_type_for

        resolved = normalize_message_type(
            message_type_for(
                level=request.get("level"), phase=request.get("phase"), status=request.get("status")
            )
        )
    return response.emit(response.ok(
        "queue-telegram-message",
        message=f"queued send_tlgmsg_id {send_tlgmsg_id} to {chat_id} as {resolved or 'plain'}.",
        data={"send_tlgmsg_id": send_tlgmsg_id, "message_type": resolved or None,
              "chat_id": chat_id},
    ))


RESTORE_DRILL_USAGE = (
    "usage: python -m db_ops.db.cli restore-drill-status <json>|@<file>|- [--config ...]\n"
    "\n"
    "Has a restore actually been PROVEN lately, per database.\n"
    "'There is a backup' and 'we can restore' are different claims and only one can be\n"
    "tested; db_ops.backup_restore already runs the drills and records every one - this\n"
    "reads that history against a policy.\n"
    "\n"
    "The request is a JSON object, given inline, as @path/to/request.json, or on stdin (-):\n"
    '  {"max_age_hours": 168,      // optional; default 168 (24*7 = one week), or\n'
    "                             // per-database in data/restore_drill_policy.json\n"
    '   "database": "APPDB_Prod",   // optional; default = every database with history\n'
    '   "status": "CRITICAL"}      // optional; only report this verdict\n'
    "\n"
    "Exit 0 when every database is within policy, 1 when any is not.\n"
)


def _restore_drill_command(argv: list[str]) -> int:
    """``restore-drill-status`` — read backup_restore_history against the drill-age policy.

    Lives here rather than in ``backup_restore`` because the question is asked BY reports and
    operators, not by the app that performs the restores: ``backup_restore`` runs drills and
    records them, ``common`` reads them, and the two never import each other.
    """
    from db_ops.common import restore_drill

    source = ""
    config_path = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(RESTORE_DRILL_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{RESTORE_DRILL_USAGE}", file=sys.stderr)
            return 2

    request, code = _read_json_request(source or "{}", RESTORE_DRILL_USAGE)
    if request is None:
        return code

    from db_ops.config import load_config, resolve_config_path
    from db_ops.db import DbOpsStore

    try:
        store = DbOpsStore.from_config(load_config(resolve_config_path("backup_restore", config_path)))
        with store.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT database_name, status, restore_start FROM backup_restore_history")]
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("restore-drill-status", str(exc)))

    override = request.get("max_age_hours")
    results = restore_drill.evaluate(
        rows, override_hours=float(override) if override is not None else None)
    wanted_db = str(request.get("database") or "").strip().lower()
    if wanted_db:
        results = [r for r in results if r["database"].lower() == wanted_db]
    wanted_status = str(request.get("status") or "").strip().upper()
    if wanted_status:
        results = [r for r in results if r["status"] == wanted_status]

    summary = restore_drill.summarize(results)
    # `success` is *the check ran*; whether every database has a proven restore is
    # `data.summary.status`. The same split `check-credentials` makes — a database whose drill is
    # overdue is a fact about the estate, not a failed command — and the exit code still says 1 so
    # a runbook reading `$?` is unchanged.
    proven = summary["status"] == "OK"
    response.emit(response.ok(
        "restore-drill-status",
        message=f"{summary['status']}: {len(results)} database(s) checked against the drill policy.",
        data={"ok": proven, "summary": summary, "databases": results},
        metrics={"databases": len(results)},
    ))
    return 0 if proven else 1


OPS_STATUS_USAGE = (
    "usage: python -m db_ops.db.cli ops-status <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Reports how db_ops ITSELF is running: every app command's last run, whether it failed, and\n"
    "whether it is overdue against its own interval. Nothing else in db_ops watches the watcher.\n"
    "\n"
    "The request is a JSON object, given inline, as @path/to/request.json, or on stdin (-):\n"
    "\n"
    '  {"telegram_chat": "control", "mode": "auto",\n'
    '   "summary_from_hour": 8, "summary_to_hour": 20,\n'
    '   "summary_interval_seconds": 3600, "window_hours": 24}\n'
    "\n"
    "Fields:\n"
    "  mode          auto (default) | summary | failures | report\n"
    "                auto     - what the scheduled app runs: alert immediately on an app that has\n"
    "                           just STARTED failing, and send the periodic summary when it is due\n"
    "                summary  - send the summary now, ignoring the window and the interval\n"
    "                failures - send only the failure alert, if there is one\n"
    "                report   - send nothing; print the JSON snapshot (safe to run any time)\n"
    "  telegram_chat notify level to route to (resolved through data/telegram_groups.json)\n"
    "  chat_id       explicit chat id; wins over telegram_chat\n"
    "  window_hours  how far back the run counts are taken (default 24)\n"
    "  summary_from_hour / summary_to_hour\n"
    "                LOCAL hours the periodic summary may be sent in (default 8..20, inclusive).\n"
    "                The failure alert ignores them entirely - a broken app at 03:00 is news then.\n"
    "  summary_interval_seconds  minimum gap between summaries (default 3600)\n"
    "\n"
    "Prints the snapshot and what was queued, as JSON.\n"
)


def _ops_status_command(argv: list[str]) -> int:
    """``ops-status`` - the CLI face of :mod:`db_ops.db.ops_status`.

    A separate scheduled app command runs this every minute, and it is deliberately the *only*
    app that reports on the others: it holds no dependency on them, so an estate where every other
    app is failing is exactly the estate this one still reports from.

    Two messages, two rules, one process. The failure alert fires on a **transition** into failure
    and ignores the clock, because an app that breaks at 03:00 is news at 03:00. The summary is
    confined to working hours and to one per interval, because a periodic "everything is fine" at
    04:00 is how a channel becomes something people mute.
    """
    from datetime import datetime, timezone

    from db_ops.db import ops_status as ops
    from db_ops.lib.secret_text import set_key_env
    from db_ops.db.telegram_queue import queue_telegram_message
    from db_ops.config import chat_id_for_level, load_config
    from db_ops.db import DbOpsStore

    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(OPS_STATUS_USAGE)
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
            print(f"Unexpected argument: {token}\n\n{OPS_STATUS_USAGE}", file=sys.stderr)
            return 2

    if not source:
        print(OPS_STATUS_USAGE, file=sys.stderr)
        return 2
    try:
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, OPS_STATUS_USAGE)
    if request is None:
        return code

    mode = str(request.get("mode") or "auto").strip().lower()
    if mode not in {"auto", "summary", "failures", "report"}:
        response.emit(response.fail(
            "ops-status", f"mode must be auto|summary|failures|report, got '{mode}'."))
        return 2

    summary_skipped = ""
    try:
        config = load_config(config_path)
        store = DbOpsStore.from_config(config)
        # The composition root resolves the folder; ops_status itself takes it as a
        # parameter so the module stays in the library tier (tests/test_common_layers.py).
        data_dir = data_sources.DEFAULT_DATA_DIR
        chat_id = str(request.get("chat_id") or "").strip()
        if not chat_id:
            level = str(request.get("telegram_chat") or "control").strip().lower()
            active_levels = _active_group_levels(data_dir)
            chat_id = chat_id_for_level(active_levels, level)
            if not chat_id:
                # Two different situations, and only one of them is anybody's mistake.
                #
                # **No groups at all** is a fresh tool root: nobody has set Telegram up yet. This
                # command is scheduled by default, so reporting that as a failure meant a new
                # install logged an error every minute with nothing wrong. It is a skip.
                #
                # **Groups exist but none carries this level** is a real misconfiguration — the
                # operator meant to route this somewhere and the routing does not reach. Saying
                # which level was not mapped is the whole content of that answer, so it stays a
                # failure.
                if not active_levels:
                    return response.emit(response.ok(
                        "ops-status",
                        message=("No Telegram groups are configured, so there is nowhere to "
                                 "report to. Add one to data/telegram_groups.json with a "
                                 "notify_level."),
                        data={"skipped": True, "reason": "telegram_not_configured"}))
                return response.emit(response.fail(
                    "ops-status",
                    f"No active Telegram group carries notify_level '{level}'."))

        status = ops.build_ops_status(
            store=store, data_dir=data_dir,
            window_hours=int(request.get("window_hours") or 24),
            # "Failures since the last alert" — the alert's own queue row is the watermark, so the
            # answer does not depend on this process happening to run at the instant an app broke.
            # See ops_status: the previous "compare the two newest runs" test never once fired.
            alerted_since=ops.last_failure_alert_at(store=store, chat_id=chat_id),
            alert_lookback_seconds=int(request.get("alert_lookback_seconds") or 3600),
        )
        queued: list[dict] = []

        if mode in {"auto", "failures"} and status["just_failed"]:
            queued.append({
                "kind": "failure_alert",
                "send_tlgmsg_id": queue_telegram_message(
                    store=store, chat_id=chat_id, text=ops.format_failure_alert(status),
                    level="error", note=ops.FAILURE_NOTE, source_type=ops.SOURCE_TYPE,
                ),
                "apps": [item["app"] for item in status["just_failed"]],
            })

        if mode in {"auto", "summary"}:
            due = True
            if mode == "auto":
                due, summary_skipped = ops.summary_is_due(
                    last_sent=ops.last_summary_sent_at(store=store, chat_id=chat_id),
                    now_local=datetime.now(timezone.utc).astimezone(),
                    from_hour=int(request.get("summary_from_hour", 8)),
                    to_hour=int(request.get("summary_to_hour", 20)),
                    interval_seconds=int(request.get("summary_interval_seconds") or 3600),
                )
            if due:
                queued.append({
                    "kind": "summary",
                    "send_tlgmsg_id": queue_telegram_message(
                        store=store, chat_id=chat_id, text=ops.format_summary(status),
                        level="error" if status["failing"] else "logging",
                        note=ops.SUMMARY_NOTE, source_type=ops.SOURCE_TYPE,
                    ),
                })
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("ops-status", str(exc)))

    failing = [item["app"] for item in status["failing"]]
    overdue = [item["app"] for item in status["overdue"]]
    # `success` is *the watcher ran*. An app that is failing is exactly what this command exists
    # to report, and answering `success: false` for it would make "db_ops noticed a problem" and
    # "db_ops could not look" the same answer — for the one command whose whole job is the
    # difference.
    trouble = ", ".join(filter(None, [
        f"{len(failing)} failing" if failing else "",
        f"{len(overdue)} overdue" if overdue else "",
    ]))
    return response.emit(response.ok(
        "ops-status",
        message=(f"{len(status['apps'])} app command(s): " + (trouble or "none failing, none overdue")
                 + (f"; {len(queued)} message(s) queued" if queued else "")),
        data={"ok": True, "mode": mode, "chat_id": chat_id, "queued": queued,
              "summary_skipped": summary_skipped,
              "failing": failing, "overdue": overdue,
              "generated_at": status["generated_at"],
              "apps": status["apps"] if mode == "report" else len(status["apps"])},
        metrics={"failing": len(failing), "overdue": len(overdue), "queued": len(queued)},
    ))


def _active_group_levels(data_dir: Path) -> dict[str, str]:
    """``notify_level -> group_id`` for the active Telegram groups.

    Read through ``common.data_sources`` rather than the Telegram app's ``groups`` subprocess:
    this command has to work when other apps do not, and shelling out to one of them to find out
    where to report their failure is the dependency it exists without. An import is not a
    process, so the constraint holds and the file still has exactly one reader.
    """
    levels: dict[str, str] = {}
    for group in data_sources.load_telegram_groups(data_dir=data_dir):
        level = str(group.get("notify_level") or "").strip().lower()
        if level and str(group.get("status") or "active").strip().lower() == "active":
            levels[level] = str(group.get("group_id") or "").strip()
    return levels


SYNC_CONFIG_USAGE = (
    "usage: python -m db_ops.db.cli sync-config <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Mirrors the config files listed in data/config_catalog.json into the store's config_*\n"
    "tables - one row per record, keyed the way the file already keys it. A record that has\n"
    "left its file is flagged is_active = 0 and keeps its row; nothing is ever deleted.\n"
    "\n"
    "The request is a JSON object, given inline, as @path/to/request.json, or on stdin (-):\n"
    "\n"
    '  {"dry_run": true}\n'
    '  {"files": ["metric_definitions.json"], "actor": "thanh"}\n'
    '  {"apps": ["telegram"], "actor": "webhost"}\n'
    "\n"
    "Fields:\n"
    "  files     only these catalog files (default: every file in the catalog)\n"
    "  apps      only files owned by these app_codes (metrics, telegram, sql_tasks, ...)\n"
    "  data_dir  where the config files are (default: the tool's data/ folder)\n"
    "  actor     who is making the change; recorded on every row and revision\n"
    "  dry_run   report what would change and write no config row\n"
    "  store     the store to write to, stated as a declaration block (see db_ops.db.declaration).\n"
    "            Omit it and the store named by --config is used.\n"
    "\n"
    "Prints a per-file summary of inserted/updated/unchanged/deactivated as JSON.\n"
)

CONFIG_ITEMS_USAGE = (
    "usage: python -m db_ops.db.cli config-items <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Reads back what sync-config wrote: the mirrored config records, newest schema first by\n"
    "app, file and position. This is the read side the web UI is built on.\n"
    "\n"
    '  {"app_code": "metrics"}\n'
    '  {"source_file": "telegram_groups.json", "include_inactive": true}\n'
    "\n"
    "Fields:\n"
    "  app_code / source_file / collection   narrow the listing (all optional)\n"
    "  include_inactive  also return retired records (default false)\n"
    "  payloads          include each record's JSON (default true; false lists keys only)\n"
    "  limit             cap the number of records returned (default 500, 0 = no cap)\n"
    "  store             the store to read, as a declaration block\n"
)


def _config_command_args(argv: list[str], usage: str) -> tuple[str, str, str | None, str | None, int]:
    """The argv shape both config commands take: one JSON source plus the shared flags.

    Returns ``(source, config_path, key, key_base64, code)``; a non-zero ``code`` means the
    caller should return it and stop. Written once for the two commands because the daemon
    injects ``--key-base64`` ahead of the command name, so neither may assume ``argv[0]``.
    """
    source = ""
    config_path = "config.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(usage)
            return "", config_path, None, None, -1
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
            return "", config_path, None, None, 2
    if not source:
        print(usage, file=sys.stderr)
        return "", config_path, None, None, 2
    return source, config_path, key, key_base64, 0


def _config_store_from(request: dict, config_path: str):
    """The ConfigStore a config command writes to.

    Same rule as ``queue-telegram-message``: the store travels in the request when the caller
    states it, so a test or another node can name a store that is not this node's own; otherwise
    it is the store ``--config`` resolves to.
    """
    from db_ops.db.config_store import ConfigStore

    if request.get("store"):
        from db_ops.db.declaration import parse as parse_store

        return ConfigStore(parse_store(request["store"]))
    return ConfigStore.from_config(load_config(config_path))


def _sync_config_command(argv: list[str]) -> int:
    """``sync-config`` — the CLI face of :mod:`db_ops.db.config_sync`.

    It lives here rather than in ``common`` for the same reason ``queue-telegram-message`` does:
    it opens the runtime store, and ``common`` writes to no database.
    """
    from db_ops.db import config_sync

    source, config_path, key, key_base64, code = _config_command_args(argv, SYNC_CONFIG_USAGE)
    if code:
        return 0 if code < 0 else code
    try:
        secret_text.set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, SYNC_CONFIG_USAGE)
    if request is None:
        return code

    try:
        store = _config_store_from(request, config_path)
        summary = config_sync.sync(
            store,
            data_dir=request.get("data_dir") or None,
            files=tuple(request.get("files") or ()),
            apps=tuple(request.get("apps") or ()),
            actor=str(request.get("actor") or "sync-config"),
            dry_run=bool(request.get("dry_run")),
        )
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("sync-config", str(exc)))

    totals = summary["totals"]
    # A file that could not be read is the outcome worth failing on: the rest of the run is
    # still applied (one bad file must not cost the whole estate's config), but the caller has
    # to see a non-zero exit rather than a success line with a quiet "failed: 1" inside it.
    failed = [item for item in summary["files"] if item["status"] == "failed"]
    headline = (f"{len(summary['files'])} file(s): "
                f"{totals['inserted']} inserted, {totals['updated']} updated, "
                f"{totals['unchanged']} unchanged, {totals['deactivated']} deactivated"
                + (" (dry run)" if summary["dry_run"] else ""))
    if failed:
        return response.emit(response.fail(
            "sync-config",
            "; ".join(f"{item['file']}: {item['error']}" for item in failed),
            message=headline, data=summary, metrics=totals))
    return response.emit(response.ok(
        "sync-config", message=headline, data=summary, metrics=totals))


def _config_items_command(argv: list[str]) -> int:
    """``config-items`` — read the mirrored config back out of the store."""
    source, config_path, key, key_base64, code = _config_command_args(argv, CONFIG_ITEMS_USAGE)
    if code:
        return 0 if code < 0 else code
    try:
        secret_text.set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, CONFIG_ITEMS_USAGE)
    if request is None:
        return code

    want_payloads = request.get("payloads", True)
    limit = int(request.get("limit", 500) or 0)
    try:
        store = _config_store_from(request, config_path)
        rows = store.list_items(
            app_code=str(request.get("app_code") or "") or None,
            source_file=str(request.get("source_file") or "") or None,
            collection=str(request.get("collection") or "") or None,
            include_inactive=bool(request.get("include_inactive")),
        )
    except Exception as exc:  # noqa: BLE001
        return response.emit(response.fail("config-items", str(exc)))

    total = len(rows)
    if limit > 0:
        rows = rows[:limit]
    items = []
    for row in rows:
        item = {
            "config_item_id": row["config_item_id"],
            "app_code": row["app_code"],
            "source_file": row["source_file"],
            "collection": row["collection"],
            "item_key": row["item_key"],
            "item_ord": row["item_ord"],
            "label": row["label"],
            "revision": row["revision"],
            "is_active": row["is_active"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }
        if want_payloads:
            item["payload"] = json.loads(row["item_json"])
        items.append(item)
    return response.emit(response.ok(
        "config-items",
        message=f"{len(items)} of {total} config record(s).",
        data={"items": items, "total": total},
        metrics={"returned": len(items), "total": total},
    ))


EXPORT_CONFIG_USAGE = (
    "usage: python -m db_ops.db.cli export-config <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Writes the mirrored config back out to data/*.json - the inverse of sync-config. Each file\n"
    "is rebuilt from its ACTIVE rows, so a record retired in the store disappears from the file\n"
    "while its row and history stay. A file whose rebuilt text already matches is left alone.\n"
    "\n"
    "The console does this automatically on every save; run it by hand after editing rows\n"
    "directly, or to prove the files and the store still agree.\n"
    "\n"
    '  {\"dry_run\": false}\n'
    '  {\"files\": [\"reports_config.json\"]}\n'
    '  {\"apps\": [\"metrics\"]}\n'
    "\n"
    "Fields:\n"
    "  files     only these catalog files (default: every file in the catalog)\n"
    "  apps      only files owned by these app_codes\n"
    "  data_dir  where the config files are (default: the tool's data/ folder)\n"
    "  store     the store to read, stated as a declaration block\n"
)


def _export_config_command(argv: list[str]) -> int:
    """``export-config`` — the store -> ``data/*.json`` direction of :mod:`db_ops.db.config_sync`.

    Its own command rather than a flag on ``sync-config`` because the two directions have
    opposite failure modes: a bad sync loses nothing (the files are still there), a bad export
    overwrites the files the apps read. Making the caller name the direction means nobody gets it
    by accident.
    """
    from db_ops.db import config_sync

    source, config_path, key, key_base64, code = _config_command_args(argv, EXPORT_CONFIG_USAGE)
    if code:
        return 0 if code < 0 else code
    try:
        secret_text.set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, EXPORT_CONFIG_USAGE)
    if request is None:
        return code
    try:
        store = _config_store_from(request, config_path)
        summary = config_sync.export(
            store,
            data_dir=request.get("data_dir") or None,
            files=tuple(request.get("files") or ()),
            apps=tuple(request.get("apps") or ()),
        )
    except Exception as exc:  # noqa: BLE001
        return response.emit(response.fail("export-config", str(exc)))

    totals = summary["totals"]
    skipped = [item for item in summary["files"] if item["status"] == "skipped"]
    headline = (f"{len(summary['files'])} file(s): {totals['written']} written, "
                f"{totals['unchanged']} unchanged, {totals['skipped']} skipped")
    if skipped:
        return response.emit(response.fail(
            "export-config", "; ".join(f"{item['file']}: {item['error']}" for item in skipped),
            message=headline, data=summary, metrics=totals))
    return response.emit(response.ok(
        "export-config", message=headline, data=summary, metrics=totals))


RUN_APP_USAGE = (
    "usage: python -m db_ops.db.cli run-app <json>|@<file>|- "
    "[--config ...] [--key ... | --key-base64 ...]\n"
    "\n"
    "Queues one app command to be run by the daemon on its next scan - the same request the\n"
    "console's 'Run now' button writes. Nothing is started by this command: the daemon owns the\n"
    "working directory, the log scope, the forwarded key and the timeout reaper, so it is the one\n"
    "process that starts an app.\n"
    "\n"
    '  {\"app_command_id\": \"APP-METRICS\", \"requested_by\": \"thanh\"}\n'
    '  {\"list\": true}\n'
    "\n"
    "Fields:\n"
    "  app_command_id  which command to queue (required unless 'list')\n"
    "  requested_by    who is asking; recorded on the request and on the job_runs row\n"
    "  note            free text kept with the request\n"
    "  list            print the recent requests instead of queueing one\n"
    "  store           the store to write to, as a declaration block\n"
)


def _run_app_command(argv: list[str]) -> int:
    """``run-app`` — ask the daemon to run one app command now."""
    from db_ops.db.run_requests import RunRequestStore

    source, config_path, key, key_base64, code = _config_command_args(argv, RUN_APP_USAGE)
    if code:
        return 0 if code < 0 else code
    try:
        secret_text.set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    request, code = _read_json_request(source, RUN_APP_USAGE)
    if request is None:
        return code
    try:
        if request.get("store"):
            from db_ops.db.declaration import parse as parse_store

            store = RunRequestStore(parse_store(request["store"]))
        else:
            store = RunRequestStore.from_config(load_config(config_path))

        if request.get("list"):
            rows = store.list_requests(app_command_id=request.get("app_command_id") or None,
                                       limit=int(request.get("limit") or 25))
            data = [{"request_id": row["request_id"], "app_command_id": row["app_command_id"],
                     "status": row["status"], "requested_by": row["requested_by"],
                     "requested_at": row["requested_at"], "job_run_id": row["job_run_id"],
                     "note": row["note"]} for row in rows]
            return response.emit(response.ok(
                "run-app", message=f"{len(data)} request(s).", data={"requests": data}))

        app_command_id = str(request.get("app_command_id") or "").strip()
        if not app_command_id:
            return response.emit(response.fail("run-app", "app_command_id is required."))
        answer = store.request_run(
            app_command_id=app_command_id,
            requested_by=str(request.get("requested_by") or "cli"),
            source="cli", note=str(request.get("note") or ""))
    except Exception as exc:  # noqa: BLE001
        return response.emit(response.fail("run-app", str(exc)))

    message = (f"{app_command_id} queued as request {answer['request_id']}; the daemon starts it "
               "on its next scan.") if answer["created"] else (
        f"{app_command_id} was already queued as request {answer['request_id']} by "
        f"{answer['requested_by'] or 'someone'}; it has not been queued twice.")
    return response.emit(response.ok("run-app", message=message, data=answer))


#: The JSON-object commands, and the handler each dispatches to. They are matched before argparse
#: sees the arguments: their payload is an object, not a set of flags, and argparse has no shape
#: for that.
_JSON_COMMANDS = {
    "queue-telegram-message": lambda rest: _queue_telegram_message_command(rest),
    "ops-status": lambda rest: _ops_status_command(rest),
    "restore-drill-status": lambda rest: _restore_drill_command(rest),
    "sync-config": lambda rest: _sync_config_command(rest),
    "config-items": lambda rest: _config_items_command(rest),
    "export-config": lambda rest: _export_config_command(rest),
    "run-app": lambda rest: _run_app_command(rest),
}

#: Options that carry a separate value, so the token after them is not a command name.
_VALUE_OPTIONS = frozenset({"--key", "--key-base64", "--key_base64", "--config"})


def _split_json_command(argv: list[str]) -> tuple[str, list[str]] | None:
    """Find a JSON command anywhere ahead of the flags, or ``None``.

    Not simply ``argv[0]``, and the reason is a production failure on 2026-08-15. The daemon
    forwards the secret passphrase to any child CLI whose source declares ``add_key_argument``,
    and it inserts ``--key-base64 <value>`` **immediately after** ``python -m db_ops.db.cli`` —
    before the subcommand. ``common/cli.py`` never declared that option, so while these three
    commands lived there the key arrived through the environment and argv[0] was always the
    command. Moving them here made the module key-aware, the daemon started injecting, and
    ``ops-status`` failed every 30 seconds with argparse reporting the *passphrase* as an invalid
    choice of subcommand — 529 times before it was caught.

    So the command is located rather than assumed, and the flags around it are handed to the
    handler untouched (each parses ``--key`` / ``--config`` itself).
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _VALUE_OPTIONS:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if token in _JSON_COMMANDS:
            return token, argv[:index] + argv[index + 1:]
        return None
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    found = _split_json_command(argv)
    if found is not None:
        command, rest = found
        return _JSON_COMMANDS[command](rest)

    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    try:
        # Export the passphrase so nested helpers resolve secrets the same way the daemon's
        # children do.
        secret_text.set_key_env(getattr(args, "key", None), getattr(args, "key_base64", None))
        return int(args.handler(args))
    except (migration.MigrationError, postgres_store.PostgresStoreError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
