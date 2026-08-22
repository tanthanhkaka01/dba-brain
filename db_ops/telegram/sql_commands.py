from __future__ import annotations

from pathlib import Path
from typing import Any

# Imported by name, not as a module: `sql_text` is also a local variable in this file
# (the SQL itself), and a module bound to the same name shadows it silently.
from db_ops.lib.sql_text import DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT_SECONDS
from db_ops.common import data_sources
from db_ops.lib import common_cli
from db_ops.lib.paths import REPO_ROOT, TOOL_ROOT  # noqa: F401 - one definition, see that module
from db_ops.lib.paths import asset_candidates


# db_ops is a standalone repo root; keep REPO_ROOT as an alias so path resolution
# never escapes the project (was TOOL_ROOT.parents[1] under the old repo/tools/db_ops layout).
DEFAULT_CONNECT_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS
# Upper bound on rows exported to a single xlsx by spbot_sql_to_xlsx. A SELECT that returns
# more than this is truncated (and the caller is told), so one careless query cannot pull an
# unbounded result into memory / a giant attachment.
DEFAULT_SQL_TO_XLSX_MAX_ROWS = DEFAULT_MAX_ROWS

# The "run SQL on one database" engine lives in common and is reached through
# `common.cli run-sql`, so the shell and every app share one implementation; Telegram only adds
# the xlsx-shaped wrapper below. The error type is this app's own since 2026-08-15: importing
# `sql_run.SqlRunError` meant importing the engine to name its exception, which is the whole thing
# the CLI boundary exists to stop. Callers catch it by this name and always did.
class SqlToXlsxError(RuntimeError):
    """A ``/spbot_sql_to_xlsx`` run that failed for a reason the operator can read."""


def execute_sql_support_command(*, command: Any, args: list[str]) -> dict[str, Any]:
    config = dict(command.action_config or {})
    db_type = str(config.get("db_type") or "sqlserver").lower()
    if db_type != "sqlserver":
        raise RuntimeError(f"Unsupported Telegram SQL command db_type: {db_type}")

    sql_path = resolve_telegram_sql_file(str(config["sql_file"]))
    sql_text = sql_path.read_text(encoding="utf-8-sig")
    params = build_sql_params(config.get("parameters") or [], args=args)
    database = find_database(config)
    database_name = str(config.get("database_name") or database.get("database_name")
                        or database.get("service_name") or "master")

    # Through `common.cli run-sql`, not a connection opened here (2026-08-15). This app does not
    # get its own opinion on driver choice, the ODBC->pymssql fallback or the timeouts, and since
    # the boundary became the CLI it does not import the engine to borrow them either.
    #
    # **`commit` is not optional here and the default would be wrong.** These commands WRITE —
    # `/spbot_update_allow_re_inspect` sets a flag on a production row — while `run-sql` rolls back
    # unless asked, because it is also the read-only engine behind `/spbot_sql_to_xlsx`. Sending
    # them without `commit: true` would turn every one of them into a silent no-op that still
    # reported success. That is why this could not be routed through `run_sql` before it took
    # `params`: the values are **bound**, never pasted into the SQL, and they come from a chat
    # message.
    success, result, error = common_cli.run_allowing_failure("run-sql", {
        "target": str(config.get("server_id") or ""),
        "database": database_name,
        "credential_name": str(config.get("credential_name") or ""),
        "sql": sql_text,
        "params": params,
        "commit": True,
        "timeout_seconds": int(config.get("connect_timeout_seconds",
                                          DEFAULT_CONNECT_TIMEOUT_SECONDS)),
    })
    if not success:
        raise RuntimeError(error or "run-sql failed without a reason.")

    return {
        "ok": True,
        "sql_file": str(sql_path),
        "parameter_count": len(params),
        # The rows the statement *changed*, which is what these commands are for. `row_count`
        # would be the rows a SELECT returned — 0 for every one of them.
        "row_count": int(result.get("affected_rows") or 0),
    }


