"""A server's repository folder holds exactly its proven copies - anything else fails the run (0.37.0).

Why it matters: the operator, 2026-11-10 (translated): *if /mnt/nas/<server_id> has a stray file - one
that is not in the backup tables - the backup/restore run fails and says so, to the group already set
for backup/restore notices; and every case must have a test proving it*. A repository is trusted
because every file in it was proven when it was copied; a file nobody proved - left by hand, left by a
copy that died, an expired copy whose delete never happened - sits beside the proven ones looking
exactly like them, and a restore that picks it restores what nobody checked.

So, case by case:

* the comparison itself (``lib.repository_check``): clean; a stray file; a leftover partial; an expired
  copy still there; a proven copy gone; one of other bytes; a link; an encrypted copy (its ``.age``
  expected, its plain file not once the encryption deleted it, both while it has not); another
  folder's history ignored; the message names counts and the first paths;
* ``list-files`` for real - POSIX ``find`` and this machine - kinds, bytes, a link listed and never
  followed, a folder that is not there;
* the field: stated on every entry, true or false only, written true;
* the pass: proofs from a real catalog, the folder listed without hashing, a stray file turning the run
  ``error`` and the error event going to the entry's backup/restore notify; a clean folder leaving it
  ``done``; the check off asks nothing.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import offload as O
from dbabrain.common import listfiles as L
from dbabrain.db.backup_catalog import BackupCatalogStore
from dbabrain.lib import offload as offload_rules
from dbabrain.lib import repository_check as RC
from dbabrain.lib.offload import BackupOffload

FOLDER = "/mnt/nas/SRV-01"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _listed(*entries) -> list[dict]:
    return [{"path": path, "type": kind, "size_bytes": size} for path, kind, size in entries]


# --------------------------------------------------------------------------- #
# 1. The comparison
# --------------------------------------------------------------------------- #
_PROOFS = {f"{FOLDER}/APPDB/FULL/a.bak": {"size_bytes": 100}, f"{FOLDER}/APPDB/LOG/a.trn": {"size_bytes": 10}}


def test_a_folder_holding_exactly_the_proven_copies_is_clean():
    expected = RC.expected_files(FOLDER, _PROOFS, [])
    found = RC.findings(expected, _listed((f"{FOLDER}/APPDB/FULL/a.bak", "file", 100),
                                          (f"{FOLDER}/APPDB/LOG/a.trn", "file", 10)))
    assert RC.is_clean(found)
    assert RC.describe(FOLDER, found).endswith("holds exactly the proven copies")


@pytest.mark.parametrize("name", ["notes.txt", "a.bak.copy", "a.bak.dbabrain_partial", "APPDB_FULL_old.bak"])
def test_a_file_the_catalog_does_not_know_is_stray(name):
    expected = RC.expected_files(FOLDER, _PROOFS, [])
    found = RC.findings(expected, _listed((f"{FOLDER}/APPDB/FULL/a.bak", "file", 100),
                                          (f"{FOLDER}/APPDB/LOG/a.trn", "file", 10),
                                          (f"{FOLDER}/APPDB/FULL/{name}", "file", 5)))
    assert [item["path"] for item in found[RC.STRAY]] == [f"{FOLDER}/APPDB/FULL/{name}"]
    assert not RC.is_clean(found) and "1 not in the catalog" in RC.describe(FOLDER, found)


def test_an_expired_copy_still_on_the_repository_is_stray():
    """An expired copy is not in the proofs (its backup file is no longer ``verified``)."""
    expected = RC.expected_files(FOLDER, {f"{FOLDER}/APPDB/LOG/a.trn": {"size_bytes": 10}}, [])
    found = RC.findings(expected, _listed((f"{FOLDER}/APPDB/LOG/a.trn", "file", 10),
                                          (f"{FOLDER}/APPDB/FULL/a.bak", "file", 100)))
    assert [item["path"] for item in found[RC.STRAY]] == [f"{FOLDER}/APPDB/FULL/a.bak"]


def test_a_proven_copy_that_is_gone_is_missing():
    found = RC.findings(RC.expected_files(FOLDER, _PROOFS, []), _listed((f"{FOLDER}/APPDB/LOG/a.trn", "file", 10)))
    assert found[RC.MISSING] == [{"path": f"{FOLDER}/APPDB/FULL/a.bak", "expected_size_bytes": 100}]


def test_a_folder_that_is_gone_makes_every_proven_copy_missing():
    found = RC.findings(RC.expected_files(FOLDER, _PROOFS, []), [])
    assert len(found[RC.MISSING]) == 2


def test_an_empty_folder_with_nothing_proven_is_clean():
    assert RC.is_clean(RC.findings(RC.expected_files(FOLDER, {}, []), []))


def test_a_proven_copy_of_other_bytes_differs():
    found = RC.findings(RC.expected_files(FOLDER, _PROOFS, []),
                        _listed((f"{FOLDER}/APPDB/FULL/a.bak", "file", 99), (f"{FOLDER}/APPDB/LOG/a.trn", "file", 10)))
    assert found[RC.SIZE_DIFFERS] == [{"path": f"{FOLDER}/APPDB/FULL/a.bak", "size_bytes": 99,
                                       "expected_size_bytes": 100}]


@pytest.mark.parametrize("kind", ["link", "other"])
def test_a_link_or_device_is_a_finding_even_at_a_proven_name(kind):
    found = RC.findings(RC.expected_files(FOLDER, _PROOFS, []),
                        _listed((f"{FOLDER}/APPDB/FULL/a.bak", kind, 0), (f"{FOLDER}/APPDB/LOG/a.trn", "file", 10)))
    assert [item["path"] for item in found[RC.NOT_A_FILE]] == [f"{FOLDER}/APPDB/FULL/a.bak"]
    assert found[RC.STRAY] == [] and found[RC.MISSING] == []


def test_a_proof_without_a_recorded_size_is_held_to_being_there_only():
    expected = RC.expected_files(FOLDER, {f"{FOLDER}/x.bak": {"size_bytes": None}}, [])
    assert RC.is_clean(RC.findings(expected, _listed((f"{FOLDER}/x.bak", "file", 12345))))


def test_another_folders_history_is_not_expected_here():
    proofs = {**_PROOFS, "/old/repository/SRV-01/APPDB/FULL/a.bak": {"size_bytes": 1},
              "/mnt/nas/SRV-010/APPDB/FULL/a.bak": {"size_bytes": 1}}    # a server whose id starts the same
    assert set(RC.expected_files(FOLDER, proofs, [])) == set(_PROOFS)


_SEALED = {"plain_path": f"{FOLDER}/APPDB/FULL/a.bak", "encrypted_path": f"{FOLDER}/APPDB/FULL/a.bak.age",
           "encrypted_size_bytes": 140, "plain_deleted_at": "2026-11-10T07:30:00Z"}


def test_an_encrypted_copy_expects_its_age_file_and_no_longer_its_plain_file():
    expected = RC.expected_files(FOLDER, _PROOFS, [_SEALED])
    assert expected[f"{FOLDER}/APPDB/FULL/a.bak.age"] == 140 and f"{FOLDER}/APPDB/FULL/a.bak" not in expected
    clean = _listed((f"{FOLDER}/APPDB/FULL/a.bak.age", "file", 140), (f"{FOLDER}/APPDB/LOG/a.trn", "file", 10))
    assert RC.is_clean(RC.findings(expected, clean))


def test_a_plain_file_still_there_after_its_encryption_deleted_it_is_stray():
    expected = RC.expected_files(FOLDER, _PROOFS, [_SEALED])
    found = RC.findings(expected, _listed((f"{FOLDER}/APPDB/FULL/a.bak.age", "file", 140),
                                          (f"{FOLDER}/APPDB/FULL/a.bak", "file", 100),
                                          (f"{FOLDER}/APPDB/LOG/a.trn", "file", 10)))
    assert [item["path"] for item in found[RC.STRAY]] == [f"{FOLDER}/APPDB/FULL/a.bak"]


def test_between_encrypting_and_deleting_the_plain_file_both_are_expected():
    expected = RC.expected_files(FOLDER, _PROOFS, [{**_SEALED, "plain_deleted_at": None}])
    both = _listed((f"{FOLDER}/APPDB/FULL/a.bak.age", "file", 140), (f"{FOLDER}/APPDB/FULL/a.bak", "file", 100),
                   (f"{FOLDER}/APPDB/LOG/a.trn", "file", 10))
    assert RC.is_clean(RC.findings(expected, both))


def test_an_age_file_of_other_bytes_than_written_differs():
    expected = RC.expected_files(FOLDER, _PROOFS, [_SEALED])
    found = RC.findings(expected, _listed((f"{FOLDER}/APPDB/FULL/a.bak.age", "file", 139),
                                          (f"{FOLDER}/APPDB/LOG/a.trn", "file", 10)))
    assert [item["path"] for item in found[RC.SIZE_DIFFERS]] == [f"{FOLDER}/APPDB/FULL/a.bak.age"]


def test_the_message_counts_each_kind_and_names_the_first_three():
    strays = [(f"{FOLDER}/x{i}.tmp", "file", 1) for i in range(5)]
    found = RC.findings(RC.expected_files(FOLDER, _PROOFS, []), _listed((f"{FOLDER}/APPDB/LOG/a.trn", "file", 10), *strays))
    text = RC.describe(FOLDER, found)
    assert "5 not in the catalog: x0.tmp, x1.tmp, x2.tmp (+2 more)" in text
    assert "1 proven and gone: a.bak" in text


# --------------------------------------------------------------------------- #
# 2. list-files, for real
# --------------------------------------------------------------------------- #
def _bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash") or shutil.which("sh")
    candidate = r"C:\Program Files\Git\bin\bash.exe"
    return candidate if os.path.exists(candidate) else None


def _posix(path: Path) -> str:
    text = str(path)
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:].replace("\\", "/")
    return text


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "SRV-01"
    (root / "APPDB" / "FULL").mkdir(parents=True)
    (root / "APPDB" / "FULL" / "a.bak").write_bytes(b"x" * 100)
    (root / "APPDB" / "FULL" / "with space 'q'.bak").write_bytes(b"y" * 7)
    (root / "APPDB" / "EMPTY").mkdir()
    return root


def test_the_posix_listing_reads_kind_bytes_and_every_name_exactly(tree, monkeypatch):
    bash = _bash()
    if not bash:
        pytest.skip("no POSIX shell here")
    probe = subprocess.run([bash, "-c", "find --version"], capture_output=True, text=True)
    if "GNU" not in probe.stdout:
        pytest.skip("GNU find (-printf) is what the repository host runs")

    def run(host, command, *, timeout=0, session=None):
        done = subprocess.run([bash, "-c", command], capture_output=True)
        return {"exit_code": done.returncode, "stdout": done.stdout.decode("utf-8"), "stderr": done.stderr.decode()}
    monkeypatch.setattr(L, "run", run)
    answer = L.list_files({"host": {"runtime": "linux", "host": "192.0.2.30", "username": "u", "password": "p"},
                           "folder": _posix(tree)})
    by_name = {item["path"].rsplit("/", 1)[-1]: item for item in answer["files"]}
    assert answer["folder_found"] and answer["listed"] == 2, "files only - an empty folder is not an entry"
    assert by_name["a.bak"]["size_bytes"] == 100 and by_name["a.bak"]["type"] == "file"
    assert by_name["with space 'q'.bak"]["size_bytes"] == 7


def test_a_folder_that_is_not_there_is_said_so_not_an_error(monkeypatch):
    monkeypatch.setattr(L, "run", lambda host, command, **_k: {"exit_code": 0, "stdout": "NOFOLDER\n", "stderr": ""})
    answer = L.list_files({"host": {"runtime": "linux", "host": "192.0.2.30", "username": "u", "password": "p"},
                           "folder": "/mnt/nas/SRV-01"})
    assert answer == {"folder": "/mnt/nas/SRV-01", "folder_found": False, "files": [], "listed": 0, "truncated": False}


def test_a_listing_with_a_hole_in_it_is_a_failure_not_a_short_list(monkeypatch):
    monkeypatch.setattr(L, "run", lambda host, command, **_k: {
        "exit_code": 1, "stdout": "f\t1\t1.0\t/mnt/nas/SRV-01/a\0", "stderr": "find: Permission denied"})
    with pytest.raises(L.ListFilesError, match="Permission denied"):
        L.list_files({"host": {"runtime": "linux", "host": "192.0.2.30", "username": "u", "password": "p"},
                      "folder": "/mnt/nas/SRV-01"})


def test_a_link_is_listed_as_a_link_and_never_followed(tree, tmp_path):
    outside = tmp_path / "elsewhere"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "big.bak").write_bytes(b"z" * 1000)
    try:
        os.symlink(outside, tree / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot create a symbolic link without privilege")
    answer = L.list_files({"folder": str(tree)})
    kinds = {item["path"].replace("\\", "/").rsplit("/", 1)[-1]: item["type"] for item in answer["files"]}
    assert kinds["linked"] == "link" and "big.bak" not in kinds


def test_more_entries_than_asked_for_is_truncated_never_cut_silently(tree):
    answer = L.list_files({"folder": str(tree), "max_files": 1})
    assert answer["truncated"] is True and answer["listed"] == 2 and len(answer["files"]) == 1


@pytest.mark.parametrize("request_, words", [
    ({"folder": "relative/path"}, "absolute"),
    ({"folder": "/mnt/nas/../etc"}, "'..'"),
    ({"folder": ""}, "required"),
    ({"folder": "/mnt/nas", "paths": ["/x"]}, "unknown field"),
    ({"folder": "/mnt/nas", "max_files": 0}, "max_files"),
])
def test_a_malformed_request_is_refused(request_, words):
    with pytest.raises(L.ListFilesError, match=None) as caught:
        L.list_files(request_)
    assert words in str(caught.value)


# --------------------------------------------------------------------------- #
# 3. The field
# --------------------------------------------------------------------------- #
def test_the_field_is_written_on_and_read_as_stated():
    assert offload_rules.OFFLOAD_FIELDS["offload_repository_check"] is True
    stated = dict(offload_rules.OFFLOAD_FIELDS)
    assert offload_rules.parse_backup_offload(stated).repository_check is True
    assert offload_rules.parse_backup_offload({**stated, "offload_repository_check": False}).repository_check is False


def test_the_field_is_true_or_false_and_never_left_out():
    stated = dict(offload_rules.OFFLOAD_FIELDS)
    with pytest.raises(offload_rules.OffloadConfigError, match="offload_repository_check must be true or false"):
        offload_rules.parse_backup_offload({**stated, "offload_repository_check": "yes"})
    del stated["offload_repository_check"]
    with pytest.raises(offload_rules.OffloadConfigError, match="offload_repository_check"):
        offload_rules.parse_backup_offload(stated)


def test_upgrade_config_writes_it_into_an_entry_that_lacks_it():
    entry = {k: v for k, v in offload_rules.OFFLOAD_FIELDS.items() if k != "offload_repository_check"}
    document = {"backup_restore": {"backups": [{"backup_id": "B1", **entry}], "restores": []}}
    offload_rules.state_offload(document)
    assert document["backup_restore"]["backups"][0]["offload_repository_check"] is True


# --------------------------------------------------------------------------- #
# 4. The pass
# --------------------------------------------------------------------------- #
SRC, REPO = "SRV-01", "REPO-01"


@pytest.fixture
def catalog(tmp_path):
    store = BackupCatalogStore(tmp_path / "catalog.sqlite")
    store.initialize()
    return store


class _Repository:
    """The repository host: copies land here, and ``list-files`` lists it - never hashing."""

    def __init__(self) -> None:
        self.source: dict[str, bytes] = {}
        self.files: dict[str, bytes] = {}
        self.links: set[str] = set()
        self.calls: list[tuple[str, dict]] = []

    def run(self, command, request, **_kwargs):
        self.calls.append((command, request))
        if command == "file-checksum":
            files = self.source if request["host"]["host"] == "src" else self.files
            return {"files": [{"path": p, "sha256": _sha(files[p]), "size_bytes": len(files[p])}
                              for p in request["paths"] if p in files]}
        if command == "copy-files":
            lines = []
            for item in request["files"]:
                self.files[item["target"]] = self.source[item["source"]]
                lines.append({"source": item["source"], "target": item["target"], "ok": True,
                              "bytes_copied": item["size_bytes"], "error": ""})
            return {"files": lines}
        if command == "list-files":
            assert set(request) <= {"host", "folder", "timeout_seconds", "max_files"}, "listed, never hashed"
            prefix = request["folder"].rstrip("/") + "/"
            found = [{"path": p, "type": "file", "size_bytes": len(b)} for p, b in self.files.items()
                     if p.startswith(prefix)]
            found += [{"path": p, "type": "link", "size_bytes": 0} for p in self.links if p.startswith(prefix)]
            return {"folder": request["folder"], "folder_found": bool(found), "files": found,
                    "listed": len(found), "truncated": False}
        raise AssertionError(command)


@pytest.fixture
def repo(monkeypatch):
    fake = _Repository()
    monkeypatch.setattr(O.common_cli, "run", fake.run)

    class _Sealed:
        rows: list[dict] = []

        @classmethod
        def from_config(cls, _config):
            return cls()

        def files_for_server(self, *, server_id, repository_server_id):
            return list(self.rows)
    from dbabrain.db import repository_encryption_store
    monkeypatch.setattr(repository_encryption_store, "RepositoryEncryptionStore", _Sealed)
    fake.sealed = _Sealed
    return fake


def _entry(check=True):
    offload = BackupOffload(enabled=True, server_id=REPO, folder="/mnt/nas", copy_max_retry=1, scan="none",
                            delete_source=False, source_share="", source_username="", source_password_ref="",
                            repository_check=check)
    return O._Entry(backup_id="SRV01_MSSQL", server_id=SRC, db_type="sqlserver", backup_dir="/backup",
                    offload=offload, jobs=(SimpleNamespace(notify="BACKUP_GROUP"),))


_REAL_HOSTS = O._Hosts


def _hosts(entry):
    hosts = object.__new__(_REAL_HOSTS)
    hosts.entry = entry
    hosts.source_host = {"host": "src", "runtime": "linux"}
    hosts.repository_host = {"host": "repo", "runtime": "linux"}
    hosts.share = None
    hosts.copy_source = {"source": {"host": "src"}}
    hosts.target = {"host": "repo"}
    hosts.copy_errors = {}
    return hosts


def _one_proven_copy(catalog, repo, entry) -> str:
    data = b"a full backup"
    repo.source["/backup/APPDB/FULL/a.bak"] = data
    catalog.record_backup(run={"backup_id": entry.backup_id, "server_id": SRC, "engine": "sqlserver",
                               "started_at": "2026-11-10T01:00:00Z", "status": "DONE"},
                          files=[{"backup_id": entry.backup_id, "server_id": SRC, "database_name": "APPDB",
                                  "engine": "sqlserver", "file_name": "a.bak", "file_type": "FULL",
                                  "file_path": "/backup/APPDB/FULL/a.bak", "host_path": "/backup/APPDB/FULL/a.bak",
                                  "backup_ended_at": "2026-11-10T01:00:00Z", "size_bytes": len(data),
                                  "checksum_after_backup": _sha(data), "verdict": "ok", "offload_state": "pending",
                                  "on_disk": 1, "checksum_matches": 1}])
    rows = catalog.files_to_offload(backup_id=entry.backup_id)
    O.offload_entry(entry, catalog=catalog, hosts=_hosts(entry), rows=rows, sleep=lambda _s: None)
    return "/mnt/nas/SRV-01/APPDB/FULL/a.bak"


def test_a_folder_of_exactly_the_proven_copies_is_clean(catalog, repo):
    entry = _entry()
    copied = _one_proven_copy(catalog, repo, entry)
    answer = O.check_repository(entry, catalog=catalog, app_config=None, hosts=_hosts(entry))
    assert answer["clean"] is True and answer["expected"] == 1 and answer["folder"] == "/mnt/nas/SRV-01"
    assert copied in repo.files
    listed = [request for command, request in repo.calls if command == "list-files"]
    assert listed[-1]["folder"] == "/mnt/nas/SRV-01" and listed[-1]["host"] == {"host": "repo", "runtime": "linux"}


def test_a_file_dropped_into_the_folder_by_hand_is_found(catalog, repo):
    entry = _entry()
    _one_proven_copy(catalog, repo, entry)
    repo.files["/mnt/nas/SRV-01/APPDB/FULL/copied_by_hand.bak"] = b"who knows"
    answer = O.check_repository(entry, catalog=catalog, app_config=None, hosts=_hosts(entry))
    assert answer["clean"] is False and answer["counts"][RC.STRAY] == 1
    assert "copied_by_hand.bak" in answer["message"]


def test_a_link_in_the_folder_is_found(catalog, repo):
    entry = _entry()
    _one_proven_copy(catalog, repo, entry)
    repo.links.add("/mnt/nas/SRV-01/APPDB/elsewhere")
    answer = O.check_repository(entry, catalog=catalog, app_config=None, hosts=_hosts(entry))
    assert answer["counts"][RC.NOT_A_FILE] == 1


def test_another_servers_folder_is_never_this_ones_business(catalog, repo):
    entry = _entry()
    _one_proven_copy(catalog, repo, entry)
    repo.files["/mnt/nas/SRV-02/APPDB/FULL/theirs.bak"] = b"another server's"
    repo.files["/mnt/nas/SRV-010/APPDB/FULL/theirs.bak"] = b"a server whose id starts the same"
    assert O.check_repository(entry, catalog=catalog, app_config=None, hosts=_hosts(entry))["clean"] is True


def test_an_encrypted_copy_whose_plain_file_was_deleted_is_clean(catalog, repo):
    entry = _entry()
    copied = _one_proven_copy(catalog, repo, entry)
    repo.files[copied + ".age"] = repo.files.pop(copied) + b"sealed"
    repo.sealed.rows = [{"file_id": _file_id(catalog, entry, copied), "plain_path": copied,
                         "encrypted_path": copied + ".age", "encrypted_size_bytes": len(repo.files[copied + ".age"]),
                         "plain_deleted_at": "2026-11-10T02:00:00Z"}]
    try:
        assert O.check_repository(entry, catalog=catalog, app_config=None, hosts=_hosts(entry))["clean"] is True
    finally:
        repo.sealed.rows = []


def _file_id(catalog, entry, path):
    return catalog.repository_proofs(server_id=entry.server_id,
                                     repository_server_id=entry.offload.server_id)[path]["file_id"]


def test_an_expired_encrypted_copy_is_no_longer_expected_and_an_age_file_left_of_it_is_a_stray(catalog, repo):
    entry = _entry()
    copied = _one_proven_copy(catalog, repo, entry)
    file_id = _file_id(catalog, entry, copied)
    sealed = repo.files.pop(copied) + b"sealed"
    repo.sealed.rows = [{"file_id": file_id, "plain_path": copied, "encrypted_path": copied + ".age",
                         "encrypted_size_bytes": len(sealed), "plain_deleted_at": "2026-11-10T02:00:00Z"}]
    try:
        catalog.set_backup_file(file_id, offload_state="expired", offload_expired_at="2026-11-11T00:00:00Z")
        answer = O.check_repository(entry, catalog=catalog, app_config=None, hosts=_hosts(entry))
        assert answer["clean"] is True, "its .age file was deleted on expiry - not missing"

        repo.files[copied + ".age"] = sealed
        answer = O.check_repository(entry, catalog=catalog, app_config=None, hosts=_hosts(entry))
        assert answer["clean"] is False and answer["counts"]["stray"] == 1
    finally:
        repo.sealed.rows = []


def test_a_truncated_listing_is_not_a_check(catalog, repo, monkeypatch):
    entry = _entry()
    monkeypatch.setattr(O.common_cli, "run", lambda command, request, **_k: {
        "files": [{"path": "/mnt/nas/SRV-01/a", "type": "file", "size_bytes": 1}], "listed": 300000,
        "truncated": True})
    answer = O.check_repository(entry, catalog=catalog, app_config=None, hosts=_hosts(entry))
    assert answer["clean"] is False and "listed in part" in answer["message"]


@pytest.fixture
def one_run(monkeypatch, catalog, repo):
    """``_run_one`` with its run row, targets and secrets stood in - the check and the event are real."""
    events: list[dict] = []
    finished: list[dict] = []
    monkeypatch.setattr(O.schedule, "start_run", lambda **_k: (1, "2026-11-10T03:00:00Z"))
    monkeypatch.setattr(O.schedule, "finish_run", lambda **k: finished.append(k) or "2026-11-10T03:01:00Z")
    monkeypatch.setattr(O, "emit_backup_restore_event", lambda **k: events.append(k))
    monkeypatch.setattr(O, "resolve_backup_target", lambda job, **_k: SimpleNamespace(platform="linux"))
    monkeypatch.setattr(O, "resolve_ssh_target", lambda *_a, **_k: SimpleNamespace(access="ssh", platform="linux"))
    monkeypatch.setattr(O, "_load_secrets", lambda **_k: {})
    monkeypatch.setattr(O, "_Hosts", lambda entry, **_k: _hosts(entry))

    def run(entry):
        summary = {"ran": 0, "succeeded": 0, "failed": 0}
        return O._run_one(entry, rows=[], releasable=[], store=None, catalog=catalog, app_config=None, logger=None,
                          data_dir=None, key=None, key_base64=None, sleep=lambda _s: None, summary=summary)
    return SimpleNamespace(run=run, events=events, finished=finished)


def test_a_stray_file_fails_the_run_and_its_error_reaches_the_entrys_backup_restore_group(catalog, repo, one_run):
    entry = _entry()
    _one_proven_copy(catalog, repo, entry)
    repo.files["/mnt/nas/SRV-01/APPDB/LOG/stray.trn"] = b"stray"

    answer = one_run.run(entry)

    assert answer["status"] == "error"
    [event] = one_run.events
    assert event["phase"] == "ERROR" and event["level"] == "error" and event["notify"] == "BACKUP_GROUP"
    assert "stray.trn" in event["message"] and "not in the catalog" in event["message"]
    assert one_run.finished[-1]["status"] == "error"


def test_a_clean_folder_leaves_the_run_done_and_says_it_was_checked(catalog, repo, one_run):
    entry = _entry()
    _one_proven_copy(catalog, repo, entry)

    answer = one_run.run(entry)

    assert answer["status"] == "done" and answer["repository_check"]["clean"] is True
    assert "repository folder checked (1 file(s))" in one_run.events[-1]["message"]


def test_with_the_check_off_the_folder_is_never_listed(catalog, repo, one_run):
    entry = _entry(check=False)
    _one_proven_copy(catalog, repo, entry)
    repo.files["/mnt/nas/SRV-01/stray"] = b"x"
    repo.calls.clear()

    answer = one_run.run(entry)

    assert answer["status"] == "done" and not [c for c, _ in repo.calls if c == "list-files"]


class _RunTable:
    """The run table as offloads_running_beside reads it: job_code, status, metadata_json."""

    def __init__(self, rows):
        import sqlite3

        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE job_runs (job_code TEXT, status TEXT, metadata_json TEXT)")
        self.db.executemany("INSERT INTO job_runs VALUES (?, ?, ?)", rows)

    def connect(self):
        import contextlib

        return contextlib.nullcontext(self.db)


def _running(backup_id, server_id, status="RUNNING"):
    return (f"backup_restore.offload_job.{backup_id}", status, json.dumps({"server_id": server_id}))


def test_a_pass_beside_another_offload_of_its_server_does_not_judge_the_folder_mid_copy(catalog, repo, one_run,
                                                                                      monkeypatch):
    """2026-11-11: a LOG pass checked /mnt/nas/<server> while its server's FULL was being copied there and
    failed on the FULL's partial and not-yet-recorded files. The check waits for the next pass."""
    entry = _entry()
    _one_proven_copy(catalog, repo, entry)
    repo.files["/mnt/nas/SRV-01/APPDB/FULL/APPDB_FULL_2.bak.dbabrain_partial"] = b"half"
    store = _RunTable([_running("SRV01_FULL", "SRV-01")])
    real = O.offloads_running_beside
    monkeypatch.setattr(O, "offloads_running_beside", lambda _store, e: real(store, e))
    repo.calls.clear()

    answer = one_run.run(entry)

    assert answer["status"] == "done" and answer["repository_check"]["skipped"] is True
    assert not [c for c, _ in repo.calls if c == "list-files"], "not listed while another offload writes there"
    assert "not checked this pass" in one_run.events[-1]["message"]


def test_only_another_entry_of_the_same_server_running_now_holds_the_check_back():
    entry = SimpleNamespace(backup_id="SRV01_LOG", server_id="SRV-01")
    store = _RunTable([_running("SRV01_FULL", "SRV-01"), _running("SRV01_LOG", "SRV-01"),
                       _running("OTHER_FULL", "SRV-02"), _running("SRV01_DIFF", "SRV-01", status="DONE")])
    assert O.offloads_running_beside(store, entry) == ["SRV01_FULL"]
    assert O.offloads_running_beside(None, entry) == [], "a store that cannot say does not skip the check"
