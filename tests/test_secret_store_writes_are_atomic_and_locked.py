"""The secret stores are rewritten atomically, under a lock, and created private (review 0.25.0, B9.1).

A read-modify-write with nothing between two writers kept only one of two concurrent changes (a
console password change and a rotation), a crash mid-write left a truncated store, and a new
plaintext source was created world-readable by umask.
"""

from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from db_ops.lib import secret_text

KEY = "test-passphrase"


def test_concurrent_writers_lose_nothing(tmp_path):
    refs = [f"REF_{n}" for n in range(12)]
    errors: list[BaseException] = []

    def write(ref):
        try:
            secret_text.set_secret_text(tmp_path, ref, f"value-{ref}", key=KEY)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(ref,)) for ref in refs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    stored = secret_text.load_secret_text_file(tmp_path / secret_text.ENCRYPTED_SECRET_TEXT_FILENAME, key=KEY)
    assert {ref: stored.get(ref) for ref in refs} == {ref: f"value-{ref}" for ref in refs}


def test_registering_a_target_takes_the_same_lock_as_every_other_writer(tmp_path):
    """`instance-add` and `remote-credential-add` store a secret through a writer of their own, and
    it was the one left without the lock: beside a console password change or a rotation, whichever
    wrote second kept only its own change (found reading the fixes back, 2026-10-02)."""
    from db_ops.common import instance_admin

    added = [f"INSTANCE_{n}" for n in range(8)]
    rotated = [f"ROTATED_{n}" for n in range(8)]
    errors: list[BaseException] = []

    def run(write, ref):
        try:
            write(ref)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(
        lambda ref: instance_admin._store_secret(tmp_path, ref, f"value-{ref}", KEY), ref))
        for ref in added]
    threads += [threading.Thread(target=run, args=(
        lambda ref: secret_text.set_secret_text(tmp_path, ref, f"value-{ref}", key=KEY), ref))
        for ref in rotated]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    stored = secret_text.load_secret_text_file(tmp_path / secret_text.ENCRYPTED_SECRET_TEXT_FILENAME, key=KEY)
    assert {ref: stored.get(ref) for ref in added + rotated} == {
        ref: f"value-{ref}" for ref in added + rotated}


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_new_store_is_private(tmp_path):
    secret_text.set_secret_text(tmp_path, "A", "x", key=KEY)
    mode = stat.S_IMODE((tmp_path / secret_text.ENCRYPTED_SECRET_TEXT_FILENAME).stat().st_mode)
    assert mode & 0o077 == 0


def test_a_failed_write_leaves_the_old_store_whole(tmp_path, monkeypatch):
    secret_text.set_secret_text(tmp_path, "A", "x", key=KEY)
    path = tmp_path / secret_text.ENCRYPTED_SECRET_TEXT_FILENAME
    before = path.read_text(encoding="utf-8")

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("db_ops.lib.json_io.os.replace", broken)
    with pytest.raises(OSError):
        secret_text.set_secret_text(tmp_path, "B", "y", key=KEY)

    assert path.read_text(encoding="utf-8") == before
    json.loads(before)
