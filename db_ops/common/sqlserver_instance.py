"""Export one SQL Server instance's server-level metadata as SQL, and replay it onto another.

**The problem this exists for.** Oracle and PostgreSQL are backed up *physically*, whole-instance:
RMAN `DUPLICATE` rebuilds from the datafiles and Oracle keeps users, roles, grants and password
hashes inside the database; `pg_basebackup` copies the entire cluster and roles live in
``pg_authid`` inside ``PGDATA``. Restore either and the server answers as the source did.

SQL Server is backed up *logically*, per database — ``assets/backup/sqlserver/mssql_backup_database.sh``
selects ``WHERE database_id > 4``, so ``master``, ``msdb`` and ``model`` are deliberately excluded.
Everything that lives in them is therefore absent after a restore: logins, server roles and
permissions, credentials, linked servers, endpoints, ``sp_configure``, Database Mail, and the whole
of SQL Agent. The restored instance has the data and none of the machinery that makes it usable.

The sharpest symptom is **orphaned users**. A restored user database keeps its
``sys.database_principals`` rows carrying the *source's* SIDs; with no server login of the same SID
nobody can connect. That is why this module preserves login SIDs rather than merely recreating
logins by name — recreating by name produces a login that exists and still cannot log in.

**Why generated SQL and not a backup of master/msdb.** Restoring those databases across versions is
not supported by SQL Server, and they carry host-specific state anyway. Deterministic SQL can be
read, diffed, held in review, and applied to a *newer* build — which is the actual requirement.

**What this module does not do.** It does not touch user-database FULL/DIFF/LOG behaviour, and it
is not called by the backup or restore workflow yet: it is the capability, sitting in ``common``
where the next caller finds it. Wiring it into a workflow is a separate, opt-in step.

Three entry points, each taking one JSON object (:mod:`db_ops.common.cli` fronts them):

* :func:`export_instance` — read the instance, write the ``server/`` artifact set. Read-only.
* :func:`replay_instance` — apply a bundle to a target, in dependency order, with gates.
* :func:`verify_instance` — compare a target against a bundle. Read-only.

What is portable between two instances is a per-estate decision, so it lives in
``data/sqlserver_instance_policy.json``, not here.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from db_ops.common import data_sources, host_ops, sql_run
from db_ops.common.evidence import FAIL, OK, SKIP, WARN, GateReport
from db_ops.lib.json_io import load_json_file
from db_ops.lib.instance_bundle import (  # noqa: F401 - one definition, see that module
    MANIFEST_NAME,
    POST_DATABASE,
    PRE_DATABASE,
    SERVER_DIR,
    artifacts_in_order,
)
from db_ops.lib.paths import TOOL_ROOT  # noqa: F401 - one definition, see that module

POLICY_FILENAME = data_sources.SQLSERVER_INSTANCE_POLICY_FILENAME


class SqlServerInstanceError(RuntimeError):
    """Any request this module refuses: bad payload, wrong engine, unusable bundle."""


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #

def load_policy(data_dir: str | Path | None = None) -> dict[str, Any]:
    """``sqlserver_instance_policy.json``, as this module's error type.

    The read itself is ``data_sources.load_sqlserver_instance_policy`` — the data folder has one
    reader, and ``backup_restore`` calls it directly while validating a config block. All this
    adds is the translation, so a caller of *this* module still catches
    :class:`SqlServerInstanceError` for everything it can refuse.
    """
    try:
        return data_sources.load_sqlserver_instance_policy(data_dir=data_dir)
    except FileNotFoundError as exc:
        raise SqlServerInstanceError(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Connection + instance facts
# --------------------------------------------------------------------------- #

def _connect(request: dict[str, Any], *, data_dir: str | Path | None, timeout_seconds: int = 30,
             autocommit: bool = False):
    """Connect to the instance's ``master`` as the login the request (or the inventory) names.

    ``master`` deliberately: every catalog this module reads is server-level, and a target's
    ``db_name`` is a service label rather than a database (see ``metrics/executor.py``).
    """
    if not isinstance(request, dict):
        raise SqlServerInstanceError("request must be a JSON object.")
    try:
        resolved = sql_run.resolve_sqlserver_target(
            str(request.get("target") or ""),
            data_dir=data_dir,
            database="master",
            credential_name=str(request.get("credential_name") or ""),
        )
    except sql_run.SqlRunError as exc:
        raise SqlServerInstanceError(str(exc)) from exc
    if str(resolved.get("db_type")) != "sqlserver":
        raise SqlServerInstanceError(
            f"{request.get('target')} is db_type={resolved.get('db_type')}; instance-metadata "
            "export applies to SQL Server only. Oracle and PostgreSQL carry this state inside "
            "their physical backups already."
        )
    return sql_run.connect_target(resolved, timeout_seconds=timeout_seconds,
                                 autocommit=autocommit), resolved


def _rows(cursor, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Query -> list of dicts with raw driver values kept (bytes stay bytes: SIDs and password
    hashes must be rendered as ``0x...`` literals, and stringifying them first destroys them)."""
    cursor.execute(sql, params) if params else cursor.execute(sql)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# Every SERVERPROPERTY is CAST to NVARCHAR. It returns `sql_variant`, which the ODBC driver
# cannot hand to pyodbc at all — the whole export died on the first query with
# "ODBC SQL type -16 is not yet supported. column-index=2" (EngineEdition, the numeric one).
# Casting server-side is the fix; there is no client-side way to read a raw sql_variant here.
_SERVER_INFO_SQL = """
SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(128))  AS build,
       CAST(SERVERPROPERTY('Edition')        AS NVARCHAR(256))  AS edition,
       CAST(SERVERPROPERTY('EngineEdition')  AS NVARCHAR(16))   AS engine_edition,
       CAST(SERVERPROPERTY('Collation')      AS NVARCHAR(256))  AS collation,
       CAST(SERVERPROPERTY('MachineName')    AS NVARCHAR(256))  AS machine_name,
       CAST(SERVERPROPERTY('InstanceName')   AS NVARCHAR(256))  AS instance_name,
       CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS NVARCHAR(16)) AS windows_auth_only,
       CAST(@@VERSION AS NVARCHAR(1024))                        AS version_text;
"""


def major_version(build: str) -> int:
    """The major version number out of a ``16.0.4235.1`` build string, 0 when unreadable."""
    match = re.match(r"^\s*(\d+)", str(build or ""))
    return int(match.group(1)) if match else 0


def server_info(cursor) -> dict[str, Any]:
    info = _rows(cursor, _SERVER_INFO_SQL)[0]
    return {key: (None if value is None else str(value)) for key, value in info.items()}


def _has_agent(info: dict[str, Any]) -> bool:
    """Express has no SQL Agent, so the Agent artifacts are skipped rather than attempted.

    EngineEdition 4 is Express; 5 is Azure SQL Database, which has no Agent either and no
    server-level catalog worth replaying.
    """
    return str(info.get("engine_edition") or "") not in {"4", "5"}


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #

def _hexlify(value: Any) -> str:
    """A ``0x...`` literal for a SID or password hash.

    Drivers hand these back as ``bytes`` (pyodbc) or already-hex ``str`` depending on the path,
    and getting it wrong produces a login that is created successfully with the *wrong* SID —
    which looks like success and orphans every user in the restored databases.
    """
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex().upper()
    text = str(value).strip()
    if text.lower().startswith("0x"):
        return "0x" + text[2:].upper()
    return "0x" + text.upper()


def _quote_name(name: Any) -> str:
    """Bracket-quote an identifier, doubling any ``]`` inside it."""
    return "[" + str(name or "").replace("]", "]]") + "]"


def _quote_string(value: Any) -> str:
    """Single-quote a literal, doubling any ``'`` inside it."""
    return "'" + str(value if value is not None else "").replace("'", "''") + "'"


def _secret_ref(policy: dict[str, Any], prefix: str, kind: str, name: str) -> str:
    """The placeholder that stands in for a secret SQL Server will not hand over.

    Credential secrets, linked-server remote passwords, proxy passwords and Database Mail SMTP
    passwords are encrypted with the service master key and have no read path. The export names
    the reference; it never invents the value, and replay resolves it or refuses to run.
    """
    fmt = str((policy.get("secrets") or {}).get("placeholder_format") or "{{secret:%s}}")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", f"{prefix}_{kind}_{name}").strip("_").upper()
    return fmt % slug


def _header(artifact: str, info: dict[str, Any], *, source: str) -> list[str]:
    """The comment block every artifact opens with.

    No timestamp in the body on purpose: two exports of an unchanged instance must be
    byte-identical, so that a diff between them means a real change rather than the clock moving.
    """
    return [
        f"-- db_ops instance metadata: {artifact}",
        f"-- source instance : {source}",
        f"-- source build    : {info.get('build')} ({info.get('edition')})",
        f"-- source collation: {info.get('collation')}",
        "-- Generated. Every statement is guarded so a re-run converges instead of failing;",
        "-- nothing here drops an existing object. Statements are separated by GO so that one",
        "-- failure costs one statement rather than the whole file.",
        "",
    ]


