"""Where a SQL task target runs, in words that are true - and, when it fails, which part failed.

`SQL033-NIGHTLY-ENGINE` failed on 2026-09-24 with *Target database not found in
database-inventory.json: ACME-192-0-2-50/APPDB-PROD/APPDB_PROD*. Every part of that sentence
misled: the file consulted was `db_instances.json`; nothing had been connected to - a pre-check had
compared the target's `database_name` case-sensitively with a list nobody maintains (`APPDB_PROD`
against `APPDB_Prod`); and `APPDB-PROD` is a `service_name`, a label SQL Server does not have and the
connection never uses.

So the runner now connects first and asks the server only when that fails, and these are the rules
it names things and reads failures by. Pure: text in, text out, nothing from ``db_ops``.
"""

from __future__ import annotations

import difflib
from typing import Any, Iterable

#: What a SQL Server connection opens when the target names no database (the operator, 2026-09-24).
#: The same as `run-sql`, which has always connected SQL Server to `master` rather than to a label.
SQLSERVER_DEFAULT_DATABASE = "master"
#: The instance name of a SQL Server default instance, for a target that names none.
SQLSERVER_DEFAULT_INSTANCE = "MSSQLSERVER"

#: SQL Server's own error numbers, as the driver prints them: `... (4060) (SQLDriverConnect)`.
_DATABASE_MISSING = ("(4060)", "cannot open database")
_LOGIN_FAILED = ("(18456)", "login failed for user")
_UNREACHABLE = ("08001", "hyt00", "08s01", "tcp provider", "named pipes provider", "login timeout expired",
                "server was not found", "network-related", "could not open a connection",
                "timed out", "connection refused")


def is_sqlserver(db_type: str) -> bool:
    return str(db_type or "").strip().lower() in ("sqlserver", "mssql")


def connect_database(db_type: str, database_name: str | None, service_name: str | None) -> str:
    """The database a run actually opens: SQL Server's named database or `master`; Oracle's service
    (it connects by service, not by database); anything else its database, else its label."""
    if is_sqlserver(db_type):
        return str(database_name or "").strip() or SQLSERVER_DEFAULT_DATABASE
    if str(db_type or "").strip().lower() == "oracle":
        return str(service_name or database_name or "").strip()
    return str(database_name or service_name or "").strip()


def location(*, server_id: str, db_type: str, instance_name: str | None = None,
             service_name: str | None = None, database_name: str | None = None) -> str:
    """``server/instance.database`` on SQL Server - never its `service_name`, which it does not have;
    ``server/service`` on Oracle, which connects by service."""
    if is_sqlserver(db_type):
        instance = str(instance_name or "").strip() or SQLSERVER_DEFAULT_INSTANCE
        return f"{server_id}/{instance}.{connect_database(db_type, database_name, service_name)}"
    return f"{server_id}/{connect_database(db_type, database_name, service_name)}"


def classify_connect_failure(text: str) -> str | None:
    """``database`` (it does not exist, or this login cannot open it), ``login``, ``unreachable``,
    or ``None`` when the failure is not about connecting at all (a SQL error inside the script)."""
    lowered = str(text or "").lower()
    if any(marker in lowered for marker in _DATABASE_MISSING):
        return "database"
    if any(marker in lowered for marker in _LOGIN_FAILED):
        return "login"
    if any(marker in lowered for marker in _UNREACHABLE):
        return "unreachable"
    return None


def near_match(name: str, names: Iterable[str]) -> str | None:
    """The existing name ``name`` most likely meant: the same name in another case first, then the
    closest spelling. ``None`` when nothing is close."""
    candidates = [str(item) for item in names]
    wanted = str(name or "").strip().lower()
    for candidate in candidates:
        if candidate.lower() == wanted:
            return candidate
    close = difflib.get_close_matches(wanted, [c.lower() for c in candidates], n=1, cutoff=0.75)
    if not close:
        return None
    return next(c for c in candidates if c.lower() == close[0])


def missing_database_message(*, database_name: str, where: str, existing: list[str]) -> str:
    """Why `database_name` could not be opened, given the names the server actually has."""
    exact = [name for name in existing if name == database_name]
    if exact:
        return (f"database '{database_name}' exists on {where}, but this login cannot open it - give "
                "the login a user in that database")
    guess = near_match(database_name, existing)
    if guess and guess.lower() == database_name.lower():
        # Either the server compares names case-sensitively or the login has no user there; the
        # list cannot say which, so the message does not pretend to.
        return (f"database '{database_name}' could not be opened on {where}; the server has "
                f"'{guess}' - use that spelling. If the login still cannot open it, give it a user "
                "in that database")
    shown = ", ".join(existing[:30]) + (" ..." if len(existing) > 30 else "")
    hint = f" - did you mean '{guess}'?" if guess else ""
    return f"database '{database_name}' does not exist on {where}{hint} It has: {shown}."


def instance_not_found_message(*, server_id: str, db_type: str, instance_name: str | None,
                               records: list[dict[str, Any]]) -> str:
    """What `db_instances.json` holds for this server, when no record matches the target."""
    mine = [r for r in records if str(r.get("server_id") or "") == server_id]
    if not mine:
        return f"{server_id} is not in db_instances.json - register it with instance-add first."
    same_type = [r for r in mine if str(r.get("db_type") or "").lower() == str(db_type or "").lower()]
    if not same_type:
        kinds = ", ".join(sorted({str(r.get("db_type") or "?") for r in mine}))
        return f"{server_id} has no {db_type} instance in db_instances.json - it has: {kinds}."
    names = ", ".join(sorted({str(r.get("instance_name") or r.get("sid") or SQLSERVER_DEFAULT_INSTANCE)
                              for r in same_type}))
    return (f"{server_id} has no {db_type} instance '{instance_name}' in db_instances.json - "
            f"it has: {names}. Set the target's instance_name to one of them.")


def instance_matches(record_instance: str | None, target_instance: str | None, db_type: str) -> bool:
    """A target naming no instance matches any; SQL Server instance names are compared ignoring
    case, as SQL Server does."""
    wanted = str(target_instance or "").strip()
    have = str(record_instance or "").strip()
    if not wanted or not have:
        return True
    return have.lower() == wanted.lower() if is_sqlserver(db_type) else have == wanted
