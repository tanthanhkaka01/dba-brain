"""What is *in* a server: its databases and their state, and the schemas inside one database.

The question every other command has to answer before it can do anything useful — "which
database?", then "which schema?" — and until now every caller answered it by writing its own
`SELECT name FROM sys.databases`. That is four spellings of one question across four engines,
and the Telegram flow that uploads a spreadsheet needs all of them just to prompt the operator
with a list.

**Input is a JSON object**, the same target/credential shape :mod:`db_ops.common.sql_run` takes,
because it *is* that shape — nothing here resolves a target or opens a connection itself:

    {"target": "ACME-192-0-2-248",   // server_id, or "<db_type> <ip> [port]"
     "credential_name": "...",        // optional; default = the instance's
     "include_system": false,         // optional; system databases are hidden by default
     "timeout_seconds": 30}

Engines answer with the vocabulary they actually use, not a flattened lowest common denominator:

* **SQL Server** — `sys.databases`: `state_desc`, recovery model, compatibility level.
* **PostgreSQL** — `pg_database`: owner, encoding, and whether connections are allowed at all.
* **Oracle** — the containers. On 12c+ this is `v$containers`, so a CDB reports its root, its
  seed and every PDB with each one's `open_mode`; the `kind` field says which is which. On a
  non-CDB (and on anything old enough not to have the view) it falls back to `v$database` and
  reports the single database, so the caller gets one shape for both.
* **MySQL** — `information_schema.schemata`. MySQL has no layer between server and schema, so
  `list_databases` and `list_schemas` return the same list; both say so in `note`.

Schemas are per database, so `list_schemas` takes a `database` and connects *into* it — for
Oracle that means the service, and the schemas are the users that own objects.
"""

from __future__ import annotations

from typing import Any

from db_ops.common import sql_run
from db_ops.lib.coerce import as_bool


class DbCatalogError(RuntimeError):
    """A user-facing failure: unknown target, unsupported engine, the query was refused."""


# SQL Server's four system databases occupy database_id 1-4; PostgreSQL's are the two templates
# plus `postgres`; Oracle's is the seed container. Hidden by default because a caller asking
# "which database do I load this spreadsheet into" never means one of these, and showing them
# invites picking one.
_PG_SYSTEM_DATABASES = frozenset({"postgres", "template0", "template1"})
_MYSQL_SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
_SQLSERVER_SYSTEM_SCHEMAS = frozenset({
    "sys", "INFORMATION_SCHEMA", "guest", "db_owner", "db_accessadmin", "db_securityadmin",
    "db_ddladmin", "db_backupoperator", "db_datareader", "db_datawriter",
    "db_denydatareader", "db_denydatawriter",
})
_PG_SYSTEM_SCHEMA_PREFIXES = ("pg_",)
_ORACLE_SYSTEM_SCHEMAS = frozenset({
    "SYS", "SYSTEM", "OUTLN", "DBSNMP", "APPQOSSYS", "AUDSYS", "GSMADMIN_INTERNAL", "XDB",
    "WMSYS", "CTXSYS", "MDSYS", "ORDSYS", "ORDDATA", "ORDPLUGINS", "SI_INFORMTN_SCHEMA",
    "OLAPSYS", "LBACSYS", "DVSYS", "DVF", "OJVMSYS", "ANONYMOUS", "DIP", "ORACLE_OCM",
    "REMOTE_SCHEDULER_AGENT", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC", "SYS$UMF", "GGSYS",
    "PUBLIC", "TSMSYS", "DMSYS", "EXFSYS", "MGMT_VIEW", "SYSMAN", "PERFSTAT",
})

_DATABASES_SQL = {
    "sqlserver": """
        SELECT d.name AS name,
               d.database_id AS database_id,
               d.state_desc AS state,
               d.recovery_model_desc AS recovery_model,
               d.compatibility_level AS compatibility_level,
               d.collation_name AS collation,
               CAST(d.is_read_only AS int) AS is_read_only,
               CASE WHEN d.database_id <= 4 THEN 1 ELSE 0 END AS is_system
        FROM sys.databases d
        ORDER BY d.name
    """,
    "postgresql": """
        SELECT d.datname AS name,
               pg_get_userbyid(d.datdba) AS owner,
               pg_encoding_to_char(d.encoding) AS encoding,
               d.datcollate AS collation,
               CASE WHEN d.datallowconn THEN 'ONLINE' ELSE 'NO_CONNECT' END AS state,
               d.datistemplate AS is_template,
               d.datallowconn AS allow_connections
        FROM pg_database d
        ORDER BY d.datname
    """,
    "mysql": """
        SELECT schema_name AS name,
               default_character_set_name AS encoding,
               default_collation_name AS collation,
               'ONLINE' AS state
        FROM information_schema.schemata
        ORDER BY schema_name
    """,
}

