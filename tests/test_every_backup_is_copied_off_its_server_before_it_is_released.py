"""Every backup file is copied off its server and proven there before its source may go (0.34.0).

Why it matters: during the ransomware wave of 2026-10-30 every backup of the estate sat on the
database host, most on the data drive itself - one encryption took the database and every way back
with it. The operator's answer (1.240, 1.246, 1.247): *each backup run's files go to a repository on
another machine, proven there by checksum and bytes as a restore's copy is; only then may the file
on the database server be deleted* - and the scenario's first principle, *never delete an original
before its copy is verified, never on the copy command's word*.

So these tests hold the four promises a person relies on when they turn ``offload_enabled`` on:

1. a copy is ``verified`` only when the repository's own hash and size are the source's, and the
   source's are what the backup recorded - a changed backup is never copied over a good one;
2. a copy that does not arrive intact is copied again, and then left ``failed`` with the reason, the
   source untouched;
3. a source is released only for a verified copy, proven AGAIN at that moment, never inside the
   newest chain of its database, and the store itself refuses a deletion before verification;
4. a repository copy is never a restore and never a drill - it does not mark the file as used.

Two hosts are simulated by one fake ``common.cli``: ``file-checksum`` on either, ``copy-files`` from
the source to the repository (which can corrupt a copy on demand), ``delete-verified-file`` on the
source. The fake holds both to the real commands' rules (0.37.0): every copy line states its file's
name, bytes and sha256, and the source is read only when they are its own; a delete names ONE file
with its name, path, bytes and sha256, inside its fence, and deletes only when all of them are true.
The catalog is a real SQLite one.
"""

from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import offload as O
from dbabrain.backup_restore import workflow
from dbabrain.db.backup_catalog import (
    ADDED_CONSTRAINTS, OFFLOAD_COLUMNS, BackupCatalogError, BackupCatalogStore, _migrate)
from dbabrain.lib import integrity as I
from dbabrain.lib.offload import BackupOffload, parse_repository_retention

