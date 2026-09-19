"""What state a database server is in — asked once, in one shape, for every engine.

The question *"is it up, and can it be used"* is asked all over this project: by the restore
workflow before it may call a drill done, by the SLA grader, by the fleet page, by `/spbot_*`
commands. Each of them used to answer it its own way, which is how three callers grow three
notions of "online" and disagree about the same server on the same afternoon.

**Three depths, because three different questions are asked** — and which one is *correct* depends
on the engine, not on the caller's appetite:

``instance``
    Is the engine up and answering. Always checked, at every depth: a verdict about a database on
    an instance nobody could reach is not a verdict, it is a guess.
``database``
    One named database's own state. This is the depth SQL Server needs, because it restores one
    database at a time and each can fail on its own.
``schema``
    A schema inside a database, and whether the asking login can actually see it.

PostgreSQL and Oracle restore at the level of the **instance** — ``pg_basebackup`` plus WAL replay,
RMAN ``DUPLICATE`` — so for a restore the instance depth is the whole answer there and walking the
databases asks the same question repeatedly. The deeper depths still exist for those engines
because other callers ask other questions of them.

**A state column is never the whole answer.** A database can read ``ONLINE`` and still refuse a
query while it finishes an upgrade step; an Oracle instance can be ``OPEN`` with the database only
``MOUNTED``; a PostgreSQL cluster accepts connections while it is still in recovery and refuses
every write. So a real statement is run at whatever depth was asked for, and the state column is
reported beside the answer rather than instead of it.

This is the verdict layer over :mod:`db_ops.common.db_catalog`, which lists what exists. Listing and
judging are kept apart on purpose: a listing that also decides has to be re-read every time the
rule changes.
"""

from __future__ import annotations

from typing import Any

from db_ops.common import db_catalog
from db_ops.lib.sql_access import normalize_db_type

#: The depths, in the order they nest. A caller asking for a deeper one gets the shallower ones
#: checked too, because the deeper answer is only meaningful if the shallower one held.
DEPTHS = ("instance", "database", "schema")


class DbStatusError(ValueError):
    """The status could not be *determined*. A server that is down is a result, not this."""


#: One statement per engine that proves the instance is answering, and returns something worth
#: printing beside the verdict. Deliberately cheap: this runs on every call, including the ones
#: that only wanted a schema, and a probe that costs a table scan will not be run often enough.
_INSTANCE_SQL = {
    "sqlserver": (
        "SELECT CONVERT(varchar(200), SERVERPROPERTY('ProductVersion')) AS version, "
        "CONVERT(varchar(30), SERVERPROPERTY('Edition')) AS edition, "
        "DATABASEPROPERTYEX('master','Status') AS state"
    ),
    "postgresql": (
        "select version() as version, "
        "case when pg_is_in_recovery() then 'IN RECOVERY' else 'ACCEPTING' end as state"
    ),
    # `v$instance.status` is the instance and `v$database.open_mode` is the database it opened.
    # Both, because RMAN finishing leaves the first OPEN and the second MOUNTED - which is the
    # Oracle shape of "restored, and unusable".
    "oracle": (
        "select i.version as version, i.status as status, d.open_mode as state "
        "from v$instance i, v$database d"
    ),
}

#: What counts as an instance that can be used, per engine. Anything else is reported with the
#: state it actually had rather than being flattened into "not ok".
_INSTANCE_OK = {
    "sqlserver": {"ONLINE"},
    "postgresql": {"ACCEPTING"},
    "oracle": {"READ WRITE", "READ ONLY"},
}

#: A database SQL Server will let a query through. `RESTORING` is the one this project keeps
#: meeting: the restore returned, the chain was never recovered, and the database is not there.
_DATABASE_OK = {
    "sqlserver": {"ONLINE"},
    "postgresql": {"ACCEPTING", "ONLINE", ""},
    "oracle": {"READ WRITE", "READ ONLY", "OPEN"},
}


def _verdict(instance: dict[str, Any], items: list[dict[str, Any]],
             *, depth: str, db_type: str, server_id: str) -> dict[str, Any]:
    """The one answer shape, whichever engine and whichever depth produced it."""
    failed = sum(1 for item in items if not item.get("ok"))
    return {
        # An instance that did not answer makes the whole reply `ok: false` even when no deeper
        # item failed - there was nothing to fail, which is not the same as nothing being wrong.
        "ok": bool(instance.get("ok")) and failed == 0,
        "depth": depth,
        "db_type": db_type,
        "server_id": server_id,
        "instance": instance,
        "items": items,
        "checked": len(items),
        "failed": failed,
    }


def _requested_depth(request: dict[str, Any]) -> str:
    depth = str(request.get("depth") or "instance").strip().lower()
    if depth not in DEPTHS:
        raise DbStatusError(f"depth must be one of {list(DEPTHS)}; got {depth!r}.")
    return depth


def _engine(resolved: dict[str, Any]) -> str:
    engine = normalize_db_type(str(resolved.get("db_type") or ""))
    if engine not in _INSTANCE_SQL:
        raise DbStatusError(
            f"db-status supports {sorted(_INSTANCE_SQL)}; this target is {engine!r}.")
    return engine


