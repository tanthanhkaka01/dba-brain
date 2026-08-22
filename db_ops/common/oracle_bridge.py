"""How db_ops reaches a **legacy Oracle** database (8i / 8.1.7).

Some Oracle hosts cannot be reached by the modern ``oracledb``/``cx_Oracle`` clients db_ops
runs on: ``python-oracledb`` thin mode speaks to 12.1+ only, and an Oracle Client 11.2 refuses
a 8.1.7 server. The one combination that works is the old one — Oracle Client 10.2 driving
``cx_Oracle`` 5.1.2 on Python 2.7 32-bit — which cannot be installed beside db_ops without
dragging a second Python and a second Oracle client into every runtime. So it lives apart, as a
self-contained tool under ``tools/python32_legacy`` that takes one JSON request on stdin and
answers with one JSON object on stdout, and db_ops only ever *calls* it.

Two transports carry that call, chosen per target by ``db_instances.json`` ``sql_access.method``
(see :func:`normalize_sql_access`):

``subprocess``  run the tool here — the caller's own machine holds the legacy runtime. No
                network hop, nothing to keep running, and the request never leaves the host.
``api``         POST to an HTTP bridge that runs the tool on some *other* host. This is what a
                machine without the legacy runtime has to use; the bridge is
                ``tools/python32_legacy/legacy_oracle_bridge.py``.
``direct``      not legacy at all — connect to the database normally. The default for every
                other target, handled by :mod:`db_ops.common.db_connect`.

**No credential is ever written in config.** The connect string is assembled here from the
target's own ``users.json`` credential (username) and the encrypted secret store (password) —
the same credential every other app resolves for that server. A hand-written
``ORACLE8I_CONNECT_*`` secret holding ``user/pass@host/service`` used to exist for this and was
deleted on 2026-08-01 (``audits/20260801_audit_secret_text.md``): it duplicated a password that
was already in the store under its own ref, so rotating the login left the copy behind.
``sql_access.connect_ref`` still overrides, for a login that exists nowhere else.

Over the ``api`` transport db_ops does not send the password at all: it encrypts
``connect + mode + minute + nonce`` into a short-lived (≈90s) HMAC-signed token that the bridge
verifies and decrypts. The token crypto below is a byte-for-byte port of the bridge's
``bridge_token.py`` so both sides interoperate — change one and you must change both.

Callers: :mod:`db_ops.metrics.executor` (SQL-family metrics on 8i targets) and
:mod:`db_ops.common.sql_run` (``run-sql``, ``/spbot_sql_to_xlsx`` — anything that runs one
statement and exports the rows).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Re-exported: what `sql_access` *means* — the accepted methods, whether a target is legacy, and
# the normalized block — moved to db_ops/lib/sql_access.py so apps can read their own config
# without importing `common`. The bridge transport below is an operation and stayed here.
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.sql_access import (  # noqa: F401 - re-exported for compatibility
    SUPPORTED_SQL_ACCESS_METHODS, LegacyOracleError, is_legacy, normalize_sql_access)

VERSION = "v1"
DEFAULT_TTL_SECONDS = 90

#: Repository root — ``tools/python32_legacy`` and every other configured relative path resolve
#: against it, never against the process's working directory (which the daemon does not let a
#: caller predict). Same rule as :mod:`db_ops.lib.result_format` and ``file_transfer``.


#: Where the legacy tool lives, and how to start it, when ``sql_access`` does not say.
#: The defaults describe the Windows master's checkout; a host that keeps the tool elsewhere
#: (or reaches it through a container) states it in config rather than in code.
DEFAULT_TOOL_DIR = "tools/python32_legacy"
DEFAULT_PYTHON_EXE = "tools/Python27-32/python.exe"
LEGACY_CLI_SCRIPT = "legacy_oracle_cli.py"
DEFAULT_ORACLE_PORT = 1521
#: How long to wait on the Oracle 8i bridge's HTTP call. Named for what it times: three
#: modules used to spell this `DEFAULT_BRIDGE_TIMEOUT_SECONDS` at 30, while two siblings used the
#: same name at 10 and 8 — the name was a per-module habit, not a shared setting.
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 30








# --------------------------------------------------------------------------- #
# Token crypto — must match the bridge's bridge_token.py exactly.
# --------------------------------------------------------------------------- #
def _to_bytes(value: Any) -> bytes:
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    key_values = bytearray(key)
    return bytes(bytearray(
        byte ^ key_values[index % len(key_values)]
        for index, byte in enumerate(bytearray(data))
    ))


def _keystream_with_nonce(secret: bytes, minute: int, nonce: str, length: int) -> bytes:
    chunks: list[bytes] = []
    counter = 0
    seed = _to_bytes("%s:%s:%s" % (VERSION, minute, nonce))
    while sum(len(chunk) for chunk in chunks) < length:
        chunks.append(hmac.new(secret, seed + b":" + _to_bytes(str(counter)), hashlib.sha256).digest())
        counter += 1
    return b"".join(chunks)[:length]


def _signature(secret: bytes, minute: int, nonce: str, ciphertext: bytes) -> bytes:
    signed = _to_bytes("%s:%s:%s:" % (VERSION, minute, nonce)) + ciphertext
    return hmac.new(secret, signed, hashlib.sha256).digest()


def encrypt_token(connect: str, mode: str, secret: str, *, now: float) -> str:
    """Build a bridge token. ``now`` is required (no implicit clock) so callers stay testable."""
    if mode not in ("normal", "sysdba"):
        raise ValueError("mode must be 'normal' or 'sysdba'")
    secret_bytes = _to_bytes(secret)
    minute = int(now) // 60
    nonce = _b64url_encode(os.urandom(12))
    payload = {"connect": connect, "mode": mode, "minute": minute, "nonce": nonce}
    plaintext = _to_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    ciphertext = _xor_bytes(plaintext, _keystream_with_nonce(secret_bytes, minute, nonce, len(plaintext)))
    sig = _signature(secret_bytes, minute, nonce, ciphertext)
    return "%s.%s.%s.%s.%s" % (VERSION, minute, nonce, _b64url_encode(ciphertext), _b64url_encode(sig))


# --------------------------------------------------------------------------- #
# Connect string
# --------------------------------------------------------------------------- #
def resolve_secret(sql_access: dict[str, Any], secrets: dict[str, str]) -> str:
    """Bridge shared secret: ``secret_ref`` from the secret store, else the environment.

    The ref names both places, which is the convention everywhere else in db_ops — see
    ``remote_exec._resolve_secret_value``: "refs double as env var names across db_ops". Until
    2026-08-21 the environment fallback read one hardcoded name instead, and that name was derived
    from a real host (``TOKEN_<ip>_ORACLE_BRIDGE``). It leaked an address into the source, and it
    silently ignored the ref the config actually named — so a second bridge, on a second host,
    could not be given its own secret through the environment at all.
    """
    ref = str(sql_access.get("secret_ref") or "").strip()
    if not ref:
        raise LegacyOracleError(
            "Oracle bridge secret not found: sql_access.secret_ref names no secret."
        )
    if secrets.get(ref):
        return secrets[ref]
    env_secret = os.environ.get(ref, "").strip()
    if env_secret:
        return env_secret
    raise LegacyOracleError(
        f"Oracle bridge secret not found: put {ref!r} in the secret store, "
        f"or set an environment variable of that name."
    )


def build_connect_string(
    *, username: str, password: str, host: str, port: Any = None, service_name: str,
) -> str:
    """The ``user/password@host:port/service`` string the legacy tool parses.

    Assembled per run from the pieces db_ops already holds, so the password exists in exactly
    one place — the encrypted store — and a rotation is picked up by the next run with nothing
    else to edit. The tool splits on the *last* ``@``, so a password containing one survives;
    it cannot contain a ``/`` before that, which is a real Oracle password constraint anyway.
    """
    username = str(username or "").strip()
    host = str(host or "").strip()
    service_name = str(service_name or "").strip()
    missing = [
        name for name, value in
        (("username", username), ("host", host), ("service_name", service_name))
        if not value
    ]
    if missing:
        raise LegacyOracleError(
            f"Cannot build the legacy Oracle connect string: missing {', '.join(missing)}. "
            "The service name comes from the instance's service_name/instance_name in "
            "db_instances.json."
        )
    return f"{username}/{password}@{host}:{int(port or DEFAULT_ORACLE_PORT)}/{service_name}"


def resolve_connect(
    sql_access: dict[str, Any],
    secrets: dict[str, str],
    *,
    credential: dict[str, Any] | None = None,
    host: str = "",
    port: Any = None,
    service_name: str = "",
) -> str:
    """The connect string for this run: the ``connect_ref`` override, else built from the credential.

    ``connect_ref`` names a secret holding a whole connect string. It stays supported for a login
    that lives nowhere else, but it is not the normal path — see the module docstring for why the
    two that existed were deleted.
    """
    from db_ops.common.sql_execution import resolve_password

    ref = str(sql_access.get("connect_ref") or "").strip()
    if ref:
        if secrets.get(ref):
            return secrets[ref]
        raise LegacyOracleError(
            f"sql_access.connect_ref '{ref}' is not in the secret store. Remove it to build the "
            "connect string from the target's own credential instead."
        )
    if not credential:
        raise LegacyOracleError(
            "No credential for this legacy Oracle target: name one in the request, or set "
            "default_credential_name on the instance and declare it under database_credentials "
            "in users.json."
        )
    return build_connect_string(
        username=str(credential.get("username") or ""),
        password=resolve_password(credential, secrets),
        host=host,
        port=port,
        service_name=service_name,
    )


def connect_mode(sql_access: dict[str, Any], credential: dict[str, Any] | None = None) -> str:
    """``normal`` or ``sysdba``. ``sql_access.mode`` wins; otherwise a credential declared with
    ``role: SYSDBA`` connects that way — on 8i the DBA views a metric reads (``v$instance``) are
    unreadable to a login that does not."""
    mode = str(sql_access.get("mode") or "").strip().lower()
    if not mode and credential:
        mode = "sysdba" if str(credential.get("role") or "").strip().upper() == "SYSDBA" else ""
    return mode if mode in {"normal", "sysdba"} else "normal"


# --------------------------------------------------------------------------- #
# Running a statement
# --------------------------------------------------------------------------- #
def prepare_sql(sql: str) -> str:
    """The SQL as the legacy tool can take it.

    Two things it cannot: ``cx_Oracle`` rejects a trailing ``;`` on a single statement
    (ORA-00911), and the Python 2.7 tool encodes the statement as ASCII, so one non-ASCII
    character anywhere — an em-dash in a comment is the usual one — raises UnicodeEncodeError
    and the run comes back as an opaque HTTP 500. Dropping those characters changes a comment;
    keeping them loses the whole result.
    """
    sql = str(sql or "").strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    if any(ord(ch) > 127 for ch in sql):
        sql = sql.encode("ascii", "ignore").decode("ascii")
    return sql


#: A schema name Oracle will accept unquoted. Anything else is refused rather than quoted,
#: because this value is concatenated into an ALTER SESSION statement (it cannot be a bind
#: variable) and it arrives from config or a request field.
_SCHEMA_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


def schema_prelude(schema: str) -> list[str]:
    """The session setup for running an application's SQL as a DBA login.

    An archived script names its tables unqualified (``FROM CT_CUT``) because the person who
    wrote it was connected as the application owner. db_ops connects with the target's declared
    credential, which on an 8i host is usually ``SYS`` — and SYS resolves ``CT_CUT`` in the SYS
    schema, so the script fails with ORA-00942 for a table that plainly exists. Setting
    CURRENT_SCHEMA makes the script run as written without granting anything or editing it.
    """
    schema = str(schema or "").strip()
    if not schema:
        return []
    if not _SCHEMA_NAME.match(schema):
        raise LegacyOracleError(
            f"Schema name {schema!r} is not a plain Oracle identifier. It becomes part of an "
            "ALTER SESSION statement, which takes no bind variable, so only [A-Za-z][A-Za-z0-9_$#]* "
            "is accepted."
        )
    return [f"alter session set current_schema = {schema}"]


def run_query(
    *,
    sql: str,
    sql_access: dict[str, Any],
    secrets: dict[str, str],
    credential: dict[str, Any] | None = None,
    host: str = "",
    port: Any = None,
    service_name: str = "",
    now: float,
    schema: str = "",
    limit: int = 0,
    timeout_seconds: int = DEFAULT_BRIDGE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute ``sql`` on a legacy Oracle target and return its first result set.

    Returns the tool's own answer, normalized::

        {"columns": [...], "rows": [[...]], "row_count": int, "truncated": bool,
         "db_version": "8.1.7.0.0", "transport": "subprocess"|"api"}

    ``limit`` is the tool's row cap (0 = no cap). Both transports run the identical request
    object against the identical script, so which one a target uses changes *where* the SQL ran
    and nothing about what comes back.
    """
    method = str(sql_access.get("method") or "direct").strip().lower()
    if method not in {"api", "subprocess"}:
        raise LegacyOracleError(
            f"sql_access.method '{method}' is not a legacy transport; expected 'api' or 'subprocess'."
        )
    connect = resolve_connect(
        sql_access, secrets, credential=credential, host=host, port=port, service_name=service_name,
    )
    request = {
        "connect": connect,
        "sql": prepare_sql(sql),
        "mode": connect_mode(sql_access, credential),
        "limit": max(0, int(limit or 0)),
        "commit": False,
        "prelude": schema_prelude(schema or sql_access.get("schema") or ""),
    }
    # The caller's timeout wins when it states one: a SQL task allowed 30 minutes and a metric
    # allowed 5 seconds run against the same target, and the target-level value is the default
    # for whoever does not say — not a ceiling on whoever does.
    timeout_seconds = int(timeout_seconds or sql_access.get("timeout_seconds") or DEFAULT_BRIDGE_TIMEOUT_SECONDS)

    if method == "subprocess":
        body = _run_subprocess(request, sql_access, timeout_seconds=timeout_seconds)
    else:
        body = _post_bridge(request, sql_access, secrets, now=now, timeout_seconds=timeout_seconds)

    if not body.get("ok", False):
        raise LegacyOracleError(f"Legacy Oracle run failed: {body.get('error') or body}")
    columns = [str(name) for name in (body.get("columns") or [])]
    rows = [list(row) for row in (body.get("rows") or [])]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": int(body.get("row_count") or len(rows)),
        "truncated": bool(body.get("truncated")),
        "db_version": str(body.get("db_version") or ""),
        "transport": method,
    }


