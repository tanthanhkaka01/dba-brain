"""A backup the database wrote inside its container is offloaded from inside it (0.37.0).

Why it matters: the store's backups live in a Docker volume - on the host, under
/var/lib/docker/volumes, which only root may enter. Its first offload (2026-11-10) listed the
backup's host path as the SSH user, saw nothing, and failed "no file found to copy" for a 6 GB
backup that was there. The backup itself is taken by ``docker exec`` into the container, so the
offload reads it the same way: listed, hashed, copied (``docker exec ... cat``, held to its size and
sha256 like any other source) and deleted INSIDE the container, by the path the backup wrote.
"""

from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

import pytest

from dbabrain.backup_restore import offload
from dbabrain.common import filecopy

DATA = b"tar bytes " * 1000


class _Channel:
    def __init__(self, status):
        self.status = status
        self.closed = False

    def recv_exit_status(self):
        return self.status

    def close(self):
        self.closed = True


class _Out(io.BytesIO):
    def __init__(self, data, status=0):
        super().__init__(data)
        self.channel = _Channel(status)


class _Client:
    def __init__(self, data=DATA, *, cat_status=0, stat=None):
        self.data, self.cat_status, self.stat, self.sent = data, cat_status, stat, []

    def exec_command(self, command, **_k):
        self.sent.append(command)
        if " stat -c %s " in command:
            text = self.stat if self.stat is not None else str(len(self.data))
            return None, _Out(text.encode(), 0 if text.isdigit() else 1), _Out(b"No such file")
        return None, _Out(self.data, self.cat_status), _Out(b"cat: read error")


def test_a_file_in_the_container_is_read_by_its_path_there_and_held_to_its_facts():
    client = _Client()
    reader = filecopy._ContainerReader(client, "db_ops_store", "/var/lib/postgresql/backup/dbops/base/X_FULL/base.tar.zst")

    class _Target:
        def __init__(self):
            self.files = {}

        def open(self, path, mode):
            buffer = io.BytesIO()
            buffer.close = lambda: self.files.__setitem__(path, buffer.getvalue())
            return buffer

        def stat(self, path):
            raise FileNotFoundError(path)

        def mkdir(self, path):
            pass

        def rename(self, a, b):
            self.files[b] = self.files.pop(a)

        def posix_rename(self, a, b):
            self.rename(a, b)

    target = _Target()
    written, streamed = filecopy.copy_one(reader, target, "/mnt/nas/S/_instance/base/X_FULL/base.tar.zst",
                                          size_bytes=len(DATA), sha256=hashlib.sha256(DATA).hexdigest(),
                                          overwrite=False, stall=30)
    assert written == len(DATA) and streamed == hashlib.sha256(DATA).hexdigest()
    assert client.sent[0] == "docker exec db_ops_store stat -c %s -- /var/lib/postgresql/backup/dbops/base/X_FULL/base.tar.zst"
    assert client.sent[1].startswith("docker exec db_ops_store cat -- /var/lib/postgresql/")


def test_a_file_the_container_cannot_show_is_refused_by_name():
    with pytest.raises(filecopy.FileCopyError, match="cannot be read in the container"):
        filecopy._ContainerReader(_Client(stat="stat: cannot statx"), "db_ops_store", "/b/missing")


def test_a_cat_that_fails_fails_the_file_even_when_bytes_arrived():
    reader = filecopy._ContainerReader(_Client(cat_status=1), "db_ops_store", "/b/f")
    reader.read(len(DATA))
    assert "cat exited 1" in reader.finish()


def test_a_wrong_size_in_the_container_is_never_copied():
    reader = filecopy._ContainerReader(_Client(), "c", "/b/f")
    with pytest.raises(filecopy.FactMismatch):
        filecopy.copy_one(reader, None, "/t/f", size_bytes=len(DATA) + 1, sha256="0" * 64, overwrite=False, stall=30)


def test_the_offload_of_a_containerised_backup_lists_hashes_copies_and_deletes_inside_the_container(monkeypatch):
    import dbabrain.backup_restore.spec_builder as spec_builder

    monkeypatch.setattr(spec_builder, "host_block", lambda target, **_k: {
        "runtime": "linux", "host": target.host, "port": 22, "username": "dev", "password": "pw", "key_file": "",
        "access": "ssh"})
    entry = SimpleNamespace(backup_id="B", server_id="S", backup_dir="/var/lib/postgresql/backup/dbops",
                            offload=SimpleNamespace(source_share="", folder="/mnt/nas", hash_timeout_seconds=60,
                                                    server_id="REPO"))
    source = SimpleNamespace(host="192.0.2.15", container_name="db_ops_store")
    hosts = offload._Hosts(entry, source=source, repository=SimpleNamespace(host="192.0.2.18", container_name=""),
                           secrets={}, data_dir=None)
    assert hosts.source_host["runtime"] == "docker" and hosts.source_host["container"] == "db_ops_store"
    assert hosts.copy_source["source"]["container"] == "db_ops_store"
    assert hosts.repository_host["runtime"] == "linux"

    row = {"file_id": 1, "file_name": "a.bak", "file_type": "FULL", "database_name": "",
           "file_path": "/var/lib/postgresql/backup/dbops/base/a.bak",
           "host_path": "/var/lib/docker/volumes/v/_data/backup/dbops/base/a.bak",
           "size_bytes": 10, "checksum_after_backup": "a" * 64}
    request = offload.release_request(entry, row, hosts)
    assert request["path"] == row["file_path"], "deleted by its path in the container"
    assert request["must_be_under"] == "/var/lib/postgresql/backup/dbops"
    assert request["host"]["container"] == "db_ops_store"


def test_a_backup_on_the_host_itself_is_still_read_by_its_host_path(monkeypatch):
    import dbabrain.backup_restore.spec_builder as spec_builder

    monkeypatch.setattr(spec_builder, "host_block", lambda target, **_k: {
        "runtime": "linux", "host": target.host, "port": 22, "username": "u", "password": "", "key_file": "/k",
        "access": "ssh"})
    entry = SimpleNamespace(backup_id="B", server_id="S", backup_dir="/b",
                            offload=SimpleNamespace(source_share="", folder="/mnt/nas", hash_timeout_seconds=60))
    hosts = offload._Hosts(entry, source=SimpleNamespace(host="192.0.2.15", container_name=""),
                           repository=SimpleNamespace(host="192.0.2.18", container_name=""), secrets={}, data_dir=None)
    assert hosts.source_host["runtime"] == "linux" and "container" not in hosts.copy_source["source"]