def run_sql_to_xlsx(
    *,
    target: str,
    sql_text: str,
    database: str = "",
    credential_name: str = "",
    data_dir: str | Path | None = None,
    max_rows: int = DEFAULT_SQL_TO_XLSX_MAX_ROWS,
    timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run SQL on ``target`` and return its first result set as data for an xlsx.

    A thin wrapper over ``python -m db_ops.common.cli run-sql`` — the shared "run SQL on one
    database" engine, which owns target resolution, the driver/pymssql fallback, the row cap and
    the always-rollback contract (see ``db_ops/common/sql_run.py`` for what rollback does and does
    not undo). Telegram adds only the export-shaped part: a result set is *required*, because a
    workbook with no sheet is not a deliverable, so a script that returns none is an error here.
    Raises :class:`SqlToXlsxError` with an operator-readable message.

    Read with :func:`common_cli.run_allowing_failure` rather than :func:`common_cli.run`: a
    failure here becomes a :class:`SqlToXlsxError` carrying the reason, which is what the reply
    template echoes to the operator — a raised ``CommonCliError`` would arrive with the command
    name pasted in front of it. The key is **not** forwarded — the child inherits ``DB_OPS_SECRET_KEY`` from this process's
    environment, which is where the daemon put it, and putting a passphrase on a command line
    would publish it to the process table.
    """
    success, result, error = common_cli.run_allowing_failure("run-sql", {
        "target": target,
        "sql": sql_text,
        "database": database,
        "credential_name": credential_name,
        "max_rows": max_rows,
        "timeout_seconds": timeout_seconds,
        "data_dir": str(data_dir) if data_dir else "",
    })
    if not success:
        raise SqlToXlsxError(error or "run-sql failed without a reason.")
    if not result["columns"]:
        raise SqlToXlsxError(
            "The statement returned no result set to export. "
            "End with a SELECT that returns rows (temp tables are fine)."
        )
    return {
        "server_id": result["server_id"],
        "database": result["database"],
        # Which login ran it: the command config rarely names one, so this is the instance
        # default — worth showing in the reply/log, since it decides what the SQL could touch.
        "credential_name": result.get("credential_name", ""),
        "username": result.get("username", ""),
        "columns": result["columns"],
        "rows": result["rows"],
        "row_count": result["row_count"],
        "affected_rows": result["affected_rows"],
        "truncated": result["truncated"],
    }


def build_sql_params(parameter_config: list[dict[str, Any]], *, args: list[str]) -> list[Any]:
    params: list[Any] = []
    for item in parameter_config:
        source = str(item.get("source") or "arg")
        if source != "arg":
            raise RuntimeError(f"Unsupported SQL parameter source: {source}")
        position = int(item.get("position", len(params) + 1))
        if position < 1:
            raise RuntimeError(f"Parameter position must be >= 1: {position}")
        required = bool(item.get("required", True))
        value = args[position - 1] if len(args) >= position else None
        if required and (value is None or str(value).strip() == ""):
            name = str(item.get("name") or f"arg_{position}")
            raise RuntimeError(f"Missing required argument: {name}")
        params.append(value)
    return params


def find_database(config: dict[str, Any]) -> dict[str, Any]:
    inventory = data_sources.load_inventory()
    for server in inventory:
        if str(server.get("server_id", "")) != str(config.get("server_id", "")):
            continue
        for database in server.get("databases", []) or []:
            if str(database.get("db_type", "")).lower() != str(config.get("db_type", "sqlserver")).lower():
                continue
            if str(database.get("service_name", "")) != str(config.get("service_name", "")):
                continue
            instance_name = str(database.get("instance_name") or database.get("sid") or "")
            if str(config.get("instance_name", "")) and instance_name != str(config.get("instance_name", "")):
                continue
            resolved = dict(database)
            resolved["server_id"] = server.get("server_id")
            resolved["company_code"] = server.get("company_code")
            resolved["ip"] = server.get("ip")
            return resolved
    raise RuntimeError(f"Telegram SQL target not found: {config.get('server_id')}/{config.get('service_name')}")


def find_credential(config: dict[str, Any]) -> dict[str, Any]:
    """The credential the command's ``action_config`` names — required, never inferred.

    Selection is the shared :func:`db_ops.common.data_sources.find_database_credential`.
    """
    return data_sources.find_database_credential(
        data_sources.load_credentials("sqlserver"),
        server_id=str(config.get("server_id", "")),
        credential_name=str(config.get("credential_name", "")),
        db_type=str(config.get("db_type", "sqlserver")),
        service_name=str(config.get("service_name", "")),
        instance_name=str(config.get("instance_name", "")),
    )


def resolve_telegram_sql_file(file_name: str) -> Path:
    path = Path(file_name)
    candidates = [path] if path.is_absolute() else [
        TOOL_ROOT / path,
        *asset_candidates("sql_telegram_commands", str(path)),
    ]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    raise FileNotFoundError(f"Telegram SQL command file not found: {file_name}")