SRC, REPO = "A1A-SRC-01", "A1A-REPO-01"
BACKUP_DIR = "/var/opt/mssql/backup"
HOST_DIR = "/srv/mssql/backup"          # the same folder, as the host sees it through the mount


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Net:
    """The source host, the repository host, and the one copy path between them."""

    def __init__(self) -> None:
        self.source: dict[str, bytes] = {}
        self.repo: dict[str, bytes] = {}
        self.corrupt_next = 0          # how many coming copies land with other bytes, same size
        self.truncate_next = 0         # how many coming copies land cut short
        self.copy_fails = False        # every copy answered as failed, nothing written
        self.calls: list[str] = []
        self.deleted: list[str] = []
        self.fences: list[str] = []
        self.share_reads: list[str] = []
        self.delete_requests: list[dict] = []
        self.copy_lines: list[dict] = []
        self.overwrites: list[bool] = []

    def run(self, command, request, **_kwargs):
        self.calls.append(command)
        if command == "file-checksum":
            files = self.source if request["host"]["host"] == "src" else self.repo
            if "folder" in request:
                folder = request["folder"].rstrip("/")
                found = sorted(p for p in files if p.startswith(folder + "/"))
                return {"host_folder": folder, "files": [
                    {"path": p, "sha256": _sha(files[p]), "size_bytes": len(files[p])} for p in found]}
            return {"files": [{"path": p, "sha256": _sha(files[p]), "size_bytes": len(files[p])}
                              for p in request["paths"] if p in files]}
        if command == "copy-files":
            lines = []
            assert isinstance(request["overwrite"], bool), "overwrite is stated on every copy"
            self.overwrites.append(request["overwrite"])
            for item in request["files"]:
                self.copy_lines.append(dict(item))
                # The real command refuses a line without every fact (filecopy._files).
                assert set(item) == {"source", "target", "file_name", "size_bytes", "sha256"}, item
                assert item["file_name"] == item["target"].rsplit("/", 1)[-1]
                if "source_share" in request:
                    self.share_reads.append(item["source"])
                    data = self.source.get("D:\\SQLBK\\" + item["source"])
                else:
                    data = self.source.get(item["source"])
                if data is not None and (len(data) != item["size_bytes"] or _sha(data) != item["sha256"]):
                    # The real copier holds the stream to the stated facts and lands nothing otherwise.
                    lines.append({"source": item["source"], "target": item["target"], "ok": False,
                                  "bytes_copied": 0, "error": "FactMismatch: not the file stated"})
                    continue
                if not request["overwrite"] and item["target"] in self.repo:
                    lines.append({"source": item["source"], "target": item["target"], "ok": False, "exists": True,
                                  "bytes_copied": 0, "error": "the target already holds a file and overwrite "
                                                              "is false - left as it is"})
                    continue
                if data is None or self.copy_fails:
                    lines.append({"source": item["source"], "target": item["target"], "ok": False,
                                  "bytes_copied": 0, "error": "FileNotFoundError: gone"})
                    continue
                if self.corrupt_next:
                    self.corrupt_next -= 1
                    data = data[:-1] + b"X"
                elif self.truncate_next:
                    self.truncate_next -= 1
                    data = data[: len(data) // 2]
                self.repo[item["target"]] = data
                lines.append({"source": item["source"], "target": item["target"], "ok": True,
                              "bytes_copied": len(data), "error": ""})
            return {"files": lines}
        # Nothing deletes through a list of bare paths any more (0.37.0): delete-files is not answered.
        raise AssertionError(command)

    def run_allowing_failure(self, command, request, **_kwargs):
        """``(ok, data, error)`` - the transport's form for a command whose failure is data."""
        self.calls.append(command)
        if command == "delete-verified-files":
            # The list form: each item the single command's request, the first one not deleted stops it.
            assert set(request) <= {"host", "files", "dry_run", "timeout_seconds"}, request
            rows, stopped = [], False
            for item in request["files"]:
                assert set(item) == {"path", "file_name", "size_bytes", "sha256", "must_be_under"}, item
                if stopped:
                    rows.append({**item, "status": "not_attempted", "reason": "an earlier item stopped the list"})
                    continue
                _ok, data, _e = self._delete_one({"host": request["host"], **item})
                rows.append(data["file"])
                stopped = data["file"]["status"] not in ("deleted", "not_found")
            return (not stopped), {"files": rows, "complete": not stopped}, ("stopped" if stopped else "")
        if command == "delete-empty-folder":
            path = request["path"].rstrip("/")
            assert path.startswith(request["must_be_under"].rstrip("/") + "/")
            files = self.source if request["host"]["host"] == "src" else self.repo
            inside = [p for p in files if p.startswith(path + "/")]
            status = "refused" if inside else "deleted"
            if not inside:
                self.deleted.append(path + "/")
            return (not inside), {"folder": {"path": path, "status": status,
                                             "reason": f"holds {inside[0]}" if inside else ""}}, ""
        assert command == "delete-verified-file", command
        return self._delete_one(request)

    def _delete_one(self, request):
        self.delete_requests.append(dict(request))
        # The real command's request rules (deleteverified): one path, every fact, a fence.
        assert set(request) <= {"host", "path", "file_name", "size_bytes", "sha256", "must_be_under",
                                "dry_run", "timeout_seconds"}, request
        assert "paths" not in request and isinstance(request["path"], str)
        path, root = request["path"], request["must_be_under"]
        assert path.startswith(root.rstrip("/\\") + ("\\" if "\\" in path else "/"))
        assert request["file_name"] == path.replace("\\", "/").rsplit("/", 1)[-1]
        self.fences.append(root)
        files = self.source if request["host"]["host"] == "src" else self.repo
        data = files.get(path)
        line = {"path": path, "file_name": request["file_name"], "size_bytes": request["size_bytes"],
                "sha256": request["sha256"]}
        if data is None:
            return True, {"file": {**line, "status": "not_found", "reason": ""}, "deleted": False}, ""
        if len(data) != request["size_bytes"] or _sha(data) != request["sha256"]:
            return False, {"file": {**line, "status": "refused", "reason": "not the file stated"}}, "refused"
        del files[path]
        self.deleted.append(path)
        return True, {"file": {**line, "status": "deleted", "reason": ""}, "deleted": True}, ""


@pytest.fixture
def net(monkeypatch):
    fake = _Net()
    monkeypatch.setattr(O.common_cli, "run", fake.run)
    monkeypatch.setattr(O.common_cli, "run_allowing_failure", fake.run_allowing_failure)
    return fake


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


def _offload(**overrides):
    values = dict(enabled=True, server_id=REPO, folder="/repository", copy_max_retry=2, scan="none",
                  delete_source=False, source_share="", source_username="", source_password_ref="")
    values.update(overrides)
    if isinstance(values.get("retention"), dict):
        # As an entry states it: seconds per file type (offload_retention), read by the one parser.
        values["retention"] = parse_repository_retention(values["retention"], label="test")
    return BackupOffload(**values)


def _entry(**overrides):
    values = dict(backup_id="SRC01_MSSQL", server_id=SRC, db_type="sqlserver", backup_dir=BACKUP_DIR,
                  offload=_offload(), jobs=())
    values.update(overrides)
    return O._Entry(**values)


def _hosts(entry, share=None):
    hosts = object.__new__(O._Hosts)
    hosts.entry = entry
    hosts.source_host = {"host": "src", "runtime": "windows" if share else "linux"}
    hosts.repository_host = {"host": "repo", "runtime": "linux"}
    hosts.share = share
    hosts.copy_source = {"source_share": share} if share else {"source": {"host": "src"}}
    hosts.target = {"host": "repo"}
    hosts.copy_errors = {}
    return hosts


def _backup(catalog, net, files, *, ended="2026-10-30T01:00:00Z", windows=False):
    """One backup run of ``files`` [(database, type, name, bytes)], written on the source and catalogued."""
    rows = []
    for database, file_type, name, data in files:
        folder = {"FULL": "FULL", "DIFF": "DIFF", "LOG": "LOG"}.get(file_type, file_type)
        if windows:
            path = f"D:\\SQLBK\\{database}\\{folder}\\{name}"
            host_path = path
        else:
            path = f"{BACKUP_DIR}/{database}/{folder}/{name}"
            host_path = f"{HOST_DIR}/{database}/{folder}/{name}"
        net.source[host_path] = data
        rows.append({"backup_id": "SRC01_MSSQL", "server_id": SRC, "database_name": database, "engine": "sqlserver",
                     "file_name": name, "file_type": file_type, "file_path": path, "host_path": host_path,
                     "backup_ended_at": ended, "size_bytes": len(data), "checksum_after_backup": _sha(data),
                     "verdict": "ok", "offload_state": "pending", "on_disk": 1, "checksum_matches": 1})
    catalog.record_backup(run={"backup_id": "SRC01_MSSQL", "server_id": SRC, "engine": "sqlserver",
                               "started_at": ended, "status": "DONE"}, files=rows)


def _rows(catalog):
    with catalog.connect() as conn:
        return {r["file_name"]: dict(r) for r in conn.execute("SELECT * FROM backup_files")}


# --------------------------------------------------------------------------- #
# 1. Copied, proven, verified
# --------------------------------------------------------------------------- #
def test_a_written_file_is_copied_to_the_repository_and_verified_there(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL_20261030.bak", b"full bytes")])
    entry = _entry()
    rows = catalog.files_to_offload(backup_id=entry.backup_id)

    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry), rows=rows, sleep=lambda _s: None)

    target = "/repository/A1A-SRC-01/APPDB/FULL/APPDB_FULL_20261030.bak"
    assert net.repo[target] == b"full bytes"
    assert answer["verified"] == 1 and answer["failed"] == []
    row = _rows(catalog)["APPDB_FULL_20261030.bak"]
    assert row["offload_state"] == "verified" and row["offload_path"] == target
    assert row["offload_server_id"] == REPO and row["offload_run_id"] == answer["offload_run_id"]
    assert row["offload_verified_at"] and row["state"] == "present" and row["source_deleted_at"] is None
    assert catalog.files_to_offload(backup_id=entry.backup_id) == [], "a verified file is done with"


