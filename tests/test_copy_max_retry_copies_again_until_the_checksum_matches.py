"""``copy_max_retry``: a staged file is copied again until its checksum is the source's (0.34.0, 1.211).

Why it matters: a restore that runs on a copy nobody proved restores whatever arrived - a truncated
file, a link that flipped a bit, a share a ransomware had half-encrypted. The operator, 2026-10-29:
*the checksum before the copy and after it; only when they match does the restore run; when they
differ, copy again over it, up to a copy max retry (default 3)* - and *test it thoroughly: a network
error, a copy that finished with a different checksum, the retries running out, a copy that is fine*.

Each case runs the real gate against a real SQLite catalog, so it proves two things at once: what the
gate decided, and that every attempt is in the store with the hash it produced - the evidence a
person reads afterwards. The transport is a fake that answers per attempt; the clock never sleeps.
"""

from __future__ import annotations

import hashlib

import pytest

from dbabrain.backup_restore import copy_gate
from dbabrain.backup_restore.copy_gate import FileToCopy, copy_verified
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import integrity as I

GOOD = b"SALESDB log backup, the bytes a restore depends on"
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()
BAD = b"SALESDB log backup, the bytes a restore depends on - with one byte flipped"
BAD_SHA = hashlib.sha256(BAD).hexdigest()


class _Transport:
    """A source that holds ``source``, a target that holds what the last copy left.

    ``plan`` is one entry per copy attempt: ``"ok"`` lands the source's bytes, ``"corrupt"`` lands
    other bytes (the copy "succeeds" with a different checksum), ``"network"`` raises the way a
    dropped SMB/SSH link does, ``"vanish"`` reports success and leaves nothing.
    """

    def __init__(self, plan, *, source=GOOD, target=None):
        self.plan = list(plan)
        self.source = source
        self.target = target
        self.copies = 0

    def hash_source(self, _path):
        return None if self.source is None else (hashlib.sha256(self.source).hexdigest(), len(self.source))

    def hash_target(self, _path):
        return None if self.target is None else (hashlib.sha256(self.target).hexdigest(), len(self.target))

    def copy(self, _item):
        self.copies += 1
        step = self.plan.pop(0)
        if step == "network":
            raise ConnectionResetError("[Errno 104] Connection reset by peer")
        if step == "vanish":
            self.target = None
            return 0
        self.target = self.source if step == "ok" else BAD
        return len(self.target)


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


@pytest.fixture
def run_id(catalog):
    return catalog.start_restore_run(action="copy_only", restore_id="COPY_250_TO_LAB252",
                                     source_server_id="ACME-192-0-2-250",
                                     target_server_id="LAB-198-51-100-52",
                                     target_folder="/opt/stage", copy_max_retry=3)


def _item(**overrides):
    values = dict(source_server_id="ACME-192-0-2-250", target_server_id="LAB-198-51-100-52",
                  file_name="SALESDB_LOG_20261029_0315.trn", file_type="LOG",
                  source_path=r"\\192.0.2.250\SQLBK\SALESDB\LOG\SALESDB_LOG_20261029_0315.trn",
                  target_path="/opt/stage/SALESDB_LOG_20261029_0315.trn",
                  database_name="SALESDB", engine="sqlserver", checksum_after_backup=GOOD_SHA,
                  chain_position=3)
    values.update(overrides)
    return FileToCopy(**values)


def _run(transport, catalog, run_id, *, integrity=None, item=None, slept=None):
    slept = [] if slept is None else slept
    outcome = copy_verified(
        item or _item(), integrity=integrity or I.RestoreIntegrity(),
        hash_source=transport.hash_source, hash_target=transport.hash_target, copy=transport.copy,
        catalog=catalog, restore_run_id=run_id, copy_engine="smbclient+sftp", sleep=slept.append)
    return outcome, slept


def _rows(catalog, run_id):
    files = catalog.run_files(run_id)
    assert len(files) == 1
    return files[0], catalog.attempts(files[0]["run_file_id"])


# --------------------------------------------------------------------------- #
# The four cases the operator named
# --------------------------------------------------------------------------- #
def test_a_copy_that_arrives_intact_is_copied_once_and_recorded_with_both_checksums(catalog, run_id):
    transport = _Transport(["ok"])
    outcome, slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "copied" and outcome.proven
    assert transport.copies == 1 and slept == []
    row, attempts = _rows(catalog, run_id)
    assert row["source_server_id"] == "ACME-192-0-2-250"
    assert row["target_server_id"] == "LAB-198-51-100-52"
    assert row["file_name"] == "SALESDB_LOG_20261029_0315.trn" and row["file_type"] == "LOG"
    assert row["checksum_after_backup"] == GOOD_SHA
    assert row["checksum_source_before_copy"] == GOOD_SHA
    assert row["checksum_target_before_copy"] is None
    assert row["checksum_target_after_copy"] == GOOD_SHA
    assert row["copy_attempts"] == 1 and row["copy_max_retry"] == 3 and row["verdict"] == "copied"
    assert [(a["attempt_no"], a["result"]) for a in attempts] == [(1, "match")]
    assert attempts[0]["checksum_target_after_copy"] == GOOD_SHA
    assert attempts[0]["copy_engine"] == "smbclient+sftp"


