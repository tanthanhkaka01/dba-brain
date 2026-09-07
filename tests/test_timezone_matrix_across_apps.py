"""The acceptance suite for one configured clock: set `+00`, set `+07`, and check everything moves.

`config.json` → `timezone` is a single declaration that four scheduling apps and every rendered
timestamp read. That makes it powerful and makes it dangerous: one wrong caller means one app runs
on a clock nobody chose, and it is invisible until an overnight job runs at lunchtime. So this file
does not test the timezone module — `tests/test_display_timezone.py` does that — it tests the
*estate*: for each app, at both zones, does the thing actually change.

Every case is pinned to one instant so the two zones are the only variable:

    2026-09-07 02:30:00 UTC   ==   2026-09-07 09:30:00 +07

That instant is chosen for what it splits. At `+07` it is 09:30 on the 7th; at `+00` it is 02:30 on
the same day, so hour-bounded windows flip. Two further instants below cross a *day* boundary and a
*month* boundary, because a zone shift moves the calendar as well as the clock, and a `from_day`
bound that only ever gets tested at midday would never show it.

The clock is frozen at `db_ops.lib.timezone.datetime` only. That is the single point every caller
is supposed to go through, so a caller that reaches around it keeps reading real wall-clock time
and fails here rather than passing by luck. That is the property this file exists to hold.
"""

import datetime as dt
from unittest.mock import patch

import pytest

from db_ops.lib import timezone as tz
from db_ops.lib.time_window import RUN_ONCE, TimeWindow, job_due, repeat_due


# --------------------------------------------------------------------------------------------- #
# The instants, and the two zones under test
# --------------------------------------------------------------------------------------------- #

#: 09:30 on the 7th at +07; 02:30 on the 7th at +00. Splits an hour window.
MIDMORNING = dt.datetime(2026, 9, 7, 2, 30, 0, tzinfo=dt.timezone.utc)

#: 00:30 on the 8th at +07; 17:30 on the 7th at +00. Splits the *day*, and the hour.
ACROSS_MIDNIGHT = dt.datetime(2026, 9, 7, 17, 30, 0, tzinfo=dt.timezone.utc)

#: 06:30 on 1 October at +07; 23:30 on 30 September at +00. Splits the month.
ACROSS_MONTH_END = dt.datetime(2026, 9, 30, 23, 30, 0, tzinfo=dt.timezone.utc)

UTC = "UTC"
PLUS7 = "+07:00"

#: A window an operator would actually write: "during the morning".
MORNING = TimeWindow(from_hour=9, to_hour=10, repeat_interval=300)


def _frozen(moment: dt.datetime):
    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz_=None):
            return moment.astimezone(tz_) if tz_ else moment.replace(tzinfo=None)
    return Frozen


@pytest.fixture(autouse=True)
def _restore_zone():
    before = tz.display_declaration()
    yield
    tz.bind_display_timezone(before)


def at(zone: str, moment: dt.datetime = MIDMORNING):
    """Bind `zone` and freeze the display clock at `moment`. Use as a context manager."""
    tz.bind_display_timezone(zone)
    return patch("db_ops.lib.timezone.datetime", _frozen(moment))


# --------------------------------------------------------------------------------------------- #
# 1. The premise — the two zones really are two different wall clocks at one instant
# --------------------------------------------------------------------------------------------- #

def test_one_instant_reads_as_two_different_clocks():
    """If this fails nothing below means anything."""
    with at(PLUS7):
        assert tz.display_now().strftime("%Y-%m-%d %H:%M") == "2026-09-07 09:30"
    with at(UTC):
        assert tz.display_now().strftime("%Y-%m-%d %H:%M") == "2026-09-07 02:30"


def test_a_zone_shift_moves_the_calendar_day_not_only_the_hour():
    """`from_day`/`from_month` bounds are as exposed to the zone as `from_hour` is, and a suite
    that only ever tests at midday would never notice."""
    with at(PLUS7, ACROSS_MIDNIGHT):
        assert tz.display_now().strftime("%Y-%m-%d %H:%M") == "2026-09-08 00:30"
    with at(UTC, ACROSS_MIDNIGHT):
        assert tz.display_now().strftime("%Y-%m-%d %H:%M") == "2026-09-07 17:30"

    with at(PLUS7, ACROSS_MONTH_END):
        assert tz.display_now().strftime("%Y-%m-%d %H:%M") == "2026-10-01 06:30"
    with at(UTC, ACROSS_MONTH_END):
        assert tz.display_now().strftime("%Y-%m-%d %H:%M") == "2026-09-30 23:30"