def test_the_offload_is_its_own_run_never_a_restore_and_never_a_use_of_the_file(catalog, net):
    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_20261030_0115.trn", b"log bytes")])
    entry = _entry()
    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                             rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    with catalog.connect() as conn:
        run = dict(conn.execute("SELECT * FROM restore_runs WHERE restore_run_id = ?",
                                (answer["offload_run_id"],)).fetchone())
    assert run["action"] == "offload" and run["phase"] == "copied"
    assert run["source_server_id"] == SRC and run["target_server_id"] == REPO
    [listed] = catalog.server_backup_files(SRC)
    assert listed["used_count"] == 0 and listed["restored_ok_count"] == 0
    assert listed["last_restore_run_id"] is None, "a repository copy is not the file's last restore"


def test_the_repository_check_is_history_and_never_rewrites_the_sources_own_state(catalog, net):
    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_1.trn", b"log bytes")])
    entry = _entry()
    O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                    rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    with catalog.connect() as conn:
        checks = [dict(r) for r in conn.execute("SELECT * FROM backup_file_checks")]
    assert [(c["checked_by"], c["location"], c["result"]) for c in checks] == [("offload", "repository", "match")]
    row = _rows(catalog)["APPDB_LOG_1.trn"]
    assert row["last_checked_at"] != checks[0]["checked_at"], "the source's latest check is its own"


def test_files_written_before_the_offload_was_turned_on_are_copied_too_and_a_full_goes_first(catalog, net):
    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_2.trn", b"log"), ("APPDB", "FULL", "APPDB_FULL.bak", b"full")])
    with catalog.connect() as conn:
        conn.execute("UPDATE backup_files SET offload_state = NULL")     # written while the offload was off

    names = [r["file_name"] for r in catalog.files_to_offload(backup_id="SRC01_MSSQL")]
    assert names == ["APPDB_FULL.bak", "APPDB_LOG_2.trn"]