# Oracle 12c+. `v$containers` does not exist on a non-CDB or on anything older, which is not an
# error to report — it is the other shape of the same answer, handled by the fallback below.
_ORACLE_CONTAINERS_SQL = """
    SELECT con_id AS con_id,
           name AS name,
           open_mode AS open_mode,
           restricted AS restricted
    FROM v$containers
    ORDER BY con_id
"""
# The non-CDB answer, richest first. `v$database` grew columns over the releases db_ops still
# talks to: `database_role` arrived with Data Guard in 9i, and this estate has 8.1.7 hosts that
# answer ORA-00904 for it. Asking for the narrow shape everywhere would drop the standby role on
# every modern instance, so the candidates are tried in order and the first that parses wins.
_ORACLE_DATABASE_SQL_CANDIDATES = (
    """
    SELECT name AS name,
           open_mode AS open_mode,
           database_role AS database_role,
           log_mode AS log_mode
    FROM v$database
    """,
    """
    SELECT name AS name,
           open_mode AS open_mode,
           log_mode AS log_mode
    FROM v$database
    """,
    """
    SELECT name AS name,
           log_mode AS log_mode
    FROM v$database
    """,
)

_SCHEMAS_SQL = {
    "sqlserver": """
        SELECT s.name AS name,
               ISNULL(p.name, '') AS owner,
               s.schema_id AS schema_id
        FROM sys.schemas s
        LEFT JOIN sys.database_principals p ON p.principal_id = s.principal_id
        ORDER BY s.name
    """,
    "postgresql": """
        SELECT n.nspname AS name,
               pg_get_userbyid(n.nspowner) AS owner
        FROM pg_namespace n
        ORDER BY n.nspname
    """,
    "mysql": """
        SELECT schema_name AS name,
               default_collation_name AS collation
        FROM information_schema.schemata
        ORDER BY schema_name
    """,
    # A schema in Oracle is a user that owns something. `all_users` is readable by any login;
    # `dba_users` is not, and asking for it would make this command need a DBA where it does not.
    "oracle": """
        SELECT username AS name,
               username AS owner
        FROM all_users
        ORDER BY username
    """,
}


#: Scheduled jobs, per engine. Each returns at least ``name`` and ``enabled`` (1/0), because the
#: one question every caller asks is "what is switched on right now" — `/spbot_disable_job` lists
#: the enabled ones to choose from, and disabling something already disabled is a wasted round
#: trip through Telegram.
#:
#: The engines do not agree on what a "job" is and this does not pretend they do:
#:
#: * SQL Server has one scheduler, the Agent, in ``msdb``.
#: * Oracle has two — ``dba_scheduler_jobs`` (10g+) and the older ``dba_jobs``, which is still
#:   how a lot of 8i/9i-era code schedules work. Both are read and the source is reported, so an
#:   operator can tell which mechanism owns the thing they are about to switch off.
#: * PostgreSQL has none built in. ``pg_cron`` is the common add-on; when it is not installed the
#:   query fails and the caller reports that rather than an empty list, which would read as
#:   "no jobs" and is a different fact entirely.
_JOBS_SQL = {
    "sqlserver": """
        SELECT j.name AS name,
               CAST(j.enabled AS int) AS enabled,
               ISNULL(c.name, '') AS category,
               ISNULL(SUSER_SNAME(j.owner_sid), '') AS owner,
               ISNULL(j.description, '') AS description,
               'agent' AS source
        FROM msdb.dbo.sysjobs AS j
        LEFT JOIN msdb.dbo.syscategories AS c ON c.category_id = j.category_id
        ORDER BY j.name
    """,
    "oracle": """
        SELECT job_name AS name,
               CASE WHEN enabled = 'TRUE' THEN 1 ELSE 0 END AS enabled,
               job_class AS category,
               owner AS owner,
               NVL(comments, ' ') AS description,
               'scheduler' AS source
        FROM dba_scheduler_jobs
        UNION ALL
        SELECT 'DBMS_JOB:' || TO_CHAR(job) AS name,
               CASE WHEN broken = 'N' THEN 1 ELSE 0 END AS enabled,
               ' ' AS category,
               log_user AS owner,
               SUBSTR(what, 1, 200) AS description,
               'dbms_job' AS source
        FROM dba_jobs
        ORDER BY 1
    """,
    "postgresql": """
        SELECT jobname AS name,
               CASE WHEN active THEN 1 ELSE 0 END AS enabled,
               schedule AS category,
               username AS owner,
               command AS description,
               'pg_cron' AS source
        FROM cron.job
        ORDER BY jobname
    """,
}


