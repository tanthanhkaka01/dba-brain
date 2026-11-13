"""The repository keeps each copy by its file type's retention, from the entry's config (0.34.0).

Why it matters: the operator, 2026-10-31 - *each file type's retention in the repository comes from
config; every config complete and usable, dynamic, nothing hard-coded*. A repository that keeps
everything fills its disk and then refuses tomorrow's copy; one that prunes by age alone deletes the
full that last week's logs need, or the certificate every encrypted backup needs, and the copies
left behind restore nothing. So the rules held here, all from ``offload_retention`` - seconds per
file type, a type not named kept for good (0.37.0, the operator 2026-11-11: *one array, the file type
and how long*):

* a copy goes by its own type's age;
* never the newest chain of a database - from the start of its newest full's run;
* a full never while a copy that depends on it is kept;
* a certificate never;
* an expired copy is deleted one verified file per item - a plain copy by its backup's facts, a base
  backup directory by its offload's proofs, an encrypted copy by its ``.age`` files' facts - and one
  copy refused never stops another.

And the pass's own numbers - batch size, pauses, timeouts, the stall limit - are the entry's, read
from the entry, each held to a range; there is no number of the offload's own left to tune.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dbabrain.backup_restore import offload as O
from dbabrain.lib import integrity as I
from dbabrain.lib import offload as L
from tests.test_every_backup_is_copied_off_its_server_before_it_is_released import (
    HOST_DIR, SRC, _backup, _entry, _hosts, _Net, _offload, _rows, _sha)

DAY = 86400
NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def _copy(file_id, file_type, ended, *, database="APPDB", chain_start=None, name=None):
    return {"file_id": file_id, "file_type": file_type, "database_name": database,
            "file_name": name or f"{file_type}_{file_id}", "backup_ended_at": ended,
            "chain_start": chain_start or ended}


def _names(rows):
    return sorted(r["file_name"] for r in rows)


# --------------------------------------------------------------------------- #
# The rules, as a plan
# --------------------------------------------------------------------------- #
def test_nothing_expires_while_every_type_is_kept_for_good():
    rows = [_copy(1, "FULL", "2025-01-01T00:00:00Z"), _copy(2, "LOG", "2025-01-01T01:00:00Z")]
    assert O.expiry_plan(rows, _offload(), now=NOW) == ([], [])


def test_each_copy_goes_by_its_own_types_age():
    rows = [_copy(1, "FULL", "2026-10-01T00:00:00Z"), _copy(2, "LOG", "2026-10-02T00:00:00Z"),
            _copy(3, "LOG", "2026-10-20T00:00:00Z"),
            _copy(4, "FULL", "2026-10-25T02:00:00Z"), _copy(5, "LOG", "2026-10-30T00:00:00Z")]
    expire, _kept = O.expiry_plan(rows, _offload(retention={"LOG": 7 * DAY}), now=NOW)
    assert _names(expire) == ["LOG_2", "LOG_3"], "logs older than 7 days, outside the newest chain"


def test_the_newest_chain_is_kept_however_old_it_is():
    rows = [_copy(1, "FULL", "2025-01-01T00:00:00Z"), _copy(2, "LOG", "2025-01-02T00:00:00Z")]
    expire, _kept = O.expiry_plan(rows, _offload(retention={"FULL": DAY, "LOG": DAY}), now=NOW)
    assert expire == [], "the only chain is the newest - the repository can still restore its latest point"


def test_a_log_written_while_the_newest_full_ran_belongs_to_it():
    rows = [_copy(1, "FULL", "2026-09-01T00:00:00Z"),
            _copy(2, "FULL", "2026-10-01T04:00:00Z", chain_start="2026-10-01T02:00:00Z"),
            _copy(3, "LOG", "2026-10-01T03:00:00Z")]
    expire, _kept = O.expiry_plan(rows, _offload(retention={"FULL": DAY, "LOG": DAY}), now=NOW)
    assert _names(expire) == ["FULL_1"]


def test_a_full_past_its_retention_stays_while_a_copy_that_needs_it_is_kept():
    rows = [_copy(1, "FULL", "2026-09-01T00:00:00Z"), _copy(2, "DIFF", "2026-10-28T00:00:00Z"),
            _copy(3, "FULL", "2026-10-30T00:00:00Z")]
    offload = _offload(retention={"FULL": 7 * DAY, "DIFF": 30 * DAY})
    expire, kept = O.expiry_plan(rows, offload, now=NOW)
    assert expire == [] and kept == ["FULL_1: past its retention, kept - a copy that depends on it is not"]

    expire, _kept = O.expiry_plan(rows, _offload(retention={"FULL": 7 * DAY, "DIFF": DAY}), now=NOW)
    assert _names(expire) == ["DIFF_2", "FULL_1"], "once its diff goes, the full may go too"


def test_a_certificate_is_never_expired_on_the_repository():
    rows = [_copy(1, "CERT", "2025-01-01T00:00:00Z", name="backup_cert.cer"),
            _copy(2, "FULL", "2025-01-01T00:00:00Z"), _copy(3, "FULL", "2026-10-30T00:00:00Z")]
    expire, kept = O.expiry_plan(rows, _offload(retention={"FULL": DAY}), now=NOW)
    assert _names(expire) == ["FULL_2"]
    assert kept == ["backup_cert.cer: a certificate - never expired on the repository"]


def test_each_database_is_its_own_chain():
    rows = [_copy(1, "FULL", "2026-09-01T00:00:00Z", database="HR"),
            _copy(2, "FULL", "2026-09-01T00:00:00Z", database="APP"),
            _copy(3, "FULL", "2026-10-30T00:00:00Z", database="APP")]
    expire, _kept = O.expiry_plan(rows, _offload(retention={"FULL": DAY}), now=NOW)
    assert _names(expire) == ["FULL_2"], "HR's only full is its newest chain"


# --------------------------------------------------------------------------- #
# The pass on the repository
# --------------------------------------------------------------------------- #
@pytest.fixture
def net(monkeypatch):
    fake = _Net()
    monkeypatch.setattr(O.common_cli, "run", fake.run)
    # The source release: one delete-verified-file per file (0.37.0).
    monkeypatch.setattr(O.common_cli, "run_allowing_failure", fake.run_allowing_failure)
    return fake


@pytest.fixture
def catalog(tmp_path):
    from dbabrain.db.backup_catalog import BackupCatalogStore

    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _offloaded(catalog, net, entry):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL_1.bak", b"old full"),
                           ("APPDB", "LOG", "APPDB_LOG_1.trn", b"old log")], ended="2026-09-01T01:00:00Z")
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL_2.bak", b"new full"),
                           ("APPDB", "LOG", "APPDB_LOG_2.trn", b"new log")], ended="2026-10-30T01:00:00Z")
    O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                    rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)


def test_an_expired_copy_is_deleted_on_the_repository_inside_its_servers_folder_and_marked(catalog, net):
    entry = _entry(offload=_offload(retention={"FULL": 7 * DAY, "LOG": 7 * DAY}))
    _offloaded(catalog, net, entry)

    answer = O.prune_repository(entry, catalog=catalog, hosts=_hosts(entry), now=NOW)

    assert answer["expired"] == 2 and answer["refused"] == []
    assert sorted(net.repo) == ["/repository/A1A-SRC-01/APPDB/FULL/APPDB_FULL_2.bak",
                                "/repository/A1A-SRC-01/APPDB/LOG/APPDB_LOG_2.trn"]
    assert net.calls.count("delete-verified-files") == 2, "one list per copy"
    assert sorted((r["file_name"], r["size_bytes"], r["sha256"]) for r in net.delete_requests) == [
        ("APPDB_FULL_1.bak", 8, _sha(b"old full")), ("APPDB_LOG_1.trn", 7, _sha(b"old log"))], \
        "each file deleted with its every fact"
    assert set(net.fences) == {"/repository/A1A-SRC-01"}
    rows = _rows(catalog)
    assert rows["APPDB_FULL_1.bak"]["offload_state"] == "expired" and rows["APPDB_FULL_1.bak"]["offload_expired_at"]
    assert rows["APPDB_FULL_2.bak"]["offload_state"] == "verified"
    assert f"{HOST_DIR}/APPDB/FULL/APPDB_FULL_1.bak" in net.source, "the server's copy is not the repository's to touch"
    assert catalog.files_to_offload(backup_id=entry.backup_id) == [], "an expired copy is not copied again"


def test_a_released_file_whose_copy_expires_is_gone_and_the_store_accepts_it(catalog, net):
    entry = _entry(offload=_offload(delete_source=True, retention={"FULL": 7 * DAY, "LOG": 7 * DAY}))
    _offloaded(catalog, net, entry)
    O.release_sources(entry, catalog=catalog, hosts=_hosts(entry),
                      rows=catalog.files_to_release(backup_id=entry.backup_id))

    O.prune_repository(entry, catalog=catalog, hosts=_hosts(entry), now=NOW)

    row = _rows(catalog)["APPDB_FULL_1.bak"]
    assert row["state"] == "pruned" and row["source_deleted_at"] and row["offload_state"] == "expired"


def test_the_store_lets_only_a_verified_copy_expire():
    from dbabrain.db.backup_catalog import BackupCatalogError, BackupCatalogStore
    import tempfile
    import os

    store = BackupCatalogStore(os.path.join(tempfile.mkdtemp(), "c.sqlite"))
    store.initialize()
    file_id = store.record_backup_file(server_id=SRC, engine="sqlserver", file_name="a.bak", file_type="FULL",
                                       file_path="/b/a.bak", verdict="ok", size_bytes=1,
                                       checksum_after_backup=_sha(b"a"))
    with pytest.raises(BackupCatalogError, match="only a copy that was verified can expire"):
        store.set_backup_file(file_id, offload_state="expired", offload_expired_at="2026-10-31T00:00:00Z")


def test_a_pass_with_retention_on_but_nothing_old_writes_nothing(catalog, net, monkeypatch):
    entry = _entry(offload=_offload(retention={"LOG": 365 * DAY}))
    _offloaded(catalog, net, entry)
    assert O._expirable(entry, catalog) == []
    assert O._expirable(_entry(offload=_offload()), catalog) == [], "every type kept for good: not even read"


# --------------------------------------------------------------------------- #
# The pass's own numbers are the entry's
# --------------------------------------------------------------------------- #
def _stated(**overrides):
    entry = {name: value for name, value in L.OFFLOAD_FIELDS.items()}
    entry.update({"offload_enabled": True, "offload_server_id": "R", "offload_folder": "/repository"})
    entry.update(overrides)
    return entry


def test_every_run_field_is_stated_read_and_written_back():
    parsed = L.parse_backup_offload(_stated(offload_files_per_pass=7, offload_stall_seconds=45))
    assert parsed.files_per_pass == 7 and parsed.stall_seconds == 45
    assert parsed.as_record() == _stated(offload_files_per_pass=7, offload_stall_seconds=45)
    assert set(L.RUN_FIELD_RANGES) <= set(L.OFFLOAD_FIELDS)


@pytest.mark.parametrize("name, value", [
    ("offload_files_per_pass", 0), ("offload_files_per_pass", 10001), ("offload_copy_retry_interval_seconds", -1),
    ("offload_run_timeout_seconds", 59), ("offload_copy_timeout_seconds", "86400"),
    ("offload_hash_timeout_seconds", True), ("offload_stall_seconds", 9),
])
def test_a_run_field_out_of_its_range_is_refused_by_name(name, value):
    with pytest.raises(L.OffloadConfigError, match=name):
        L.parse_backup_offload(_stated(**{name: value}))


def test_an_entry_from_before_the_run_fields_is_given_all_of_them_by_upgrade_config():
    document = {"backup_restore": {"backups": [{"backup_id": "B", **{k: v for k, v in _stated().items()
                                                                     if k not in L.RUN_FIELD_RANGES}}]}}
    changes = L.state_offload(document)
    entry = document["backup_restore"]["backups"][0]
    assert changes and all(entry[name] == L.OFFLOAD_FIELDS[name] for name in L.RUN_FIELD_RANGES)


def test_the_pass_hands_the_entrys_own_numbers_to_every_call(catalog, net, monkeypatch):
    seen = []
    real = net.run

    def spy(command, request, **kwargs):
        seen.append((command, request.get("timeout_seconds"), request.get("stall_seconds"),
                     kwargs.get("timeout_seconds")))
        return real(command, request, **kwargs)

    monkeypatch.setattr(O.common_cli, "run", spy)
    net.corrupt_next = 1
    slept = []
    entry = _entry(offload=_offload(hash_timeout_seconds=120, copy_timeout_seconds=900, stall_seconds=33,
                                    copy_retry_interval_seconds=7, copy_overwrite=True))
    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_1.trn", b"log")])
    O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                    rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=slept.append)

    assert ("file-checksum", 120, None, 120 + O._PROCESS_MARGIN_SECONDS) in seen
    assert ("copy-files", 900, 33, 900 + O._PROCESS_MARGIN_SECONDS) in seen
    assert slept == [7], "the pause between two copies is offload_copy_retry_interval_seconds"


def test_a_pass_takes_the_entrys_files_per_pass_unless_told_otherwise(catalog, net, monkeypatch):
    from types import SimpleNamespace

    entry = _entry(offload=_offload(files_per_pass=1), jobs=(SimpleNamespace(notify=None),))
    _backup(catalog, net, [("APPDB", "LOG", "A.trn", b"a"), ("APPDB", "LOG", "B.trn", b"b")])
    taken = []
    monkeypatch.setattr(O, "entries_to_offload", lambda _p: [entry])
    monkeypatch.setattr(O.DbaBrainStore, "from_config", classmethod(lambda cls, _c: object()))
    monkeypatch.setattr(O.BackupCatalogStore, "from_config", classmethod(lambda cls, _c: catalog))
    monkeypatch.setattr(O.schedule, "reap_stale_runs", lambda **_k: [])
    monkeypatch.setattr(O, "_run_one", lambda e, **k: taken.append(len(k["rows"])) or {"status": "done"})

    O.run_offloads(app_config=None, config_path="x")
    O.run_offloads(app_config=None, config_path="x", limit=5)
    assert taken == [1, 2]


def test_the_offload_states_list_holds_expired():
    assert "expired" in I.OFFLOAD_STATES


# --------------------------------------------------------------------------- #
# Found by the second review of 2026-10-31, each one checked here
# --------------------------------------------------------------------------- #
def test_the_pieces_of_one_full_are_one_unit_kept_together_while_a_copy_needs_them():
    start = "2026-09-01T00:00:00Z"
    rows = [_copy(1, "FULL", "2026-09-01T01:00:00Z", chain_start=start, name="APP_FULL_1_of_2.bak"),
            _copy(2, "FULL", "2026-09-01T01:00:00Z", chain_start=start, name="APP_FULL_2_of_2.bak"),
            _copy(3, "LOG", "2026-10-28T00:00:00Z"),
            _copy(4, "FULL", "2026-10-30T00:00:00Z")]
    expire, _kept = O.expiry_plan(rows, _offload(retention={"FULL": 7 * DAY, "LOG": 30 * DAY}), now=NOW)
    assert expire == [], "no piece of a full goes while the log after it stays"

    expire, _kept = O.expiry_plan(rows, _offload(retention={"FULL": 7 * DAY, "LOG": DAY}), now=NOW)
    assert _names(expire) == ["APP_FULL_1_of_2.bak", "APP_FULL_2_of_2.bak", "LOG_3"]


def test_a_database_with_no_full_on_the_repository_expires_nothing():
    rows = [_copy(1, "LOG", "2025-01-01T00:00:00Z"), _copy(2, "DIFF", "2025-01-02T00:00:00Z")]
    expire, kept = O.expiry_plan(rows, _offload(retention={"DIFF": DAY, "LOG": DAY}), now=NOW)
    assert expire == [] and kept == ["APPDB: no full on the repository - nothing of it expires"]


def test_a_control_file_and_spfile_travel_with_the_full_of_their_run():
    start = "2026-09-01T00:00:00Z"
    rows = [_copy(1, "RMAN_L0", "2026-09-01T01:00:00Z", chain_start=start),
            _copy(2, "CONTROLFILE", "2026-09-01T01:05:00Z", chain_start=start),
            _copy(3, "SPFILE", "2026-09-01T01:05:00Z", chain_start=start),
            _copy(4, "ARCHIVELOG", "2026-10-28T00:00:00Z"),
            _copy(5, "CONTROLFILE", "2026-08-01T00:00:00Z", chain_start="2026-08-01T00:00:00Z"),
            _copy(6, "RMAN_L0", "2026-10-30T00:00:00Z")]
    expire, kept = O.expiry_plan(rows, _offload(retention={"RMAN_L0": 7 * DAY, "ARCHIVELOG": 30 * DAY}), now=NOW)
    assert expire == [], "the archivelog keeps its level 0 - and its control file and spfile"
    assert "CONTROLFILE_5: belongs to no full on the repository - kept" in kept

    expire, _kept = O.expiry_plan(rows, _offload(retention={"RMAN_L0": 7 * DAY, "ARCHIVELOG": DAY}), now=NOW)
    assert _names(expire) == ["ARCHIVELOG_4", "CONTROLFILE_2", "RMAN_L0_1", "SPFILE_3"]


def test_two_folders_of_one_server_may_go_to_two_repositories():
    from tests.test_a_restore_from_the_repository_uses_only_what_an_offload_proved import _location

    locations = [_location("/backup/db1/wal/0001", "/r1/S/wal/0001", "R1"),
                 _location("/backup/db2/wal/0001", "/r2/S/wal/0001", "R2")]
    assert O.repository_source(locations, "/backup/db1") == ("R1", "/r1/S")
    assert O.repository_source(locations, r"\backup\db2") == ("R2", "/r2/S"), "a Windows-spelled folder too"


def test_the_catalog_answers_the_copies_of_the_folder_asked_not_the_newest_of_another(catalog, net):
    entry = _entry()
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL_1.bak", b"full")], ended="2026-09-01T01:00:00Z")
    O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                    rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)
    assert [r["offload_path"] for r in catalog.repository_locations(server_id=SRC, under=HOST_DIR)] == [
        "/repository/A1A-SRC-01/APPDB/FULL/APPDB_FULL_1.bak"]
    assert catalog.repository_locations(server_id=SRC, under="/elsewhere") == []


# --------------------------------------------------------------------------- #
# An expired copy is deleted one verified file per item (0.37.0) - plain, a base backup directory,
# or its .age files
# --------------------------------------------------------------------------- #
OLD, NEW = "2026-09-01T01:00:00Z", "2026-10-30T01:00:00Z"
FENCE = "/repository/A1A-SRC-01"


class _Copies:
    """A catalog of an entry's repository copies: what expiry_plan reads and prune_repository writes."""

    def __init__(self, rows, proofs=None):
        self.rows, self.proofs, self.expired = rows, proofs or {}, []

    def repository_copies(self, **_k):
        return [dict(r) for r in self.rows]

    def server_repository_copies(self, **_k):
        return [dict(r) for r in self.rows]

    def repository_proofs(self, **_k):
        return dict(self.proofs)

    def set_backup_file(self, file_id, **values):
        assert values["offload_state"] == "expired" and values["offload_expired_at"]
        self.expired.append(file_id)