# --------------------------------------------------------------------------- #
# 2. Not intact: copied again, then failed with the reason, the source kept
# --------------------------------------------------------------------------- #
def test_a_copy_that_arrives_corrupted_is_copied_again_until_it_is_proven(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL.bak", b"full bytes")])
    net.corrupt_next = 1
    entry = _entry(offload=_offload(copy_overwrite=True))
    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                             rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    assert answer["verified"] == 1 and answer["recopied"] == 1
    assert _rows(catalog)["APPDB_FULL.bak"]["offload_state"] == "verified"


def test_a_truncated_copy_is_neither_checksum_nor_bytes_and_is_copied_again_with_both_recorded(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL.bak", b"full backup bytes")])
    net.truncate_next = 1
    entry = _entry(offload=_offload(copy_overwrite=True))
    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                             rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    assert answer["verified"] == 1 and answer["recopied"] == 1
    [run_file] = catalog.run_files(answer["offload_run_id"])
    first, second = catalog.attempts(run_file["run_file_id"])
    assert first["result"] == "mismatch" and first["size_target_after_copy"] == len(b"full backup bytes") // 2
    assert first["checksum_target_after_copy"] != _sha(b"full backup bytes")
    assert second["result"] == "match" and second["size_target_after_copy"] == len(b"full backup bytes")
    assert run_file["size_source_bytes"] == run_file["size_target_bytes"] == len(b"full backup bytes")


def test_a_copy_that_never_lands_on_the_repository_spends_every_retry_and_keeps_the_copy_error(catalog, net):
    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_5.trn", b"log bytes")])
    net.copy_fails = True
    entry = _entry(offload=_offload(copy_max_retry=2))
    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                             rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    assert answer["verified"] == 0 and net.calls.count("copy-files") == 3, "copy_max_retry 2 = three copies"
    [run_file] = catalog.run_files(answer["offload_run_id"])
    attempts = catalog.attempts(run_file["run_file_id"])
    assert [a["result"] for a in attempts] == ["copy_error"] * 3
    assert all("after the copy" in a["error_text"] for a in attempts)
    row = _rows(catalog)["APPDB_LOG_5.trn"]
    assert row["offload_state"] == "failed" and "FileNotFoundError: gone" in row["offload_error"]
    assert f"{HOST_DIR}/APPDB/LOG/APPDB_LOG_5.trn" in net.source, "the source is kept"


def test_a_copy_that_never_arrives_intact_is_failed_with_its_reason_and_the_source_is_untouched(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL.bak", b"full bytes")])
    net.corrupt_next = 99
    entry = _entry(offload=_offload(copy_max_retry=2, copy_overwrite=True))
    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                             rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    assert answer["verified"] == 0 and net.calls.count("copy-files") == 3, "copy_max_retry 2 = three copies"
    row = _rows(catalog)["APPDB_FULL.bak"]
    assert row["offload_state"] == "failed" and "none arrived equal" in row["offload_error"]
    assert row["state"] == "present" and f"{HOST_DIR}/APPDB/FULL/APPDB_FULL.bak" in net.source
    with catalog.connect() as conn:
        assert conn.execute("SELECT phase FROM restore_runs").fetchone()["phase"] == "refused"


def test_without_overwrite_a_differing_file_already_on_the_repository_is_kept_and_the_row_fails(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL.bak", b"full bytes")])
    target = "/repository/A1A-SRC-01/APPDB/FULL/APPDB_FULL.bak"
    net.repo[target] = b"something else"
    entry = _entry(offload=_offload(copy_max_retry=1))

    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                             rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    assert net.overwrites and not any(net.overwrites), "offload_copy_overwrite false travels as overwrite false"
    assert answer["verified"] == 0 and net.repo[target] == b"something else"
    row = _rows(catalog)["APPDB_FULL.bak"]
    assert row["offload_state"] == "failed" and row["state"] == "present"


def test_without_overwrite_an_identical_file_already_on_the_repository_is_proven_not_copied_again(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL.bak", b"full bytes")])
    target = "/repository/A1A-SRC-01/APPDB/FULL/APPDB_FULL.bak"
    net.repo[target] = b"full bytes"            # an earlier pass landed it and died before recording
    entry = _entry()

    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                             rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    assert answer["verified"] == 1 and _rows(catalog)["APPDB_FULL.bak"]["offload_state"] == "verified"


def test_a_backup_that_changed_after_it_was_written_is_never_copied(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL.bak", b"full bytes")])
    net.source[f"{HOST_DIR}/APPDB/FULL/APPDB_FULL.bak"] = b"encrypted by somebody"
    entry = _entry()
    O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                    rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    assert "copy-files" not in net.calls and net.repo == {}
    row = _rows(catalog)["APPDB_FULL.bak"]
    assert row["offload_state"] == "failed" and "the backup changed after it was written" in row["offload_error"]


def test_a_file_gone_from_its_server_before_its_copy_is_marked_missing_and_left_alone(catalog, net):
    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_3.trn", b"log")])
    net.source.clear()
    entry = _entry()
    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                             rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    assert answer["source_missing"] == 1
    assert _rows(catalog)["APPDB_LOG_3.trn"]["state"] == "missing"
    assert catalog.files_to_offload(backup_id=entry.backup_id) == [], "it cannot be copied again"


# --------------------------------------------------------------------------- #
# 3. Released only on proof, never the newest chain, never before verification
# --------------------------------------------------------------------------- #
def _two_chains(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL_1.bak", b"full one"),
                           ("APPDB", "LOG", "APPDB_LOG_1.trn", b"log one")], ended="2026-10-23T01:00:00Z")
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL_2.bak", b"full two"),
                           ("APPDB", "LOG", "APPDB_LOG_2.trn", b"log two")], ended="2026-10-30T01:00:00Z")
    entry = _entry(offload=_offload(delete_source=True))
    O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                    rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)
    return entry


def test_a_verified_source_is_released_but_the_newest_chain_stays_on_the_server(catalog, net):
    entry = _two_chains(catalog, net)

    answer = O.release_sources(entry, catalog=catalog, hosts=_hosts(entry),
                               rows=catalog.files_to_release(backup_id=entry.backup_id))

    assert answer["deleted"] == 2 and answer["refused"] == []
    assert answer["kept"] == 0, "the newest chain is not even offered: the query keeps it out"
    assert sorted(net.deleted) == [f"{HOST_DIR}/APPDB/FULL/APPDB_FULL_1.bak", f"{HOST_DIR}/APPDB/LOG/APPDB_LOG_1.trn"]
    assert net.fences == [HOST_DIR, HOST_DIR], "each delete is fenced to the backup folder as the host names it"
    rows = _rows(catalog)
    assert rows["APPDB_FULL_1.bak"]["state"] == "pruned" and rows["APPDB_FULL_1.bak"]["source_deleted_at"]
    assert rows["APPDB_FULL_2.bak"]["state"] == "present" and rows["APPDB_LOG_2.trn"]["source_deleted_at"] is None


def test_a_repository_copy_that_changed_since_it_was_proven_keeps_its_source(catalog, net):
    entry = _two_chains(catalog, net)
    net.repo["/repository/A1A-SRC-01/APPDB/FULL/APPDB_FULL_1.bak"] = b"tampered"

    answer = O.release_sources(entry, catalog=catalog, hosts=_hosts(entry),
                               rows=catalog.files_to_release(backup_id=entry.backup_id))

    assert net.deleted == [f"{HOST_DIR}/APPDB/LOG/APPDB_LOG_1.trn"]
    assert answer["refused"] == ["APPDB_FULL_1.bak: repository copy mismatch"]
    row = _rows(catalog)["APPDB_FULL_1.bak"]
    assert row["offload_state"] == "failed" and row["state"] == "present"
    assert row["checksum_matches"] == 1, "the SOURCE is still the backup's bytes - only the copy changed"
    assert [r["file_name"] for r in catalog.files_to_offload(backup_id=entry.backup_id)] == ["APPDB_FULL_1.bak"]


def test_each_released_file_is_its_own_delete_command_carrying_its_name_path_bytes_and_sha256(catalog, net):
    entry = _two_chains(catalog, net)

    O.release_sources(entry, catalog=catalog, hosts=_hosts(entry),
                      rows=catalog.files_to_release(backup_id=entry.backup_id))

    assert net.calls.count("delete-verified-file") == 2 and "delete-files" not in net.calls
    by_name = {r["file_name"]: r for r in net.delete_requests}
    assert set(by_name) == {"APPDB_FULL_1.bak", "APPDB_LOG_1.trn"}
    full = by_name["APPDB_FULL_1.bak"]
    assert full["path"] == f"{HOST_DIR}/APPDB/FULL/APPDB_FULL_1.bak"
    assert full["size_bytes"] == len(b"full one") and full["sha256"] == _sha(b"full one")
    assert full["must_be_under"] == HOST_DIR


def test_a_source_that_changed_after_its_copy_was_verified_is_refused_by_the_host_and_kept(catalog, net):
    """The repository copy is still the backup; the file on the server is not any more. The delete
    states the backup's facts, so the host refuses it - the changed file stays, the copy stays."""
    entry = _two_chains(catalog, net)
    net.source[f"{HOST_DIR}/APPDB/FULL/APPDB_FULL_1.bak"] = b"full ONE"      # same size, other bytes

    answer = O.release_sources(entry, catalog=catalog, hosts=_hosts(entry),
                               rows=catalog.files_to_release(backup_id=entry.backup_id))

    assert net.deleted == [f"{HOST_DIR}/APPDB/LOG/APPDB_LOG_1.trn"]
    assert answer["deleted"] == 1 and len(answer["refused"]) == 1
    assert answer["refused"][0].startswith("APPDB_FULL_1.bak: delete refused")
    assert f"{HOST_DIR}/APPDB/FULL/APPDB_FULL_1.bak" in net.source
    assert _rows(catalog)["APPDB_FULL_1.bak"]["state"] == "present"


@pytest.mark.parametrize("column, value, words", [
    ("checksum_after_backup", None, "no sha256 recorded"),
    ("size_bytes", None, "no size recorded"),
])
def test_a_row_without_its_recorded_facts_is_never_sent_to_delete(catalog, net, column, value, words):
    entry = _two_chains(catalog, net)
    rows = catalog.files_to_release(backup_id=entry.backup_id)
    rows = [{**r, column: value} if r["file_name"] == "APPDB_LOG_1.trn" else r for r in rows]
    # The repository re-check would refuse a row without a checksum first; this holds the delete
    # request itself to its facts, whatever reaches it.
    with pytest.raises(O.OffloadRefused, match=words):
        O.release_request(entry, next(r for r in rows if r["file_name"] == "APPDB_LOG_1.trn"), _hosts(entry))


def test_a_catalog_name_that_is_not_the_paths_last_part_is_never_sent_to_delete(catalog, net):
    entry = _two_chains(catalog, net)
    row = {**catalog.files_to_release(backup_id=entry.backup_id)[0], "file_name": "SOMETHING_ELSE.bak"}
    with pytest.raises(O.OffloadRefused, match="not the last part"):
        O.release_request(entry, row, _hosts(entry))


def test_every_copy_line_carries_the_files_name_bytes_and_sha256_measured_on_the_source(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL_20261030.bak", b"full bytes")])
    entry = _entry()
    O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                    rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    [line] = net.copy_lines
    assert line == {"source": f"{HOST_DIR}/APPDB/FULL/APPDB_FULL_20261030.bak",
                    "target": "/repository/A1A-SRC-01/APPDB/FULL/APPDB_FULL_20261030.bak",
                    "file_name": "APPDB_FULL_20261030.bak", "size_bytes": len(b"full bytes"),
                    "sha256": _sha(b"full bytes")}


def test_a_database_with_no_full_on_the_server_releases_nothing(catalog, net):
    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_9.trn", b"log")])
    entry = _entry(offload=_offload(delete_source=True))
    O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                    rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    answer = O.release_sources(entry, catalog=catalog, hosts=_hosts(entry),
                               rows=catalog.files_to_release(backup_id=entry.backup_id))
    assert answer["deleted"] == 0 and net.deleted == []


def test_the_store_refuses_a_source_deleted_before_its_copy_is_verified(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL.bak", b"full")])
    file_id = _rows(catalog)["APPDB_FULL.bak"]["file_id"]

    with pytest.raises(BackupCatalogError, match="only after its repository copy is verified"):
        catalog.set_backup_file(file_id, source_deleted_at="2026-10-31T00:00:00Z")
    with pytest.raises(BackupCatalogError, match="names offload_path"):
        catalog.set_backup_file(file_id, offload_state="verified", offload_server_id=REPO)
    with catalog.connect() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE backup_files SET source_deleted_at = 'now' WHERE file_id = ?", (file_id,))


# --------------------------------------------------------------------------- #
# The shapes a source comes in
# --------------------------------------------------------------------------- #
def test_a_base_backup_directory_is_copied_file_by_file_its_manifest_held_to_the_backup(catalog, net):
    base = "/srv/pg/backup/base/20261030T010000Z_FULL"
    net.source.update({f"{base}/backup_manifest": b"manifest", f"{base}/base.tar": b"data", f"{base}/pg_wal.tar": b"wal"})
    catalog.record_backup(run={"backup_id": "PG01", "server_id": SRC, "engine": "postgresql",
                               "started_at": "2026-10-30T01:00:00Z", "status": "DONE"},
                          files=[{"backup_id": "PG01", "server_id": SRC, "database_name": "", "engine": "postgresql",
                                  "file_name": "20261030T010000Z_FULL", "file_type": "FULL_BASE",
                                  "file_path": "/backup/base/20261030T010000Z_FULL", "host_path": base,
                                  "size_bytes": 8, "checksum_after_backup": _sha(b"manifest"), "verdict": "ok"}])
    entry = _entry(backup_id="PG01", db_type="postgresql", backup_dir="/backup")

    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                             rows=catalog.files_to_offload(backup_id="PG01"), sleep=lambda _s: None)

    assert answer["verified"] == 1
    assert sorted(net.repo) == [f"/repository/A1A-SRC-01/_instance/base/20261030T010000Z_FULL/{n}"
                                for n in ("backup_manifest", "base.tar", "pg_wal.tar")],         "an instance-wide base backup goes under _instance, the engine's own base/ kept below it"


# --------------------------------------------------------------------------- #
# The repository's layout: <folder>/<server_id>/<db_name>/<the engine's own folders> (0.37.0)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("backup_dir, row, expected", [
    # SQL Server writes <DB>/FULL|DIFF|LOG/<file>: already database first, kept as it is.
    ("/var/opt/mssql/backup", {"database_name": "APPDB", "file_type": "FULL",
                               "file_path": "/var/opt/mssql/backup/APPDB/FULL/APPDB_FULL_1.bak"},
     "/mnt/nas/SRV-01/APPDB/FULL/APPDB_FULL_1.bak"),
    ("/var/opt/mssql/backup", {"database_name": "APPDB", "file_type": "LOG",
                               "file_path": "/var/opt/mssql/backup/APPDB/LOG/APPDB_LOG_1.trn"},
     "/mnt/nas/SRV-01/APPDB/LOG/APPDB_LOG_1.trn"),
    ("D:\\SQLBK", {"database_name": "HR", "file_type": "DIFF", "file_path": "D:\\SQLBK\\HR\\DIFF\\HR_DIFF.bak"},
     "/mnt/nas/SRV-01/HR/DIFF/HR_DIFF.bak"),
    # PostgreSQL's base/ and wal/ are the whole instance's.
    ("/backup", {"database_name": "", "file_type": "FULL_BASE", "file_path": "/backup/base/20261030T010000Z_FULL"},
     "/mnt/nas/SRV-01/_instance/base/20261030T010000Z_FULL"),
    ("/backup", {"database_name": None, "file_type": "WAL", "file_path": "/backup/wal/000000010000000000000001"},
     "/mnt/nas/SRV-01/_instance/wal/000000010000000000000001"),
    # Oracle's RMAN pieces are flat in their folder: under their database.
    ("/opt/oracle/backup/dbops", {"database_name": "FREE", "file_type": "RMAN_L0",
                                  "file_path": "/opt/oracle/backup/dbops/FREE_L0_20261101_x.bkp"},
     "/mnt/nas/SRV-01/FREE/FREE_L0_20261101_x.bkp"),
    # Outside the backup folder: <db_name>/<file_type>/<file>.
    ("/backup", {"database_name": "APPDB", "file_type": "log", "file_name": "a.trn", "file_path": "/elsewhere/a.trn"},
     "/mnt/nas/SRV-01/APPDB/LOG/a.trn"),
])
def test_every_copy_sits_under_its_server_id_and_its_database(backup_dir, row, expected):
    entry = _entry(server_id="SRV-01", backup_dir=backup_dir, offload=_offload(folder="/mnt/nas/"))
    assert O.repository_path(entry, row) == expected


def test_two_servers_with_a_database_of_the_same_name_never_share_a_folder():
    row = {"database_name": "APPDB", "file_type": "FULL", "file_path": "/b/APPDB/FULL/APPDB_FULL_1.bak"}
    one = O.repository_path(_entry(server_id="SRV-01", backup_dir="/b"), row)
    two = O.repository_path(_entry(server_id="SRV-02", backup_dir="/b"), row)
    assert one != two and one.split("/")[2] == "SRV-01" and two.split("/")[2] == "SRV-02"


def test_a_certificate_or_key_is_never_offered_to_the_repository(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL.bak", b"full"),
                           ("_cert", "CERT", "dbabrain_backup_cert.cer", b"cer"),
                           ("_cert", "CERT", "dbabrain_backup_cert.pvk", b"pvk")])

    offered = [r["file_name"] for r in catalog.files_to_offload(backup_id="SRC01_MSSQL")]

    assert offered == ["APPDB_FULL.bak"]
    assert "CERT" in I.NEVER_ON_REPOSITORY


