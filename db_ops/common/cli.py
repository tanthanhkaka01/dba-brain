"""CLI for the shared ``common`` layer: config-write admin + shared lookups.

Every db_ops app has a CLI entrypoint; this is the one for the shared layer. It is a
thin facade — all logic stays in the common modules it fronts:

* ``add-sql`` / ``metric-toggle``  -> :mod:`db_ops.common.config_admin` (atomic config writes)
* ``list-targets``                 -> :mod:`db_ops.lib.data_sources` (the same listing
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

    python -m db_ops.common.cli add-sql '{"db_type": "sqlserver", "server_id": "...", "display_name": "...", "sql_file": "..."}'
    python -m db_ops.common.cli metric-toggle '{"server_id": "...", "state": "off", "scope": "collector:cmd"}'
    python -m db_ops.common.cli list-targets
    python -m db_ops.common.cli run-sql @request.json    # {"connection": {...}, "sql_text": "SELECT 1 AS x"}
"""

from __future__ import annotations
import json
import sys
from pathlib import Path
from typing import Any
from db_ops.common import config_admin
from db_ops.common.cli_catalog import STATED_CONNECTION
from db_ops.lib.json_io import looks_like_json_request
from db_ops.common.cli_usage import (  # noqa: F401 - re-exported: every name kept its address
    ASK_USAGE,
    AUTHORIZE_USAGE,
    CHECK_IDENTIFIERS_USAGE,
    CHECK_SECRET_LITERALS_USAGE,
    CHECK_SECRET_USAGE,
    DB_STATUS_USAGE,
    EMERGENCY_USAGE,
    FILE_TRANSFER_USAGE,
    HOST_FACTS_USAGE,
    HOST_RESTART_USAGE,
    HOST_SERVICE_USAGE,
    INVENTORY_SUMMARY_USAGE,
    LIFT_EXAMPLE_USAGE,
    LIST_TARGETS_USAGE,
    METRIC_SEVERITY_USAGE,
    PROBE_HOST_USAGE,
    ROTATE_PASSWORD_USAGE,
    RUN_CMD_USAGE,
    RUN_SQL_USAGE,
    SECRET_SET_USAGE,
    SELF_STATUS_USAGE,
    SQLSERVER_INSTANCE_USAGE,
    SQLSERVER_PATCH_USAGE,
    STATED_ACCESS,
    STATED_POLICY,
    STATED_RULES,
    TRACE_SESSION_USAGE,
    USAGE,
    USAGE_APP_COMMAND_SET,
)
from db_ops.common.cli_request import COMMAND
from db_ops.common.cli_request import (  # noqa: F401 - re-exported: every name kept its address
    _optional_json_request,
    _read_json_request,
    _read_key_flags,
    _unreadable_config,
)
from db_ops.common.cli_sql import (  # noqa: F401 - re-exported: every name kept its address
    _db_status_command,
    _run_sql_command,
    _trace_session_command,
)
from db_ops.common.cli_host import (  # noqa: F401 - re-exported: every name kept its address
    _file_transfer_command,
    _probe_host_command,
    _run_cmd_command,
)
from db_ops.common.cli_gate import (  # noqa: F401 - re-exported: every name kept its address
    _ask_command,
    _gate_command,
)


