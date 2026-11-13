"""A restore from the repository reads only the copies an offload proved (0.34.0, restore_from).

Why it matters: the repository exists for the day the database server is gone - encrypted, wiped,
or simply not trusted any more. A restore that reads from it must be held to the same proof as one
that reads from the server: every file the restore uses is the bytes an offload proved on the
repository, at the checksum and size that offload recorded. Anything else lying there - a copy cut
off by a crash, a file somebody dropped in, a copy that changed since - is refused, and nothing is
restored from it.

The folder on the repository is found from the catalog (the copies keep the server's own layout),
not from any entry; a server whose copies are on two repositories, or none, is refused by name. And
the entries the repository cannot serve yet - a share-driven SQL Server restore, Oracle, a restore
in place that copies nothing - say so instead of quietly reading the server.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import offload as O
from dbabrain.backup_restore import restore_script, workflow
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import integrity as I
from dbabrain.lib.offload import OffloadConfigError
from tests.test_a_restore_copy_is_held_to_the_checksum import _Hosts, _script_job


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _offloaded(catalog, files: dict[str, bytes], *, server="LAB-A", repository="REPO", state="verified"):
    """Each of ``files`` (its path on the repository) written by a backup of ``server`` and proven onto the
    repository by one offload run - as offload_entry leaves the catalog."""
    run = catalog.start_restore_run(action="offload", restore_id="LAB_A_PG", source_server_id=server,
                                    target_server_id=repository, copy_max_retry=3, started_at="2026-10-31T00:00:00.1Z")
    for repo_path, body in files.items():
        name = repo_path.rsplit("/", 1)[-1]
        server_path = repo_path.replace("/b/A/", "/srv/pg/backup/")
        file_id = catalog.record_backup_file(
            server_id=server, backup_id="LAB_A_PG", engine="postgresql", file_name=name, file_type="WAL",
            file_path=server_path, host_path=server_path, verdict="ok", size_bytes=len(body),
            checksum_after_backup=sha(body), backup_ended_at="2026-10-30T00:00:00Z")
        catalog.add_run_file(restore_run_id=run, file_id=file_id, source_server_id=server, target_server_id=repository,
                             file_name=name, file_type="WAL", source_path=server_path, target_path=repo_path,
                             checksum_after_backup=sha(body), checksum_source_before_copy=sha(body),
                             checksum_target_after_copy=sha(body), size_source_bytes=len(body),
                             size_target_bytes=len(body), copy_max_retry=3, verdict="copied", copy_attempts=1)
        catalog.set_backup_file(file_id, offload_state="verified", offload_server_id=repository, offload_path=repo_path,
                                offload_run_id=run, offload_verified_at="2026-10-31T00:00:01Z")
        if state != "verified":
            catalog.set_backup_file(file_id, offload_state=state, offload_expired_at="2026-10-31T01:00:00Z")
    return run


def _restore(monkeypatch, hosts, catalog):
    monkeypatch.setattr(restore_script.common_cli, "run", hosts.run)
    proofs = catalog.repository_proofs(server_id="LAB-A", repository_server_id="REPO")
    request = {"source": {"host": "192.0.2.49"}, "source_dir": "/b/A", "target": {"host": "192.0.2.50"},
               "include": ["wal/"]}
    return restore_script._copy_gated_between_hosts(
        _script_job(), request, source=SimpleNamespace(server_id="REPO"), target=None, catalog=catalog, proofs=proofs)


# --------------------------------------------------------------------------- #
# Held to the offload's proof
# --------------------------------------------------------------------------- #
def test_every_file_restored_from_the_repository_is_held_to_what_its_offload_proved(monkeypatch, catalog):
    _offloaded(catalog, {"/b/A/wal/0001": b"wal one", "/b/A/wal/0002": b"wal two"})
    hosts = _Hosts({"/b/A/wal/0001": b"wal one", "/b/A/wal/0002": b"wal two"})

    out = _restore(monkeypatch, hosts, catalog)

    assert out["integrity"]["proven"] is True
    assert hosts.files["192.0.2.50"]["/b/B/wal/0002"] == b"wal two"
    (copy,) = hosts.named("copy-files")
    assert [(line["source"], line["size_bytes"], line["sha256"]) for line in copy["files"]] == [
        ("/b/A/wal/0001", 7, sha(b"wal one")), ("/b/A/wal/0002", 7, sha(b"wal two"))], \
        "each file leaves the repository stated as the bytes its offload proved"
    rows = {r["file_name"]: r for r in catalog.run_files(out["restore_run_id"])}
    assert rows["0001"]["source_server_id"] == "REPO", "the bytes came from the repository"
    assert rows["0001"]["checksum_after_backup"] == sha(b"wal one") and rows["0001"]["file_id"]
    marked = {r["file_name"]: r for r in catalog.server_backup_files("LAB-A")}
    assert marked["0001"]["last_restore_run_id"] == out["restore_run_id"], "the backup file is marked as used"


def test_a_file_on_the_repository_no_offload_proved_is_refused_and_nothing_is_restored(monkeypatch, catalog):
    _offloaded(catalog, {"/b/A/wal/0001": b"wal one"})
    hosts = _Hosts({"/b/A/wal/0001": b"wal one", "/b/A/wal/0002": b"dropped in by somebody"})

    with pytest.raises(restore_script.CopyNotProvenError, match="not proven"):
        _restore(monkeypatch, hosts, catalog)


def test_a_file_offloaded_after_the_plan_stays_on_the_repository_for_the_next_run(monkeypatch, catalog):
    """The staging is read again for each round, so it can name a file the plan did not hold - an
    offload that landed in between. This run's gate proves the plan's files, and that one is not
    among them: its offload's proof or not, staged now it would be a file the run never held to
    anything. It stays on the repository, and the answer names it."""
    _offloaded(catalog, {"/b/A/wal/0001": b"wal one", "/b/A/wal/0002": b"wal two"})
    hosts = _Hosts({"/b/A/wal/0001": b"wal one"}, late={"/b/A/wal/0002": b"wal two"})

    out = _restore(monkeypatch, hosts, catalog)

    (copy,) = hosts.named("copy-files")
    assert [(line["file_name"], line["size_bytes"], line["sha256"]) for line in copy["files"]] == [
        ("0001", 7, sha(b"wal one"))]
    assert out["written_after_the_plan"] == ["wal/0002"]
    assert [request["paths"] for request in hosts.named("file-checksum")
            if request["host"]["host"] == "192.0.2.49"] == [["/b/A/wal/0001"]], "the gate's own hash, and no other"


def test_a_copy_cut_off_on_the_repository_is_never_even_selected():
    from dbabrain.common.backup_copy import _selected

    files, _dirs = _selected([("wal/0001", 3), ("wal/0002.dbabrain_partial", 1)], [], {},
                             include=("wal/",), window_since=None)
    assert files == [("wal/0001", 3)]
    files, _dirs = _selected([("x.bak.dbabrain_partial", 1)], [], {}, include=(), window_since=None)
    assert files == [], "with no include either: a partial is never a backup"


def test_a_repository_copy_that_changed_since_its_offload_is_refused(monkeypatch, catalog):
    _offloaded(catalog, {"/b/A/wal/0001": b"wal one"})
    hosts = _Hosts({"/b/A/wal/0001": b"encrypted by somebody"})

    with pytest.raises(restore_script.CopyNotProvenError, match="the backup changed after it was written"):
        _restore(monkeypatch, hosts, catalog)


def test_an_expired_copy_proves_nothing(catalog):
    _offloaded(catalog, {"/b/A/wal/0001": b"wal one"}, state="expired")
    assert catalog.repository_proofs(server_id="LAB-A", repository_server_id="REPO") == {}


# --------------------------------------------------------------------------- #
# The folder on the repository, from the catalog
# --------------------------------------------------------------------------- #
def _location(host_path, offload_path, server="REPO"):
    return {"offload_server_id": server, "host_path": host_path, "file_path": host_path, "offload_path": offload_path}


def test_the_servers_folder_is_found_on_the_repository_through_the_layout_both_keep():
    locations = [_location("/srv/pg/backup/wal/0001", "/repository/LAB-A/wal/0001")]
    assert O.repository_source(locations, "/srv/pg/backup") == ("REPO", "/repository/LAB-A")
    assert O.repository_source(locations, "/srv/pg/backup/wal") == ("REPO", "/repository/LAB-A/wal")


@pytest.mark.parametrize("locations, words", [
    ([], "no verified repository copy of"),
    ([_location("/srv/pg/backup/x", "/r/A/x", "R1"), _location("/srv/pg/backup/y", "/r/A/y", "R2")], "in 2 places"),
    ([_location("/elsewhere/x/0001", "/r/A/x/0001")], "no verified repository copy lies under"),
])
def test_a_folder_the_repository_cannot_stand_for_is_refused_by_name(locations, words):
    with pytest.raises(O.OffloadRefused, match=words):
        O.repository_source(locations, "/srv/pg/backup")


def test_the_transfer_reads_the_repository_host_and_folder_not_the_servers(monkeypatch, catalog):
    _offloaded(catalog, {"/b/A/wal/0001": b"wal one"})
    seen = {}
    monkeypatch.setattr(restore_script, "_ssh_login", lambda target, **_k: {"host": target.host})
    monkeypatch.setattr(restore_script, "prune_staged_backups", lambda *a, **k: {"pruned": 0})
    monkeypatch.setattr("dbabrain.backup_restore.backup.resolve_ssh_target",
                        lambda server_id, **_k: SimpleNamespace(server_id=server_id, host="repo-host", container_name=""))
    monkeypatch.setattr(catalog, "repository_locations",
                        lambda **_k: [_location("/srv/pg/backup/wal/0001", "/b/A/wal/0001")])

    def gated(job, request, *, source, target, catalog, proofs, plan):
        seen.update(source=source.server_id, source_dir=request["source_dir"], login=request["source"],
                    proofs=sorted(proofs), include=request["include"], plan=plan)
        return {"copied": 1, "skipped": 0, "bytes_copied": 7}

    def run(command, request, **_k):
        # The one command the transfer sends itself: the plan, which reads the chain with it.
        seen.setdefault("asked", []).append((command, request))
        return {"include": ["wal/"], "files": [{"path": "wal/0001", "size_bytes": 7}]}

    monkeypatch.setattr(restore_script, "_copy_gated_between_hosts", gated)
    monkeypatch.setattr(restore_script.common_cli, "run", run)
    job = SimpleNamespace(**{**vars(_script_job()), "source_backup_host_dir": "/srv/pg/backup",
                             "restore_from": "repository", "copy_selection": "chain",
                             "copy_recent_hours": 24, "backup_dir": "/backup", "copy_mode": "auto",
                             "space_check": None, "staging_marker": "", "cleanup_retention": 0})
    monkeypatch.setattr(restore_script.restore_space, "as_request", lambda _s: None)

    out = restore_script.transfer_backup_to_target(job, source=SimpleNamespace(host="db-host", container_name="pg"),
                                                   target=SimpleNamespace(host="lab-b"), catalog=catalog, prune=False)

    assert seen["source"] == "REPO" and seen["source_dir"] == "/b/A" and seen["login"] == {"host": "repo-host"}
    ((command, plan),) = seen["asked"]
    assert command == "prepare-staging" and plan["plan_only"] is True
    assert plan["source_dir"] == "/b/A" and plan["source"] == {"host": "repo-host"}, \
        "the chain is read on the repository too"
    assert plan["chain"]["db_type"] == "postgresql"
    assert seen["include"] == ["wal/"], "and stated from then on"
    assert seen["plan"]["files"] == [{"path": "wal/0001", "size_bytes": 7}], \
        "the plan is handed on - the repository is not walked a second time for it"
    assert seen["proofs"] == ["/b/A/wal/0001"]
    assert out["restore_from"] == "repository" and out["repository_server_id"] == "REPO"


# --------------------------------------------------------------------------- #
# The entries the repository does not serve, refused by name
# --------------------------------------------------------------------------- #
def _script_entry(**overrides):
    from dbabrain.backup_restore.restore_script import ScriptRestore
    from dbabrain.lib.time_window import TimeWindow

    values = dict(restore_id="R", db_type="postgresql", server_id="LAB-A", backup_dir="/backup", script="x.sh",
                  time_window=TimeWindow(), target_server_id="LAB-B", restore_from="repository", action="restore",
                  integrity=I.RestoreIntegrity())
    values.update(overrides)
    return ScriptRestore(**values)


def test_a_script_restore_onto_another_server_may_read_the_repository():
    action, error = workflow._entry_action(_script_entry(), copy_only=False)
    assert error is None and action == "restore"


@pytest.mark.parametrize("entry, words", [
    (SimpleNamespace(restore_id="S", restore_from="repository", action="restore", db_type=None), "share-driven SQL Server"),
    ("oracle", "this entry is oracle"),
    ("in place", "an in-place entry copies nothing"),
])
def test_what_the_repository_cannot_serve_is_refused_by_name(entry, words):
    if entry == "oracle":
        entry = _script_entry(db_type="oracle")
    elif entry == "in place":
        entry = _script_entry(target_server_id="")
    action, error = workflow._entry_action(entry, copy_only=False)
    assert action is None and isinstance(error, OffloadConfigError) and words in str(error)


def test_a_file_in_no_containers_mount_is_held_to_its_backup_by_its_own_path(monkeypatch, catalog):
    """1.213b read the stored row's ``path`` - a FILE line's word, not the catalog's column - so a file
    with no host_path (no container) was never found, and a restore that refuses uncatalogued files
    refused it (the second review of 2026-10-31)."""
    catalog.record_backup_file(server_id="LAB-A", backup_id="LAB_A_PG", engine="postgresql", file_name="0001",
                               file_type="WAL", file_path="/b/A/wal/0001", host_path="", verdict="ok", size_bytes=7,
                               checksum_after_backup=sha(b"wal one"))
    hosts = _Hosts({"/b/A/wal/0001": b"wal one"})
    monkeypatch.setattr(restore_script.common_cli, "run", hosts.run)
    job = _script_job()
    job.integrity = I.RestoreIntegrity(copy_retry_interval_seconds=0, uncatalogued="refuse")

    out = restore_script._copy_gated_between_hosts(
        job, {"source": {"host": "192.0.2.49"}, "target": {"host": "192.0.2.50"}, "include": ["wal/"]},
        source=None, target=None, catalog=catalog)
    assert out["integrity"]["proven"] is True
