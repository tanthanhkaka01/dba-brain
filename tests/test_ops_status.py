"""Watching the watcher.

On 2026-08-12 a NameError in the SQL task scanner made every scheduled scan exit 1, once a
minute, for a day. The daemon kept running, the container stayed up, the metric reports kept
arriving, and not one scheduled SQL task ran. Nothing in db_ops noticed, because everything in
db_ops watches databases and nothing watched db_ops. A person found it by noticing an absence,
which is the worst detector there is: absence is what nobody notices at 03:00.

What these tests pin is the judgement, not the plumbing:

* an app that stopped being **scheduled** writes no failure row at all, so "no errors" must never
  be read as healthy — overdue is a first-class verdict next to failed;
* the immediate alert fires on the **transition** into failure, so an app broken since Tuesday
  does not message the group every minute for three days (the standing failures ride the periodic
  summary instead);
* the summary's working-hours window is **local**, and it constrains only the summary — a failure
  at 03:00 is still news at 03:00.
"""

import json
from datetime import datetime, timedelta, timezone

from db_ops.db import ops_status as ops


class _Conn:
    """Answers the queries build_ops_status makes, over rows held in memory.

    A fake rather than a mock: it applies ``ops.run_failed`` itself, so "this run failed" means the
    same thing here as it does in Postgres, and the aggregation the database now does is reproduced
    rather than stubbed. The queries are told apart by shape — a grouped total, a newest-run lookup,
    a streak start — which is also a guard: a query this does not recognise raises instead of
    quietly returning the wrong rows.
    """

    def __init__(self, rows):
        self._rows = [dict(row) for row in rows]

    def execute(self, sql, params=None):
        params = list(params or [])
        text = " ".join(str(sql).split())
        if "GROUP BY job_code" in text:
            return self._totals(since=params[0])
        if "MIN(started_at) AS streak_start" in text:
            code, since, after = params
            failures = [row for row in self._for(code, since)
                        if ops.run_failed(row) and row["started_at"] > after]
            return [{"streak_start": min((row["started_at"] for row in failures), default=None)}]
        if "LIMIT 1" in text:
            rows = self._for(params[0], params[1])
            if "IN ('error'" in text:          # the newest *failed* run
                rows = [row for row in rows if ops.run_failed(row)]
            return sorted(rows, key=lambda row: row["started_at"], reverse=True)[:1]
        raise AssertionError(f"unexpected query: {text}")

    def _for(self, code, since):
        return [row for row in self._rows if row["job_code"] == code and row["started_at"] >= since]

    def _totals(self, since):
        out = {}
        for row in self._rows:
            if row["started_at"] < since:
                continue
            bucket = out.setdefault(row["job_code"], {
                "job_code": row["job_code"], "runs": 0, "failed": 0,
                "last_failure": None, "last_success": None,
            })
            bucket["runs"] += 1
            if ops.run_failed(row):
                bucket["failed"] += 1
                bucket["last_failure"] = max(bucket["last_failure"] or "", row["started_at"])
            else:
                bucket["last_success"] = max(bucket["last_success"] or "", row["started_at"])
        return list(out.values())

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Store:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _Conn(self._rows)


def _run(code, status, started_at, error_text=""):
    return {"job_code": code, "status": status, "started_at": started_at,
            "finished_at": started_at, "duration_ms": 10, "message": "", "error_text": error_text}


def _data_dir(tmp_path, commands):
    (tmp_path / "app_commands.json").write_text(
        json.dumps({"app_commands": commands}), encoding="utf-8")
    return tmp_path


NOW = datetime(2026, 8, 13, 6, 0, 0, tzinfo=timezone.utc)