# --------------------------------------------------------------------------------------------- #
# 2. App commands (jobs/daemon)
# --------------------------------------------------------------------------------------------- #

def _app_command(window: TimeWindow):
    from db_ops.jobs.daemon import AppCommand
    return AppCommand(
        app_command_id="APP-PROBE", app_code="probe", app_name="probe", display_name="Probe",
        log_scope="probe", working_dir=".", command_text="x", time_window=window, active=True)


@pytest.mark.parametrize("zone,expected", [(PLUS7, True), (UTC, False)])
def test_an_app_commands_window_follows_the_configured_zone(zone, expected):
    from db_ops.jobs import daemon
    with at(zone):
        assert daemon.app_command_in_schedule_window(_app_command(MORNING)) is expected


@pytest.mark.parametrize("zone,expected", [(PLUS7, True), (UTC, False)])
def test_an_app_commands_day_bound_follows_the_configured_zone(zone, expected):
    """`from_day: 8` at the instant that is the 8th in one zone and the 7th in the other."""
    from db_ops.jobs import daemon
    window = TimeWindow(from_day=8, to_day=8, repeat_interval=300)
    with at(zone, ACROSS_MIDNIGHT):
        assert daemon.app_command_in_schedule_window(_app_command(window)) is expected


@pytest.mark.parametrize("zone,expected", [(PLUS7, True), (UTC, False)])
def test_an_app_commands_month_bound_follows_the_configured_zone(zone, expected):
    from db_ops.jobs import daemon
    window = TimeWindow(from_month=10, to_month=10, repeat_interval=300)
    with at(zone, ACROSS_MONTH_END):
        assert daemon.app_command_in_schedule_window(_app_command(window)) is expected


@pytest.mark.parametrize("zone", [UTC, PLUS7])
def test_a_wrapping_overnight_window_is_open_in_both_zones_at_its_own_small_hours(zone):
    """`from_hour: 22, to_hour: 6` is where a zone mistake hides best — it is open for eight hours
    of the day, so the wrong clock still looks plausible. Asserted from the zone's own side: at
    each zone, pick the instant that is 02:00 *there*, and the window must be open."""
    from db_ops.jobs import daemon
    window = TimeWindow(from_hour=22, to_hour=6, repeat_interval=300)
    local_2am = {UTC: dt.datetime(2026, 9, 7, 2, 0, tzinfo=dt.timezone.utc),
                 PLUS7: dt.datetime(2026, 9, 6, 19, 0, tzinfo=dt.timezone.utc)}[zone]
    with at(zone, local_2am):
        assert tz.display_now().hour == 2
        assert daemon.app_command_in_schedule_window(_app_command(window)) is True


@pytest.mark.parametrize("zone", [UTC, PLUS7])
def test_an_app_commands_repeat_interval_is_unaffected_by_the_zone(zone):
    """The other half of the contract. Elapsed time is UTC arithmetic against a stored timestamp,
    so a schedule must not fire early because the reports moved to another clock."""
    from db_ops.jobs import daemon
    command = _app_command(TimeWindow(from_hour=0, to_hour=23, repeat_interval=300))
    # `started_at` is the column both readers look at first (daemon.row_time,
    # backup_restore.run_time); `finished_at` is present because row_time reads all three.
    latest = {"started_at": "2026-09-07T02:26:00Z", "created_at": "2026-09-07T02:26:00Z",
              "finished_at": "2026-09-07T02:26:30Z", "status": "done"}
    with at(zone):
        # 4 minutes elapsed of a 300s interval -> not due, on either clock.
        assert daemon.app_command_is_due(command, latest, now=MIDMORNING) is False


# --------------------------------------------------------------------------------------------- #
# 3. Metrics
# --------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("zone,expected", [(PLUS7, True), (UTC, False)])
def test_a_metrics_schedule_window_follows_the_configured_zone(zone, expected):
    """The 22 metrics declared `from_hour: 1, to_hour: 6` are the heavy overnight scans. On the
    wrong clock they run during the working day against production."""
    from db_ops.metrics import collector
    metric = type("M", (), {"schedule_window": MORNING})()
    with at(zone):
        assert collector._metric_window_open(metric=metric, now=MIDMORNING) is expected