def _parsed_request(request: Any) -> dict[str, Any]:
    """The common fields, validated once. Unknown keys are ignored, as everywhere in `common`."""
    if not isinstance(request, dict):
        raise DbCatalogError("request must be a JSON object.")
    target = str(request.get("target") or "").strip()
    if not target:
        raise DbCatalogError('request needs a "target" (a server_id, or "<db_type> <ip> [port]").')
    return {
        "target": target,
        "credential_name": str(request.get("credential_name")
                               or request.get("user_ref") or "").strip(),
        "database": str(request.get("database") or "").strip(),
        "include_system": bool(request.get("include_system", False)),
        # list-jobs only. Parsed here rather than read off the raw request in `list_jobs` because
        # this function is the one place that decides what a catalog request contains, and a field
        # read around it is a field the next reader will not find.
        "enabled_only": request.get("enabled_only", False),
        "timeout_seconds": request.get("timeout_seconds"),
        "data_dir": request.get("data_dir"),
        "sql_access": request.get("sql_access"),
    }


def _resolve(parsed: dict[str, Any]) -> dict[str, Any]:
    """Target + credential, through the one resolver every SQL caller uses."""
    try:
        return sql_run.resolve_sqlserver_target(
            parsed["target"],
            data_dir=parsed["data_dir"] or None,
            database=parsed["database"],
            credential_name=parsed["credential_name"],
            sql_access=parsed["sql_access"],
        )
    except sql_run.SqlRunError as exc:
        raise DbCatalogError(str(exc)) from exc


def _query(parsed: dict[str, Any], sql: str, *, database: str = "") -> list[dict[str, Any]]:
    """Run one catalog query through `sql_run` and return its rows as dicts."""
    request: dict[str, Any] = {
        "target": parsed["target"],
        "sql": sql,
        "credential_name": parsed["credential_name"],
        "data_dir": parsed["data_dir"],
        "sql_access": parsed["sql_access"],
    }
    if database:
        request["database"] = database
    if parsed["timeout_seconds"]:
        request["timeout_seconds"] = parsed["timeout_seconds"]
    try:
        result = sql_run.json_safe_result(sql_run.run_sql(request))
    except sql_run.SqlRunError as exc:
        raise DbCatalogError(str(exc)) from exc
    columns = [str(name) for name in result.get("columns") or []]
    return [dict(zip(columns, row)) for row in result.get("rows") or []]


def _is_system_database(db_type: str, row: dict[str, Any]) -> bool:
    name = str(row.get("name") or "")
    if db_type == "sqlserver":
        return bool(row.get("is_system"))
    if db_type == "postgresql":
        return name in _PG_SYSTEM_DATABASES or bool(row.get("is_template"))
    if db_type == "mysql":
        return name in _MYSQL_SYSTEM_SCHEMAS
    if db_type == "oracle":
        return str(row.get("kind") or "") in {"SEED", "CDB$ROOT"}
    return False


def _is_system_schema(db_type: str, name: str) -> bool:
    if db_type == "sqlserver":
        return name in _SQLSERVER_SYSTEM_SCHEMAS
    if db_type == "postgresql":
        return name.startswith(_PG_SYSTEM_SCHEMA_PREFIXES) or name == "information_schema"
    if db_type == "mysql":
        return name in _MYSQL_SYSTEM_SCHEMAS
    if db_type == "oracle":
        return name.upper() in _ORACLE_SYSTEM_SCHEMAS
    return False