def test_a_windows_source_is_hashed_on_its_own_host_and_read_through_its_share(catalog, net):
    _backup(catalog, net, [("HR", "FULL", "HR_FULL.bak", b"hr full")], windows=True)
    share = {"host": "192.0.2.45", "share": "SQLBK", "subpath": "", "username": "u", "password": "p"}
    entry = _entry(backup_dir="D:\\SQLBK", offload=_offload(source_share="\\\\192.0.2.45\\SQLBK",
                                                            source_username="u", source_password_ref="R"))

    answer = O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry, share=share),
                             rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)

    assert answer["verified"] == 1
    assert net.share_reads == ["HR\\FULL\\HR_FULL.bak"], "the path under the share, never the drive letter"
    assert net.repo["/repository/A1A-SRC-01/HR/FULL/HR_FULL.bak"] == b"hr full"


@pytest.mark.parametrize("file_path, root, expected", [
    ("/var/opt/mssql/backup/APPDB/FULL/a.bak", "/var/opt/mssql/backup", "APPDB/FULL/a.bak"),
    ("/var/opt/mssql/backup2/a.bak", "/var/opt/mssql/backup", ""),
    ("D:\\SQLBK\\HR\\LOG\\b.trn", "d:\\sqlbk", "HR/LOG/b.trn"),
    ("/a.bak", "", ""),
])
def test_a_files_place_under_the_backup_folder_is_its_place_on_the_repository(file_path, root, expected):
    assert O._relative(file_path, root) == expected


