"""Who is holding this transaction — the person, not the service account.

On an application-tier estate every SQL Server session looks the same from the database:
`login_name=salesdbadmin, host_name=ACMEAOS04, program_name=salesOnline`. That is the AOS, the shared
service account and the middle tier; it names no user, no workstation and no business process. An
alert saying "SPID 505 has held 2,481 locks for 6 hours" is therefore true and unusable.

Tracing one such session by hand on 2026-08-11 took eleven queries across four sources, and the
answer was in the first one all along: **Dynamics AX writes the caller into
``sys.dm_exec_sessions.context_info``**, as ASCII, in the shape

    <ax_user_id> <ax_session_id> <client_type> - <mode>      e.g. "ACMECEN01.PU 4654 CLIENT - read-only 0"

so `ACMECEN01.PU` -> `USERINFO` -> "ORDER PROCESSING PU&NON". This module makes that one call.
The rule applies literally here: the hand-traced version took its parsing, its joins and
its dead ends with it, and the next person would have rediscovered them.

**What it cannot tell you, and why.** The end-user workstation is not recoverable from the database
on this estate, and this was checked rather than assumed: `SYSCLIENTSESSIONS.CLIENTCOMPUTER` and
`SYSUSERLOG.COMPUTERNAME` both record the *AOS*, the AOS holds no inbound client TCP connections,
and Windows logon auditing is off there. `client_net_address` is returned because on a two-tier
estate it *is* the workstation — it is the AOS on a three-tier one, and the output says which by
naming it `client_net_address` rather than pretending.

Nothing here is AX-specific except the `context_info` convention: the DMV half works on any SQL
Server, and a session whose `context_info` is empty simply reports no application identity.

Read-only by construction: it runs through :func:`db_ops.common.sql_run.run_sql`, which rolls back.
"""

from __future__ import annotations

from typing import Any

from db_ops.common import sql_run


class SessionTraceError(RuntimeError):
    """The trace could not run — an operator message."""


#: Sessions younger than this are ordinary traffic, not something to look at. Overridable per call.
DEFAULT_MIN_TRAN_SECONDS = 60

#: The diagnostic itself. Deliberately one statement over DMVs only, so it runs on any SQL Server
#: and needs no permissions beyond VIEW SERVER STATE.
#:
#: `log_bytes_used` is the field that decides what an old transaction actually *is*: a few thousand
#: bytes and a sleeping session is a client that opened a transaction and walked away, while a
#: growing figure is work in progress. Reporting the age alone cannot tell those apart, and they
#: want opposite responses.
_TRACE_SQL = """
SELECT
    s.session_id,
    s.login_name,
    s.host_name                         AS client_host,
    s.program_name,
    s.host_process_id,
    c.client_net_address,
    s.login_time,
    s.status                            AS session_status,
    r.status                            AS request_status,
    r.command,
    r.wait_type,
    r.blocking_session_id,
    RTRIM(LTRIM(CAST(s.context_info AS varchar(128)))) AS app_context,
    at.transaction_begin_time,
    DATEDIFF(second, at.transaction_begin_time, GETDATE()) AS tran_age_seconds,
    at.transaction_type,
    dt.database_transaction_log_bytes_used   AS log_bytes_used,
    dt.database_transaction_log_record_count AS log_records,
    (SELECT COUNT(*) FROM sys.dm_tran_locks l WHERE l.request_session_id = s.session_id) AS locks_held,
    (SELECT COUNT(*) FROM sys.dm_exec_sessions b WHERE b.session_id IN
        (SELECT br.session_id FROM sys.dm_exec_requests br WHERE br.blocking_session_id = s.session_id)
    ) AS blocked_sessions,
    STUFF((SELECT ',' + CAST(br.session_id AS varchar(12))
           FROM sys.dm_exec_requests br WHERE br.blocking_session_id = s.session_id
           FOR XML PATH('')), 1, 1, '') AS blocked_session_ids,
    SUBSTRING(t.text, 1, 400)           AS last_sql
FROM sys.dm_tran_session_transactions st
JOIN sys.dm_tran_active_transactions at ON at.transaction_id = st.transaction_id
JOIN sys.dm_exec_sessions s ON s.session_id = st.session_id
LEFT JOIN sys.dm_exec_connections c ON c.session_id = s.session_id
LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id
LEFT JOIN sys.dm_tran_database_transactions dt
       ON dt.transaction_id = st.transaction_id AND dt.database_id = DB_ID()
OUTER APPLY sys.dm_exec_sql_text(c.most_recent_sql_handle) t
WHERE s.is_user_process = 1
  {filters}
ORDER BY tran_age_seconds DESC;
"""


def _parse_app_context(text: str) -> dict[str, str]:
    """Split the ``context_info`` convention into fields, without insisting on it.

    Any application may put anything in ``context_info``; only Dynamics AX is known to use this
    shape here. So the raw text is always returned and the parsed fields are best-effort — a
    caller that guessed wrong gets empty strings, not a wrong user id.
    """
    raw = str(text or "").strip()
    if not raw:
        return {"app_user": "", "app_session": "", "app_client_type": "", "app_context_raw": ""}
    parts = raw.split()
    session = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
    return {
        "app_user": parts[0] if parts and not parts[0].isdigit() else "",
        "app_session": session,
        "app_client_type": parts[2] if len(parts) > 2 else "",
        "app_context_raw": raw,
    }