@pytest.mark.parametrize("zone", [UTC, PLUS7])
def test_a_metric_with_no_window_is_always_open_in_either_zone(zone):
    """"Not configured" is a state, not a failure: a real-time metric has no window and must not
    become zone-sensitive by accident."""
    from db_ops.metrics import collector
    metric = type("M", (), {"schedule_window": None})()
    with at(zone):
        assert collector._metric_window_open(metric=metric, now=MIDMORNING) is True


@pytest.mark.parametrize("zone", [UTC, PLUS7])
def test_a_metrics_collection_interval_is_unaffected_by_the_zone(zone):
    with at(zone):
        last = MIDMORNING - dt.timedelta(seconds=200)
        assert repeat_due(last, 300, MIDMORNING) is False
        assert repeat_due(last, 100, MIDMORNING) is True


# --------------------------------------------------------------------------------------------- #
# 4. Backup / restore
# --------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("zone,expected", [(PLUS7, True), (UTC, False)])
def test_a_backup_jobs_window_follows_the_configured_zone(zone, expected):
    """`data/restore_config.json` has 17 hour-bounded jobs, most of them `1..5` — the maintenance
    window. Running those at the wrong hour is a restore drill against a busy instance."""
    from db_ops.backup_restore import schedule
    with at(zone):
        assert schedule.is_due(job_code="probe", time_window=MORNING,
                               latest_runs={}, now=MIDMORNING) is expected


@pytest.mark.parametrize("zone", [UTC, PLUS7])
def test_a_backup_job_inside_its_window_still_respects_its_interval(zone):
    """Window open is necessary, not sufficient — and the interval half must stay zone-blind."""
    from db_ops.backup_restore import schedule
    always_open = TimeWindow(from_hour=0, to_hour=23, repeat_interval=3600)
    latest = {"probe": {"status": "done", "started_at": "2026-09-07T02:00:00Z",
                        "created_at": "2026-09-07T02:00:00Z"}}
    with at(zone):
        # 30 minutes into a 1-hour interval, window wide open -> not due on either clock.
        assert schedule.is_due(job_code="probe", time_window=always_open,
                               latest_runs=latest, now=MIDMORNING) is False


# --------------------------------------------------------------------------------------------- #
# 5. SQL tasks — through the real scan, not the window helper
# --------------------------------------------------------------------------------------------- #

def _sql_pair(window: TimeWindow):
    from db_ops.sql_tasks.runner import SqlCommand, SqlTarget
    command = SqlCommand(
        sql_id=1, sql_code="PROBE", sql_name="Probe", db_type="sqlserver", script_type="sql",
        script_path=None, script_paths=(), script_files=(), active=True)
    target = SqlTarget(
        sql_id=1, target_no=1, server_id="SRV-1", db_type="sqlserver", service_name="svc",
        instance_name="inst", credential_name="cred", time_window=window, active=True)
    return {1: command}, [target]


@pytest.mark.parametrize("zone,expected_count", [(PLUS7, 1), (UTC, 0)])
def test_the_sql_task_scan_follows_the_configured_zone(zone, expected_count):
    """`due_sql_tasks` is the real entry point the runner calls, and it reads the clock itself
    rather than being handed one — so this covers the wiring, not just the comparison."""
    from db_ops.sql_tasks import runner
    commands, targets = _sql_pair(MORNING)
    with at(zone):
        due = runner.due_sql_tasks(commands=commands, targets=targets, latest_runs={})
    assert len(due) == expected_count


@pytest.mark.parametrize("zone", [UTC, PLUS7])
def test_a_manual_sql_target_is_never_scheduled_in_either_zone(zone):
    """`repeat_interval: -1` means a human asks. No zone makes it due."""
    from db_ops.sql_tasks import runner
    commands, targets = _sql_pair(TimeWindow(from_hour=0, to_hour=23, repeat_interval=-1))
    with at(zone):
        assert runner.due_sql_tasks(commands=commands, targets=targets, latest_runs={}) == []