# --------------------------------------------------------------------------- #
# What the offload refuses by name rather than half-doing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("overrides, words", [
    ({"scan": "clamav"}, "offload_scan 'clamav' is not built yet"),
    ({"server_id": SRC}, "is the backup's own server"),
    ({"folder": "repository"}, "must be an absolute path"),
])
def test_what_is_not_built_or_not_safe_is_refused_by_name(overrides, words):
    assert words in O.refusal(_entry(offload=_offload(**overrides)))
    assert O.refusal(_entry()) == ""


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #
def test_a_pass_writes_nothing_for_an_entry_with_nothing_to_copy_and_retries_a_failure_only_later(
        catalog, net, monkeypatch):
    entry = _entry(jobs=(SimpleNamespace(active=True, time_window=SimpleNamespace(retry_interval=3600, timeout=600),
                                         notify=None),))
    started = []
    monkeypatch.setattr(O, "entries_to_offload", lambda _path: [entry])
    monkeypatch.setattr(O.DbaBrainStore, "from_config", classmethod(lambda cls, _c: object()))
    monkeypatch.setattr(O.BackupCatalogStore, "from_config", classmethod(lambda cls, _c: catalog))
    monkeypatch.setattr(O.schedule, "reap_stale_runs", lambda **_k: [])
    monkeypatch.setattr(O, "_run_one", lambda e, **k: started.append(len(k["rows"])) or {"status": "done"})

    assert O.run_offloads(app_config=None, config_path="x")["jobs"] == [], "nothing catalogued, nothing run"

    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_7.trn", b"log")])
    file_id = _rows(catalog)["APPDB_LOG_7.trn"]["file_id"]
    catalog.set_backup_file(file_id, offload_state="failed", offload_attempted_at="2999-01-01T00:00:00Z")
    O.run_offloads(app_config=None, config_path="x")
    assert started == [], "a failure is not retried before the entry's retry_interval"

    catalog.set_backup_file(file_id, offload_attempted_at="2000-01-01T00:00:00Z")
    O.run_offloads(app_config=None, config_path="x")
    assert started == [1]