def _plain(file_id, name, data, ended, file_type="FULL", folder="APPDB/FULL"):
    return {"file_id": file_id, "backup_id": "SRC01_MSSQL", "file_type": file_type, "database_name": "APPDB",
            "file_name": name,
            "backup_ended_at": ended, "chain_start": ended, "offload_path": f"{FENCE}/{folder}/{name}",
            "size_bytes": len(data), "checksum_after_backup": _sha(data)}


def _prune(net, catalog, *, encrypted=frozenset(), sealed=None, retention=None):
    entry = _entry(offload=_offload(retention=retention or {"FULL": DAY, "FULL_BASE": DAY}))
    return O.prune_repository(entry, catalog=catalog, hosts=_hosts(entry), now=NOW, encrypted=encrypted,
                              sealed=sealed)


def test_a_copy_of_other_bytes_is_not_deleted_and_never_stops_the_next(net):
    first, second = _plain(1, "A_FULL_1.bak", b"one", OLD), _plain(2, "A_FULL_2.bak", b"two", OLD)
    newest = _plain(3, "A_FULL_3.bak", b"three", NEW)
    for row, data in ((first, b"ONE"), (second, b"two"), (newest, b"three")):
        net.repo[row["offload_path"]] = data          # the first copy changed since it was proven
    catalog = _Copies([first, second, newest])

    answer = _prune(net, catalog)

    assert catalog.expired == [2] and answer["expired"] == 1
    assert len(answer["refused"]) == 1 and "A_FULL_1.bak" in answer["refused"][0]
    assert first["offload_path"] in net.repo, "a file that is not the one proven is never deleted"
    assert second["offload_path"] not in net.repo and newest["offload_path"] in net.repo


