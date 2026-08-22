"""SQL Server: one `RESTORE` per file, and the recovery flag is the caller's decision.

The two rules that make a chain work, and that this enforces rather than trusts:

* **`NORECOVERY` between steps, `RECOVERY` only on the last.** A database recovered early cannot
  have the remaining logs applied at all — the only fix is to start the whole restore again. So
  ``with_recovery`` defaults to **false**: a caller stepping through a chain gets the safe answer
  without saying so, and the one step that finishes it says it does.
* **`STOPAT` only on a log.** SQL Server accepts it on a full or a differential and *silently
  ignores it there*, which reads as a point-in-time restore that never happened. Asked for on any
  other level, it is refused.
"""

from __future__ import annotations

from typing import Any

from db_ops.common.restorestep import DIFF, FULL, LOG, RestoreStepError


def _quote_name(name: str) -> str:
    return "[" + str(name).replace("]", "]]") + "]"


def _quote_literal(value: str) -> str:
    return "N'" + str(value).replace("'", "''") + "'"


def build_statements(level: str, request: dict[str, Any], paths: list[str]) -> list[str]:
    """The RESTORE statements this step will send. Pure — nothing is executed here."""
    database = str(request.get("database") or "").strip()
    if not database:
        raise RestoreStepError("database is required for sqlserver.")

    with_recovery = bool(request.get("with_recovery", False))
    stopat = str(request.get("stopat") or "").strip()
    if stopat and level != LOG:
        raise RestoreStepError(
            f"stopat applies to a log restore only; SQL Server accepts it on a {level} and "
            "silently ignores it, which reads as a point-in-time restore that never happened."
        )

    move = request.get("move") or {}
    if move and level != FULL:
        raise RestoreStepError("move applies to the full restore only; it is rejected on a log.")
    move_clause = "".join(
        f", MOVE {_quote_literal(logical)} TO {_quote_literal(path)}"
        for logical, path in sorted(move.items())
    )

    target = _quote_name(database)
    verb = "RESTORE LOG" if level == LOG else "RESTORE DATABASE"
    statements: list[str] = []
    for index, path in enumerate(paths, start=1):
        last = index == len(paths)
        options = ["RECOVERY" if (last and with_recovery) else "NORECOVERY", "CHECKSUM",
                   f"STATS = {int(request.get('stats') or 10)}"]
        # REPLACE only on a full: it means "overwrite the existing database", a statement about
        # starting a chain rather than continuing one.
        if level == FULL and index == 1 and bool(request.get("replace", True)):
            options.append("REPLACE")
        if last and stopat:
            options.append(f"STOPAT = {_quote_literal(stopat)}")
        statements.append(
            f"{verb} {target} FROM DISK = {_quote_literal(path)}\n"
            f"    WITH {', '.join(options)}"
            + (move_clause if (level == FULL and index == 1) else "")
            + ";"
        )
    return statements


def apply(level: str, request: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    statements = build_statements(level, request, paths)
    if request.get("dry_run"):
        return {"engine": "sqlserver", "level": level, "applied": [], "statements": statements,
                "dry_run": True}

    target = request.get("target") or {}
    if not str(target.get("host") or "").strip():
        raise RestoreStepError("target.host is required for sqlserver.")

    from db_ops.common.db_connect import connect_engine

    connection = connect_engine(
        db_type="sqlserver", host=str(target["host"]), port=int(target.get("port") or 1433),
        database="master", username=str(target.get("username") or ""),
        password=str(target.get("password") or ""), autocommit=True,
        # No statement timeout: a restore runs for minutes, and the connection layer reads None as
        # "reuse the connect timeout" - which cut the first real restore off at 30s, mid-chain.
        statement_timeout_seconds=0,
    )
    try:
        cursor = connection.cursor()
        for statement in statements:
            cursor.execute(statement)
            # RESTORE reports progress as info messages and can leave result sets behind; draining
            # them keeps the next statement from reading the previous one's.
            while cursor.nextset():
                pass
    finally:
        connection.close()

    return {"engine": "sqlserver", "level": level, "applied": paths,
            "statements": statements,
            "recovered": bool(request.get("with_recovery", False)),
            "stopat": str(request.get("stopat") or "") or None}
