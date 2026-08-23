"""Pulling the encrypted secret store back from the worker.

`--include-secrets` on its own is a plain file copy: the worker's store replaces the master's.
That is last-writer-wins — a ref the master added since the last deploy exists nowhere else and
would be deleted without a word. `--merge-secrets` unions the two stores instead, and refuses
to guess when the same ref holds different values on the two sides.
"""

import json
from pathlib import Path

import pytest

from db_ops.lib.secret_text import encrypt_secret_text, load_secret_text_file
from db_ops.control.worker_data import SecretMergeConflict, _merge_secret_store

KEY = "lab-passphrase"


def write_store(path: Path, secrets: dict) -> Path:
    path.write_text(json.dumps(encrypt_secret_text(secrets, KEY), indent=2) + "\n", encoding="utf-8")
    return path


class FakeSftp:
    """The only thing _merge_secret_store asks of sftp is to fetch the worker's file."""

    def __init__(self, source: Path):
        self.source = source

    def get(self, remote: str, local: str) -> None:
        Path(local).write_bytes(self.source.read_bytes())


def merge(tmp_path, *, master: dict, worker: dict, dry_run: bool = False):
    master_path = write_store(tmp_path / "master.json", master)
    worker_path = write_store(tmp_path / "worker.json", worker)
    action = _merge_secret_store(
        FakeSftp(worker_path), remote="/remote/encrypted_secret_text.json",
        local=master_path, key=KEY, dry_run=dry_run,
    )
    return action, load_secret_text_file(master_path, key=KEY)


def test_a_ref_the_worker_added_arrives_on_the_master(tmp_path):
    action, merged = merge(
        tmp_path,
        master={"SQLSERVER_PROD": "prod-pw"},
        worker={"SQLSERVER_PROD": "prod-pw", "MSSQL_AG_LAB_PASSWORD": "lab-pw"},
    )
    assert action == "MERGED"
    assert merged == {"SQLSERVER_PROD": "prod-pw", "MSSQL_AG_LAB_PASSWORD": "lab-pw"}


def test_a_ref_only_the_master_has_survives_the_pull(tmp_path):
    """The failure a plain copy would cause: the master added ORACLE_PROD after the last deploy,
    so the worker's store has never seen it. Overwriting would delete it."""
    action, merged = merge(
        tmp_path,
        master={"SQLSERVER_PROD": "prod-pw", "ORACLE_PROD": "added-on-the-master"},
        worker={"SQLSERVER_PROD": "prod-pw", "MSSQL_AG_LAB_PASSWORD": "lab-pw"},
    )
    assert action == "MERGED"
    assert merged["ORACLE_PROD"] == "added-on-the-master"
    assert merged["MSSQL_AG_LAB_PASSWORD"] == "lab-pw"


def test_the_same_ref_with_two_different_values_is_a_conflict_and_writes_nothing(tmp_path):
    master_path = write_store(tmp_path / "master.json", {"SQLSERVER_PROD": "the-real-one"})
    worker_path = write_store(tmp_path / "worker.json", {"SQLSERVER_PROD": "a-lab-password"})
    before = master_path.read_bytes()

    with pytest.raises(SecretMergeConflict, match="SQLSERVER_PROD"):
        _merge_secret_store(FakeSftp(worker_path), remote="/remote/x.json",
                            local=master_path, key=KEY, dry_run=False)

    assert master_path.read_bytes() == before  # untouched: guessing would replace a real password


def test_nothing_new_means_nothing_is_rewritten(tmp_path):
    action, merged = merge(
        tmp_path,
        master={"SQLSERVER_PROD": "prod-pw"},
        worker={"SQLSERVER_PROD": "prod-pw"},
    )
    assert action == "UNCHANGED"
    assert merged == {"SQLSERVER_PROD": "prod-pw"}


def test_dry_run_reports_the_new_refs_without_touching_the_store(tmp_path):
    master_path = write_store(tmp_path / "master.json", {"A_PASSWORD": "a"})
    worker_path = write_store(tmp_path / "worker.json", {"A_PASSWORD": "a", "B_PASSWORD": "b"})
    before = master_path.read_bytes()

    action = _merge_secret_store(FakeSftp(worker_path), remote="/remote/x.json",
                                 local=master_path, key=KEY, dry_run=True)

    assert action == "WOULD"
    assert master_path.read_bytes() == before


