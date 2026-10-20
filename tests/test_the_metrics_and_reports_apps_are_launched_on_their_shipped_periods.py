"""The shipped metrics and reports apps are launched on the period each one needs.

At 120 s a metric declaring 150 s was collected about every 230 s: the daemon relaunches the app on
the app's own period, a metric becomes due on its own, and a pass that finds it a few seconds early
leaves it to the next launch. 30 s bounded that lateness at half a minute (the operator, 2026-10-03),
and the reports app keeps it.

The metrics app goes further since 0.30.0 (the operator, 2026-10-16): every second, `async`, at most
four passes alive. A `sync` command starts no pass while one runs, and the nightly
index-fragmentation scan (1009 s on the largest instance) held INSTANCE_STATUS of every server for
~18 minutes. A pass claims each batch it runs, so the next one runs everything else.

The schedule ships twice - in the catalogue ``init`` seeds a node from, and in the example a public
checkout reads - and both must say it, or a new node gets whichever one was edited last.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ("dbabrain/jobs/catalogue/app_commands.json", "data/app_commands.example.json")


def _app(copy: str, app_id: str) -> dict:
    payload = json.loads((ROOT / copy).read_text(encoding="utf-8"))
    commands = payload.get("app_commands") if isinstance(payload, dict) else payload
    return next(item for item in commands if item.get("app_command_id") == app_id)


@pytest.mark.parametrize("copy", SHIPPED)
def test_the_reports_app_is_launched_every_30_seconds(copy: str) -> None:
    app = _app(copy, "APP-REPORTS-CREATE")
    window = app["time_window"]

    assert window["repeat_interval"] == 30
    assert app["run_mode"] == "sync"
    # The timeout is how long ONE run may take, not how often it starts: a pass longer than the
    # period delays the next launch (the daemon never starts a second copy), it is not cut short.
    assert window["timeout"] > window["repeat_interval"]


@pytest.mark.parametrize("copy", SHIPPED)
def test_the_metrics_app_is_launched_every_second_four_passes_at_most(copy: str) -> None:
    app = _app(copy, "APP-METRICS")

    assert app["time_window"]["repeat_interval"] == 1
    assert (app["run_mode"], app["max_parallel"]) == ("async", 4)
    # Still the budget of one pass, which may hold the slowest metric's batch: above its timeout.
    assert app["time_window"]["timeout"] > 1800
