from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from db_ops.config import load_config, resolve_config_path
from db_ops.lib import secret_text
from db_ops.logging_ops import log_function_call, log_function_error, setup_app_logger
from db_ops.logging_ops.runtime_stdout import patch_stdout
from db_ops.webhost.server import serve


def _add_key_args(parser: argparse.ArgumentParser) -> None:
    """The passphrase flags, on every subcommand that opens the store.

    Declared here rather than only at the top level because the daemon inspects a child CLI's
    source for ``add_key_argument`` and injects ``--key-base64`` immediately after the module
    name — before the subcommand. Both positions therefore have to parse.
    """
    secret_text.add_key_argument(parser, inherited=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DB Ops web host: reports over HTTP plus the console.")
    parser.add_argument("--config", default=None, help="Path to config JSON. Defaults to config.webhost.json or config.json.")
    secret_text.add_key_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    srv = subparsers.add_parser("serve", help="Serve a directory over HTTP with a stable 'latest' link.")
    _add_key_args(srv)
    srv.add_argument("--root", default=None,
                     help="Directory to publish (default: <runtime>/reports).")
    srv.add_argument("--mount", default="report_dba",
                     help="URL path prefix the root is served under (default: report_dba).")
    srv.add_argument("--port", type=int, default=8080, help="TCP port to listen on (default: 8080).")
    srv.add_argument("--bind", default="0.0.0.0", help="Address to bind (default: 0.0.0.0).")
    srv.add_argument("--webroot", default=None,
                     help="Web root holding the <mount> symlink (default: <runtime>/webroot).")
    srv.add_argument("--latest", default="database-inventory.html",
                     help="Stable filename (symlink) that always points at the newest report.")
    srv.add_argument("--latest-glob", default="*_database-inventory-report.html",
                     help="Glob (within --root) selecting reports the --latest link tracks.")
    srv.add_argument("--refresh-seconds", type=int, default=60,
                     help="How often to refresh the 'latest' symlink (default: 60).")
    srv.add_argument("--console", dest="console", action="store_true", default=True,
                     help="Serve the web console at /<console-mount>/ (default: on).")
    srv.add_argument("--no-console", dest="console", action="store_false",
                     help="Serve reports only; no login, no console.")
    srv.add_argument("--console-mount", default=None,
                     help="URL prefix for the console (default: from data/webhost_config.json).")
    srv.set_defaults(handler=_handle_serve)

    add = subparsers.add_parser("user-add", help="Create a console account.")
    _add_key_args(add)
    add.add_argument("--username", required=True)
    add.add_argument("--level", type=int, required=True, help="Permission level, 1..100.")
    add.add_argument("--display-name", default="")
    add.add_argument("--email", default="")
    add.add_argument("--note", default="")
    _add_password_args(add)
    _add_secret_args(add)
    add.set_defaults(handler=_handle_user_add)

    pwd = subparsers.add_parser("user-password", help="Change an account's password.")
    _add_key_args(pwd)
    pwd.add_argument("--username", required=True)
    pwd.add_argument("--keep-sessions", action="store_true",
                     help="Do not revoke the account's existing sessions (not recommended).")
    _add_password_args(pwd)
    _add_secret_args(pwd)
    pwd.set_defaults(handler=_handle_user_password)

    lvl = subparsers.add_parser("user-level", help="Change an account's permission level.")
    _add_key_args(lvl)
    lvl.add_argument("--username", required=True)
    lvl.add_argument("--level", type=int, required=True, help="Permission level, 1..100.")
    lvl.set_defaults(handler=_handle_user_level)

    dis = subparsers.add_parser("user-disable",
                                help="Disable an account and revoke its sessions. The row is kept.")
    _add_key_args(dis)
    dis.add_argument("--username", required=True)
    dis.add_argument("--note", default="")
    dis.set_defaults(handler=_handle_user_disable)

    lst = subparsers.add_parser("user-list", help="List console accounts.")
    _add_key_args(lst)
    lst.add_argument("--all", action="store_true", help="Include disabled accounts.")
    lst.set_defaults(handler=_handle_user_list)

    show = subparsers.add_parser(
        "user-password-show",
        help="Print one account's password from the secret store (needs the passphrase).")
    _add_key_args(show)
    show.add_argument("--username", required=True)
    show.set_defaults(handler=_handle_user_password_show)

    ses = subparsers.add_parser("sessions", help="List or revoke console sessions.")
    _add_key_args(ses)
    ses.add_argument("--username", default="", help="Only this account.")
    ses.add_argument("--all", action="store_true", help="Include ended sessions.")
    ses.add_argument("--revoke", action="store_true",
                     help="Revoke every listed session instead of printing them.")
    ses.set_defaults(handler=_handle_sessions)

    return parser.parse_args(argv)


def _add_password_args(parser: argparse.ArgumentParser) -> None:
    """How a password reaches this process.

    ``--password-stdin`` is the documented path and ``--password`` is the convenience one, because
    argv is world-readable in the process table on both platforms — the same reason the store
    declaration travels on stdin (see ``db_ops/db/declaration.py``). Anyone using ``--password`` on
    a shared host should treat that password as disclosed.
    """
    parser.add_argument("--password", default=None,
                        help="The password. Visible in the process list; prefer --password-stdin.")
    parser.add_argument("--password-stdin", action="store_true",
                        help="Read the password from the first line of stdin.")


def _add_secret_args(parser: argparse.ArgumentParser) -> None:
    """Whether the password is also kept in the secret store, where it can be read back.

    On by default, because the alternative in practice is not "a safer password" but "a password
    nobody can recall and an account that gets reset every few months". What it costs is stated at
    :mod:`db_ops.db.web_auth_store`: the hash stops being the only copy, and anyone with
    ``DB_OPS_SECRET_KEY`` can read it. Pass ``--no-remember`` for an account where that matters
    more than being able to look it up.
    """
    parser.add_argument("--remember", dest="remember", action="store_true", default=True,
                        help="Also store the password in data/encrypted_secret_text.json "
                             "under WEB_CONSOLE_<USERNAME> (default: on).")
    parser.add_argument("--no-remember", dest="remember", action="store_false",
                        help="Keep only the hash. The password cannot be recovered, only reset.")


def _remember(args, username: str, password: str) -> str:
    """Write the password to the secret store when asked. Returns the ref, or ``""``."""
    from db_ops.db.web_auth_store import remember_password

    if not getattr(args, "remember", False):
        return ""
    try:
        return remember_password(
            username, password,
            key=secret_text.resolve_cli_key(getattr(args, "key", None),
                                            getattr(args, "key_base64", None)))
    except Exception as exc:  # noqa: BLE001 - the account is what matters; the copy is a note.
        print(f"NOTE: the account was written, but the password could not be stored in the "
              f"secret store: {exc}", file=sys.stderr)
        return ""


def _read_password(args) -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\r\n")
    if getattr(args, "password", None):
        return str(args.password)
    raise SystemExit("A password is required: pass --password-stdin (preferred) or --password.")


def _stores(args):
    """The auth store, the config store and the run-history store, all on the declared backend."""
    from db_ops.db import DbOpsStore
    from db_ops.db.config_store import ConfigStore
    from db_ops.db.web_auth_store import WebAuthStore

    config = load_config(resolve_config_path("webhost", args.config))
    secret_text.set_key_env(getattr(args, "key", None), getattr(args, "key_base64", None))
    return config, WebAuthStore.from_config(config), ConfigStore.from_config(config), DbOpsStore.from_config(config)


def _web_settings():
    """The console settings, read from ``data/webhost_config.json`` on disk.

    Read from the file rather than from the config mirror, unlike the app blocks the dashboard
    draws. The split is deliberate: these are the settings the server needs *to start* — the mount,
    the cookie name, the session length — and taking them from the store would mean a store that
    is unreachable, or that has never been synced, leaves the console with no idea where to serve
    itself. The blocks are presentation and can wait for a query; the mount cannot.
    """
    from db_ops.lib.paths import DEFAULT_DATA_DIR
    from db_ops.webhost.app import WebSettings

    path = Path(DEFAULT_DATA_DIR) / "webhost_config.json"
    if not path.is_file():
        return WebSettings()
    return WebSettings.from_payload(json.loads(path.read_text(encoding="utf-8-sig")))


def _handle_serve(args, config, logger) -> int:
    from db_ops.db import DbOpsStore
    from db_ops.db.config_store import ConfigStore
    from db_ops.db.run_requests import RunRequestStore
    from db_ops.db.web_auth_store import WebAuthStore
    from db_ops.webhost.app import WebApp
    from db_ops.lib.paths import DEFAULT_DATA_DIR

    root = Path(args.root) if args.root else (config.runtime_dir / "reports")
    webroot = Path(args.webroot) if args.webroot else (config.runtime_dir / "webroot")

    console = None
    if args.console:
        settings = _web_settings()
        if args.console_mount:
            from dataclasses import replace

            settings = replace(settings, mount=str(args.console_mount).strip("/"))
        console = WebApp(
            auth_store=WebAuthStore.from_config(config),
            config_store=ConfigStore.from_config(config),
            ops_store=DbOpsStore.from_config(config),
            request_store=RunRequestStore.from_config(config),
            data_dir=DEFAULT_DATA_DIR,
            log_dir=config.log_dir,
            settings=settings,
        )
    return serve(
        root=root,
        mount=args.mount,
        port=int(args.port),
        bind=args.bind,
        webroot=webroot,
        latest=args.latest,
        latest_glob=args.latest_glob,
        refresh_seconds=int(args.refresh_seconds),
        logger=logger,
        console=console,
    )


def _handle_user_add(args, config, logger) -> int:
    _, auth, _, _ = _stores(args)
    password = _read_password(args)
    ref = _remember(args, args.username, password)
    user_id = auth.create_user(
        username=args.username, password=password, level=args.level,
        display_name=args.display_name, email=args.email, actor="webhost.cli",
        note=args.note, password_ref=ref)
    print(f"Created user '{args.username}' (id {user_id}) at level {args.level}.")
    if ref:
        print(f"Password also stored in the secret store as '{ref}'. "
              f"Read it back with: python -m db_ops.webhost.cli user-password-show "
              f"--username {args.username} --key-base64 <KEY>")
    return 0


def _handle_user_password(args, config, logger) -> int:
    _, auth, _, _ = _stores(args)
    password = _read_password(args)
    ref = _remember(args, args.username, password)
    auth.set_password(username=args.username, password=password,
                      actor="webhost.cli", revoke_sessions=not args.keep_sessions,
                      password_ref=ref if ref else None)
    ended = "" if args.keep_sessions else " Existing sessions were revoked."
    print(f"Password changed for '{args.username}'.{ended}")
    if ref:
        print(f"The secret store copy '{ref}' was updated too.")
    return 0


def _handle_user_password_show(args, config, logger) -> int:
    """Print one account's password from the secret store.

    Reads the secret store, never the ``web_users`` row: the row holds a hash, and the whole
    point of a hash is that this command cannot work from it.
    """
    from db_ops.db.web_auth_store import recall_password, secret_ref_for

    _, auth, _, _ = _stores(args)
    if auth.get_user(args.username) is None:
        print(f"No active user named '{args.username}'.", file=sys.stderr)
        return 1
    password = recall_password(
        args.username,
        key=secret_text.resolve_cli_key(getattr(args, "key", None),
                                        getattr(args, "key_base64", None)))
    if not password:
        print(f"'{secret_ref_for(args.username)}' is not in the secret store — this account was "
              "created with --no-remember, or before the copy existed. The password cannot be "
              "recovered from the hash; set a new one with user-password.", file=sys.stderr)
        return 1
    print(password)
    return 0


def _handle_user_level(args, config, logger) -> int:
    _, auth, _, _ = _stores(args)
    auth.set_level(username=args.username, level=args.level, actor="webhost.cli")
    print(f"'{args.username}' is now level {args.level}.")
    return 0


def _handle_user_disable(args, config, logger) -> int:
    _, auth, _, _ = _stores(args)
    auth.deactivate_user(username=args.username, actor="webhost.cli", note=args.note)
    print(f"'{args.username}' is disabled. The row is kept; the username is free to reissue.")
    return 0


def _handle_user_list(args, config, logger) -> int:
    _, auth, _, _ = _stores(args)
    rows = auth.list_users(include_inactive=args.all)
    if not rows:
        print("No accounts. Create one with: python -m db_ops.webhost.cli user-add ...")
        return 0
    print(f"{'username':22} {'level':>5}  {'state':9} {'last login':21} {'secret ref':26} "
          "display name")
    for row in rows:
        state = "active" if int(row["is_active"]) else "disabled"
        print(f"{str(row['username']):22} {int(row['user_level']):>5}  {state:9} "
              f"{str(row['last_login_at'] or '-'):21} {str(row['password_ref'] or '-'):26} "
              f"{str(row['display_name'] or '')}")
    return 0


def _handle_sessions(args, config, logger) -> int:
    _, auth, _, _ = _stores(args)
    user_id = None
    if args.username:
        user = auth.get_user(args.username)
        if user is None:
            print(f"No active user named '{args.username}'.", file=sys.stderr)
            return 1
        user_id = int(user["web_user_id"])

    if args.revoke:
        if user_id is None:
            print("--revoke needs --username, so a slip cannot log out the whole estate.",
                  file=sys.stderr)
            return 2
        count = auth.revoke_user_sessions(user_id, reason="revoked from CLI")
        print(f"Revoked {count} session(s) for '{args.username}'.")
        return 0

    rows = auth.list_sessions(web_user_id=user_id, include_inactive=args.all)
    if not rows:
        print("No sessions.")
        return 0
    print(f"{'id':>5}  {'username':22} {'state':8} {'issued':21} {'expires':21} client")
    for row in rows:
        state = "active" if int(row["is_active"]) else str(row["revoked_reason"] or "ended")
        print(f"{int(row['web_session_id']):>5}  {str(row['username']):22} {state:8} "
              f"{str(row['issued_at']):21} {str(row['expires_at']):21} {str(row['client_ip'] or '-')}")
    return 0


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logger = None
    try:
        config = load_config(resolve_config_path("webhost", args.config))
        patch_stdout(config.log_dir / "webhost_runtime.log", app_name="webhost")
        logger = setup_app_logger(config, app_name="webhost", enable_telegram_alerts=False, enable_console=False)
        log_function_call(logger, function_name=f"webhost.{args.command}")
        secret_text.set_key_env(getattr(args, "key", None), getattr(args, "key_base64", None))
        return int(args.handler(args, config, logger))
    except Exception as exc:  # noqa: BLE001
        if logger is not None:
            log_function_error(logger, function_name="webhost.main", error=exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
