"""A restore's copy is held to the checksum on the Linux paths (0.34.0, 1.213), and on a Windows
node's (1.213b).

Why it matters: until now the worker's copy to a restore target compared SIZES - a file the target
already held at the source's size was never even downloaded, and one that arrived was trusted if its
size matched. A truncated copy shows in a size; a corrupted or tampered one does not. Now every file
goes through the gate built in 1.211: the source hashed as this node read it, the target hashed on
the target before and after, copied again over itself up to ``copy_max_retry``, every step in the
catalog - and a file already on the target is skipped only when its hash IS what its backup recorded.

The gate is batched: on a real host each hash is one ``common.cli file-checksum`` round trip, and a
chain is a thousand files, so the targets are hashed once per round, not once per file.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
from pathlib import Path, PurePosixPath

import pytest

from dbabrain.backup_restore import copy_backup as copy_module
from dbabrain.backup_restore.copy_backup import _Gate, _copy_gated_via_sftp, _smbclient_download_selected_to_staging
from dbabrain.backup_restore.copy_gate import FileToCopy, confirm_on_target, copy_verified_batch, summarize
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import integrity as I

from tests.test_backup_restore import _FakeShare, _share, make_config


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


# --------------------------------------------------------------------------- #
# The batch gate: the same rules per file, one hash call per round
# --------------------------------------------------------------------------- #
def _items(names):
    return [FileToCopy(source_server_id="S", target_server_id="T", file_name=name, file_type="LOG",
                       source_path=f"/s/{name}", target_path=f"/t/{name}") for name in names]


def test_a_batch_hashes_the_targets_once_per_round_and_copies_again_only_what_is_wrong(catalog):
    run = catalog.start_restore_run(action="restore", source_server_id="S", target_server_id="T", copy_max_retry=3)
    source = {"/s/a.trn": b"a", "/s/b.trn": b"b", "/s/d.trn": b"d"}         # c.trn is not on the source
    target = {"/t/d.trn": b"d"}                                               # d.trn is already there
    plan = {"/t/b.trn": ["corrupt", "ok"]}                                    # b arrives wrong once
    calls = {"targets": 0}
    slept: list = []

    def hash_targets(paths):
        calls["targets"] += 1
        return {p: (sha(target[p]), len(target[p])) for p in paths if p in target}

    def copy(item):
        step = (plan.get(item.target_path) or ["ok"]).pop(0) if plan.get(item.target_path) else "ok"
        target[item.target_path] = source[item.source_path] if step == "ok" else b"garbage"
        return 1

    outcomes = copy_verified_batch(
        _items(["a.trn", "b.trn", "c.trn", "d.trn"]), integrity=I.RestoreIntegrity(),
        hash_sources=lambda paths: {p: (sha(source[p]), 1) for p in paths if p in source},
        hash_targets=hash_targets, copy=copy, catalog=catalog, restore_run_id=run, sleep=slept.append)

    assert [o.verdict for o in outcomes] == ["copied", "recopied", "source_missing", "match"]
    # before (a, b, d) + after round 1 (a, b) + after round 2 (b) - never one call per file.
    assert calls["targets"] == 3
    assert slept == [30], "one pause between rounds, not one per file"
    assert summarize(outcomes)["proven"] is False, "c.trn is missing - the run is not proven"
    rows = {r["file_name"]: r for r in catalog.run_files(run)}
    assert [a["result"] for a in catalog.attempts(rows["b.trn"]["run_file_id"])] == ["mismatch", "match"]


def test_a_file_already_on_the_target_is_proven_against_its_backup_without_reading_the_source(catalog):
    run = catalog.start_restore_run(action="restore", source_server_id="S", target_server_id="T", copy_max_retry=3)
    good = sha(b"full")
    items = [dataclasses.replace(_items(["full.bak"])[0], checksum_after_backup=good),
             dataclasses.replace(_items(["changed.bak"])[0], checksum_after_backup=good),
             _items(["uncatalogued.bak"])[0]]
    on_target = {"/t/full.bak": (good, 4), "/t/changed.bak": (sha(b"other"), 5), "/t/uncatalogued.bak": (good, 4)}

    outcomes, remaining = confirm_on_target(items, integrity=I.RestoreIntegrity(),
                                            hash_targets=lambda paths: {p: on_target[p] for p in paths},
                                            catalog=catalog, restore_run_id=run)

    assert outcomes[0].verdict == "match" and outcomes[0].proven
    assert remaining == [1, 2], "a changed copy and a file no backup recorded are copied again"
    row = catalog.run_files(run)[0]
    assert row["checksum_source_before_copy"] is None, "the source was not read - and the store accepts that"
    assert row["checksum_target_after_copy"] == good == row["checksum_after_backup"]


# --------------------------------------------------------------------------- #
# The smbclient pre-skip asks the gate instead of comparing sizes
# --------------------------------------------------------------------------- #
LISTING = (
    "\\APPDB-DB$APPDB\\APPDB_Prod\\FULL\n"
    "  APPDB_Prod_FULL_20260609_010000.bak      A  10  Tue Jun 09 01:00:00 2026\n"
    "\n"
    "\\APPDB-DB$APPDB\\APPDB_Prod\\LOG\n"
    "  APPDB_Prod_LOG_20260609_020000.trn       A  10  Tue Jun 09 02:00:00 2026\n"
)


def _share_config(tmp_path):
    return dataclasses.replace(
        make_config(tmp_path), prod_backup_share=Path(r"\\192.0.2.250\SQLBK\APPDB-DB$APPDB"),
        vm_import_unc=Path("/opt/import/ACME"),
        copy_window_start_utc=datetime.datetime(2026, 6, 7, 5, 0, tzinfo=datetime.timezone.utc),
        copy_window_end_utc=datetime.datetime(2026, 6, 10, 5, 0, tzinfo=datetime.timezone.utc))


@pytest.mark.parametrize("proven, downloaded", [
    (set(), ["FULL", "LOG"]),
    ({"APPDB_Prod/FULL/APPDB_Prod_FULL_20260609_010000.bak"}, ["LOG"]),
])
def test_a_same_size_file_is_skipped_only_when_the_gate_proves_it(tmp_path, monkeypatch, proven, downloaded):
    fake = _share(monkeypatch, _FakeShare(LISTING))
    monkeypatch.setattr(copy_module, "_remote_destination_sizes", lambda config, *, logger=None:
                        {"appdb_prod/full/appdb_prod_full_20260609_010000.bak": 10})
    asked: list = []

    def confirm(candidates):
        asked.extend(candidates)
        return proven

    _staging, pre_skipped, _total = _smbclient_download_selected_to_staging(
        _share_config(tmp_path), logger=None, confirm=confirm)

    assert [rel for rel, _ in asked] == ["APPDB_Prod/FULL/APPDB_Prod_FULL_20260609_010000.bak"]
    assert [g.split("\\")[2] for g in fake.gets()] == downloaded, "the same size alone is no reason to skip"
    assert len(pre_skipped) == len(proven)


# --------------------------------------------------------------------------- #
# The SFTP push through the gate, end to end on a fake target
# --------------------------------------------------------------------------- #
class _Target:
    """The Linux restore target: SFTP that stores bytes, and `file-checksum` that hashes them."""

    def __init__(self, corrupt=()):
        self.files: dict[str, bytes] = {}
        self.corrupt = {path: list(times) for path, times in dict(corrupt).items()}
        self.puts: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, command, sudo=False):
        return type("R", (), {"ok": True, "stdout": "", "stderr": ""})()

    def mkdirs(self, path):
        return None

    def put(self, local, remote):
        self.puts.append(remote)
        final = remote.removesuffix(".dbabrain_partial")
        bad = self.corrupt.get(final)
        self.files[remote] = b"garbage" if bad and bad.pop(0) else Path(local).read_bytes()
        return {}

    def set_mtime(self, path, mtime):
        return None

    def rename(self, source, target):
        self.files[target] = self.files.pop(source)

    def host_block(self):
        return {"runtime": "linux", "host": "192.0.2.40"}

    def checksum(self, command, request, **_kw):
        assert command == "file-checksum"
        return {"files": [{"path": p, "sha256": sha(self.files[p]), "size_bytes": len(self.files[p])}
                          for p in request["paths"] if p in self.files]}


def _staged(tmp_path, names):
    root = tmp_path / "staging"
    for name, body in names.items():
        path = root / "APPDB_Prod" / ("LOG" if name.endswith(".trn") else "FULL") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return root, sorted(p for p in root.rglob("*") if p.is_file())


def _linux_config(tmp_path, *, integrity=True):
    return dataclasses.replace(
        make_config(tmp_path), vm_platform="linux", vm_import_unc=Path("/opt/import/ACME"),
        source_id="ACME-192-0-2-250", target_id="LAB-192-0-2-40", restore_id="ACME_TO_LAB",
        integrity=I.RestoreIntegrity(copy_retry_interval_seconds=0) if integrity else None)


def test_the_sftp_copy_proves_every_file_and_records_it(tmp_path, monkeypatch, catalog):
    target = _Target(corrupt={"/opt/import/ACME/APPDB_Prod/LOG/APPDB_Prod_LOG_1.trn": [True]})
    monkeypatch.setattr(copy_module, "open_ssh_connection", lambda config: target)
    monkeypatch.setattr("dbabrain.transport.common_cli.run", target.checksum)
    root, files = _staged(tmp_path, {"APPDB_Prod_FULL_1.bak": b"full", "APPDB_Prod_LOG_1.trn": b"log"})
    config = _linux_config(tmp_path)
    gate = _Gate(config, catalog, logger=None)

    results = _copy_gated_via_sftp(config, files, root=root, linux_import=PurePosixPath("/opt/import/ACME"),
                                   gate=gate, logger=None, from_share=True)
    summary = gate.finish()

    assert sorted(r.status for r in results) == ["COPIED", "REPLACED"], "the LOG arrived wrong once and was recopied"
    assert all(p.endswith(".dbabrain_partial") for p in target.puts), "always a temporary name, then a rename"
    assert summary["proven"] is True and summary["files"] == 2
    rows = {r["file_name"]: r for r in catalog.run_files(summary["restore_run_id"])}
    assert rows["APPDB_Prod_LOG_1.trn"]["file_type"] == "LOG" and rows["APPDB_Prod_FULL_1.bak"]["file_type"] == "FULL"
    assert rows["APPDB_Prod_LOG_1.trn"]["source_server_id"] == "ACME-192-0-2-250"
    assert rows["APPDB_Prod_LOG_1.trn"]["source_path"].startswith(str(config.prod_backup_share)), \
        "named on the share it came from, not the node's staging folder"
    with catalog.connect() as conn:
        assert conn.execute("SELECT phase FROM restore_runs").fetchone()[0] == "verified"


def test_a_file_that_never_arrives_intact_fails_the_copy_and_refuses_the_run(tmp_path, monkeypatch, catalog):
    target = _Target(corrupt={"/opt/import/ACME/APPDB_Prod/FULL/APPDB_Prod_FULL_1.bak": [True] * 4})
    monkeypatch.setattr(copy_module, "open_ssh_connection", lambda config: target)
    monkeypatch.setattr("dbabrain.transport.common_cli.run", target.checksum)
    root, files = _staged(tmp_path, {"APPDB_Prod_FULL_1.bak": b"full"})
    config = _linux_config(tmp_path)
    gate = _Gate(config, catalog, logger=None)

    results = _copy_gated_via_sftp(config, files, root=root,
                                   linux_import=PurePosixPath("/opt/import/ACME"),
                                   gate=gate, logger=None, from_share=True)
    summary = gate.finish()

    assert [r.status for r in results] == ["FAILED"], "a FAILED copy is what stops the restore"
    assert summary["proven"] is False and "4 attempt(s)" in summary["unproven"][0]
    with catalog.connect() as conn:
        phase, reason = conn.execute("SELECT phase, refused_reason FROM restore_runs").fetchone()
    assert phase == "refused" and "none arrived equal to the source" in reason


def test_an_entry_without_an_integrity_block_is_refused_before_anything_is_copied(tmp_path, catalog):
    with pytest.raises(I.IntegrityConfigError, match="states no integrity block"):
        _Gate(_linux_config(tmp_path, integrity=False), catalog, logger=None)


@pytest.mark.parametrize("relative, file_type", [
    ("APPDB/FULL/APPDB_FULL_1.bak", "FULL"), ("APPDB/DIFF/APPDB_DIFF_1.bak", "DIFF"),
    ("APPDB/LOG/APPDB_LOG_1.trn", "LOG"), ("APPDB_LOG_2.trn", "LOG"), ("_cert/db_ops_backup_cert.cer", "CERT"),
])
def test_a_sql_server_files_type_is_read_from_the_layout_the_scripts_write(relative, file_type):
    assert I.sqlserver_file_type(relative) == file_type


# --------------------------------------------------------------------------- #
# A Windows node's copy (share to share, path to path) through the same gate - 1.213b
# --------------------------------------------------------------------------- #
def _windows_config(tmp_path):
    return dataclasses.replace(
        make_config(tmp_path), source_id="ACME-192-0-2-250", target_id="LAB-192-0-2-41",
        restore_id="ACME_TO_WINLAB", integrity=I.RestoreIntegrity(copy_retry_interval_seconds=0))


def _on_share(config, names):
    files = []
    for name, body in names.items():
        path = Path(config.prod_backup_share) / "APPDB_Prod" / ("LOG" if name.endswith(".trn") else "FULL") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        files.append(path)
    return sorted(files)


def _target_of(config, name):
    return Path(config.vm_import_unc) / "APPDB_Prod" / ("LOG" if name.endswith(".trn") else "FULL") / name


def _corrupting_copy(monkeypatch, bad_times):
    """shutil.copy2 that writes garbage the first ``bad_times[name]`` times a file is copied."""
    real = copy_module.shutil.copy2
    seen: list[str] = []

    def copy2(source, target):
        name = Path(source).name
        seen.append(str(target))
        if bad_times.get(name, 0) > 0:
            bad_times[name] -= 1
            Path(target).write_bytes(b"garbage")
            return target
        return real(source, target)
    monkeypatch.setattr(copy_module.shutil, "copy2", copy2)
    return seen


def test_the_windows_copy_proves_every_file_through_a_temporary_name(tmp_path, monkeypatch, catalog):
    config = _windows_config(tmp_path)
    files = _on_share(config, {"APPDB_Prod_FULL_1.bak": b"full", "APPDB_Prod_LOG_1.trn": b"log"})
    written = _corrupting_copy(monkeypatch, {"APPDB_Prod_LOG_1.trn": 1})
    gate = _Gate(config, catalog, logger=None)

    results = copy_module._copy_gated_locally(config, files, gate=gate, engine=copy_module.ENGINE_SHARE, logger=None)
    summary = gate.finish()

    assert sorted(r.status for r in results) == ["COPIED", "REPLACED"], "the LOG arrived wrong once and was recopied"
    assert all(p.endswith(".dbabrain_partial") for p in written), "always a temporary name, then a rename"
    assert not list(Path(config.vm_import_unc).rglob("*.dbabrain_partial")), "nothing left under a temporary name"
    assert _target_of(config, "APPDB_Prod_LOG_1.trn").read_bytes() == b"log"
    assert summary["proven"] is True and summary["files"] == 2
    rows = {r["file_name"]: r for r in catalog.run_files(summary["restore_run_id"])}
    assert rows["APPDB_Prod_LOG_1.trn"]["file_type"] == "LOG" and rows["APPDB_Prod_FULL_1.bak"]["file_type"] == "FULL"
    attempts = catalog.attempts(rows["APPDB_Prod_LOG_1.trn"]["run_file_id"])
    assert [a["result"] for a in attempts] == ["mismatch", "match"]
    assert {a["copy_engine"] for a in attempts} == {"share"}, "the attempt names the engine that made it"
    with catalog.connect() as conn:
        assert conn.execute("SELECT phase FROM restore_runs").fetchone()[0] == "verified"


@pytest.mark.parametrize("target_bytes, status, copied", [
    (b"full", "SKIPPED_EXISTS", False),   # the backup's own bytes: proven, never re-read or copied
    (b"FULL", "COPIED", True),            # the same size, other bytes: the size alone proves nothing
])
def test_a_same_size_windows_target_is_skipped_only_when_it_is_the_backups_bytes(
        tmp_path, monkeypatch, catalog, target_bytes, status, copied):
    config = _windows_config(tmp_path)
    files = _on_share(config, {"APPDB_Prod_FULL_1.bak": b"full"})
    target = _target_of(config, "APPDB_Prod_FULL_1.bak")
    target.parent.mkdir(parents=True)
    target.write_bytes(target_bytes)
    monkeypatch.setattr(catalog, "stored_files", lambda **_kw: {
        "APPDB_Prod_FULL_1.bak": {"checksum_after_backup": sha(b"full"), "file_id": None, "host_path": ""}})
    written = _corrupting_copy(monkeypatch, {})
    gate = _Gate(config, catalog, logger=None)

    results = copy_module._copy_gated_locally(config, files, gate=gate, engine=copy_module.ENGINE_SHARE, logger=None)

    assert [r.status for r in results] == [status]
    assert bool(written) is copied
    assert target.read_bytes() == b"full"
    assert gate.finish()["proven"] is True


def test_a_windows_file_that_never_arrives_intact_fails_the_copy_and_refuses_the_run(tmp_path, monkeypatch, catalog):
    config = _windows_config(tmp_path)
    files = _on_share(config, {"APPDB_Prod_FULL_1.bak": b"full"})
    _corrupting_copy(monkeypatch, {"APPDB_Prod_FULL_1.bak": 99})
    gate = _Gate(config, catalog, logger=None)

    results = copy_module._copy_gated_locally(config, files, gate=gate, engine=copy_module.ENGINE_PYTHON, logger=None)
    summary = gate.finish()

    assert [r.status for r in results] == ["FAILED"], "a FAILED copy is what stops the restore"
    assert summary["proven"] is False and "4 attempt(s)" in summary["unproven"][0]
    with catalog.connect() as conn:
        assert conn.execute("SELECT phase FROM restore_runs").fetchone()[0] == "refused"


@pytest.mark.parametrize("engine", [copy_module.ENGINE_SHARE, copy_module.ENGINE_PYTHON])
def test_no_copy_engine_runs_without_the_gate(tmp_path, monkeypatch, engine):
    """``checksum_gate=off`` is gone: an entry without an integrity block is refused on every engine."""
    config = dataclasses.replace(
        make_config(tmp_path), prod_backup_share=Path(r"\\192.0.2.250\SQLBK"),
        vm_import_unc=Path(r"\\192.0.2.41\IMPORT"), integrity=None)
    monkeypatch.setattr(copy_module, "copy_engine", lambda _config: engine)
    monkeypatch.setattr(copy_module, "store_share_logins", lambda _requests: None)
    listed: list = []
    monkeypatch.setattr(copy_module, "list_recent_backup_files_on_share", lambda *a, **k: listed.append(1) or [])
    monkeypatch.setattr(copy_module, "list_recent_backup_files", lambda *a, **k: listed.append(1) or [])

    with pytest.raises(I.IntegrityConfigError, match="states no integrity block"):
        copy_module.run_copy_backup(config)
    assert listed == [], "refused before the share is even listed"


# --------------------------------------------------------------------------- #
# copy-backup-dir (script restores, host to host) through the same gate - 1.213b
# --------------------------------------------------------------------------- #
class _Hosts:
    """Two SSH hosts behind ``common.cli``: ``copy-backup-dir`` (plan or copy, size-and-replace rule)
    and ``file-checksum`` (each host hashes its own files)."""

    def __init__(self, source, target=None, corrupt=None):
        self.files = {"192.0.2.49": dict(source), "192.0.2.50": dict(target or {})}
        self.corrupt = dict(corrupt or {})
        self.copies: list[dict] = []

    def run(self, command, request, **_kw):
        if command == "file-checksum":
            held = self.files[request["host"]["host"]]
            return {"files": [{"path": p, "sha256": sha(held[p]), "size_bytes": len(held[p])}
                              for p in request["paths"] if p in held]}
        assert command == "copy-backup-dir"
        src = {p.removeprefix("/b/A/"): body for p, body in self.files["192.0.2.49"].items()}
        if request.get("plan_only"):
            return {"files": [{"path": rel, "size_bytes": len(body)} for rel, body in sorted(src.items())]}
        self.copies.append(request)
        dst, copied = self.files["192.0.2.50"], 0
        for rel, body in src.items():
            path = f"/b/B/{rel}"
            if rel not in request.get("replace", []) and path in dst and len(dst[path]) == len(body):
                continue
            dst[path] = b"x" * len(body) if self.corrupt.get(rel, 0) > 0 else body
            self.corrupt[rel] = self.corrupt.get(rel, 0) - 1
            copied += 1
        return {"copied": copied, "skipped": len(src) - copied, "bytes_copied": 0, "removed_absent_at_source": 0,
                "opened_for_engine": True, "copy_mode": "tar", "copy_fell_back": False}


def _script_job(integrity=True):
    from types import SimpleNamespace

    return SimpleNamespace(
        restore_id="PG_A_TO_B", db_type="postgresql", label="PG_A_TO_B", server_id="LAB-A", target_server_id="LAB-B",
        source_backup_host_dir="/b/A", target_backup_dir="/b/B",
        integrity=I.RestoreIntegrity(copy_retry_interval_seconds=0) if integrity else None)


def _script_copy(monkeypatch, hosts, catalog, job=None):
    from dbabrain.backup_restore import restore_script

    monkeypatch.setattr(restore_script.common_cli, "run", hosts.run)
    request = {"source": {"host": "192.0.2.49"}, "target": {"host": "192.0.2.50"}, "include": ["base/F", "wal/"]}
    return restore_script._copy_gated_between_hosts(job or _script_job(), request, source=None, target=None,
                                                    catalog=catalog)


def test_a_host_to_host_copy_is_proven_and_a_wrong_copy_is_sent_again_by_name(monkeypatch, catalog):
    hosts = _Hosts({"/b/A/base/F/backup_manifest": b"manifest", "/b/A/wal/0001": b"wal"}, corrupt={"wal/0001": 1})

    out = _script_copy(monkeypatch, hosts, catalog)

    assert [c.get("replace") for c in hosts.copies] == [["base/F/backup_manifest", "wal/0001"], ["wal/0001"]], \
        "one copy per round, and the round after names only what arrived wrong - at the same size"
    assert all(c["include"] == ["base/F", "wal/"] for c in hosts.copies), "the request as asked, every round"
    assert hosts.files["192.0.2.50"]["/b/B/wal/0001"] == b"wal"
    assert out["integrity"]["proven"] is True and out["integrity"]["recopied"] == 1
    rows = {r["file_name"]: r for r in catalog.run_files(out["restore_run_id"])}
    assert rows["0001"]["file_type"] == "WAL" and rows["backup_manifest"]["file_type"] == "MANIFEST"
    assert {a["copy_engine"] for a in catalog.attempts(rows["0001"]["run_file_id"])} == {"copy-backup-dir"}
    with catalog.connect() as conn:
        assert conn.execute("SELECT phase FROM restore_runs").fetchone()[0] == "verified"


def test_a_target_already_equal_is_matched_and_the_copy_still_runs_once_for_the_mirror(monkeypatch, catalog):
    body = {"/b/A/wal/0001": b"wal"}
    hosts = _Hosts(body, target={"/b/B/wal/0001": b"wal"})

    out = _script_copy(monkeypatch, hosts, catalog)

    assert out["integrity"]["verdicts"] == {"match": 1}
    assert [c.get("replace") for c in hosts.copies] == [[]], \
        "one call with nothing to replace: the mirror, the marker and the engine's access are the copy's"


def test_a_host_to_host_copy_that_never_arrives_intact_refuses_the_restore(monkeypatch, catalog):
    from dbabrain.backup_restore import restore_script

    hosts = _Hosts({"/b/A/wal/0001": b"wal"}, corrupt={"wal/0001": 99})

    with pytest.raises(restore_script.CopyNotProvenError, match="not proven"):
        _script_copy(monkeypatch, hosts, catalog)
    assert len(hosts.copies) == 4, "copy_max_retry 3: four rounds, then refused"
    with catalog.connect() as conn:
        phase, ended = conn.execute("SELECT phase, ended_at FROM restore_runs").fetchone()
    assert phase == "refused" and ended, "no restore follows, so the refusal closes its own row"


def test_a_host_to_host_copy_without_integrity_is_refused_before_the_source_is_read(monkeypatch, catalog):
    hosts = _Hosts({"/b/A/wal/0001": b"wal"})

    with pytest.raises(I.IntegrityConfigError):
        _script_copy(monkeypatch, hosts, catalog, job=_script_job(integrity=False))
    assert hosts.copies == []


@pytest.mark.parametrize("db_type, relative, file_type", [
    ("postgresql", "base/20260925T004710Z_FULL/base/1/1259", "FULL_BASE"),
    ("postgresql", "base/20260925T060000Z_INCR/backup_manifest", "MANIFEST"),
    ("postgresql", "base/20260925T060000Z_INCR/global/pg_control", "INCR_BASE"),
    ("postgresql", "wal/000000010000000000000001", "WAL"),
    ("oracle", "ORCL_ARCH_20260925_123.bkp", "ARCHIVELOG"),
    ("oracle", "c-1234-20260925-00", "CONTROLFILE"),
    ("oracle", "ORCL_DB_L0_20260925_7.bkp", "RMAN_L0"),
    ("sqlserver", "APPDB/LOG/APPDB_LOG_1.trn", "LOG"),
])
def test_a_staged_files_type_is_read_from_the_layout_its_engine_writes(db_type, relative, file_type):
    from dbabrain.backup_restore import restore_script

    assert restore_script._script_file_type(db_type, relative) == file_type