def test_a_copy_already_gone_from_the_repository_expires(net):
    catalog = _Copies([_plain(1, "A_FULL_1.bak", b"one", OLD), _plain(3, "A_FULL_3.bak", b"three", NEW)])
    answer = _prune(net, catalog)
    assert catalog.expired == [1] and answer["refused"] == []


@pytest.mark.parametrize("change", ["no sha256", "short sha256", "no size", "size as text"])
def test_a_copy_without_every_fact_is_refused_before_the_repository_is_reached(net, change):
    row = _plain(1, "A_FULL_1.bak", b"one", OLD)
    row.update({"no sha256": {"checksum_after_backup": None}, "short sha256": {"checksum_after_backup": "ab"},
                "no size": {"size_bytes": None}, "size as text": {"size_bytes": "3"}}[change])
    net.repo[row["offload_path"]] = b"one"
    catalog = _Copies([row, _plain(3, "A_FULL_3.bak", b"three", NEW)])

    answer = _prune(net, catalog)

    assert catalog.expired == [] and "nothing deleted" in answer["refused"][0]
    assert "delete-verified-files" not in net.calls and row["offload_path"] in net.repo


def _base(file_id, stamp, ended):
    folder = f"{FENCE}/cluster/base/{stamp}_FULL"
    members = {f"{folder}/base.tar.zst": b"base " + stamp.encode(), f"{folder}/pg_wal.tar": b"wal " + stamp.encode(),
               f"{folder}/backup_manifest": b"manifest " + stamp.encode()}
    row = {"file_id": file_id, "backup_id": "SRC01_MSSQL", "file_type": "FULL_BASE", "database_name": "cluster",
           "file_name": "backup_manifest",
           "backup_ended_at": ended, "chain_start": ended, "offload_path": folder,
           "size_bytes": None, "checksum_after_backup": _sha(members[f"{folder}/backup_manifest"])}
    proofs = {path: {"file_id": file_id, "size_bytes": len(data), "checksum_after_backup": _sha(data)}
              for path, data in members.items()}
    return row, members, proofs


