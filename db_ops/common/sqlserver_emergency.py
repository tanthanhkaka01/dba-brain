"""The three things a DBA needs to do to a SQL Server instance from a phone, at 3 a.m.

Shrink a log that has filled its volume, kill the session holding an estate hostage, start the
job that fixes it. Each one is destructive enough that doing it by hand under pressure — over a
VPN, in SSMS, on the wrong instance — is its own risk, and none of them existed as a remote
action before: the 2026-08-09 log-full outage on 192.0.2.115 ran for ten hours partly because
the only way to act was to reach a workstation.

**Why they live in ``common`` and not in an app.** They are reached from a terminal, from the
Telegram command processor and from a shell script, and the whole point of a safety control is
that it behaves identically on all three. One implementation, one confirmation gate, one evidence
record. See :mod:`db_ops.common.confirm`.

**Every one of them looks before it asks.** The confirmation banner names what is actually there —
this file, this session's login and transaction, this job's current run state — because a prompt
that says "kill 723?" when 723 is already gone teaches the operator that the prompt is noise. The
pre-checks run first, the confirmation second, the change third.

**Why the operations take a JSON object.** Same contract as ``run-sql`` and ``host-restart``: one
shape that a config file, a Telegram action and a shell caller all pass through untranslated.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from db_ops.common import confirm, sql_run
from db_ops.common.evidence import FAIL, OK, WARN, GateReport

__all__ = ["EmergencyError", "kill_spid", "shrink_log", "start_job"]

# A SQL Server identifier as it may appear in a request. Deliberately narrow: these values are
# interpolated into T-SQL after quoting, and the cheapest way to be sure quoting is enough is to
# refuse anything that has no business being in a database, file or job name in the first place.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .$#-]{0,127}$")


class EmergencyError(RuntimeError):
    """A user-facing failure: unknown target, missing object, refused confirmation."""


def _quote(name: str) -> str:
    """Bracket-quote an identifier for T-SQL, doubling any closing bracket."""
    return "[" + str(name).replace("]", "]]") + "]"


def _literal(value: str) -> str:
    """Single-quote a string literal for T-SQL, doubling any quote."""
    return "'" + str(value).replace("'", "''") + "'"


def _require_name(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise EmergencyError(f"{field} is required.")
    if not _SAFE_NAME.match(text):
        raise EmergencyError(
            f"{field}={text!r} is not a plain SQL Server name. Letters, digits, space and "
            "_ . $ # - only, up to 128 characters."
        )
    return text


def _connect(request: dict[str, Any], *, data_dir: str | Path | None, database: str = ""):
    """Resolve the target and open one connection, reusing the ``run-sql`` path.

    Target resolution, credential lookup and the driver rules belong to ``sql_run``/``db_connect``
    — this module adds an operation, not a second way to reach a database.
    """
    spec = str(request.get("target") or request.get("server_id") or "").strip()
    if not spec:
        raise EmergencyError('target is required (a server_id, e.g. "ACME-192-0-2-115").')
    try:
        resolved = sql_run.resolve_sqlserver_target(
            spec,
            data_dir=data_dir,
            database=database,
            credential_name=str(request.get("credential_name") or request.get("user_ref") or ""),
        )
    except sql_run.SqlRunError as exc:
        raise EmergencyError(str(exc)) from exc
    timeout = max(1, int(request.get("timeout_seconds") or 60))
    return resolved, sql_run.connect_target(resolved, timeout_seconds=timeout, autocommit=True)


@contextmanager
def _closing(connection):
    """Close the connection on the way out — the drivers here are not context managers.

    A failure to close is swallowed on purpose: it would otherwise replace whatever the operation
    actually reported, and "the log was shrunk but the socket did not close cleanly" must not
    surface as a failed shrink.
    """
    try:
        yield connection
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass


def _rows(cursor, sql_text: str) -> list[list[Any]]:
    _columns, rows, _affected, _truncated = sql_run.execute_capture_first(cursor, sql_text)
    return rows


def _authorize(
    report: GateReport,
    request: dict[str, Any],
    *,
    operation: str,
    target_id: str,
    target_label: str,
    extra_effects: list[str],
) -> bool:
    """Thin alias for :func:`db_ops.common.confirm.authorize_operation`.

    The body moved to ``confirm`` on 2026-08-17 when ``disable-job`` needed the same gate and a
    second copy would have been the third place that decides how hard an operation is to confirm.
    """
    return confirm.authorize_operation(
        report, request, operation=operation, target_id=target_id,
        target_label=target_label, extra_effects=extra_effects)


def _finish(report: GateReport, request: dict[str, Any]) -> dict[str, Any]:
    return report.to_dict(request.get("override") or ())


# --------------------------------------------------------------------------- #
# shrink-log
# --------------------------------------------------------------------------- #
def shrink_log(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """``DBCC SHRINKFILE`` one database's log down to ``size_mb``.

    The size is required and has no default. A shrink is the emergency brake for a log that has
    filled its volume, and "to 0" — the value a forgotten default would most plausibly carry — is
    the one answer that guarantees the file grows straight back, fragmenting VLFs on the way.

    What this deliberately does **not** do is touch the recovery model. Flipping to SIMPLE to
    force truncation is how the 2026-08-09 outage was survived and also why it happened: it breaks
    the log backup chain every time, so the database reports FULL recovery while having no
    point-in-time restore at all. If the log will not shrink because it is waiting on
    ``LOG_BACKUP``, the fix is a log backup, not a smaller file.
    """
    database = _require_name(request.get("database"), "database")
    if request.get("size_mb") in (None, ""):
        raise EmergencyError('size_mb is required (target size of the log file in MB).')
    size_mb = int(request["size_mb"])
    if size_mb < 1:
        raise EmergencyError("size_mb must be at least 1.")

    resolved, connection = _connect(request, data_dir=data_dir, database=database)
    target_id = str(resolved.get("server_id") or request.get("target"))
    report = GateReport("shrink-log", target=f"{target_id}/{database}", echo=echo)
    report.note("target", {"server_id": target_id, "ip": resolved.get("ip"),
                           "database": database, "size_mb": size_mb})
    try:
        with _closing(connection):
            cursor = connection.cursor()
            files = _rows(cursor, f"""
                SELECT f.name, f.size / 128.0,
                       FILEPROPERTY(f.name, 'SpaceUsed') / 128.0, f.physical_name,
                       d.log_reuse_wait_desc, d.recovery_model_desc
                FROM sys.database_files AS f
                CROSS JOIN (SELECT log_reuse_wait_desc, recovery_model_desc
                            FROM sys.databases WHERE name = DB_NAME()) AS d
                WHERE f.type_desc = 'LOG';
            """)
            if not files:
                report.add("log-file", FAIL, f"{database} has no log file visible to this login.")
                return _finish(report, request)

            name, size, used, physical, reuse_wait, recovery = files[0]
            size, used = float(size or 0), float(used or 0)
            report.note("log_file", {"name": name, "physical_name": physical,
                                     "size_mb": round(size, 1), "used_mb": round(used, 1),
                                     "log_reuse_wait": reuse_wait, "recovery_model": recovery})
            report.add("log-file", OK,
                       f"{name} is {size:,.0f} MB, {used:,.0f} MB used "
                       f"({(used / size * 100) if size else 0:.1f}%), "
                       f"recovery={recovery}, log_reuse_wait={reuse_wait}")

            # Not blocking: an operator who has read this and still wants the file smaller is
            # entitled to it. Silence would be worse — this is the single most common reason a
            # shrink appears to do nothing at all.
            if str(reuse_wait or "").upper() == "LOG_BACKUP":
                report.add("log-reuse", WARN,
                           f"{database} is waiting on LOG_BACKUP: the active log cannot be "
                           "released until a log backup runs, so this shrink will free little or "
                           "nothing and the file will grow straight back. Back the log up first.",
                           blocking=False)
            if size <= size_mb:
                report.add("size", OK,
                           f"already {size:,.0f} MB, at or below the requested {size_mb:,} MB — "
                           "nothing to do.")
                return _finish(report, request)

            if request.get("dry_run"):
                report.add("dry-run", OK,
                           f"would run DBCC SHRINKFILE ({name}, {size_mb}) — nothing executed.")
                return _finish(report, request)

            if not _authorize(report, request, operation="shrink-log", target_id=target_id,
                              target_label=f"{target_id} — database {database}, log file {name}",
                              extra_effects=[f"{name}: {size:,.0f} MB -> {size_mb:,} MB"]):
                return _finish(report, request)

            cursor.execute(f"DBCC SHRINKFILE ({_quote(name)}, {size_mb});")
            after = _rows(cursor, f"""
                SELECT f.size / 128.0, FILEPROPERTY(f.name, 'SpaceUsed') / 128.0
                FROM sys.database_files AS f WHERE f.name = {_literal(name)};
            """)
            new_size = float(after[0][0] or 0) if after else 0.0
            report.note("after", {"size_mb": round(new_size, 1)})
            report.add("shrink", OK if new_size < size else WARN,
                       f"{name}: {size:,.0f} MB -> {new_size:,.0f} MB"
                       + ("" if new_size < size else " (unchanged — the log is still active)"),
                       blocking=False)
    except EmergencyError:
        raise
    except Exception as exc:  # noqa: BLE001 - an operator message, not a trace.
        report.add("shrink", FAIL, f"failed: {exc}")
    return _finish(report, request)


# --------------------------------------------------------------------------- #
# kill-spid
# --------------------------------------------------------------------------- #
def kill_spid(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """``KILL`` one session, after showing whose it is and what it is holding.

    The banner carries the login, host, program, transaction age and how many sessions are blocked
    behind it, because "kill 723" is not a decision anyone can make — "kill the RPA connection
    that has held a transaction open for three days and is blocking fourteen sessions" is.

    Rollback is the part that surprises people: killing a session that has written for an hour
    starts an hour of rollback during which nothing is released. The estimate SQL Server offers is
    reported when there is one.
    """
    if request.get("spid") in (None, ""):
        raise EmergencyError("spid is required.")
    spid = int(request["spid"])
    if spid <= 50:
        raise EmergencyError(f"spid={spid} is a system session; refusing.")

    resolved, connection = _connect(request, data_dir=data_dir)
    target_id = str(resolved.get("server_id") or request.get("target"))
    report = GateReport("kill-spid", target=f"{target_id}/SPID={spid}", echo=echo)
    report.note("target", {"server_id": target_id, "ip": resolved.get("ip"), "spid": spid})
    try:
        with _closing(connection):
            cursor = connection.cursor()
            found = _rows(cursor, f"""
                SELECT s.login_name, ISNULL(s.host_name, ''), ISNULL(s.program_name, ''),
                       s.status, s.open_transaction_count,
                       DATEDIFF(SECOND, s.last_request_end_time, GETDATE()),
                       ISNULL(DB_NAME(s.database_id), ''),
                       (SELECT COUNT(*) FROM sys.dm_exec_requests AS r
                        WHERE r.blocking_session_id = s.session_id),
                       ISNULL(DATEDIFF(SECOND,
                            (SELECT MIN(tat.transaction_begin_time)
                             FROM sys.dm_tran_session_transactions AS tst
                             JOIN sys.dm_tran_active_transactions AS tat
                               ON tat.transaction_id = tst.transaction_id
                             WHERE tst.session_id = s.session_id), GETDATE()), -1)
                FROM sys.dm_exec_sessions AS s
                WHERE s.session_id = {spid} AND s.is_user_process = 1;
            """)
            if not found:
                # Not a failure of the operator's intent, but nothing must be killed: session ids
                # are reused, so a stale one now belongs to somebody else.
                report.add("session", FAIL,
                           f"SPID {spid} is not an active user session on {target_id}. It has "
                           "already ended, or the id now belongs to a different connection. "
                           "Nothing was killed.")
                return _finish(report, request)

            login, host, program, status, open_tran, idle_s, dbname, blocking, tran_age = found[0]
            facts = {"login": login, "host": host, "program": program, "status": status,
                     "open_transactions": int(open_tran or 0), "idle_seconds": int(idle_s or 0),
                     "database": dbname, "blocking_sessions": int(blocking or 0),
                     "transaction_age_seconds": int(tran_age or -1)}
            report.note("session", facts)
            report.add("session", OK,
                       f"SPID {spid}: {login}@{host} ({program or 'no program'}), {status}, "
                       f"db={dbname or 'n/a'}, open_tran={facts['open_transactions']}, "
                       f"idle={facts['idle_seconds']}s, "
                       f"tran_age={facts['transaction_age_seconds']}s, "
                       f"blocking={facts['blocking_sessions']} session(s)")

            if request.get("dry_run"):
                report.add("dry-run", OK, f"would run KILL {spid} — nothing executed.")
                return _finish(report, request)

            effects = [f"{login}@{host} loses its connection"]
            if facts["open_transactions"]:
                effects.append(
                    f"an open transaction ({facts['transaction_age_seconds']}s old) is rolled "
                    "back; rollback can take longer than the work it undoes"
                )
            if facts["blocking_sessions"]:
                effects.append(f"{facts['blocking_sessions']} blocked session(s) should resume")
            if not _authorize(report, request, operation="kill-spid", target_id=target_id,
                              target_label=f"{target_id} — SPID {spid}, {login}@{host}",
                              extra_effects=effects):
                return _finish(report, request)

            cursor.execute(f"KILL {spid};")
            report.add("kill", OK, f"KILL {spid} issued.", blocking=False)

            status_rows = _rows(cursor, f"KILL {spid} WITH STATUSONLY;") if request.get(
                "report_rollback", True) else []
            if status_rows:
                report.note("rollback", [list(map(str, row)) for row in status_rows])
                report.add("rollback", OK, "; ".join(str(cell) for cell in status_rows[0]),
                           blocking=False)
    except EmergencyError:
        raise
    except Exception as exc:  # noqa: BLE001
        # `KILL ... WITH STATUSONLY` raises when there is no rollback to report, which is the
        # ordinary case for a sleeping session. That must not read as a failed kill.
        if "STATUSONLY" in str(exc) or "6118" in str(exc):
            report.add("rollback", OK, "no rollback in progress (nothing to estimate).",
                       blocking=False)
        else:
            report.add("kill", FAIL, f"failed: {exc}")
    return _finish(report, request)


# --------------------------------------------------------------------------- #
# start-job
# --------------------------------------------------------------------------- #
def start_job(
    request: dict[str, Any],
    *,
    data_dir: str | Path | None = None,
    echo: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Start a SQL Server Agent job by name, now.

    ``sp_start_job`` returns as soon as the job is *handed to* the Agent, so success here means
    "started", never "finished". The report says so rather than letting an operator read a green
    result as a completed backup.

    Refuses a job that is already running. Starting a second instance of a maintenance job is not
    possible in Agent anyway, and the error it returns is less clear than saying so up front.
    """
    job = _require_name(request.get("job_name") or request.get("job"), "job_name")

    resolved, connection = _connect(request, data_dir=data_dir, database="msdb")
    target_id = str(resolved.get("server_id") or request.get("target"))
    report = GateReport("start-job", target=f"{target_id}/{job}", echo=echo)
    report.note("target", {"server_id": target_id, "ip": resolved.get("ip"), "job_name": job})
    try:
        with _closing(connection):
            cursor = connection.cursor()
            found = _rows(cursor, f"""
                SELECT j.enabled,
                       CONVERT(varchar(19), ja.start_execution_date, 120),
                       CASE WHEN ja.start_execution_date IS NOT NULL
                             AND ja.stop_execution_date IS NULL THEN 1 ELSE 0 END,
                       CONVERT(varchar(19), ja.stop_execution_date, 120)
                FROM msdb.dbo.sysjobs AS j
                OUTER APPLY (
                    SELECT TOP 1 a.start_execution_date, a.stop_execution_date
                    FROM msdb.dbo.sysjobactivity AS a
                    WHERE a.job_id = j.job_id
                    ORDER BY a.start_execution_date DESC
                ) AS ja
                WHERE j.name = {_literal(job)};
            """)
            if not found:
                report.add("job", FAIL,
                           f"no Agent job named {job!r} on {target_id}. Nothing was started.")
                return _finish(report, request)

            enabled, started_at, running, stopped_at = found[0]
            report.note("job", {"enabled": bool(enabled), "running": bool(running),
                                "last_start": started_at, "last_stop": stopped_at})
            if int(running or 0):
                report.add("job", FAIL,
                           f"{job!r} is already running (started {started_at}). Agent will not "
                           "start a second instance. Nothing was started.")
                return _finish(report, request)
            report.add("job", OK,
                       f"{job!r} found, enabled={bool(enabled)}, last run "
                       f"{started_at or 'never'}{' -> ' + stopped_at if stopped_at else ''}")
            if not enabled:
                # sp_start_job runs a disabled job quite happily; the operator should know that is
                # what they are about to do.
                report.add("enabled", WARN,
                           f"{job!r} is disabled. Starting it runs it once now; it stays disabled "
                           "on its schedule.", blocking=False)

            if request.get("dry_run"):
                report.add("dry-run", OK,
                           f"would run msdb.dbo.sp_start_job @job_name = {job!r} — nothing executed.")
                return _finish(report, request)

            if not _authorize(report, request, operation="start-job", target_id=target_id,
                              target_label=f"{target_id} — Agent job {job!r}",
                              extra_effects=[f"{job!r} starts immediately on {target_id}"]):
                return _finish(report, request)

            cursor.execute(f"EXEC msdb.dbo.sp_start_job @job_name = {_literal(job)};")
            report.add("start", OK,
                       f"{job!r} handed to SQL Server Agent. This reports the START only — check "
                       "the job history for its outcome.", blocking=False)
    except EmergencyError:
        raise
    except Exception as exc:  # noqa: BLE001
        report.add("start", FAIL, f"failed: {exc}")
    return _finish(report, request)