# --------------------------------------------------------------------------------------------- #
# 6. Reports — the window, and the message the operator reads when it is closed
# --------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("zone,expected", [(PLUS7, ""), (UTC, "outside allowed hour window: 2")])
def test_a_scheduled_reports_window_and_its_skip_reason_follow_the_zone(zone, expected):
    """The reason has to name an hour the operator recognises. "hour window: 2" is only useful if
    2 is the hour on *their* clock — it used to be the hour on the reports app's hardcoded +07."""
    from db_ops.reports import metrics_reports
    with at(zone):
        reason = metrics_reports._schedule_skip_reason(
            report_config={"active": True,
                           "time_window": {"from_hour": 9, "to_hour": 10, "repeat_interval": 300}},
            evaluated_at=MIDMORNING, last_sent_at="")
    assert reason == expected


# --------------------------------------------------------------------------------------------- #
# 7. Maintenance windows (common/host_ops) — the gate in front of a restart or a patch
# --------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("zone,expected", [(PLUS7, True), (UTC, False)])
def test_a_maintenance_window_gate_follows_the_configured_zone(zone, expected):
    """This one gates a host restart. Getting the clock wrong here reboots a machine outside the
    window somebody agreed with the business."""
    from db_ops.common import evidence, host_ops
    with at(zone):
        report = evidence.GateReport("probe")
        host_ops.check_maintenance_window(
            report, window={"from_hour": 9, "to_hour": 10}, ignore=False)
    gate = [g for g in report.gates if g.name == "schedule.maintenance_window"][0]
    assert (gate.status == evidence.OK) is expected


# --------------------------------------------------------------------------------------------- #
# 8. Day boundaries — "today" ends at the operator's midnight
# --------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("zone,expected_day", [(PLUS7, "2026-09-08"), (UTC, "2026-09-07")])
def test_todays_date_follows_the_configured_zone(zone, expected_day):
    with at(zone, ACROSS_MIDNIGHT):
        assert tz.display_today().isoformat() == expected_day


@pytest.mark.parametrize("zone,expected_stamp", [(PLUS7, "20260908_003000"),
                                                 (UTC, "20260907_173000")])
def test_a_generated_reports_filename_stamp_follows_the_configured_zone(zone, expected_stamp):
    """The stamp is also the day a report is archived under, so it decides when "today" ends for
    the archive as well as what the file is called."""
    with at(zone, ACROSS_MIDNIGHT):
        assert tz.file_stamp() == expected_stamp


@pytest.mark.parametrize("zone,expected", [(PLUS7, "2026-09-08 00:30:00 +07"),
                                           (UTC, "2026-09-07 17:30:00 +00")])
def test_a_report_header_and_its_filename_stamp_agree(zone, expected):
    """They did not, before: the stamp came off the host clock and the row describing it was UTC."""
    with at(zone, ACROSS_MIDNIGHT):
        stamp, header = tz.file_stamp(), tz.format_display()
    assert header == expected
    assert tz.label_from_file_stamp(stamp, zone) == expected


def test_the_once_a_day_report_guard_uses_the_operators_midnight(tmp_path):
    """A report generated at 07:30 local is still *yesterday* in UTC, so a UTC-day guard lets the
    same daily report out twice. The offset is passed in from the configured zone."""
    from db_ops.db import DbOpsStore

    store = DbOpsStore(tmp_path / "db_ops.sqlite")
    store.initialize()
    report_id = store.insert_report(
        report_code="rp_daily", report_name="Daily", report_type="BACKUP_HEALTH",
        report_level="logging", report_text="body")
    with store.connect() as conn:
        # 17:30Z on the 7th = 00:30 on the 8th at +07.
        conn.execute("UPDATE reports SET created_at = ? WHERE report_id = ?",
                     ("2026-09-07T17:30:00Z", report_id))

    def exists(day, zone):
        return store.report_exists_on_local_date(
            report_code="rp_daily", local_date=day,
            utc_offset_minutes=tz.offset_minutes(zone, at=ACROSS_MIDNIGHT))

    assert exists("2026-09-08", PLUS7) and not exists("2026-09-07", PLUS7)
    assert exists("2026-09-07", UTC) and not exists("2026-09-08", UTC)


@pytest.mark.parametrize("zone,expected_archive", [(PLUS7, "app_20260907.log"),
                                                  (UTC, "app_20260906.log")])