def test_a_base_backup_directory_goes_file_by_file_by_its_proofs_and_then_its_folder(net):
    old, old_files, old_proofs = _base(1, "20260901T000000Z", OLD)
    new, new_files, new_proofs = _base(2, "20261030T000000Z", NEW)
    net.repo.update({**old_files, **new_files})
    catalog = _Copies([old, new], proofs={**old_proofs, **new_proofs})

    answer = _prune(net, catalog)

    assert catalog.expired == [1] and answer["refused"] == []
    assert sorted(net.repo) == sorted(new_files), "the newest base backup stays whole"
    assert sorted(r["path"] for r in net.delete_requests) == sorted(old_files)
    assert net.deleted[-1] == old["offload_path"] + "/", "its emptied folder goes last"


def test_a_stray_left_in_a_base_backup_folder_keeps_the_folder_and_says_so(net):
    old, old_files, old_proofs = _base(1, "20260901T000000Z", OLD)
    new, new_files, new_proofs = _base(2, "20261030T000000Z", NEW)
    stray = old["offload_path"] + "/copied_by_hand.txt"
    net.repo.update({**old_files, **new_files, stray: b"x"})
    catalog = _Copies([old, new], proofs={**old_proofs, **new_proofs})

    answer = _prune(net, catalog)

    assert catalog.expired == [1], "every proven file of it is gone"
    assert stray in net.repo and "its folder is not" in answer["refused"][0]