def _stamp(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _command(code, repeat=60, active=True):
    return {"app_command_id": code, "display_name": code, "active": active,
            "time_window": {"repeat_interval": repeat}}


def test_an_app_that_simply_stopped_being_scheduled_is_reported_as_overdue(tmp_path):
    """The failure mode with no failure row. An app that is never started writes nothing at all,
    so a report built only from error rows shows a perfectly clean estate."""
    data_dir = _data_dir(tmp_path, [_command("APP-METRICS", repeat=120)])
    store = _Store([_run("APP-METRICS", "done", _stamp(minutes_ago=90))])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW)

    app = status["apps"][0]
    assert app["overdue"] is True
    assert app["failing"] is False
    assert [item["app"] for item in status["overdue"]] == ["APP-METRICS"]


def test_a_slow_but_working_app_is_not_called_overdue(tmp_path):
    """A run that takes longer than its own interval, or a busy daemon, is lateness — not a
    stopped scheduler. Without the grace factor every summary would cry wolf."""
    data_dir = _data_dir(tmp_path, [_command("APP-METRICS", repeat=120)])
    store = _Store([_run("APP-METRICS", "done", _stamp(minutes_ago=3))])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW)

    assert status["apps"][0]["overdue"] is False


def test_a_one_shot_app_is_never_overdue(tmp_path):
    """repeat_interval 0 means "start it and leave it up" — the web host. Measuring it against an
    interval it does not have would report the healthy case as broken forever."""
    data_dir = _data_dir(tmp_path, [_command("APP-WEBHOST", repeat=0)])
    store = _Store([_run("APP-WEBHOST", "running", _stamp(minutes_ago=600))])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW)

    assert status["apps"][0]["overdue"] is False


def test_the_alert_fires_when_an_app_goes_from_working_to_failing(tmp_path):
    data_dir = _data_dir(tmp_path, [_command("APP-SQL_TASKS")])
    store = _Store([
        _run("APP-SQL_TASKS", "error", _stamp(minutes_ago=1), error_text="ERROR: boom"),
        _run("APP-SQL_TASKS", "done", _stamp(minutes_ago=2)),
    ])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW)

    assert [item["app"] for item in status["just_failed"]] == ["APP-SQL_TASKS"]
    assert "started failing" in ops.format_failure_alert(status)
    assert "ERROR: boom" in ops.format_failure_alert(status)


def test_an_app_that_has_been_failing_all_along_does_not_alert_again(tmp_path):
    """1445 consecutive failures is one incident, not 1445 messages. The standing failure is
    carried by the periodic summary, which is where it stops being forgotten without becoming
    something people mute.

    What holds it back is the streak's *start*: the run of failures began before the last alert
    went out, so however many new failure rows it writes, it is the same incident already sent.
    """
    data_dir = _data_dir(tmp_path, [_command("APP-SQL_TASKS")])
    store = _Store([
        _run("APP-SQL_TASKS", "error", _stamp(minutes_ago=1)),
        _run("APP-SQL_TASKS", "error", _stamp(minutes_ago=2)),
        _run("APP-SQL_TASKS", "error", _stamp(minutes_ago=200)),
        _run("APP-SQL_TASKS", "done", _stamp(minutes_ago=201)),
    ])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW,
                                  alerted_since=NOW - timedelta(minutes=180))

    assert status["just_failed"] == []
    assert [item["app"] for item in status["failing"]] == ["APP-SQL_TASKS"]
    assert "FAILED" in ops.format_summary(status)


def test_an_app_that_recovered_and_broke_again_is_a_new_incident(tmp_path):
    """The other half of the streak rule: once an app has worked again, the next failure is not the
    incident that was already reported, and staying quiet about it would be the original bug."""
    data_dir = _data_dir(tmp_path, [_command("APP-SQL_TASKS")])
    store = _Store([
        _run("APP-SQL_TASKS", "error", _stamp(minutes_ago=1), error_text="ERROR: boom again"),
        _run("APP-SQL_TASKS", "done", _stamp(minutes_ago=100)),
        _run("APP-SQL_TASKS", "error", _stamp(minutes_ago=200)),
    ])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW,
                                  alerted_since=NOW - timedelta(minutes=180))

    assert [item["app"] for item in status["just_failed"]] == ["APP-SQL_TASKS"]
    assert "ERROR: boom again" in ops.format_failure_alert(status)