def _secret_set_command(argv: list[str]) -> int:
    """``secret-set`` — one secret into the encrypted store, with no plaintext file on the way.

    Missing until 2026-09-11. Moving one bot token onto a new node meant writing it into a
    plaintext source, running `encrypt-secret`, and deleting the file — while
    `lib.secret_text.set_secret_text`, which does exactly the single-entry write, already existed
    and `instance-add` was already using it for database passwords.
    """
    import os
    from pathlib import Path

    from db_ops.lib import response
    from db_ops.lib import secret_text as _secret_text
    from db_ops.lib.paths import DEFAULT_DATA_DIR, TOOL_ROOT

    if argv and argv[0] in {"-h", "--help"}:
        print(SECRET_SET_USAGE)
        return 0
    if not argv or argv[0] != "-":
        print(SECRET_SET_USAGE, file=sys.stderr)
        return response.emit(response.fail(
            "secret-set", "the request must arrive on stdin (-): a secret given inline is visible "
                          "on the command line, and one in a file is the plaintext this avoids"))
    request, code = _read_json_request("-", SECRET_SET_USAGE)
    if request is None:
        return code
    ref = str(request.get("ref") or "").strip()
    value = str(request.get("value") or "")
    if not ref or not value:
        return response.emit(response.fail("secret-set", "both 'ref' and 'value' are required"))

    source = Path(TOOL_ROOT) / "secrets" / "secret_text.json"
    key = os.environ.get("DB_OPS_SECRET_KEY") or None
    try:
        if request.get("also_plaintext"):
            written = _secret_text.set_secret_everywhere(
                DEFAULT_DATA_DIR, ref, value, key=key, plaintext_store=source,
                overwrite=bool(request.get("overwrite")))
        else:
            written = _secret_text.set_secret_text(
                DEFAULT_DATA_DIR, ref, value, key=key, overwrite=bool(request.get("overwrite")))
    except Exception as exc:  # noqa: BLE001 - reported as a response like every other command.
        return response.emit(response.fail("secret-set", str(exc).replace(value, "<value>")))

    plaintext: dict = {"path": str(source), "exists": source.exists()}
    if source.exists():
        try:
            refs = {name for name in json.loads(source.read_text(encoding="utf-8-sig"))
                    if not str(name).startswith("_")}
        except (ValueError, AttributeError):
            refs = set()
        plaintext["holds_ref"] = ref in refs
        if ref not in refs:
            plaintext["warning"] = (
                f"{source} exists and does not hold {ref}. encrypt-secret REPLACES the store with "
                "that file, so running it would drop this secret"
                + (" - and every other secret, since the file holds none." if not refs else ".")
                + " Keep adding secrets with secret-set, or add this ref there before encrypt-secret.")
    return response.emit(response.ok(
        "secret-set",
        message=(f"stored {ref} in the encrypted store" if written
                 else f"{ref} already holds that value; nothing written"),
        data={"ref": ref, "written": bool(written),
              "store": str(Path(DEFAULT_DATA_DIR) / "encrypted_secret_text.json"),
              "plaintext_source": plaintext}))


def _instance_add_command(argv: list[str]) -> int:
    """``instance-add`` — the CLI face of :mod:`db_ops.common.instance_admin`.

    Its own usage line has advertised ``--key-base64`` since the day it was written and this
    function read only ``DB_OPS_SECRET_KEY``, so the flag was accepted and silently ignored: an
    operator who passed it got "a password was given but no passphrase is available to encrypt
    it" while looking straight at the passphrase they had typed. It is parsed now, the same way
    ``run-sql`` and every other key-taking command here parses it.
    """
    import os

    from db_ops.common import instance_admin
    from db_ops.lib import response
    from db_ops.lib.secret_text import set_key_env

    if argv and argv[0] in {"-h", "--help"}:
        print(instance_admin.USAGE)
        return 0
    if not argv:
        print(instance_admin.USAGE, file=sys.stderr)
        return response.emit(response.fail("instance-add", "no request given; see --help"))
    key, key_base64, code = _read_key_flags(argv[1:], instance_admin.USAGE, "instance-add")
    if code:
        return code
    try:
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    request, code = _read_json_request(argv[0], instance_admin.USAGE)
    if request is None:
        return code
    try:
        outcome = instance_admin.add_instance(
            request, key=os.environ.get("DB_OPS_SECRET_KEY") or None)
    except instance_admin.InstanceAdminError as exc:
        return response.emit(response.fail("instance-add", str(exc)))
    if isinstance(request, dict) and request.get("keep_default"):
        # Nothing was registered: a login was filed beside the default, and `replaced` is that login.
        verb = "replaced" if outcome["replaced"] else "added"
        what = (f"login {outcome['credential_name']} on {outcome['server_id']}, beside its "
                "default")
    else:
        verb = "replaced" if outcome["replaced"] else "registered"
        what = f"{outcome['server_id']} ({outcome['db_type']})"
    return response.emit(response.ok(
        "instance-add",
        message=f"{verb} {what} - wrote {', '.join(outcome['files_written'])}",
        data=outcome))


