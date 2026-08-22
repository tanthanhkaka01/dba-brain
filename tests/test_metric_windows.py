"""An on-demand metrics report must not drag windowed metrics out of their window.

The metrics confined to 01-06h are the expensive ones - DBCC CHECKDB, index fragmentation
scans, an Oracle restore validation. `/spbot_report_hourly_metrics` collects with --force, and
--force used to bypass the window check as well as the interval, so typing that command at
15:00 ran all of them against a production instance.
"""

from datetime import datetime

import pytest

from db_ops.lib.time_window import TimeWindow
from db_ops.metrics.collector import _metric_due, _metric_window_open, _window_label
from db_ops.metrics.models import MetricDefinition, MetricTarget


def _metric(**kwargs):
    return MetricDefinition(**{
        "metric_code": "DATABASE_CHECKDB", "db_type": "sqlserver", "category": "integrity",
        "default_importance": 1, "active": True, "interval_seconds": 72000, **kwargs,
    })


def _target():
    return MetricTarget(
        target_id="t1", server_id="s1", ip="10.0.0.1", db_type="sqlserver", db_name="master",
        credential_name="c", port=1433, service_name="svc", instance_name="i",
        connection_info={}, credential={"username": "u", "password_ref": "P"},
    )


NIGHT = TimeWindow(from_hour=1, to_hour=6)
# Naive on purpose: a window is evaluated in local time (now.astimezone() keeps the wall-clock
# hour of a naive value), so naive mocks make these assertions independent of the test host's
# timezone. Pinning them to UTC would assert something different on a UTC+7 machine.
AFTERNOON = datetime(2026, 7, 27, 15, 0)
NIGHT_TIME = datetime(2026, 7, 27, 3, 0)


def test_a_windowed_metric_is_shut_outside_its_hours():
    assert not _metric_window_open(metric=_metric(schedule_window=NIGHT), now=AFTERNOON)
    assert _metric_window_open(metric=_metric(schedule_window=NIGHT), now=NIGHT_TIME)


def test_a_metric_with_no_window_is_always_open():
    """Unset bounds mean always open - the real-time metrics must be unaffected by all this."""
    assert _metric_window_open(metric=_metric(schedule_window=None), now=AFTERNOON)


def test_the_window_is_not_part_of_the_due_check_any_more():
    """This is the whole fix. The window used to live inside _metric_due, which made it
    collateral damage of --force: anything skipping the interval also skipped the window.
    _metric_due now answers only "has enough time elapsed", so --force cannot reach the window."""
    class _Store:

        @classmethod
        def from_config(cls, config, **kwargs):
            """Store doubles must offer the same constructor contract as the real classes."""
            return cls(getattr(config, 'sqlite_path', None))
        def latest_result_time(self, **_): return None
        def latest_successful_result_time(self, **_): return None

    # Never collected -> due, even at an hour its window forbids. The window gate is separate.
    assert _metric_due(store=_Store(), target=_target(), metric=_metric(schedule_window=NIGHT),
                       now=AFTERNOON)


def test_the_skip_reason_names_the_hours_so_the_log_explains_itself():
    assert _window_label(_metric(schedule_window=NIGHT)) == "01-06h"
    assert _window_label(_metric(schedule_window=None)) == "always open"


@pytest.mark.parametrize("include_windowed,expected", [(False, False), (True, True)])
def test_the_reports_collect_step_passes_the_flag_only_when_asked(include_windowed, expected, monkeypatch):
    """The report shells out to `metrics.cli collect --force`; --include-windowed must appear
    in that argv only on request, since it is what re-enables CHECKDB at 15:00."""
    import db_ops.reports.service as service

    seen = {}

    class _Completed:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _Completed()

    monkeypatch.setattr(service.subprocess, "run", _fake_run)
    monkeypatch.setattr(service, "_collect_summary_from_cli_output", lambda _out: {})

    service.collect_target_metrics(config_path="config.json", target_id="T1",
                                   include_windowed=include_windowed)

    assert ("--include-windowed" in seen["argv"]) is expected
    assert "--force" in seen["argv"]      # the interval is still bypassed either way


def _hourly_command():
    """The `/spbot_report_hourly_metrics` definition, shaped like the shipped one.

    It used to be read out of the operator's live `data/telegram_support_commands.json`, which
    tied this test to one estate's catalogue *and* to the inventory that catalogue resolves
    against. The subject here is argument parsing — whether the flag word survives into argv and
    whether `consume_rest` swallows it — so the definition only has to have the shape the parser
    sees. Declaring it makes the test say what it depends on.
    """
    from db_ops.telegram.command_processor import SupportCommand

    return SupportCommand(
        command_id=1,
        command_text="spbot_report_hourly_metrics",
        command_type="action",
        action_type="cli",
        reply_default="",
        reply_text="",
        is_group=True,
        is_private=True,
        need_file=False,
        action_config={
            "working_dir": "tools/db_ops",
            "command_argv": [
                "{python}", "-m", "db_ops.reports.cli", "--config", "{config_path}",
                "force-hourly-report", "--server-id", "{server_id}",
            ],
            "defaults": {"summary_limit": 150, "dedupe_seconds": 0},
            "parameters": [
                {"name": "windowed", "source": "flag", "flag_words": ["full", "all", "+windowed"],
                 "present": "yes", "absent": "no"},
                {"name": "target", "source": "arg", "position": 1, "required": True,
                 "consume_rest": True, "resolve": "target"},
            ],
            "conditional_args": [
                {"parameter": "windowed", "equals": "yes", "argv": ["--include-windowed"]},
            ],
        },
    )


@pytest.mark.parametrize("args,expected", [
    (["ACME-192-0-2-248"], False),
    (["ACME-192-0-2-248", "full"], True),
    (["ACME-192-0-2-248", "all"], True),
    (["mssql", "192.0.2.248", "1433"], False),
    (["mssql", "192.0.2.248", "1433", "full"], True),
])
def test_the_telegram_command_adds_the_flag_only_when_the_word_is_typed(estate, args, expected):
    """End to end through the parser: the word the operator types has to survive into the argv.
    Testing the collector alone would pass while the command still never sent the flag."""
    from db_ops.telegram.command_processor import build_cli_argv, cli_action_values

    estate.db_instances(estate.instance(server_id="ACME-192-0-2-248", ip="192.0.2.248"))
    command = _hourly_command()
    values = cli_action_values(command=command, args=args, config_path=estate.config())
    argv = build_cli_argv(dict(command.action_config), values)

    assert ("--include-windowed" in argv) is expected


def test_the_flag_word_is_not_swallowed_by_the_target(estate):
    """`target` is a consume_rest parameter, so it would otherwise eat the keyword and hand
    "mssql 192.0.2.248 full" to the target resolver as one spec."""
    from db_ops.telegram.command_processor import cli_action_values

    estate.db_instances(estate.instance(server_id="ACME-192-0-2-248", ip="192.0.2.248"))
    values = cli_action_values(command=_hourly_command(),
                               args=["mssql", "192.0.2.248", "full"],
                               config_path=estate.config())

    assert values["target"] == "mssql 192.0.2.248"