def test_a_network_error_is_an_attempt_spent_and_the_next_copy_is_made_after_the_pause(catalog, run_id):
    transport = _Transport(["network", "ok"])
    outcome, slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "recopied" and outcome.proven
    assert slept == [30], "the default pause, once, between the two attempts"
    row, attempts = _rows(catalog, run_id)
    assert [(a["attempt_no"], a["result"]) for a in attempts] == [(1, "copy_error"), (2, "match")]
    assert "Connection reset by peer" in attempts[0]["error_text"]
    assert attempts[0]["checksum_target_after_copy"] is None
    assert row["copy_attempts"] == 2 and row["verdict"] == "recopied"


def test_a_copy_that_finishes_with_a_different_checksum_is_copied_again_over_itself(catalog, run_id):
    transport = _Transport(["corrupt", "ok"])
    outcome, slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "recopied"
    assert transport.copies == 2 and slept == [30]
    row, attempts = _rows(catalog, run_id)
    assert [(a["result"], a["checksum_target_after_copy"]) for a in attempts] == [
        ("mismatch", BAD_SHA), ("match", GOOD_SHA)], "each attempt keeps the hash it produced"
    assert row["checksum_source_before_copy"] == GOOD_SHA
    assert row["checksum_target_after_copy"] == GOOD_SHA


def test_when_the_retries_run_out_the_file_is_not_proven_and_every_attempt_is_kept(catalog, run_id):
    transport = _Transport(["corrupt"] * 4)
    outcome, slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "copy_failed_after_retries" and not outcome.proven
    assert transport.copies == 4, "copy_max_retry 3 = the first copy and three more"
    assert slept == [30, 30, 30], "no pause after the last attempt"
    assert "4 attempt(s)" in outcome.reason
    row, attempts = _rows(catalog, run_id)
    assert [a["attempt_no"] for a in attempts] == [1, 2, 3, 4]
    assert {a["result"] for a in attempts} == {"mismatch"}
    assert row["verdict"] == "copy_failed_after_retries" and row["copy_attempts"] == 4
    assert row["checksum_target_after_copy"] == BAD_SHA, "what is on the target is what the row says"


def test_a_link_that_never_comes_back_also_runs_out_and_says_why(catalog, run_id):
    transport = _Transport(["network"] * 4)
    outcome, _slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "copy_failed_after_retries"
    assert "Connection reset by peer" in outcome.reason
    row, attempts = _rows(catalog, run_id)
    assert [a["result"] for a in attempts] == ["copy_error"] * 4
    assert row["checksum_target_after_copy"] is None


# --------------------------------------------------------------------------- #
# The edges of the count
# --------------------------------------------------------------------------- #
def test_a_copy_that_never_leaves_a_file_on_the_target_runs_out_and_says_so(catalog, run_id):
    transport = _Transport(["vanish"] * 4)
    outcome, slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "copy_failed_after_retries" and not outcome.proven
    assert transport.copies == 4 and slept == [30, 30, 30]
    assert "is not on LAB-198-51-100-52 after the copy" in outcome.reason
    row, attempts = _rows(catalog, run_id)
    assert [a["result"] for a in attempts] == ["copy_error"] * 4
    assert row["target_on_disk"] == 0 and row["checksum_target_after_copy"] is None


def test_the_last_allowed_attempt_still_counts_when_it_succeeds(catalog, run_id):
    transport = _Transport(["network", "corrupt", "vanish", "ok"])
    outcome, slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "recopied"
    assert slept == [30, 30, 30]
    _row, attempts = _rows(catalog, run_id)
    assert [a["result"] for a in attempts] == ["copy_error", "mismatch", "copy_error", "match"]
    assert "not on LAB-198-51-100-52 after the copy" in attempts[2]["error_text"]


def test_copy_max_retry_zero_means_one_copy_and_no_pause(catalog, run_id):
    transport = _Transport(["corrupt", "ok"])
    outcome, slept = _run(transport, catalog, run_id,
                          integrity=I.RestoreIntegrity(copy_max_retry=0))

    assert outcome.verdict == "copy_failed_after_retries"
    assert transport.copies == 1 and slept == []


def test_the_pause_is_the_entrys_own(catalog, run_id):
    transport = _Transport(["network", "network", "ok"])
    _outcome, slept = _run(transport, catalog, run_id,
                           integrity=I.RestoreIntegrity(copy_retry_interval_seconds=5))
    assert slept == [5, 5]


