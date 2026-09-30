"""A container recreated under a new host name must free the runs its predecessor left open - at once.

On 2026-09-30 the worker was upgraded to 0.25.0 with ``docker compose``. The compose file pins no
``hostname``, so the new container came up under a new one. The production engine task (sql_id 37,
every minute) had been killed 71 s into a run by the stop, and its ``running`` row named the old
host. The new container could not check a pid on "another host", so the rule left the row alone
for its timeout plus an hour of grace - and the row is the claim, so **target 1 did not run** until
it was closed by hand 24 minutes later (0.26.0 §1.69). The 0.24.0 upgrade two days earlier had the
same shape.

The host name was standing in for the node, and on a container it is not one. The tool root now
carries an identity of its own (``runtime/node_identity``, kept on the host across a recreate); a
claim records it, and a reaper that finds *its own* identity under a host name it no longer has
knows every process of that host is gone.

What must not change: a row from a genuinely different node still waits for its grace, and a row or
a reaper without an identity behaves exactly as before - an absent identity never reaps sooner.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from db_ops.db import DbOpsStore
from db_ops.lib import node_identity, run_claim

REPO = Path(__file__).resolve().parents[1]
NODE = "3f2a9c0d5e6b4a7c8d9e0f1a2b3c4d5e"
OTHER_NODE = "0123456789abcdef0123456789abcdef"


def _claim(*, host: str, node: str = "", pid: int = 622344) -> dict:
    return run_claim.claim_fields(pid=pid, host=host, node=node)


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def test_a_row_left_by_this_node_under_its_old_host_name_is_freed_at_once():
    """The 2026-09-30 row: 71 s old, timeout 600 s, and still free - its host is gone."""
    verdict = run_claim.reap_verdict(
        metadata=_claim(host="a231498851da", node=NODE), this_host="5b5682f96e0c",
        elapsed_seconds=71, timeout_seconds=600, pid_alive=None, this_node=NODE)

    assert verdict.reap, verdict.reason
    assert "no longer has" in verdict.reason


def test_the_old_pid_is_not_asked_about_because_the_new_container_reuses_numbers():
    """A recreated container counts pids from 1 again: pid 67 of the old one may be alive as
    something else in the new one. Reading that as the run would hold the row for ever."""
    verdict = run_claim.reap_verdict(
        metadata=_claim(host="old-container", node=NODE, pid=67), this_host="new-container",
        elapsed_seconds=5, timeout_seconds=7200, pid_alive=True, this_node=NODE)

    assert verdict.reap


def test_another_node_s_row_still_waits_for_its_grace():
    verdict = run_claim.reap_verdict(
        metadata=_claim(host="other-machine", node=OTHER_NODE), this_host="this-machine",
        elapsed_seconds=600 + 60, timeout_seconds=600, pid_alive=None, this_node=NODE)

    assert not verdict.reap


def test_a_row_written_without_an_identity_waits_for_its_grace_as_before():
    """Every row an older build wrote, and every run started by hand."""
    verdict = run_claim.reap_verdict(
        metadata=_claim(host="a231498851da"), this_host="5b5682f96e0c",
        elapsed_seconds=71, timeout_seconds=600, pid_alive=None, this_node=NODE)

    assert not verdict.reap


def test_a_reaper_without_an_identity_waits_for_the_grace_as_before():
    verdict = run_claim.reap_verdict(
        metadata=_claim(host="a231498851da", node=NODE), this_host="5b5682f96e0c",
        elapsed_seconds=71, timeout_seconds=600, pid_alive=None, this_node="")

    assert not verdict.reap


def test_on_the_same_host_a_live_pid_still_holds_its_row():
    """The identity adds a case; it does not replace the pid check where the pid can be read."""
    verdict = run_claim.reap_verdict(
        metadata=_claim(host="dbabrain", node=NODE), this_host="dbabrain",
        elapsed_seconds=99999, timeout_seconds=600, pid_alive=True, this_node=NODE)

    assert not verdict.reap


def test_startup_frees_the_old_host_s_row_too():
    """The daemon's own recovery - the first scan a recreated container makes."""
    verdict = run_claim.startup_verdict(
        metadata=_claim(host="old-container", node=NODE, pid=67), this_host="new-container",
        elapsed_seconds=30, timeout_seconds=3600, pid_alive=None, this_node=NODE)

    assert verdict.reap


def test_startup_still_leaves_another_node_s_row_alone():
    verdict = run_claim.startup_verdict(
        metadata=_claim(host="other-machine", node=OTHER_NODE), this_host="this-machine",
        elapsed_seconds=30, timeout_seconds=3600, pid_alive=None, this_node=NODE)

    assert not verdict.reap