def _render(artifact: str, info: dict[str, Any], *, source: str, body: list[str]) -> str:
    """Header plus one batch per statement.

    The GO separators are the point. Without them a file is a single batch, and the executor's
    "one failing batch does not abort the rest" degrades into "one failing statement aborts
    everything": the first live trial lost all 22 sp_configure statements to a single rejected
    one, and all four role-membership statements to a single illegal principal.
    """
    statements = [line for line in body if line.strip()]
    if not statements:
        statements = [f"-- nothing to replay for {artifact}"]
    return "\n".join(_header(artifact, info, source=source)) + "\nGO\n".join(statements) + "\nGO\n"


# --------------------------------------------------------------------------- #
# Exporters — one per artifact, each returning the SQL text
# --------------------------------------------------------------------------- #

_LOGINS_SQL = """
SELECT p.name, p.type_desc, p.sid, p.is_disabled, p.default_database_name,
       p.default_language_name, l.password_hash, l.is_policy_checked, l.is_expiration_checked
  FROM sys.server_principals AS p
  LEFT JOIN sys.sql_logins AS l ON l.principal_id = p.principal_id
 WHERE p.type IN ('S', 'U', 'G')
 ORDER BY p.name;
"""


def _create_login_random_password(guard: str, quoted: str, tail: list[str]) -> str:
    """``CREATE LOGIN`` with a password nobody knows — as T-SQL that actually parses.

    The obvious spelling, ``WITH PASSWORD = NEWID()``, is not valid: ``PASSWORD`` takes a string
    literal, not an expression, so SQL Server rejects the whole statement at parse time with
    ``Incorrect syntax near 'NEWID'``. Nothing caught it because the branch only runs when the
    source login's hash could not be read, and on the instance this was written against it always
    could. On 192.0.2.248 — exported by a login without VIEW ANY DEFINITION — every one of the
    35 SQL logins took this branch and every one failed, so the restored database's users stayed
    orphaned while the bundle reported itself replayed.

    So the password is generated into a variable and the statement executed dynamically. The
    GUID gives the entropy; the four-character suffix is what satisfies CHECK_POLICY = ON, where
    a hex-only string can fail complexity and take the login with it.
    """
    suffix = (", " + ", ".join(tail)) if tail else ""
    # Doubled for the T-SQL string literal below: a login name may legally contain an apostrophe,
    # and one that did would otherwise end the literal early and execute whatever followed.
    inner = f"CREATE LOGIN {quoted} WITH PASSWORD = '".replace("'", "''")
    closing = f"'{suffix};".replace("'", "''")
    return (
        f"{guard}\nBEGIN\n"
        "    DECLARE @pwd nvarchar(72) = REPLACE(CONVERT(nvarchar(64), NEWID()), '-', '') + N'Aa1!';\n"
        f"    DECLARE @sql nvarchar(max) = N'{inner}' + @pwd + N'{closing}';\n"
        "    EXEC sys.sp_executesql @sql;\n"
        "END"
    )


def _export_logins(cursor, policy, info, prefix) -> str:
    rule = policy.get("logins") or {}
    skip_prefixes = tuple(rule.get("skip_name_prefixes") or ())
    skip_names = {str(name).lower() for name in (rule.get("skip_names") or ())}
    keep_sid = bool(rule.get("preserve_sid", True))
    keep_hash = bool(rule.get("preserve_password_hash", True))

    lines: list[str] = []
    for row in _rows(cursor, _LOGINS_SQL):
        name = str(row["name"])
        if name.lower() in skip_names or name.startswith(skip_prefixes):
            continue
        quoted = _quote_name(name)
        guard = f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = {_quote_string(name)})"
        if str(row["type_desc"]) == "SQL_LOGIN":
            tail = []
            if keep_sid and row["sid"] is not None:
                tail.append(f"SID = {_hexlify(row['sid'])}")
            if row["default_database_name"]:
                tail.append(f"DEFAULT_DATABASE = {_quote_name(row['default_database_name'])}")
            tail.append("CHECK_POLICY = " + ("ON" if row["is_policy_checked"] else "OFF"))
            tail.append("CHECK_EXPIRATION = " + ("ON" if row["is_expiration_checked"] else "OFF"))
            if keep_hash and row["password_hash"] is not None:
                statement = (f"CREATE LOGIN {quoted} WITH "
                             f"PASSWORD = {_hexlify(row['password_hash'])} HASHED, "
                             + ", ".join(tail) + ";")
                lines.append(f"{guard}\n    {statement}")
            else:
                # No hash available (or preservation switched off) - which is the ordinary case
                # for an export taken by a login without VIEW ANY DEFINITION, not an exotic one.
                # A random password beats a known one: the login exists so its SID resolves, and
                # it cannot be logged into until an operator sets a password deliberately.
                lines.append(_create_login_random_password(guard, quoted, tail))
        else:
            # Windows login or group: the SID belongs to the domain, so it is never stated.
            # On a target in another domain this simply will not resolve — replay reports it.
            lines.append(f"{guard}\n    CREATE LOGIN {quoted} FROM WINDOWS;")
        if row["is_disabled"]:
            lines.append(f"ALTER LOGIN {quoted} DISABLE;")
        lines.append("")
    return _render("logins", info, source=prefix, body=lines)


_SERVER_ROLES_SQL = """
SELECT r.name AS role_name, r.is_fixed_role, m.name AS member_name, o.name AS owner_name
  FROM sys.server_principals AS r
  LEFT JOIN sys.server_role_members AS rm ON rm.role_principal_id = r.principal_id
  LEFT JOIN sys.server_principals AS m ON m.principal_id = rm.member_principal_id
  LEFT JOIN sys.server_principals AS o ON o.principal_id = r.owning_principal_id
 WHERE r.type = 'R'
 ORDER BY r.name, m.name;
"""


#: ``public`` is a built-in server role that every instance already has, but SQL Server reports
#: ``is_fixed_role = 0`` for it — so the "not fixed, therefore user-defined" test lets it through
#: and the export emits ``CREATE SERVER ROLE [public]``. Guarded, so it is a no-op rather than an
#: error, but it is still a statement asserting something untrue about the instance.
_BUILT_IN_SERVER_ROLES = frozenset({"public"})


def _export_server_roles(cursor, policy, info, prefix) -> str:
    rule = policy.get("logins") or {}
    skip_prefixes = tuple(rule.get("skip_name_prefixes") or ())
    skip_names = {str(name).lower() for name in (rule.get("skip_names") or ())}
    lines: list[str] = []
    created: set[str] = set()
    for row in _rows(cursor, _SERVER_ROLES_SQL):
        role = str(row["role_name"])
        if (not row["is_fixed_role"] and role.lower() not in _BUILT_IN_SERVER_ROLES
                and role not in created):
            created.add(role)
            lines.append(
                f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = {_quote_string(role)} AND type = 'R')\n"
                f"    CREATE SERVER ROLE {_quote_name(role)}"
                + (f" AUTHORIZATION {_quote_name(row['owner_name'])}" if row["owner_name"] else "")
                + ";"
            )
        member = row["member_name"]
        # `ALTER SERVER ROLE ... ADD MEMBER [sa]` is rejected outright: sa is a *special*
        # principal, not an ordinary login (error 15405). The logins exporter already skipped
        # these; membership has to skip the same set or the file dies on them.
        if member and str(member).lower() not in skip_names and not str(member).startswith(skip_prefixes):
            lines.append(
                f"IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = {_quote_string(member)})\n"
                f"    ALTER SERVER ROLE {_quote_name(role)} ADD MEMBER {_quote_name(member)};"
            )
    return _render("server_roles", info, source=prefix, body=lines)


_PERMISSIONS_SQL = """
SELECT pe.state_desc, pe.permission_name, pe.class_desc, pr.name AS grantee
  FROM sys.server_permissions AS pe
  JOIN sys.server_principals AS pr ON pr.principal_id = pe.grantee_principal_id
 WHERE pr.name NOT LIKE '##%'
   AND pe.class_desc = 'SERVER'
 ORDER BY pr.name, pe.permission_name;
"""


def _export_permissions(cursor, policy, info, prefix) -> str:
    lines: list[str] = []
    for row in _rows(cursor, _PERMISSIONS_SQL):
        state = "GRANT" if str(row["state_desc"]) == "GRANT_WITH_GRANT_OPTION" else str(row["state_desc"])
        suffix = " WITH GRANT OPTION" if str(row["state_desc"]) == "GRANT_WITH_GRANT_OPTION" else ""
        lines.append(
            f"IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = {_quote_string(row['grantee'])})\n"
            f"    {state} {row['permission_name']} TO {_quote_name(row['grantee'])}{suffix};"
        )
    return _render("permissions", info, source=prefix, body=lines)


