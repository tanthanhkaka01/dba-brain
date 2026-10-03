"""The tool root's own commands: ``init``, ``guide``, ``encrypt-secret``, ``export-data``, ``import-data``.

They lived in the root package's ``db_ops/cli.py`` until 0.24.0 - the entry point answering commands
of its own, outside every layer rule (rules R41). The operator: *``db_ops`` is not an app; what it
holds goes into ``lib`` or ``common``.* Each of these is an operation whose subject is the
configuration itself - creating it, printing its guide, encrypting its secret store, carrying it to
another machine - so each is a ``common.cli`` command, and like every one it takes one JSON object
and answers in the envelope (R13, R15). They are the commands R09 allows to read and write
configuration, and each runs on an empty one: ``init`` is how an empty one stops being empty.

``db-ops init`` / ``dbabrain init`` still work as typed: the entry point forwards them here
(``db_ops/cli.py``, an alias table and nothing else), with ``format: "txt"`` for the two a person
types first, so a first run prints what it always printed. An option is a JSON key now -
``db-ops init '{"force": true}'`` - and the old flags are refused by name.

**The passphrase is never in the request** (R14). ``encrypt-secret`` reads ``DB_OPS_SECRET_KEY``, or
``--key`` / ``--key-base64`` after the JSON, the way every ``common`` command that needs it does.
"""

from __future__ import annotations

from db_ops.lib import errors
import json
import os
import sys
from pathlib import Path
from typing import Any

from db_ops.lib import response

INIT_USAGE = """\
Usage: db-ops init ['<json>'|@file|-]          (python -m db_ops.common.cli init ...)

Turn a directory into a working tool root. Writes the smallest tree that runs: config.json, a
SQLite store declaration, an empty inventory, a starter metric catalogue, and the Telegram and
secret files, each carrying notes that say what to put in it. Nothing is overwritten without
"force", because the files this writes are the ones you edit next.

  {"root": ".",            // the directory to initialise; default: the current one
   "app_name": "dbabrain", // default dbabrain
   "force": false,         // overwrite files that already exist
   "format": "json"}       // json (the envelope) | txt (the lines a person reads; `db-ops init`)

The store starts on SQLite in runtime/ - data/store_config.json is where you move to PostgreSQL.
"""

GUIDE_USAGE = """\
Usage: db-ops guide ['<json>'|@file|-]         (python -m db_ops.common.cli guide ...)

The operating guide for this build - the text `init` writes as AGENTS.md.

  {"write": false,   // true = put it here as AGENTS.md (an edited one is saved in
                     //   runtime/agents_guide/ first); false = print it, write nothing
   "root": ".",      // where to write it; default: the current directory
   "format": "json"} // json | txt (the guide itself; `db-ops guide`)
"""

ENCRYPT_SECRET_USAGE = """\
Usage: db-ops encrypt-secret ['<json>'|@file|-] [--key TEXT | --key-base64 B64]

Encrypt secrets/secret_text.json into data/encrypted_secret_text.json, the file the toolkit reads.
Run it again after every edit to the plaintext file - a secret does nothing until it is encrypted.

  {"source": "secrets/secret_text.json",          // default, under the tool root
   "dest": "data/encrypted_secret_text.json"}     // default, under the tool root

The passphrase is DB_OPS_SECRET_KEY, or --key / --key-base64 after the JSON - never a request field.
Keep it: nothing else decrypts the store, and there is no recovery.
"""

EXPORT_DATA_USAGE = """\
Usage: db-ops export-data '<json>'|@file|-     (python -m db_ops.common.cli export-data ...)

Write this machine's whole configuration to one JSON file, so another machine can run the same
estate after nothing more than `pip install dbabrain` and `db-ops import-data`.

  {"bundle": "<name>-bundle.json",  // required: the file to write
   "root": ".",                     // the tool root to export; default: the one this process resolved
   "include_secrets": true,         // false = leave data/encrypted_secret_text.json out
   "include_assets": true,          // false = leave assets/ and data/ssh_keys/ out
   "force": false}                  // overwrite an existing bundle file

THE BUNDLE IS A CREDENTIAL. It names hosts, accounts and chat ids and carries the secret store as
ciphertext. Never commit it - .gitignore covers `<name>-bundle.json`. The passphrase is not in it.
"""