def test_a_larger_copy_max_retry_allows_more_attempts(catalog, tmp_path):
    run = catalog.start_restore_run(action="restore", restore_id="R", source_server_id="S",
                                    target_server_id="T", copy_max_retry=10)
    transport = _Transport(["corrupt"] * 10 + ["ok"])
    outcome, _slept = _run(transport, catalog, run, integrity=I.RestoreIntegrity(copy_max_retry=10))
    assert outcome.verdict == "recopied" and transport.copies == 11


# --------------------------------------------------------------------------- #
# Before any copy
# --------------------------------------------------------------------------- #
def test_a_target_that_already_holds_the_same_bytes_is_not_copied_again(catalog, run_id):
    transport = _Transport([], target=GOOD)
    outcome, _slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "match" and outcome.proven
    assert transport.copies == 0
    row, attempts = _rows(catalog, run_id)
    assert row["checksum_target_before_copy"] == GOOD_SHA == row["checksum_target_after_copy"]
    assert row["copy_attempts"] == 0 and attempts == []


def test_a_target_holding_other_bytes_is_copied_over(catalog, run_id):
    transport = _Transport(["ok"], target=BAD)
    outcome, _slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "copied"
    row, _attempts = _rows(catalog, run_id)
    assert row["checksum_target_before_copy"] == BAD_SHA
    assert row["checksum_target_after_copy"] == GOOD_SHA


def test_a_source_that_changed_after_its_backup_is_never_copied(catalog, run_id):
    transport = _Transport(["ok"], source=BAD)
    outcome, _slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "source_mismatch" and not outcome.proven
    assert transport.copies == 0, "copying again cannot fix a changed backup"
    assert "changed after it was written" in outcome.reason
    row, attempts = _rows(catalog, run_id)
    assert row["checksum_after_backup"] == GOOD_SHA and row["checksum_source_before_copy"] == BAD_SHA
    assert attempts == []


def test_a_source_that_is_gone_is_recorded_and_not_copied(catalog, run_id):
    transport = _Transport(["ok"], source=None)
    outcome, _slept = _run(transport, catalog, run_id)

    assert outcome.verdict == "source_missing" and transport.copies == 0
    row, _attempts = _rows(catalog, run_id)
    assert row["checksum_source_before_copy"] is None and row["verdict"] == "source_missing"


def test_with_the_source_check_off_the_copy_is_still_held_to_the_source(catalog, run_id):
    transport = _Transport(["ok"], source=BAD)
    outcome, _slept = _run(transport, catalog, run_id,
                           integrity=I.RestoreIntegrity(verify_source_before_copy=False))
    assert outcome.verdict == "copied"
    assert outcome.checksum_target_after_copy == BAD_SHA == outcome.checksum_source_before_copy


def test_an_uncatalogued_file_is_compared_copy_to_source_or_refused_as_the_entry_says(catalog, run_id):
    compared, _ = _run(_Transport(["ok"]), catalog, run_id, item=_item(checksum_after_backup=None))
    assert compared.verdict == "copied"

    second = catalog.start_restore_run(action="restore", restore_id="R2", source_server_id="S",
                                       target_server_id="T", copy_max_retry=3)
    transport = _Transport(["ok"])
    refused, _ = _run(transport, catalog, second, item=_item(checksum_after_backup=None),
                      integrity=I.RestoreIntegrity(uncatalogued="refuse"))
    assert refused.verdict == "uncatalogued_refused" and transport.copies == 0


def test_a_source_server_that_cannot_be_asked_ends_the_run_and_writes_no_verdict(catalog, run_id):
    def unreachable(_path):
        raise ConnectionRefusedError("WinRM 5985 refused")

    with pytest.raises(ConnectionRefusedError):
        copy_verified(_item(), integrity=I.RestoreIntegrity(), hash_source=unreachable,
                      hash_target=lambda p: None, copy=lambda i: 0, catalog=catalog,
                      restore_run_id=run_id, sleep=lambda s: None)
    assert catalog.run_files(run_id) == []


def test_the_gate_refuses_to_run_without_a_stated_integrity(catalog, run_id):
    with pytest.raises(I.IntegrityConfigError, match="no integrity stated"):
        copy_verified(_item(), integrity=None, hash_source=lambda p: None, hash_target=lambda p: None,
                      copy=lambda i: 0, catalog=catalog, restore_run_id=run_id)


def test_without_a_catalog_the_gate_still_decides(tmp_path):
    outcome = copy_verified(_item(), integrity=I.RestoreIntegrity(), hash_source=_Transport([]).hash_source,
                            hash_target=_Transport(["ok"]).hash_target,
                            copy=_Transport(["ok"]).copy, sleep=lambda s: None)
    # The fake target stays empty here (three separate fakes), so every attempt is spent.
    assert outcome.verdict == "copy_failed_after_retries"
    assert outcome.run_file_id is None
    assert copy_gate.FileOutcome(verdict="match").proven