def _user_names(*, request: dict[str, Any], user_ids: list[str]) -> dict[str, str]:
    """Display names for the application user ids, when the connected database can supply them.

    A separate query rather than a join, because `USERINFO` is a Dynamics AX table: referencing it
    in the main statement would make the whole diagnostic fail to compile on every other database.
    A failure here costs the display name and nothing else.
    """
    if not user_ids:
        return {}
    quoted = ", ".join("'" + uid.replace("'", "''") + "'" for uid in sorted(set(user_ids)))
    lookup = dict(request)
    lookup["sql"] = f"SELECT ID, NAME FROM USERINFO WHERE ID IN ({quoted});"
    try:
        result = sql_run.run_sql(lookup)
    except Exception:  # noqa: BLE001 - not an AX database, no such table, or no rights to it.
        return {}
    return {str(row[0]): str(row[1] or "") for row in (result.get("rows") or [])}


def trace_sessions(request: Any) -> dict[str, Any]:
    """Every open transaction on the target, with whoever the application says is behind it.

    Request fields (a JSON object, like every other ``common`` command):

    ``target``            (required) server_id, or ``<db_type> <ip> [port]``
    ``database``          which database to report transaction log usage for (default: the login's)
    ``session_id``        trace exactly one SPID instead of scanning
    ``min_tran_seconds``  ignore transactions younger than this (default 60)
    ``blocking_only``     only sessions that are blocking someone
    ``credential_name`` / ``data_dir`` / ``timeout_seconds`` — as ``run-sql``
    """
    if not isinstance(request, dict):
        raise SessionTraceError("The request must be a JSON object.")
    target = str(request.get("target") or "").strip()
    if not target:
        raise SessionTraceError("target is required (a server_id, or '<db_type> <ip> [port]').")

    filters = []
    session_id = request.get("session_id")
    try:
        # 0 means "no filter", not "SPID 0". Session ids 1-50 are SQL Server's own workers and 0 is
        # never a user session, so it is free as a sentinel — and a caller that has to pass
        # *something* (a Telegram command with one required argument, a shell wrapper) needs one.
        # Without it, `session_id: 0` silently matched nothing and read as "no long transactions".
        session_id = None if session_id in (None, "", 0, "0") else int(session_id)
    except (TypeError, ValueError):
        raise SessionTraceError(f"session_id must be a number, got {session_id!r}.") from None
    if session_id is not None:
        filters.append(f"AND s.session_id = {session_id}")
    else:
        try:
            min_age = int(request.get("min_tran_seconds", DEFAULT_MIN_TRAN_SECONDS))
        except (TypeError, ValueError):
            raise SessionTraceError("min_tran_seconds must be a number.") from None
        filters.append(f"AND DATEDIFF(second, at.transaction_begin_time, GETDATE()) >= {min_age}")
    if request.get("blocking_only"):
        filters.append("AND EXISTS (SELECT 1 FROM sys.dm_exec_requests br "
                       "WHERE br.blocking_session_id = s.session_id)")

    run_request = {
        key: value for key, value in request.items()
        if key in {"target", "database", "credential_name", "data_dir", "timeout_seconds"}
    }
    run_request["sql"] = _TRACE_SQL.format(filters="\n  ".join(filters))
    result = sql_run.run_sql(run_request)

    columns = [str(name) for name in (result.get("columns") or [])]
    sessions: list[dict[str, Any]] = []
    for row in result.get("rows") or []:
        record = {columns[i]: row[i] for i in range(min(len(columns), len(row)))}
        record.update(_parse_app_context(record.pop("app_context", "")))
        sessions.append(record)

    names = _user_names(request=run_request,
                        user_ids=[str(s["app_user"]) for s in sessions if s.get("app_user")])
    for record in sessions:
        record["app_user_name"] = names.get(str(record.get("app_user") or ""), "")

    # One row per *transaction*, not per session: `sys.dm_tran_session_transactions` returns a row
    # for each, and a session with nested transactions legitimately appears more than once (SPID 135
    # did, live, with two). Counting them as sessions would have reported "2 sessions" for one.
    return {
        "ok": True,
        "server_id": result.get("server_id", ""),
        "database": result.get("database", ""),
        "transaction_count": len(sessions),
        "session_count": len({record.get("session_id") for record in sessions}),
        "sessions": [_json_safe(record) for record in sessions],
    }


def _json_safe(record: dict[str, Any]) -> dict[str, Any]:
    """Driver values (datetime, Decimal) rendered as strings, so the response serializes."""
    out: dict[str, Any] = {}
    for key, value in record.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def describe(session: dict[str, Any]) -> str:
    """One operator-readable line per session — what a Telegram message or a log quotes."""
    who = str(session.get("app_user") or "")
    name = str(session.get("app_user_name") or "")
    if who and name:
        who = f"{who} ({name})"
    age = session.get("tran_age_seconds")
    minutes = f"{int(age) // 60}m" if isinstance(age, int) else "?"
    blocked = session.get("blocked_session_ids") or ""
    parts = [
        f"spid={session.get('session_id')}",
        f"app_user={who or 'unknown'}",
        f"app_session={session.get('app_session') or '-'}",
        f"tran_age={minutes}",
        f"began={session.get('transaction_begin_time')}",
        f"log_bytes={session.get('log_bytes_used')}",
        f"locks={session.get('locks_held')}",
        f"status={session.get('session_status')}/{session.get('request_status') or 'no-request'}",
        f"via={session.get('client_host')}@{session.get('client_net_address')}",
    ]
    if blocked:
        parts.append(f"blocking={blocked}")
    return ", ".join(parts)