IMPORT_DATA_USAGE = """\
Usage: db-ops import-data '<json>'|@file|-     (python -m db_ops.common.cli import-data ...)

Apply a bundle written by `db-ops export-data`, so this machine runs the estate it came from.

  {"bundle": "<name>-bundle.json",  // required: the bundle to read
   "root": ".",                     // the tool root to write into; default: the one resolved
   "plan_only": false,              // true = report what would be written, change nothing
   "force": false,                  // replace files that exist here with different content
   "include_secrets": true,         // false = do not write the secret store
   "include_assets": true}          // false = do not write assets/ or data/ssh_keys/

Every entry's checksum is verified before anything is written, so a truncated or edited bundle
leaves the tree untouched. The passphrase does not travel: set DB_OPS_SECRET_KEY afterwards.
"""

#: The naming convention `.gitignore` covers. Suggested rather than enforced: the operator may
#: legitimately write a bundle to a path outside any repository.
BUNDLE_NAME_HINT = "<name>-bundle.json"

#: The flags these commands took until 0.24.0, and the JSON key each became - refused by name, so
#: muscle memory gets a sentence rather than a JSON parse error.
_OLD_FLAGS = {"--force": "force", "--app-name": "app_name", "--write": "write", "--root": "root",
              "--no-secrets": "include_secrets", "--no-assets": "include_assets",
              "--plan": "plan_only", "--plan-only": "plan_only", "--dry-run": "plan_only",
              "--source": "source", "--dest": "dest"}


class ToolRootError(errors.OperationFailed):
    """A tool-root command refused: the reason is the operator's to act on."""


def _flag(request: dict[str, Any], name: str, default: bool) -> bool:
    value = request.get(name, default)
    if isinstance(value, bool):
        return value
    raise ToolRootError(f"{name} must be true or false, got {value!r}.")


def _looks_like_an_ignored_bundle_name(name: str) -> bool:
    return (name.endswith("-bundle.json")
            or name.endswith(".dbabrain-bundle.json")
            or name.startswith("dbabrain-export"))


