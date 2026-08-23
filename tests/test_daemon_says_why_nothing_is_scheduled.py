"""A daemon with nothing to run must not look like a daemon that is working.

Both print the same tick, forever: `status=running`, `active_commands=0`. Nothing distinguishes
"there is no work right now" from "everything you configured was silently discarded", and the
three reasons a schedule is empty have three different fixes.

The third is the one that catches people, and it caught this project on 2026-08-23. The shipped
`data/app_commands.example.json` was written for the operator's two-node estate, where a `worker`
runs the schedule. A single-machine install copies it, starts the daemon — which defaults to
`master` — and watches it tick indefinitely with every command filtered out by a role the user
never chose. The example says `all` now, and the daemon says so when it happens.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.jobs.daemon import _command_runs_on_node, load_app_commands

EXAMPLE = Path(__file__).resolve().parent.parent / "data" / "app_commands.example.json"


def test_the_shipped_example_runs_on_a_single_machine():
    """`all` runs on master and worker both; `worker` runs on neither, for a default install."""
    commands = json.loads(EXAMPLE.read_text(encoding="utf-8"))["app_commands"]
    assert commands, "the example schedules nothing"
    for command in commands:
        assert command.get("node_role") == "all", (
            f"{command['app_command_id']} is node_role={command.get('node_role')!r}; a reader "
            f"copying this file runs the daemon as 'master' and it would never run")


def test_the_example_loads_and_every_command_runs_on_a_default_node(tmp_path):
    """Parsing is not scheduling — check through the loader the daemon actually uses."""
    target = tmp_path / "app_commands.json"
    target.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    loaded = load_app_commands(target, logger=None)
    assert loaded, "the loader returned nothing for the shipped example"
    for command in loaded.values():
        assert _command_runs_on_node(command, "master"), (
            f"{command.app_command_id} would not run on a default single-machine install")


@pytest.mark.parametrize("node_role", ["master", "worker"])
def test_all_means_all(node_role: str):
    target = load_app_commands(EXAMPLE, logger=None)
    for command in target.values():
        assert _command_runs_on_node(command, node_role)