def test_a_failure_between_two_checks_is_still_reported_after_it_has_recovered(tmp_path):
    """The bug this rule replaced. APP-CONTROL runs once a minute; APP-TELEGRAM runs every four
    seconds. On 2026-08-13 APP-TELEGRAM failed at 09:41:09 and 09:41:31 and was healthy again at
    09:41:52 — so at every moment this app could have looked, the newest run was a success and the
    old two-row test saw nothing. Sixteen failures in a day went unreported that way, and not one
    alert was sent in seven weeks. A failure that has already healed is still news; the message
    says it recovered."""
    data_dir = _data_dir(tmp_path, [_command("APP-TELEGRAM", repeat=1)])
    store = _Store([
        _run("APP-TELEGRAM", "done", _stamp(minutes_ago=1)),
        _run("APP-TELEGRAM", "done", _stamp(minutes_ago=2)),
        _run("APP-TELEGRAM", "error", _stamp(minutes_ago=3), error_text="ERROR: send failed"),
        _run("APP-TELEGRAM", "error", _stamp(minutes_ago=4), error_text="ERROR: send failed"),
        _run("APP-TELEGRAM", "done", _stamp(minutes_ago=5)),
    ])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW,
                                  alerted_since=NOW - timedelta(minutes=30))

    app = status["apps"][0]
    assert app["failing"] is False          # its newest run worked
    assert [item["app"] for item in status["just_failed"]] == ["APP-TELEGRAM"]
    alert = ops.format_failure_alert(status)
    assert "recovered since" in alert
    assert "ERROR: send failed" in alert


def test_the_same_short_failure_is_not_reported_twice(tmp_path):
    """Reporting a healed failure must not turn into reporting it once a minute forever: the alert
    that went out is itself the watermark, so the next pass sees nothing newer."""
    data_dir = _data_dir(tmp_path, [_command("APP-TELEGRAM", repeat=1)])
    store = _Store([
        _run("APP-TELEGRAM", "done", _stamp(minutes_ago=1)),
        _run("APP-TELEGRAM", "error", _stamp(minutes_ago=3)),
        _run("APP-TELEGRAM", "done", _stamp(minutes_ago=5)),
    ])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW,
                                  alerted_since=NOW - timedelta(minutes=2))

    assert status["just_failed"] == []