_CREDENTIALS_SQL = """
SELECT name, credential_identity FROM sys.credentials ORDER BY name;
"""


def _export_credentials(cursor, policy, info, prefix) -> tuple[str, list[str]]:
    lines: list[str] = []
    refs: list[str] = []
    for row in _rows(cursor, _CREDENTIALS_SQL):
        name = str(row["name"])
        ref = _secret_ref(policy, prefix, "credential", name)
        refs.append(ref)
        lines.append(
            f"IF NOT EXISTS (SELECT 1 FROM sys.credentials WHERE name = {_quote_string(name)})\n"
            f"    CREATE CREDENTIAL {_quote_name(name)} WITH IDENTITY = {_quote_string(row['credential_identity'])},\n"
            f"        SECRET = {_quote_string(ref)};"
        )
    return _render("credentials", info, source=prefix, body=lines), refs


_LINKED_SERVERS_SQL = """
SELECT s.name, s.product, s.provider, s.data_source, s.catalog, s.is_rpc_out_enabled,
       s.is_data_access_enabled
  FROM sys.servers AS s
 WHERE s.is_linked = 1
 ORDER BY s.name;
"""

_LINKED_LOGINS_SQL = """
SELECT s.name AS server_name, l.remote_name, l.uses_self_credential, p.name AS local_login
  FROM sys.linked_logins AS l
  JOIN sys.servers AS s ON s.server_id = l.server_id
  LEFT JOIN sys.server_principals AS p ON p.principal_id = l.local_principal_id
 WHERE s.is_linked = 1
 ORDER BY s.name, p.name;
"""


def _export_linked_servers(cursor, policy, info, prefix) -> tuple[str, list[str]]:
    lines: list[str] = []
    refs: list[str] = []
    for row in _rows(cursor, _LINKED_SERVERS_SQL):
        name = str(row["name"])
        lines.append(
            f"IF NOT EXISTS (SELECT 1 FROM sys.servers WHERE name = {_quote_string(name)} AND is_linked = 1)\n"
            f"    EXEC sp_addlinkedserver @server = {_quote_string(name)}, "
            f"@srvproduct = {_quote_string(row['product'])}, "
            f"@provider = {_quote_string(row['provider'])}, "
            f"@datasrc = {_quote_string(row['data_source'])}"
            + (f", @catalog = {_quote_string(row['catalog'])}" if row["catalog"] else "")
            + ";"
        )
    for row in _rows(cursor, _LINKED_LOGINS_SQL):
        server = str(row["server_name"])
        local = row["local_login"]
        if row["uses_self_credential"]:
            lines.append(
                f"EXEC sp_addlinkedsrvlogin @rmtsrvname = {_quote_string(server)}, @useself = 'TRUE'"
                + (f", @locallogin = {_quote_string(local)}" if local else "")
                + ";"
            )
            continue
        ref = _secret_ref(policy, prefix, "linked_server_login", f"{server}_{row['remote_name'] or 'default'}")
        refs.append(ref)
        lines.append(
            f"EXEC sp_addlinkedsrvlogin @rmtsrvname = {_quote_string(server)}, @useself = 'FALSE'"
            + (f", @locallogin = {_quote_string(local)}" if local else "")
            + f", @rmtuser = {_quote_string(row['remote_name'])}, @rmtpassword = {_quote_string(ref)};"
        )
    return (
        _render("linked_servers", info, source=prefix, body=lines),
        refs,
    )


_ENDPOINTS_SQL = """
SELECT e.name, e.protocol_desc, e.type_desc, e.state_desc, t.port
  FROM sys.endpoints AS e
  LEFT JOIN sys.tcp_endpoints AS t ON t.endpoint_id = e.endpoint_id
 WHERE e.endpoint_id > 65535        -- system endpoints (TSQL Default TCP, ...) are not portable
 ORDER BY e.name;
"""


def _export_endpoints(cursor, policy, info, prefix) -> str:
    lines: list[str] = []
    for row in _rows(cursor, _ENDPOINTS_SQL):
        name = str(row["name"])
        lines.append(
            f"-- endpoint {name}: {row['type_desc']} over {row['protocol_desc']}, port {row['port']}, "
            f"state {row['state_desc']}.\n"
            "-- A certificate-backed endpoint also needs its certificate moved as a FILE with its\n"
            "-- own passphrase; that is a manual prerequisite, not something SQL can carry.\n"
            f"IF NOT EXISTS (SELECT 1 FROM sys.endpoints WHERE name = {_quote_string(name)})\n"
            f"    CREATE ENDPOINT {_quote_name(name)} STATE = {row['state_desc']}\n"
            f"        AS TCP (LISTENER_PORT = {int(row['port'] or 0)})\n"
            f"        FOR {row['type_desc']} (ROLE = ALL);"
        )
    return _render("endpoints", info, source=prefix, body=lines)


_CONFIG_SQL = """
SELECT name, CAST(value AS BIGINT) AS value, CAST(value_in_use AS BIGINT) AS value_in_use,
       is_dynamic, is_advanced
  FROM sys.configurations
 ORDER BY name;
"""


def _export_sp_configure(cursor, policy, info, prefix) -> tuple[str, list[str]]:
    rule = policy.get("sp_configure") or {}
    portable = {str(name).lower() for name in (rule.get("portable") or ())}
    host_specific = {str(name).lower() for name in (rule.get("host_specific") or ())}
    # Advanced options have to be visible before most of these can be set, so the file turns the
    # switch on -- and turns it back to whatever the SOURCE had at the end. Leaving it on is a
    # change the bundle never intended to make: the first live trial changed exactly one value on
    # the target, and it was this one.
    lines = ["EXEC sp_configure 'show advanced options', 1;", "RECONFIGURE;"]
    skipped: list[str] = []
    advanced_source_value = 0
    for row in _rows(cursor, _CONFIG_SQL):
        name = str(row["name"]).strip()
        value = row["value_in_use"]
        if name.lower() == "show advanced options":
            advanced_source_value = int(value or 0)
        statement = f"EXEC sp_configure {_quote_string(name)}, {int(value or 0)};"
        if name.lower() in portable:
            lines.append(statement)
            continue
        reason = "host-specific" if name.lower() in host_specific else "unclassified"
        skipped.append(f"{name} ({reason}, source value {value})")
        # Commented out, with the source value visible: an operator reviewing a rebuild wants to
        # see what the source had without the replay imposing it on different hardware.
        lines.append(f"-- {statement}  -- SKIPPED: {reason}; source value {value}")
    lines += [
        "RECONFIGURE;",
        f"EXEC sp_configure 'show advanced options', {advanced_source_value};",
        "RECONFIGURE;",
    ]
    return _render("sp_configure", info, source=prefix, body=lines), skipped


_MODEL_SQL = """
SELECT recovery_model_desc, page_verify_option_desc, is_auto_create_stats_on,
       is_auto_update_stats_on, is_auto_update_stats_async_on, is_auto_shrink_on,
       is_auto_close_on, is_parameterization_forced, is_date_correlation_on,
       is_trustworthy_on, is_read_committed_snapshot_on, snapshot_isolation_state_desc
  FROM sys.databases WHERE name = 'model';
"""

_MODEL_SETTERS = {
    "recovery_model_desc": "RECOVERY %s",
    "page_verify_option_desc": "PAGE_VERIFY %s",
    "is_auto_create_stats_on": "AUTO_CREATE_STATISTICS %s",
    "is_auto_update_stats_on": "AUTO_UPDATE_STATISTICS %s",
    "is_auto_update_stats_async_on": "AUTO_UPDATE_STATISTICS_ASYNC %s",
    "is_auto_shrink_on": "AUTO_SHRINK %s",
    "is_auto_close_on": "AUTO_CLOSE %s",
    "is_parameterization_forced": "PARAMETERIZATION %s",
    "is_date_correlation_on": "DATE_CORRELATION_OPTIMIZATION %s",
    # is_trustworthy_on is deliberately absent: "Cannot alter the trustworthy state of the
    # model or tempdb databases" (error 15309). SQL Server reports the column and refuses
    # the ALTER, so exporting it produces a statement that can only ever fail.
}


def _export_model_options(cursor, policy, info, prefix) -> str:
    portable = {str(name) for name in ((policy.get("model_options") or {}).get("portable") or ())}
    rows = _rows(cursor, _MODEL_SQL)
    lines: list[str] = []
    for column, template in _MODEL_SETTERS.items():
        if column not in portable or not rows:
            continue
        value = rows[0][column]
        if column.startswith("is_"):
            rendered = "ON" if value else "OFF"
            if column == "is_parameterization_forced":
                rendered = "FORCED" if value else "SIMPLE"
        else:
            rendered = str(value)
        lines.append(f"ALTER DATABASE [model] SET {template % rendered};")
    # File size and growth are absent on purpose: a new instance's disks are its own.
    return _render("model_options", info, source=prefix, body=lines)


_OPERATORS_SQL = "SELECT name, enabled, email_address, pager_address, weekday_pager_start_time FROM msdb.dbo.sysoperators ORDER BY name;"


