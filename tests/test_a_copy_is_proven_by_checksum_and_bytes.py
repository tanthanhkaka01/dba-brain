"""A copy is proven by its checksum AND its size in bytes, never by one of them (0.34.0, 1.245).

Why it matters: the operator's Backup & Restore Verification scenario (2026-10-30) asks that every
copy be re-hashed at the destination and compared on *checksum and bytes*. A SHA-256 already
decides equality, so the size is the second, independent reading the scenario asks for: it is read
by another call (``stat`` beside ``sha256sum``, ``FileInfo.Length`` beside the .NET hash), and a
hasher that answered for the wrong file - a stale temporary name, a path the shell split - shows up
as two readings that disagree. Disagreeing readings are never proof: the gate copies again.

A size that was not read is no difference (the checksum still decides); a size that was read must be
the same on both sides. Each case runs the real gate against a real SQLite catalog.
"""

from __future__ import annotations

import hashlib

import pytest

from dbabrain.backup_restore import copy_gate
from dbabrain.backup_restore.copy_gate import FileToCopy, confirm_on_target, copy_verified, same_bytes
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import integrity as I

GOOD = b"SALESDB full backup, the bytes a restore depends on"
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


@pytest.fixture
def run_id(catalog):
    return catalog.start_restore_run(action="copy_only", restore_id="COPY_250_TO_LAB252",
                                     source_server_id="ACME-192-0-2-250", target_server_id="LAB-198-51-100-52",
                                     target_folder="/opt/stage", copy_max_retry=3)


def _item(**overrides):
    values = dict(source_server_id="ACME-192-0-2-250", target_server_id="LAB-198-51-100-52",
                  file_name="SALESDB_FULL_20261029.bak", file_type="FULL",
                  source_path="/var/opt/mssql/backup/SALESDB_FULL_20261029.bak",
                  target_path="/opt/stage/SALESDB_FULL_20261029.bak", database_name="SALESDB",
                  engine="sqlserver", checksum_after_backup=GOOD_SHA, size_after_backup=len(GOOD))
    values.update(overrides)
    return FileToCopy(**values)


class _Target:
    """The target answers ``answers`` in turn after each copy - (sha, size) pairs - and ``before``
    when asked before the first copy."""

    def __init__(self, answers, *, before=None):
        self.answers = list(answers)
        self.now = before
        self.copies = 0

    def hash_source(self, _path):
        return GOOD_SHA, len(GOOD)

    def hash_target(self, _path):
        return self.now

    def copy(self, _item):
        self.copies += 1
        self.now = self.answers.pop(0)
        return len(GOOD)


def _run(target, catalog, run_id, item=None):
    return copy_verified(item or _item(), integrity=I.RestoreIntegrity(), hash_source=target.hash_source,
                         hash_target=target.hash_target, copy=target.copy, catalog=catalog,
                         restore_run_id=run_id, copy_engine="sftp", sleep=lambda _s: None)


def test_the_same_checksum_with_another_size_is_not_proof_and_the_file_is_copied_again(catalog, run_id):
    target = _Target([(GOOD_SHA, len(GOOD) - 1), (GOOD_SHA, len(GOOD))])
    outcome = _run(target, catalog, run_id)

    assert outcome.verdict == "recopied" and target.copies == 2
    attempts = catalog.attempts(catalog.run_files(run_id)[0]["run_file_id"])
    assert [a["result"] for a in attempts] == ["mismatch", "match"]
    assert f"{len(GOOD) - 1} bytes on the target, {len(GOOD)} at the source" in attempts[0]["error_text"]


def test_a_size_that_never_agrees_spends_every_attempt(catalog, run_id):
    target = _Target([(GOOD_SHA, 1)] * 4)
    outcome = _run(target, catalog, run_id)

    assert outcome.verdict == "copy_failed_after_retries" and not outcome.proven
    assert target.copies == 4


def test_a_target_already_holding_the_checksum_at_another_size_is_copied_not_matched(catalog, run_id):
    target = _Target([(GOOD_SHA, len(GOOD))], before=(GOOD_SHA, 7))
    outcome = _run(target, catalog, run_id)

    assert outcome.verdict == "copied" and target.copies == 1


def test_a_target_holding_the_same_checksum_and_size_is_matched_without_a_copy(catalog, run_id):
    target = _Target([], before=(GOOD_SHA, len(GOOD)))
    outcome = _run(target, catalog, run_id)

    assert outcome.verdict == "match" and target.copies == 0


def test_a_size_nobody_read_is_no_difference_and_the_checksum_decides(catalog, run_id):
    target = _Target([(GOOD_SHA, None)])
    outcome = _run(target, catalog, run_id)

    assert outcome.verdict == "copied"


def test_a_file_confirmed_against_the_catalog_alone_is_held_to_the_recorded_size_too():
    item = _item()
    integrity = I.RestoreIntegrity()

    outcomes, remaining = confirm_on_target([item], integrity=integrity,
                                            hash_targets=lambda paths: {p: (GOOD_SHA, len(GOOD)) for p in paths})
    assert outcomes[0].verdict == "match" and remaining == []

    outcomes, remaining = confirm_on_target([item], integrity=integrity,
                                            hash_targets=lambda paths: {p: (GOOD_SHA, len(GOOD) + 1) for p in paths})
    assert outcomes[0].verdict == "copying" and remaining == [0], "another size goes through the full gate"


def test_a_base_backup_on_the_target_is_confirmed_by_its_manifest_not_its_directory_size():
    """A PostgreSQL base backup is hashed through its backup_manifest and recorded with its whole
    directory's size: held to the manifest's size it never matched, and was copied again (2026-11-02)."""
    item = _item(file_name="20261020T010000Z_FULL", file_type="FULL_BASE", engine="postgresql",
                 source_path="/opt/backup/pg/base/20261020T010000Z_FULL/backup_manifest",
                 target_path="/opt/stage/base/20261020T010000Z_FULL/backup_manifest",
                 size_after_backup=52_000_000_000)
    outcomes, remaining = confirm_on_target([item], integrity=I.RestoreIntegrity(),
                                            hash_targets=lambda paths: {p: (GOOD_SHA, 180_000) for p in paths})
    assert outcomes[0].verdict == "match" and remaining == []


@pytest.mark.parametrize("found, sha, size, expected", [
    ((GOOD_SHA, 10), GOOD_SHA, 10, True),
    ((GOOD_SHA, 10), GOOD_SHA, 11, False),
    ((GOOD_SHA, None), GOOD_SHA, 11, True),
    ((GOOD_SHA, 10), GOOD_SHA, None, True),
    (("0" * 64, 10), GOOD_SHA, 10, False),
    (None, GOOD_SHA, 10, False),
    ((GOOD_SHA, 10), None, 10, False),
])
def test_same_bytes_needs_the_checksum_and_any_size_both_sides_read(found, sha, size, expected):
    assert same_bytes(found, sha, size) is expected
    assert copy_gate.same_bytes is same_bytes
