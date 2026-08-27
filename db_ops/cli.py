"""The root entry point: the logging/store smoke test, and the one command that spans apps.

Everything else in this tree belongs to exactly one component. ``check-credentials`` does not —
answering "does every configured target resolve to a real login" needs the *metrics* target
loader (which attaches the resolved credential) and the *Telegram* SQL-command resolver (which
owns its own lookup rules), and no component may import two apps: apps may not import each other,
and ``common`` may not import any.

It lived in ``db_ops.common.cli`` until 2026-08-15 behind the single entry in
``ALLOWED_UPWARD_IMPORTS``. That entry was the shared layer's only upward edge in the whole tree,
kept alive by one command that was never really shared-layer work. Here it costs no exception:
``db_ops/cli.py`` is a root module, not a component, so it sits outside both import rules — which
is exactly what a composition root is for.

A config-only reimplementation was the obvious alternative and is the wrong one: it would answer a
*different* question than the one the apps ask at runtime, which is the failure this command
exists to catch.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path

from db_ops.config import DEFAULT_CONFIG_PATH, load_config
from db_ops.db import DbOpsStore
from db_ops.db.schema_export import export_sqlite_schema
from db_ops.db.job_runs import JobRun
from db_ops.levels import normalize_level
from db_ops.logging_ops import log_event, log_function_call, log_function_error, setup_app_logger
from db_ops.logging_ops.runtime_stdout import patch_stdout


CHECK_CREDENTIALS_USAGE = (
    "usage: python -m db_ops.cli check-credentials [<json>|@<file>|-]\n"
    "\n"
    "Verify every configured target resolves to a named credential. Exit 1 if any does not.\n"
    "\n"
    "Answers in the standard response envelope; the unresolvable targets are in data.problems.\n"
    'Pass {"format": "txt"} for the old plain-text listing (problems on stderr, summary on\n'
    "stdout), which is what pasted runbook lines expect.\n"
    '  {"data_dir": "data"}\n'
    "\n"
    "Fields:\n"
    "  data_dir   folder holding db_instances.json / users.json / sql_targets.json\n"
    "             (default: the folder data_sources resolves)\n"
    "\n"
    "Legacy form (still accepted): check-credentials <data-dir>\n"
)


def _check_credentials_command(argv: list[str]) -> int:
    """Verify every configured target resolves to a named credential. Exit 1 if any does not.

    A credential is required, never inferred (see
    :func:`db_ops.common.data_sources.find_database_credential`), so a config edit that drops
    ``default_credential_name`` / ``credential_name`` now stops that target instead of quietly
    connecting as whatever entry happened to come first. Run this after editing
    ``db_instances.json`` / ``users.json`` / ``sql_targets.json`` and before a deploy.
    """
    from db_ops.common import data_sources
    from db_ops.common.cli import _optional_json_request
    from db_ops.metrics import targets as metric_targets
    from db_ops.telegram import sql_commands

    # The request parser stays in `common`: one JSON-object contract for the whole tool, not a
    # second one here that would drift on `@file` or stdin handling.
    request, code = _optional_json_request(argv, CHECK_CREDENTIALS_USAGE)
    if request is None and code:
        return code
    requested_dir = str((request or {}).get("data_dir") or "") if request is not None else (argv[0] if argv else "")

    # An argument starting with `-` is a flag somebody expected this command to take — every other
    # command accepts `--key-base64` — and it was being read as a folder name. The check then
    # walked a directory that does not exist and reported "checked 0 target(s); 0 without a
    # resolvable credential", which reads as a pass. **A verification command must never report
    # success for having looked at nothing.**
    if requested_dir.startswith("-"):
        print(
            f"check-credentials takes a folder, not a flag: {requested_dir}\n"
            "It reads the secret store through the same resolution as everything else and needs "
            "no key.\n\n" + CHECK_CREDENTIALS_USAGE,
            file=sys.stderr,
        )
        return 2

    # load_metric_targets needs a concrete folder; default to the one data_sources resolves.
    data_dir = Path(requested_dir) if requested_dir else data_sources.users_path().parent
    if not data_dir.is_dir():
        print(f"check-credentials: no such folder: {data_dir}", file=sys.stderr)
        return 2
    problems: list[str] = []
    checked = 0

    for target in metric_targets.load_metric_targets(data_dir=data_dir):
        # Host-only entries (no db_type) and API-bridge targets carry no DB login by design.
        if not target.db_type:
            continue
        if str((target.sql_access or {}).get("method") or "direct").lower() == "api":
            continue
        checked += 1
        if not target.credential:
            problems.append(
                f"metrics target {target.target_id}: no credential "
                f"(default_credential_name={target.credential_name or '<unset>'})"
            )

    groups = data_sources.load_all_credentials(data_dir)
    targets_file = data_dir / "sql_targets.json"
    if targets_file.exists():
        entries = json.loads(targets_file.read_bytes().decode("utf-8-sig")).get("sql_targets", [])
        for entry in entries:
            if str(entry.get("active", 1)) in ("0", "false", "False"):
                continue
            checked += 1
            db_type = str(entry.get("db_type") or "").lower()
            try:
                data_sources.find_database_credential(
                    groups.get(db_type, []),
                    server_id=str(entry.get("server_id") or ""),
                    credential_name=str(entry.get("credential_name") or ""),
                    db_type=db_type,
                    service_name=str(entry.get("service_name") or ""),
                    instance_name=str(entry.get("instance_name") or ""),
                )
            except data_sources.CredentialNotFound as exc:
                problems.append(f"sql target {entry.get('sql_id')}/{entry.get('target_no')}: {exc}")

    commands_file = data_dir / "telegram_support_commands.json"
    if commands_file.exists():
        commands = json.loads(commands_file.read_bytes().decode("utf-8-sig"))
        for command in commands.get("telegram_support_commands", []):
            if str(command.get("action_type")) != "sql_execute":
                continue
            checked += 1
            try:
                sql_commands.find_credential(command.get("action_config") or {})
            except Exception as exc:  # noqa: BLE001 - report, do not abort the whole check.
                problems.append(f"telegram command {command.get('command_text')}: {exc}")

    # The answer, in the one response shape (2026-08-16). This command used to put its *finding*
    # in the exit code and its detail on **stderr** — the precise split `lib/response.py`'s own
    # docstring forbids, and it made this one of two commands in the tree a program could not
    # consume at all. `success` is now "the check ran"; whether anything is wrong is
    # `data.problems`, which is a fact about the estate rather than about this process.
    from db_ops.lib import response

    summary = f"checked {checked} target(s); {len(problems)} without a resolvable credential"
    if str((request or {}).get("format") or "json").strip().lower() == "txt":
        for problem in problems:
            print(problem, file=sys.stderr)
        print(summary)
        return 1 if problems else 0
    response.emit(response.ok(
        "check-credentials", message=summary,
        data={"checked": checked, "problems": problems},
        metrics={"checked": checked, "problem_count": len(problems)},
    ))
    # The exit code still says "something is unresolvable", because a runbook and a scheduled
    # caller both check `$?` — and it agrees with the response rather than replacing it.
    return 1 if problems else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DB Ops logging and Telegram alert test.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config JSON.")
    parser.add_argument("--level", default="logging", help="logging, warning, or error.")
    parser.add_argument("--message", default="", help="Message to log and optionally alert.")
    parser.add_argument("--job-code", default="", help="When set, also insert this event into SQLite job_runs.")
    parser.add_argument("--status", default="done", help="Job status for SQLite job_runs.")
    parser.add_argument("--duration-ms", type=int, default=None, help="Job duration in milliseconds.")
    parser.add_argument("--error-text", default=None, help="Error text for SQLite job_runs.")
    parser.add_argument("--recent", type=int, default=0, help="Print recent SQLite job_runs and exit.")
    parser.add_argument("--export-sqlite-schema", action="store_true", help="Export SQLite schema JSON documentation and exit.")
    parser.add_argument("--schema-output-dir", default="runtime", help="Output directory for SQLite structure JSON.")
    return parser.parse_args(argv)


#: Every app, by the name someone types. The value is the module holding that app's ``main`` —
#: imported only when it is asked for, which is what lets ``--help`` and ``--version`` work on an
#: install with no database driver at all. Importing all twelve up front would make the help text
#: depend on having pyodbc.
APPS: dict[str, str] = {
    "metrics": "db_ops.metrics.cli",
    "reports": "db_ops.reports.cli",
    "telegram": "db_ops.telegram.cli",
    "sla": "db_ops.sla.cli",
    "backup-restore": "db_ops.backup_restore.cli",
    "sql-tasks": "db_ops.sql_tasks.cli",
    "sre": "db_ops.sre.cli",
    "control": "db_ops.control.cli",
    "webhost": "db_ops.webhost.cli",
    "db": "db_ops.db.cli",
    "common": "db_ops.common.cli",
    "daemon": "db_ops.jobs.daemon",
}


INIT_USAGE = (
    "usage: db-ops init [<directory>] [--force] [--app-name NAME]\n"
    "\n"
    "Turn a directory into a working tool root. Defaults to the current one.\n"
    "\n"
    "Writes the smallest tree that runs: config.json, a SQLite store declaration, an empty\n"
    "inventory, a starter metric catalogue, and the Telegram and secret files, each carrying\n"
    "notes that say what to put in it. Nothing is overwritten without --force, because the\n"
    "files this writes are the ones you edit next.\n"
    "\n"
    "The store starts on SQLite in runtime/ - a first run has no PostgreSQL, and needing one\n"
    "to hold the results of monitoring is a poor first request. data/store_config.json is\n"
    "where you move to PostgreSQL later.\n"
)


def _init_command(argv: list[str]) -> int:
    """``init`` — the first command anybody runs, and until 2026-08-22 it did not exist.

    Measured on a real `pip install` into a clean virtualenv: from an empty directory the toolkit
    resolved its configuration to `site-packages` and told the reader to create a file there from
    an example that does not ship. Installable and unstartable.
    """
    from pathlib import Path

    from db_ops import scaffold

    target = "."
    app_name = "dbabrain"
    force = False
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(INIT_USAGE)
            return 0
        if token == "--force":
            force = True
        elif token == "--app-name":
            app_name = rest.pop(0) if rest else app_name
        elif token.startswith("-"):
            print(f"init: unknown option {token}\n\n{INIT_USAGE}", file=sys.stderr)
            return 2
        else:
            target = token

    try:
        result = scaffold.initialise(Path(target), app_name=app_name, force=force)
    except scaffold.ScaffoldError as exc:
        print(f"init refused: {exc}", file=sys.stderr)
        return 1

    for name in result.written:
        print(f"  created  {name}")
    for name in result.skipped:
        print(f"  kept     {name} (already there; --force overwrites)")
    print("")
    print(scaffold.next_steps(result.root))
    return 0


ENCRYPT_SECRET_USAGE = (
    "usage: db-ops encrypt-secret [--source PATH] [--dest PATH] --key-base64 B64 | --key TEXT\n"
    "\n"
    "Encrypt secrets/secret_text.json into data/encrypted_secret_text.json, which is the file\n"
    "the toolkit actually reads. The plaintext source is never read at run time and must never\n"
    "be committed.\n"
    "\n"
    "Run it again after every edit to the plaintext file - adding a secret does nothing until\n"
    "it is encrypted.\n"
    "\n"
    "Keep the passphrase. Nothing else can decrypt the store, and there is no recovery.\n"
)


def _encrypt_secret_command(argv: list[str]) -> int:
    """``encrypt-secret`` — turn the plaintext secret source into the store the toolkit reads.

    This lived only in ``db_ops.control.cli`` until 2026-08-22, and `control` is master-side deploy
    tooling that the public distribution does not ship. So the first-run instructions named a
    command that did not exist in the distribution they were written for — found by running the
    documented steps against a real SQL Server rather than by reading them.

    It belongs here on two counts: every install needs it, and it is the secret *store*, which
    ships, rather than the deploy tooling, which does not.
    """
    from pathlib import Path

    from db_ops.lib.paths import TOOL_ROOT
    from db_ops.lib.secret_text import encrypt_secret_text_file, resolve_cli_key

    source = Path(TOOL_ROOT) / "secrets" / "secret_text.json"
    dest = Path(TOOL_ROOT) / "data" / "encrypted_secret_text.json"
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(ENCRYPT_SECRET_USAGE)
            return 0
        if token == "--source":
            source = Path(rest.pop(0)) if rest else source
        elif token == "--dest":
            dest = Path(rest.pop(0)) if rest else dest
        elif token == "--key":
            key = rest.pop(0) if rest else None
        elif token in {"--key-base64", "--key_base64"}:
            key_base64 = rest.pop(0) if rest else None
        else:
            print(f"encrypt-secret: unknown option {token}", file=sys.stderr)
            print("", file=sys.stderr)
            print(ENCRYPT_SECRET_USAGE, file=sys.stderr)
            return 2

    try:
        resolved = resolve_cli_key(key, key_base64)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    resolved = resolved or os.environ.get("DB_OPS_SECRET_KEY", "").strip()
    if not resolved:
        # Refuse rather than guess. Encrypting with the wrong passphrase produces a store that
        # decrypts nowhere, and the round-trip check inside encrypt_secret_text_file cannot catch
        # it, because it verifies against the same key it just used.
        print("ERROR: no passphrase. Pass --key-base64 or --key, or set DB_OPS_SECRET_KEY.",
              file=sys.stderr)
        return 2

    if not source.exists():
        print(f"ERROR: {source} not found. `db-ops init` creates it.", file=sys.stderr)
        return 1
    try:
        count = encrypt_secret_text_file(source, dest, resolved)
    except Exception as exc:  # noqa: BLE001 - CLI failure path.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Encrypted {count} secret(s) -> {dest}")
    return 0


#: The naming convention `.gitignore` covers. Suggested rather than enforced: the operator may
#: legitimately write a bundle to a path outside any repository, and refusing a filename would be
#: the tool overruling that. Departing from it earns one line of warning, because the failure it
#: prevents — an estate committed to git — is silent and permanent.
BUNDLE_NAME_HINT = "<name>-bundle.json"


def _looks_like_an_ignored_bundle_name(name: str) -> bool:
    return (name.endswith("-bundle.json")
            or name.endswith(".dbabrain-bundle.json")
            or name.startswith("dbabrain-export"))


EXPORT_DATA_USAGE = (
    "usage: db-ops export-data <name>-bundle.json [--root DIR] [--no-secrets] [--no-assets]\n"
    "                          [--force]\n"
    "\n"
    "Write this machine's whole configuration to one JSON file, so another machine can run the\n"
    "same estate after nothing more than `pip install dbabrain` and `db-ops import-data`.\n"
    "\n"
    "What crosses: config.json, data/config_catalog.json and every data/*.json the catalog\n"
    "lists, the encrypted secret store, and the estate's own assets/ and data/ssh_keys/.\n"
    "Generated output does not cross - database-inventory.json is rebuilt on the new machine.\n"
    "\n"
    "  --root DIR     the tool root to export (default: the one this process resolved)\n"
    "  --no-secrets   leave data/encrypted_secret_text.json out\n"
    "  --no-assets    leave assets/ and data/ssh_keys/ out\n"
    "  --force        overwrite an existing bundle file\n"
    "\n"
    "THE BUNDLE IS A CREDENTIAL. It names hosts, accounts and chat ids and carries the secret\n"
    "store as ciphertext. Never commit it - .gitignore covers `<name>-bundle.json`, which is why\n"
    "the usage line names that shape. The passphrase is NOT in it and cannot be: the importing\n"
    "machine sets DB_OPS_SECRET_KEY itself.\n"
)


def _export_data_command(argv: list[str]) -> int:
    """``export-data`` — one estate, one file.

    The list of files is derived from ``data/config_catalog.json`` rather than written out here,
    for the reason stated in :mod:`db_ops.lib.config_bundle`: a second list of "which files are
    configuration" is a list that disagrees with the first one the week after somebody adds a
    config file.
    """
    from db_ops import __version__
    from db_ops.lib import config_bundle
    from db_ops.lib.paths import TOOL_ROOT

    target: str | None = None
    root = Path(TOOL_ROOT)
    include_secrets = True
    include_assets = True
    force = False
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(EXPORT_DATA_USAGE)
            return 0
        if token == "--root":
            root = Path(rest.pop(0)) if rest else root
        elif token == "--no-secrets":
            include_secrets = False
        elif token == "--no-assets":
            include_assets = False
        elif token == "--force":
            force = True
        elif token.startswith("-"):
            print(f"export-data: unknown option {token}\n\n{EXPORT_DATA_USAGE}", file=sys.stderr)
            return 2
        elif target is None:
            target = token
        else:
            print(f"export-data: one output file, not two ({target}, {token})", file=sys.stderr)
            return 2

    if not target:
        print(f"export-data: name the file to write.\n\n{EXPORT_DATA_USAGE}", file=sys.stderr)
        return 2
    destination = Path(target).expanduser()
    if destination.exists() and not force:
        print(f"export-data: {destination} already exists. Use --force to replace it.",
              file=sys.stderr)
        return 2

    try:
        bundle = config_bundle.build_bundle(
            root,
            include_secrets=include_secrets,
            include_assets=include_assets,
            tool_version=__version__,
        )
    except config_bundle.BundleError as exc:
        print(f"export-data refused: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"export-data failed: {exc}", file=sys.stderr)
        return 1

    text = config_bundle.bundle_text(bundle)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")

    counts: dict[str, int] = {}
    for entry in bundle["files"].values():
        counts[entry["role"]] = counts.get(entry["role"], 0) + 1
    print(f"wrote {destination} ({len(text.encode('utf-8')) / 1024:.0f} KB)")
    for role in sorted(counts):
        print(f"  {counts[role]:>4}  {role}")
    missing = bundle.get("missing_at_source") or []
    if missing:
        # Named, not silent: these are the files the *other* machine will not get, and finding
        # that out here is cheaper than finding it out when an app cannot start there.
        print(f"  not present here, so not carried ({len(missing)}):")
        for name in missing:
            print(f"      {name}")
    if not bundle.get("includes_secret_store"):
        print("  no secret store in this bundle - the importing machine supplies credentials.")
    else:
        print("  secret store carried as ciphertext; the passphrase is not in this file.")
    if not _looks_like_an_ignored_bundle_name(destination.name):
        print(f"  WARNING: {destination.name} is not a name .gitignore covers. This file is an "
              f"entire estate; name it {BUNDLE_NAME_HINT} or keep it outside any repository.")
    return 0


IMPORT_DATA_USAGE = (
    "usage: db-ops import-data <bundle.json> [--root DIR] [--plan] [--force]\n"
    "                          [--no-secrets] [--no-assets]\n"
    "\n"
    "Apply a bundle written by `db-ops export-data`, so this machine runs the estate the\n"
    "bundle came from.\n"
    "\n"
    "  --root DIR     the tool root to write into (default: the one this process resolved)\n"
    "  --plan         report what would be written and change nothing\n"
    "  --force        replace files that already exist here with different content\n"
    "  --no-secrets   do not write the secret store, even if the bundle carries one\n"
    "  --no-assets    do not write assets/ or data/ssh_keys/\n"
    "\n"
    "Every entry's checksum is verified before anything is written, so a truncated or edited\n"
    "bundle leaves the tree untouched rather than half-applied.\n"
    "\n"
    "The passphrase does not travel in a bundle. After importing one that carries the secret\n"
    "store, set DB_OPS_SECRET_KEY to the source machine's passphrase and run\n"
    "`db-ops check-credentials` to prove it.\n"
)


def _import_data_command(argv: list[str]) -> int:
    """``import-data`` — the second half of the sentence in :mod:`db_ops.lib.config_bundle`.

    Refuses by default to replace a file that already exists with different content. The machine
    being imported into may already be somebody's working install, and an import that silently
    replaced ``db_instances.json`` there would destroy the only copy of it.
    """
    from db_ops.lib import config_bundle
    from db_ops.lib.paths import TOOL_ROOT

    source: str | None = None
    root = Path(TOOL_ROOT)
    plan_only = False
    force = False
    include_secrets = True
    include_assets = True
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(IMPORT_DATA_USAGE)
            return 0
        if token == "--root":
            root = Path(rest.pop(0)) if rest else root
        elif token in {"--plan", "--plan-only", "--dry-run"}:
            plan_only = True
        elif token == "--force":
            force = True
        elif token == "--no-secrets":
            include_secrets = False
        elif token == "--no-assets":
            include_assets = False
        elif token.startswith("-"):
            print(f"import-data: unknown option {token}\n\n{IMPORT_DATA_USAGE}", file=sys.stderr)
            return 2
        elif source is None:
            source = token
        else:
            print(f"import-data: one bundle, not two ({source}, {token})", file=sys.stderr)
            return 2

    if not source:
        print(f"import-data: name the bundle to read.\n\n{IMPORT_DATA_USAGE}", file=sys.stderr)
        return 2
    path = Path(source).expanduser()
    if not path.is_file():
        print(f"import-data: no such file: {path}", file=sys.stderr)
        return 2

    try:
        document = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"import-data: {path} is not readable as JSON: {exc}", file=sys.stderr)
        return 1
    try:
        entries = config_bundle.select_entries(
            config_bundle.read_bundle(document),
            include_secrets=include_secrets,
            include_assets=include_assets,
        )
    except config_bundle.BundleError as exc:
        print(f"import-data refused: {exc}", file=sys.stderr)
        return 1
    if not entries:
        print("import-data: the flags given exclude every file in this bundle.", file=sys.stderr)
        return 2

    root = root.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    planned = config_bundle.plan_import(entries, root)
    if plan_only:
        print(f"bundle: {path}")
        print(f"exported {document.get('exported_at', '?')} "
              f"by version {document.get('exported_by_version') or '?'}")
        print(f"target tool root: {root.resolve()}")
        for item in planned:
            print(f"  {item.action:<9}  {item.path}")
        counts: dict[str, int] = {}
        for item in planned:
            counts[item.action] = counts.get(item.action, 0) + 1
        print("  " + ", ".join(f"{counts[name]} {name}" for name in sorted(counts)))
        return 0

    try:
        result = config_bundle.apply_bundle(entries, root, force=force)
    except config_bundle.BundleError as exc:
        print(f"import-data refused: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"import-data failed: {exc}", file=sys.stderr)
        return 1

    print(f"imported into {root.resolve()}")
    print(f"  {len(result.created)} created, {len(result.overwritten)} replaced, "
          f"{len(result.unchanged)} already identical")
    missing = document.get("missing_at_source") or []
    if missing:
        print(f"  absent on the source machine, so not in this bundle ({len(missing)}):")
        for name in missing:
            print(f"      {name}")
    if document.get("includes_secret_store") and include_secrets:
        print("")
        print("The secret store is here as ciphertext and the passphrase is not. Set")
        print("DB_OPS_SECRET_KEY to the source machine's passphrase, then verify with:")
        print("  db-ops check-credentials")
    return 0


def installed_apps() -> dict[str, str]:
    """The entries of :data:`APPS` this installation actually has.

    `v0.1.0` of the public distribution ships seven of the fourteen components
    (:mod:`db_ops.lib.distribution`), so on an installed copy this table would otherwise advertise
    seven commands that raise `ModuleNotFoundError` the moment somebody runs the line the help text
    just offered them. Measured, not predicted: the first thin wheel built on 2026-08-22 listed all
    twelve apps and could run five.

    Detected rather than hardcoded, so one code path is right in both trees — the full checkout
    lists twelve and the thin wheel lists five, and neither needs to know which it is.

    `find_spec` **locates** a module without executing it, which is what keeps the dispatch lazy:
    importing twelve apps to print a help message would make `--help` depend on having an ODBC
    driver, so a slim install would crash while explaining how to use the tool.
    """
    import importlib.util

    present: dict[str, str] = {}
    for name, module in APPS.items():
        try:
            if importlib.util.find_spec(module) is not None:
                present[name] = module
        except (ImportError, ValueError):
            # A parent package that is not installed raises rather than returning None.
            continue
    return present


def _usage() -> str:
    from db_ops import __version__

    apps = installed_apps() or APPS
    width = max(len(name) for name in apps)
    lines = [
        # ASCII only: this prints to whatever console the operator has, and the Windows one is
        # cp1252. An em dash there arrives as a question mark at best.
        f"db_ops {__version__} - database operations toolkit",
        "",
        "usage: db-ops <app> [args...]",
        "",
        "apps:",
    ]
    lines += [f"  {name.ljust(width)}  python -m {module}" for name, module in sorted(apps.items())]
    lines += [
        "",
        "  init                   create a tool root here: config, a SQLite store, empty inventory",
        "  encrypt-secret         encrypt secrets/secret_text.json into the store the tool reads",
        "  check-credentials      does every configured target resolve to a real login",
        "  export-data            write this machine's whole configuration to one JSON file",
        "  import-data            apply such a file, so this machine runs the same estate",
        "",
        "Each app takes its own arguments; ask it directly, e.g. `db-ops metrics --help`.",
        "Every app is also runnable as `python -m <module>`, which is what the daemon does.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Dispatch to an app, or answer for the toolkit itself.

    A console script is called with no arguments, so ``argv`` defaults to the real ones; the tests
    pass a list instead, which is why it is a parameter at all.

    Routing only. Every app owns its own parser and its own ``main``, and this must stay a lookup
    — the moment it starts interpreting an app's arguments there are two parsers for one command
    and they disagree the first time one of them changes.
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    if argv[0] in ("-V", "--version"):
        from db_ops import __version__

        print(__version__)
        return 0

    # Underscores are what the module names use and what a reader may well type.
    app = argv[0].replace("_", "-")
    if app in APPS:
        import importlib

        # A name this build does not ship gets a sentence, not a traceback. `ModuleNotFoundError:
        # db_ops.sre.cli` tells a reader their install is broken; it is not, and the difference
        # matters because the next thing they do is reinstall.
        #
        # Asked through `installed_apps()` rather than with a bare `find_spec`, which *raises*
        # `ModuleNotFoundError` when the parent package is the missing one — so checking for a
        # missing app with it produced exactly the traceback this branch exists to prevent.
        present = installed_apps()
        if app not in present:
            print(
                f"db-ops: '{app}' is not in this build.\n"
                f"This distribution ships: {', '.join(sorted(present))}.\n"
                "The rest of the toolkit arrives in a later release.",
                file=sys.stderr,
            )
            return 2
        return int(importlib.import_module(present[app]).main(argv[1:]) or 0)

    # `init` is deliberately first among the words: it is the only command that works before a
    # tool root exists, so it must not be reachable only after one does.
    if argv[0] == "init":
        return _init_command(argv[1:])

    if argv[0] in {"encrypt-secret", "encrypt-secret-text"}:
        return _encrypt_secret_command(argv[1:])

    # Next to `init` on purpose: these are the other two commands that are about the tool root
    # rather than about a database, and `import-data` is the one a second machine runs *instead*
    # of editing the twelve files `init` writes.
    if argv[0] in {"export-data", "export-config"}:
        return _export_data_command(argv[1:])

    if argv[0] in {"import-data", "import-config"}:
        return _import_data_command(argv[1:])

    # Subcommand first, flags second. `check-credentials` is a word, and every other form this
    # entry point takes starts with `--`, so the two cannot be confused for one another.
    if argv[0] == "check-credentials":
        return _check_credentials_command(argv[1:])

    if not argv[0].startswith("-"):
        print(f"db-ops: unknown app {argv[0]!r}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2

    args = parse_args(argv)
    logger = None
    try:
        level = normalize_level(args.level)
        config = load_config(args.config)
        patch_stdout(config.log_dir / "db_ops_runtime.log", app_name=config.app_name)
        logger = setup_app_logger(config, enable_telegram_alerts=not (args.export_sqlite_schema or args.recent))
        store = DbOpsStore.from_config(config)
        if args.export_sqlite_schema:
            log_function_call(logger, function_name="db.export_sqlite_schema")
            result = export_sqlite_schema(sqlite_path=config.sqlite_path, output_dir=args.schema_output_dir)
            print(result)
            return 0
        if args.recent:
            log_function_call(logger, function_name="db.fetch_recent_job_runs")
            for row in store.fetch_recent_job_runs(args.recent):
                print(
                    f"{row['log_id']} | {row['created_at']} | {row['job_code']} | "
                    f"{row['level']} | {row['status']} | {row['message']}"
                )
            return 0

        if not args.message:
            raise ValueError("--message is required unless --recent is used.")

        log_function_call(logger, function_name="logging_ops.log_event", text=args.message)
        log_event(logger, level=level, message=args.message)
        if args.job_code:
            log_function_call(logger, function_name="db.insert_job_run", text=args.job_code)
            log_id = store.insert_job_run(
                JobRun(
                    job_code=args.job_code,
                    level=level,
                    status=args.status,
                    message=args.message,
                    duration_ms=args.duration_ms,
                    error_text=args.error_text,
                    host_name=socket.gethostname(),
                )
            )
            print(f"SQLite job_runs inserted: {log_id}")
    except Exception as exc:  # noqa: BLE001 - command-line failure path.
        if logger:
            log_function_error(logger, function_name="db_ops.cli", error_text=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