def test_a_claim_without_an_identity_carries_no_empty_field():
    assert run_claim.NODE_FIELD not in _claim(host="h")


# --------------------------------------------------------------------------- #
# The identity
# --------------------------------------------------------------------------- #
def test_the_identity_is_written_once_and_read_back_the_same(tmp_path):
    first = node_identity.ensure(tmp_path / "runtime")

    assert first
    assert node_identity.ensure(tmp_path / "runtime") == first


def test_two_roots_are_two_nodes(tmp_path):
    assert node_identity.ensure(tmp_path / "a") != node_identity.ensure(tmp_path / "b")


def test_a_root_that_cannot_hold_the_file_has_no_identity_rather_than_a_new_one_each_time(tmp_path):
    blocker = tmp_path / "runtime"
    blocker.write_text("a file where the folder should be", encoding="utf-8")

    assert node_identity.ensure(blocker) == ""


# --------------------------------------------------------------------------- #
# The store records it
# --------------------------------------------------------------------------- #
@pytest.fixture()
def store(tmp_path):
    built = DbOpsStore(tmp_path / "db_ops.sqlite")
    built.initialize()
    return built


def _running_sql_row(store) -> dict:
    run_id = store.insert_sql_run(
        run_key="37|1|SRV", sql_id=37, sql_code="SQL037", target_no=1, server_id="SRV",
        db_type="sqlserver", service_name="S", instance_name="I", database_name="D",
        credential_name="C", status="running", level="logging", message="m",
        started_at="2026-09-30T08:07:54Z", metadata={"host_name": "a231498851da"})
    row = next(r for r in store.fetch_running_sql_runs() if int(r["sql_run_id"]) == run_id)
    return run_claim.row_metadata(row)


def test_a_claim_made_under_a_daemon_records_the_node(store, monkeypatch):
    monkeypatch.setenv(node_identity.ENV_VAR, NODE)

    assert run_claim.claim_node(_running_sql_row(store)) == NODE


def test_a_claim_made_by_hand_records_no_node(store, monkeypatch):
    monkeypatch.delenv(node_identity.ENV_VAR, raising=False)

    assert run_claim.claim_node(_running_sql_row(store)) == ""


# --------------------------------------------------------------------------- #
# Every reaper asks with its identity
# --------------------------------------------------------------------------- #
def test_every_reaper_passes_this_node():
    """A reaper that forgets it silently falls back to the hour's grace - the bug, back."""
    missing = []
    for path in (REPO / "db_ops").rglob("*.py"):
        if path.name == "run_claim.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"reap_verdict", "startup_verdict"}
                    and not any(k.arg == "this_node" for k in node.keywords)):
                missing.append(f"{path.relative_to(REPO)}:{node.lineno}")

    assert not missing, missing


# --------------------------------------------------------------------------- #
# Closing one by hand - the 2026-09-30 script, as a command
# --------------------------------------------------------------------------- #
def _open_run(store) -> int:
    return store.insert_sql_run(
        run_key="37|1|SRV", sql_id=37, sql_code="SQL037", target_no=1, server_id="SRV",
        db_type="sqlserver", service_name="S", instance_name="I", database_name="D",
        credential_name="C", status="running", level="logging", message="m",
        started_at="2026-09-30T08:07:54Z", metadata={"host_name": "a231498851da", "pid": 622344})


def test_closing_a_run_by_hand_releases_its_target(store):
    from db_ops.sql_tasks.runner import close_orphaned_run

    run_id = _open_run(store)
    answer = close_orphaned_run(store=store, sql_run_id=run_id, reason="killed by the upgrade",
                                confirm="yes")

    assert answer["closed"], answer
    assert answer["claim_host"] == "a231498851da"
    assert not store.fetch_running_sql_runs()
    assert _open_run(store), "the target can be claimed again"


def test_closing_by_hand_needs_an_explicit_yes(store):
    from db_ops.sql_tasks.runner import close_orphaned_run

    run_id = _open_run(store)
    answer = close_orphaned_run(store=store, sql_run_id=run_id, reason="r", confirm="")

    assert not answer["closed"]
    assert store.fetch_running_sql_runs()


def test_a_run_that_is_not_running_is_left_with_its_own_ending(store):
    from db_ops.sql_tasks.runner import close_orphaned_run

    run_id = _open_run(store)
    store.update_sql_run(sql_run_id=run_id, status="done", level="logging", message="m",
                         finished_at="2026-09-30T08:09:00Z")
    answer = close_orphaned_run(store=store, sql_run_id=run_id, reason="r", confirm="yes")

    assert not answer["closed"]