def test_the_scheduled_workflow_runs_the_offload_after_the_restores_and_not_without_the_backup_half(monkeypatch):
    order = []
    monkeypatch.setattr(workflow, "run_backup", lambda **_k: order.append("backup") or {"failed": 0})
    monkeypatch.setattr(workflow, "run_scheduled_restores", lambda **_k: order.append("restore") or {"failed": 0})
    monkeypatch.setattr(O, "run_offloads", lambda **_k: order.append("offload") or {"failed": 1})

    result = workflow.run_workflow(app_config=None, config_path="x")
    assert order == ["backup", "restore", "offload"] and result["failed"] == 1

    order.clear()
    workflow.run_workflow(app_config=None, config_path="x", skip_backup=True)
    workflow.run_workflow(app_config=None, config_path="x", skip_offload=True)
    assert order == ["restore", "backup", "restore"]


def test_a_backup_of_an_offloading_entry_is_recorded_as_waiting_for_its_copy(catalog):
    from dbabrain.backup_restore.backup import BackupRunResult, record_in_catalog

    item = SimpleNamespace(backup_id="B", server_id=SRC, db_type="sqlserver", offload=_offload())
    result = BackupRunResult(job=item, status="done", exit_code=0, duration_ms=1, stdout="", stderr="", files=[
        {"database_name": "APPDB", "file_name": "a.trn", "file_type": "LOG", "path": "/b/a.trn", "size_bytes": 3,
         "sha256": _sha(b"abc"), "engine_verify": "passed"}])
    record_in_catalog(catalog, item, result, job_run_id=1, started_at="2026-10-30T00:00:00Z",
                      finished_at="2026-10-30T00:01:00Z")
    assert _rows(catalog)["a.trn"]["offload_state"] == "pending"

    item.offload = _offload(enabled=False)
    result.files[0]["file_name"], result.files[0]["path"] = "b.trn", "/b/b.trn"
    record_in_catalog(catalog, item, result, job_run_id=2, started_at="2026-10-30T00:02:00Z",
                      finished_at="2026-10-30T00:03:00Z")
    assert _rows(catalog)["b.trn"]["offload_state"] is None


# --------------------------------------------------------------------------- #
# The catalog made before the repository copy
# --------------------------------------------------------------------------- #
def test_a_catalog_from_before_the_offload_gains_its_columns_once_and_twice_changes_nothing(tmp_path):
    conn = sqlite3.connect(tmp_path / "v1.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE backup_files (file_id INTEGER PRIMARY KEY, backup_id TEXT NOT NULL DEFAULT '')")
    _migrate(conn, "sqlite")
    _migrate(conn, "sqlite")
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(backup_files)")]
    # Later versions add columns of their own after these (row_hash, 1.224): the offload's are there,
    # in order, and none twice.
    offload = [name for name, _sql in OFFLOAD_COLUMNS]
    assert columns[2:2 + len(offload)] == offload and len(columns) == len(set(columns))


def test_the_live_postgresql_catalog_is_widened_in_place_and_new_rules_are_not_reproven_over_old_rows():
    statements = []

    class _Conn:
        def execute(self, sql, params=()):
            statements.append(sql)
            return SimpleNamespace(fetchone=lambda: None)

    _migrate(_Conn(), "postgresql")
    text = "\n".join(statements)
    for name, _sql in OFFLOAD_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {name} " in text
    assert "DROP CONSTRAINT IF EXISTS restore_runs_action_check" in text
    assert "ADD CONSTRAINT ck_restore_runs_action CHECK (action IN ('restore', 'copy_only', 'offload'))" in text
    assert "ADD CONSTRAINT ck_backup_file_checks_location CHECK (location IN ('source', 'vault', 'repository'))" in text
    for _table, name, _check, validate in ADDED_CONSTRAINTS:
        line = next(s for s in statements if f"ADD CONSTRAINT {name} " in s)
        assert line.endswith("NOT VALID") is (not validate)


def test_the_run_actions_are_the_entry_actions_and_the_offload():
    assert I.RUN_ACTIONS == ("restore", "copy_only", "offload")
    with pytest.raises(I.IntegrityConfigError):
        I.parse_restore_action({"action": "offload"}, label="r")


