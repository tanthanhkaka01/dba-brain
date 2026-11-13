"""A backup is encrypted on its way to the repository: one read, and no plain copy ever lands (1.330).

Why it matters: on 2026-11-11 a 52.8 GB SQL Server FULL took three hours from the backup's end to its
release - the bytes were read six to seven times in sequence: copied, re-hashed on the repository,
re-hashed and encrypted there, the ``.age`` hashed, decrypted back. The operator approved the one
stream the same day: read at the source -> sha256 of the plain bytes -> age -> ``<file>.age`` on the
repository, its sha256 taken on the way. What must not change is what made the old way safe: the plain
bytes held to the backup's own size and sha256 before anything is kept, nothing at the name unless it
is whole, a key at its end never encrypting, and the proof that the ``.age`` decrypts back to the backup
left to the caller.
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest

from dbabrain.common import filecopy
from dbabrain.lib import repository_keys as K

pyrage = pytest.importorskip("pyrage")
from pyrage import x25519  # noqa: E402

DATA = b"backup page " * 50_000
SHA = hashlib.sha256(DATA).hexdigest()
NOW = datetime(2026, 11, 11, 7, 0, tzinfo=timezone.utc)


class _Reader:
    def __init__(self, data=DATA, size=None, failure=""):
        self.stream, self.size, self.failure = io.BytesIO(data), size, failure

    def read(self, count):
        return self.stream.read(count)

    def finish(self):
        return self.failure

    def close(self):
        return ""


class _Target:
    """An SFTP target as a dict: what is at each name."""

    def __init__(self, files=None):
        self.files = dict(files or {})

    def open(self, path, mode):
        buffer = io.BytesIO()
        buffer.close = lambda: self.files.__setitem__(path, buffer.getvalue())
        return buffer

    def stat(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return type("S", (), {"st_size": len(self.files[path])})()

    def mkdir(self, path):
        pass

    def rename(self, a, b):
        if b in self.files:
            raise OSError("exists")
        self.files[b] = self.files.pop(a)

    def posix_rename(self, a, b):
        self.files[b] = self.files.pop(a)


@pytest.fixture
def key():
    identity = x25519.Identity.generate()
    return identity, {"recipients": [str(identity.to_public())], "key_id": 3}


def test_the_file_lands_only_as_its_age_and_decrypts_back_to_the_backups_bytes(key):
    identity, encrypt = key
    target = _Target()

    facts = filecopy.copy_one_encrypted(_Reader(), target, "/mnt/nas/S/DB/FULL/DB_FULL.bak", size_bytes=len(DATA),
                                        sha256=SHA, overwrite=False, encrypt=encrypt, stall=30)

    assert list(target.files) == ["/mnt/nas/S/DB/FULL/DB_FULL.bak.age"], "no plain copy, no partial left"
    sealed = target.files["/mnt/nas/S/DB/FULL/DB_FULL.bak.age"]
    assert pyrage.decrypt(sealed, [identity]) == DATA
    assert facts["bytes_copied"] == len(DATA) and facts["sha256_streamed"] == SHA
    assert facts["encrypted_size_bytes"] == len(sealed)
    assert facts["encrypted_sha256"] == hashlib.sha256(sealed).hexdigest() and facts["key_id"] == 3
    assert facts["encrypted_path"].endswith(".bak.age") and facts["age_version"].startswith("pyrage ")


@pytest.mark.parametrize("data, size, words", [
    (DATA[:-1], None, "bytes streamed"),               # short: the stream is not the backup
    (DATA[:-1] + b"X", None, "hash to"),               # same size, other bytes
    (DATA, len(DATA) + 5, "not the"),                  # the reader's own size says it is another file
], ids=["short", "other-bytes", "other-size"])
def test_bytes_that_are_not_the_backups_never_land(key, data, size, words):
    target = _Target()
    with pytest.raises(filecopy.FactMismatch, match=words):
        filecopy.copy_one_encrypted(_Reader(data, size=size), target, "/r/F.bak", size_bytes=len(DATA), sha256=SHA,
                                    overwrite=False, encrypt=key[1], stall=30)
    assert "/r/F.bak.age" not in target.files and "/r/F.bak" not in target.files


def test_a_stream_cut_half_way_is_never_put_in_place(key):
    target = _Target()
    with pytest.raises(OSError, match="smbclient died"):
        filecopy.copy_one_encrypted(_Reader(failure="smbclient died"), target, "/r/F.bak", size_bytes=len(DATA),
                                    sha256=SHA, overwrite=False, encrypt=key[1], stall=30)
    assert "/r/F.bak.age" not in target.files


def test_an_age_already_at_the_name_is_never_replaced_without_overwrite(key):
    target = _Target({"/r/F.bak.age": b"an earlier copy"})
    with pytest.raises(OSError):
        filecopy.copy_one_encrypted(_Reader(), target, "/r/F.bak", size_bytes=len(DATA), sha256=SHA,
                                    overwrite=False, encrypt=key[1], stall=30)
    assert target.files["/r/F.bak.age"] == b"an earlier copy"


def test_the_encrypt_block_is_checked_and_a_key_at_its_end_never_encrypts(key):
    good = {"recipients": key[1]["recipients"], "key_id": 3, "key_expires_at": K.time_text(NOW + timedelta(days=1))}
    assert filecopy.encryption_of({"encrypt": good}, now=lambda: NOW) == {"recipients": good["recipients"], "key_id": 3}
    assert filecopy.encryption_of({}, now=lambda: NOW) is None
    with pytest.raises(filecopy.FileCopyError, match="expired"):
        filecopy.encryption_of({"encrypt": {**good, "key_expires_at": K.time_text(NOW)}}, now=lambda: NOW)
    for bad in ({**good, "recipients": ["AGE-SECRET-KEY-1XYZ"]}, {**good, "key_id": 0}, {**good, "identity": "x"}):
        with pytest.raises(filecopy.FileCopyError):
            filecopy.encryption_of({"encrypt": bad}, now=lambda: NOW)


# --------------------------------------------------------------------------- #
# The pass: copied encrypted, proven by decrypting back, sealed in the same pass
# --------------------------------------------------------------------------- #
from types import SimpleNamespace  # noqa: E402

from dbabrain.backup_restore import offload as O  # noqa: E402
from dbabrain.backup_restore import repository_encryption as E  # noqa: E402
from dbabrain.common import repoencrypt  # noqa: E402
from dbabrain.db.backup_catalog import BackupCatalogStore  # noqa: E402
from dbabrain.db.repository_encryption_store import RepositoryEncryptionStore  # noqa: E402
from tests.test_a_repository_folder_holds_exactly_the_proven_copies import SRC, _entry, _hosts  # noqa: E402

BACKUP = b"a full backup of APPDB " * 4000
SOURCE_PATH = "/backup/APPDB/FULL/a.bak"
TARGET = "/mnt/nas/SRV-01/APPDB/FULL/a.bak"


class _Hosts2:
    """Source and repository as dicts; copy-files encrypts for real; verify-encrypted-file is the real one."""

    def __init__(self, identity):
        self.identity = identity
        self.source = {SOURCE_PATH: BACKUP}
        self.files: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.corrupt = False

    def run(self, command, request, **_k):
        self.calls.append(command)
        if command == "file-checksum":
            files = self.source if request["host"]["host"] == "src" else self.files
            return {"files": [{"path": p, "sha256": hashlib.sha256(files[p]).hexdigest(), "size_bytes": len(files[p])}
                              for p in request["paths"] if p in files]}
        if command == "copy-files":
            assert request["encrypt"]["key_id"] == 1 and "identity" not in json_text(request)
            lines = []
            for item in request["files"]:
                sealed = pyrage.encrypt(self.source[item["source"]], [x25519.Recipient.from_str(r)
                                                                      for r in request["encrypt"]["recipients"]])
                if self.corrupt:
                    sealed = pyrage.encrypt(b"other bytes", [self.identity.to_public()])
                self.files[item["target"] + ".age"] = sealed
                lines.append({"source": item["source"], "target": item["target"], "ok": True, "error": "",
                              "bytes_copied": item["size_bytes"], "encrypted_path": item["target"] + ".age",
                              "encrypted_size_bytes": len(sealed),
                              "encrypted_sha256": hashlib.sha256(sealed).hexdigest(), "key_id": 1,
                              "age_version": "pyrage test"})
            return {"files": lines, "encrypted": True}
        raise AssertionError(command)

    def run_allowing_failure(self, command, request, **_k):
        self.calls.append(command)
        assert command == "verify-encrypted-file"
        try:
            return True, repoencrypt.verify_encrypted_file(
                request, open_reader=lambda _h, p: io.BytesIO(self.files[p])), ""
        except repoencrypt.RepoEncryptError as exc:
            return False, {}, str(exc)


def json_text(value) -> str:
    import json

    return json.dumps(value)


@pytest.fixture
def world(tmp_path, monkeypatch):
    identity = x25519.Identity.generate()
    fake = _Hosts2(identity)
    monkeypatch.setattr(O.common_cli, "run", fake.run)
    monkeypatch.setattr(E.common_cli, "run", fake.run)
    monkeypatch.setattr(E.common_cli, "run_allowing_failure", fake.run_allowing_failure)
    catalog = BackupCatalogStore(tmp_path / "catalog.sqlite")
    catalog.initialize()
    catalog.record_backup(run={"backup_id": "SRV01_MSSQL", "server_id": SRC, "engine": "sqlserver",
                               "started_at": "2026-11-11T04:17:00Z", "status": "DONE"},
                          files=[{"backup_id": "SRV01_MSSQL", "server_id": SRC, "database_name": "APPDB",
                                  "engine": "sqlserver", "file_name": "a.bak", "file_type": "FULL",
                                  "file_path": SOURCE_PATH, "host_path": SOURCE_PATH,
                                  "backup_ended_at": "2026-11-11T04:19:00Z", "size_bytes": len(BACKUP),
                                  "checksum_after_backup": hashlib.sha256(BACKUP).hexdigest(), "verdict": "ok",
                                  "offload_state": "pending", "on_disk": 1, "checksum_matches": 1}])
    store = RepositoryEncryptionStore(tmp_path / "enc.sqlite")
    key = {"key_id": 1, "recipient": str(identity.to_public()), "encrypt_until": "2027-02-09T04:00:00Z"}
    sealing = E.Sealing(store=store, key=key, identity=str(identity))
    return SimpleNamespace(fake=fake, catalog=catalog, store=store, sealing=sealing, identity=identity)


def _offload(world):
    entry = _entry(check=False)
    hosts = _hosts(entry)
    rows = world.catalog.files_to_offload(backup_id=entry.backup_id)
    return O.offload_entry(entry, catalog=world.catalog, hosts=hosts, rows=rows, sleep=lambda _s: None,
                           sealing=world.sealing)


def test_a_pass_copies_encrypted_proves_by_decrypting_back_and_seals_the_row_in_the_same_pass(world):
    answer = _offload(world)

    assert answer["verified"] == 1 and answer["sealed"]["encrypted"] == 1 and not answer["sealed"]["failed"]
    assert list(world.fake.files) == [TARGET + ".age"], "the plain copy never landed on the repository"
    assert pyrage.decrypt(world.fake.files[TARGET + ".age"], [world.identity]) == BACKUP
    [recorded] = world.store.files_of(1)
    assert recorded["plain_path"] == TARGET and recorded["proven_at"] and recorded["plain_deleted_at"]
    assert recorded["plain_sha256"] == hashlib.sha256(BACKUP).hexdigest()
    assert 1 in world.store.done_rows(backup_id="SRV01_MSSQL"), "the after-copy encryption has nothing left"
    assert "verify-encrypted-file" in world.fake.calls and "encrypt-file" not in world.fake.calls
    # The repository is never asked to re-hash a plain copy, and no plain file is ever deleted there.
    assert "delete-verified-file" not in world.fake.calls


def test_an_age_that_does_not_decrypt_to_the_backup_is_never_a_verified_copy(world):
    world.fake.corrupt = True

    answer = _offload(world)

    assert answer["verified"] == 0 and answer["failed"]
    assert world.store.files_of(1) == [] and not world.store.done_rows(backup_id="SRV01_MSSQL")
    row = world.catalog.files_to_offload(backup_id="SRV01_MSSQL", retry_before="9999")
    assert row and row[0]["offload_state"] == "failed"


def test_a_whole_age_an_earlier_pass_left_is_proven_and_adopted_without_copying_again(world):
    world.fake.files[TARGET + ".age"] = pyrage.encrypt(BACKUP, [world.identity.to_public()])

    answer = _offload(world)

    assert answer["verified"] == 1 and "copy-files" not in world.fake.calls
    assert world.store.files_of(1)[0]["proven_at"]


def test_the_step_3_line_says_the_copies_were_sealed_on_the_way():
    merged = O._merge_encrypt({"rows": 1, "encrypted": 1, "files": 1, "failed": [], "waiting": [], "key_id": 2,
                               "on_the_way": True}, {"rows": 0, "encrypted": 0, "files": 0, "failed": []})
    assert merged["encrypted"] == 1 and merged["key_id"] == 2 and merged["on_the_way"]