def _check_instance(parsed: dict[str, Any], engine: str) -> dict[str, Any]:
    """Connect, run the engine's probe, and say what came back."""
    try:
        rows = db_catalog._query(parsed, _INSTANCE_SQL[engine])
    except db_catalog.DbCatalogError as exc:
        # Could not reach it at all. That is a finding, not an error: the caller asked what state
        # the server is in and "unreachable" is the state.
        return {"ok": False, "state": "UNREACHABLE", "detail": str(exc)[:300]}
    row = rows[0] if rows else {}
    state = str(row.get("state") or "").strip().upper()
    ok = state in _INSTANCE_OK[engine]
    answer = {"ok": ok, "state": state or "UNKNOWN",
              "version": str(row.get("version") or "")[:200]}
    if row.get("status"):                       # oracle: the instance beside the database
        answer["instance_status"] = str(row["status"]).strip().upper()
    if row.get("edition"):
        answer["edition"] = str(row["edition"])
    if not ok:
        answer["detail"] = f"state is {answer['state']}"
    return answer


def _wanted(request: dict[str, Any], key: str) -> list[str]:
    values = request.get(key) or []
    if isinstance(values, str):
        values = [values]
    return [str(v).strip() for v in values if str(v).strip()]


def _check_databases(request: dict[str, Any], parsed: dict[str, Any],
                     engine: str) -> list[dict[str, Any]]:
    listed = db_catalog.list_databases({**request, "include_system": True})
    by_name = {str(d.get("name")): d for d in (listed.get("databases") or [])}
    wanted = _wanted(request, "databases") or sorted(by_name)

    items: list[dict[str, Any]] = []
    for name in wanted:
        found = by_name.get(name)
        if found is None:
            # Asked about and not there. Silence here is how an empty check passes for a database
            # that a restore never created.
            items.append({"name": name, "kind": "database", "state": "ABSENT", "ok": False,
                          "detail": "not present on this instance"})
            continue
        state = str(found.get("state") or "").strip().upper()
        if state not in _DATABASE_OK[engine]:
            items.append({"name": name, "kind": "database", "state": state or "UNKNOWN",
                          "ok": False, "detail": f"state is {state or 'unknown'}"})
            continue
        items.append(_answers(parsed, engine, database=name, name=name, kind="database"))
    return items


def _check_schemas(request: dict[str, Any], parsed: dict[str, Any],
                   engine: str) -> list[dict[str, Any]]:
    database = str(request.get("database") or "").strip()
    if engine in {"sqlserver", "postgresql"} and not database:
        raise DbStatusError(
            'depth "schema" needs a "database" on sqlserver and postgresql: a schema lives inside '
            "one, and without it the answer would describe the login's default instead.")
    listed = db_catalog.list_schemas({**request, "include_system": True})
    present = {str(s.get("name")) for s in (listed.get("schemas") or [])}
    wanted = _wanted(request, "schemas") or sorted(present)

    items: list[dict[str, Any]] = []
    for name in wanted:
        if name not in present:
            items.append({"name": name, "kind": "schema", "state": "ABSENT", "ok": False,
                          "detail": f"not visible in {database or 'this database'} to this login"})
            continue
        items.append({"name": name, "kind": "schema", "state": "PRESENT", "ok": True,
                      "detail": f"visible to {parsed.get('credential_name') or 'this login'}"})
    return items


def _answers(parsed: dict[str, Any], engine: str, *, database: str,
             name: str, kind: str) -> dict[str, Any]:
    """Run a real statement inside one database, because the state column is not the answer."""
    sql = {
        "sqlserver": "SELECT COUNT(*) AS n FROM sys.tables",
        "postgresql": "select count(*) as n from information_schema.tables",
        "oracle": "select count(*) as n from all_tables",
    }[engine]
    try:
        rows = db_catalog._query(parsed, sql, database=database)
    except db_catalog.DbCatalogError as exc:
        return {"name": name, "kind": kind, "state": "ONLINE", "ok": False,
                "detail": str(exc)[:300]}
    tables = rows[0].get("n") if rows else None
    return {"name": name, "kind": kind, "state": "ONLINE", "ok": True,
            "detail": f"answered; {tables} tables visible"}


def status(request: Any) -> dict[str, Any]:
    """What state this target is in, at the depth asked for. Never raises for a bad verdict."""
    if not isinstance(request, dict):
        raise DbStatusError("request must be a JSON object.")
    depth = _requested_depth(request)
    parsed = db_catalog._parsed_request(request)
    resolved = db_catalog._resolve(parsed)
    engine = _engine(resolved)
    server_id = str(resolved.get("server_id") or "")

    instance = _check_instance(parsed, engine)
    items: list[dict[str, Any]] = []
    # An instance nobody could reach cannot be asked about its databases, and asking anyway
    # produces a second, less useful copy of the same failure.
    if instance.get("ok"):
        if depth == "database":
            items = _check_databases(request, parsed, engine)
        elif depth == "schema":
            items = _check_schemas(request, parsed, engine)
    return _verdict(instance, items, depth=depth, db_type=engine, server_id=server_id)
