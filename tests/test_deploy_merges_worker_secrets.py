"""A deploy must not destroy a secret that only the worker has.

`/spbot_create_db_docker` registers a new database password on the **worker**. A deploy then
re-encrypts the master's plaintext source over the top and ships it, so the ref disappears — the
same loss that destroyed a bot-created SQL task on 2026-07-31, one layer down and harder to spot,
because what breaks later is a connection that used to work.

The merge that prevents it has two properties worth pinning: it writes **both** master stores (or
the next refresh drops the refs again), and it **refuses to guess** when a ref differs on the two
sides — a wrong guess there replaces a production credential with a lab one.

Since 2026-08-11 the merge is **opt-in** (`--merge`): a deploy is master -> worker by default, so
the tests below that exercise the merge say so explicitly. The default itself is pinned by
`test_a_deploy_ships_the_master_unless_the_merge_is_asked_for`, because "which way does a deploy
run" is now a decision the caller makes and a silent flip of it would destroy work in one direction
or the other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from db_ops.lib.secret_text import encrypt_secret_text, load_secret_text_file
from db_ops.control import deploy as deploy_ops
from db_ops.control import worker_data
from db_ops.control.worker_data import SecretMergeConflict

KEY = "lab-passphrase"


def _store(path: Path, secrets: dict) -> Path:
    path.write_text(json.dumps(encrypt_secret_text(secrets, KEY), indent=2) + "\n", encoding="utf-8")
    return path


class _FakeSFTP:
    def __init__(self, worker_store: Path | None):
        self.worker_store = worker_store

    def stat(self, remote_path):
        if self.worker_store is None:
            raise IOError("missing")
        return object()

    def get(self, remote_path, local_path):
        if self.worker_store is None:
            raise IOError("missing")
        Path(local_path).write_text(self.worker_store.read_text(encoding="utf-8"), encoding="utf-8")

    def close(self):
        pass


class _FakeClient:
    def __init__(self, sftp):
        self._sftp = sftp

    def open_sftp(self):
        return self._sftp

    def close(self):
        pass


def _merge(tmp_path, monkeypatch, *, master, worker, plaintext=None, key=KEY, dry_run=False):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    _store(data / "encrypted_secret_text.json", master)
    worker_file = _store(tmp_path / "worker_store.json", worker) if worker is not None else None
    plaintext_path = tmp_path / "secret_text.json"
    if plaintext is not None:
        plaintext_path.write_text(json.dumps(plaintext), encoding="utf-8")
    monkeypatch.setattr(worker_data, "ssh_connect",
                        lambda *a, **k: _FakeClient(_FakeSFTP(worker_file)))
    action = worker_data.merge_worker_secrets(
        host="h", user="u", password="p", key=key,
        to_master_path=str(data), plaintext_secret_path=str(plaintext_path), dry_run=dry_run)
    return action, data / "encrypted_secret_text.json", plaintext_path


def test_a_password_registered_on_the_worker_survives_the_deploy(tmp_path, monkeypatch):
    action, encrypted, _ = _merge(
        tmp_path, monkeypatch,
        master={"PROD_DB": "p1"},
        worker={"PROD_DB": "p1", "PG_LAB_07_PASSWORD": "lab-secret"},
    )

    merged = load_secret_text_file(encrypted, key=KEY)
    assert action == "MERGED"
    assert merged == {"PROD_DB": "p1", "PG_LAB_07_PASSWORD": "lab-secret"}


def test_the_plaintext_source_is_updated_too(tmp_path, monkeypatch):
    """The master keeps two copies and a deploy regenerates the encrypted one from the plaintext.
    Writing only the encrypted store would let the very next deploy drop the merged ref again."""
    action, encrypted, plaintext = _merge(
        tmp_path, monkeypatch,
        master={"PROD_DB": "p1"},
        worker={"PG_LAB_07_PASSWORD": "lab-secret"},
        plaintext={"PROD_DB": "p1"},
    )

    assert action == "MERGED"
    assert json.loads(plaintext.read_text(encoding="utf-8")) == {
        "PROD_DB": "p1", "PG_LAB_07_PASSWORD": "lab-secret"}
    assert load_secret_text_file(encrypted, key=KEY)["PG_LAB_07_PASSWORD"] == "lab-secret"


def test_a_ref_that_differs_on_both_sides_stops_everything(tmp_path, monkeypatch):
    """Same name, two values: one of them is current and the merge cannot tell which. Picking
    either could replace a production credential, so it writes nothing and raises."""
    with pytest.raises(SecretMergeConflict, match="PROD_DB"):
        _merge(tmp_path, monkeypatch,
               master={"PROD_DB": "master-value"},
               worker={"PROD_DB": "worker-value"})


def test_a_master_only_ref_is_never_dropped(tmp_path, monkeypatch):
    """The direction that mattered first: a ref added on the master exists nowhere else, and a
    plain worker->master copy would delete it."""
    _action, encrypted, _ = _merge(
        tmp_path, monkeypatch,
        master={"PROD_DB": "p1", "ONLY_ON_MASTER": "m1"},
        worker={"PROD_DB": "p1"},
    )

    assert load_secret_text_file(encrypted, key=KEY)["ONLY_ON_MASTER"] == "m1"


def test_no_key_skips_instead_of_breaking_the_deploy(tmp_path, monkeypatch, capsys):
    """Both stores are encrypted, so with no key there is nothing to compare. A deploy that ran
    before this step existed has to keep running — but the operator is told what it means."""
    monkeypatch.delenv("DB_OPS_SECRET_KEY", raising=False)
    action, encrypted, _ = _merge(
        tmp_path, monkeypatch,
        master={"PROD_DB": "p1"}, worker={"PG_LAB": "x"}, key=None)

    assert action == "SKIPPED"
    assert load_secret_text_file(encrypted, key=KEY) == {"PROD_DB": "p1"}
    assert "will be lost by this deploy" in capsys.readouterr().out


def test_a_worker_without_a_store_is_skipped(tmp_path, monkeypatch):
    action, encrypted, _ = _merge(
        tmp_path, monkeypatch, master={"PROD_DB": "p1"}, worker=None)

    assert action == "MISSING"
    assert load_secret_text_file(encrypted, key=KEY) == {"PROD_DB": "p1"}


# --------------------------------------------------------------------------- #
# Where it sits in the deploy
# --------------------------------------------------------------------------- #
def test_the_merge_runs_before_anything_is_built_or_shipped(monkeypatch):
    """Order is the whole point. The bundle is assembled from the master's data/, so a merge
    after build-image would ship the un-merged copy; and a conflict has to abort while nothing
    has been built or sent yet.

    `reclaim` comes first for the same reason one step later: everything after it reads the
    worker's files as the SSH user, and anything the container wrote is owned by root. The chown
    `copy_bundle` already does is too late — the merge has skipped what it could not read by then,
    and the copy is what overwrites it."""
    calls = []
    monkeypatch.setattr(deploy_ops, "reclaim_worker_files", lambda **k: calls.append("reclaim"))
    monkeypatch.setattr(deploy_ops, "merge_worker_secrets", lambda **k: calls.append("secrets"))
    monkeypatch.setattr(deploy_ops, "merge_worker_config", lambda **k: calls.append("config"))
    monkeypatch.setattr(deploy_ops, "pull_sql_tree", lambda **k: calls.append("sql"))
    monkeypatch.setattr(deploy_ops, "build_image", lambda **k: calls.append("build"))
    monkeypatch.setattr(deploy_ops, "copy_bundle", lambda **k: calls.append("copy"))
    monkeypatch.setattr(deploy_ops, "start_daemon", lambda **k: calls.append("start"))

    deploy_ops.deploy(host="h", user="u", password="p", key_base64=None, key=None,
                      merge_worker=True)

    assert calls == ["reclaim", "secrets", "config", "sql", "build", "copy", "start"]


def test_a_secret_conflict_aborts_before_the_bundle_is_built(monkeypatch):
    def boom(**_kwargs):
        raise SecretMergeConflict("Refs differ between the master and the worker: PROD_DB")

    built = []
    monkeypatch.setattr(deploy_ops, "reclaim_worker_files", lambda **k: None)
    monkeypatch.setattr(deploy_ops, "merge_worker_secrets", boom)
    monkeypatch.setattr(deploy_ops, "build_image", lambda **k: built.append("build"))
    monkeypatch.setattr(deploy_ops, "copy_bundle", lambda **k: built.append("copy"))

    with pytest.raises(SecretMergeConflict):
        deploy_ops.deploy(host="h", user="u", password="p", key_base64=None, key=None,
                          merge_worker=True)

    assert built == [], "nothing may be built or shipped once the secret stores disagree"


def test_a_deploy_ships_the_master_unless_the_merge_is_asked_for(monkeypatch):
    """The default direction, pinned.

    It was worker -> master -> worker from 2.24.00 until 2026-08-11. That protected bot-registered
    config, and it also made `db_instances.json` un-editable on the master at the paths the worker
    owns — a severity map edited here was silently replaced by the worker's copy, over the master's
    own file, before the build. Both directions can destroy work, so which one runs by default is
    a decision, and a decision belongs in a test rather than in an absent flag.
    """
    calls = []
    monkeypatch.setattr(deploy_ops, "reclaim_worker_files", lambda **k: calls.append("reclaim"))
    monkeypatch.setattr(deploy_ops, "merge_worker_secrets", lambda **k: calls.append("secrets"))
    monkeypatch.setattr(deploy_ops, "merge_worker_config", lambda **k: calls.append("config"))
    monkeypatch.setattr(deploy_ops, "pull_sql_tree", lambda **k: calls.append("sql"))
    monkeypatch.setattr(deploy_ops, "build_image", lambda **k: calls.append("build"))
    monkeypatch.setattr(deploy_ops, "copy_bundle", lambda **k: calls.append("copy"))
    monkeypatch.setattr(deploy_ops, "start_daemon", lambda **k: calls.append("start"))

    deploy_ops.deploy(host="h", user="u", password="p", key_base64=None, key=None)

    assert calls == ["build", "copy", "start"], "no flag means master -> worker"


def test_no_merge_worker_skips_the_secret_step_too(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_ops, "merge_worker_secrets", lambda **k: calls.append("secrets"))
    monkeypatch.setattr(deploy_ops, "merge_worker_config", lambda **k: calls.append("config"))
    monkeypatch.setattr(deploy_ops, "pull_sql_tree", lambda **k: calls.append("sql"))
    monkeypatch.setattr(deploy_ops, "build_image", lambda **k: calls.append("build"))
    monkeypatch.setattr(deploy_ops, "copy_bundle", lambda **k: calls.append("copy"))
    monkeypatch.setattr(deploy_ops, "start_daemon", lambda **k: calls.append("start"))

    deploy_ops.deploy(host="h", user="u", password="p", key_base64=None, key=None,
                      merge_worker=False)

    assert calls == ["build", "copy", "start"]