def _export_operators(cursor, policy, info, prefix) -> str:
    lines: list[str] = []
    for row in _rows(cursor, _OPERATORS_SQL):
        name = str(row["name"])
        lines.append(
            f"IF NOT EXISTS (SELECT 1 FROM msdb.dbo.sysoperators WHERE name = {_quote_string(name)})\n"
            f"    EXEC msdb.dbo.sp_add_operator @name = {_quote_string(name)}, "
            f"@enabled = {1 if row['enabled'] else 0}, "
            f"@email_address = {_quote_string(row['email_address'])};"
        )
    return _render("operators", info, source=prefix, body=lines)


_PROXIES_SQL = """
SELECT p.name, p.enabled, c.name AS credential_name
  FROM msdb.dbo.sysproxies AS p
  LEFT JOIN sys.credentials AS c ON c.credential_id = p.credential_id
 ORDER BY p.name;
"""


def _export_proxies(cursor, policy, info, prefix) -> str:
    lines: list[str] = []
    for row in _rows(cursor, _PROXIES_SQL):
        name = str(row["name"])
        lines.append(
            f"IF NOT EXISTS (SELECT 1 FROM msdb.dbo.sysproxies WHERE name = {_quote_string(name)})\n"
            f"    EXEC msdb.dbo.sp_add_proxy @proxy_name = {_quote_string(name)}, "
            f"@credential_name = {_quote_string(row['credential_name'])}, "
            f"@enabled = {1 if row['enabled'] else 0};"
        )
    return _render("proxies", info, source=prefix, body=lines)


_SCHEDULES_SQL = """
SELECT name, enabled, freq_type, freq_interval, freq_subday_type, freq_subday_interval,
       freq_relative_interval, freq_recurrence_factor, active_start_date, active_end_date,
       active_start_time, active_end_time
  FROM msdb.dbo.sysschedules ORDER BY name;
"""


def _export_agent_schedules(cursor, policy, info, prefix) -> str:
    lines: list[str] = []
    for row in _rows(cursor, _SCHEDULES_SQL):
        name = str(row["name"])
        args = ", ".join(
            f"@{key} = {int(row[key] or 0)}"
            for key in ("freq_type", "freq_interval", "freq_subday_type", "freq_subday_interval",
                        "freq_relative_interval", "freq_recurrence_factor", "active_start_date",
                        "active_end_date", "active_start_time", "active_end_time")
        )
        lines.append(
            f"IF NOT EXISTS (SELECT 1 FROM msdb.dbo.sysschedules WHERE name = {_quote_string(name)})\n"
            f"    EXEC msdb.dbo.sp_add_schedule @schedule_name = {_quote_string(name)}, "
            f"@enabled = {1 if row['enabled'] else 0}, {args};"
        )
    return _render("agent_schedules", info, source=prefix, body=lines)


_JOBS_SQL = """
SELECT j.job_id, j.name, j.enabled, j.description, j.notify_level_email,
       c.name AS category_name, p.name AS owner_name, o.name AS notify_operator
  FROM msdb.dbo.sysjobs AS j
  LEFT JOIN msdb.dbo.syscategories AS c ON c.category_id = j.category_id
  LEFT JOIN sys.server_principals AS p ON p.sid = j.owner_sid
  LEFT JOIN msdb.dbo.sysoperators AS o ON o.id = j.notify_email_operator_id
 ORDER BY j.name;
"""

_JOBSTEPS_SQL = """
SELECT s.job_id, s.step_id, s.step_name, s.subsystem, s.command, s.database_name,
       s.on_success_action, s.on_fail_action, s.retry_attempts, s.retry_interval,
       -- Action 4 means "go to step", and the step it goes to is held here. Without these two the
       -- exported job is not the job: sp_add_jobstep is handed action 4 with no destination and
       -- refuses it (error 14235), so any multi-step job with branching failed to replay.
       s.on_success_step_id, s.on_fail_step_id,
       pr.name AS proxy_name
  FROM msdb.dbo.sysjobsteps AS s
  LEFT JOIN msdb.dbo.sysproxies AS pr ON pr.proxy_id = s.proxy_id
 ORDER BY s.job_id, s.step_id;
"""

#: sysjobsteps.on_success_action / on_fail_action value for "Go to step".
_GOTO_STEP = 4

_JOBSCHEDULES_SQL = """
SELECT js.job_id, s.name AS schedule_name
  FROM msdb.dbo.sysjobschedules AS js
  JOIN msdb.dbo.sysschedules AS s ON s.schedule_id = js.schedule_id;
"""


def _export_agent_jobs(cursor, policy, info, prefix) -> str:
    rule = policy.get("agent_jobs") or {}
    keep_id = bool(rule.get("preserve_job_id", True))
    default_owner = str(rule.get("default_owner") or "sa")
    skip_patterns = [str(pattern) for pattern in (rule.get("skip_categories") or ())]

    steps: dict[str, list[dict[str, Any]]] = {}
    for row in _rows(cursor, _JOBSTEPS_SQL):
        steps.setdefault(str(row["job_id"]), []).append(row)
    schedules: dict[str, list[str]] = {}
    for row in _rows(cursor, _JOBSCHEDULES_SQL):
        schedules.setdefault(str(row["job_id"]), []).append(str(row["schedule_name"]))

    lines: list[str] = []
    for job in _rows(cursor, _JOBS_SQL):
        category = str(job["category_name"] or "")
        if any(_matches(category, pattern) for pattern in skip_patterns):
            lines.append(f"-- SKIPPED job {job['name']}: category {category} is not portable.")
            continue
        name = str(job["name"])
        job_id = str(job["job_id"])
        owner = job["owner_name"] or default_owner
        args = [f"@job_name = {_quote_string(name)}", f"@enabled = {1 if job['enabled'] else 0}",
                f"@owner_login_name = {_quote_string(owner)}"]
        if job["description"]:
            args.append(f"@description = {_quote_string(job['description'])}")
        if keep_id:
            # Preserving job_id keeps history correlatable across a rebuild and makes replay
            # idempotent. A collision with a different job of the same id is gated at replay
            # rather than silently overwritten.
            args.append(f"@job_id = {_quote_string(job_id)} OUTPUT")
        # One job is ONE batch. Its statements sit inside an IF ... BEGIN ... END block, and
        # the renderer puts a GO between every element of `lines` — so emitting the job as
        # several entries split that block apart: sp_add_job ran alone, then every step ran
        # as its own batch against a job the split had prevented from being created.
        job_lines = [
            f"IF NOT EXISTS (SELECT 1 FROM msdb.dbo.sysjobs WHERE name = {_quote_string(name)})",
            "BEGIN",
            "    EXEC msdb.dbo.sp_add_job " + ", ".join(a for a in args if "OUTPUT" not in a) + ";",
        ]
        for step in steps.get(job_id, []):
            step_args = [
                f"@job_name = {_quote_string(name)}",
                f"@step_name = {_quote_string(step['step_name'])}",
                f"@step_id = {int(step['step_id'] or 1)}",
                f"@subsystem = {_quote_string(step['subsystem'])}",
                f"@command = {_quote_string(step['command'])}",
                f"@on_success_action = {int(step['on_success_action'] or 1)}",
                f"@on_fail_action = {int(step['on_fail_action'] or 2)}",
                f"@retry_attempts = {int(step['retry_attempts'] or 0)}",
                f"@retry_interval = {int(step['retry_interval'] or 0)}",
            ]
            # Only for action 4: sp_add_jobstep rejects a step id for any other action, so
            # emitting it unconditionally would break the steps that currently work.
            if int(step["on_success_action"] or 1) == _GOTO_STEP and step["on_success_step_id"]:
                step_args.append(f"@on_success_step_id = {int(step['on_success_step_id'])}")
            if int(step["on_fail_action"] or 2) == _GOTO_STEP and step["on_fail_step_id"]:
                step_args.append(f"@on_fail_step_id = {int(step['on_fail_step_id'])}")
            if step["database_name"]:
                step_args.append(f"@database_name = {_quote_string(step['database_name'])}")
            if step["proxy_name"]:
                step_args.append(f"@proxy_name = {_quote_string(step['proxy_name'])}")
            job_lines.append("    EXEC msdb.dbo.sp_add_jobstep " + ", ".join(step_args) + ";")
        for schedule in schedules.get(job_id, []):
            job_lines.append(
                f"    EXEC msdb.dbo.sp_attach_schedule @job_name = {_quote_string(name)}, "
                f"@schedule_name = {_quote_string(schedule)};"
            )
        job_lines.append(f"    EXEC msdb.dbo.sp_add_jobserver @job_name = {_quote_string(name)};")
        job_lines.append("END")
        lines.append("\n".join(job_lines))
    return _render("agent_jobs", info, source=prefix, body=lines)


