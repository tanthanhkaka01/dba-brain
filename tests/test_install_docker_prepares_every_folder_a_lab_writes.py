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

import json
import subprocess
from pathlib import Path

import pytest

from dbabrain.common import cli_docker_db
from dbabrain.common.docker_db import provisioner
from dbabrain.common.docker_db import remote_host as remote
from dbabrain.transport import common_cli
from dbabrain.sre import cli as sre_cli
from dbabrain.sre import remote as sre_remote
from dbabrain.lib import docker_db_registry as resolve


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

    def close(self):
        pass


def test_the_backup_mount_is_prepared_with_the_containers_dir():
    host = FakeHost()

    summary = remote.ensure_docker(host, sudo_password="pw", containers_dir="/opt/db_ops/containers",
                                   backup_mount="/opt/db_ops/backup")

    prepared = host.sudo_cmds[-1]
    assert "mkdir -p /opt/db_ops/containers /opt/db_ops/backup" in prepared
    assert "chown -R labuser: /opt/db_ops/containers" in prepared
    assert "chown labuser: /opt/db_ops/backup" in prepared
    assert summary["backup_mount"] == "/opt/db_ops/backup"


def test_the_backup_mount_is_handed_over_at_its_top_never_recursively():
    """A second lab on the same host must not take the first lab's backups away from its engine.

    On 2026-09-24 the SQL Server HA lab was built on a host whose single lab already backed up into
    the mount; `chown -R` made every existing backup folder the SSH user's, and SQL Server (uid
    10001) failed its next LOG and FULL backups with "Access is denied".
    """
    host = FakeHost()

    remote.ensure_docker(host, sudo_password="pw", containers_dir="/opt/db_ops/containers",
                         backup_mount="/opt/db_ops/backup")

    assert "chown -R labuser: /opt/db_ops/backup" not in host.sudo_cmds[-1]
    assert "-R labuser: /opt/db_ops/containers /opt/db_ops/backup" not in host.sudo_cmds[-1]


def test_a_lab_with_no_backup_mount_prepares_only_the_containers_dir():
    host = FakeHost()

    remote.ensure_docker(host, sudo_password="pw", containers_dir="/opt/db_ops/containers")

    assert "/opt/db_ops/backup" not in host.sudo_cmds[-1]


def test_a_preparation_that_fails_says_so_and_names_the_folders():
    with pytest.raises(remote.RemoteHostError, match="/opt/db_ops/backup") as raised:
        remote.ensure_docker(FakeHost(sudo_rc=1), sudo_password="wrong",
                             containers_dir="/opt/db_ops/containers", backup_mount="/opt/db_ops/backup")
    assert "a password is required" in str(raised.value)


