"""The shipped metrics and reports apps are launched every 30 seconds (the operator, 2026-10-03).

At 120 s a metric declaring 150 s was collected about every 230 s: the daemon relaunches the app on
the app's own period, a metric becomes due on its own, and a pass that finds it a few seconds early
leaves it to the next launch. 30 s bounds that lateness at half a minute. The schedule ships twice -
in the catalogue ``init`` seeds a node from, and in the example a public checkout reads - and both
must say it, or a new node gets whichever one was edited last.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ("db_ops/jobs/catalogue/app_commands.json", "data/app_commands.example.json")


def _app(copy: str, app_id: str) -> dict:
    payload = json.loads((ROOT / copy).read_text(encoding="utf-8"))
    commands = payload.get("app_commands") if isinstance(payload, dict) else payload
    return next(item for item in commands if item.get("app_command_id") == app_id)


@pytest.mark.parametrize("copy", SHIPPED)
@pytest.mark.parametrize("app_id", ["APP-METRICS", "APP-REPORTS-CREATE"])
def test_the_app_is_launched_every_30_seconds(copy: str, app_id: str) -> None:
    window = _app(copy, app_id)["time_window"]

    assert window["repeat_interval"] == 30
    # The timeout is how long ONE run may take, not how often it starts: a pass longer than the
    # period delays the next launch (the daemon never starts a second copy), it is not cut short.
    assert window["timeout"] > window["repeat_interval"]