def _app_command_set_command(argv: list[str]) -> int:
    """``app-command-set`` — the CLI face of :mod:`db_ops.common.app_command_admin`."""
    from db_ops.common import app_command_admin
    from db_ops.lib import response

    if argv and argv[0] in {"-h", "--help"}:
        print(USAGE_APP_COMMAND_SET)
        return 0
    if not argv:
        print(USAGE_APP_COMMAND_SET, file=sys.stderr)
        return response.emit(response.fail("app-command-set", "no request given; see --help"))
    request, code = _read_json_request(argv[0], USAGE_APP_COMMAND_SET)
    if request is None:
        return code
    try:
        outcome = app_command_admin.set_app_command(request)
    except app_command_admin.AppCommandAdminError as exc:
        return response.emit(response.fail("app-command-set", str(exc)))
    return response.emit(response.ok(
        "app-command-set", message=outcome["message"], data=outcome))


def _sql_command_add_command(argv: list[str]) -> int:
    """``sql-command-add`` — the CLI face of :mod:`db_ops.common.sql_task_admin`."""
    from db_ops.common import sql_task_admin
    from db_ops.lib import response

    if argv and argv[0] in {"-h", "--help"}:
        print(sql_task_admin.USAGE_COMMAND)
        return 0
    if not argv:
        print(sql_task_admin.USAGE_COMMAND, file=sys.stderr)
        return response.emit(response.fail("sql-command-add", "no request given; see --help"))
    request, code = _read_json_request(argv[0], sql_task_admin.USAGE_COMMAND)
    if request is None:
        return code
    try:
        outcome = sql_task_admin.add_sql_command(request)
    except sql_task_admin.SqlTaskAdminError as exc:
        return response.emit(response.fail("sql-command-add", str(exc)))
    verb = "replaced" if outcome["replaced"] else "registered"
    return response.emit(response.ok(
        "sql-command-add",
        message=f"{verb} sql_id {outcome['sql_id']} ({outcome['sql_code']}) - wrote "
                f"{', '.join(outcome['files_written'])}",
        data=outcome))


def _sql_target_add_command(argv: list[str]) -> int:
    """``sql-target-add`` — the CLI face of :mod:`db_ops.common.sql_task_admin`."""
    from db_ops.common import sql_task_admin
    from db_ops.lib import response

    if argv and argv[0] in {"-h", "--help"}:
        print(sql_task_admin.USAGE_TARGET)
        return 0
    if not argv:
        print(sql_task_admin.USAGE_TARGET, file=sys.stderr)
        return response.emit(response.fail("sql-target-add", "no request given; see --help"))
    request, code = _read_json_request(argv[0], sql_task_admin.USAGE_TARGET)
    if request is None:
        return code
    try:
        outcome = sql_task_admin.add_sql_target(request)
    except sql_task_admin.SqlTaskAdminError as exc:
        return response.emit(response.fail("sql-target-add", str(exc)))
    verb = "replaced" if outcome["replaced"] else "registered"
    return response.emit(response.ok(
        "sql-target-add",
        message=f"{verb} target {outcome['target_no']} of sql_id {outcome['sql_id']} on "
                f"{outcome['server_id']} - wrote {', '.join(outcome['files_written'])}",
        data=outcome))


def _remote_credential_add_command(argv: list[str]) -> int:
    """``remote-credential-add`` — the CLI face of :mod:`db_ops.common.remote_credential_admin`."""
    import os

    from db_ops.common import remote_credential_admin
    from db_ops.lib import response
    from db_ops.lib.secret_text import set_key_env

    usage = remote_credential_admin.USAGE
    if argv and argv[0] in {"-h", "--help"}:
        print(usage)
        return 0
    if not argv:
        print(usage, file=sys.stderr)
        return response.emit(response.fail("remote-credential-add", "no request given; see --help"))
    key, key_base64, code = _read_key_flags(argv[1:], usage, "remote-credential-add")
    if code:
        return code
    try:
        set_key_env(key, key_base64)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    request, code = _read_json_request(argv[0], usage)
    if request is None:
        return code
    try:
        outcome = remote_credential_admin.add_remote_credential(
            request, key=os.environ.get("DB_OPS_SECRET_KEY") or None)
    except remote_credential_admin.InstanceAdminError as exc:
        return response.emit(response.fail("remote-credential-add", str(exc)))
    verb = "replaced" if outcome["replaced"] else "registered"
    tail = (f"and wired cmd_access ({outcome['method']}) on {outcome['server_id']}"
            if outcome["cmd_access_written"]
            else "- no cmd_access block written, so nothing reaches the host yet")
    return response.emit(response.ok(
        "remote-credential-add",
        message=f"{verb} {outcome['credential_name']} for {outcome['host']} {tail} - wrote "
                f"{', '.join(outcome['files_written'])}",
        data=outcome))