def test_a_base_backup_with_no_proof_of_its_files_deletes_nothing(net):
    old, old_files, _ = _base(1, "20260901T000000Z", OLD)
    new, new_files, new_proofs = _base(2, "20261030T000000Z", NEW)
    net.repo.update({**old_files, **new_files})
    answer = _prune(net, _Copies([old, new], proofs=new_proofs))
    assert "no proof of its files" in answer["refused"][0] and all(p in net.repo for p in old_files)


def _age(file_id, path, data):
    return {"file_id": file_id, "encrypted_path": path, "encrypted_size_bytes": len(data),
            "encrypted_sha256": _sha(data)}


def test_an_encrypted_copy_goes_by_its_age_files_facts(net):
    old, new = _plain(1, "A_FULL_1.bak", b"one", OLD), _plain(3, "A_FULL_3.bak", b"three", NEW)
    sealed_bytes = b"age-encryption.org/v1 one"
    net.repo[old["offload_path"] + ".age"] = sealed_bytes            # the plain file is gone already
    net.repo[new["offload_path"]] = b"three"
    catalog = _Copies([old, new])

    answer = _prune(net, catalog, encrypted={1}, sealed={1: [_age(1, old["offload_path"] + ".age", sealed_bytes)]})

    assert catalog.expired == [1] and answer["refused"] == []
    assert [(r["path"], r["size_bytes"], r["sha256"]) for r in net.delete_requests] == [
        (old["offload_path"] + ".age", len(sealed_bytes), _sha(sealed_bytes))]
    assert sorted(net.repo) == [new["offload_path"]]


