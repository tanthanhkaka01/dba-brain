"""The repository keeps each copy by its file type's retention, from the entry's config (0.34.0).

Why it matters: the operator, 2026-10-31 - *each file type's retention in the repository comes from
config; every config complete and usable, dynamic, nothing hard-coded*. A repository that keeps
everything fills its disk and then refuses tomorrow's copy; one that prunes by age alone deletes the
full that last week's logs need, or the certificate every encrypted backup needs, and the copies
left behind restore nothing. So the rules held here, all from ``offload_retention_full`` / ``_diff`` /
``_log`` (seconds, 0 for good):

* a copy goes by its own level's age;
* never the newest chain of a database - from the start of its newest full's run;
* a full never while a copy that depends on it is kept;
* a certificate never.

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
def test_nothing_expires_while_every_level_keeps_for_good():
    rows = [_copy(1, "FULL", "2025-01-01T00:00:00Z"), _copy(2, "LOG", "2025-01-01T01:00:00Z")]
    assert O.expiry_plan(rows, _offload(), now=NOW) == ([], [])


def test_each_copy_goes_by_its_own_levels_age():
    rows = [_copy(1, "FULL", "2026-10-01T00:00:00Z"), _copy(2, "LOG", "2026-10-02T00:00:00Z"),
            _copy(3, "LOG", "2026-10-20T00:00:00Z"),
            _copy(4, "FULL", "2026-10-25T02:00:00Z"), _copy(5, "LOG", "2026-10-30T00:00:00Z")]
    expire, _kept = O.expiry_plan(rows, _offload(retention_log=7 * DAY), now=NOW)
    assert _names(expire) == ["LOG_2", "LOG_3"], "logs older than 7 days, outside the newest chain"


def test_the_newest_chain_is_kept_however_old_it_is():
    rows = [_copy(1, "FULL", "2025-01-01T00:00:00Z"), _copy(2, "LOG", "2025-01-02T00:00:00Z")]
    expire, _kept = O.expiry_plan(rows, _offload(retention_full=DAY, retention_log=DAY), now=NOW)
    assert expire == [], "the only chain is the newest - the repository can still restore its latest point"


def test_a_log_written_while_the_newest_full_ran_belongs_to_it():
    rows = [_copy(1, "FULL", "2026-09-01T00:00:00Z"),
            _copy(2, "FULL", "2026-10-01T04:00:00Z", chain_start="2026-10-01T02:00:00Z"),
            _copy(3, "LOG", "2026-10-01T03:00:00Z")]
    expire, _kept = O.expiry_plan(rows, _offload(retention_full=DAY, retention_log=DAY), now=NOW)
    assert _names(expire) == ["FULL_1"]


def test_a_full_past_its_retention_stays_while_a_copy_that_needs_it_is_kept():
    rows = [_copy(1, "FULL", "2026-09-01T00:00:00Z"), _copy(2, "DIFF", "2026-10-28T00:00:00Z"),
            _copy(3, "FULL", "2026-10-30T00:00:00Z")]
    offload = _offload(retention_full=7 * DAY, retention_diff=30 * DAY)
    expire, kept = O.expiry_plan(rows, offload, now=NOW)
    assert expire == [] and kept == ["FULL_1: past its retention, kept - a copy that depends on it is not"]

    expire, _kept = O.expiry_plan(rows, _offload(retention_full=7 * DAY, retention_diff=DAY), now=NOW)
    assert _names(expire) == ["DIFF_2", "FULL_1"], "once its diff goes, the full may go too"


def test_a_certificate_is_never_expired_on_the_repository():
    rows = [_copy(1, "CERT", "2025-01-01T00:00:00Z", name="backup_cert.cer"),
            _copy(2, "FULL", "2025-01-01T00:00:00Z"), _copy(3, "FULL", "2026-10-30T00:00:00Z")]
    expire, kept = O.expiry_plan(rows, _offload(retention_full=DAY), now=NOW)
    assert _names(expire) == ["FULL_2"]
    assert kept == ["backup_cert.cer: a certificate - never expired on the repository"]


def test_each_database_is_its_own_chain():
    rows = [_copy(1, "FULL", "2026-09-01T00:00:00Z", database="HR"),
            _copy(2, "FULL", "2026-09-01T00:00:00Z", database="APP"),
            _copy(3, "FULL", "2026-10-30T00:00:00Z", database="APP")]
    expire, _kept = O.expiry_plan(rows, _offload(retention_full=DAY), now=NOW)
    assert _names(expire) == ["FULL_2"], "HR's only full is its newest chain"


# --------------------------------------------------------------------------- #
# The pass on the repository
# --------------------------------------------------------------------------- #
@pytest.fixture
def net(monkeypatch):
    fake = _Net()
    monkeypatch.setattr(O.common_cli, "run", fake.run)
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
    entry = _entry(offload=_offload(retention_full=7 * DAY, retention_log=7 * DAY))
    _offloaded(catalog, net, entry)

    answer = O.prune_repository(entry, catalog=catalog, hosts=_hosts(entry), now=NOW)

    assert answer["expired"] == 2 and answer["refused"] == []
    assert sorted(net.repo) == ["/repository/A1A-SRC-01/APPDB/FULL/APPDB_FULL_2.bak",
                                "/repository/A1A-SRC-01/APPDB/LOG/APPDB_LOG_2.trn"]
    assert net.fences == ["/repository/A1A-SRC-01"]
    rows = _rows(catalog)
    assert rows["APPDB_FULL_1.bak"]["offload_state"] == "expired" and rows["APPDB_FULL_1.bak"]["offload_expired_at"]
    assert rows["APPDB_FULL_2.bak"]["offload_state"] == "verified"
    assert f"{HOST_DIR}/APPDB/FULL/APPDB_FULL_1.bak" in net.source, "the server's copy is not the repository's to touch"
    assert catalog.files_to_offload(backup_id=entry.backup_id) == [], "an expired copy is not copied again"


def test_a_released_file_whose_copy_expires_is_gone_and_the_store_accepts_it(catalog, net):
    entry = _entry(offload=_offload(delete_source=True, retention_full=7 * DAY, retention_log=7 * DAY))
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
    entry = _entry(offload=_offload(retention_log=365 * DAY))
    _offloaded(catalog, net, entry)
    assert O._expirable(entry, catalog) == []
    assert O._expirable(_entry(offload=_offload()), catalog) == [], "all levels 0: not even read"


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
                                    copy_retry_interval_seconds=7))
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
    expire, _kept = O.expiry_plan(rows, _offload(retention_full=7 * DAY, retention_log=30 * DAY), now=NOW)
    assert expire == [], "no piece of a full goes while the log after it stays"

    expire, _kept = O.expiry_plan(rows, _offload(retention_full=7 * DAY, retention_log=DAY), now=NOW)
    assert _names(expire) == ["APP_FULL_1_of_2.bak", "APP_FULL_2_of_2.bak", "LOG_3"]


def test_a_database_with_no_full_on_the_repository_expires_nothing():
    rows = [_copy(1, "LOG", "2025-01-01T00:00:00Z"), _copy(2, "DIFF", "2025-01-02T00:00:00Z")]
    expire, kept = O.expiry_plan(rows, _offload(retention_diff=DAY, retention_log=DAY), now=NOW)
    assert expire == [] and kept == ["APPDB: no full on the repository - nothing of it expires"]


def test_a_control_file_and_spfile_travel_with_the_full_of_their_run():
    start = "2026-09-01T00:00:00Z"
    rows = [_copy(1, "RMAN_L0", "2026-09-01T01:00:00Z", chain_start=start),
            _copy(2, "CONTROLFILE", "2026-09-01T01:05:00Z", chain_start=start),
            _copy(3, "SPFILE", "2026-09-01T01:05:00Z", chain_start=start),
            _copy(4, "ARCHIVELOG", "2026-10-28T00:00:00Z"),
            _copy(5, "CONTROLFILE", "2026-08-01T00:00:00Z", chain_start="2026-08-01T00:00:00Z"),
            _copy(6, "RMAN_L0", "2026-10-30T00:00:00Z")]
    expire, kept = O.expiry_plan(rows, _offload(retention_full=7 * DAY, retention_log=30 * DAY), now=NOW)
    assert expire == [], "the archivelog keeps its level 0 - and its control file and spfile"
    assert "CONTROLFILE_5: belongs to no full on the repository - kept" in kept

    expire, _kept = O.expiry_plan(rows, _offload(retention_full=7 * DAY, retention_log=DAY), now=NOW)
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
