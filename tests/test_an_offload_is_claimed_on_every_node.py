"""An offload is claimed on every node; a backup job still on its own host (0.40.0, 1.383).

The run claim is the store's unique index on ``(claim_key, host_name)`` while a row is running: one
run of a job per host. On 2026-11-16 a FULL of 100.197 run by hand on the master and the worker's own
pass both offloaded that backup - each claim held on its own host - and the chat read steps 4 to 10
twice. The repository was not harmed (the second copy found the first and matched it), but two nodes
copying, sealing and deleting the same files at once is a race the claim exists to stop. The operator
chose: **an offload's claim holds across every node**; the other runs keep theirs per host.

The row of a claim of every node writes ``host_name`` as ``""`` - the value the index reads alike on
every node - and keeps the host that runs it in the claim's metadata, where every reader looks first.
"""

from __future__ import annotations

import pytest

from dbabrain.backup_restore import offload, schedule
from dbabrain.db import DbaBrainStore
from dbabrain.db.job_runs import JobRun
from dbabrain.db.store_job_runs import RunAlreadyClaimed
from dbabrain.lib import run_claim


@pytest.fixture
def store(tmp_path):
    return DbaBrainStore(tmp_path / "dbabrain.sqlite")


def _claim(store, *, host, every_node, code="backup_restore.offload_job.SRV_FULL"):
    return store.insert_job_run(JobRun(job_code=code, level="logging", status="RUNNING", message="m",
                                       host_name=host, claim_key=code, metadata={}, claim_global=every_node))


def test_a_claim_of_every_node_refuses_the_same_job_from_another_host(store):
    _claim(store, host="DB-MASTER", every_node=True)
    with pytest.raises(RunAlreadyClaimed, match="already running on another node"):
        _claim(store, host="worker-container", every_node=True)


def test_a_claim_of_one_host_still_lets_another_host_run_its_own(store):
    _claim(store, host="DB-MASTER", every_node=False, code="backup_restore.backup_job.SRV_FULL.full")
    _claim(store, host="worker-container", every_node=False, code="backup_restore.backup_job.SRV_FULL.full")


def test_the_host_that_holds_a_claim_of_every_node_is_still_named_by_the_claim(store):
    log_id = _claim(store, host="DB-MASTER", every_node=True)
    row = next(r for r in store.fetch_recent_job_runs(limit=5) if int(r["log_id"]) == log_id)
    with store.connect() as conn:
        stored = conn.execute("SELECT host_name, metadata_json FROM job_runs WHERE log_id = ?", (log_id,)).fetchone()
    assert stored["host_name"] == ""
    assert run_claim.claim_owner(run_claim.row_metadata(stored), host_fallback=run_claim.row_host(stored))[1] == \
        "DB-MASTER"
    assert row is not None


def test_a_finished_claim_frees_the_job_for_any_node(store):
    log_id = _claim(store, host="DB-MASTER", every_node=True)
    with store.connect() as conn:
        conn.execute("UPDATE job_runs SET status = 'DONE' WHERE log_id = ?", (log_id,))
    _claim(store, host="worker-container", every_node=True)


def test_the_offload_claims_on_every_node_and_a_backup_job_on_its_host(monkeypatch):
    import inspect

    # A backup job and a restore keep their claim on their own host.
    assert inspect.signature(schedule.start_run).parameters["every_node"].default is False
    asked = {}
    monkeypatch.setattr(schedule, "start_run", lambda **kwargs: asked.update(kwargs) or (1, "t"))
    offload.start_offload_run(type("E", (), {"backup_id": "SRV_FULL"})(), store=None, metadata={})
    assert asked["every_node"] is True and asked["job_code"] == "backup_restore.offload_job.SRV_FULL"