def test_an_age_file_of_other_bytes_is_kept_and_its_copy_does_not_expire(net):
    old, new = _plain(1, "A_FULL_1.bak", b"one", OLD), _plain(3, "A_FULL_3.bak", b"three", NEW)
    net.repo[old["offload_path"] + ".age"] = b"changed on the repository"
    catalog = _Copies([old, new])

    answer = _prune(net, catalog, encrypted={1}, sealed={1: [_age(1, old["offload_path"] + ".age", b"as made")]})

    assert catalog.expired == [] and "A_FULL_1.bak" in answer["refused"][0]
    assert old["offload_path"] + ".age" in net.repo


def test_a_copy_encrypted_in_part_is_kept_until_the_encryption_finishes(net):
    old, new = _plain(1, "A_FULL_1.bak", b"one", OLD), _plain(3, "A_FULL_3.bak", b"three", NEW)
    net.repo[old["offload_path"]] = b"one"
    catalog = _Copies([old, new])

    answer = _prune(net, catalog, encrypted={1}, sealed={})

    assert catalog.expired == [] and net.delete_requests == []
    assert any("encrypted in part" in line for line in answer["kept"])


def test_a_type_the_retention_does_not_name_is_kept_for_good(net):
    old_log = _plain(2, "A_LOG_1.trn", b"log", "2026-09-02T00:00:00Z", file_type="LOG", folder="APPDB/LOG")
    rows = [_plain(1, "A_FULL_1.bak", b"one", OLD), old_log, _plain(3, "A_FULL_3.bak", b"three", NEW)]
    expire, _kept = O.expiry_plan(rows, _offload(retention={"FULL": DAY}), now=NOW)
    assert [r["file_name"] for r in expire] == [], "the old full waits for its log, which is kept for good"
    expire, _kept = O.expiry_plan(rows, _offload(retention={"FULL": DAY, "default": DAY}), now=NOW)
    assert sorted(r["file_name"] for r in expire) == ["A_FULL_1.bak", "A_LOG_1.trn"], "default covers LOG"


