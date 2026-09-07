"""Every `time_window` in every app means hours in the configured timezone — not the host's.

`from_hour: 1` has to mean the same 01:00 on the Windows master and inside the worker container.
Until 2026-09-07 it did not: each caller passed `datetime.now().astimezone()`, so a window meant
whatever clock the machine was set to, and the two nodes agreed only because `docker-compose.yml`
pinned `TZ: Asia/Ho_Chi_Minh` into the image. Anyone running the published image without that line
had every overnight window seven hours out and nothing said so.

`db_ops/lib/time_window.py` was never the bug — it takes `current` and compares it. The bug was in
the six callers, and a seventh added later reading the wrong clock would be just as silent. So this
pins one instant and one window and holds all of them to it together:

    2026-09-07 02:30 UTC   ==   09:30 at +07
    window: from_hour 9 -> to_hour 10

At `+07` every caller must say OPEN. At `UTC` every caller must say CLOSED. A caller that does not
flip is one still reading a clock nobody configured.

The freeze is applied to `db_ops.lib.timezone.datetime` alone, on purpose: that is the single point
every caller is supposed to go through, so a caller that bypasses it keeps reading the real wall
clock and fails here instead of passing by luck.
"""

import datetime as dt
from unittest.mock import patch

import pytest

from db_ops.lib import timezone as tz
from db_ops.lib.time_window import TimeWindow


#: 09:30 at +07, 02:30 at +00 — inside the window on one clock and outside it on the other.
MOMENT = dt.datetime(2026, 9, 7, 2, 30, 0, tzinfo=dt.timezone.utc)
WINDOW = TimeWindow(from_hour=9, to_hour=10, repeat_interval=300)
WINDOW_JSON = {"from_hour": 9, "to_hour": 10, "repeat_interval": 300}


class _FrozenDateTime(dt.datetime):
    @classmethod
    def now(cls, tz_=None):
        return MOMENT.astimezone(tz_) if tz_ else MOMENT.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _restore_zone():
    before = tz.display_declaration()
    yield
    tz.bind_display_timezone(before)


def _open_at(zone: str) -> dict[str, bool]:
    """Ask every `time_window` caller whether the window is open, with `zone` configured."""
    tz.bind_display_timezone(zone)
    with patch("db_ops.lib.timezone.datetime", _FrozenDateTime):
        from db_ops.backup_restore import schedule
        from db_ops.common import evidence, host_ops
        from db_ops.jobs import daemon
        from db_ops.metrics import collector
        from db_ops.reports import metrics_reports
        from db_ops.sql_tasks import runner

        command = daemon.AppCommand(
            app_command_id="probe", app_code="probe", app_name="probe", display_name="probe",
            log_scope="probe", working_dir=".", command_text="x", time_window=WINDOW, active=True)

        report = evidence.GateReport("probe")
        host_ops.check_maintenance_window(
            report, window={"from_hour": 9, "to_hour": 10}, ignore=False)
        maintenance = [g for g in report.gates if g.name == "schedule.maintenance_window"][0]

        skip_reason = metrics_reports._schedule_skip_reason(
            report_config={"active": True, "time_window": WINDOW_JSON},
            evaluated_at=MOMENT, last_sent_at="")

        return {
            "jobs.daemon": daemon.app_command_in_schedule_window(command),
            "sql_tasks.runner": runner.is_time_window_open(WINDOW, tz.display_now()),
            "backup_restore.schedule": schedule.is_due(
                job_code="probe", time_window=WINDOW, latest_runs={}, now=MOMENT),
            "metrics.collector": collector._metric_window_open(
                metric=type("M", (), {"schedule_window": WINDOW})(), now=MOMENT),
            "common.host_ops": maintenance.status == evidence.OK,
            "reports.schedule": skip_reason == "",
        }


def test_the_configured_zone_is_the_hour_a_window_means():
    """The claim, stated as one assertion per side.

    At +07 the frozen instant is 09:30 and the 09:00-10:00 window is open everywhere; at UTC it is
    02:30 and closed everywhere. Both directions matter: a caller hardcoded to open would pass the
    first half, and one hardcoded to closed would pass the second.
    """
    at_plus_seven = _open_at("+07:00")
    at_utc = _open_at("UTC")

    assert all(at_plus_seven.values()), (
        f"09:30 +07 is inside 09:00-10:00, but these said closed: "
        f"{sorted(k for k, v in at_plus_seven.items() if not v)}")
    assert not any(at_utc.values()), (
        f"02:30 UTC is outside 09:00-10:00, but these said open — they are reading a clock that is "
        f"not the configured one: {sorted(k for k, v in at_utc.items() if v)}")


def test_setting_the_zone_to_utc_really_moves_the_schedule_to_utc():
    """The operator's question, asked directly: if `timezone` is `UTC`, do windows run on +00?

    Answered by moving the window rather than the clock — at UTC the same instant is inside
    02:00-03:00 and outside 09:00-10:00, which is the opposite of what it is at +07.
    """
    tz.bind_display_timezone("UTC")
    with patch("db_ops.lib.timezone.datetime", _FrozenDateTime):
        now_utc = tz.display_now()
    assert now_utc.hour == 2 and now_utc.utcoffset() == dt.timedelta(0)

    from db_ops.lib.time_window import is_time_window_open

    assert is_time_window_open(TimeWindow(from_hour=2, to_hour=3), now_utc)
    assert not is_time_window_open(TimeWindow(from_hour=9, to_hour=10), now_utc)


def test_the_skip_reason_names_the_hour_on_the_configured_clock():
    """The message an operator reads when a report does not run has to name an hour they can
    recognise. "outside allowed hour window: 2" is only useful if 2 is the hour on their clock."""
    tz.bind_display_timezone("UTC")
    with patch("db_ops.lib.timezone.datetime", _FrozenDateTime):
        from db_ops.reports import metrics_reports
        reason = metrics_reports._schedule_skip_reason(
            report_config={"active": True, "time_window": WINDOW_JSON},
            evaluated_at=MOMENT, last_sent_at="")
    assert reason == "outside allowed hour window: 2"


def test_elapsed_time_is_not_affected_by_the_display_zone():
    """The other half of the contract, and the reason only the open-check moved.

    `repeat_interval`, retry and stale-running are subtractions between two instants. Those are
    offset-safe and are done in UTC against stored timestamps — a schedule must not fire an hour
    early because someone changed which clock the reports are printed on.
    """
    from db_ops.lib.time_window import repeat_due

    last_run = MOMENT - dt.timedelta(seconds=200)
    for zone in ("UTC", "+07:00", "America/New_York"):
        tz.bind_display_timezone(zone)
        assert repeat_due(last_run, 300, MOMENT) is False, zone
        assert repeat_due(last_run, 100, MOMENT) is True, zone