def _build_showcase_command(argv: list[str]) -> int:
    """``build-showcase`` — the CLI face of :mod:`db_ops.common.showcase`.

    The help text lives in that module beside the behaviour it describes.
    """
    from db_ops.common import identifier_scan, showcase
    from db_ops.lib import response

    if argv and argv[0] in {"-h", "--help"}:
        print(showcase.USAGE)
        return 0
    if not argv:
        print(showcase.USAGE, file=sys.stderr)
        return response.emit(response.fail("build-showcase", "no request given; see --help"))
    request, code = _read_json_request(argv[0], showcase.USAGE)
    if request is None:
        return code
    try:
        outcome = showcase.build(request)
    except (showcase.ShowcaseError, identifier_scan.IdentifierScanError) as exc:
        return response.emit(response.fail("build-showcase", str(exc)))
    return response.emit(response.ok(
        "build-showcase",
        message=(f"{outcome['pages']} page(s) rewritten with {outcome['terms']} term(s) "
                 f"into {outcome['output']}"),
        data=outcome,
        metrics={"pages": outcome["pages"], "terms": outcome["terms"]}))

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
    dest = str(request.get("destination") or request.get("dest") or default_dest)

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

# The standard notify levels. Routing itself moved to the Telegram app
# (`db_ops.telegram.cli route|groups`) — `common` reads no Telegram settings; this list is only
# the vocabulary other commands here validate against.
_TELEGRAM_LEVELS = ("logging", "warning", "critical", "error", "test", "private")


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