def _config_upgrade_hint(data_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Whether the kept configuration is in shapes this version has moved - said, never applied.

    After an upgrade, `init` over an existing root (or an import) is the moment the operator is
    looking. Every reader takes both shapes, so nothing breaks - which is exactly why nobody would
    notice that the files never moved. Moving an estate's files is the operator's call.
    """
    from db_ops.common import config_upgrade

    try:
        _, plan = config_upgrade.upgrade({"data_dir": str(data_dir)})
    except Exception:  # noqa: BLE001 - a hint must never be the reason a command fails.
        return {}, []
    if not plan["records_changed"]:
        return {}, []
    return ({"records_changed": plan["records_changed"],
             "plan": 'python -m db_ops.common.cli upgrade-config "{}"',
             "apply": 'python -m db_ops.common.cli upgrade-config "{\\"dry_run\\": false}"'},
            ["", f"This root's configuration was written by an older version: {plan['records_changed']} "
                 "record(s) use shapes this version has moved.",
             "  See the plan:   python -m db_ops.common.cli upgrade-config \"{}\"",
             "  Apply it:       python -m db_ops.common.cli upgrade-config "
             "\"{\\\"dry_run\\\": false}\"   (each file is copied to runtime/config_upgrade/ first)"])


def _node_role_hint(root: Path) -> tuple[str, list[str]]:
    """The ``node_role`` an imported schedule needs, if it is not the one a process gets by default.

    An estate exported from a worker declares ``worker`` on every command, and a process not told
    otherwise is ``master`` - so the daemon on the new machine starts and runs nothing, correctly,
    for a reason only a log line gives. The import says it while the operator is still typing.
    """
    try:
        document = json.loads((root / "data" / "app_commands.json").read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "", []
    entries = document.get("app_commands") if isinstance(document, dict) else document
    if not isinstance(entries, list):
        return "", []
    roles = {
        str(role).strip().lower()
        for entry in entries
        if isinstance(entry, dict) and entry.get("active", True)
        for role in ([entry.get("node_role")] if isinstance(entry.get("node_role"), str)
                     else entry.get("node_role") or [])
        if str(role).strip()
    }
    if not roles or "master" in roles or "all" in roles:
        return "", []
    role = sorted(roles)[0]
    return role, ["", f"Every scheduled command here is for node_role {sorted(roles)}, and a process "
                      "that is", "not told otherwise is 'master' - so the daemon would start and run "
                      "nothing. Either:", f"  export DB_OPS_NODE_ROLE={role}",
                  "or set node_role to 'all' in data/app_commands.json."]


# --------------------------------------------------------------------------- #
# The five commands: request in, (message, data, lines for a person) out
# --------------------------------------------------------------------------- #
def _init(request: dict[str, Any], _key: str) -> tuple[str, dict[str, Any], list[str]]:
    """``init`` - the first command anybody runs, and until 2026-08-22 it did not exist.

    Measured on a real `pip install` into a clean virtualenv: from an empty directory the toolkit
    resolved its configuration to `site-packages` and told the reader to create a file there from
    an example that does not ship. Installable and unstartable.
    """
    from db_ops.common import scaffold

    try:
        result = scaffold.initialise(Path(str(request.get("root") or ".")),
                                     app_name=str(request.get("app_name") or "dbabrain"),
                                     force=_flag(request, "force", False))
    except scaffold.ScaffoldError as exc:
        raise ToolRootError(f"init refused: {exc}") from exc
    lines = [f"  created  {name}" for name in result.written]
    lines += [f"  kept     {name} (already there; \"force\": true overwrites)" for name in result.skipped]
    if result.guide_saved_copy:
        lines.append(f"  saved    the edited AGENTS.md as {result.guide_saved_copy} - AGENTS.md is now "
                     "this version's guide")
    steps = scaffold.next_steps(result.root)
    lines += ["", steps]
    upgrade, upgrade_lines = _config_upgrade_hint(Path(result.root) / "data")
    lines += upgrade_lines
    data = {"root": str(result.root), "written": list(result.written), "kept": list(result.skipped),
            "guide_saved_copy": str(result.guide_saved_copy or ""), "next_steps": steps,
            "config_upgrade": upgrade}
    return (f"{len(result.written)} file(s) created, {len(result.skipped)} kept in {result.root}",
            data, lines)


def _guide(request: dict[str, Any], _key: str) -> tuple[str, dict[str, Any], list[str]]:
    """``guide`` - the getting-started document, printed, or written as AGENTS.md.

    It exists as a command because until `init` has run there is no file to read, and a reader who
    has just installed the package should not have to create a directory tree to find out.
    """
    from db_ops.common import scaffold

    if not _flag(request, "write", False):
        return "the operating guide for this build", {"guide": scaffold.AGENTS_GUIDE}, [scaffold.AGENTS_GUIDE]
    root = Path(str(request.get("root") or "."))
    outcome, saved = scaffold.write_guide(root)
    target = root / "AGENTS.md"
    message = {
        "written": f"wrote {target}",
        "unchanged": f"{target} is already this build's guide",
        "replaced": f"wrote {target}; the edited copy it replaced is saved as {saved}",
    }[outcome]
    return message, {"outcome": outcome, "path": str(target), "saved_copy": str(saved or "")}, [message]


def _encrypt_secret(request: dict[str, Any], key: str) -> tuple[str, dict[str, Any], list[str]]:
    """``encrypt-secret`` - turn the plaintext secret source into the store the toolkit reads."""
    from db_ops.lib.paths import TOOL_ROOT
    from db_ops.lib.secret_text import encrypt_secret_text_file

    if "key" in request or "key_base64" in request or "passphrase" in request:
        raise ToolRootError("the passphrase is never a request field (rules R14): set DB_OPS_SECRET_KEY, "
                            "or pass --key / --key-base64 after the JSON.")
    source = Path(str(request.get("source") or Path(TOOL_ROOT) / "secrets" / "secret_text.json"))
    dest = Path(str(request.get("dest") or Path(TOOL_ROOT) / "data" / "encrypted_secret_text.json"))
    resolved = key or os.environ.get("DB_OPS_SECRET_KEY", "").strip()
    if not resolved:
        # Refuse rather than guess. Encrypting with the wrong passphrase produces a store that
        # decrypts nowhere, and the round trip inside encrypt_secret_text_file cannot catch it,
        # because it verifies against the same key it just used.
        raise ToolRootError("no passphrase. Set DB_OPS_SECRET_KEY, or pass --key-base64 / --key.")
    if not source.exists():
        raise ToolRootError(f"{source} not found. `db-ops init` creates it.")
    count = encrypt_secret_text_file(source, dest, resolved)
    message = f"Encrypted {count} secret(s) -> {dest}"
    return message, {"count": count, "source": str(source), "dest": str(dest)}, [message]


def _export_data(request: dict[str, Any], _key: str) -> tuple[str, dict[str, Any], list[str]]:
    """``export-data`` - one estate, one file. The file list comes from ``data/config_catalog.json``
    (:mod:`db_ops.lib.config_bundle`), not from here: a second list of "which files are configuration"
    disagrees with the first the week after somebody adds a config file."""
    from db_ops.lib import config_bundle
    from db_ops.lib.paths import TOOL_ROOT
    from db_ops.lib.version import __version__

    target = str(request.get("bundle") or "").strip()
    if not target:
        raise ToolRootError('export-data needs "bundle": the file to write.')
    destination = Path(target).expanduser()
    if destination.exists() and not _flag(request, "force", False):
        raise ToolRootError(f'{destination} already exists. Pass "force": true to replace it.')
    try:
        bundle = config_bundle.build_bundle(
            Path(str(request.get("root") or TOOL_ROOT)),
            include_secrets=_flag(request, "include_secrets", True),
            include_assets=_flag(request, "include_assets", True),
            tool_version=__version__,
        )
    except config_bundle.BundleError as exc:
        raise ToolRootError(f"export-data refused: {exc}") from exc
    text = config_bundle.bundle_text(bundle)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")

    counts: dict[str, int] = {}
    for entry in bundle["files"].values():
        counts[entry["role"]] = counts.get(entry["role"], 0) + 1
    missing = list(bundle.get("missing_at_source") or [])
    warnings = []
    if not _looks_like_an_ignored_bundle_name(destination.name):
        warnings.append(f"{destination.name} is not a name .gitignore covers. This file is an entire "
                        f"estate; name it {BUNDLE_NAME_HINT} or keep it outside any repository.")
    lines = [f"wrote {destination} ({len(text.encode('utf-8')) / 1024:.0f} KB)"]
    lines += [f"  {counts[role]:>4}  {role}" for role in sorted(counts)]
    if missing:
        # Named, not silent: the files the *other* machine will not get.
        lines.append(f"  not present here, so not carried ({len(missing)}):")
        lines += [f"      {name}" for name in missing]
    lines.append("  secret store carried as ciphertext; the passphrase is not in this file."
                 if bundle.get("includes_secret_store")
                 else "  no secret store in this bundle - the importing machine supplies credentials.")
    lines += [f"  WARNING: {warning}" for warning in warnings]
    data = {"bundle": str(destination), "files_by_role": counts, "missing_at_source": missing,
            "includes_secret_store": bool(bundle.get("includes_secret_store")), "warnings": warnings}
    return f"wrote {destination}: {sum(counts.values())} file(s)", data, lines


def _import_data(request: dict[str, Any], _key: str) -> tuple[str, dict[str, Any], list[str]]:
    """``import-data`` - refuses by default to replace a file that exists with different content:
    the machine being imported into may already be somebody's working install, and an import that
    silently replaced ``db_instances.json`` there would destroy the only copy of it."""
    from db_ops.lib import config_bundle
    from db_ops.lib.paths import TOOL_ROOT, is_installed_package_root

    source = str(request.get("bundle") or "").strip()
    if not source:
        raise ToolRootError('import-data needs "bundle": the file to read.')
    path = Path(source).expanduser()
    if not path.is_file():
        raise ToolRootError(f"no such file: {path}")
    try:
        document = json.loads(path.read_bytes().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolRootError(f"{path} is not readable as JSON: {exc}") from exc
    include_secrets = _flag(request, "include_secrets", True)
    try:
        entries = config_bundle.select_entries(config_bundle.read_bundle(document),
                                               include_secrets=include_secrets,
                                               include_assets=_flag(request, "include_assets", True))
    except config_bundle.BundleError as exc:
        raise ToolRootError(f"import-data refused: {exc}") from exc
    if not entries:
        raise ToolRootError("the options given exclude every file in this bundle.")

    root = Path(str(request.get("root") or TOOL_ROOT)).expanduser()
    # The fallback tool root is the package's own location, which for a pip install is
    # site-packages: an estate unpacked there is a config nothing will look for, under a message
    # that reads like success. Refuse, and name both ways out.
    if not request.get("root") and is_installed_package_root(root):
        raise ToolRootError(
            f"the tool root resolved to {root.resolve()}, which is where pip installed the package - "
            "not an estate. That happens when the directory you are standing in carries no "
            f'configuration the tool recognises. State the root: {{"bundle": "{source}", "root": "."}}, '
            "or create one here first with `db-ops init`.")
    root.mkdir(parents=True, exist_ok=True)
    planned = config_bundle.plan_import(entries, root)
    header = [f"bundle: {path}", f"exported {document.get('exported_at', '?')} by version "
              f"{document.get('exported_by_version') or '?'}", f"target tool root: {root.resolve()}"]
    if _flag(request, "plan_only", False):
        counts: dict[str, int] = {}
        for item in planned:
            counts[item.action] = counts.get(item.action, 0) + 1
        lines = header + [f"  {item.action:<9}  {item.path}" for item in planned]
        lines.append("  " + ", ".join(f"{counts[name]} {name}" for name in sorted(counts)))
        return ("plan only - nothing written: " + ", ".join(f"{counts[n]} {n}" for n in sorted(counts)),
                {"plan_only": True, "root": str(root.resolve()),
                 "plan": [{"action": item.action, "path": str(item.path)} for item in planned],
                 "counts": counts}, lines)

    try:
        result = config_bundle.apply_bundle(entries, root, force=_flag(request, "force", False))
    except config_bundle.BundleError as exc:
        raise ToolRootError(f"import-data refused: {exc}") from exc
    summary = (f"{len(result.created)} created, {len(result.overwritten)} replaced, "
               f"{len(result.unchanged)} already identical")
    lines = [f"imported into {root.resolve()}", f"  {summary}"]
    missing = list(document.get("missing_at_source") or [])
    if missing:
        lines.append(f"  absent on the source machine, so not in this bundle ({len(missing)}):")
        lines += [f"      {name}" for name in missing]
    carries_store = bool(document.get("includes_secret_store") and include_secrets)
    if carries_store:
        lines += ["", "The secret store is here as ciphertext and the passphrase is not. Set",
                  "DB_OPS_SECRET_KEY to the source machine's passphrase, then verify with:",
                  "  db-ops check-credentials"]
    role, role_lines = _node_role_hint(root)
    lines += role_lines
    upgrade, upgrade_lines = _config_upgrade_hint(root / "data")
    lines += upgrade_lines
    data = {"plan_only": False, "root": str(root.resolve()), "created": list(map(str, result.created)),
            "replaced": list(map(str, result.overwritten)), "unchanged": list(map(str, result.unchanged)),
            "missing_at_source": missing, "secret_store_imported": carries_store,
            "node_role_needed": role, "config_upgrade": upgrade}
    return f"imported into {root.resolve()}: {summary}", data, lines


_COMMANDS = {
    "init": (_init, INIT_USAGE),
    "guide": (_guide, GUIDE_USAGE),
    "encrypt-secret": (_encrypt_secret, ENCRYPT_SECRET_USAGE),
    "export-data": (_export_data, EXPORT_DATA_USAGE),
    "import-data": (_import_data, IMPORT_DATA_USAGE),
}
COMMANDS = tuple(_COMMANDS)


def run(operation: str, argv: list[str], *, read_request: Any, read_key_flags: Any) -> int:
    action, usage = _COMMANDS[operation]
    if argv and argv[0] in {"-h", "--help"}:
        print(usage)
        return 0
    old = [token for token in argv if token in _OLD_FLAGS]
    if old:
        keys = ", ".join(f'{flag} -> "{_OLD_FLAGS[flag]}"' for flag in old)
        return response.emit(response.fail(
            operation, f"{operation} takes one JSON object since 0.24.0 - the flags became keys: {keys}. "
                       f"For example: db-ops {operation} " + "'{\"force\": true}'."))
    request: dict[str, Any] = {}
    rest = argv
    if argv and not argv[0].startswith("--"):
        request, code = read_request(argv[0], usage)
        if request is None:
            return code
        rest = argv[1:]
    key = key_base64 = None
    if operation == "encrypt-secret":
        key, key_base64, code = read_key_flags(rest, usage, operation)
        if code:
            return code
    elif rest:
        return response.emit(response.fail(
            operation, f"{operation} takes one JSON object; got {len(argv)} arguments."))
    text = str(request.get("format") or "json").strip().lower() == "txt"
    try:
        from db_ops.lib.secret_text import resolve_cli_key

        resolved_key = resolve_cli_key(key, key_base64) or "" if (key or key_base64) else ""
        message, data, lines = action(request, resolved_key)
    except (ToolRootError, OSError, ValueError) as exc:
        if text:
            print(f"{operation}: {exc}", file=sys.stderr)
            return 1
        return response.emit(response.fail(operation, str(exc)))
    if text:
        print("\n".join(lines))
        return 0
    return response.emit(response.ok(operation, message=message, data=data))