def test_create_db_docker_asks_common_to_install_docker_with_the_resolved_login(monkeypatch, tmp_path):
    """sre's half since 1.37: it resolves the SSH password and hands `common` a login - it does
    not open the host itself."""
    sent = {}

    def fake_run(command, request, **kwargs):
        sent.update(request, _command=command, _stream=kwargs.get("stream_stderr"))
        return {"worker_host": "192.0.2.249", "compose_path": "/c/LAB_1433/docker-compose.yml",
                "summary": "ok", "status": "running"}

    monkeypatch.setattr(common_cli, "run", fake_run)
    monkeypatch.setattr(sre_remote, "resolve_remote_ssh_password", lambda **_: "pw")
    monkeypatch.setattr(resolve, "resolve_password_value", lambda *a, **k: ("sa-pw", "env:LAB_SA"))

    # A config of its own: a public checkout ships config.example.json and no config.json.
    config = tmp_path / "config.json"
    config.write_text((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8-sig"),
                      encoding="utf-8")

    code = sre_cli.main(["--config", str(config), "create-db-docker", "--name", "LAB_1433",
                         "--engine", "mssql", "--version", "2025-latest", "--password-ref", "LAB_SA",
                         "--remote-host", "192.0.2.249", "--remote-user", "labuser",
                         # This root states no lab folders, so the command line does: nothing
                         # in the code stands in for them.
                         "--containers-dir", "/opt/acme/containers", "--backup-mount", "/opt/acme/backup",
                         "--remote-password-ref", "LAB_SSH", "--install-docker", "--no-register"])

    assert code == 0
    assert sent["_command"] == "create-db-docker" and sent["_stream"] is True
    assert sent["install_docker"] is True
    assert sent["remote"] == {"host": "192.0.2.249", "port": 22, "username": "labuser",
                              "password": "pw", "key_file": ""}
    assert sent["password"] == "sa-pw" and sent["password_ref"] == "LAB_SA"


def test_create_db_docker_hands_ensure_docker_the_labs_own_backup_mount(monkeypatch):
    """`common`'s half: the folder prepared is the one this lab will mount, not a guess."""
    seen = {}

    def fake_ensure_docker(host, **kwargs):
        seen.update(kwargs)
        return {"host": "192.0.2.249", "already_present": True, "installed": False}

    monkeypatch.setattr(remote, "ensure_docker", fake_ensure_docker)
    monkeypatch.setattr(remote.RemoteUbuntuHost, "from_login", classmethod(lambda cls, login: FakeHost()))
    monkeypatch.setattr(provisioner, "provision", lambda spec, **_: {"name": spec.name})

    cli_docker_db._create({"name": "LAB_1433", "engine": "mssql", "version": "2025-latest",
                           "password_ref": "LAB_SA", "password": "pw", "install_docker": True,
                           # Both folders are the request's to state: the command assumes none.
                           "containers_dir": "/opt/acme/containers", "backup_mount": "/opt/acme/backup",
                           "remote": {"host": "192.0.2.249", "username": "labuser", "password": "pw"}})

    assert seen["backup_mount"] == "/opt/acme/backup"
    assert seen["containers_dir"] == "/opt/acme/containers"
    assert seen["sudo_password"] == "pw", "the SSH password is the sudo password unless told otherwise"


def test_a_dry_run_never_installs_docker(monkeypatch):
    """The sre command used to run ensure_docker before looking at --dry-run."""
    monkeypatch.setattr(remote, "ensure_docker", lambda *a, **k: pytest.fail("installed on a dry run"))
    monkeypatch.setattr(remote.RemoteUbuntuHost, "from_login",
                        classmethod(lambda cls, login: pytest.fail("connected on a dry run")))

    result = cli_docker_db._create({"name": "LAB_1433", "engine": "mssql", "version": "2025-latest",
                                    "install_docker": True, "dry_run": True,
                                    "containers_dir": "/opt/acme/containers",
                                    "backup_mount": "/opt/acme/backup",
                                    "remote": {"host": "192.0.2.249", "username": "labuser"}})

    assert result["dry_run"] is True and result["docker"] is None


# --------------------------------------------------------------------------- #
# Where a lab's folders come from: a flag, else the node's own - never the code (0.29.0)
# --------------------------------------------------------------------------- #
def _create_through_the_cli(monkeypatch, tmp_path, *extra, folders=None):
    sent = {}

    def fake_run(command, request, **kwargs):
        sent.update(request, _command=command)
        return {"worker_host": "192.0.2.249", "compose_path": "/c/LAB_1433/docker-compose.yml",
                "summary": "ok", "status": "running"}

    monkeypatch.setattr(common_cli, "run", fake_run)
    monkeypatch.setattr(sre_remote, "resolve_remote_ssh_password", lambda **_: "pw")
    monkeypatch.setattr(resolve, "resolve_password_value", lambda *a, **k: ("sa-pw", "env:LAB_SA"))
    config = tmp_path / "config.json"
    config.write_text((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8-sig"),
                      encoding="utf-8")
    if folders is not None:
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "data" / "sre_config.json").write_text(
            json.dumps({"sre": {"docker_db": folders}}), encoding="utf-8")
    code = sre_cli.main(["--config", str(config), "create-db-docker", "--data-dir", str(tmp_path / "data"),
                         "--name", "LAB_1433", "--engine", "mssql", "--version", "2025-latest",
                         "--password-ref", "LAB_SA", "--remote-host", "192.0.2.249",
                         "--remote-user", "labuser", "--remote-password-ref", "LAB_SSH", "--no-register",
                         *extra])
    return code, sent


def test_a_lab_is_built_under_the_folders_its_node_states(monkeypatch, tmp_path):
    """The command that builds the lab reads no configuration, so the app puts the node's own
    folders in the request - the ones every lab that node already has is under."""
    code, sent = _create_through_the_cli(monkeypatch, tmp_path, folders={
        "containers_dir": "/opt/db_ops/containers", "backup_mount": "/opt/db_ops/backup"})

    assert code == 0
    assert sent["containers_dir"] == "/opt/db_ops/containers"
    assert sent["backup_mount"] == "/opt/db_ops/backup"


def test_a_flag_still_decides_over_what_the_node_states(monkeypatch, tmp_path):
    code, sent = _create_through_the_cli(
        monkeypatch, tmp_path, "--containers-dir", "/srv/labs", "--backup-mount", "-",
        folders={"containers_dir": "/opt/db_ops/containers", "backup_mount": "/opt/db_ops/backup"})

    assert code == 0
    assert sent["containers_dir"] == "/srv/labs"
    assert sent["backup_mount"] == "", "the skip sentinel asks for no mount"


def test_a_node_that_states_no_folder_is_refused_before_anything_is_asked_of_a_host(monkeypatch, tmp_path):
    """Until 0.29.0 this took a folder the code held as a constant - a folder nobody had chosen."""
    code, sent = _create_through_the_cli(monkeypatch, tmp_path)

    assert code != 0
    assert "_command" not in sent, "refused by the spec, before the command that reaches the host"