# --------------------------------------------------------------------------- #
# Found by the review of 2026-10-31 (a second reader, Gemini, each one checked here)
# --------------------------------------------------------------------------- #
def _verify_all(catalog, net, entry):
    O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry),
                    rows=catalog.files_to_offload(backup_id=entry.backup_id), sleep=lambda _s: None)


def test_a_log_written_while_the_newest_full_ran_belongs_to_its_chain_and_stays(catalog, net):
    # The newest full's run started 02:00 and ended 04:00; a log written at 03:00 is inside it.
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL_1.bak", b"older full")], ended="2026-10-23T04:00:00Z")
    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_0300.trn", b"log during the full")],
            ended="2026-10-30T03:00:00Z")
    catalog.record_backup(run={"backup_id": "SRC01_MSSQL", "server_id": SRC, "engine": "sqlserver",
                               "started_at": "2026-10-30T02:00:00Z", "status": "DONE"},
                          files=[{"backup_id": "SRC01_MSSQL", "server_id": SRC, "database_name": "APPDB",
                                  "engine": "sqlserver", "file_name": "APPDB_FULL_2.bak", "file_type": "FULL",
                                  "file_path": f"{BACKUP_DIR}/APPDB/FULL/APPDB_FULL_2.bak",
                                  "host_path": f"{HOST_DIR}/APPDB/FULL/APPDB_FULL_2.bak",
                                  "backup_ended_at": "2026-10-30T04:00:00Z", "size_bytes": 8,
                                  "checksum_after_backup": _sha(b"new full"), "verdict": "ok"}])
    net.source[f"{HOST_DIR}/APPDB/FULL/APPDB_FULL_2.bak"] = b"new full"
    entry = _entry(offload=_offload(delete_source=True))
    _verify_all(catalog, net, entry)

    releasable = [r["file_name"] for r in catalog.files_to_release(backup_id=entry.backup_id)]
    assert releasable == ["APPDB_FULL_1.bak"], "the 03:00 log is in the window of the newest full's run"


def test_the_newest_chain_never_reaches_the_release_list_so_a_pass_with_nothing_to_release_is_quiet(catalog, net):
    _backup(catalog, net, [("APPDB", "FULL", "APPDB_FULL.bak", b"full"), ("APPDB", "LOG", "APPDB_LOG.trn", b"log")])
    entry = _entry(offload=_offload(delete_source=True))
    _verify_all(catalog, net, entry)

    assert catalog.files_to_release(backup_id=entry.backup_id) == []


def test_failures_waiting_out_their_pause_never_starve_the_files_written_since(catalog, net):
    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_A.trn", b"a")], ended="2026-10-29T00:00:00Z")
    _backup(catalog, net, [("APPDB", "LOG", "APPDB_LOG_B.trn", b"b")], ended="2026-10-30T00:00:00Z")
    old = _rows(catalog)["APPDB_LOG_A.trn"]["file_id"]
    catalog.set_backup_file(old, offload_state="failed", offload_attempted_at="2026-10-31T00:00:00Z")

    waiting = catalog.files_to_offload(backup_id="SRC01_MSSQL", limit=1, retry_before="2026-10-30T23:00:00Z")
    due = catalog.files_to_offload(backup_id="SRC01_MSSQL", limit=1, retry_before="2026-10-31T01:00:00Z")
    assert [r["file_name"] for r in waiting] == ["APPDB_LOG_B.trn"]
    assert [r["file_name"] for r in due] == ["APPDB_LOG_A.trn"]


def test_a_file_outside_the_backup_folder_is_released_inside_its_own_folders_fence(catalog, net):
    _two_chains(catalog, net)
    wal = "/srv/pg/wal_archive/000000010000000000000042"
    net.source[wal] = b"wal"
    catalog.record_backup(run={"backup_id": "SRC01_MSSQL", "server_id": SRC, "engine": "sqlserver",
                               "started_at": "2026-10-22T00:00:00Z", "status": "DONE"},
                          files=[{"backup_id": "SRC01_MSSQL", "server_id": SRC, "database_name": "APPDB",
                                  "engine": "sqlserver", "file_name": "000000010000000000000042", "file_type": "LOG",
                                  "file_path": "/wal_archive/000000010000000000000042", "host_path": wal,
                                  "backup_ended_at": "2026-10-22T00:00:00Z", "size_bytes": 3,
                                  "checksum_after_backup": _sha(b"wal"), "verdict": "ok"}])
    entry = _entry(offload=_offload(delete_source=True))
    _verify_all(catalog, net, entry)

    answer = O.release_sources(entry, catalog=catalog, hosts=_hosts(entry),
                               rows=catalog.files_to_release(backup_id=entry.backup_id))
    assert wal in net.deleted and "/srv/pg/wal_archive" in net.fences and answer["refused"] == []


def test_a_released_file_checked_again_stays_pruned_and_its_copy_cannot_be_unverified(catalog, net):
    entry = _two_chains(catalog, net)
    O.release_sources(entry, catalog=catalog, hosts=_hosts(entry),
                      rows=catalog.files_to_release(backup_id=entry.backup_id))
    file_id = _rows(catalog)["APPDB_FULL_1.bak"]["file_id"]

    assert catalog.check_backup_file(file_id, checksum_now=None) == "missing"
    assert _rows(catalog)["APPDB_FULL_1.bak"]["state"] == "pruned", "deleted on purpose, not lost"
    with pytest.raises(BackupCatalogError, match="only after its repository copy is verified"):
        catalog.set_backup_file(file_id, offload_state="failed")


def test_a_checksum_in_capitals_is_the_same_checksum():
    from dbabrain.backup_restore.copy_gate import same_bytes

    assert same_bytes((_sha(b"x").upper(), 1), _sha(b"x"), 1)