# --------------------------------------------------------------------------- #
# The chains are the server's, across its entries (2026-11-11: FULL 60 / DIFF 30 / LOG 14 days, each
# level its own entry - read alone, the LOG and DIFF entries had no full and expired nothing)
# --------------------------------------------------------------------------- #
def _of(backup_id, row):
    return {**row, "backup_id": backup_id}


def test_a_log_entry_beside_its_servers_full_entry_expires_its_own_old_logs(net):
    old_full = _of("SRV_FULL", _plain(1, "A_FULL_1.bak", b"one", OLD))
    old_log = _of("SRV_LOG", _plain(2, "A_LOG_1.trn", b"log", "2026-09-02T00:00:00Z", file_type="LOG",
                                    folder="APPDB/LOG"))
    new_full = _of("SRV_FULL", _plain(3, "A_FULL_3.bak", b"three", NEW))
    new_log = _of("SRV_LOG", _plain(4, "A_LOG_4.trn", b"new", "2026-10-30T02:00:00Z", file_type="LOG",
                                    folder="APPDB/LOG"))
    for row, data in ((old_full, b"one"), (old_log, b"log"), (new_full, b"three"), (new_log, b"new")):
        net.repo[row["offload_path"]] = data
    rows = [old_full, old_log, new_full, new_log]

    class _Server(_Copies):
        def repository_copies(self, *, backup_id):
            return [dict(r) for r in self.rows if r["backup_id"] == backup_id]

    catalog = _Server(rows)
    log_entry = _entry(backup_id="SRV_LOG", offload=_offload(retention={"FULL": 60 * DAY, "DIFF": 30 * DAY,
                                                                        "LOG": 14 * DAY}))
    answer = O.prune_repository(log_entry, catalog=catalog, hosts=_hosts(log_entry), now=NOW)

    assert catalog.expired == [2] and answer["expired"] == 1, "its own old log, by the server's chains"
    assert old_full["offload_path"] in net.repo, "the FULL entry's copy is the FULL entry's to expire"
    assert new_log["offload_path"] in net.repo, "the newest chain stays"


def test_a_full_waits_for_the_logs_another_entry_holds_on_it(net):
    old_full = _of("SRV_FULL", _plain(1, "A_FULL_1.bak", b"one", OLD))
    young_log = _of("SRV_LOG", _plain(2, "A_LOG_2.trn", b"log", "2026-10-20T00:00:00Z", file_type="LOG",
                                      folder="APPDB/LOG"))
    new_full = _of("SRV_FULL", _plain(3, "A_FULL_3.bak", b"three", NEW))
    net.repo.update({old_full["offload_path"]: b"one", young_log["offload_path"]: b"log",
                     new_full["offload_path"]: b"three"})

    class _Server(_Copies):
        def repository_copies(self, *, backup_id):
            return [dict(r) for r in self.rows if r["backup_id"] == backup_id]

    catalog = _Server([old_full, young_log, new_full])
    full_entry = _entry(backup_id="SRV_FULL", offload=_offload(retention={"FULL": DAY, "LOG": 14 * DAY}))
    answer = O.prune_repository(full_entry, catalog=catalog, hosts=_hosts(full_entry), now=NOW)

    assert answer["expired"] == 0 and catalog.expired == [], "a log on it is younger than 14 days"