def _config_data_dir(config_path: str, *, named: bool) -> Path | None:
    """The data dir a secret-touching command reads: the named config's own, or this node's.

    These four looked for ``data_dir`` on the loaded config - a field it does not have - and fell
    back to the process's default data dir when the config could not be read: ``rotate-password
    --config D:/other/config.json`` read and rewrote the secret store of whatever root the process
    stood in (owner decision G3.1). A named config must load, and its own ``data/`` is used; with
    none named, ``None``: the node's own data dir, as ``data_sources`` resolves it for every other
    config-file command (``DB_OPS_HOME`` / ``DB_OPS_DATA_DIR``).
    """
    from db_ops.lib.config import load_config
    from db_ops.lib.paths import resolve_data_dir

    if not named:
        return None
    path = Path(config_path).expanduser()
    load_config(path)
    return resolve_data_dir(tool_root=path.resolve().parent)


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
    config_named = False
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(ROTATE_PASSWORD_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
            config_named = True
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


    try:
        data_dir = _config_data_dir(config_path, named=config_named)
    except Exception as exc:  # noqa: BLE001 - every unreadable config is the same refusal.
        from db_ops.lib import response as _response

        return _response.emit(_response.fail("rotate-password", _unreadable_config(config_path, exc)))

    try:
        # Each new password is stored the moment it is verified, before the next ref is touched,
        # and a store failure rolls that change back. Storing them all after every change had run
        # lost every password after the first store error (review 0.25.0, F11.1).
        persist = None
        if not request.get("dry_run"):
            plaintext = request.get("plaintext_store", "secrets/secret_text.json")
            persist = password_rotation.store_writer(data_dir=data_dir,
                                                     plaintext_store=plaintext or None)
        outcome = password_rotation.rotate(request, data_dir=data_dir, persist=persist)
        if persist is not None:
            outcome["stored"] = sum(1 for item in outcome.get("results", []) if item.get("stored"))
            stranded = [item["password_ref"] for item in outcome.get("results", [])
                        if item.get("_new_password")]
            if stranded:
                # Store write AND rollback failed: the database holds a password nobody has.
                outcome["stranded"] = stranded
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
    if outcome.get("stranded"):
        reason = ("LOCKED OUT: the new password of " + ", ".join(outcome["stranded"])
                  + " could not be stored and the rollback failed - reset it on the server by hand. "
                  + reason)
    return response.emit(response.fail(
        "rotate-password", reason, message=message + ".", data=safe,
        metrics={"rotated": len(rotated), "failed": len(failed)}))


def _check_secret_literals_command(argv: list[str]) -> int:
    """``check-secret-literals`` — is a value from the store sitting in a file that ships?

    The one scan here that *must* open the secret store, which is why it is its own command:
    ``check-identifiers`` promises to open nothing, and that promise is worth keeping.
    """
    from db_ops.common import secret_literals
    from db_ops.lib import response
    from db_ops.lib.secret_text import resolve_cli_key

    source = ""
    config_path = "config.json"
    config_named = False
    key = None
    key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(CHECK_SECRET_LITERALS_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
            config_named = True
        elif token == "--key":
            key = rest.pop(0) if rest else None
        elif token in {"--key-base64", "--key_base64"}:
            key_base64 = rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}", file=sys.stderr)
            print(CHECK_SECRET_LITERALS_USAGE, file=sys.stderr)
            return 2

    request, code = _read_json_request(source or "{}", CHECK_SECRET_LITERALS_USAGE)
    if request is None:
        return code

    from db_ops.common import secret_literals as _sl  # noqa: F401 - imported above, kept explicit

    try:
        data_dir = _config_data_dir(config_path, named=config_named)
    except Exception as exc:  # noqa: BLE001 - every unreadable config is the same refusal.
        from db_ops.lib import response as _response

        return _response.emit(_response.fail("check-secret-literals", _unreadable_config(config_path, exc)))

    try:
        resolved = resolve_cli_key(key, key_base64)
    except ValueError as exc:
        return response.emit(response.fail("check-secret-literals", str(exc)))

    try:
        outcome = secret_literals.scan(request, data_dir=data_dir, key=resolved)
    except Exception as exc:  # noqa: BLE001 - report as a response like every other command.
        return response.emit(response.fail("check-secret-literals", str(exc)))

    # `success` means the scan ran. A finding is a fact about the tree - the same distinction
    # `check-identifiers` and `check-secret` make. Callers gate on `data.hits`.
    print(secret_literals.format_report(outcome), file=sys.stderr)
    hits = int(outcome["hits"])
    return response.emit(response.ok(
        "check-secret-literals",
        message=(f"{hits} shipped file(s) hold a stored secret value; searched "
                 f"{outcome['secrets_searched']} value(s) across "
                 f"{outcome['files_scanned']} file(s)."),
        data=outcome,
        metrics={
            "hits": hits,
            "files_scanned": int(outcome["files_scanned"]),
            "secrets_searched": int(outcome["secrets_searched"]),
        },
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
    config_named = False
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(CHECK_IDENTIFIERS_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
            config_named = True
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{CHECK_IDENTIFIERS_USAGE}", file=sys.stderr)
            return 2

    request, code = _read_json_request(source or "{}", CHECK_IDENTIFIERS_USAGE)
    if request is None:
        return code

    from db_ops.lib import response

    try:
        data_dir = _config_data_dir(config_path, named=config_named)
    except Exception as exc:  # noqa: BLE001 - every unreadable config is the same refusal.
        from db_ops.lib import response as _response

        return _response.emit(_response.fail("check-identifiers", _unreadable_config(config_path, exc)))

    try:
        outcome = identifier_scan.scan(request, data_dir=data_dir)
    except identifier_scan.IdentifierScanError as exc:
        # Refused, not broken: nothing to search for, or nothing to read - a scan that would report
        # every tree clean. The export tells the two apart by `refused` (it says SKIPPED for this
        # one, and stops for anything else), which a message's wording cannot carry.
        return response.emit(response.fail("check-identifiers", str(exc), data={"refused": True}))
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
    config_named = False
    key = key_base64 = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(CHECK_SECRET_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else config_path
            config_named = True
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


    try:
        data_dir = _config_data_dir(config_path, named=config_named)
    except Exception as exc:  # noqa: BLE001 - every unreadable config is the same refusal.
        from db_ops.lib import response as _response

        return _response.emit(_response.fail("check-secret", _unreadable_config(config_path, exc)))

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
    if not Path(inventory).is_file():
        # Said, not raised as `[Errno 2]`: on a new install there is no inventory yet, and the
        # request can name one (rules R09).
        return response.emit(response.fail(
            "inventory-summary",
            f"no inventory at {inventory}: pass \"inventory\" (a database-inventory.json), or build "
            "one first with the inventory workflow."))
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


def read_app_commands(data_dir: str | Path | None = None) -> list[dict[str, Any]] | None:
    """The records in ``data/app_commands.json``, or ``None`` when the file is not there.

    ``None`` and ``[]`` are different answers: no file is "not configured", an empty list is a node
    configured to run nothing. Read here with the shared JSON reader rather than through
    ``db_ops.db.ops_status``, because ``common`` may not import ``db``
    (``tests/test_import_boundaries.py``).
    """
    from db_ops.lib.json_io import load_json_file
    from db_ops.lib.paths import DEFAULT_DATA_DIR

    path = Path(data_dir or DEFAULT_DATA_DIR) / "app_commands.json"
    if not path.is_file():
        return None
    payload = load_json_file(path)
    items = payload.get("app_commands") if isinstance(payload, dict) else payload
    return [item for item in (items or []) if isinstance(item, dict)]


#: What the last-run column says when the caller stated none - named, so it is not read as
#: "never ran".
LAST_RUNS_NOT_STATED = ("not stated - the store is not opened here; the caller states them "
                        "(/spbot_self_status does)")


def collect_self_status(config_path: str | None = None, *,
                        last_runs: dict[str, Any] | None = None,
                        store_error: str = "") -> tuple[dict[str, Any], Any]:
    """Everything ``self-status`` reports, as facts, and the config it was read with (or ``None``).

    One command since 0.24.0 (rules R43; ``db.cli self-status`` was a second door to this report and
    went, the operator's choice). Each app command's last run is in ``job_runs``, which ``common``
    may not open - so ``last_runs`` is the caller's to state (rules R09): the bot reads it from its
    node's store before it asks. ``None`` is "not stated", which the column says in words.
    """
    from db_ops.common import self_status

    from db_ops.lib.version import __version__
    from db_ops.lib.paths import TOOL_ROOT

    public_version = None
    try:
        from db_ops.lib.distribution import PUBLIC_VERSION as public_version
    except Exception:  # noqa: BLE001 - a build without the public literal still reports itself.
        public_version = None

    # The store is named, never connected to: this command has to keep answering when the store
    # is the thing that is down, which is exactly when someone asks what version is running.
    store_text = None
    # Bound before the try, not inside it: when there is no config this command still has to
    # answer - that is the whole point of it - and reading `config` in the call below then raised
    # UnboundLocalError instead. The private tree has a config.json at its root, so only the
    # public suite saw it.
    runtime_dir = None
    config = None
    try:
        from db_ops.lib.config import load_config, resolve_config_path

        config = load_config(resolve_config_path("common", config_path))
        runtime_dir = getattr(config, "runtime_dir", None)
        store_config = getattr(config, "store", None)
        backend = getattr(store_config, "backend", None)
        if backend == "postgresql":
            postgres = getattr(store_config, "postgresql", None)
            # The SCHEMA is part of the answer, not a detail. On PostgreSQL the database is
            # routinely shared - this estate keeps the production store and every soak node in one
            # `db_ops` database, told apart only by schema - so a line ending at the database name
            # cannot answer "which store is this node on", which is the only reason to read it.
            # Asked on 2026-09-15 by an operator looking at exactly that line, with three schemas
            # in that database and no way to tell from here which one was live.
            _schema = str(getattr(postgres, "schema", "") or "").strip()
            store_text = (f"postgresql {getattr(postgres, 'username', '?')}@"
                          f"{getattr(postgres, 'host', '?')}:{getattr(postgres, 'port', '?')}"
                          f"/{getattr(postgres, 'database', '?')}"
                          + (f" schema={_schema}" if _schema else ""))
        elif backend:
            store_text = f"{backend} {getattr(getattr(store_config, 'sqlite', None), 'path', '')}"
    except Exception:  # noqa: BLE001 - no config is a fact about the install, not an error here.
        store_text = None
        config = None

    # Resolved here rather than inside self_status: this is the composition root, and everything
    # else that module reports comes from the machine rather than from data/.
    try:
        from db_ops.lib.data_sources import webhost_endpoints

        web_facts = webhost_endpoints(
            host=self_status.host_addresses().get("ip") or "",
            # Passed in, because whether this node can answer for its own address is a fact about
            # the runtime: in a container the socket's address is a bridge address nobody can
            # reach, and offering it as a link is worse than offering none.
            runtime=self_status.runtime())
    except Exception as exc:  # noqa: BLE001 - an unreadable config costs the links, not the report.
        web_facts = {"served_here": False, "error": str(exc)}

    facts = self_status.collect(
        tool_root=Path(TOOL_ROOT), version=__version__,
        public_version=public_version, store=store_text,
        runtime_dir=runtime_dir, web=web_facts)

    # What this node schedules, from config; when each last ran, as the caller stated it.
    try:
        commands = read_app_commands()
        facts["apps"] = self_status.summarize_apps(
            commands, node_role=str(facts.get("node_role") or ""), last_runs=last_runs,
            store_error=store_error or ("" if last_runs is not None else LAST_RUNS_NOT_STATED))
    except Exception as exc:  # noqa: BLE001 - an unreadable file is a line, not a lost report.
        facts["apps"] = {"configured": 0, "state": "not configured", "items": [],
                         "error": f"app_commands.json cannot be read: {exc}"}
    return facts, config


def emit_self_status(facts: dict[str, Any], request: dict[str, Any]) -> int:
    """Print the report the way the request asked."""
    from db_ops.common import self_status
    from db_ops.lib import response

    listing = self_status.render(facts)
    if str(request.get("format") or "json").strip().lower() == "txt":
        print(listing)
        return 0
    # `format: txt` prints the listing and nothing else; the default prints the envelope and
    # nothing else, carrying the same listing under `data`. stdout is the answer, never both -
    # tests/test_common_cli_response_shape.py holds every command in this CLI to that.
    return response.emit(response.ok(
        "self-status",
        message=f"{facts['version']} on {(facts['host'] or {}).get('hostname')}",
        data={"listing": listing, **facts},
        metrics={"cores": (facts.get("cpu") or {}).get("cores")},
    ))


def _self_status_command(argv: list[str]) -> int:
    """``self-status`` - the installation describing itself.

    Distinct from the three status answers that already exist, and the distinction is the point:
    ``ops-status`` reads the store for whether the apps ran, ``host-facts`` reaches a monitored
    host over its cmd_access, ``worker-status`` drives the worker from the master. This one is
    the process reporting on itself and the machine under it, which is what nothing answered.
    """
    source = ""
    config_path = None
    rest = list(argv)
    while rest:
        token = rest.pop(0)
        if token in {"-h", "--help"}:
            print(SELF_STATUS_USAGE)
            return 0
        if token == "--config":
            config_path = rest.pop(0) if rest else None
        elif token in {"--key", "--key-base64", "--key_base64"}:
            # Taken and unused: a command line `upgrade-config` pointed here from `db.cli
            # self-status` (0.24.0) may still carry the key that door wanted for the store.
            rest.pop(0) if rest else None
        elif not source:
            source = token
        else:
            print(f"Unexpected argument: {token}\n\n{SELF_STATUS_USAGE}", file=sys.stderr)
            return 2

    request, code = _read_json_request(source or "{}", SELF_STATUS_USAGE)
    if request is None:
        return code
    from db_ops.lib import response

    last_runs = request.get("last_runs")
    if last_runs is not None and not (isinstance(last_runs, dict)
                                      and all(isinstance(v, dict) for v in last_runs.values())):
        return response.emit(response.fail(
            "self-status", 'last_runs must be an object: {"<app code>": {"status", "started_at"}}.'))
    facts, _config = collect_self_status(config_path, last_runs=last_runs,
                                         store_error=str(request.get("store_error") or ""))
    return emit_self_status(facts, request)




def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # The reader holds the request to the reference entry of the command it was read for (R49).
    token = COMMAND.set(argv[0] if argv else "")
    try:
        return _dispatch(argv)
    except Exception as exc:  # noqa: BLE001 - every command answers in the envelope (rules R15).
        # A command that raised instead of answering left a traceback on stderr as its only report,
        # and a caller reading stdout could not tell a broken configuration from a crashed process.
        # `list-targets`, `sql-command-add`, `sql-target-add` and `lift-example` all did, given a
        # data folder holding one malformed file - found by the empty-configuration guard (R09).
        from db_ops.lib import errors, response

        # The exception's kind travels with it (lib.errors): a caller decides by `error_kind`,
        # never by matching the sentence.
        return response.emit(response.fail(argv[0] if argv else "", f"{type(exc).__name__}: {exc}",
                                           kind=errors.kind_of(exc)))
    finally:
        COMMAND.reset(token)


def _dispatch(argv: list[str]) -> int:
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    if argv[0] == "list-targets":
        from db_ops.lib import data_sources as target_resolve
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
    if argv[0] == "check-secret-literals":
        return _check_secret_literals_command(argv[1:])
    if argv[0] == "lift-example":
        return _lift_example_command(argv[1:])
    if argv[0] == "build-showcase":
        return _build_showcase_command(argv[1:])
    if argv[0] == "instance-add":
        return _instance_add_command(argv[1:])
    if argv[0] == "app-command-set":
        return _app_command_set_command(argv[1:])
    if argv[0] == "sql-command-add":
        return _sql_command_add_command(argv[1:])
    if argv[0] == "sql-target-add":
        return _sql_target_add_command(argv[1:])
    if argv[0] == "remote-credential-add":
        return _remote_credential_add_command(argv[1:])
    if argv[0] == "secret-set":
        return _secret_set_command(argv[1:])
    if argv[0] == "probe-host":
        return _probe_host_command(argv[1:])
    if argv[0] == "db-status":
        return _db_status_command(argv[1:])
    if argv[0] == "self-status":
        return _self_status_command(argv[1:])
    if argv[0] in {"restore-full", "restore-diff", "restore-log",
                   "restore-key", "restore-metadata", "verify-restore"}:
        from db_ops.common import cli_restorestep

        return cli_restorestep.run(argv[0], argv[1:], read_request=_read_json_request)
    if argv[0] in {"describe-object", "due-check", "check-objects", "check-references",
                   "standardize-field-names", "upgrade-config"}:
        from db_ops.common import cli_config_objects

        return cli_config_objects.run(argv[0], argv[1:], read_request=_read_json_request)
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
    if argv[0] in {"backup-chain", "copy-backup-dir", "prune-staged-backups"}:
        from db_ops.common import cli_backup_copy

        return cli_backup_copy.run(argv[0], argv[1:], read_request=_read_json_request)
    if argv[0] in {"smb-list", "smb-get", "smb-delete", "smb-credential"}:
        # 0.24.0: a Windows share, reached here rather than by each app (rules R10).
        from db_ops.common import cli_smb

        return cli_smb.run(argv[0], argv[1:], read_request=_read_json_request)
    if argv[0] == "run-sqlcmd":
        from db_ops.common import cli_sqlcmd

        return cli_sqlcmd.run(argv[1:], read_request=_read_json_request)
    if argv[0] == "metric-batch":
        # 0.24.0: the metrics app's execution, one process per target (rules R03, R10).
        from db_ops.common import cli_metric_batch

        return cli_metric_batch.run(argv[1:], read_request=_read_json_request)
    if argv[0] in {"create-db-docker", "move-db-docker"}:
        from db_ops.common import cli_docker_db

        return cli_docker_db.run(argv[0], argv[1:], read_request=_read_json_request)
    if argv[0] == "backup-database":
        from db_ops.common import cli_backup

        return cli_backup.run(argv[1:], read_request=_read_json_request)
    if argv[0] == "check-credentials":
        # 0.24.0, from the root package (rules R41): its two resolvers are lib's now.
        from db_ops.common import cli_check_credentials

        return cli_check_credentials.run(argv[1:])
    if argv[0] in {"init", "guide", "encrypt-secret", "export-data", "import-data"}:
        # The tool root's own commands, moved here from the root package in 0.24.0 (rules R41).
        from db_ops.common import cli_tool_root

        return cli_tool_root.run(argv[0], argv[1:], read_request=_read_json_request,
                                 read_key_flags=_read_key_flags)
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
    if argv[0] == "ask":
        return _ask_command(argv[1:])
    if argv[0] == "authorize":
        return _gate_command(argv[1:], AUTHORIZE_USAGE, "authorize")
    if argv[0] in {"shrink-log", "kill-spid", "start-job", "disable-job"}:
        return _gate_command(argv[1:], EMERGENCY_USAGE, argv[0])
    if argv[0] in {"sqlserver-precheck", "sqlserver-apply-cu", "sqlserver-verify-build"}:
        return _gate_command(argv[1:], SQLSERVER_PATCH_USAGE, argv[0])
    if argv[0] in {"sqlserver-export-instance", "sqlserver-replay-instance",
                   "sqlserver-verify-instance"}:
        return _gate_command(argv[1:], SQLSERVER_INSTANCE_USAGE, argv[0])
    return config_admin.main(argv)


if __name__ == "__main__":
    # The answer leaves as UTF-8 when stdout is a pipe: transport.common_cli, this CLI's one client,
    # decodes UTF-8, and a Windows pipe defaults to the ANSI code page - so every non-ASCII
    # character in an answer arrived as U+FFFD, the em dash of an "already exists" refusal among
    # them (found testing create-db-docker on the labs, 2026-09-25). A console keeps its own.
    if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