def run_bridge_query(
    *,
    sql: str,
    sql_access: dict[str, Any],
    secrets: dict[str, str],
    now: float,
    credential: dict[str, Any] | None = None,
    host: str = "",
    port: Any = None,
    service_name: str = "",
    limit: int = 0,
    timeout_seconds: int = DEFAULT_BRIDGE_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """:func:`run_query`, with the rows as ``list[dict]`` keyed by column name.

    The shape the direct executors return, so metric parsing does not branch on transport.
    """
    result = run_query(
        sql=sql, sql_access=sql_access, secrets=secrets, credential=credential, host=host,
        port=port, service_name=service_name, now=now, limit=limit,
        timeout_seconds=timeout_seconds,
    )
    return [dict(zip(result["columns"], row)) for row in result["rows"]]


def _post_bridge(
    request: dict[str, Any], sql_access: dict[str, Any], secrets: dict[str, str],
    *, now: float, timeout_seconds: int,
) -> dict[str, Any]:
    """Send the request to the HTTP bridge. The password travels only inside the token."""
    bridge_url = str(sql_access.get("bridge_url") or "").strip()
    if not bridge_url:
        raise LegacyOracleError("sql_access.bridge_url is required for method 'api'.")
    secret = resolve_secret(sql_access, secrets)
    payload = {
        "token": encrypt_token(request["connect"], request["mode"], secret, now=now),
        "sql": request["sql"],
        "limit": request["limit"],
        "prelude": request["prelude"],
    }
    http_request = urllib.request.Request(
        bridge_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=int(timeout_seconds)) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The bridge answered — it just answered with a failure, and its body carries the real
        # reason (`{"error": "ORA-00904: invalid column name", ...}`). This used to fall through
        # to the OSError branch below, because HTTPError *is* an OSError, so a plain SQL error
        # was reported as "the bridge did not answer" and told the operator to go and start a
        # bridge that was already running. Worse for a caller: a query that fails because this
        # Oracle release lacks a column is indistinguishable from the host being down, so it
        # cannot fall back to a narrower query.
        raise LegacyOracleError(_bridge_error_text(bridge_url, exc)) from exc
    except OSError as exc:  # URLError/timeout/refused — the bridge host, not the SQL.
        raise LegacyOracleError(
            f"Oracle bridge at {bridge_url} did not answer ({exc}). Start it on that host "
            "(tools/python32_legacy/run_bridge.ps1), or use sql_access.method 'subprocess' "
            "where the legacy runtime is installed."
        ) from exc


def _bridge_error_text(bridge_url: str, exc: Any) -> str:
    """The bridge's own error message, or a description of the HTTP failure if it sent none."""
    body = ""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - an unreadable body is not the failure being reported.
        body = ""
    detail = ""
    if body:
        try:
            parsed = json.loads(body)
            detail = str(parsed.get("error") or "").strip()
        except (ValueError, AttributeError):
            detail = body.strip()[:400]
    if detail:
        return f"Oracle bridge at {bridge_url} refused the statement: {detail}"
    return (
        f"Oracle bridge at {bridge_url} answered HTTP {getattr(exc, 'code', '?')} with no "
        "message body."
    )


def _run_subprocess(
    request: dict[str, Any], sql_access: dict[str, Any], *, timeout_seconds: int,
) -> dict[str, Any]:
    """Run the legacy tool as a child process and read its one JSON object back.

    The request goes in on **stdin**, never as arguments: the connect string carries the
    password, and an argument is readable in the process table by anyone on the host for as long
    as the query runs. Stdin also removes every quoting and length limit on the SQL — a Windows
    command line caps out around 32k, which a real report query reaches.
    """
    argv = subprocess_argv(sql_access)
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_seconds)),
            cwd=str(_tool_dir(sql_access)),
        )
    except FileNotFoundError as exc:
        raise LegacyOracleError(
            f"Legacy Oracle runtime not startable ({argv[0]}): {exc}. This host has no "
            "tools/python32_legacy runtime — provision it (see that folder's README) or point "
            "the target at a bridge with sql_access.method 'api'."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LegacyOracleError(
            f"Legacy Oracle run timed out after {timeout_seconds}s. Raise "
            "sql_access.timeout_seconds if the statement is genuinely that slow."
        ) from exc

    stdout = (completed.stdout or "").strip()
    if not stdout:
        # The tool reports its own failures as JSON on stdout and exits 2, so empty stdout means
        # it never got that far: a missing cx_Oracle, an Oracle client DLL that would not load.
        # stderr is the only thing that says which, so it must reach the operator.
        raise LegacyOracleError(
            f"Legacy Oracle tool produced no output (exit {completed.returncode}): "
            f"{(completed.stderr or '').strip()[:800] or 'no stderr'}"
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise LegacyOracleError(
            f"Legacy Oracle tool did not answer with JSON (exit {completed.returncode}): "
            f"{stdout[:800]}"
        ) from exc


def subprocess_argv(sql_access: dict[str, Any]) -> list[str]:
    """How to start the legacy tool on this host, from config.

    ``launcher`` states the whole command when the tool is not reached as a local script — a
    container image, say (``["docker","run","--rm","-i","legacy-oracle-8i"]``). Otherwise the
    tool is a script under ``tool_dir``, run by the Python it ships with. Both forms end in
    ``--request -``: the tool reads one JSON object on stdin whichever way it was started.
    """
    launcher = sql_access.get("launcher")
    if launcher:
        if not isinstance(launcher, list) or not all(str(part).strip() for part in launcher):
            raise LegacyOracleError("sql_access.launcher must be a non-empty list of strings.")
        return [str(part) for part in launcher] + ["--request", "-"]

    tool_dir = _tool_dir(sql_access)
    python_exe = Path(str(sql_access.get("python_exe") or DEFAULT_PYTHON_EXE))
    if not python_exe.is_absolute():
        python_exe = tool_dir / python_exe
    script = tool_dir / LEGACY_CLI_SCRIPT
    if not script.is_file():
        raise LegacyOracleError(
            f"Legacy Oracle tool not found at {script}. Set sql_access.tool_dir, or use "
            "sql_access.method 'api' from a host that does not carry the tool."
        )
    if not python_exe.is_file():
        raise LegacyOracleError(
            f"Legacy Oracle interpreter not found at {python_exe}. The tool needs its own "
            "Python 2.7 32-bit runtime (tools/python32_legacy/README.md explains how it is "
            "provisioned); it is deliberately not part of the db_ops venv."
        )
    return [str(python_exe), str(script)]


def _tool_dir(sql_access: dict[str, Any]) -> Path:
    tool_dir = Path(str(sql_access.get("tool_dir") or DEFAULT_TOOL_DIR))
    return tool_dir if tool_dir.is_absolute() else TOOL_ROOT / tool_dir
