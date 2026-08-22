"""Is db_ops itself running — the one question no other app in db_ops asks.

Every app here watches databases. Nothing watched the watcher, and the cost of that showed on
2026-08-12: a NameError in the SQL task scanner made every scheduled scan exit 1 once a minute
for a day. The daemon kept running, the container stayed up, the metric reports kept arriving —
and not one scheduled SQL task ran. It was found by a person noticing the absence, which is the
worst detector there is, because absence is exactly what nobody notices at 03:00.

So this reads ``job_runs`` — the row every app command writes when the daemon runs it — and
answers two questions the estate's own monitoring cannot:

``summary``    what is the state of every app command right now: when it last ran, whether that
               run worked, how often it has failed lately, and whether it is **overdue** against
               the interval it is configured with. Overdue matters as much as failed: an app that
               stopped being scheduled writes no failure row at all, so a report that only listed
               errors would show a perfectly clean estate.
``failures``   which apps have failed **since this app last said so**, for an alert that goes out
               immediately.

The alert deliberately reports **transitions**, not the current state: an app that has been
broken since Tuesday must not message the group every minute for three days, which is how a
channel becomes something people mute. The still-broken ones are carried by the hourly summary
instead, so nothing is forgotten and nothing repeats. That is the same split
``sla_policies.json`` makes with ``reminder_after_seconds`` and for the same reason.

**A transition is found over a period, never sampled at an instant (2026-08-14).** The first
version of this compared the two newest ``job_runs`` rows at the moment it happened to run, and in
seven weeks it never sent a single alert — while 16 APP-TELEGRAM failures, 15 APP-SQL_TASKS
failures and 4 APP-METRICS failures went out unreported. The arithmetic says why: this app is
scheduled once a minute, APP-TELEGRAM runs every ~4 seconds, and the two-row test only holds
between an app's *first* failure and its *next run of any kind*. On 2026-08-13 APP-TELEGRAM failed
at 09:41:09 and again at 09:41:31 and was healthy again by 09:41:52 — a 22-second window in which
the sample had to land, out of 60. Asking a once-a-minute observer for an instantaneous edge is
asking it to miss almost everything.

So the failure question is now asked over an interval instead: *which apps have a failed run newer
than the last failure alert we sent*. The queue row is still the only state (see
:data:`SOURCE_TYPE`) — the alert's own timestamp is the watermark. A standing failure still does
not repeat, because its streak began before that watermark; a failure that came and went is
reported once, with the fact that it has already recovered.

The CLI face is :mod:`db_ops.common.cli` ``ops-status``; this module holds everything it decides,
so a caller in Python, a scheduled app command and a shell operator all get the same answer.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Statuses ``job_runs`` writes for a run that did not work.
FAILED_STATUSES = {"error", "failed", "timeout"}

#: What the daemon writes into a long-running app's row when it is shut down (``daemon.py``
#: closes every running command with status ``timeout`` on SIGTERM). A deploy restarts the
#: container, so the web host — which runs once and stays up — collects one of these on every
#: deploy. Counting them made the first live summary report "APP-WEBHOST 14/15 failed" on a web
#: host that had never once failed, and would have alerted on every deploy: the app the operator
#: just restarted on purpose, reported as an incident. A shutdown is not a fault.
SHUTDOWN_MARKER = "was interrupted: the daemon stopped"

#: The other half of the same story, written by ``daemon.recover_stale_running_jobs``: on startup
#: the daemon closes every row still marked ``running``, because their processes died with the old
#: container. Same restart, opposite end of it — and just as much not a fault. It surfaced the
#: moment the failure alert began working (2026-08-14): the first replay reported APP-TELEGRAM and
#: APP-WEBHOST as having "started failing" at 05:43 and 05:58, which were the two deploys done that
#: morning. An alert that fires on every deploy is an alert people turn off.
STALE_RECOVERY_MARKER = "recovered from stale RUNNING state on daemon startup"

#: Messages that mean "the daemon restarted", not "the app failed". Matched on the message text
#: because that is what both the store and a person reading job_runs can see; the rows also carry
#: ``stale_recovery`` in their metadata JSON, which no supported backend can filter on portably.
NON_FAULT_MARKERS = (SHUTDOWN_MARKER, STALE_RECOVERY_MARKER)

#: How far past its own interval an app command may drift before it is called overdue. A scan
#: that takes longer than its interval, or a daemon that is busy, must not be reported as stopped:
#: the multiplier is what separates "late" from "not running at all".
DEFAULT_OVERDUE_FACTOR = 3.0

#: A floor under that grace, for the apps whose interval is seconds. APP-TELEGRAM repeats every
#: 1s; three seconds of lateness is not news, and without this every summary would call it
#: overdue during any slow poll.
MIN_OVERDUE_GRACE_SECONDS = 300


def run_failed(row: Any) -> bool:
    """Whether one ``job_runs`` row represents a failure the operator should hear about."""
    status = str((row or {}).get("status") or "").strip().lower()
    if status not in FAILED_STATUSES:
        return False
    message = str((row or {}).get("message") or "")
    return not any(marker in message for marker in NON_FAULT_MARKERS)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_app_commands(data_dir: Path) -> list[dict[str, Any]]:
    """The scheduled apps as configured, so the report can name one that never ran at all.

    Read from config rather than from the store's distinct job codes: an app command added last
    week and never once executed has no row in ``job_runs``, and it is precisely the one worth
    reporting.
    """
    path = Path(data_dir) / "app_commands.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    items = data.get("app_commands") if isinstance(data, dict) else data
    return [item for item in (items or []) if isinstance(item, dict)]


def _interval_seconds(command: dict[str, Any]) -> int:
    window = command.get("time_window") if isinstance(command.get("time_window"), dict) else {}
    try:
        return int(window.get("repeat_interval") or 0)
    except (TypeError, ValueError):
        return 0


def _window_hours(command: dict[str, Any]) -> tuple[int | None, int | None]:
    window = command.get("time_window") if isinstance(command.get("time_window"), dict) else {}
    def _hour(name: str) -> int | None:
        value = window.get(name)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return _hour("from_hour"), _hour("to_hour")


#: The SQL predicate for "this run failed", kept next to :func:`run_failed` so the database and
#: Python can never disagree about what a failure is — including the shutdown marker, which is a
#: deploy and not a fault.
_FAILED_SQL = (
    "lower(COALESCE(status, '')) IN (" + ", ".join(f"'{name}'" for name in sorted(FAILED_STATUSES)) + ") "
    + "".join(f"AND COALESCE(message, '') NOT LIKE '%{marker}%' " for marker in NON_FAULT_MARKERS)
).strip()


def _summarize_runs(conn: Any, since: str) -> dict[str, dict[str, Any]]:
    """Per app: how many runs, how many failed, and when it last failed and last worked.

    Aggregated in the database rather than by reading the rows. ``job_runs`` carries 28k rows in a
    24h window on this estate (22,776 of them APP-TELEGRAM's, one every four seconds), and this
    runs once a minute: the old version pulled every one of those rows over the wire to keep two.
    """
    rows = conn.execute(
        "SELECT job_code, COUNT(*) AS runs, "
        f"       SUM(CASE WHEN {_FAILED_SQL} THEN 1 ELSE 0 END) AS failed, "
        f"       MAX(CASE WHEN {_FAILED_SQL} THEN started_at END) AS last_failure, "
        f"       MAX(CASE WHEN {_FAILED_SQL} THEN NULL ELSE started_at END) AS last_success "
        "FROM job_runs WHERE started_at >= ? GROUP BY job_code",
        [since],
    )
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row["job_code"] or "")
        if code:
            out[code] = dict(row)
    return out


def _latest_run(conn: Any, code: str, since: str, *, failed_only: bool = False) -> dict[str, Any]:
    """The newest run of one app — optionally the newest *failed* one.

    The failed one is what an alert has to quote: an app that already recovered has a successful
    run as its latest, and reporting that row's (empty) error text would describe the failure as
    having no error at all.
    """
    condition = f" AND {_FAILED_SQL}" if failed_only else ""
    rows = list(conn.execute(
        "SELECT job_code, status, started_at, finished_at, duration_ms, message, error_text "
        f"FROM job_runs WHERE job_code = ? AND started_at >= ?{condition} "
        "ORDER BY started_at DESC LIMIT 1",
        [code, since],
    ))
    return dict(rows[0]) if rows else {}


def _streak_started_at(conn: Any, code: str, *, after: str, since: str) -> str:
    """When the current run of failures began — the first failure after the last success.

    This is what keeps a standing failure quiet. The alert compares the *start* of the streak with
    the watermark, so an app that has been broken since Tuesday has a streak older than every
    alert already sent and stays out of them, while a fresh outage has a streak that begins after
    the last one and is reported at once.
    """
    rows = list(conn.execute(
        f"SELECT MIN(started_at) AS streak_start FROM job_runs WHERE job_code = ? AND {_FAILED_SQL} "
        "AND started_at >= ? AND started_at > ?",
        [code, since, after or since],
    ))
    return str((rows[0]["streak_start"] if rows else "") or "")


def build_ops_status(
    *,
    store: Any,
    data_dir: Path,
    window_hours: int = 24,
    now: datetime | None = None,
    alerted_since: datetime | None = None,
    alert_lookback_seconds: int = 3600,
) -> dict[str, Any]:
    """One row per configured app command, with what the store says about how it has been going.

    ``alerted_since`` is when the last failure alert went out — the watermark a failure has to be
    newer than to be worth another message. The caller reads it from the queue
    (:func:`last_failure_alert_at`); with nothing to read, the lookback bounds how far back a first
    alert may reach, so a freshly deployed control app reports the last hour rather than the last
    day in one burst.
    """
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=max(1, int(window_hours)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    watermark = alerted_since or (now - timedelta(seconds=max(0, int(alert_lookback_seconds))))
    watermark_text = watermark.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    commands = [item for item in load_app_commands(data_dir) if bool(item.get("active", True))]
    apps: list[dict[str, Any]] = []
    with store.connect() as conn:
        totals = _summarize_runs(conn, since)
        for command in commands:
            code = str(command.get("app_command_id") or command.get("app_code") or "").strip()
            if not code:
                continue
            interval = _interval_seconds(command)
            totals_row = totals.get(code) or {}
            last = _latest_run(conn, code, since) if totals_row else {}
            last_started = _parse_time(last.get("started_at"))
            last_failure = str(totals_row.get("last_failure") or "")
            last_success = str(totals_row.get("last_success") or "")
            age_seconds = int((now - last_started).total_seconds()) if last_started else None
            # repeat_interval 0 means "run once and stay up" (the web host): it is not overdue, ever.
            overdue = False
            if interval > 0:
                grace = max(interval * DEFAULT_OVERDUE_FACTOR, MIN_OVERDUE_GRACE_SECONDS)
                overdue = age_seconds is None or age_seconds > grace
            from_hour, to_hour = _window_hours(command)
            failing = run_failed(last)
            # New failures are the ones this app has not reported yet. A standing failure is
            # excluded by its streak, not by its newest row, so an app that fails continuously is
            # reported once and then left to the summary.
            has_new_failure = bool(last_failure) and last_failure > watermark_text
            streak_start = ""
            if has_new_failure and failing:
                streak_start = _streak_started_at(conn, code, after=last_success, since=since)
            just_failed = has_new_failure and (not failing or (streak_start and streak_start > watermark_text))
            failed_run = _latest_run(conn, code, since, failed_only=True) if just_failed else {}
            apps.append({
                "app": code,
                "display_name": str(command.get("display_name") or command.get("app_name") or ""),
                "interval_seconds": interval,
                "hours": (from_hour, to_hour),
                "last_run": last.get("started_at") or "",
                "last_status": str(last.get("status") or "").lower() or "never run",
                "age_seconds": age_seconds,
                "overdue": overdue,
                "failing": failing,
                "just_failed": bool(just_failed),
                "last_failure": last_failure,
                "streak_started_at": streak_start,
                "runs": int(totals_row.get("runs") or 0),
                "failed": int(totals_row.get("failed") or 0),
                "last_error": str(last.get("error_text") or "").strip(),
                # What the alert quotes: the newest run that actually failed, which is not the
                # newest run once the app has recovered.
                "failure_error": str(failed_run.get("error_text") or "").strip(),
                "failure_status": str(failed_run.get("status") or "").lower(),
                "duration_ms": last.get("duration_ms"),
            })

    apps.sort(key=lambda item: (not item["failing"], not item["overdue"], item["app"]))
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_hours": int(window_hours),
        "alerted_since": watermark_text,
        "apps": apps,
        "failing": [item for item in apps if item["failing"]],
        "overdue": [item for item in apps if item["overdue"] and not item["failing"]],
        "just_failed": [item for item in apps if item["just_failed"]],
    }


def _age_text(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _first_error_line(text: str) -> str:
    """The line of a traceback an operator can act on.

    The stored ``error_text`` starts with the runner's own wrapper ("Command exited with return
    code 1") and then carries the child's whole stdout. The useful line is the last one that looks
    like an error, which is what the child printed before giving up.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.lower().startswith(("error:", "error ", "traceback")) or "Error" in line:
            return line[:180]
    return (lines[-1][:180] if lines else "")


def format_summary(status: dict[str, Any]) -> str:
    """The hourly message: a verdict line, then every app with its state.

    The header line decides the emoji — :mod:`db_ops.telegram.severity` tags the message centrally
    from it, so this must not carry one of its own.
    """
    apps = status.get("apps") or []
    failing, overdue = status.get("failing") or [], status.get("overdue") or []
    if failing:
        head = f"db_ops control: {len(failing)} app(s) FAILED"
    elif overdue:
        head = f"db_ops control: {len(overdue)} app(s) overdue"
    else:
        head = f"db_ops control: all {len(apps)} app(s) OK"

    lines = [head, f"window={status.get('window_hours')}h  at {status.get('generated_at')}", ""]
    for item in apps:
        marks = []
        if item["failing"]:
            marks.append("FAILED")
        if item["overdue"]:
            marks.append("OVERDUE")
        state = "/".join(marks) or "ok"
        line = (f"{item['app']}: {state}, last {_age_text(item['age_seconds'])}"
                f", {item['failed']}/{item['runs']} failed")
        if item["interval_seconds"]:
            line += f", every {item['interval_seconds']}s"
        lines.append(line)
        if item["failing"] and item["last_error"]:
            lines.append(f"    {_first_error_line(item['last_error'])}")
    # Length is the send layer's problem now: db_ops.telegram.api splits an over-long body across
    # messages instead of clipping it, so a clip here would throw away apps the reader needs to
    # see and nothing downstream could put them back.
    return "\n".join(lines)


def format_failure_alert(status: dict[str, Any]) -> str:
    """The immediate message, for apps that have failed since the last one went out.

    It names the failing **run**, not the app's newest run, and says whether the app is still down.
    A short failure that has already recovered is still reported — it is the thing the operator
    asked to hear about — but "recovered" has to be on the line, or a message about a 20-second
    blip reads as an outage in progress.
    """
    just = status.get("just_failed") or []
    head = f"db_ops control: {len(just)} app(s) started failing"
    lines = [head, f"at {status.get('generated_at')}", ""]
    for item in just:
        state = "still failing" if item.get("failing") else "recovered since"
        status_text = item.get("failure_status") or item.get("last_status")
        lines.append(f"{item['app']}: {status_text} at {item.get('last_failure') or item['last_run']}"
                     f", {state} ({item['failed']}/{item['runs']} failed in {status.get('window_hours')}h)")
        detail = item.get("failure_error") or item.get("last_error")
        if detail:
            lines.append(f"    {_first_error_line(detail)}")
    # See above: the send layer splits, so nothing is dropped here.
    return "\n".join(lines)


#: What the queued rows are stamped with, so the next run can find the last summary it sent
#: without a state table of its own. The queue row *is* the state: it is written in the same
#: transaction as the send, so a crash between "decide" and "send" cannot leave a summary
#: recorded that nobody received.
SOURCE_TYPE = "ops_status"
SUMMARY_NOTE = "control-summary"
#: The failure alert's own note. It is the watermark for the next alert, so it has to be one
#: constant both the sender and the reader use — a mismatch here would look exactly like the bug
#: this replaced: no alerts, ever, and nothing to show why.
FAILURE_NOTE = "control-failure"


def _last_queued_at(*, store: Any, chat_id: str, note: str) -> datetime | None:
    with store.connect() as conn:
        rows = list(conn.execute(
            "SELECT MAX(row_ins_date) AS last_sent FROM telegram_send_messages "
            "WHERE source_type = ? AND note = ? AND tlgchat_id = ?",
            [SOURCE_TYPE, note, str(chat_id)],
        ))
    return _parse_time(rows[0]["last_sent"]) if rows else None


def last_summary_sent_at(*, store: Any, chat_id: str) -> datetime | None:
    """When the last hourly summary was queued for this chat, or ``None`` if never."""
    return _last_queued_at(store=store, chat_id=chat_id, note=SUMMARY_NOTE)


def last_failure_alert_at(*, store: Any, chat_id: str) -> datetime | None:
    """When the last failure alert was queued for this chat — the watermark a new failure must beat."""
    return _last_queued_at(store=store, chat_id=chat_id, note=FAILURE_NOTE)


def summary_is_due(
    *,
    last_sent: datetime | None,
    now_local: datetime,
    from_hour: int = 8,
    to_hour: int = 20,
    interval_seconds: int = 3600,
) -> tuple[bool, str]:
    """Whether the hourly summary should go out now, and why not when it should not.

    Hours are **local**, because the window exists so that a person reads the message during their
    working day; a UTC comparison would move it by the timezone offset and put the 08:00 report at
    15:00. The app command itself runs around the clock — only this message is confined, so a
    failure at 03:00 still alerts immediately.

    Deliberately inclusive of ``to_hour``: 20 means "up to and including the 20:xx report", which
    is what an operator means by "to 20h" and what ``time_window`` does elsewhere in db_ops.
    """
    hour = now_local.hour
    if from_hour <= to_hour:
        inside = from_hour <= hour <= to_hour
    else:                       # a window that wraps midnight, e.g. 20 -> 6
        inside = hour >= from_hour or hour <= to_hour
    if not inside:
        return False, f"outside the summary window {from_hour:02d}-{to_hour:02d}h (local hour {hour:02d})"
    if last_sent is not None:
        elapsed = (now_local.astimezone(timezone.utc) - last_sent.astimezone(timezone.utc)).total_seconds()
        if elapsed < interval_seconds:
            return False, f"last summary {int(elapsed)}s ago, interval {interval_seconds}s"
    return True, ""
