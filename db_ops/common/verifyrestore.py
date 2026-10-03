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

from db_ops.lib import errors
import shlex
from typing import Any

from db_ops.common.hostcmd import parse_host, run


class VerifyError(errors.RequestError):
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


def _detail(result: dict[str, Any], text: str) -> str:
    """What to tell the reader, including when the command produced nothing.

    A check that cannot reach the database has to say **why**, and the why is often only on stderr:
    ``docker exec`` writing "No such container", ``sudo`` asking for a terminal, ssh refusing. The
    PostgreSQL command redirects ``2>&1`` into its own output and Oracle's does not, so on
    2026-09-19 pointing the check at a container with no Oracle in it, and then at a container that
    does not exist, both answered ``ok=False`` with an **empty detail** — correct, and useless to
    whoever has to fix it.
    """
    if text:
        return text[:200]
    stderr = " ".join(str(result.get("stderr") or "").split())
    if stderr:
        return stderr[:200]
    return f"exit code {result.get('exit_code')}, no output"


def _verdict(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = bool(rows) and all(r.get("ok") for r in rows)
    return {"ok": ok, "databases": rows,
            "checked": len(rows), "failed": sum(1 for r in rows if not r.get("ok"))}


def _sqlserver(request: dict[str, Any]) -> dict[str, Any]:
    target = request.get("target") or {}
    if not str(target.get("host") or "").strip():
        raise VerifyError("target.host is required for sqlserver.")
    wanted = [str(d).strip() for d in (request.get("database_names") or request.get("databases") or [])
              if str(d).strip()]

    from db_ops.common import db_catalog, sql_run
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
        found = [(str(r["name"]), str(r.get("state") or ""), r.get("recovery_model"))
                 for r in db_catalog.databases(cursor, "sqlserver") if not r.get("is_system")]
        for name, state, recovery in found:
            if wanted and name not in wanted:
                continue
            answered, detail = False, ""
            if state == "ONLINE":
                try:
                    # A real query, not just the state column: a database can read ONLINE and still
                    # refuse one while it finishes an upgrade step.
                    tables = sql_run.query_rows(
                        cursor, f"SELECT COUNT(*) AS n FROM [{name.replace(']', ']]')}].sys.tables")
                    detail = f"{tables[0]['n']} user tables"
                    answered = True
                except Exception as exc:  # noqa: BLE001 - a refusal is the finding.
                    detail = str(exc)[:200]
            else:
                detail = f"state is {state}"
            rows.append({"database_name": name, "state": state, "recovery_model": recovery,
                         "ok": answered, "detail": detail})
    finally:
        connection.close()

    missing = [name for name in wanted if name not in {r["database_name"] for r in rows}]
    for name in missing:
        # Asked about and not there at all - a restore that never created it. Silence here would
        # let an empty check pass for a database that does not exist.
        rows.append({"database_name": name, "state": "ABSENT", "ok": False,
                     "detail": "not present on the instance"})
    return _verdict(rows)


_ORACLE_SQL = ("set heading off feedback off pagesize 0\n"
               "select open_mode from v$database;\nselect count(*) from dual;\nexit\n")


def _oracle(request: dict[str, Any]) -> dict[str, Any]:
    host = parse_host(request.get("host"))
    # **Which instance.** `sqlplus / as sysdba` connects to whatever ORACLE_SID the login shell
    # happens to carry. One container with one instance is the common shape and it is right by
    # accident; a host with two SIDs is not, and the check would then answer for an instance nobody
    # restored while labelling the row with the one they did. The restore step has always been told
    # the SID (`restore_by_id` passes `oracle_sid`); this one was not, until 2026-09-19.
    sid = str(request.get("oracle_sid") or "").strip()
    prefix = f"ORACLE_SID={shlex.quote(sid)} " if sid else ""
    result = run(host,
                 "printf %s " + shlex.quote(_ORACLE_SQL) + f" | {prefix}sqlplus -s -L / as sysdba",
                 timeout=int(request.get("timeout_seconds") or 300))
    text = " ".join(result["stdout"].split())
    open_mode = "UNKNOWN"
    for mode in ("READ WRITE", "READ ONLY", "MOUNTED"):
        if mode in text.upper():
            open_mode = mode
            break
    # Mounted is exactly the trap: RMAN finished, the instance is up, and the database is not open.
    answered = open_mode in {"READ WRITE", "READ ONLY"} and "ORA-" not in text
    return _verdict([{"database_name": str(request.get("database_name") or request.get("database") or sid or "instance"),
                      "state": open_mode, "ok": answered,
                      "detail": _detail(result, text)}])


def _postgresql(request: dict[str, Any]) -> dict[str, Any]:
    host = parse_host(request.get("host"))
    # -U is not optional. psql defaults to the OS user, and `docker exec` runs as root, so the
    # check failed with `FATAL: role "root" does not exist` against a cluster that was perfectly
    # healthy - a verification that reports the verifier's own login problem as the database's.
    user = str(request.get("username") or "postgres").strip()
    # **Which cluster.** With no `-p`, psql takes the default port, which is right for the one
    # cluster inside a container and wrong the moment a host runs two. Stated when the caller knows
    # it, left alone when it does not - the same reasoning as the SQL Server port, found the same
    # day and for the same reason.
    port = str(request.get("port") or "").strip()
    port_option = f"-p {shlex.quote(port)} " if port else ""
    command = (f"psql -U {shlex.quote(user)} {port_option}-tAX "
               "-c 'select pg_is_in_recovery()' -c 'select count(*) from pg_database' 2>&1")
    result = run(host, command, timeout=int(request.get("timeout_seconds") or 300))
    text = " ".join(result["stdout"].split())
    reached = result["exit_code"] == 0
    in_recovery = reached and text.lower().startswith("t")
    answered = reached and not in_recovery
    # **A state is only claimed when something answered.** This read
    # `"IN RECOVERY" if in_recovery else "ACCEPTING"`, which has no branch for "the question was
    # never asked": `psql` missing from the container, a role that does not exist, a `sudo` that
    # wanted a terminal — every one of them came back labelled **ACCEPTING**, because the output did
    # not begin with `t`. The verdict was right (`ok: False`) and the word beside it was the
    # opposite of the truth, and the word is what a reader scans. Measured on 2026-09-19 against the
    # store on 192.0.2.115, three ways.
    if not reached:
        state = "NO ANSWER"
    elif in_recovery:
        # Still in recovery is the PostgreSQL version of "restored but not usable": the server is up
        # and refuses writes, and nothing said so.
        state = "IN RECOVERY"
    else:
        state = "ACCEPTING"
    return _verdict([{"database_name": str(request.get("database_name") or request.get("database") or "cluster"),
                      "state": state, "ok": answered, "detail": _detail(result, text)}])