_ALERTS_SQL = """
SELECT a.name, a.enabled, a.message_id, a.severity, a.database_name, a.job_id,
       o.name AS operator_name
  FROM msdb.dbo.sysalerts AS a
  LEFT JOIN msdb.dbo.sysnotifications AS n ON n.alert_id = a.id
  LEFT JOIN msdb.dbo.sysoperators AS o ON o.id = n.operator_id
 ORDER BY a.name;
"""


def _export_alerts(cursor, policy, info, prefix) -> str:
    lines: list[str] = []
    for row in _rows(cursor, _ALERTS_SQL):
        name = str(row["name"])
        args = [f"@name = {_quote_string(name)}", f"@enabled = {1 if row['enabled'] else 0}",
                f"@message_id = {int(row['message_id'] or 0)}", f"@severity = {int(row['severity'] or 0)}"]
        if row["database_name"]:
            args.append(f"@database_name = {_quote_string(row['database_name'])}")
        lines.append(
            f"IF NOT EXISTS (SELECT 1 FROM msdb.dbo.sysalerts WHERE name = {_quote_string(name)})\n"
            f"    EXEC msdb.dbo.sp_add_alert " + ", ".join(args) + ";"
        )
        if row["operator_name"]:
            lines.append(
                f"EXEC msdb.dbo.sp_add_notification @alert_name = {_quote_string(name)}, "
                f"@operator_name = {_quote_string(row['operator_name'])}, @notification_method = 1;"
            )
    return _render("alerts", info, source=prefix, body=lines)


_DBMAIL_ACCOUNT_SQL = """
SELECT a.name, a.description, a.email_address, a.display_name, a.replyto_address,
       s.servername, s.port, s.enable_ssl, s.username
  FROM msdb.dbo.sysmail_account AS a
  LEFT JOIN msdb.dbo.sysmail_server AS s ON s.account_id = a.account_id
 ORDER BY a.name;
"""

_DBMAIL_PROFILE_SQL = """
SELECT p.name AS profile_name, a.name AS account_name, pa.sequence_number
  FROM msdb.dbo.sysmail_profile AS p
  LEFT JOIN msdb.dbo.sysmail_profileaccount AS pa ON pa.profile_id = p.profile_id
  LEFT JOIN msdb.dbo.sysmail_account AS a ON a.account_id = pa.account_id
 ORDER BY p.name, pa.sequence_number;
"""


def _export_db_mail(cursor, policy, info, prefix) -> tuple[str, list[str]]:
    lines: list[str] = []
    refs: list[str] = []
    for row in _rows(cursor, _DBMAIL_ACCOUNT_SQL):
        name = str(row["name"])
        args = [
            f"@account_name = {_quote_string(name)}",
            f"@email_address = {_quote_string(row['email_address'])}",
            f"@display_name = {_quote_string(row['display_name'])}",
            f"@mailserver_name = {_quote_string(row['servername'])}",
            f"@port = {int(row['port'] or 25)}",
            f"@enable_ssl = {1 if row['enable_ssl'] else 0}",
        ]
        if row["username"]:
            ref = _secret_ref(policy, prefix, "db_mail_account", name)
            refs.append(ref)
            args += [f"@username = {_quote_string(row['username'])}", f"@password = {_quote_string(ref)}"]
        lines.append(
            f"IF NOT EXISTS (SELECT 1 FROM msdb.dbo.sysmail_account WHERE name = {_quote_string(name)})\n"
            f"    EXEC msdb.dbo.sysmail_add_account_sp " + ", ".join(args) + ";"
        )
    seen: set[str] = set()
    for row in _rows(cursor, _DBMAIL_PROFILE_SQL):
        profile = str(row["profile_name"])
        if profile not in seen:
            seen.add(profile)
            lines.append(
                f"IF NOT EXISTS (SELECT 1 FROM msdb.dbo.sysmail_profile WHERE name = {_quote_string(profile)})\n"
                f"    EXEC msdb.dbo.sysmail_add_profile_sp @profile_name = {_quote_string(profile)};"
            )
        if row["account_name"]:
            lines.append(
                f"EXEC msdb.dbo.sysmail_add_profileaccount_sp @profile_name = {_quote_string(profile)}, "
                f"@account_name = {_quote_string(row['account_name'])}, "
                f"@sequence_number = {int(row['sequence_number'] or 1)};"
            )
    return _render("db_mail", info, source=prefix, body=lines), refs


def _matches(value: str, pattern: str) -> bool:
    """Glob-ish category match: ``REPL-*`` covers every replication category."""
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern


#: name -> (exporter, returns_secret_refs). Kept as data so a new artifact is one entry plus one
#: function, and the ordering stays in config where it can be reviewed.
_EXPORTERS: dict[str, tuple[Callable[..., Any], bool]] = {
    "sp_configure": (_export_sp_configure, True),
    "credentials": (_export_credentials, True),
    "logins": (_export_logins, False),
    "server_roles": (_export_server_roles, False),
    "permissions": (_export_permissions, False),
    "endpoints": (_export_endpoints, False),
    "linked_servers": (_export_linked_servers, True),
    "db_mail": (_export_db_mail, True),
    "operators": (_export_operators, False),
    "proxies": (_export_proxies, False),
    "agent_schedules": (_export_agent_schedules, False),
    "agent_jobs": (_export_agent_jobs, False),
    "alerts": (_export_alerts, False),
    "model_options": (_export_model_options, False),
}



# --------------------------------------------------------------------------- #
# Encryption prerequisites
# --------------------------------------------------------------------------- #

_TDE_CERTS_SQL = """
SELECT c.name, c.pvt_key_encryption_type_desc, c.expiry_date
  FROM master.sys.certificates AS c
 WHERE c.name NOT LIKE '##%';
"""

_TDE_DATABASES_SQL = """
SELECT DB_NAME(k.database_id) AS database_name, k.encryption_state_desc, c.name AS certificate_name
  FROM sys.dm_database_encryption_keys AS k
  LEFT JOIN master.sys.certificates AS c ON c.thumbprint = k.encryptor_thumbprint;
"""

_DB_CRYPTO_SQL = """
SELECT (SELECT COUNT(*) FROM {db}.sys.symmetric_keys WHERE name = '##MS_DatabaseMasterKey##') AS master_key,
       (SELECT COUNT(*) FROM {db}.sys.certificates WHERE name NOT LIKE '##%') AS certificates,
       (SELECT COUNT(*) FROM {db}.sys.symmetric_keys WHERE name <> '##MS_DatabaseMasterKey##') AS symmetric_keys,
       (SELECT COUNT(*) FROM {db}.sys.column_master_keys) AS column_master_keys,
       (SELECT COUNT(*) FROM {db}.sys.column_encryption_keys) AS column_encryption_keys;
"""


def crypto_prerequisites(cursor) -> dict[str, Any]:
    """Encryption that a restore will NOT bring with it, and what has to move by hand.

    None of this is exportable as SQL, which is exactly why it is reported. A restore that
    silently loses the ability to decrypt is the worst kind: the database mounts, the rows are
    there, and every read of an encrypted column fails later, somewhere else.
    """
    findings: dict[str, Any] = {"tde_certificates": [], "tde_databases": [], "databases": []}
    try:
        findings["tde_certificates"] = [
            {"name": str(row["name"]),
             "private_key_protected_by": str(row["pvt_key_encryption_type_desc"]),
             "expires": str(row["expiry_date"])}
            for row in _rows(cursor, _TDE_CERTS_SQL)
        ]
        findings["tde_databases"] = [
            {"database": str(row["database_name"]), "state": str(row["encryption_state_desc"]),
             "certificate": str(row["certificate_name"])}
            for row in _rows(cursor, _TDE_DATABASES_SQL)
        ]
    except Exception as exc:  # noqa: BLE001 - a permission gap is a fact, not a crash.
        findings["error"] = str(exc)

    databases = _rows(
        cursor,
        "SELECT name FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' "
        "AND HAS_DBACCESS(name) = 1 ORDER BY name;",
    )
    for row in databases:
        name = str(row["name"])
        try:
            counts = _rows(cursor, _DB_CRYPTO_SQL.format(db=_quote_name(name)))[0]
        except Exception as exc:  # noqa: BLE001
            findings["databases"].append({"database": name, "unreadable": str(exc)[:200]})
            continue
        if any(int(value or 0) for value in counts.values()):
            findings["databases"].append({"database": name,
                                          **{k: int(v or 0) for k, v in counts.items()}})
    return findings