def test_the_log_rotation_boundary_follows_the_configured_zone(zone, expected_archive, tmp_path):
    """A log file named `_20260907` has to hold the day its lines claim to be from — the line
    prefix and the rotation boundary read the same clock, or the archive is off by one.

    Asserted through `archive_yesterday_if_missing`, which is the function that actually names the
    file. Checking the imported `display_today` symbol instead passed while the code underneath
    was reverted to the host clock — measured, which is why it is written this way.
    """
    from db_ops.logging_ops.handlers import archive_yesterday_if_missing

    log = tmp_path / "app.log"
    log.write_text("a line", encoding="utf-8")
    with at(zone, ACROSS_MIDNIGHT):
        archived = archive_yesterday_if_missing(log)

    assert archived is not None and archived.name == expected_archive


@pytest.mark.parametrize("zone,expected", [(PLUS7, "2026-09-07T16:59:59Z"),
                                           (UTC, "2026-09-07T23:59:59Z")])
def test_a_backfilled_day_ends_at_the_operators_midnight(zone, expected):
    """`?date=2026-09-07` means the operator's 7th. On a UTC day it swept in the first seven hours
    of their 8th and dropped their evening."""
    from db_ops.reports.backfill import _end_of_day
    with at(zone):
        assert _end_of_day("2026-09-07") == expected


# --------------------------------------------------------------------------------------------- #
# 9. The invariant — none of this may reach a stored timestamp
# --------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("zone", [UTC, PLUS7])
def test_a_stored_timestamp_is_utc_whatever_the_display_zone_is(zone, tmp_path):
    """Every range query in the tool compares stored text lexically. A row written at +07 would
    sort between two UTC rows and land in the wrong window, seven hours wide."""
    from db_ops.db import DbOpsStore

    tz.bind_display_timezone(zone)
    store = DbOpsStore(tmp_path / f"store_{zone.replace(':', '')}.sqlite")
    store.initialize()
    report_id = store.insert_report(
        report_code="rp", report_name="R", report_type="BACKUP_HEALTH",
        report_level="logging", report_text="body")
    with store.connect() as conn:
        created = conn.execute("SELECT created_at FROM reports WHERE report_id = ?",
                               (report_id,)).fetchone()["created_at"]

    assert created.endswith("Z"), created
    written = dt.datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    assert abs((written - dt.datetime.now(dt.timezone.utc)).total_seconds()) < 120, (
        f"{created} is not this moment in UTC — a display clock reached a stored column")


@pytest.mark.parametrize("zone", [UTC, PLUS7])
def test_the_run_once_and_manual_conventions_are_zone_blind(zone):
    """`repeat_interval` 0 and -1 are about *whether* a thing repeats, not *when*. A zone change
    must not turn a run-once into a repeater."""
    with at(zone):
        last = MIDMORNING - dt.timedelta(days=3)
        assert job_due(last_run=last, last_status="done", repeat_interval=RUN_ONCE,
                       retry_interval=60, now=MIDMORNING) is False
        assert job_due(last_run=None, last_status=None, repeat_interval=-1,
                       retry_interval=60, now=MIDMORNING) is False


# --------------------------------------------------------------------------------------------- #
# 9b. A time that stands on its own must name its clock
# --------------------------------------------------------------------------------------------- #

@pytest.mark.parametrize("zone,offset", [(UTC, "+00"), (PLUS7, "+07")])
def test_the_run_line_of_a_report_carries_its_offset(zone, offset):
    """`Run: 2026-09-07 14:44:14` was shipped in 0.10.0 and is the bug this pins.

    It reached Telegram with no zone on it — the exact unlabelled wall clock the release existed to
    remove — because it borrowed the *column* renderer, which may omit the offset only because a
    table header states it once. A line that stands on its own has no header to lean on.
    """
    from db_ops.reports import metrics_reports

    tz.bind_display_timezone(zone)
    text = metrics_reports._run_time_text(
        {"started_at": "2026-09-07T02:30:00Z", "finished_at": "2026-09-07T02:30:00Z"})

    assert text.endswith(offset), f"{text!r} does not name its clock"