def _oracle_containers(parsed: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """The Oracle answer, in whichever of its two shapes this instance has.

    `v$containers` is tried first and its absence is not reported as a failure: a non-CDB, and
    every release before 12c, simply does not have it. Falling back to `v$database` means one
    caller-visible shape for a CDB, a non-CDB and an 8i host reached over the legacy bridge.
    """
    try:
        rows = _query(parsed, _ORACLE_CONTAINERS_SQL)
    except DbCatalogError:
        rows = []
    if rows:
        containers = []
        for row in rows:
            con_id = row.get("CON_ID", row.get("con_id"))
            name = str(row.get("NAME", row.get("name")) or "")
            try:
                numeric_id = int(con_id)
            except (TypeError, ValueError):
                numeric_id = -1
            kind = "CDB$ROOT" if numeric_id == 1 else "SEED" if numeric_id == 2 else "PDB"
            containers.append({
                "name": name,
                "con_id": numeric_id if numeric_id >= 0 else None,
                "kind": kind,
                "open_mode": str(row.get("OPEN_MODE", row.get("open_mode")) or ""),
                "restricted": str(row.get("RESTRICTED", row.get("restricted")) or ""),
            })
        return "CDB", containers

    rows = []
    last_error: DbCatalogError | None = None
    for candidate in _ORACLE_DATABASE_SQL_CANDIDATES:
        try:
            rows = _query(parsed, candidate)
            break
        except DbCatalogError as exc:
            # ORA-00904 is "this release does not have that column", which the next candidate
            # answers. Anything else — no such view, refused login, bridge down — is real and
            # would only be repeated by the narrower queries, so stop and report it.
            if "ORA-00904" not in str(exc):
                raise
            last_error = exc
    else:
        raise last_error or DbCatalogError("v$database did not answer in any known shape.")

    containers = [{
        "name": str(row.get("NAME", row.get("name")) or ""),
        "con_id": None,
        "kind": "DATABASE",
        "open_mode": str(row.get("OPEN_MODE", row.get("open_mode")) or ""),
        "database_role": str(row.get("DATABASE_ROLE", row.get("database_role")) or ""),
        "log_mode": str(row.get("LOG_MODE", row.get("log_mode")) or ""),
    } for row in rows]
    return "NON_CDB", containers


def list_databases(request: Any) -> dict[str, Any]:
    """Every database on one server, with the state its engine reports.

    Returns the ``data`` half of a `common` CLI response::

        {"server_id", "db_type", "ip", "port", "credential_name", "username",
         "container_type": "CDB",          // oracle only
         "databases": [{"name", "state", ...}],
         "count": 12, "system_hidden": 4, "note": ""}
    """
    parsed = _parsed_request(request)
    resolved = _resolve(parsed)
    db_type = str(resolved["db_type"])

    note = ""
    container_type = ""
    if db_type == "oracle":
        container_type, rows = _oracle_containers(parsed)
        if container_type == "NON_CDB":
            note = ("This instance is not a CDB (or predates v$containers), so it reports one "
                    "database rather than a container list.")
    elif db_type in _DATABASES_SQL:
        rows = _query(parsed, _DATABASES_SQL[db_type])
        if db_type == "mysql":
            note = "MySQL has no layer between server and schema: these are also its schemas."
    else:
        raise DbCatalogError(
            f"list-databases does not know engine {db_type!r}; supported: "
            f"{', '.join(sorted(set(_DATABASES_SQL) | {'oracle'}))}."
        )

    # Normalize the key case once. SQL Server and PostgreSQL hand back the aliases as written;
    # Oracle upper-cases every unquoted identifier, so a caller reading row["name"] would find
    # nothing on one engine and everything on the others.
    normalized = [{str(key).lower(): value for key, value in row.items()} for row in rows]
    hidden = 0
    if not parsed["include_system"]:
        keep = [row for row in normalized if not _is_system_database(db_type, row)]
        hidden = len(normalized) - len(keep)
        normalized = keep

    data: dict[str, Any] = {
        "server_id": resolved["server_id"],
        "db_type": db_type,
        "ip": resolved.get("ip", ""),
        "port": resolved.get("port"),
        "credential_name": resolved.get("credential_name", ""),
        "username": resolved.get("username", ""),
        "databases": normalized,
        "count": len(normalized),
        "system_hidden": hidden,
        "note": note,
    }
    if container_type:
        data["container_type"] = container_type
    return data


def list_schemas(request: Any) -> dict[str, Any]:
    """Every schema inside one database.

    ``database`` is required for the engines that have more than one — it decides which database
    the connection lands in, so a missing one would silently answer about ``master`` (SQL Server)
    or the login's default (PostgreSQL) and look like a correct answer about the wrong place.
    """
    parsed = _parsed_request(request)
    resolved = _resolve(parsed)
    db_type = str(resolved["db_type"])
    if db_type not in _SCHEMAS_SQL:
        raise DbCatalogError(
            f"list-schemas does not know engine {db_type!r}; supported: "
            f"{', '.join(sorted(_SCHEMAS_SQL))}."
        )
    database = parsed["database"]
    if not database and db_type in ("sqlserver", "postgresql"):
        raise DbCatalogError(
            f'list-schemas on {db_type} needs a "database": schemas live inside one, and '
            "without it the answer would describe whichever database the login defaults to. "
            "Run list-databases first."
        )

    rows = _query(parsed, _SCHEMAS_SQL[db_type], database=database)
    normalized = [{str(key).lower(): value for key, value in row.items()} for row in rows]
    hidden = 0
    if not parsed["include_system"]:
        keep = [row for row in normalized
                if not _is_system_schema(db_type, str(row.get("name") or ""))]
        hidden = len(normalized) - len(keep)
        normalized = keep

    return {
        "server_id": resolved["server_id"],
        "db_type": db_type,
        "database": resolved.get("database_name", "") or database,
        "credential_name": resolved.get("credential_name", ""),
        "username": resolved.get("username", ""),
        "schemas": normalized,
        "count": len(normalized),
        "system_hidden": hidden,
        "note": ("MySQL has no layer between server and schema: these are also its databases."
                 if db_type == "mysql" else ""),
    }


def list_jobs(request: Any) -> dict[str, Any]:
    """Every scheduled job on one target, with whether it is currently enabled.

    Returns the ``data`` half of a `common` CLI response::

        {"server_id", "db_type", "jobs": [{"name", "enabled", "category", "owner",
                                           "description", "source"}],
         "count", "enabled_count", "disabled_hidden", "note"}

    ``enabled_only`` (default **false**) filters to the switched-on ones. ``/spbot_disable_job``
    asks for it: offering a job that is already disabled as something to disable is a wasted round
    trip through Telegram, and the count of what was filtered is reported so "hidden" cannot read
    as "missing" — the same pairing as :func:`db_ops.lib.listing.active_only`.
    """
    parsed = _parsed_request(request)
    resolved = _resolve(parsed)
    db_type = str(resolved["db_type"])
    if db_type not in _JOBS_SQL:
        raise DbCatalogError(
            f"list-jobs does not know engine {db_type!r}; supported: "
            f"{', '.join(sorted(_JOBS_SQL))}."
        )

    enabled_only = as_bool(parsed.get("enabled_only"), default=False)
    database = "msdb" if db_type == "sqlserver" else parsed["database"]
    try:
        rows = _query(parsed, _JOBS_SQL[db_type], database=database)
    except DbCatalogError as exc:
        if db_type == "postgresql" and _looks_like_missing_pg_cron(str(exc)):
            # An empty list here would read as "this server has no jobs", which is a different
            # and much more reassuring statement than "this server has no scheduler installed".
            raise DbCatalogError(
                "PostgreSQL has no built-in job scheduler and pg_cron is not installed on "
                f"{resolved['server_id']} (relation cron.job does not exist). There is nothing "
                "to list or disable here."
            ) from exc
        raise

    normalized = [{str(key).lower(): value for key, value in row.items()} for row in rows]
    for row in normalized:
        row["enabled"] = bool(int(row.get("enabled") or 0))
    enabled_count = sum(1 for row in normalized if row["enabled"])

    disabled_hidden = 0
    if enabled_only:
        disabled_hidden = len(normalized) - enabled_count
        normalized = [row for row in normalized if row["enabled"]]

    note = ""
    if db_type == "oracle":
        note = ("Oracle schedules work two ways; `source` says which owns each entry "
                "(scheduler = DBMS_SCHEDULER, dbms_job = the older DBMS_JOB).")
    return {
        "server_id": resolved["server_id"],
        "db_type": db_type,
        "ip": resolved.get("ip", ""),
        "credential_name": resolved.get("credential_name", ""),
        "jobs": normalized,
        "count": len(normalized),
        "enabled_count": enabled_count,
        "disabled_hidden": disabled_hidden,
        "note": note,
    }


def _looks_like_missing_pg_cron(message: str) -> bool:
    lowered = message.lower()
    return "cron.job" in lowered and ("does not exist" in lowered or "undefined" in lowered)