def test_an_unchanged_encrypted_store_still_updates_a_stale_plaintext_source(tmp_path):
    master_path = write_store(tmp_path / "master.json", {"A_PASSWORD": "a", "B_PASSWORD": "b"})
    worker_path = write_store(tmp_path / "worker.json", {"A_PASSWORD": "a", "B_PASSWORD": "b"})
    plaintext_path = tmp_path / "secret_text.json"
    plaintext_path.write_text(json.dumps({"A_PASSWORD": "a"}) + "\n", encoding="utf-8")
    encrypted_before = master_path.read_bytes()

    action = _merge_secret_store(
        FakeSftp(worker_path),
        remote="/remote/encrypted_secret_text.json",
        local=master_path,
        key=KEY,
        dry_run=False,
        plaintext=plaintext_path,
    )

    assert action == "MERGED"
    assert master_path.read_bytes() == encrypted_before
    assert load_secret_text_file(plaintext_path) == {"A_PASSWORD": "a", "B_PASSWORD": "b"}


def test_a_plaintext_only_ref_is_added_to_the_encrypted_store(tmp_path):
    master_path = write_store(tmp_path / "master.json", {"A_PASSWORD": "a"})
    worker_path = write_store(tmp_path / "worker.json", {"A_PASSWORD": "a"})
    plaintext_path = tmp_path / "secret_text.json"
    plaintext_path.write_text(
        json.dumps({"A_PASSWORD": "a", "MASTER_ONLY": "local"}) + "\n",
        encoding="utf-8",
    )

    action = _merge_secret_store(
        FakeSftp(worker_path),
        remote="/remote/encrypted_secret_text.json",
        local=master_path,
        key=KEY,
        dry_run=False,
        plaintext=plaintext_path,
    )

    expected = {"A_PASSWORD": "a", "MASTER_ONLY": "local"}
    assert action == "MERGED"
    assert load_secret_text_file(master_path, key=KEY) == expected
    assert load_secret_text_file(plaintext_path) == expected


def test_plaintext_conflict_writes_neither_master_store(tmp_path):
    master_path = write_store(tmp_path / "master.json", {"A_PASSWORD": "encrypted-value"})
    worker_path = write_store(tmp_path / "worker.json", {"A_PASSWORD": "encrypted-value"})
    plaintext_path = tmp_path / "secret_text.json"
    plaintext_path.write_text(
        json.dumps({"A_PASSWORD": "different-plaintext-value"}) + "\n",
        encoding="utf-8",
    )
    encrypted_before = master_path.read_bytes()
    plaintext_before = plaintext_path.read_bytes()

    with pytest.raises(SecretMergeConflict, match="master plaintext source"):
        _merge_secret_store(
            FakeSftp(worker_path),
            remote="/remote/encrypted_secret_text.json",
            local=master_path,
            key=KEY,
            dry_run=False,
            plaintext=plaintext_path,
        )

    assert master_path.read_bytes() == encrypted_before
    assert plaintext_path.read_bytes() == plaintext_before


def test_missing_plaintext_source_is_created_from_the_merged_store(tmp_path):
    master_path = write_store(tmp_path / "master.json", {"A_PASSWORD": "a"})
    worker_path = write_store(
        tmp_path / "worker.json",
        {"A_PASSWORD": "a", "WORKER_ONLY": "worker"},
    )
    plaintext_path = tmp_path / "secrets" / "secret_text.json"

    action = _merge_secret_store(
        FakeSftp(worker_path),
        remote="/remote/encrypted_secret_text.json",
        local=master_path,
        key=KEY,
        dry_run=False,
        plaintext=plaintext_path,
    )

    expected = {"A_PASSWORD": "a", "WORKER_ONLY": "worker"}
    assert action == "MERGED"
    assert load_secret_text_file(master_path, key=KEY) == expected
    assert load_secret_text_file(plaintext_path) == expected


def test_plaintext_merge_dry_run_writes_neither_store(tmp_path):
    master_path = write_store(tmp_path / "master.json", {"A_PASSWORD": "a"})
    worker_path = write_store(
        tmp_path / "worker.json",
        {"A_PASSWORD": "a", "WORKER_ONLY": "worker"},
    )
    plaintext_path = tmp_path / "secret_text.json"
    plaintext_path.write_text(json.dumps({"A_PASSWORD": "a"}) + "\n", encoding="utf-8")
    encrypted_before = master_path.read_bytes()
    plaintext_before = plaintext_path.read_bytes()

    action = _merge_secret_store(
        FakeSftp(worker_path),
        remote="/remote/encrypted_secret_text.json",
        local=master_path,
        key=KEY,
        dry_run=True,
        plaintext=plaintext_path,
    )

    assert action == "WOULD"
    assert master_path.read_bytes() == encrypted_before
    assert plaintext_path.read_bytes() == plaintext_before
