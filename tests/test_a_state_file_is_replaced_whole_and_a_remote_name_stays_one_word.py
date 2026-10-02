"""State and secret files are replaced whole, and an operator's name reaches a remote shell as one word.

Three robustness gaps of the 0.25.0 review:

* **Secret files were written truncate-then-write, at the umask's mode** (F6.2). The deploy's secret
  merge wrote the encrypted store and the plaintext source with ``write_text``: a crash mid-write left
  an empty store, and a newly created plaintext source - every credential, in clear - was 0644. They go
  through ``secret_text.write_secret_file`` now: atomic, 0600 on create.
* **Other state files were truncated first too** (B9.2) - the docker-db registry, the store
  declaration, the daemon's state file, the scaffolded guide. A failure between the truncate and the
  last byte lost the file; now the old one stays until the new one is complete.
* **Names went into remote shell lines unquoted** (F6.3). ``remote_dir``, ``container`` and ``user``
  come from the operator's flags, so this is robustness rather than injection: a directory with a
  space in it broke the deploy half way, after the worker had been half prepared.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path

import pytest

from db_ops.control import deploy, worker_data, worker_status
from db_ops.lib import docker_db_registry
from db_ops.lib.secret_text import encrypt_secret_text, write_secret_file

KEY = "a-test-only-passphrase"
SPACED_DIR = "/opt/db ops/worker"
CONTAINER = "db_ops daemon"


def test_the_deploy_s_secret_merge_writes_both_files_through_the_secret_writer(tmp_path, monkeypatch):
    node, master, plaintext = tmp_path / "node.json", tmp_path / "master.json", tmp_path / "plain.json"
    node.write_text(json.dumps(encrypt_secret_text({"FROM_NODE": "1"}, KEY)), encoding="utf-8")
    master.write_text(json.dumps(encrypt_secret_text({"FROM_MASTER": "2"}, KEY)), encoding="utf-8")
    written: list[Path] = []
    monkeypatch.setattr(worker_data, "write_secret_file",
                        lambda path, text: (written.append(Path(path)), write_secret_file(Path(path), text)))

    worker_data._merge_secret_stores_from_files(node=node, master=master, key=KEY, plaintext=plaintext)

    assert set(written) == {master, plaintext}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_new_secret_file_is_readable_by_its_owner_only(tmp_path):
    path = tmp_path / "secret_text.json"

    write_secret_file(path, "{}\n")

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_registry_write_that_fails_leaves_the_registry_it_had(tmp_path):
    path = tmp_path / "docker_db_connections.json"
    before = json.dumps({"docker_db_connections": [{"name": "lab"}]}, indent=2) + "\n"
    path.write_text(before, encoding="utf-8")

    with pytest.raises(TypeError):
        docker_db_registry.save_registry(path, {"docker_db_connections": [object()]})

    assert path.read_text(encoding="utf-8") == before


class _Client:
    def close(self):
        pass


def _one_word(command: str, name: str) -> bool:
    """Does ``name`` survive the shell's word splitting as (part of) a single word?"""
    return any(name in word for word in shlex.split(command))


def test_a_deploy_into_a_directory_with_a_space_keeps_it_one_word(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / deploy.IMAGE_TAR_NAME).write_bytes(b"")
    commands: list[str] = []
    monkeypatch.setattr(deploy, "ssh_connect", lambda *a, **k: _Client())
    monkeypatch.setattr(deploy, "ssh_run", lambda client, command, **k: commands.append(command) or 0)
    monkeypatch.setattr(deploy, "ssh_capture", lambda client, command, **k: (commands.append(command), (1, "", ""))[1])
    monkeypatch.setattr(deploy, "sftp_put_tree", lambda *a, **k: None)

    deploy.copy_bundle(host="192.0.2.10", user="ops", password="pw", remote_dir=SPACED_DIR, bundle=bundle)
    deploy.reclaim_worker_files(host="192.0.2.10", user="ops", password="pw", remote_dir=SPACED_DIR)

    naming = [command for command in commands if "db ops" in command]
    assert naming, commands
    assert all(_one_word(command, SPACED_DIR) for command in naming), naming


def test_a_container_name_reaches_docker_as_one_word(monkeypatch):
    commands: list[str] = []

    def capture(client, command, **_kwargs):
        commands.append(command)
        return (0, "db_ops | Up", "") if command.startswith("docker ps") else (0, "", "")

    monkeypatch.setattr(worker_status, "ssh_connect", lambda *a, **k: _Client())
    monkeypatch.setattr(worker_status, "ssh_capture", capture)

    worker_status.run_worker_status(host="192.0.2.10", user="ops", password="pw", container=CONTAINER)

    naming = [command for command in commands if "db_ops daemon" in command]
    assert len(naming) >= 3, commands
    assert all(_one_word(command, CONTAINER) for command in naming), naming