def render_crypto_notes(findings: dict[str, Any]) -> str:
    """The operator-facing note that ships in the bundle."""
    lines = [
        "db_ops instance metadata - ENCRYPTION PREREQUISITES",
        "=" * 52,
        "",
        "These objects are NOT in this bundle and are NOT in a user-database backup either, or",
        "are in it but unusable on a different instance. Moving them is a manual step.",
        "",
    ]
    if findings.get("tde_certificates"):
        lines += ["TDE / server certificates in master:"]
        for item in findings["tde_certificates"]:
            lines.append(f"  - {item['name']} (private key protected by "
                         f"{item['private_key_protected_by']}, expires {item['expires']})")
        lines += [
            "  BACKUP CERTIFICATE ... TO FILE = ... WITH PRIVATE KEY (FILE = ..., ENCRYPTION BY",
            "  PASSWORD = ...) on the source, then CREATE CERTIFICATE ... FROM FILE on the target",
            "  BEFORE restoring any TDE database. Without it the restore cannot even mount.",
            "",
        ]
    if findings.get("tde_databases"):
        lines += ["Databases with TDE enabled:"]
        for item in findings["tde_databases"]:
            lines.append(f"  - {item['database']}: {item['state']}, encryptor {item['certificate']}")
        lines.append("")
    for item in findings.get("databases", []):
        lines.append(f"Database {item['database']}:")
        if item.get("master_key"):
            lines += [
                "  - has a Database Master Key. It is encrypted by this instance's Service Master",
                "    Key as well as by its password. The target has a DIFFERENT Service Master Key,",
                "    so after a restore it opens only with the password. Record that password now:",
                "      OPEN MASTER KEY DECRYPTION BY PASSWORD = '...';",
                "      ALTER MASTER KEY ADD ENCRYPTION BY SERVICE MASTER KEY;",
                "    on the target, or everything encrypted under it is unreadable.",
            ]
        if item.get("certificates") or item.get("symmetric_keys"):
            lines.append(f"  - {item.get('certificates', 0)} certificate(s), "
                         f"{item.get('symmetric_keys', 0)} symmetric key(s): these travel inside the "
                         "database backup, but only work once the master key above is open.")
        if item.get("column_master_keys"):
            lines += [
                f"  - {item['column_master_keys']} Always Encrypted Column Master Key(s) and "
                f"{item.get('column_encryption_keys', 0)} Column Encryption Key(s).",
                "    SQL Server stores only the CMK's provider name and key path - the key itself",
                "    lives in a Windows certificate store or a key vault, OUTSIDE SQL Server, and is",
                "    read by the CLIENT. The restored database keeps the metadata and the encrypted",
                "    CEK; the data stays unreadable until that key is available to the client.",
            ]
        lines.append("")
    if len(lines) <= 6:
        lines.append("No encryption objects found. Nothing to move by hand.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #

def export_instance(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Read one instance and write its ``server/`` artifact set. Changes nothing on the instance.

    Request::

        {"target": "ACME-192-0-2-115",
         "output_dir": "runtime/instance_bundles/ACME-192-0-2-115",
         "include": ["logins", "agent_jobs"],      # default: everything the policy declares
         "secret_prefix": "MSSQL_2_115"}
    """
    policy = load_policy(data_dir)
    report = GateReport("sqlserver-export-instance", target=str(request.get("target") or ""), echo=echo)

    connection, resolved = _connect(request, data_dir=data_dir)
    try:
        cursor = connection.cursor()
        info = server_info(cursor)
        report.note("source", info)
        report.note("resolved_target", {k: v for k, v in resolved.items() if k != "password"})
        report.add("instance.identity", OK,
                   f"{info.get('machine_name')} build {info.get('build')} ({info.get('edition')})",
                   blocking=False, data=info)

        prefix = str(request.get("secret_prefix") or resolved.get("server_id") or "MSSQL")
        wanted = [str(name) for name in (request.get("include") or [])]
        names = artifacts_in_order(policy)
        if wanted:
            unknown = sorted(set(wanted) - set(names))
            if unknown:
                raise SqlServerInstanceError(
                    f"Unknown artifact(s): {', '.join(unknown)}. Known: {', '.join(names)}."
                )
            names = [name for name in names if name in wanted]

        agent = _has_agent(info)
        out_root = Path(str(request.get("output_dir") or TOOL_ROOT / "runtime" / "instance_bundles" / prefix))
        server_root = out_root / SERVER_DIR
        server_root.mkdir(parents=True, exist_ok=True)

        secret_refs: list[str] = []
        skipped_config: list[str] = []
        written: dict[str, str] = {}
        failed: dict[str, str] = {}
        for name in names:
            spec = (policy.get("artifacts") or {}).get(name) or {}
            if spec.get("requires_agent") and not agent:
                report.add(f"export.{name}", SKIP,
                           f"{info.get('edition')} has no SQL Agent; nothing to export",
                           blocking=False)
                continue
            # One artifact per try, because the login that can read `sys.server_principals` is
            # not always the login that can read `msdb.dbo.sysmail_server`. Unguarded, the first
            # such refusal aborted the whole export: on 192.0.2.248 the db_mail artifact raised
            # "SELECT permission was denied on the object 'sysmail_server'" and took the logins,
            # roles and permissions down with it - seven .sql files were on disk, the manifest was
            # never written, and the restore's metadata replay then reported the directory "is not
            # an instance bundle". A bundle missing one artifact is worth having; a bundle missing
            # its manifest is not a bundle. What must never happen is a SILENT gap, so the failure
            # is recorded in the report and in the manifest, by name and with the engine's words.
            try:
                exporter, returns_refs = _EXPORTERS[name]
                outcome = exporter(cursor, policy, info, prefix)
                if returns_refs:
                    text, extra = outcome
                    if name == "sp_configure":
                        skipped_config = list(extra)
                    else:
                        secret_refs.extend(extra)
                else:
                    text = outcome
            except Exception as exc:  # noqa: BLE001 - any artifact may be refused by permissions.
                failed[name] = str(exc).strip()
                report.add(f"export.{name}", WARN,
                           f"NOT exported: {str(exc).strip()[:300]}", blocking=False)
                continue
            path = server_root / f"{name}.sql"
            path.write_text(text.rstrip() + "\n", encoding="utf-8")
            written[name] = str(path)
            report.add(f"export.{name}", OK, f"{path.name} ({len(text.splitlines())} lines)",
                       blocking=False)

        crypto = crypto_prerequisites(cursor)
        (server_root / "crypto_prerequisites.txt").write_text(
            render_crypto_notes(crypto), encoding="utf-8"
        )
        crypto_hits = (len(crypto.get("tde_certificates") or [])
                       + len(crypto.get("databases") or []))
        report.add(
            "export.encryption",
            WARN if crypto_hits else OK,
            "no encryption objects to move by hand" if not crypto_hits else
            f"{crypto_hits} encryption prerequisite(s) this bundle CANNOT carry - see "
            "server/crypto_prerequisites.txt",
            blocking=False,
            data=crypto if crypto_hits else None,
        )

        manifest = {
            "schema_version": 1,
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": info,
            "source_server_id": resolved.get("server_id"),
            "secret_prefix": prefix,
            "artifacts": sorted(written),
            # Named rather than merely absent from `artifacts`: a replay reading this bundle in six
            # months has no other way to tell "this instance had no linked servers" from "the
            # export was not allowed to look".
            "artifacts_failed": dict(sorted(failed.items())),
            "secret_refs": sorted(set(secret_refs)),
            "sp_configure_skipped": skipped_config,
            "encryption_prerequisites": crypto,
        }
        (out_root / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        report.note("bundle_dir", str(out_root))
        report.note("manifest", manifest)
        if secret_refs:
            report.add("export.secrets", WARN,
                       f"{len(set(secret_refs))} secret(s) cannot be read out of SQL Server and are "
                       "placeholders; add them to the secret store before replay",
                       blocking=False, data=sorted(set(secret_refs)))
        if skipped_config:
            report.add("export.sp_configure_skipped", WARN,
                       f"{len(skipped_config)} setting(s) exported commented out (host-specific or "
                       "unclassified); review before replay",
                       blocking=False, data=skipped_config)
    finally:
        connection.close()
    return host_ops._finish(report, request, [])  # noqa: SLF001


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #

_SECRET_PATTERN = re.compile(r"\{\{secret:([A-Za-z0-9_]+)\}\}")


def read_bundle(bundle_dir: str | Path) -> tuple[Path, dict[str, Any]]:
    root = Path(bundle_dir)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        raise SqlServerInstanceError(
            f"{manifest_path} not found; {root} is not an instance bundle. Run "
            "sqlserver-export-instance first."
        )
    return root, load_json_file(manifest_path)


def resolve_secrets(text: str, secrets: dict[str, str]) -> tuple[str, list[str]]:
    """Substitute ``{{secret:NAME}}`` placeholders; return the text and the names still missing."""
    missing: list[str] = []

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        value = secrets.get(name)
        if value is None:
            missing.append(name)
            return match.group(0)
        return str(value).replace("'", "''")

    return _SECRET_PATTERN.sub(_replace, text), missing


def replay_instance(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Apply a bundle to a target instance, in dependency order, with version/edition gates.

    Request::

        {"target": "ACME-192-0-2-116",
         "bundle_dir": "runtime/instance_bundles/ACME-192-0-2-115",
         "phase": "pre-database",   # or post-database / all
         "dry_run": true, "confirm": true,
         "on_unsupported": "skip"}  # or "fail"

    ``phase`` matters: ``pre-database`` must run **before** the user databases are restored, so
    their users are never orphaned; ``post-database`` must run **after**, because Agent job steps
    name databases that have to exist.
    """
    policy = load_policy(data_dir)
    root, manifest = read_bundle(request.get("bundle_dir") or "")
    phase = str(request.get("phase") or "all").strip().lower()
    if phase not in {PRE_DATABASE, POST_DATABASE, "all"}:
        raise SqlServerInstanceError(
            f"phase must be '{PRE_DATABASE}', '{POST_DATABASE}' or 'all'; got {phase!r}."
        )
    dry_run = bool(request.get("dry_run"))
    strict = str(request.get("on_unsupported") or "skip").lower() == "fail"

    report = GateReport("sqlserver-replay-instance", target=str(request.get("target") or ""), echo=echo)
    report.note("bundle_dir", str(root))
    report.note("source", manifest.get("source"))
    report.note("phase", phase)
    report.note("dry_run", dry_run)


    # autocommit, because sp_configure and RECONFIGURE are refused inside a user transaction
    # ("CONFIG statement cannot be used inside a user transaction", error 574) and the default
    # connection wraps every batch in one. Replay is a sequence of independently guarded,
    # idempotent statements, so there is no transaction to want here anyway.
    connection, resolved = _connect(request, data_dir=data_dir, autocommit=True)
    try:
        cursor = connection.cursor()
        info = server_info(cursor)
        report.note("target_instance", info)

        source_major = major_version((manifest.get("source") or {}).get("build"))
        target_major = major_version(info.get("build"))
        if target_major < source_major:
            report.add("version.direction", FAIL,
                       f"target is SQL Server {target_major}, bundle came from {source_major}; "
                       "replaying onto an older major version is refused")
            return host_ops._finish(report, request, [])  # noqa: SLF001
        report.add("version.direction", OK,
                   f"bundle {source_major} -> target {target_major}"
                   + (" (same version)" if source_major == target_major else " (upgrade)"),
                   blocking=False)

        source_collation = (manifest.get("source") or {}).get("collation")
        if source_collation and source_collation != info.get("collation"):
            # Not blocking: it does not stop logins working. Reported loudly because it changes
            # comparison semantics for everything created afterwards, and it is the kind of thing
            # discovered six months later.
            report.add("instance.collation", WARN,
                       f"server collation differs: bundle {source_collation}, target "
                       f"{info.get('collation')}", blocking=False)

        if not dry_run:
            from db_ops.common import confirm

            # Checked after the version gate, so a refused replay is never even offered, and
            # before anything executes. dry_run skips it deliberately: rehearsing an operation
            # is not performing one, and confirming something that will not happen teaches
            # people to type "yes" without reading.
            allowed = confirm.require_confirmation(
                report,
                request,
                operation=f"replay instance metadata ({phase})",
                target=str(request.get("target") or info.get("machine_name") or ""),
                effects=[
                    f"apply {root / SERVER_DIR} to this instance",
                    "creates logins, roles, permissions and Agent objects; existing objects are left alone",
                ],
            )
            if not allowed:
                return host_ops._finish(report, request, [])  # noqa: SLF001

        agent = _has_agent(info)
        secrets = _load_secrets(request, data_dir=data_dir)
        names = artifacts_in_order(policy, phase="" if phase == "all" else phase)
        results: list[dict[str, Any]] = []
        missing_all: set[str] = set()

        # Resolve every placeholder across every file FIRST. Failing closed here beats creating
        # a credential whose secret is the literal string "{{secret:...}}" and discovering it at
        # first use, in a failure nobody traces back to this replay.
        # Resolution is per artifact, and so is the refusal. An artifact whose secrets are missing
        # is never executed - a credential created with the literal string "{{secret:...}}" is a
        # failure nobody traces back to this replay - but it no longer takes the rest of the bundle
        # down with it. SQL Server will not hand out linked-server or credential passwords, so
        # those two are *always* placeholders unless an operator has put them in the store by hand;
        # blocking globally on that meant logins, roles, permissions and Agent jobs could never
        # replay on any instance that had a single linked server.
        strict_secrets = str(request.get("on_missing_secret") or "skip").lower() == "fail"
        prepared: list[tuple[str, Path, str]] = []
        blocked: dict[str, list[str]] = {}
        for name in names:
            path = root / SERVER_DIR / f"{name}.sql"
            if not path.exists():
                continue
            text, missing = resolve_secrets(path.read_text(encoding="utf-8"), secrets)
            if missing:
                missing_all.update(missing)
                blocked[name] = sorted(missing)
                continue
            prepared.append((name, path, text))
        if missing_all:
            report.add("replay.secrets", FAIL if strict_secrets else WARN,
                       f"{len(missing_all)} secret reference(s) are not in the secret store; "
                       + ("nothing was executed" if strict_secrets else
                          f"skipped {', '.join(sorted(blocked))} and replayed the rest"),
                       data=sorted(missing_all), blocking=strict_secrets)
            if strict_secrets:
                return host_ops._finish(report, request, [])  # noqa: SLF001
            for name, refs in sorted(blocked.items()):
                results.append({"artifact": name, "status": SKIP,
                                "reason": f"{len(refs)} secret(s) not in the store",
                                "missing_secrets": refs})

        for name, path, text in prepared:
            spec = (policy.get("artifacts") or {}).get(name) or {}
            minimum = int(spec.get("min_major_version") or 0)
            if spec.get("requires_agent") and not agent:
                results.append({"artifact": name, "status": SKIP, "reason": "target has no SQL Agent"})
                report.add(f"replay.{name}", FAIL if strict else SKIP,
                           f"{info.get('edition')} has no SQL Agent", blocking=strict)
                continue
            if target_major < minimum:
                results.append({"artifact": name, "status": SKIP,
                                "reason": f"needs SQL Server {minimum}+"})
                report.add(f"replay.{name}", FAIL if strict else SKIP,
                           f"needs SQL Server {minimum}+, target is {target_major}", blocking=strict)
                continue
            if dry_run:
                results.append({"artifact": name, "status": SKIP, "reason": "dry run",
                                "statements": _count_statements(text)})
                report.add(f"replay.{name}", OK,
                           f"would run {_count_statements(text)} statement(s) from {path.name}",
                           blocking=False)
                continue
            outcome = _execute_artifact(cursor, connection, text)
            results.append({"artifact": name, **outcome})
            detail = f"{outcome['succeeded']} ok, {outcome['failed']} failed"
            if outcome["unsupported"]:
                detail += f", {len(outcome['unsupported'])} unsupported on this edition"
            report.add(f"replay.{name}", outcome["status"], detail,
                       blocking=outcome["status"] == FAIL,
                       data=(outcome["errors"] + outcome["unsupported"]) or None)

        report.note("results", results)
    finally:
        connection.close()
    return host_ops._finish(report, request, list(request.get("overrides") or []))  # noqa: SLF001


def _load_secrets(request: dict[str, Any], *, data_dir: str | Path | None = None) -> dict[str, str]:
    """Secret values for the placeholders, from the encrypted store — never from the request.

    A request field would put the plaintext in a command line, a log and a shell history, which
    is the thing the placeholder scheme exists to avoid.
    """
    from db_ops.common import data_sources

    try:
        return dict(data_sources.load_secret_text(data_dir))
    except Exception:  # noqa: BLE001 - an unreadable store is reported as "every ref missing",
        return {}      # which is the fail-closed path rather than a crash.


def _split_batches(text: str) -> list[str]:
    """Split on ``GO`` batch separators, the way sqlcmd does - but not inside a string literal.

    An Agent job step's command is *itself* T-SQL, and a multi-statement step routinely carries its
    own ``GO``. The export writes that text as a quoted argument::

        EXEC msdb.dbo.sp_add_jobstep ..., @command = 'USE APPDB_Prod
        GO
        EXEC [emp].[usp_EmployeeContract_ChangeContractStatus]
        ', @on_success_action = 1, ...;

    Splitting on every line that reads ``GO`` cut that statement in half, so the first batch ended
    mid-literal and SQL Server answered "Unclosed quotation mark after the character string 'USE
    APPDB_Prod'". 167 of 174 jobs failed to replay that way - every job whose step has more than one
    statement - while the export reported success, because writing a bundle and being able to run
    it are different things and only the first was ever checked.

    Literal tracking is quote parity per line: ``''`` is T-SQL's escape for a quote and contributes
    two, leaving the state unchanged, which is exactly the wanted behaviour.
    """
    batches: list[str] = []
    current: list[str] = []
    in_literal = False
    for line in text.splitlines():
        if not in_literal and line.strip().upper() == "GO":
            batch = "\n".join(current).strip()
            if batch:
                batches.append(batch)
            current = []
            continue
        current.append(line)
        if line.count("'") % 2:
            in_literal = not in_literal
    batch = "\n".join(current).strip()
    if batch:
        batches.append(batch)
    return batches


def _is_executable(batch: str) -> bool:
    """Does this batch contain anything to run, or is it only comments?

    The header comment block rides along with the first statement, and an artifact with nothing
    to export is comments end to end. Executing those succeeds and reports "1 ok, 0 failed" for a
    file that did nothing — the first live trial showed exactly that for `credentials` and
    `logins` on an instance that had neither. A count nobody can trust is worse than no count.
    """
    for line in batch.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            return True
    return False


def _count_statements(text: str) -> int:
    """Statements a dry run would execute — comments and blank lines do not count."""
    return sum(
        1
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("--")
        and line.rstrip().endswith(";")
    )


#: SQL Server's way of saying "this build cannot do that at all". Not a defect in the bundle and
#: not something a retry fixes: `xp_cmdshell` and `Ole Automation Procedures` exist on Windows and
#: simply do not on Linux, so a Windows-to-Linux replay meets them every time. Reporting them as
#: failures would make a correct replay look broken and teach people to ignore the FAIL line.
_UNSUPPORTED_MARKERS = (
    "is not supported by this edition",
    "is not supported in this version",
    "not supported on this platform",
)

#: Error 15401: "Windows NT user or group '...' not found."
#:
#: A Windows login that is **local to the source machine** — a machine account like
#: ``APPDB-DB\\appdbadmin`` or a service account like ``NT Service\\MSSQL$APPDB`` — cannot be created
#: anywhere else, by anyone, ever. Only the source host can resolve it. Reporting that as a failure
#: made ``replay.logins`` red on every single cross-host replay for a reason nobody could act on,
#: which is the fastest way to teach an operator to stop reading a gate. It is a skip: the bundle
#: named something that does not apply here, and said so.
#:
#: Deliberately matched on the error rather than on the login name: a *domain* account resolves
#: perfectly well on another domain-joined host, and only the target can say which it is.
_NOT_RESOLVABLE_MARKERS = (
    "windows nt user or group",
)


def _classify_error(message: str) -> str:
    lowered = message.lower()
    if any(marker in message for marker in _UNSUPPORTED_MARKERS):
        return SKIP
    if any(marker in lowered for marker in _NOT_RESOLVABLE_MARKERS) and "15401" in message:
        return SKIP
    return FAIL


def _execute_artifact(cursor, connection, text: str) -> dict[str, Any]:
    """Run one artifact, batch by batch, collecting per-batch outcomes.

    One failing batch does not abort the file: a rebuild wants the 90% that applied plus a list
    of what did not, not a stop at the first linked server whose provider is missing. That is
    also why the generated SQL separates statements with GO — without batches there is nothing
    for this resilience to work on.
    """
    succeeded = 0
    errors: list[str] = []
    unsupported: list[str] = []
    for batch in _split_batches(text):
        if not _is_executable(batch):
            continue
        try:
            cursor.execute(batch)
            while cursor.nextset():  # drain, so the next execute starts clean
                pass
            succeeded += 1
        except Exception as exc:  # noqa: BLE001 - reported per batch, never silently dropped.
            first_line = next(
                (line.strip() for line in batch.splitlines()
                 if line.strip() and not line.strip().startswith("--")),
                "",
            )
            entry = f"{str(exc)[:300]} | {first_line[:120]}"
            (unsupported if _classify_error(str(exc)) == SKIP else errors).append(entry)
    try:
        connection.commit()
    except Exception:  # noqa: BLE001 - autocommit connections have nothing to commit.
        pass
    return {
        "status": FAIL if errors else (WARN if unsupported else OK),
        "succeeded": succeeded,
        "failed": len(errors),
        "errors": errors,
        "unsupported": unsupported,
    }


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

#: Three-part naming, deliberately: `USE db; SELECT ...` returns after the USE, whose result set
#: does not exist, so every read failed and was recorded as "0 orphans" instead of "unknown".
_ORPHANS_SQL = """
SELECT COUNT(*) AS orphan_count
  FROM {db}.sys.database_principals AS dp
 WHERE dp.type IN ('S', 'U', 'G')
   AND dp.sid IS NOT NULL
   AND dp.authentication_type_desc <> 'NONE'
   AND dp.name NOT IN ('dbo', 'guest', 'sys', 'INFORMATION_SCHEMA')
   AND NOT EXISTS (SELECT 1 FROM sys.server_principals AS sp WHERE sp.sid = dp.sid);
"""

_COUNT_SQL = {
    "logins": "SELECT COUNT(*) AS n FROM sys.server_principals WHERE type IN ('S','U','G') AND name NOT LIKE '##%';",
    "server_roles": "SELECT COUNT(*) AS n FROM sys.server_principals WHERE type = 'R';",
    "linked_servers": "SELECT COUNT(*) AS n FROM sys.servers WHERE is_linked = 1;",
    "endpoints": "SELECT COUNT(*) AS n FROM sys.endpoints WHERE endpoint_id > 65535;",
    "credentials": "SELECT COUNT(*) AS n FROM sys.credentials;",
    "agent_jobs": "SELECT COUNT(*) AS n FROM msdb.dbo.sysjobs;",
    "agent_schedules": "SELECT COUNT(*) AS n FROM msdb.dbo.sysschedules;",
    "operators": "SELECT COUNT(*) AS n FROM msdb.dbo.sysoperators;",
    "alerts": "SELECT COUNT(*) AS n FROM msdb.dbo.sysalerts;",
    "proxies": "SELECT COUNT(*) AS n FROM msdb.dbo.sysproxies;",
}


def verify_instance(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Compare a live instance against a bundle. Read-only; changes nothing.

    The headline number is **orphaned users**: database principals whose SID has no matching
    server login. It must be zero. Anything else means SID preservation did not work, and it is
    the single most useful signal this capability produces — a login that exists by name and not
    by SID looks fine in every listing and still cannot connect.
    """
    root, manifest = read_bundle(request.get("bundle_dir") or "")
    report = GateReport("sqlserver-verify-instance", target=str(request.get("target") or ""), echo=echo)
    report.note("bundle_dir", str(root))
    report.note("source", manifest.get("source"))

    connection, _resolved = _connect(request, data_dir=data_dir)
    try:
        cursor = connection.cursor()
        info = server_info(cursor)
        report.note("target_instance", info)

        orphans = _orphans(cursor)
        readable = [item for item in orphans if item["readable"]]
        unreadable = [item for item in orphans if not item["readable"]]
        total = sum(int(item["orphan_count"]) for item in readable)
        if total:
            status = FAIL
            detail = (f"{total} orphaned user(s) across "
                      f"{len([o for o in readable if o['orphan_count']])} database(s); the logins "
                      "exist by name but not by SID")
        elif unreadable:
            # Never "no orphaned users" when some databases were not looked at: an unverified
            # clean result reads exactly like a verified one and is the more dangerous of the two.
            status = WARN
            detail = (f"no orphaned users in {len(readable)} database(s); "
                      f"{len(unreadable)} could not be read and were NOT checked")
        else:
            status = OK
            detail = f"no orphaned users across {len(readable)} database(s)"
        report.add(
            "verify.orphaned_users", status, detail,
            blocking=status == FAIL,
            data=[item for item in orphans if item["orphan_count"] or not item["readable"]] or None,
        )

        for name, sql in _COUNT_SQL.items():
            if name not in set(manifest.get("artifacts") or []):
                continue
            try:
                count = int(_rows(cursor, sql)[0]["n"])
            except Exception as exc:  # noqa: BLE001 - a missing catalog is a fact, not a crash.
                report.add(f"verify.{name}", WARN, f"could not read: {exc}", blocking=False)
                continue
            report.add(f"verify.{name}", OK if count else WARN,
                       f"{count} present on the target", blocking=False, data={"count": count})

        report.add("verify.providers", WARN,
                   "linked-server OLE DB providers are not checked from SQL alone; a linked server "
                   "can exist and still fail at first query if its provider is not installed",
                   blocking=False)
    finally:
        connection.close()
    return host_ops._finish(report, request, [])  # noqa: SLF001


def _orphans(cursor) -> list[dict[str, Any]]:
    """Per-database orphan counts, skipping databases that cannot be read right now."""
    databases = _rows(
        cursor,
        "SELECT name FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE' "
        "AND HAS_DBACCESS(name) = 1 ORDER BY name;",
    )
    out: list[dict[str, Any]] = []
    for row in databases:
        name = str(row["name"])
        try:
            found = _rows(cursor, _ORPHANS_SQL.format(db=_quote_name(name)))
        except Exception as exc:  # noqa: BLE001
            # Unknown, not zero. Folding an unreadable database into the count is how a clean
            # verdict gets reported for a database nobody actually looked at.
            out.append({"database_name": name, "orphan_count": None, "readable": False,
                        "error": str(exc)[:200]})
            continue
        out.append({"database_name": name,
                    "orphan_count": int(found[0]["orphan_count"]) if found else 0,
                    "readable": True})
    return out
