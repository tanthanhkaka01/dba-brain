"""`install_docker = yes` prepares every folder a lab writes, with the sudo it already has.

On 2026-09-24 `/spbot_create_db_docker ... install_docker=yes` failed on a lab host with

    Cannot create /opt/db_ops/backup on 192.0.2.249 as labuser: [Errno 13] Permission denied

from a user with full sudo rights. The preparation step used sudo to create
`/opt/db_ops/containers` and hand it to the SSH user - leaving `/opt/db_ops` root-owned - and the
backup bind mount, added to the tool later, was created afterwards over SFTP as that plain user,
which cannot write there. The same step also never looked at its own exit code, so a sudo that
failed surfaced only later, as an SFTP error nobody could explain.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from db_ops.sre import cli as sre_cli
from db_ops.sre import remote
from db_ops.sre.docker_db.models import DEFAULT_BACKUP_MOUNT


class FakeHost:
    def __init__(self, *, sudo_rc=0):
        self.user, self.host, self.sudo_rc = "labuser", "192.0.2.249", sudo_rc
        self.sudo_cmds: list[str] = []

    def run(self, argv, *, capture_output=False, **_):
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    def run_sudo(self, command_str, sudo_password, *, capture_output=True):
        self.sudo_cmds.append(command_str)
        return subprocess.CompletedProcess(args=command_str, returncode=self.sudo_rc, stdout="",
                                           stderr="sudo: a password is required")

    def reconnect(self):
        pass


def test_the_backup_mount_is_prepared_with_the_containers_dir():
    host = FakeHost()

    summary = remote.ensure_docker(host, sudo_password="pw", containers_dir="/opt/db_ops/containers",
                                   backup_mount="/opt/db_ops/backup")

    prepared = host.sudo_cmds[-1]
    assert "mkdir -p /opt/db_ops/containers /opt/db_ops/backup" in prepared
    assert "chown -R labuser: /opt/db_ops/containers /opt/db_ops/backup" in prepared
    assert summary["backup_mount"] == "/opt/db_ops/backup"


def test_a_lab_with_no_backup_mount_prepares_only_the_containers_dir():
    host = FakeHost()

    remote.ensure_docker(host, sudo_password="pw", containers_dir="/opt/db_ops/containers")

    assert "/opt/db_ops/backup" not in host.sudo_cmds[-1]


def test_a_preparation_that_fails_says_so_and_names_the_folders():
    with pytest.raises(remote.RemoteHostError, match="/opt/db_ops/backup") as raised:
        remote.ensure_docker(FakeHost(sudo_rc=1), sudo_password="wrong",
                             containers_dir="/opt/db_ops/containers", backup_mount="/opt/db_ops/backup")
    assert "a password is required" in str(raised.value)


def test_create_db_docker_hands_ensure_docker_the_labs_own_backup_mount(monkeypatch, tmp_path):
    """The wiring: the folder prepared is the one this lab will mount, not a guess."""
    seen = {}

    def fake_ensure_docker(host, **kwargs):
        seen.update(kwargs)
        return {"host": "192.0.2.249", "already_present": True, "installed": False}

    monkeypatch.setattr(remote, "ensure_docker", fake_ensure_docker)
    monkeypatch.setattr(remote, "RemoteUbuntuHost", lambda *args, **kwargs: FakeHost())
    monkeypatch.setattr(remote, "resolve_remote_ssh_password", lambda **_: "pw")
    monkeypatch.setattr(sre_cli, "provision", lambda spec, **_: 0)

    # A config of its own: a public checkout ships config.example.json and no config.json.
    config = tmp_path / "config.json"
    config.write_text((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8-sig"),
                      encoding="utf-8")

    sre_cli.main(["--config", str(config), "create-db-docker", "--name", "LAB_1433", "--engine", "mssql",
                  "--version", "2025-latest", "--password-ref", "LAB_SA",
                  "--remote-host", "192.0.2.249", "--remote-user", "labuser",
                  "--remote-password-ref", "LAB_SSH", "--install-docker"])

    assert seen["backup_mount"] == DEFAULT_BACKUP_MOUNT
