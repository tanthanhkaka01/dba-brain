"""`control rotate-passphrase` moves every store the passphrase opens, in order, or stops (0.42.0, 1.411).

Without `--apply` it only reads: both secret stores and every active envelope must open with the old
passphrase, or nothing happens. With it: the worker's container stops first (no job runs on a store that
changes under it), the runtime store's envelopes move in one transaction, then the master's and the
worker's secret stores, and the container starts and is unlocked with the NEW passphrase last.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbabrain.control import passphrase_rotation as R
from dbabrain.lib import secret_text

OLD, NEW = "old pass", "new pass"


class _Worker:
    def __init__(self, text):
        self.store = text
        self.calls: list[str] = []

    def get(self, remote, local):
        self.calls.append("get")
        Path(local).write_text(self.store, encoding="utf-8")

    def put_bytes(self, content, remote):
        self.calls.append("put")
        self.store = content.decode("utf-8")

    def close(self):
        pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "encrypted_secret_text.json").write_text(
        json.dumps(secret_text.encrypt_secret_text({"M": "1"}, OLD, iterations=1000)), encoding="utf-8")
    worker = _Worker(json.dumps(secret_text.encrypt_secret_text({"W": "2", "X": "3"}, OLD, iterations=1000)))
    order: list[str] = []
    monkeypatch.setattr(R, "ssh_connect", lambda *a, **k: worker)
    monkeypatch.setattr(R, "ssh_run", lambda _c, command, **_k: order.append(command.split("&& ")[-1]) or 0)
    monkeypatch.setattr(R, "_store_target", lambda _p, key: key)
    monkeypatch.setattr(R.worker_unlock, "unlock_worker",
                        lambda _c, **kw: order.append(f"unlock {kw['secret_key']}") or 0)
    from dbabrain.db import passphrase_rotation as P
    facts = {"repository_keys": {"active": 8, "retired": 0, "not_opening": []},
             "backup_certificates": {"active": 1, "retired": 0, "not_opening": []}}
    monkeypatch.setattr(P, "plan", lambda target, old_passphrase: (order.append(f"plan {old_passphrase}"), facts)[1])
    monkeypatch.setattr(P, "reseal_all", lambda target, **kw: order.append(
        f"reseal {kw['old_passphrase']}->{kw['new_passphrase']}") or {"repository_keys": [], "backup_certificates": []})
    return data, worker, order, facts


def _rotate(data, **kw):
    return R.rotate_passphrase(host="192.0.2.10", user="dev", password="pw", old_key=OLD, new_key=NEW,
                               data_dir=data, say=lambda _line: None, **kw)


def test_a_plan_reads_everything_and_changes_nothing(world):
    data, worker, order, _facts = world
    before = (data / "encrypted_secret_text.json").read_text(encoding="utf-8"), worker.store

    assert _rotate(data) == 0
    assert order == [f"plan {OLD}"] and "put" not in worker.calls
    assert ((data / "encrypted_secret_text.json").read_text(encoding="utf-8"), worker.store) == before


def test_apply_stops_reseals_seals_both_stores_starts_and_unlocks_with_the_new_one_in_that_order(world):
    data, worker, order, _facts = world

    assert _rotate(data, apply=True) == 0
    assert order == [f"plan {OLD}", "docker compose stop -t 120", f"reseal {OLD}->{NEW}",
                     "docker compose up -d", f"unlock {NEW}", f"plan {NEW}"]
    assert secret_text.load_secret_text(data, key=NEW) == {"M": "1"}
    assert secret_text.decrypt_secret_text(json.loads(worker.store), NEW) == {"W": "2", "X": "3"}


def test_a_row_that_does_not_open_stops_the_run_before_the_worker_is_touched(world):
    data, worker, order, facts = world
    facts["backup_certificates"]["not_opening"] = [7]

    with pytest.raises(R.RotationRefused, match="nothing was changed"):
        _rotate(data, apply=True)
    assert order == [f"plan {OLD}"] and "put" not in worker.calls


def test_the_same_passphrase_twice_is_refused(world):
    data, _worker, _order, _facts = world
    with pytest.raises(R.RotationRefused, match="is the old one"):
        R.rotate_passphrase(host="h", user="u", password="p", old_key=OLD, new_key=OLD, data_dir=data)