def test_a_first_run_with_no_alert_history_only_looks_back_one_hour(tmp_path):
    """A control app that has never alerted must not open with a day of history in one message —
    and must not stay silent either. The lookback bounds the first alert; everything older is the
    summary's job."""
    data_dir = _data_dir(tmp_path, [_command("APP-SQL_TASKS")])
    store = _Store([
        _run("APP-SQL_TASKS", "done", _stamp(minutes_ago=1)),
        _run("APP-SQL_TASKS", "error", _stamp(minutes_ago=300)),
        _run("APP-SQL_TASKS", "done", _stamp(minutes_ago=301)),
    ])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW, alerted_since=None)

    assert status["just_failed"] == []
    assert status["alerted_since"] == (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_an_inactive_app_command_is_not_reported_at_all(tmp_path):
    """Switched off on purpose is not a gap; reporting it would teach the reader to skim."""
    data_dir = _data_dir(tmp_path, [_command("APP-OLD", active=False)])

    status = ops.build_ops_status(store=_Store([]), data_dir=data_dir, now=NOW)

    assert status["apps"] == []


def test_the_summary_waits_for_working_hours_but_the_alert_never_does():
    local = timezone(timedelta(hours=7))
    at_three_am = datetime(2026, 8, 13, 3, 0, tzinfo=local)
    at_nine_am = datetime(2026, 8, 13, 9, 0, tzinfo=local)

    due, reason = ops.summary_is_due(last_sent=None, now_local=at_three_am)
    assert due is False and "outside the summary window" in reason

    due, _ = ops.summary_is_due(last_sent=None, now_local=at_nine_am)
    assert due is True


def test_the_summary_is_not_repeated_inside_its_interval():
    local = timezone(timedelta(hours=7))
    now_local = datetime(2026, 8, 13, 9, 30, tzinfo=local)
    sent_20_minutes_ago = (now_local - timedelta(minutes=20)).astimezone(timezone.utc)

    due, reason = ops.summary_is_due(last_sent=sent_20_minutes_ago, now_local=now_local,
                                     interval_seconds=3600)

    assert due is False and "interval 3600s" in reason


def test_the_last_hour_of_the_window_still_gets_its_report():
    """"to 20h" means the 20:xx report is sent, which is what an operator means by it and what
    time_window does everywhere else in db_ops."""
    local = timezone(timedelta(hours=7))
    due, _ = ops.summary_is_due(last_sent=None,
                                now_local=datetime(2026, 8, 13, 20, 45, tzinfo=local))

    assert due is True


def test_a_deploy_restart_is_not_reported_as_an_app_failure(tmp_path):
    """The daemon closes every long-running app with status `timeout` when it shuts down, so a
    deploy leaves one on the web host every time. Counting those made the first live summary say
    "APP-WEBHOST 14/15 failed" about a web host that had never failed — and would have alerted on
    every deploy, reporting the restart the operator had just performed as an incident."""
    shutdown = ("App command APP-WEBHOST was interrupted: the daemon stopped (signal_15). "
                "It resumes on the next scan.")
    data_dir = _data_dir(tmp_path, [_command("APP-WEBHOST", repeat=0)])
    store = _Store([
        {**_run("APP-WEBHOST", "timeout", _stamp(minutes_ago=1)), "message": shutdown},
        {**_run("APP-WEBHOST", "timeout", _stamp(minutes_ago=30)), "message": shutdown},
    ])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW)

    app = status["apps"][0]
    assert app["failing"] is False
    assert app["failed"] == 0
    assert status["just_failed"] == []


def test_the_startup_side_of_a_restart_is_not_reported_as_an_app_failure(tmp_path):
    """The other end of the same deploy. On startup the daemon closes every row still marked
    running, because those processes died with the old container — `recover_stale_running_jobs`.
    Both ends had to be excused: the first replay of the fixed alert reported APP-TELEGRAM and
    APP-WEBHOST as having started failing at 05:43 and 05:58 on 2026-08-14, which were that
    morning's two deploys. An alert that fires on every deploy is an alert people turn off."""
    stale = ("App command APP-TELEGRAM recovered from stale RUNNING state on daemon startup "
             "after 546s (timeout=300s).")
    data_dir = _data_dir(tmp_path, [_command("APP-TELEGRAM", repeat=1)])
    store = _Store([
        _run("APP-TELEGRAM", "done", _stamp(minutes_ago=1)),
        {**_run("APP-TELEGRAM", "timeout", _stamp(minutes_ago=2)), "message": stale},
    ])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW,
                                  alerted_since=NOW - timedelta(minutes=30))

    assert status["just_failed"] == []
    assert status["apps"][0]["failed"] == 0


def test_a_real_timeout_is_still_a_failure(tmp_path):
    """Only the daemon's own shutdown row is excused. An app command that genuinely ran past its
    timeout is exactly what this report exists to surface."""
    data_dir = _data_dir(tmp_path, [_command("APP-METRICS", repeat=120)])
    store = _Store([
        {**_run("APP-METRICS", "timeout", _stamp(minutes_ago=1)),
         "message": "App command APP-METRICS exceeded its timeout of 2400s."},
        _run("APP-METRICS", "done", _stamp(minutes_ago=3)),
    ])

    status = ops.build_ops_status(store=store, data_dir=data_dir, now=NOW)

    assert status["apps"][0]["failing"] is True
    assert [item["app"] for item in status["just_failed"]] == ["APP-METRICS"]
