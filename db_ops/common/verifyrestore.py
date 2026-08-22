"""Proving a restored database is actually usable — not merely that the restore command returned.

A restore that "succeeded" and left a database nobody can query is the failure this exists to
catch, and every engine has its own version of it: SQL Server leaves databases ``RESTORING`` when a
chain was never recovered, Oracle mounts without opening, PostgreSQL starts in recovery and never
promotes. All three report success at the command that put them there.

So the check is the same question in three dialects: **is it open, and does it answer?** A state
column alone is not enough — a database can read ``ONLINE`` and still refuse a query while it
finishes an upgrade step — so a real statement is run against it.
"""

from __future__ import annotations

import shlex
from typing import Any

from db_ops.common.hostcmd import parse_host, run


class VerifyError(ValueError):
    """The verification could not be performed. A database that *failed* is a result, not this."""


def verify(request: dict[str, Any]) -> dict[str, Any]:
    """Check restored databases. Returns per-database rows; never raises for a bad verdict."""
    db_type = str(request.get("db_type") or "").strip().lower()
    if db_type == "sqlserver":
        return _sqlserver(request)
    if db_type == "oracle":
        return _oracle(request)
    if db_type in {"postgresql", "postgres"}:
        return _postgresql(request)
    raise VerifyError(f"db_type must be sqlserver, oracle or postgresql; got {db_type!r}.")


def _verdict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = bool(rows) and all(r.get("ok") for r in rows)
    return {"ok": ok, "databases": rows,
            "checked": len(rows), "failed": sum(1 for r in rows if not r.get("ok"))}


def _sqlserver(request: dict[str, Any]) -> dict[str, Any]:
    target = request.get("target") or {}
    if not str(target.get("host") or "").strip():
        raise VerifyError("target.host is required for sqlserver.")
    wanted = [str(d).strip() for d in (request.get("databases") or []) if str(d).strip()]

    from db_ops.common.db_connect import connect_engine

    connection = connect_engine(
        db_type="sqlserver", host=str(target["host"]), port=int(target.get("port") or 1433),
        database="master", username=str(target.get("username") or ""),
        password=str(target.get("password") or ""), autocommit=True,
        statement_timeout_seconds=0,
    )
    rows: list[dict[str, Any]] = []
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT name, state_desc, recovery_model_desc FROM sys.databases "
                       "WHERE database_id > 4")
        found = [(r[0], r[1], r[2]) for r in cursor.fetchall()]
        for name, state, recovery in found:
            if wanted and name not in wanted:
                continue
            answered, detail = False, ""
            if state == "ONLINE":
                try:
                    # A real query, not just the state column: a database can read ONLINE and still
                    # refuse one while it finishes an upgrade step.
                    cursor.execute(
                        f"SELECT COUNT(*) FROM [{name.replace(']', ']]')}].sys.tables")
                    detail = f"{cursor.fetchone()[0]} user tables"
                    answered = True
                except Exception as exc:  # noqa: BLE001 - a refusal is the finding.
                    detail = str(exc)[:200]
            else:
                detail = f"state is {state}"
            rows.append({"database": name, "state": state, "recovery_model": recovery,
                         "ok": answered, "detail": detail})
    finally:
        connection.close()

    missing = [name for name in wanted if name not in {r["database"] for r in rows}]
    for name in missing:
        # Asked about and not there at all - a restore that never created it. Silence here would
        # let an empty check pass for a database that does not exist.
        rows.append({"database": name, "state": "ABSENT", "ok": False,
                     "detail": "not present on the instance"})
    return _verdict(rows)


_ORACLE_SQL = ("set heading off feedback off pagesize 0\n"
               "select open_mode from v$database;\nselect count(*) from dual;\nexit\n")


def _oracle(request: dict[str, Any]) -> dict[str, Any]:
    host = parse_host(request.get("host"))
    result = run(host, "printf %s " + shlex.quote(_ORACLE_SQL) + " | sqlplus -s -L / as sysdba",
                 timeout=int(request.get("timeout_seconds") or 300))
    text = " ".join(result["stdout"].split())
    open_mode = "UNKNOWN"
    for mode in ("READ WRITE", "READ ONLY", "MOUNTED"):
        if mode in text.upper():
            open_mode = mode
            break
    # Mounted is exactly the trap: RMAN finished, the instance is up, and the database is not open.
    answered = open_mode in {"READ WRITE", "READ ONLY"} and "ORA-" not in text
    return _verdict([{"database": str(request.get("database") or "instance"),
                      "state": open_mode, "ok": answered, "detail": text[:200]}])


def _postgresql(request: dict[str, Any]) -> dict[str, Any]:
    host = parse_host(request.get("host"))
    # -U is not optional. psql defaults to the OS user, and `docker exec` runs as root, so the
    # check failed with `FATAL: role "root" does not exist` against a cluster that was perfectly
    # healthy - a verification that reports the verifier's own login problem as the database's.
    user = str(request.get("username") or "postgres").strip()
    command = (f"psql -U {shlex.quote(user)} -tAX "
               "-c 'select pg_is_in_recovery()' -c 'select count(*) from pg_database' 2>&1")
    result = run(host, command, timeout=int(request.get("timeout_seconds") or 300))
    text = " ".join(result["stdout"].split())
    in_recovery = text.lower().startswith("t")
    answered = result["exit_code"] == 0 and not in_recovery
    return _verdict([{"database": str(request.get("database") or "cluster"),
                      # Still in recovery is the PostgreSQL version of "restored but not usable":
                      # the server is up and refuses writes, and nothing said so.
                      "state": "IN RECOVERY" if in_recovery else "ACCEPTING",
                      "ok": answered, "detail": text[:200]}])