def test_a_window_refusal_says_what_time_it_thinks_it_is():
    """The message explaining why a host restart was blocked. If it names an hour without a zone,
    the reader cannot tell whether the tool disagrees with them about the time or about the rule."""
    from db_ops.common import evidence, host_ops

    with at(UTC):
        report = evidence.GateReport("probe")
        host_ops.check_maintenance_window(
            report, window={"from_hour": 9, "to_hour": 10}, ignore=False)
    detail = [g for g in report.gates if g.name == "schedule.maintenance_window"][0].detail

    assert "+00" in detail, f"{detail!r} names an hour but not a clock"


def test_no_producer_renders_a_bare_wall_clock():
    """The guard for the whole class, because two of these were missed by hand.

    A `strftime("%Y-%m-%d %H:%M…")` with no offset beside it is how the bug looks in source. The
    two allowed exceptions are named, each with the reason it is one — anything new has to justify
    itself here rather than reach a reader unlabelled.
    """
    import re
    from pathlib import Path

    ALLOWED = {
        # A table cell: the column header carries the offset once for every row.
        "db_ops/reports/metrics_reports.py",
        # A comparison key compared against server-local file mtimes as text, not a rendered time.
        "db_ops/lib/backupfiles_retention.py",
    }
    pattern = re.compile(r'strftime\("%Y-%m-%d %H:%M')
    offenders = []
    for path in Path("db_ops").rglob("*.py"):
        rel = path.as_posix()
        if rel in ALLOWED or rel.endswith("lib/timezone.py"):
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)

    assert not offenders, (
        f"these render a wall clock with no offset: {offenders}. Use "
        f"db_ops.lib.timezone.format_display / format_display_text, or add the file to ALLOWED "
        f"with the reason it is an exception.")


# --------------------------------------------------------------------------------------------- #
# 10. The whole matrix in one assertion, so a NEW caller is caught
# --------------------------------------------------------------------------------------------- #

def _every_scheduler_says_open() -> dict[str, bool]:
    """Ask every app whose schedule reads a wall clock, at whatever zone/instant is bound."""
    from db_ops.backup_restore import schedule
    from db_ops.common import evidence, host_ops
    from db_ops.jobs import daemon
    from db_ops.metrics import collector
    from db_ops.reports import metrics_reports
    from db_ops.sql_tasks import runner

    report = evidence.GateReport("probe")
    host_ops.check_maintenance_window(report, window={"from_hour": 9, "to_hour": 10}, ignore=False)
    gate = [g for g in report.gates if g.name == "schedule.maintenance_window"][0]
    commands, targets = _sql_pair(MORNING)

    return {
        "app command (jobs.daemon)": daemon.app_command_in_schedule_window(_app_command(MORNING)),
        "sql task (sql_tasks.runner)": bool(
            runner.due_sql_tasks(commands=commands, targets=targets, latest_runs={})),
        "backup/restore (backup_restore.schedule)": schedule.is_due(
            job_code="probe", time_window=MORNING, latest_runs={}, now=MIDMORNING),
        "metric (metrics.collector)": collector._metric_window_open(
            metric=type("M", (), {"schedule_window": MORNING})(), now=MIDMORNING),
        "maintenance gate (common.host_ops)": gate.status == evidence.OK,
        "scheduled report (reports)": metrics_reports._schedule_skip_reason(
            report_config={"active": True,
                           "time_window": {"from_hour": 9, "to_hour": 10, "repeat_interval": 300}},
            evaluated_at=MIDMORNING, last_sent_at="") == "",
    }


def test_every_scheduler_in_the_estate_flips_together():
    """The catch-all, and the reason this file is worth its length.

    One window, one instant, every app at once. At +07 all must be open; at UTC all must be closed.
    A seventh caller added later that reads the host clock does not flip, and the failure names it.
    """
    with at(PLUS7):
        open_at_plus7 = _every_scheduler_says_open()
    with at(UTC):
        open_at_utc = _every_scheduler_says_open()

    stuck_closed = sorted(k for k, v in open_at_plus7.items() if not v)
    stuck_open = sorted(k for k, v in open_at_utc.items() if v)

    assert not stuck_closed, (
        f"09:30 +07 is inside 09:00-10:00, but these said closed: {stuck_closed}")
    assert not stuck_open, (
        f"02:30 UTC is outside 09:00-10:00, but these said open — they are reading a clock that is "
        f"not the configured one: {stuck_open}")
